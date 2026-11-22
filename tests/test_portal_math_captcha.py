"""Arithmetic verification must not guess, execute text, or submit business forms."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import portal_math_captcha as math


@pytest.mark.parametrize(
    "question,answer",
    [
        ("17 + 8 = ?", "25"),
        ("9 × (8 − 3)", "45"),
        ("12:8", "1.5"),
        ("2 - 9", "-7"),
        ("6 ÷ 4 - 3", "-1.5"),
        ("3+2*4", "11"),
    ],
)
def test_arithmetic_uses_exact_values_and_precedence(question, answer):
    assert math.arithmetic(question)[1] == answer


@pytest.mark.parametrize(
    "question",
    [
        "__import__('os').system('echo oops')",
        "1+2;print(3)",
        "2**30",
        "12//4",
        "12%5",
        "7/0",
        "1/3",
        "17",
        "1.2+3",
        "1+2=99",
        "100000+1",
        "1+" * 40 + "1",
    ],
)
def test_unknown_or_unbounded_arithmetic_is_rejected(question):
    with pytest.raises(ValueError):
        math.arithmetic(question)


def test_conflicting_ocr_is_rejected_even_when_one_reading_has_a_majority():
    assert math.consensus(["17+8", "17 + 8 = ?", "unreadable"]) == "25"
    with pytest.raises(ValueError):
        math.consensus(["17+8", "17+8", "11+8"])
    with pytest.raises(ValueError):
        math.consensus(["17+8", "unreadable", ""])


def test_wrong_origin_cannot_be_inspected_or_clicked(tmp_path):
    browser = SimpleNamespace(get_current_url=lambda: "https://untrusted.invalid/portal/ui")
    assert math.solve(browser, tmp_path) == {
        "status": "untrusted_page",
        "submissions": 0,
        "portal_ready": False,
    }


def test_success_requires_form_disappearance_and_price_form(monkeypatch, tmp_path):
    challenge = {"status": "found", "image": False, "questions": ["19 - 7 = ?"]}
    states = iter([challenge, challenge, {"status": "not_found"}])
    monkeypatch.setattr(math, "discover", lambda _: next(states))
    monkeypatch.setattr(math.time, "sleep", lambda _: None)
    actions = []
    browser = SimpleNamespace(
        type=lambda selector, answer: actions.append((selector, answer)),
        click=lambda selector: actions.append(selector),
        is_element_visible=lambda _: True,
    )
    assert math.solve(browser, tmp_path) == {
        "status": "passed",
        "submissions": 1,
        "portal_ready": True,
    }
    assert actions == [(math.ANSWER, "12"), math.SUBMIT]


def test_rejected_answer_stops_after_two_submissions(monkeypatch, tmp_path):
    monkeypatch.setattr(
        math,
        "discover",
        lambda _: {
            "status": "found",
            "image": False,
            "questions": ["8+4"],
        },
    )
    monkeypatch.setattr(math.time, "sleep", lambda _: None)
    submitted = []
    browser = SimpleNamespace(type=lambda *_: None, click=lambda *_: submitted.append(1))
    assert math.solve(browser, tmp_path)["status"] == "attempts_exhausted"
    assert len(submitted) == 2


def test_changed_image_is_not_submitted(monkeypatch, tmp_path):
    monkeypatch.setattr(math, "discover", lambda _: {"status": "found", "image": True})
    monkeypatch.setattr(math, "read_image", lambda _: "25")
    screenshots = iter([b"first challenge", b"replacement challenge"])
    browser = SimpleNamespace(
        save_screenshot=lambda path, **_: Path(path).write_bytes(next(screenshots)),
    )
    assert math.solve(browser, tmp_path)["status"] == "changed"


def test_unknown_challenge_never_becomes_portal_success(monkeypatch, tmp_path):
    monkeypatch.setattr(math, "discover", lambda _: {"status": "unsupported_form"})
    browser = SimpleNamespace(is_element_visible=lambda _: True)
    assert math.solve(browser, tmp_path)["portal_ready"] is False
