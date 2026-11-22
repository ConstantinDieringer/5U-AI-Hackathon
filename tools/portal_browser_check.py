"""Compare the portal UI in visible Chrome and headless Chrome, independently of agents.

Install requirements-browser.txt and Chrome, then run:
    python -m tools.portal_browser_check --pure-wu --interactive --output-dir PATH

This diagnostic opens only the portal UI. It does not load app settings, send team
credentials, call the mailbox or booking API, or install a production fallback.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

PORTAL_URL = "https://start-hack.5u.ai/portal/ui"
PURE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def capture(page: Any, output: Path, number: int) -> dict[str, Any]:
    title = page.title()
    body = page.locator("body").inner_text(timeout=5000).lower()
    challenge = any(
        marker in f"{title.lower()} {body}"
        for marker in (
            "just a moment",
            "nur einen moment",
            "verify you are human",
            "sicherheitsüberprüfung wird durchgeführt",
        )
    )
    screenshot = output / f"portal-browser-{number:02d}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    result = {
        "title": title,
        "challenge_visible": challenge,
        "screenshot": str(screenshot),
        # Disappearance of the challenge alone does not prove rates or bookings work.
        "portal_rates_verified": False,
    }
    (output / f"portal-browser-{number:02d}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--headless", action="store_true", help="Counterexample to visible Chrome")
    parser.add_argument(
        "--pure-wu",
        action="store_true",
        help="Use Pure-WU's Chrome user agent, locale and webdriver setting",
    )
    parser.add_argument(
        "--interactive", action="store_true", help="Keep Chrome open; Enter captures, q closes"
    )
    parser.add_argument("--wait-seconds", type=float, default=15)
    args = parser.parse_args()
    if not 0 <= args.wait_seconds <= 60:
        parser.error("--wait-seconds must be between 0 and 60")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        parser.error("Install the optional requirements-browser.txt first")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        # Fail visibly if Chrome is absent; silently changing the browser invalidates
        # the comparison with the original headless test. Use an isolated fresh profile.
        browser = playwright.chromium.launch(channel="chrome", headless=args.headless)
        context_args = (
            {"user_agent": PURE_USER_AGENT, "locale": "en-US"} if args.pure_wu else {}
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900}, **context_args)
        if args.pure_wu:
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
        page = context.new_page()
        response = page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30000)
        print(
            json.dumps(
                {
                    "headless": args.headless,
                    "pure_wu": args.pure_wu,
                    "initial_status": response.status if response else None,
                }
            ),
            flush=True,
        )
        page.wait_for_timeout(args.wait_seconds * 1000)
        number = 1
        capture(page, output, number)
        if args.interactive:
            print("Chrome stays open. Enter captures the current page; q closes.", flush=True)
            while True:
                try:
                    command = input().strip().lower()
                except EOFError:
                    break
                if command == "q":
                    break
                number += 1
                capture(page, output, number)
        browser.close()


if __name__ == "__main__":
    main()
