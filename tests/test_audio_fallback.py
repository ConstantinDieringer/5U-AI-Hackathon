"""Regression: a rejected voice model must not discard a readable carrier offer."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from app import db
from app.mail import audio, documents

OFFER = "Our all-in price is 358.64 EUR. Collection in 4 hours."


@pytest.fixture(autouse=True)
def setup_audio(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "audio.db"))
    monkeypatch.setattr(
        audio,
        "settings",
        replace(
            audio.settings,
            groq_api_key="test-secret",
            local_audio_enabled=True,
            llm_audio_model="gpt-4o-mini-audio-preview",
        ),
    )
    monkeypatch.setattr(audio, "_local", lambda *a: OFFER)


def response_client(monkeypatch, respond):
    seen = []

    def handle(request):
        seen.append(request)
        return respond(request, len(seen))

    monkeypatch.setattr(audio, "_client", httpx.Client(transport=httpx.MockTransport(handle)))
    return seen


def test_every_provider_failure_remains_unpriced_and_is_not_cached(tmp_path, monkeypatch):
    response_client(monkeypatch, lambda *_: httpx.Response(401))

    def fail(*args):
        raise RuntimeError("test-secret upstream")

    monkeypatch.setattr(audio, "_local", fail)
    path = tmp_path / "voice.mp3"
    path.write_bytes(b"ID3" + b"\0" * 50)
    with pytest.raises(audio.TranscriptionError) as error:
        documents.transcribe(documents.read(path), None, "bid", path)
    assert "test-secret" not in str(error.value)
    assert documents.cached_transcript(path) is None
