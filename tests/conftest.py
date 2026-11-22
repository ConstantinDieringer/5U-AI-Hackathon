"""No test may reach 5U: the mailbox and portal are rate limited and shared with the live
system. Every test starts with clients whose transport refuses the request; a test that
needs an answer installs its own client or stub. A refused call fails the test, so a code
path that swallows the error still shows up."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import httpx
import pytest
from openai import OpenAI

from app import hackloop, llm
from app.agents import bid_reader, booking, consent_check, email_review
from app.mail import audio, guard, speech


@pytest.fixture(autouse=True)
def no_live_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        email_review,
        "settings",
        dataclasses.replace(email_review.settings, email_review_enabled=False),
    )
    # no booking waiter thread may outlive its test database; price tests set their own wait
    monkeypatch.setattr(
        booking, "settings", dataclasses.replace(booking.settings, airline_confirm_wait_s=0)
    )
    guard._reset_for_tests()  # sends remembered by the mail guard belong to one test only

    def no_model(data: bytes) -> str:  # a test that needs a transcript installs its own
        raise ValueError("no speech model in tests")

    monkeypatch.setattr(speech, "transcribe", no_model)
    # model second opinions answer "nothing found" unless a test installs its own reading
    monkeypatch.setattr(bid_reader, "read", lambda *_a, **_k: None)
    monkeypatch.setattr(consent_check, "classify", lambda *_a, **_k: "unclear")
    refused: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        refused.append(f"{request.method} {request.url.path}")
        raise RuntimeError(f"test tried a live call: {request.method} {request.url.path}")

    transport = httpx.MockTransport(refuse)
    monkeypatch.setattr(hackloop, "_page_transport", transport)
    monkeypatch.setattr(
        hackloop, "_client", httpx.Client(base_url="https://blocked.invalid", transport=transport)
    )
    monkeypatch.setattr(
        llm,
        "_client",
        OpenAI(
            base_url="https://blocked.invalid/v1/gateway",
            api_key="test",
            max_retries=0,
            http_client=httpx.Client(transport=transport),
        ),
    )
    monkeypatch.setattr(audio, "_client", httpx.Client(transport=transport))
    monkeypatch.setattr(audio, "_cooldowns", {})
    monkeypatch.setattr(audio, "_models", (0.0, set()))
    monkeypatch.setattr(llm, "available_models", lambda **kw: set())
    # Audio tests explicitly supply a key/offline worker; other suites never run ASR.
    monkeypatch.setattr(
        audio,
        "settings",
        dataclasses.replace(
            audio.settings,
            groq_api_key="",
            local_audio_enabled=False,
        ),
    )
    yield
    assert not refused, f"live calls attempted: {refused}"


@pytest.fixture
def compose_repair_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt into the second composer call (COMPOSE_REPAIR, off by default for cost)."""
    from app.agents import rfq

    monkeypatch.setattr(rfq, "settings", dataclasses.replace(rfq.settings, compose_repair=True))
