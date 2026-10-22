"""Naukri automation bot entrypoint.

Usage:
    python main.py                     # search, apply, refresh profile, email digest
    python main.py --dry-run           # full search + matching, no applications
    python main.py --headful           # watch the browser work
    python main.py --limit 5           # cap applications this run
    python main.py --self-check        # validate config, resume, DB, session (no network)
    python main.py --only-profile      # just bump the profile
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from src import __version__
from src.logging_setup import get_logger, setup_logging

logger = get_logger("main")

DEFAULT_CONFIG = "config/search_config.json"
DEFAULT_PROFILE = "config/profile_data.json"
DEFAULT_QA_MEMORY = "config/qa_memory.json"
DEFAULT_AUTH_FILE = "auth_state.json"
REPORT_DIR = Path("reports")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Automated Naukri job application bot",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Search configuration JSON")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="Profile/answers JSON")
    parser.add_argument("--qa-memory", default=DEFAULT_QA_MEMORY, help="Learned screener answers JSON")
    parser.add_argument("--dry-run", action="store_true", help="Search and match without applying")
    parser.add_argument(
        "--browser-mode",
        choices=("auto", "headless", "headful"),
        default="auto",
        help=(
            "auto: run headless and retry with a visible window if Naukri blocks it; "
            "headless: never show a window; headful: always show one"
        ),
    )
    parser.add_argument(
        "--headful", action="store_true", help="Shortcut for --browser-mode headful"
    )
    parser.add_argument("--limit", type=int, default=None, help="Override max applications this run")
    parser.add_argument("--no-profile-refresh", action="store_true", help="Skip the profile bump")
    parser.add_argument("--only-profile", action="store_true", help="Refresh the profile and exit")
    parser.add_argument("--no-email", action="store_true", help="Write the digest but do not email it")
    parser.add_argument("--save-session", action="store_true", help="Persist refreshed cookies locally")
    parser.add_argument("--report-json", default=None, help="Path for the full run report JSON")
    parser.add_argument("--log-level", default="INFO", help="DEBUG | INFO | WARNING | ERROR")
    parser.add_argument("--log-dir", default="logs", help="Directory for rotated log files")
    parser.add_argument("--self-check", action="store_true", help="Validate setup without applying")
    parser.add_argument("--check-browser", action="store_true", help="Include a Chromium launch test in self-check")
    parser.add_argument("--version", action="version", version=f"naukri-bot {__version__}")
    return parser


def load_json(path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value must be an object")
        return data
    except FileNotFoundError:
        if default is not None:
            logger.warning("Config file %s not found; using defaults.", path)
            return default
        raise
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------
class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.warnings = 0
        self.failed = 0
        self.lines: List[str] = []

    def ok(self, message: str) -> None:
        self.passed += 1
        self.lines.append(f"[OK]   {message}")

    def warn(self, message: str) -> None:
        self.warnings += 1
        self.lines.append(f"[WARN] {message}")

    def fail(self, message: str) -> None:
        self.failed += 1
        self.lines.append(f"[FAIL] {message}")

    def render(self) -> str:
        return "\n".join(self.lines)


def run_self_check(args: argparse.Namespace) -> int:
    checker = Checker()

    # Python & dependencies -------------------------------------------------
    if sys.version_info >= (3, 10):
        checker.ok(f"Python {sys.version.split()[0]}")
    else:
        checker.fail("Python 3.10+ is required")

    import importlib.util

    required = ("playwright", "psycopg2", "dotenv", "pypdf", "reportlab")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        checker.warn("Missing required packages: " + ", ".join(missing))
    else:
        checker.ok("All required Python packages are installed")

    # Configuration --------------------------------------------------------
    try:
        search_config = load_json(args.config)
        checker.ok(f"Search config: {args.config}")
        if not search_config.get("search_queries"):
            checker.fail("search_config.search_queries is empty")
        if not search_config.get("blacklist_keywords") and not search_config.get("mandatory_keywords"):
            checker.warn("No mandatory/blacklist keywords configured; every job will match")
    except Exception as exc:
        checker.fail(f"Search config: {exc}")
        search_config = {}

    try:
        profile = load_json(args.profile)
        checker.ok(f"Profile config: {args.profile}")
        for key in ("notice_period_days", "current_ctc_lpa", "expected_ctc_lpa", "total_experience_years"):
            if key not in profile:
                checker.warn(f"profile_data.json is missing '{key}' (some questions stay unanswered)")
        checker.ok(f"{len(profile.get('custom_answers', {}))} custom answer(s), "
                   f"{len(profile.get('skills_experience', {}))} skill(s) configured")
    except Exception as exc:
        checker.fail(f"Profile config: {exc}")
        profile = {}

    # QA memory ------------------------------------------------------------
    try:
        from src.qa_store import QuestionMemory

        memory = QuestionMemory(args.qa_memory, autosave=False)
        pending = memory.pending()
        checker.ok(f"QA memory: {len(memory.all_entries())} question(s), {len(pending)} awaiting an answer")
        if pending:
            checker.warn(
                f"{len(pending)} screener question(s) need answers in {args.qa_memory} "
                f"(e.g. {pending[0].question!r})"
            )
    except Exception as exc:
        checker.fail(f"QA memory: {exc}")

    # Resume + date stamping ----------------------------------------------
    base_resume = profile.get("resume_path", "assets/Subham_Resume.pdf")
    if Path(base_resume).exists():
        try:
            from src.profile import build_dated_resume, cleanup_resume

            prepared = build_dated_resume(
                base_resume,
                footer=bool(search_config.get("profile_refresh", {}).get("resume_date_footer", True)),
            )
            try:
                filename_ok = datetime.now().strftime("%d_%b_%Y") in prepared.path.name
                if filename_ok:
                    checker.ok(f"Dated resume created: {prepared.path.name}")
                else:
                    checker.fail("Dated resume filename does not contain today's date")
                if prepared.footer_added:
                    checker.ok("Date footer written inside the PDF")
                else:
                    checker.warn("PDF date footer unavailable (filename-only date). Install pypdf + reportlab.")
            finally:
                cleanup_resume(prepared.path)
        except Exception as exc:
            checker.fail(f"Resume preparation failed: {exc}")
    else:
        checker.fail(f"Resume not found: {base_resume}")

    # Database -------------------------------------------------------------
    try:
        from src.db import JobDatabase

        db = JobDatabase()
        try:
            stats = db.stats()
            checker.ok(f"Database ready ({db.backend}): {stats or 'no jobs recorded yet'}")
        finally:
            db.close()
    except Exception as exc:
        checker.fail(f"Database unavailable: {exc}")

    # Session --------------------------------------------------------------
    from src.auth import resolve_storage_state_path

    auth_file = resolve_storage_state_path()
    cookies_env = os.getenv("NAUKRI_COOKIES")
    if auth_file.exists():
        try:
            state = json.loads(auth_file.read_text(encoding="utf-8"))
            cookie_count = len(state.get("cookies", []))
            if cookie_count:
                checker.ok(f"Session file {auth_file} contains {cookie_count} cookie(s)")
            else:
                checker.warn(f"Session file {auth_file} has no cookies; run: python -m src.local_auth")
        except Exception as exc:
            checker.warn(f"Session file {auth_file} could not be parsed: {exc}")
    elif cookies_env:
        checker.ok("NAUKRI_COOKIES is set (session will be loaded from the environment)")
    else:
        checker.warn("No session found. Run: python -m src.local_auth (or set NAUKRI_COOKIES)")

    # Email ----------------------------------------------------------------
    has_smtp = os.getenv("SMTP_FROM") or os.getenv("SENDER_EMAIL")
    if has_smtp:
        checker.ok("Email credentials detected (SMTP_* or SENDER_* variables)")
    else:
        checker.warn("Email not configured; digests will only be written to reports/")

    # Browser --------------------------------------------------------------
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            executable = p.chromium.executable_path
        if Path(executable).exists():
            checker.ok("Playwright Chromium is installed")
            if args.check_browser:
                from playwright.sync_api import sync_playwright as launch_api

                with launch_api() as p:
                    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                    page = browser.new_page()
                    page.set_content("<h1>ok</h1>")
                    rendered = page.inner_text("h1")
                    browser.close()
                checker.ok(f"Chromium launched and rendered a page ({rendered!r})")
        else:
            checker.warn("Chromium is not installed. Run: python -m playwright install chromium")
    except Exception as exc:
        checker.warn(f"Browser check skipped: {exc}")

    print(checker.render())
    print(
        f"\nSelf-check: {checker.passed} passed, "
        f"{checker.warnings} warning(s), {checker.failed} failed"
    )
    if checker.failed:
        print("Fix the [FAIL] items above before running the bot.")
    return 0 if checker.failed == 0 else 1


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def _write_report(report: Dict[str, Any], path: Optional[str] = None) -> Optional[Path]:
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        if path is None:
            stamp = str(report.get("run_id", datetime.now().strftime("%Y%m%d_%H%M%S")))
            path = str(REPORT_DIR / f"run_{stamp}.json")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        logger.info("Run report written to %s", target)
        return target
    except OSError as exc:
        logger.error("Could not write run report: %s", exc)
        return None


def _validate_setup(search_config: Dict[str, Any], profile: Dict[str, Any]) -> None:
    if not search_config.get("search_queries"):
        raise ValueError("search_config.json must contain a non-empty 'search_queries' list")
    experience = search_config.get("experience_filter") or {}
    if float(experience.get("min_years", 0)) > float(experience.get("max_years", 99)):
        raise ValueError("experience_filter.min_years cannot exceed max_years")
    required_profile = ("notice_period_days", "current_ctc_lpa", "expected_ctc_lpa")
    missing = [key for key in required_profile if key not in profile]
    if missing:
        logger.warning("profile_data.json missing keys: %s (related questions will be skipped)", missing)


def main(argv: Optional[List[str]] = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    setup_logging(args.log_level, args.log_dir, run_id=run_id)
    # auth.py reads this at import time; set it before any src.auth import.
    os.environ.setdefault("STORAGE_STATE_PATH", DEFAULT_AUTH_FILE)
    os.environ.setdefault("QA_MEMORY_PATH", args.qa_memory)

    if args.self_check:
        return run_self_check(args)

    if args.limit is not None and args.limit < 0:
        logger.error("--limit must be zero or greater")
        return 1

    try:
        profile_data = load_json(args.profile)
        search_config = load_json(args.config)
        _validate_setup(search_config, profile_data)
    except Exception as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    from src.auth import (
        display_available,
        ensure_logged_in_status,
        launch_browser,
        resolve_storage_state_path,
        save_storage_state,
    )
    from src.crawler import run_crawler
    from src.db import JobDatabase
    from src.forms import FormResolver
    from src.matcher import JobMatcher
    from src.notifier import send_digest_email
    from src.profile import refresh_profile
    from src.qa_store import QuestionMemory
    from playwright.sync_api import sync_playwright

    auth_file = resolve_storage_state_path()
    os.environ["STORAGE_STATE_PATH"] = str(auth_file)
    if not auth_file.exists() and not os.getenv("NAUKRI_COOKIES"):
        logger.error(
            "Missing session. Run 'python -m src.local_auth' to create %s, "
            "or set NAUKRI_COOKIES.",
            auth_file,
        )
        return 1

    memory = QuestionMemory(args.qa_memory)
    matcher = JobMatcher(search_config)
    form_resolver = FormResolver(profile_data, memory=memory)
    db: Optional[JobDatabase] = None
    report: Dict[str, Any] = {
        "run_id": run_id,
        "dry_run": args.dry_run,
        "browser_mode": args.browser_mode,
        "headful": args.headful,
        "profile_refreshed": False,
        "applied": [],
        "would_apply": [],
        "skipped_external": [],
        "skipped_unanswered": [],
        "skipped_mismatch": [],
        "skipped_other": [],
        "errors": [],
        "pending_questions": [],
    }
    exit_code = 0

    try:
        if not args.only_profile:
            db = JobDatabase()
            logger.info("Application store: %s backend", db.backend)
        else:
            logger.info("Profile-only mode: skipping database setup.")

        headful = args.headful or args.browser_mode == "headful"
        escape_block = args.browser_mode == "auto"

        with sync_playwright() as p:
            browser, context, page = launch_browser(p, headless=not headful)
            try:
                session_state = ensure_logged_in_status(context, page)

                # Naukri's WAF (Akamai) frequently 403s headless Chromium from
                # residential IPs. In auto mode, retry once with a real window
                # before giving up - that is what makes local runs work.
                if session_state == "blocked" and escape_block and not headful:
                    if display_available():
                        logger.warning(
                            "Headless browser was blocked; retrying with a visible browser window..."
                        )
                        try:
                            browser.close()
                            headful = True
                            browser, context, page = launch_browser(p, headless=False)
                            session_state = ensure_logged_in_status(context, page)
                        except Exception as exc:
                            logger.error("Headful retry failed: %s", exc)
                    else:
                        logger.error(
                            "Headless browser was blocked and no display is available for a "
                            "headful retry. Run with `xvfb-run -a python main.py --headful`."
                        )

                report["headful"] = headful
                report["session_state"] = session_state
                if session_state != "authenticated":
                    if session_state == "blocked":
                        logger.error(
                            "Naukri blocked this client (bot protection). Run with --headful, or "
                            "let GitHub Actions run it from a clean IP."
                        )
                        report["errors"].append("Blocked by Naukri bot protection (HTTP 403).")
                    else:
                        logger.error(
                            "Naukri session is not authenticated (state=%s). Re-run "
                            "'python -m src.local_auth' or refresh NAUKRI_COOKIES.",
                            session_state,
                        )
                        report["errors"].append(f"Authentication failed (state={session_state}).")
                    exit_code = 1
                else:
                    logger.info("Authenticated Naukri session confirmed.")

                    if args.only_profile or not args.no_profile_refresh:
                        if args.dry_run and not args.only_profile:
                            logger.info("Dry run: skipping profile refresh.")
                            report["profile_steps"] = "skipped (dry run)"
                        else:
                            profile_result = refresh_profile(
                                page,
                                profile_data=profile_data,
                                config=search_config,
                            )
                            report["profile_refreshed"] = profile_result.ok
                            report["profile_steps"] = profile_result.summary()
                            if profile_result.errors:
                                report["errors"].extend(profile_result.errors)
                            logger.info("Profile refresh: %s", profile_result.summary())

                    if not args.only_profile:
                        profile_errors = list(report["errors"])
                        crawler_report = run_crawler(
                            page,
                            db,
                            matcher,
                            form_resolver,
                            search_config,
                            dry_run=args.dry_run,
                            limit_override=args.limit,
                            run_id=run_id,
                        )
                        report.update(crawler_report)
                        # Keep auth/profile errors collected before the crawl.
                        report["errors"] = profile_errors + list(crawler_report.get("errors", []))

                    if args.save_session:
                        try:
                            target = save_storage_state(context, str(auth_file))
                            logger.info("Saved refreshed session to %s", target)
                        except Exception as exc:
                            logger.warning("Could not save session: %s", exc)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        exit_code = 130
    except Exception as exc:
        logger.exception("Fatal run error: %s", exc)
        report["errors"].append(f"Fatal: {exc}")
        exit_code = 1
    finally:
        if db is not None:
            try:
                report["db_stats"] = db.stats()
            except Exception as exc:
                logger.debug("Could not read DB stats: %s", exc)
            db.close()

    report["pending_questions"] = [
        {"question": e.question, "times_asked": e.times_asked, "options": e.options}
        for e in memory.pending()
    ]
    report["finished_at"] = datetime.now().isoformat()

    pending_md = memory.pending_report()
    if pending_md:
        try:
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            (REPORT_DIR / "pending_questions.md").write_text(pending_md, encoding="utf-8")
            logger.warning(
                "%d screener question(s) need answers. See reports/pending_questions.md "
                "and update %s.",
                len(report["pending_questions"]),
                args.qa_memory,
            )
        except OSError as exc:
            logger.debug("Could not write pending questions report: %s", exc)

    _write_report(report, args.report_json)
    try:
        send_digest_email(report, dry_run=args.dry_run or args.no_email)
    except Exception as exc:
        logger.error("Digest dispatch failed: %s", exc)
        report["errors"].append(f"Digest dispatch failed: {exc}")

    logger.info(
        "Done: %d applied, %d skipped (external), %d need answers, %d errors",
        len(report.get("applied", [])),
        len(report.get("skipped_external", [])),
        len(report.get("skipped_unanswered", [])),
        len(report.get("errors", [])),
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
