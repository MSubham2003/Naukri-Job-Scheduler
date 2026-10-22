"""Naukri search + apply crawler.

Design goals:
* never lose the search-results page (job details open in their own tab);
* one bad job must not abort the run;
* every decision is logged and counted in the run report;
* dry-run mode exercises the whole flow without submitting applications.
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import quote_plus

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from .db import JobDatabase
from .forms import FormResolver
from .logging_setup import get_logger
from .matcher import JobMatcher

logger = get_logger("crawler")

SCREENSHOT_DIR = Path("reports/screenshots")
CARD_SELECTORS = "div.cust-job-tuple, div.srp-jobtuple-wrapper"
APPLY_SELECTORS = (
    "button#apply-button",
    "button.apply-button",
    "a#apply-button",
    "button:has-text('Apply on Naukri')",
    "div.apply-button-container button",
    "button:has-text('Apply')",
)
EXTERNAL_SELECTORS = (
    "button:has-text('Apply on company site')",
    "a:has-text('Apply on company site')",
    "button:has-text('Company site')",
    "a[target='_blank']:has-text('Apply')",
)
MODAL_SELECTOR = (
    ".apply-message-container, .chatbot-container, div[class*='apply-modal'], "
    "div[class*='bot-container'], div[class*='drawer']"
)
JD_SELECTORS = (
    "div.job-desc",
    "div.jd-desc",
    "section[class*='job-desc']",
    "div[class*='job-description']",
    "div[class*='jd']",
)


# ---------------------------------------------------------------------------
# Helpers (pure where possible so they are easy to test)
# ---------------------------------------------------------------------------
def extract_clean_job_id(href: str, card_id_attr: str = "") -> str:
    """Extract a stable job id from a Naukri URL or a card attribute."""
    if card_id_attr and card_id_attr.strip():
        return card_id_attr.strip()
    if not href:
        return ""
    match = re.search(r"-(\d{8,16})(?:[/?#]|$)", href)
    if match:
        return match.group(1)
    match = re.search(r"jobId=(\d+)", href)
    if match:
        return match.group(1)
    cleaned = href.split("?")[0].split("#")[0].strip("/")
    return cleaned.split("/")[-1] if cleaned else ""


def build_search_url(query: str, min_experience: float = 0, location: str = "") -> str:
    slug = quote_plus(query.lower().replace(" ", "-"))
    url = f"https://www.naukri.com/{slug}-jobs?k={quote_plus(query)}"
    if min_experience:
        url += f"&experience={min_experience:g}"
    if location:
        url += f"&l={quote_plus(location)}"
    return url


def _iter_queries(config: Dict[str, Any]) -> Iterable[Dict[str, str]]:
    for entry in config.get("search_queries", []):
        if isinstance(entry, str):
            yield {"query": entry, "location": ""}
        elif isinstance(entry, dict) and entry.get("query"):
            yield {"query": str(entry["query"]), "location": str(entry.get("location", ""))}


def _sleep_range(config: Dict[str, Any], key: str, default: Tuple[float, float]) -> None:
    delays = config.get("delays", {})
    low = float(delays.get(f"{key}_min_sec", default[0]))
    high = float(delays.get(f"{key}_max_sec", default[1]))
    if high < low:
        low, high = high, low
    seconds = random.uniform(low, high)
    logger.debug("Sleeping %.1fs (%s)", seconds, key)
    time.sleep(seconds)


def _safe_text(locator, default: str = "") -> str:
    try:
        if locator.count() == 0:
            return default
        return locator.first.inner_text().strip()
    except Exception:
        return default


def _screenshot(page, name: str) -> None:
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        page.screenshot(path=str(path))
        logger.info("Saved screenshot: %s", path)
    except Exception as exc:
        logger.debug("Screenshot failed: %s", exc)


def _goto(page: Page, url: str, attempts: int = 2, timeout: int = 45000) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return True
        except PlaywrightError as exc:
            logger.warning("Navigation failed (%d/%d) for %s: %s", attempt, attempts, url, exc)
            if attempt < attempts:
                time.sleep(2 * attempt)
    return False


def _first_visible(page: Page, selectors: Iterable[str]):
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                return locator.first
        except Exception:
            continue
    return None


BLOCK_MARKERS = ("access denied", "you don't have permission to access", "errors.edgesuite.net")


def _is_blocked(page: Page) -> bool:
    """True when the WAF served an Akamai 'Access Denied' page."""
    try:
        body = page.locator("body").inner_text(timeout=3000)[:800].lower()
    except Exception:
        return False
    return any(marker in body for marker in BLOCK_MARKERS)


def _extract_jd(detail_page: Page, limit: int = 4000) -> str:
    for selector in JD_SELECTORS:
        try:
            locator = detail_page.locator(selector)
            if locator.count() > 0:
                text = locator.first.inner_text().strip()
                if text:
                    return text[:limit]
        except Exception:
            continue
    return ""


def _wait_for_apply_controls(detail_page: Page, timeout_ms: int = 9000) -> Tuple[str, Optional[Any]]:
    """Poll until the apply button or an external-apply marker appears.

    Returns ``("external"|"apply"|"none", element)``. The posting page is
    client-rendered, so a single check right after navigation is flaky.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if _first_visible(detail_page, EXTERNAL_SELECTORS) is not None:
            return "external", None
        button = _first_visible(detail_page, APPLY_SELECTORS)
        if button is not None:
            return "apply", button
        if time.monotonic() >= deadline:
            return "none", None
        detail_page.wait_for_timeout(700)


def _is_already_applied(detail_page: Page) -> bool:
    try:
        button = _first_visible(detail_page, APPLY_SELECTORS)
        if button is None:
            return False
        text = button.inner_text().strip().lower()
        return "applied" in text
    except Exception:
        return False


def _submit_and_verify(detail_page: Page, modal) -> bool:
    submit = modal.locator(
        "button:has-text('Submit'), button:has-text('Save'), button:has-text('Apply')"
    )
    if submit.count() == 0:
        logger.warning("Screener modal has no submit button.")
        return False
    submit.first.click()
    detail_page.wait_for_timeout(3000)
    try:
        confirmation = detail_page.locator(
            "text=/applied successfully|application submitted|successfully applied/i"
        )
        if confirmation.count() > 0:
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Main crawler
# ---------------------------------------------------------------------------
def run_crawler(
    page: Page,
    db: JobDatabase,
    matcher: JobMatcher,
    form_resolver: FormResolver,
    config: Dict[str, Any],
    dry_run: bool = False,
    limit_override: Optional[int] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Search each configured query, apply to matching jobs, return a report."""
    started = time.time()
    max_applies = int(limit_override if limit_override is not None else config.get("max_applications_per_run", 15))
    max_per_query = int(config.get("max_applications_per_query", max_applies))
    applied_count = 0
    seen_in_this_run: set[str] = set()

    report: Dict[str, Any] = {
        "run_id": run_id or datetime.now().strftime("%Y%m%d_%H%M%S"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "applied": [],
        "would_apply": [],
        "skipped_external": [],
        "skipped_unanswered": [],
        "skipped_mismatch": [],
        "skipped_other": [],
        "errors": [],
        "queries": {},
        "total_scanned": 0,
        "limit": max_applies,
    }

    query_list = list(_iter_queries(config))
    if not query_list:
        report["errors"].append("No search queries configured.")
        logger.error("No search queries configured.")
        return report

    # NOTE: do NOT override the Accept header globally. Naukri's SPA fetches
    # /jobapi/v3/search with XHR; forcing "Accept: text/html" makes the API
    # return nothing and the result cards never render. Playwright already
    # sends realistic headers (Accept-Language comes from the context locale).

    if config.get("warm_up_homepage", True):
        logger.info("Warming up session on the Naukri homepage...")
        _goto(page, "https://www.naukri.com/")
        page.wait_for_timeout(random.uniform(1500, 2500))
        if _is_blocked(page):
            message = (
                "Naukri blocked the run during warm-up (403 Access Denied). "
                "Retry with --headful or from a different network/CI runner."
            )
            logger.error(message)
            report["errors"].append(message)
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            report["duration_seconds"] = round(time.time() - started, 1)
            return report

    try:
        for query_info in query_list:
            if applied_count >= max_applies:
                logger.info("Reached application limit (%d); stopping search.", max_applies)
                break

            query = query_info["query"]
            location = query_info["location"]
            search_url = build_search_url(
                query,
                min_experience=config.get("experience_filter", {}).get("min_years", 0),
                location=location,
            )
            query_key = f"{query} @ {location}" if location else query
            query_stats = {"found": 0, "applied": 0, "skipped": 0}
            report["queries"][query_key] = query_stats

            logger.info("Searching: %s", query_key)
            if not _goto(page, search_url):
                report["errors"].append(f"Could not open search results for {query_key}")
                continue

            if _is_blocked(page):
                message = (
                    "Naukri blocked the crawl with bot protection (403 Access Denied). "
                    "Retry with --headful or from a different network/CI runner."
                )
                logger.error(message)
                report["errors"].append(message)
                break

            if "nlogin" in page.url or "/login" in page.url:
                message = "Session expired while searching; stopping run."
                logger.error(message)
                report["errors"].append(message)
                break

            try:
                page.wait_for_selector(CARD_SELECTORS, timeout=15000)
            except PlaywrightTimeoutError:
                try:
                    diagnostics = {
                        "url": page.url,
                        "title": page.title(),
                        "cust-job-tuple": page.locator("div.cust-job-tuple").count(),
                        "srp-jobtuple-wrapper": page.locator("div.srp-jobtuple-wrapper").count(),
                        "blocked": _is_blocked(page),
                    }
                except Exception:
                    diagnostics = {"url": page.url}
                logger.warning("No job cards appeared for %s - %s", query_key, diagnostics)
                _screenshot(page, f"no_cards_{re.sub(r'[^a-zA-Z0-9]+', '_', query_key)[:40]}")
                if diagnostics.get("blocked"):
                    message = "Blocked by Naukri bot protection while loading search results."
                    report["errors"].append(message)
                    break
                continue

            page.mouse.wheel(0, 1400)
            page.wait_for_timeout(random.uniform(1200, 2000))

            cards = page.locator("div.cust-job-tuple").all() or page.locator(
                "div.srp-jobtuple-wrapper"
            ).all()
            query_stats["found"] = len(cards)
            logger.info("Found %d job card(s) for %s", len(cards), query_key)

            for card in cards:
                if applied_count >= max_applies or query_stats["applied"] >= max_per_query:
                    break
                report["total_scanned"] += 1
                try:
                    outcome = _process_card(
                        card,
                        page,
                        db,
                        matcher,
                        form_resolver,
                        config,
                        report,
                        seen_in_this_run,
                        applied_count,
                        max_applies,
                        dry_run,
                        query_key,
                    )
                except Exception as exc:  # one bad card must never kill the run
                    logger.exception("Unexpected error while processing a job card: %s", exc)
                    report["errors"].append(f"{query_key}: {exc}")
                    continue

                if outcome in ("applied", "would_apply"):
                    # Dry-run "would apply" counts toward the run limit too.
                    applied_count += 1
                    query_stats["applied"] += 1
                elif outcome:
                    query_stats["skipped"] += 1

            if max_per_query and query_stats["applied"] >= max_per_query:
                logger.info("Per-query application cap reached for %s", query_key)

    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["duration_seconds"] = round(time.time() - started, 1)
        report["applied_count"] = len(report["applied"])
        logger.info(
            "Run finished in %.1fs: %d applied, %d external, %d need answers, %d filtered, %d other, %d errors",
            report["duration_seconds"],
            len(report["applied"]),
            len(report["skipped_external"]),
            len(report["skipped_unanswered"]),
            len(report["skipped_mismatch"]),
            len(report["skipped_other"]),
            len(report["errors"]),
        )

    return report


def _process_card(
    card,
    page: Page,
    db: JobDatabase,
    matcher: JobMatcher,
    form_resolver: FormResolver,
    config: Dict[str, Any],
    report: Dict[str, Any],
    seen: set[str],
    applied_count: int,
    max_applies: int,
    dry_run: bool,
    query_key: str,
) -> Optional[str]:
    """Evaluate and (optionally) apply to one search-result card."""
    title_link = card.locator("a.title")
    if title_link.count() == 0:
        return None

    href = title_link.first.get_attribute("href") or ""
    job_id = extract_clean_job_id(href, card.get_attribute("data-job-id") or "")
    if not job_id or job_id in seen:
        return None
    seen.add(job_id)

    if db.is_job_processed(job_id, retry_unanswered=bool(config.get("retry_unanswered", True))):
        logger.debug("Job %s already processed; skipping.", job_id)
        return None

    title = _safe_text(title_link, "Unknown Role")
    company = _safe_text(card.locator("a.comp-name, .comp-name, span.company-name"), "Unknown Company")
    exp_text = _safe_text(card.locator("span.expwdth, span.experience, span[class*='exp']"), "0-2 Yrs")
    location = _safe_text(card.locator("span.loc, span.location, span[class*='loc']"), "")
    card_snippet = _safe_text(card.locator("div.job-description, .job-desc, li"), "")[:600]

    _sleep_range(config, "card_scan", (1.0, 2.0))
    logger.info(
        "[%d/%d] Evaluating '%s' at '%s' (id=%s, exp=%s%s)",
        applied_count + 1,
        max_applies,
        title,
        company,
        job_id,
        exp_text,
        f", loc={location}" if location else "",
    )

    match = matcher.evaluate_job(title, exp_text, jd_snippet=card_snippet, location=location)
    if not match.matched:
        logger.info("  -> skipped: %s", match.reason)
        db.record_job(
            job_id, company, title, "SKIPPED", "CRITERIA_MISMATCH",
            naukri_url=href, match_score=match.score, location=location, source_query=query_key,
        )
        report["skipped_mismatch"].append(
            {"company": company, "title": title, "reason": match.reason}
        )
        return "skipped"

    detail_page = page.context.new_page()
    try:
        logger.info("  -> opening job detail (%s)", href)
        if not _goto(detail_page, href):
            report["errors"].append(f"Could not open job {job_id}")
            return "error"
        detail_page.wait_for_timeout(random.uniform(1200, 2200))

        if detail_page.locator("text=/job has expired|job is expired|no longer available/i").count() > 0:
            logger.info("  -> skipped: job posting expired")
            db.record_job(
                job_id, company, title, "SKIPPED", "EXPIRED",
                naukri_url=href, match_score=match.score, location=location, source_query=query_key,
            )
            report["skipped_other"].append({"company": company, "title": title, "reason": "expired"})
            return "skipped"

        jd = _extract_jd(detail_page)
        if jd:
            jd_match = matcher.evaluate_job(title, exp_text, jd_snippet=jd, location=location)
            if not jd_match.matched:
                logger.info("  -> skipped after JD review: %s", jd_match.reason)
                db.record_job(
                    job_id, company, title, "SKIPPED", "CRITERIA_MISMATCH",
                    naukri_url=href, match_score=jd_match.score, location=location,
                    source_query=query_key,
                )
                report["skipped_mismatch"].append(
                    {"company": company, "title": title, "reason": f"{jd_match.reason} (JD)"}
                )
                return "skipped"
            match = jd_match

        if _is_blocked(detail_page):
            message = f"Blocked by Naukri while opening job {job_id}."
            logger.error(message)
            report["errors"].append(message)
            return "error"

        control_kind, apply_button = _wait_for_apply_controls(detail_page)

        if control_kind == "external":
            external_url = detail_page.url
            logger.info("  -> skipped: external application portal (%s)", external_url)
            db.record_job(
                job_id, company, title, "SKIPPED", "EXTERNAL_SITE",
                naukri_url=href, external_url=external_url,
                match_score=match.score, location=location, source_query=query_key,
            )
            report["skipped_external"].append(
                {"company": company, "title": title, "naukri_url": href, "external_url": external_url}
            )
            return "skipped"

        if _is_already_applied(detail_page):
            logger.info("  -> skipped: already applied on Naukri")
            db.record_job(
                job_id, company, title, "SKIPPED", "ALREADY_APPLIED",
                naukri_url=href, match_score=match.score, location=location, source_query=query_key,
            )
            report["skipped_other"].append(
                {"company": company, "title": title, "reason": "already applied"}
            )
            return "skipped"

        if apply_button is None:
            logger.info("  -> skipped: no apply button (closed/expired posting)")
            db.record_job(
                job_id, company, title, "SKIPPED", "NO_APPLY_BUTTON",
                naukri_url=href, match_score=match.score, location=location, source_query=query_key,
            )
            report["skipped_other"].append(
                {"company": company, "title": title, "reason": "no apply button"}
            )
            return "skipped"

        if dry_run:
            logger.info("  -> DRY RUN: would click apply for '%s'", title)
            report["would_apply"].append(
                {
                    "company": company,
                    "title": title,
                    "naukri_url": href,
                    "score": match.score,
                    "location": location,
                }
            )
            return "would_apply"

        logger.info("  -> clicking apply...")
        apply_button.click()
        detail_page.wait_for_timeout(2500)

        modal = _first_visible(detail_page, (MODAL_SELECTOR,))
        if modal is not None:
            logger.info("  -> screener detected; answering questions...")
            resolution = form_resolver.resolve_and_fill(modal, company=company, job_title=title)
            if not resolution.ok:
                question = resolution.first_unresolved()
                logger.warning("  -> skipped: unanswered mandatory question %r", question)
                db.record_job(
                    job_id, company, title, "SKIPPED", "UNANSWERED_QUESTION",
                    naukri_url=href, unanswered_question=question,
                    match_score=match.score, location=location, source_query=query_key,
                )
                report["skipped_unanswered"].append(
                    {"company": company, "title": title, "naukri_url": href, "question": question}
                )
                _close_modal(detail_page)
                _screenshot(detail_page, f"unanswered_{job_id}")
                return "skipped"

            if not _submit_and_verify(detail_page, modal):
                logger.warning("  -> screener submitted but confirmation was not detected.")

        applied_count += 1
        db.record_job(
            job_id, company, title, "APPLIED",
            naukri_url=href, match_score=match.score, location=location, source_query=query_key,
        )
        report["applied"].append(
            {
                "company": company,
                "title": title,
                "naukri_url": href,
                "score": match.score,
                "location": location,
            }
        )
        logger.info("  -> [SUCCESS] applied (%d/%d) to '%s' at '%s'", applied_count, max_applies, title, company)

        _sleep_range(config, "between_applies", (6.0, 10.0))
        return "applied"

    finally:
        try:
            detail_page.close()
        except Exception:
            pass


def _close_modal(detail_page: Page) -> None:
    close = detail_page.locator(
        "button.close, .crossIcon, span.close, button[aria-label='Close']"
    )
    if close.count() > 0:
        try:
            close.first.click()
            detail_page.wait_for_timeout(500)
        except Exception:
            pass
