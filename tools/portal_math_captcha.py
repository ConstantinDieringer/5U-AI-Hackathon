"""Local arithmetic OCR for the standalone portal browser (no hosted solver).

Unknown forms and ambiguous recognition fail closed. The real portal's optional
math challenge still needs a live acceptance test when it is enabled again.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from fractions import Fraction
from pathlib import Path

PORTAL_ORIGIN = "https://start-hack.5u.ai"
ANSWER = '[data-portal-math-probe="answer"]'
IMAGE = '[data-portal-math-probe="image"]'
SUBMIT = '[data-portal-math-probe="submit"]'

# Discover only a dedicated verification form, never the price/booking form.
# Return JSON because SeleniumBase's evaluate() returns JS primitives reliably.
DISCOVER = r"""JSON.stringify((() => {
  document.querySelectorAll('[data-portal-math-probe]').forEach(
    e => e.removeAttribute('data-portal-math-probe'));
  const visible = e => !!(e.getClientRects().length) &&
    getComputedStyle(e).visibility !== 'hidden';
  const forms = [...document.forms].filter(f => visible(f) &&
    /captcha|math|arithmetic|rechenaufgabe/i.test(
      [f.id, f.className, f.innerText].join(' ')));
  if (!forms.length) {
    const hinted = /captcha|arithmetic|math challenge|rechenaufgabe/i.test(
      document.body.innerText) || [...document.querySelectorAll(
        '[id*="captcha" i],[class*="captcha" i],[data-captcha]')].some(visible);
    return {status: hinted ? 'unsupported_form' : 'not_found'};
  }
  if (forms.length !== 1) return {status: 'unsupported_form'};
  const f = forms[0];
  const action = new URL(f.action || location.href, location.href);
  const paths = ['/portal/ui', '/portal/captcha', '/portal/verify',
    '/portal/ui/captcha', '/portal/ui/verify', '/captcha', '/verify'];
  if (action.origin !== location.origin || !paths.includes(action.pathname) ||
      action.search || !['get', 'post'].includes(f.method.toLowerCase()))
    return {status: 'unsupported_form'};
  const inputs = [...f.querySelectorAll('input,textarea,select')].filter(e =>
    !['hidden','submit','button','reset'].includes(e.type) && !e.disabled);
  if (inputs.length !== 1) return {status: 'unsupported_form'};
  const answer = inputs[0];
  const labels = [...(answer.labels || [])].map(e => e.innerText).join(' ');
  if (!visible(answer) || !['text','number','tel'].includes(answer.type) ||
      !/captcha|math|answer|result|solution|antwort|ergebnis|loesung|lösung/i.test(
        [answer.id, answer.name, answer.placeholder, labels].join(' ')))
    return {status: 'unsupported_form'};
  const buttons = [...f.querySelectorAll('button,input[type=submit]')].filter(e =>
    visible(e) && !e.disabled && (e.type === 'submit' ||
      /^(verify|check|confirm|submit|prüfen|bestätigen|absenden)$/i.test(e.innerText.trim())));
  if (buttons.length !== 1) return {status: 'unsupported_form'};
  const button = buttons[0];
  if ((button.hasAttribute('formaction') && button.formAction !== f.action) ||
      (button.hasAttribute('formmethod') && button.formMethod !== f.method))
    return {status: 'unsupported_form'};
  const images = [...f.querySelectorAll('img,canvas')].filter(visible);
  if (images.length > 1) return {status: 'unsupported_form'};
  const questions = [...f.querySelectorAll(
    '[data-captcha-expression],.captcha-question,.math-question,label')]
    .map(e => e.getAttribute('data-captcha-expression') || e.innerText);
  answer.setAttribute('data-portal-math-probe', 'answer');
  button.setAttribute('data-portal-math-probe', 'submit');
  if (images.length) images[0].setAttribute('data-portal-math-probe', 'image');
  return {status: 'found', image: images.length === 1, questions,
    image_source: images.length ? images[0].getAttribute('src') : null,
    action: action.href};
})())"""


def arithmetic(text: str) -> tuple[str, str]:
    """Parse a small integer arithmetic expression; never execute page/OCR text."""
    expression = re.sub(r"\s+", "", text).translate(
        str.maketrans({"×": "*", "x": "*", "X": "*", "÷": "/", ":": "/", "−": "-"})
    )
    expression = re.sub(r"(?:=\??|\?)$", "", expression)
    if not 3 <= len(expression) <= 64 or not re.fullmatch(r"[0-9()+*/-]+", expression):
        raise ValueError("not an arithmetic expression")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("invalid expression") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > 32 or not any(isinstance(n, ast.BinOp) for n in nodes):
        raise ValueError("expression too large or missing operator")

    def calculate(node: ast.AST) -> Fraction:
        if isinstance(node, ast.Constant) and type(node.value) is int and node.value <= 99999:
            return Fraction(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = calculate(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            left, right = calculate(node.left), calculate(node.right)
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            elif isinstance(node.op, ast.Div) and right:
                value = left / right
            else:
                raise ValueError("unsupported operator or division by zero")
            if abs(value) <= 1000000:
                return value
        raise ValueError("unsupported arithmetic")

    value = calculate(tree.body)
    if value.denominator == 1:
        return expression, str(value.numerator)
    # Only exact finite decimals, never round an uncertain answer.
    scaled = value * 1000000
    if scaled.denominator != 1:
        raise ValueError("answer needs rounding")
    sign = "-" if scaled < 0 else ""
    integer, decimal = divmod(abs(scaled.numerator), 1000000)
    return expression, f"{sign}{integer}.{decimal:06d}".rstrip("0")


def consensus(readings: list[str]) -> str:
    candidates: Counter[tuple[str, str]] = Counter()
    for reading in readings:
        try:
            candidates[arithmetic(reading)] += 1
        except ValueError:
            continue
    if len(candidates) != 1 or next(iter(candidates.values()), 0) < 2:
        raise ValueError("OCR readings do not agree on a single expression")
    return next(iter(candidates))[1]


def read_image(path: Path) -> str:
    """Use bounded local Tesseract calls on several image variants."""
    if not shutil.which("tesseract"):
        raise ValueError("local Tesseract is not installed")
    from PIL import Image, ImageFilter, ImageOps

    with Image.open(path) as source:
        if source.width * source.height > 1000000:
            raise ValueError("CAPTCHA image too large")
        rgba = source.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        image = ImageOps.grayscale(background.convert("RGB"))
    image = ImageOps.autocontrast(image)
    if image.getpixel((0, 0)) < 128:
        image = ImageOps.invert(image)
    image = image.resize((image.width * 3, image.height * 3))
    variants = [
        image,
        image.point(lambda p: 255 if p > 150 else 0),
        image.filter(ImageFilter.MedianFilter(3)),
    ]
    readings = []
    with tempfile.TemporaryDirectory(prefix="5u-math-ocr-") as folder:
        for index, variant in enumerate(variants):
            target = Path(folder) / f"variant-{index}.png"
            ImageOps.expand(variant, border=20, fill="white").save(target)
            result = subprocess.run(
                [
                    "tesseract",
                    str(target),
                    "stdout",
                    "-l",
                    "eng",
                    "--psm",
                    "7",
                    "-c",
                    "tessedit_char_whitelist=0123456789+-*/xX():=?×÷−",
                    "-c",
                    "load_system_dawg=0",
                    "-c",
                    "load_freq_dawg=0",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode:
                raise ValueError("local OCR failed")
            readings.append(result.stdout)
    return consensus(readings)


def discover(browser) -> dict:
    if browser.get_current_url().split("?", 1)[0] != f"{PORTAL_ORIGIN}/portal/ui":
        return {"status": "untrusted_page"}
    return json.loads(browser.evaluate(DISCOVER))


def solve(browser, output: Path, attempts: int = 2) -> dict:
    """Submit only a recognized standalone math form; verify portal readiness."""
    if not 0 <= attempts <= 2:
        raise ValueError("math attempts must be between 0 and 2")
    submissions = 0
    for index in range(attempts + 1):
        challenge = discover(browser)
        if challenge["status"] != "found":
            ready = challenge["status"] == "not_found" and browser.is_element_visible("#team_key")
            return {
                "status": "passed" if ready and submissions else challenge["status"],
                "submissions": submissions,
                "portal_ready": ready,
            }
        if index == attempts:
            return {
                "status": "attempts_exhausted",
                "submissions": submissions,
                "portal_ready": False,
            }
        try:
            if challenge["image"]:
                image = output / f"math-captcha-{index + 1:02d}.png"
                browser.save_screenshot(str(image), selector=IMAGE)
                answer = read_image(image)
            else:
                answers = set()
                for question in challenge["questions"]:
                    try:
                        answers.add(arithmetic(question))
                    except ValueError:
                        continue
                if len(answers) != 1:
                    raise ValueError("no unambiguous arithmetic question")
                answer = next(iter(answers))[1]
        except (ValueError, OSError, subprocess.TimeoutExpired):
            return {"status": "unreadable", "submissions": submissions, "portal_ready": False}
        # Redetect after OCR: don't submit a refreshed challenge or a changed form.
        if discover(browser) != challenge:
            return {"status": "changed", "submissions": submissions, "portal_ready": False}
        if challenge["image"]:
            fresh = output / "math-captcha-current.png"
            browser.save_screenshot(str(fresh), selector=IMAGE)
            if fresh.read_bytes() != image.read_bytes():
                return {"status": "changed", "submissions": submissions, "portal_ready": False}
        browser.type(ANSWER, answer)
        browser.click(SUBMIT)
        submissions += 1
        time.sleep(2)
    raise AssertionError("unreachable")
