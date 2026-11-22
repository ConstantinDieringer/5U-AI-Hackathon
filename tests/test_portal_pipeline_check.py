"""Experimental portal checks must not mistake challenges for rates or click business actions."""

import io
import json
from types import SimpleNamespace

from tools import portal_pipeline_check as probe


def test_reader_200_challenge_is_not_a_working_fallback(monkeypatch, tmp_path):
    response = io.BytesIO(
        b"Title: Just a moment...\nWarning: This page maybe requiring CAPTCHA\n"
        b"Performing security verification"
    )
    response.status = 200
    monkeypatch.setattr(probe, "urlopen", lambda *args, **kwargs: response)
    result = probe.reader_check(tmp_path)
    assert result["status"] == 200
    assert result["challenge_visible"] is True
    assert result["portal_form_found"] is False
    assert result["portal_rates_verified"] is False
    assert json.loads((tmp_path / "reader-result.json").read_text()) == result


def test_verification_never_clicks_a_portal_frame():
    def unexpected(*args, **kwargs):
        raise AssertionError("A business page must not be clicked by the verification worker")

    page = SimpleNamespace(
        frames=[SimpleNamespace(url=probe.PORTAL_URL, get_by_role=unexpected)],
        mouse=SimpleNamespace(click=unexpected),
    )
    assert probe.click_verification(page) is False
