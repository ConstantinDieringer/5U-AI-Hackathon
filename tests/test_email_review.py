"""A stronger final reviewer may edit style, never protected shipment facts."""

import dataclasses
import json

import httpx
import pytest

from app import db, hackloop, llm
from app.agents import email_review
from app.mail.outgoing import CustomerStyleError, customer_body, validate_customer_text

BODY = (
    "Hi Sara,\n\nTest Air flies FRA to DEL for 911,68 €.\n\n"
    "The earliest possible departure is 29 November 2026.\n\n"
    "Your booking reference is BK-MCN63DLN.\n\nYour austrian Power Team\n"
)


@pytest.fixture(autouse=True)
def enabled_review(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "review.db"))
    monkeypatch.setattr(
        email_review,
        "settings",
        dataclasses.replace(
            email_review.settings,
            email_review_enabled=True,
            llm_review_model="gpt-5",
        ),
    )


def test_one_stronger_review_corrects_the_draft_and_reaches_the_transport(monkeypatch):
    checked, wire = [], []

    def complete(purpose, messages, session_id, **kwargs):
        checked.append((purpose, session_id, kwargs))
        data = json.loads(messages[1]["content"])
        assert data["immutable_names"] == ["Test Air"]
        return json.dumps({"body": data["draft"].replace("Hi Sara,", "Hello Sara,")})

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(hackloop, "send", lambda **kw: wire.append(kw) or {"id": 12})
    sent = hackloop.send_customer(
        "sara@example.test",
        "Re: RFQ",
        BODY,
        in_reply_to=10,
        session_id=99,
        protected_terms=("Test Air",),
    )
    assert len(checked) == 1 and checked[0][:2] == ("review_customer_email", 99)
    assert checked[0][2]["model_name"] == "gpt-5"
    assert sent["body"] == wire[0]["body"] and sent["body"].startswith("Hello Sara,")
    assert wire[0]["in_reply_to"] == 10
    assert "BK-MCN63DLN" in sent["body"] and "911,68 €" in sent["body"]


@pytest.mark.parametrize(
    "old,new",
    [
        ("911,68", "900"),
        ("€", "$"),
        ("BK-MCN63DLN", "BK-OTHER"),
        ("November", "December"),
        ("earliest possible", "confirmed"),
        ("Test Air", "Other Air"),
        ("Your austrian Power Team", "Another Team"),
    ],
)
def test_changed_facts_or_qualification_are_discarded(monkeypatch, old, new):
    calls = []
    monkeypatch.setattr(
        llm,
        "complete",
        lambda *a, **k: calls.append(a) or json.dumps({"body": BODY.replace(old, new)}),
    )
    assert email_review.review_customer_mail(BODY, "en", 99, ("Test Air",)) == BODY
    assert len(calls) == 1
    assert any(e["kind"] == "email_style_review_fallback" for e in db.events_after(0))


def test_reviewer_timeout_keeps_checked_text_without_a_retry(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError("test timeout")

    monkeypatch.setattr(llm, "complete", fail)
    assert email_review.review_customer_mail(BODY, "en", 99) == BODY


@pytest.mark.parametrize("extra", ["Your shipment is confirmed.", "Here is your exclusive price."])
def test_reviewer_cannot_add_confirmation_or_a_fake_exclusive_offer(monkeypatch, extra):
    monkeypatch.setattr(
        llm,
        "complete",
        lambda *a, **k: json.dumps({"body": BODY.replace("Hi Sara,", "Hi Sara,\n\n" + extra)}),
    )
    assert email_review.review_customer_mail(BODY, "en", 99) == BODY


def test_pause_during_review_prevents_sending(monkeypatch):
    db.ingest_mail(
        mail_id=99,
        sender="sara@example.test",
        recipient="team",
        subject="RFQ",
        body="hi",
        category="rfq",
    )

    def complete(*args, **kwargs):
        db.set_paused(99, True)
        return json.dumps({"body": BODY})

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(hackloop, "send", lambda **kw: pytest.fail("Paused mail must not be sent"))
    with pytest.raises(hackloop.CustomerSendBlocked):
        hackloop.send_customer("sara@example.test", "Re: RFQ", BODY, session_id=99)


def test_internal_field_cannot_reach_transport(monkeypatch):
    monkeypatch.setattr(
        hackloop, "send", lambda **kw: pytest.fail("Internal labels must not be sent")
    )
    with pytest.raises(CustomerStyleError):
        hackloop.send_customer("sara@example.test", "RFQ", "The value is road_transit.")


def test_deadline_expiring_during_review_prevents_sending(monkeypatch):
    clock = [10.0]

    def complete(*args, **kwargs):
        clock[0] = 12.0
        return json.dumps({"body": BODY})

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(hackloop.time, "time", lambda: clock[0])
    monkeypatch.setattr(hackloop, "send", lambda **kw: pytest.fail("Expired mail must not be sent"))
    with pytest.raises(hackloop.CustomerSendBlocked):
        hackloop.send_customer("sara@example.test", "Re: RFQ", BODY, deadline_at=11.0)


def test_screenshot_format_is_fixed_even_when_reviewer_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        email_review,
        "settings",
        dataclasses.replace(email_review.settings, email_review_enabled=False),
    )
    wire = []
    monkeypatch.setattr(hackloop, "send", lambda **kw: wire.append(kw) or {"id": 22})
    draft = (
        "Hi Deniz,\n\nBooking reference: BK-MCN63DLN\nRoute: MUC to ORD with Xenon Cargo Airlines\n"
        "Price: 911,68 € all-in\n\nPickup from 81311 (Munich South) takes 4.0 hours; "
        "the earliest possible departure is 2026-11-29. The earliest possible arrival is 02-12-2026."
        "\n\nYour austrian Power Team"
    )
    sent = hackloop.send_customer("deniz@example.test", "Re: RFQ", draft)
    text = sent["body"]
    assert text == wire[0]["body"]
    assert "29 November 2026" in text and "2 December 2026" in text and "4 hours" in text
    assert "911,68 €" in text and "BK-MCN63DLN" in text
    plain = text.replace("BK-MCN63DLN", "")
    assert not any(symbol in plain for symbol in (":", ";", "_", "-", "(", ")"))
    assert "\n\n" in text and "Munich South" in text


def test_zero_decimals_preserve_real_fractions_and_thousands():
    text = customer_body(
        "4.0 hours and 736.00 kg. Total 1.200,00 EUR. Also 911,68 € and 100.000 kg.", "en"
    )
    assert "4 hours" in text and "736 kg" in text and "1.200 €" in text
    assert "911,68 €" in text and "100.000 kg" in text


@pytest.mark.parametrize(
    "colon,semicolon", [(":", ";"), ("：", "；"), ("∶", ";"), ("&#58;", "&semi;")]
)
def test_wire_payload_and_subject_are_clean_even_when_reviewer_reintroduces_punctuation(
    monkeypatch, colon, semicolon
):
    wire = []

    def receive(request):
        payload = json.loads(request.content)
        wire.append(payload)
        return httpx.Response(201, json={"id": 100, **payload})

    monkeypatch.setattr(
        hackloop,
        "_client",
        httpx.Client(base_url="https://mail.invalid", transport=httpx.MockTransport(receive)),
    )
    monkeypatch.setattr(
        llm,
        "complete",
        lambda *a, **k: json.dumps(
            {
                "body": BODY.replace(
                    "Hi Sara,", f"Hi Sara{colon} thanks{semicolon} good to hear from you."
                )
            }
        ),
    )
    sent = hackloop.send_customer(
        "sara@example.test",
        f"Re{colon} Quote{semicolon} FRA–DEL",
        BODY,
        in_reply_to=10,
        session_id=99,
    )
    assert len(wire) == 1
    assert wire[0]["subject"] == "Quote. FRA to DEL"
    assert sent["subject"] == wire[0]["subject"] and sent["body"] == wire[0]["body"]
    assert wire[0]["in_reply_to"] == 10
    assert "911,68 €" in sent["body"] and "BK-MCN63DLN" in sent["body"]
    for text in (sent["subject"], sent["body"].replace("BK-MCN63DLN", "")):
        assert not any(c in text for c in ":;_()-：；∶;")


@pytest.mark.parametrize(
    "text",
    [
        "Price: 100 €",
        "Thanks; all good",
        "Thanks&#59; all good",
        "Price： 100 €",
        "100 EUR",
        "4.0 hours",
        "2026-11-29",
    ],
)
def test_independent_final_assertion_rejects_unrendered_text(text):
    with pytest.raises(CustomerStyleError):
        validate_customer_text(text)


def test_final_assertion_still_blocks_if_renderer_regresses(monkeypatch):
    from app.mail import outgoing

    monkeypatch.setattr(
        email_review,
        "settings",
        dataclasses.replace(email_review.settings, email_review_enabled=False),
    )
    monkeypatch.setattr(outgoing, "customer_body", lambda text, lang: "Price: 100 €")
    monkeypatch.setattr(
        hackloop, "send", lambda **kw: pytest.fail("Invalid text reached transport")
    )
    with pytest.raises(CustomerStyleError):
        hackloop.send_customer("sara@example.test", "Quote", BODY)
