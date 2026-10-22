"""Browser initialization and cookie ingestion."""

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from .logging_setup import get_logger

logger = get_logger("auth")

# NOTE: playwright-stealth is deliberately NOT used. Its patches break Naukri's
# logged-in React apps (profile/homepage render an empty skeleton or an error
# boundary). The launch flags plus the navigator.webdriver mask below are enough.


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

STORAGE_STATE_PATH = Path(os.getenv("STORAGE_STATE_PATH", "auth_state.json"))

# Historical names the bot may have saved a session under.
STORAGE_STATE_CANDIDATES = ("auth_state.json", "storage_state.json")


def display_available() -> bool:
    """True when a headed browser can actually open a window here."""
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))


def resolve_storage_state_path() -> Path:
    """Return the session file to use: configured path, else first that exists."""
    configured = os.getenv("STORAGE_STATE_PATH")
    candidates = [configured, *STORAGE_STATE_CANDIDATES] if configured else list(STORAGE_STATE_CANDIDATES)
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return Path(configured or STORAGE_STATE_CANDIDATES[0])


def launch_browser(playwright, headless=True):
    """Launch a Chromium browser and return (browser, context, page)."""
    browser = playwright.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--window-size=1366,768",
        ],
    )
    storage_state = resolve_storage_state_path()
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 768},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        storage_state=str(storage_state) if storage_state.exists() else None,
    )
    page = context.new_page()
    apply_stealth(page)
    return browser, context, page


def apply_stealth(page):
    """Minimal automation masking that does not break Naukri's SPAs."""
    page.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en']});
        if (!window.chrome) { window.chrome = { runtime: {} }; }
        """
    )


def cookies_from_env():
    """Read cookies from the NAUKRI_COOKIES environment variable."""
    raw = os.getenv("NAUKRI_COOKIES")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else data.get("cookies", [])


def load_cookies(context, cookies=None):
    """Ingest cookies into the browser context and persist storage state."""
    cookies = cookies if cookies is not None else cookies_from_env()
    if not cookies:
        return 0
    context.add_cookies(cookies)
    save_storage_state(context)
    return len(cookies)


def save_storage_state(context, path=None):
    target = Path(path) if path else resolve_storage_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(target))
    return target


HOME_URL = "https://www.naukri.com/mnjuser/homepage"
LOGIN_MARKERS = ("a[href*='mnjuser/profile']", "text=Recommended jobs", "img[alt*='profile']")
BOT_BLOCK_MARKERS = ("access denied", "errors.edgesuite.net", "you don't have permission to access")


def detect_session(page):
    """Classify the current session.

    Returns one of ``authenticated``, ``unauthenticated``, ``blocked``
    (bot protection / WAF) or ``unknown``.
    """
    try:
        response = page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
        status = response.status if response is not None else 0
        page.wait_for_timeout(2500)
        try:
            body = page.locator("body").inner_text()[:3000].lower()
        except Exception:
            body = ""

        if status in (403, 429) or any(marker in body for marker in BOT_BLOCK_MARKERS):
            logger.error(
                "Naukri returned bot protection (HTTP %s). Headless browsers are often blocked; "
                "retry with --headful or run from GitHub Actions.",
                status or "?",
            )
            return "blocked"

        for marker in LOGIN_MARKERS:
            try:
                if page.locator(marker).count() > 0:
                    return "authenticated"
            except Exception:
                continue
        if "/mnjuser/" in page.url:
            return "authenticated"
        if page.locator("text=Login").count() > 0 and page.locator("text=Register").count() > 0:
            return "unauthenticated"
        return "unknown"
    except Exception as exc:
        logger.warning("Session check failed: %s", exc)
        return "error"


def is_logged_in(page):
    """Backwards-compatible boolean session check."""
    return detect_session(page) == "authenticated"


def ensure_logged_in_status(context, page):
    """Ensure a valid session, loading cookies from the environment if needed."""
    state = detect_session(page)
    if state == "authenticated":
        return state
    if state in ("unauthenticated", "unknown"):
        loaded = load_cookies(context)
        if loaded:
            logger.info("Loaded %d cookie(s) from NAUKRI_COOKIES; rechecking session.", loaded)
            state = detect_session(page)
    return state


def ensure_logged_in(context, page):
    """Backwards-compatible boolean session bootstrap."""
    return ensure_logged_in_status(context, page) == "authenticated"


def build_context(headless=True):
    """Convenience factory returning an active Playwright session bundle."""
    playwright = sync_playwright().start()
    browser, context, page = launch_browser(playwright, headless=headless)
    return playwright, browser, context, page
