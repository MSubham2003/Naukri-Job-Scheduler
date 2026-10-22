"""Naukri profile refresh (resume re-upload + headline/summary update).

Naukri ranks recently updated profiles higher. This module:

* re-uploads the resume under a date-stamped filename, and (when ``pypdf`` and
  ``reportlab`` are installed) stamps "Profile updated: <date>" at the end of
  the PDF itself;
* rotates the resume headline and the profile summary by a trailing character
  so the profile timestamp moves without changing the meaning of the text;
* reads back "Profile last updated" so the run report proves the bump.

Selectors were verified against the live ``/mnjuser/profile`` DOM
(``span.edit[aria-label='Edit resume headline']`` opens a
``div.lightbox.profileEditDrawer`` with ``textarea#resumeHeadlineTxt`` and a
``button[type=submit]`` Save control).
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logging_setup import get_logger

logger = get_logger("profile")

DEFAULT_RESUME_PATH = "assets/Subham_Resume.pdf"
DATE_FORMAT = "%d_%b_%Y"
SCREENSHOT_DIR = Path("reports/screenshots")

PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
RESUME_INPUT_SELECTORS = ("input#attachCV", "input[type='file'][name='file']")
EDITOR_CONTAINER_SELECTORS = (
    "div.lightbox:visible",
    "div[class*='profileEditDrawer']:visible",
    "div[class*='model_open']:visible",
)
HEADLINE_EDIT_SELECTORS = (
    "span.edit[aria-label*='headline' i]",
    "span[aria-label='Edit resume headline']",
    "span.edit[name='resumeHeadline']",
    "span:has-text('Resume headline') ~ span.edit",
)
SUMMARY_EDIT_SELECTORS = (
    "span.edit[aria-label*='summary' i]",
    "span[aria-label='Edit profile summary']",
    "span.edit[name='summary']",
)


@dataclass
class PreparedResume:
    """A temporary resume copy ready to upload."""

    path: Path
    footer_added: bool = False


@dataclass
class ProfileRefreshResult:
    resume_uploaded: bool = False
    headline_updated: bool = False
    summary_updated: bool = False
    resume_filename: str = ""
    footer_added: bool = False
    last_updated: str = ""
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (self.resume_uploaded or self.headline_updated or self.summary_updated) and not any(
            "navigation" in e.lower() for e in self.errors
        )

    def summary(self) -> str:
        steps = [
            f"resume={'yes' if self.resume_uploaded else 'no'}",
            f"headline={'yes' if self.headline_updated else 'no'}",
            f"summary={'yes' if self.summary_updated else 'no'}",
        ]
        if self.last_updated:
            steps.append(f"last_updated={self.last_updated}")
        return ", ".join(steps)


# ---------------------------------------------------------------------------
# Resume preparation
# ---------------------------------------------------------------------------
def _pdf_date_stamp(when: datetime) -> str:
    return f"D:{when.strftime('%Y%m%d%H%M%S')}Z"


def add_date_footer(source: Path, target: Path, when: Optional[datetime] = None) -> bool:
    """Copy ``source`` to ``target`` and stamp the date on the last page."""
    when = when or datetime.now()
    shutil.copyfile(source, target)
    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.lib.utils import simpleSplit
        from reportlab.pdfgen import canvas
    except ImportError as exc:  # pragma: no cover - optional deps
        logger.warning("PDF date footer skipped (install pypdf + reportlab): %s", exc)
        return False

    try:
        reader = PdfReader(str(source))
        if not reader.pages:
            logger.warning("Resume PDF has no pages; using filename date only.")
            return False

        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)

        last_page = writer.pages[-1]
        width = float(last_page.mediabox.width)
        footer_text = f"Profile updated: {when.strftime('%d %b %Y')}"

        buffer = io.BytesIO()
        overlay_canvas = canvas.Canvas(buffer, pagesize=(width, float(last_page.mediabox.height)))
        overlay_canvas.setFont("Helvetica", 8)
        overlay_canvas.setFillGray(0.45)
        for index, line in enumerate(reversed(simpleSplit(footer_text, "Helvetica", 8, width - 80))):
            overlay_canvas.drawCentredString(width / 2, 16 + index * 10, line)
        overlay_canvas.save()
        buffer.seek(0)
        overlay_page = PdfReader(buffer).pages[0]
        last_page.merge_page(overlay_page)

        metadata = {}
        if reader.metadata:
            metadata.update({str(k): str(v) for k, v in reader.metadata.items()})
        metadata["/ModDate"] = _pdf_date_stamp(when)
        metadata["/Producer"] = metadata.get("/Producer", "Naukri Bot")
        writer.add_metadata(metadata)
        with open(target, "wb") as handle:
            writer.write(handle)
        logger.info("Added date footer to resume: %s", footer_text)
        return True
    except Exception as exc:
        logger.warning("Could not add PDF date footer (%s); using filename date only.", exc)
        try:
            shutil.copyfile(source, target)
        except OSError:
            pass
        return False


def build_dated_resume(
    base_resume_path: str | os.PathLike[str] = DEFAULT_RESUME_PATH,
    when: Optional[datetime] = None,
    footer: bool = True,
    output_dir: Optional[str | os.PathLike[str]] = None,
) -> PreparedResume:
    """Create a temporary date-stamped copy of the resume.

    The filename ends with the run date (``..._19_Oct_2026.pdf``) so Naukri
    treats it as a fresh document even when the content is otherwise unchanged.
    """
    when = when or datetime.now()
    base = Path(base_resume_path)
    if not base.exists():
        raise FileNotFoundError(f"Resume not found: {base}")

    directory = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix="naukri_resume_"))
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{base.stem}_{when.strftime(DATE_FORMAT)}{base.suffix or '.pdf'}"

    footer_added = False
    if footer and base.suffix.lower() == ".pdf":
        footer_added = add_date_footer(base, target, when)
    else:
        shutil.copyfile(base, target)
    logger.info(
        "Prepared dated resume: %s%s", target.name, " (with date footer)" if footer_added else ""
    )
    return PreparedResume(path=target, footer_added=footer_added)


def cleanup_resume(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        parent = path.parent
        if path.exists():
            path.unlink()
        if parent.name.startswith("naukri_resume_"):
            shutil.rmtree(parent, ignore_errors=True)
    except OSError as exc:  # pragma: no cover - cleanup best effort
        logger.debug("Could not clean up %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Text rotation + DOM helpers
# ---------------------------------------------------------------------------
def _rotate_text(current: str, variants: Optional[List[str]]) -> str:
    """Return the next text variant, or toggle a trailing dot."""
    current = (current or "").strip()
    if variants:
        cleaned = [v.strip() for v in variants if str(v).strip()]
        if cleaned:
            for index, variant in enumerate(cleaned):
                if current == variant and index + 1 < len(cleaned):
                    return cleaned[index + 1]
            return cleaned[0]
    if not current:
        return ""
    if current.endswith("."):
        return current[:-1].rstrip()
    return current + "."


def _visible(page, selectors) -> Optional[Any]:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                return locator.first
        except Exception:
            continue
    return None


def _scroll_until_visible(page, selectors, max_scrolls: int = 8, step: int = 900) -> Optional[Any]:
    """Naukri lazy-loads profile widgets; scroll until the control appears."""
    element = _visible(page, selectors)
    if element is not None:
        return element
    for _ in range(max_scrolls):
        page.mouse.wheel(0, step)
        page.wait_for_timeout(1200)
        element = _visible(page, selectors)
        if element is not None:
            return element
    return None


OVERLAY_CLOSE_SELECTORS = (
    "div[class*='ltCont'] span.icon[aria-label='Close']",
    "div[class*='pro-card'] span[aria-label='Close']",
    "div[class*='crossLayer'] span[aria-label='Close']",
)


def _dismiss_overlays(page) -> None:
    """Close Naukri's Pro-upgrade lightbox, which intercepts pointer events."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    except Exception:
        pass
    for selector in OVERLAY_CLOSE_SELECTORS:
        try:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 3)):
                element = locator.nth(index)
                if element.is_visible():
                    element.click(timeout=2000)
                    page.wait_for_timeout(300)
        except Exception:
            continue


def _click(page, element, label: str, timeout: int = 4000) -> bool:
    """Click normally, falling back to a DOM click when an overlay interferes."""
    try:
        element.click(timeout=timeout)
        return True
    except Exception as exc:
        logger.debug("Native click failed for %s (%s); using DOM click.", label, str(exc)[:90])
    try:
        element.dispatch_event("click")
        return True
    except Exception as exc2:
        logger.warning("Could not click %s: %s", label, exc2)
        return False


def _screenshot(page, name: str) -> None:
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        page.screenshot(path=str(path))
        logger.info("Saved screenshot: %s", path)
    except Exception as exc:  # pragma: no cover - diagnostics only
        logger.debug("Screenshot failed: %s", exc)


def _open_editor(page, edit_selectors, label: str) -> Optional[Any]:
    """Click an edit control and return the visible editor drawer."""
    element = _scroll_until_visible(page, edit_selectors)
    if element is None:
        logger.warning("%s edit control not found (Naukri UI may have changed).", label)
        return None

    for attempt in range(2):
        _dismiss_overlays(page)
        try:
            element.scroll_into_view_if_needed()
        except Exception:
            pass
        if not _click(page, element, label):
            return None
        for _ in range(10):
            editor = _visible(page, EDITOR_CONTAINER_SELECTORS)
            if editor is not None:
                page.wait_for_timeout(800)
                return editor
            page.wait_for_timeout(500)
        logger.warning("%s editor drawer did not appear (attempt %d).", label, attempt + 1)
        _dismiss_overlays(page)
    return None


def _close_editor(page, editor) -> None:
    try:
        close = editor.locator("span.icon[aria-label='Close'], div[class*='crossLayer']")
        if close.count() > 0:
            _click(page, close.first, "close editor", timeout=2500)
            page.wait_for_timeout(600)
    except Exception:
        pass


def _update_text_block(
    page,
    edit_selectors,
    textarea_selector: str,
    variants: Optional[List[str]],
    label: str,
) -> bool:
    """Toggle or rotate a profile text field (headline / summary)."""
    editor = _open_editor(page, edit_selectors, label)
    if editor is None:
        return False

    try:
        textarea = editor.locator(textarea_selector)
        if textarea.count() == 0:
            textarea = editor.locator("textarea")
        if textarea.count() == 0:
            logger.warning("%s editor has no textarea.", label)
            _close_editor(page, editor)
            return False

        field_element = textarea.first
        current = (field_element.input_value() or "").strip()
        new_text = _rotate_text(current, variants)
        if not new_text or new_text == current:
            logger.info("%s is empty; nothing to update.", label)
            _close_editor(page, editor)
            return False

        field_element.fill(new_text)
        save = editor.locator(
            "button[type='submit'], button:has-text('Save'), div[role='button']:has-text('Save')"
        )
        if save.count() == 0:
            logger.warning("%s save button not found.", label)
            _close_editor(page, editor)
            return False

        if not _click(page, save.first, f"{label} save"):
            _close_editor(page, editor)
            return False
        closed = False
        for _ in range(12):
            page.wait_for_timeout(500)
            if _visible(page, EDITOR_CONTAINER_SELECTORS) is None:
                closed = True
                break
        logger.info(
            "%s rotated to %r (%s).",
            label,
            new_text[:60],
            "saved, drawer closed" if closed else "save unconfirmed - drawer still open",
        )
        if not closed:
            _close_editor(page, editor)
        return closed
    except Exception as exc:
        logger.error("Could not update %s: %s", label, exc)
        _screenshot(page, f"{label.lower().replace(' ', '_')}_failed")
        _close_editor(page, editor)
        return False


def _read_last_updated(page) -> str:
    for selector in (
        "text=/Profile last updated/i",
        "span[class*='lastUpdated']",
        "div[class*='lastUpdated']",
    ):
        try:
            locator = page.locator(selector)
            if locator.count() > 0:
                return locator.first.inner_text().strip().replace("\n", " ")
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def refresh_profile(
    page,
    profile_data: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> ProfileRefreshResult:
    """Refresh resume, headline and summary on the Naukri profile page."""
    profile_data = profile_data or {}
    config = config or {}
    settings = config.get("profile_refresh", config)
    result = ProfileRefreshResult()

    resume_path = (
        profile_data.get("resume_path")
        or os.getenv("RESUME_PATH")
        or DEFAULT_RESUME_PATH
    )
    prepared: Optional[PreparedResume] = None

    logger.info("Refreshing Naukri profile...")
    try:
        page.goto(PROFILE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        if "nlogin" in page.url or "/login" in page.url:
            result.errors.append("Navigation redirected to login; session expired.")
            logger.error("Profile refresh aborted: session expired (redirected to login).")
            return result

        # Wait for the profile app rather than the old shimmer skeleton.
        for _ in range(10):
            if _visible(page, RESUME_INPUT_SELECTORS) is not None:
                break
            page.wait_for_timeout(1000)
        else:
            logger.warning("Profile page content did not render within 10s; continuing anyway.")

        # 1. Resume re-upload with the date appended -------------------------
        if settings.get("update_resume", True):
            try:
                prepared = build_dated_resume(
                    resume_path,
                    footer=bool(settings.get("resume_date_footer", True)),
                )
                file_input = _visible(page, RESUME_INPUT_SELECTORS)
                if file_input is None:
                    logger.warning("Resume upload input not found on the profile page.")
                    result.errors.append("Resume upload input not found.")
                else:
                    file_input.set_input_files(str(prepared.path))
                    result.resume_filename = prepared.path.name
                    toast = _wait_for_toast(page, timeout=15000)
                    page.wait_for_timeout(2500)
                    result.resume_uploaded = True
                    result.footer_added = prepared.footer_added
                    logger.info(
                        "Uploaded resume %s (%s).",
                        prepared.path.name,
                        "success toast" if toast else "awaiting confirmation",
                    )
            except FileNotFoundError as exc:
                logger.warning("%s", exc)
                result.errors.append(str(exc))
            except Exception as exc:
                logger.error("Resume upload failed: %s", exc)
                result.errors.append(f"Resume upload failed: {exc}")
                _screenshot(page, "resume_upload_failed")
        else:
            logger.info("Resume upload disabled by configuration.")

        # 2. Headline --------------------------------------------------------
        if settings.get("update_headline", True):
            page.wait_for_timeout(700)
            result.headline_updated = _update_text_block(
                page,
                tuple(settings.get("headline_edit_selectors") or HEADLINE_EDIT_SELECTORS),
                "textarea#resumeHeadlineTxt",
                settings.get("headline_variants"),
                "Resume headline",
            )

        # 3. Profile summary -------------------------------------------------
        if settings.get("update_summary", True):
            page.wait_for_timeout(700)
            result.summary_updated = _update_text_block(
                page,
                tuple(settings.get("summary_edit_selectors") or SUMMARY_EDIT_SELECTORS),
                "textarea#profileSummaryTxt",
                settings.get("summary_variants"),
                "Profile summary",
            )

        result.last_updated = _read_last_updated(page)
        if result.last_updated:
            logger.info("Naukri reports: %s", result.last_updated)
        return result
    except Exception as exc:
        logger.exception("Profile refresh failed: %s", exc)
        result.errors.append(str(exc))
        _screenshot(page, "profile_refresh_failed")
        return result
    finally:
        cleanup_resume(prepared.path if prepared else None)


def _wait_for_toast(page, timeout: int = 6000) -> bool:
    try:
        page.locator(
            ".toast-success, .success-message, [class*='toast-success'], "
            "[class*='success-toast'], div[class*='successMessage'], div[class*='Toast']"
        ).first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False
