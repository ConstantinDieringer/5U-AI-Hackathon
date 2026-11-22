"""Test the portal with SeleniumBase and a normal Chromium browser on Linux/Xvfb.

Runs unattended using an application-owned profile. No ChatGPT browser control,
personal browser profile, team key, mailbox access or booking request is used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

PORTAL_URL = "https://start-hack.5u.ai/portal/ui"


def snapshot(browser, output: Path, number: int) -> dict:
    title = browser.get_title()
    challenge = any(
        marker in title.lower()
        for marker in ("just a moment", "nur einen moment", "checking your browser")
    )
    form = browser.is_element_visible("#team_key")
    screenshot = output / f"seleniumbase-{number:02d}.png"
    browser.save_screenshot(str(screenshot))
    result = {
        "engine": "seleniumbase-cdp",
        "seleniumbase_version": version("seleniumbase"),
        "title": title,
        "challenge_visible": challenge,
        "portal_form_found": form,
        "x_display_configured": bool(os.environ.get("DISPLAY")),
        "headless": browser.driver.config.headless,
        "portal_rates_verified": False,
        "screenshot": str(screenshot),
    }
    (output / f"seleniumbase-{number:02d}.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--browser-path", type=Path, default=Path("/usr/bin/chromium"))
    parser.add_argument("--attempts", type=int, default=1)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("This experiment requires Linux with Xvfb")
    if not 0 <= args.attempts <= 2:
        parser.error("--attempts must be between 0 and 2")
    if not args.browser_path.is_file():
        parser.error("--browser-path must name an installed Chromium browser")
    from seleniumbase import config as sb_config
    from seleniumbase import sb_cdp

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="5u-seleniumbase-profile-") as profile:
        browser = None
        try:
            browser = sb_cdp.Chrome(
                browser_executable_path=str(args.browser_path),
                user_data_dir=profile,
                headless=False,
                xvfb=True,
                lang="en-US",
            )
            browser.goto(PORTAL_URL)
            time.sleep(10)
            result = snapshot(browser, output, 1)
            if not result["x_display_configured"] or result["headless"]:
                raise RuntimeError("The browser is not running in the intended Xvfb mode")
            for attempt in range(1, args.attempts + 1):
                if result["portal_form_found"] or not result["challenge_visible"]:
                    break
                print(json.dumps({"verification_attempt": attempt}), flush=True)
                browser.solve_captcha()
                time.sleep(30)
                result = snapshot(browser, output, attempt + 1)
        finally:
            try:
                if browser is not None:
                    browser.quit()
            finally:
                display = getattr(sb_config, "_virtual_display", None)
                if display is not None:
                    display.stop()


if __name__ == "__main__":
    main()
