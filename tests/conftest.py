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
from app.agents import booking, email_review
from app.mail import guard, speech


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
    refused: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        refused.append(f"{request.method} {request.url.path}")
        raise RuntimeError(f"test tried a live call: {request.method} {request.url.path}")

    transport = httpx.MockTransport(refuse)
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
    yield
    assert not refused, f"live calls attempted: {refused}"
