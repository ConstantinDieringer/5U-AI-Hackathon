"""Offline Chromium/Xvfb + real Tesseract acceptance check; never calls 5U."""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from tools import portal_math_captcha as math


def main() -> None:
    from seleniumbase import config as sb_config
    from seleniumbase import sb_cdp

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture = (
        Path(__file__).resolve().parents[1] / "tests/fixtures/portal_math_captcha.html"
    ).read_text(encoding="utf-8")
    state = {"scene": "image", "posts": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, page: str):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode("utf-8"))

        def do_GET(self):
            page = fixture
            if state["scene"] == "text":
                start = page.index("  <canvas")
                stop = page.index("</canvas>", start) + len("</canvas>")
                page = page[:start] + '<p class="math-question">9 × (8 − 3) = ?</p>' + page[stop:]
                page = page[: page.index("<script>")] + "</body></html>"
            if state["scene"] == "distorted":
                page = page.replace(
                    "ctx.fillText(", "ctx.translate(0, 5); ctx.rotate(-0.03); ctx.fillText("
                )
                page = page.replace(
                    "</script>",
                    """
                    ctx.strokeStyle = '#b0b0b0'; ctx.lineWidth = 1;
                    ctx.beginPath(); ctx.moveTo(10, 23); ctx.lineTo(260, 30); ctx.stroke();
                    </script>""",
                )
            if state["scene"] == "business":
                page = (
                    '<form id="math-captcha"><h1>Math CAPTCHA</h1>'
                    '<input id="team_key" type="password"><input name="answer">'
                    "<button>Get rate</button></form>"
                )
            if state["scene"] == "disabled":
                page = '<form><input id="team_key" type="password"><button>Get rate</button></form>'
            if state["scene"] == "unknown":
                page = '<div class="captcha">Solve arithmetic</div><input id="team_key">'
            self.respond(page)

        def do_POST(self):
            fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            state["posts"].append(fields.get("answer", [""])[0])
            expected = "45" if state["scene"] == "text" else "25"
            if fields.get("answer") == [expected]:
                self.respond(
                    "<title>Rate portal fixture</title><form>"
                    '<input id="team_key" type="password"><button>Get rate</button></form>'
                )
            else:
                self.respond(fixture)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    original_origin = math.PORTAL_ORIGIN
    math.PORTAL_ORIGIN = f"http://127.0.0.1:{server.server_port}"
    browser = None
    try:
        with tempfile.TemporaryDirectory(prefix="5u-offline-browser-") as profile:
            browser = sb_cdp.Chrome(
                browser_executable_path="/usr/bin/chromium",
                user_data_dir=profile,
                headless=False,
                xvfb=True,
                lang="en-US",
            )
            results = []
            for scene, expected in [
                ("image", "passed"),
                ("distorted", "passed"),
                ("text", "passed"),
                ("business", "unsupported_form"),
                ("disabled", "not_found"),
                ("unknown", "unsupported_form"),
            ]:
                state["scene"] = scene
                before = len(state["posts"])
                browser.goto(math.PORTAL_ORIGIN + "/portal/ui")
                result = math.solve(browser, output)
                result.update(scene=scene, posts=len(state["posts"]) - before)
                print(json.dumps(result), flush=True)
                if result["status"] != expected or result["posts"] != int(expected == "passed"):
                    raise RuntimeError(f"Offline acceptance check failed: {result}")
                results.append(result)
            (output / "offline-smoke.json").write_text(
                json.dumps(results, indent=2), encoding="utf-8"
            )
    finally:
        try:
            if browser is not None:
                browser.quit()
        finally:
            display = getattr(sb_config, "_virtual_display", None)
            if display is not None:
                display.stop()
            math.PORTAL_ORIGIN = original_origin
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    main()
