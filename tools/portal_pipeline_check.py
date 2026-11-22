"""Unattended portal experiments. No ChatGPT browser, personal profile or team credential.

Chrome mode starts its own process and temporary profile, then drives it over a
loopback-only DevTools connection. Reader mode sends only the public portal URL to Jina.
Neither mode sends mail, books freight or enables a production pricing fallback.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from tools.portal_browser_check import PORTAL_URL, capture


def reader_check(output: Path) -> dict:
    request = Request(f"https://r.jina.ai/{PORTAL_URL}", headers={"Accept": "text/plain"})
    try:
        try:
            response = urlopen(request, timeout=30)
        except HTTPError as exc:
            response = exc
        with response:
            text = response.read(1_000_001).decode("utf-8", errors="replace")
            status = response.status
        lower = text.lower()
        blocked = any(
            marker in lower
            for marker in ("just a moment", "requiring captcha", "performing security verification")
        )
        result = {
            "mode": "reader",
            "status": status,
            "challenge_visible": blocked,
            "portal_form_found": "team key" in lower and "weight" in lower,
            "portal_rates_verified": False,
        }
    except (URLError, TimeoutError) as exc:
        result = {"mode": "reader", "error": type(exc).__name__, "portal_rates_verified": False}
    (output / "reader-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)
    return result


def click_verification(page) -> bool:
    """Click only the Cloudflare widget, never a portal business-action button.

    The standard Turnstile checkbox may be in a closed shadow root. The fallback
    clicks the checkbox position in that widget's live bounding box, not screen
    coordinates or an arbitrary iframe. Each attempt is separately recorded.
    """
    for frame in page.frames:
        if urlparse(frame.url).hostname != "challenges.cloudflare.com":
            continue
        checkbox = frame.get_by_role("checkbox")
        if checkbox.count() and checkbox.first.is_visible():
            checkbox.first.click(timeout=3000)
            return True
        element = frame.frame_element()
        box = element.bounding_box()
        if box and 290 <= box["width"] <= 310 and 60 <= box["height"] <= 80:
            # Normal Turnstile widget: checkbox in the left hand part, centre height.
            # Do not click invisible, differently sized or unrecognised challenges.
            page.mouse.click(box["x"] + 20, box["y"] + box["height"] / 2)
            return True
    return False


def chrome_check(args, output: Path) -> None:
    from playwright.sync_api import Error, sync_playwright

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="5u-portal-pipeline-") as profile:
        command = [
            str(args.chrome_path),
            f"--user-data-dir={profile}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,900",
        ]
        if not args.headed:
            command.append("--headless=new")
        command.append("about:blank")
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        browser = None
        try:
            endpoint = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 15
            while True:
                try:
                    with urlopen(f"{endpoint}/json/version", timeout=1):
                        break
                except (URLError, TimeoutError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Isolated Chrome did not start") from None
                    time.sleep(0.2)
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(endpoint)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()
                page.set_viewport_size({"width": 1280, "height": 900})
                response = page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30000)
                print(json.dumps({
                    "mode": "chrome-cdp", "headless": not args.headed,
                    "profile": "temporary_application_profile",
                    "initial_status": response.status if response else None,
                    "webdriver": page.evaluate("navigator.webdriver"),
                }), flush=True)
                page.wait_for_timeout(12000)
                result = capture(page, output, 1)
                for attempt in range(1, args.attempts + 1):
                    if not result["challenge_visible"]:
                        break
                    clicked = click_verification(page)
                    print(json.dumps({"attempt": attempt, "verification_clicked": clicked}), flush=True)
                    if not clicked:
                        break
                    page.wait_for_timeout(45000)
                    result = capture(page, output, attempt + 1)
                form = page.locator("#team_key")
                print(json.dumps({
                    "mode": "chrome-cdp", "challenge_visible": result["challenge_visible"],
                    "portal_form_found": bool(form.count() and form.is_visible()),
                    "portal_rates_verified": False,
                }), flush=True)
                # A CDP connection may only disconnect on Browser.close() in the
                # Playwright API. Tell our owned Chrome process to exit as well.
                browser.new_browser_cdp_session().send("Browser.close")
                browser = None
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Error:
                    print(json.dumps({"cleanup": "browser_already_unavailable"}), flush=True)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("reader", "chrome-cdp"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--chrome-path", type=Path)
    parser.add_argument("--headed", action="store_true", help="Use a visible application browser")
    parser.add_argument("--attempts", type=int, default=1, help="Bounded checkbox attempts, 0 to 2")
    args = parser.parse_args()
    if not 0 <= args.attempts <= 2:
        parser.error("--attempts must be between 0 and 2")
    if args.mode == "chrome-cdp" and (
        args.chrome_path is None or not args.chrome_path.is_file()
    ):
        parser.error("--chrome-path must name an installed Chrome executable")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.mode == "reader":
        reader_check(output)
    else:
        chrome_check(args, output)


if __name__ == "__main__":
    main()
