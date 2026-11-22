# Portal browser experiments

Optional, standalone checks for the rate portal's browser page. They are separate from the production rate API client (`app/hackloop.py`), never send team keys or cookies and never claim a verified rate. Production uses the portal API and the airline mail fallback.

For the optional local portal-browser experiment, install `requirements-browser.txt`
and Google Chrome, then run:

```bash
python -m tools.portal_browser_check --pure-wu --interactive --output-dir /path/to/screenshots
```

This opens visible Chrome using the Pure-WU browser settings. Add `--headless`
for comparison. It records screenshots and whether the Cloudflare page remains;
it does not verify rates, send credentials, run mail agents or change live pricing.

The unattended experiment starts and controls its own Chrome process and temporary
application profile, without ChatGPT browser control or your personal Chrome profile:

```bash
python -m tools.portal_pipeline_check --mode chrome-cdp --chrome-path /path/to/chrome --output-dir /path/to/results
python -m tools.portal_pipeline_check --mode reader --output-dir /path/to/results
```

Chrome runs headlessly by default; `--headed` compares a visible application-owned
browser. Verification attempts are bounded with `--attempts` (0 to 2). Reader mode
tests Pure-WU's Jina fallback using only the public URL, without credentials.

For SeleniumBase with a normal Chromium browser on a virtual Linux display:

```bash
docker build -f Dockerfile.portal-browser -t 5u-portal-browser .
docker run --rm --shm-size=256m -v /absolute/path/to/results:/results 5u-portal-browser
```

This separate test image uses SeleniumBase and Xvfb, without a hosted solver or a
personal Chrome profile. It records the browser's actual display/headless mode,
portal-form availability and screenshots. It never claims rates were verified
merely because a CAPTCHA was clicked or the portal form became visible.

After Cloudflare, the browser checks for a dedicated arithmetic CAPTCHA form.
It reads plain arithmetic text or crops an image/canvas and runs local Tesseract
OCR on three image variants. OCR readings must agree on the same expression;
calculation permits only bounded arithmetic, without executing page text. The
browser submits at most two answers in the same session (`--math-attempts 0`
disables submission). Unknown forms, conflicting readings, changed images and
unrecognized operators stop the check with a nonzero exit status. Diagnostics
include cropped challenge images and `math-captcha-result.json`, without team
keys or cookies. The image includes the OCR engine and English data; no hosted
solver, account or trial is needed. Recognition of the real optional CAPTCHA
still needs validation once the organizer enables it again. This remains a
standalone browser check, separate from the production rate API client.

Exercise the full browser/OCR/submit flow against a loopback-only synthetic
CAPTCHA, including mild distortion, text, disabled and unsupported forms:

```bash
docker run --rm --shm-size=256m -v /absolute/path/to/results:/results --entrypoint python3 5u-portal-browser -m tools.portal_math_smoke_check --output-dir /results
```
