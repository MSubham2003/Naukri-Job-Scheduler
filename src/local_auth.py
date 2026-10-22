"""Interactive Naukri login capture.

Run ``python -m src.local_auth``, log in (including any OTP/CAPTCHA), then press
ENTER: the browser session is written to ``auth_state.json`` so headless runs
can reuse it. For CI, gzip and base64-encode the file into the
``NAUKRI_STORAGE_STATE`` secret (raw base64 often exceeds GitHub's 48 KB limit).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

from .auth import resolve_storage_state_path
from .logging_setup import get_logger, setup_logging

logger = get_logger("local_auth")

LOGIN_URL = "https://www.naukri.com/nlogin/login"
SUCCESS_MARKERS = ("mnjuser/homepage", "mnjuser/profile")


def capture_session(auth_file: Path) -> int:
    setup_logging()
    logger.info("Launching a headed browser for Naukri authentication...")
    logger.info("Log in, complete any OTP/CAPTCHA, then return here and press ENTER.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = context.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("Could not open the login page automatically (%s).", exc)
            logger.warning("Navigate to https://www.naukri.com/nlogin/login manually.")

        input("\nPress ENTER once you are logged in and on your Naukri homepage... ")

        auth_file.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(auth_file))
        cookie_count = len(context.cookies())
        browser.close()

    if cookie_count == 0:
        logger.error("No cookies were captured; the session was probably not logged in.")
        return 1

    logger.info("Saved %d cookies to %s", cookie_count, auth_file)
    size = auth_file.stat().st_size
    print("\nNext steps:")
    print(f"  1. Keep {auth_file} private (it is gitignored).")
    print("  2. For GitHub Actions, add NAUKRI_STORAGE_STATE (gzip keeps it under")
    print("     GitHub's 48 KB secret limit):")
    print(f"       gzip -9 -c {auth_file} | base64 -w 0")
    print("\n     Or use NAUKRI_COOKIES with only the cookie array:")
    print(
        "       python -c \"import json; "
        f"print(json.dumps(json.load(open('{auth_file}'))['cookies'], "
        "separators=(',', ':')))\""
    )
    print(f"\n  Session file size: {size:,} bytes (raw base64 would be "
          f"{size * 4 // 3:,} bytes; the 48 KB secret limit is 49,152).")
    print("  3. Verify with: python main.py --self-check")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture a Naukri browser session")
    parser.add_argument("--file", default=None, help="Where to save the session state")
    args = parser.parse_args()
    auth_file = Path(args.file) if args.file else resolve_storage_state_path()
    return capture_session(auth_file)


if __name__ == "__main__":
    raise SystemExit(main())
