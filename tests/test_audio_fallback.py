"""Regression: a rejected voice model must not discard a readable carrier offer."""

from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from openai import BadRequestError

from app import db, llm
from app.agents import bids
from app.mail import audio, documents
from app.models import Mail

OFFER = "Our all-in price is 358.64 EUR. Collection in 4 hours."
LOCAL_ADAPTER = audio._local


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


def test_groq_success_does_not_use_fallback(monkeypatch):
    seen = response_client(monkeypatch, lambda *_: httpx.Response(200, json={"text": OFFER}))
    monkeypatch.setattr(audio, "_gateway", lambda *a: pytest.fail("unexpected gateway call"))
    monkeypatch.setattr(audio, "_local", lambda *a: pytest.fail("unexpected offline call"))
    assert audio.transcribe("mp3", b"audio", None, "bid") == OFFER
    assert len(seen) == 1
    assert seen[0].url.path == "/openai/v1/audio/transcriptions"
    assert seen[0].headers["authorization"] == "Bearer test-secret"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422, 429, 500, 503])
def test_groq_failure_reaches_offline_when_gateway_has_no_audio(status, monkeypatch):
    seen = response_client(
        monkeypatch,
        lambda *_: httpx.Response(
            status,
            json={"error": "private upstream details test-secret"},
        ),
    )
    monkeypatch.setattr(llm, "complete", lambda *a, **kw: pytest.fail("unsupported model sent"))
    assert audio.transcribe("mp3", b"audio", None, "bid") == OFFER
    assert len(seen) == (2 if status >= 500 else 1)
    events = db.events_after(0)
    assert "test-secret" not in str(events)


def test_daily_quota_uses_fallback_without_retrying_next_attachment(monkeypatch):
    seen = response_client(
        monkeypatch,
        lambda *_: httpx.Response(
            429,
            headers={"retry-after": "3600"},
            json={"error": "quota"},
        ),
    )
    for _ in range(2):
        assert audio.transcribe("mp3", b"audio", None, "bid") == OFFER
    assert len(seen) == 1


def test_network_failure_is_bounded_then_falls_back(monkeypatch):
    def timeout(request, count):
        raise httpx.ReadTimeout("sensitive details", request=request)

    seen = response_client(monkeypatch, timeout)
    assert audio.transcribe("mp3", b"audio", None, "bid") == OFFER
    assert len(seen) == 2


@pytest.mark.parametrize("payload", [{"text": ""}, {"text": 42}, [], {"text": "Thank you."}])
def test_empty_invalid_or_unusable_transcript_uses_fallback(payload, monkeypatch):
    response_client(monkeypatch, lambda *_: httpx.Response(200, json=payload))
    assert (
        audio.transcribe(
            "mp3",
            b"audio",
            None,
            "bid",
            accept_text=lambda t: bids.parse_price(t) is not None,
        )
        == OFFER
    )


def test_malformed_json_uses_fallback(monkeypatch):
    response_client(monkeypatch, lambda *_: httpx.Response(200, content=b"not JSON"))
    assert audio.transcribe("mp3", b"audio", None, "bid") == OFFER


def test_existing_gateway_fallback_is_used_if_allowed(monkeypatch):
    response_client(monkeypatch, lambda *_: httpx.Response(503))
    monkeypatch.setattr(llm, "available_models", lambda **kw: {audio.settings.llm_audio_model})
    seen = []

    def complete(*args, **kw):
        seen.append((args, kw))
        return OFFER

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(audio, "_local", lambda *a: pytest.fail("unexpected offline call"))
    assert audio.transcribe("wav", b"audio", None, "bid") == OFFER
    assert seen[0][1]["model_name"] == "gpt-4o-mini-audio-preview"
    assert seen[0][0][1][0]["content"][1]["input_audio"]["format"] == "wav"


def test_listed_model_that_rejects_audio_is_cooled_down(monkeypatch):
    response_client(monkeypatch, lambda *_: httpx.Response(401))
    monkeypatch.setattr(llm, "available_models", lambda **kw: {audio.settings.llm_audio_model})
    seen = []

    def reject(*args, **kw):
        seen.append(kw)
        raise BadRequestError(
            "input_audio not supported test-secret",
            response=httpx.Response(400, request=httpx.Request("POST", "https://test.invalid")),
            body={},
        )

    monkeypatch.setattr(llm, "complete", reject)
    for _ in range(2):
        assert audio.transcribe("mp3", b"audio", None, "bid") == OFFER
    assert len(seen) == 1


def test_gateway_discovery_outage_also_uses_offline(monkeypatch):
    response_client(monkeypatch, lambda *_: httpx.Response(401))

    def unavailable(**kw):
        raise RuntimeError("test-secret upstream")

    monkeypatch.setattr(llm, "available_models", unavailable)
    assert audio.transcribe("mp3", b"audio", None, "bid") == OFFER


def test_configured_text_model_is_never_sent_audio(monkeypatch):
    response_client(monkeypatch, lambda *_: httpx.Response(401))
    monkeypatch.setattr(audio, "settings", replace(audio.settings, llm_audio_model="gpt-5"))
    monkeypatch.setattr(llm, "available_models", lambda **kw: {"gpt-5"})
    monkeypatch.setattr(llm, "complete", lambda *a, **kw: pytest.fail("text model sent audio"))
    assert audio.transcribe("mp3", b"audio", None, "bid") == OFFER


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


def test_duplicate_delivery_transcribes_once_and_atomic_cache_is_reused(tmp_path, monkeypatch):
    path = tmp_path / "voice.mp3"
    path.write_bytes(b"ID3" + b"\0" * 50)
    path.with_name("voice.mp3.transcript.txt").write_text("", encoding="utf-8")
    calls = []

    def transcribe(*args, **kw):
        calls.append(1)
        time.sleep(0.05)
        return OFFER

    monkeypatch.setattr(audio, "transcribe", transcribe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: documents.transcribe(documents.read(path), None, "bid", path),
                range(2),
            )
        )
    assert results == [OFFER, OFFER]
    assert calls == [1]
    assert documents.cached_transcript(path) == OFFER
    assert path in documents.inbox_files(tmp_path)
    assert not any(p.name.endswith(".transcript.txt") for p in documents.inbox_files(tmp_path))


def test_local_worker_is_killed_at_timeout_and_releases_slot(monkeypatch):
    # Exercise the real adapter rather than setup_audio's fake local recognizer.
    worker = LOCAL_ADAPTER
    monkeypatch.setattr(audio, "settings", replace(audio.settings, local_audio_enabled=True))
    slot = threading.Semaphore(1)
    monkeypatch.setattr(audio, "_local_slot", slot)

    def timeout(*args, **kw):
        assert kw["timeout"] <= 30
        raise subprocess.TimeoutExpired(args[0], kw["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(audio.TranscriptionError, match="timed out"):
        worker("mp3", b"audio", time.monotonic() + 45)
    assert slot.acquire(blocking=False)
    slot.release()


def test_session_18673_model_refusal_still_produces_trucker_bid(tmp_path, monkeypatch):
    response_client(monkeypatch, lambda *_: httpx.Response(401))
    monkeypatch.setattr(llm, "complete", lambda *a, **kw: pytest.fail("disallowed model sent"))
    monkeypatch.setattr(bids, "_spawn", lambda fn, *a: fn(*a))
    path = tmp_path / "voice-message.mp3"
    path.write_bytes(b"ID3" + b"\0" * 50)
    monkeypatch.setattr(bids, "_attachment_paths", lambda mail: [path])
    mail = Mail(
        18850,
        18673,
        "domblick-kurierdienst@hackloop.mail",
        "austrian-power@hackloop.mail",
        "Re: [AP-18673] Pickup 40527 to DUS airport, 1728 kg / 6.31 cbm",
        "Our offer is in the attached voice message.",
        "2026-11-21T21:42:26+00:00",
    )
    try:
        bids.on_bid_mail(mail, 18673)
        (offer,) = bids.offers(18673)
        assert offer.total_eur == Decimal("358.64")
        assert offer.transit_days == 4 / 24
        assert offer.timing_kind == "collection_lead"
        assert offer.mail_id == 18850
        events = db.events_after(0)
        assert any(e["kind"] == "audio_transcribed" and e["session_id"] == 18673 for e in events)
    finally:
        bids.close(18673)


@pytest.mark.parametrize(
    "spoken",
    [
        "358 euros and 64 cents, collection in four hours.",
        "Three hundred fifty-eight euros and sixty-four cents, collection in four hours.",
        "Dreihundertachtundfünfzig Euro und vierundsechzig Cent, Abholung in vier Stunden.",
    ],
)
def test_spoken_cents_and_time_are_not_lost(spoken):
    normalized = audio.normalize_transcript(spoken)
    assert bids.parse_price(normalized) == (Decimal("358.64"), "EUR")
    assert bids.parse_transit_days(normalized) == 4 / 24


def test_ambiguous_cents_do_not_silently_quote_only_the_whole_euros():
    with pytest.raises(audio.TranscriptionError, match="unresolved"):
        audio.normalize_transcript("358 euros and one hundred cents")


def test_currency_followed_by_and_another_service_is_valid():
    text = audio.normalize_transcript("Our price is 358 euros and collection is in four hours.")
    assert bids.parse_price(text) == (Decimal("358.00"), "EUR")
    assert bids.parse_transit_days(text) == 4 / 24


def test_health_reports_configuration_without_exposing_key(monkeypatch):
    from app.ui import server

    monkeypatch.setattr(server, "settings", replace(server.settings, groq_api_key="test-secret"))
    health = server.api_health()
    assert health["audio_transcription"]["groq_configured"] is True
    assert "test-secret" not in str(health)


def test_cached_voice_replay_keeps_spoken_evidence_and_does_not_retranscribe(tmp_path, monkeypatch):
    path = tmp_path / "voice-message.mp3"
    path.write_bytes(b"ID3" + b"\0" * 50)
    spoken = "Hi. We can do that for 358 euros and 64 cents. Collection in four hours."
    path.with_name(path.name + documents.TRANSCRIPT_SUFFIX).write_text(spoken, encoding="utf-8")
    monkeypatch.setattr(bids, "_attachment_paths", lambda mail: [path])
    monkeypatch.setattr(
        audio, "transcribe", lambda *a, **kw: pytest.fail("cached voice reprocessed")
    )
    mail = Mail(
        18850,
        18673,
        "domblick-kurierdienst@hackloop.mail",
        "austrian-power@hackloop.mail",
        "Re: [AP-18673] Pickup",
        "Our offer is attached.",
        "2026-11-21T21:42:26+00:00",
    )
    try:
        bids.on_bid_mail(mail, 18673)
        (offer,) = bids.offers(18673)
        assert offer.total_eur == Decimal("358.64")
        assert offer.transit_days == 4 / 24
    finally:
        bids.close(18673)
