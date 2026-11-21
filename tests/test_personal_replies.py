"""Individual replies use real customer conversation, with current facts isolated."""

import json

import pytest

from app import db, llm
from app.agents import rfq
from app.mail.conversation import quote_conversation
from app.models import Mail, RateOption, Rfq

CUSTOMER = "sara@hackloop.mail"
TEAM = "austrian-power@hackloop.mail"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))


def incoming(mid, sender=CUSTOMER, body="Please quote FRA to DEL.", refs=(), day=21):
    return db.ingest_mail(
        mail_id=mid,
        sender=sender,
        recipient=TEAM,
        subject="Freight request",
        body=body,
        category="rfq" if not refs else "customer",
        received_at=f"2026-11-{day:02d}T12:00:00+00:00",
        references=refs,
    )


def outgoing(mid, sid, recipient=CUSTOMER, body="Here is your quote: 950,00 €."):
    db.record_outgoing(
        mail_id=mid,
        recipient=recipient,
        session_id=sid,
        subject="Quote",
        body=body,
        category="quote",
        sent_at="2026-11-20T12:01:00+00:00",
    )


def option():
    return RateOption("Test Air", "FRA", "DEL", 2, 120000, "EUR", 120000, "sheet")


def payload(body, ids=()):
    return json.dumps(
        {
            "body": body,
            "answered_questions": [],
            "unavailable_questions": [],
            "addressed_constraints": [],
            "unavailable_constraints": [],
            "referenced_mail_ids": list(ids),
        }
    )


def test_context_includes_both_sides_of_same_customer_only():
    incoming(1, day=20, body="Thanks for explaining the air freight process.")
    outgoing(2, 1)
    incoming(3, sender="other@hackloop.mail", day=20, body="OTHER CUSTOMER SECRET")
    incoming(10, body="A new request to Delhi.")
    incoming(11, body="The cargo is ready on Tuesday.", refs=(10,))
    incoming(12, sender="carrier@hackloop.mail", body="CARRIER SECRET", refs=(10,))
    outgoing(13, 10, recipient="other@hackloop.mail", body="WRONG RECIPIENT SECRET")
    incoming(20, day=22, body="FUTURE SHIPMENT")
    context = quote_conversation(10)
    assert context["current_customer_message"] == "A new request to Delhi."
    assert {m["mail_id"] for m in context["earlier_messages"]} == {1, 2, 11}
    assert {m["speaker"] for m in context["earlier_messages"]} == {"customer", "our_team"}
    text = json.dumps(context)
    assert "SECRET" not in text and "FUTURE" not in text and "950,00" not in text


def test_history_is_bounded_and_quotes_are_not_reintroduced():
    incoming(1, day=20, body="A" * 3000 + "\nOn Monday wrote:\nOBSOLETE")
    for mid in range(2, 9):
        incoming(mid, body="B" * 3000, refs=(1,), day=20)
    incoming(10)
    context = quote_conversation(10)
    assert len(context["earlier_messages"]) == 4
    assert all(len(m["text"]) <= 950 for m in context["earlier_messages"])
    assert "OBSOLETE" not in json.dumps(context)
    assert quote_conversation(999)["earlier_messages"] == []


def test_standard_quote_is_individual_and_receives_grounded_context(monkeypatch):
    incoming(1, day=20, body="Thank you for helping with our first request.")
    outgoing(2, 1)
    incoming(10, body="Could you help us with Delhi this time?")
    sent = []
    body = (
        "Hi Sara,\n\nLovely to hear from you again. For Delhi this time, Test Air can "
        "take your cargo from FRA to DEL for 1.200,00 € all-in, with two days in transit."
        "\n\nIf that works for you, reply here and we can arrange the booking."
        "\n\nYour austrian Power Team"
    )

    def complete(purpose, messages, *args, **kwargs):
        assert kwargs["model_name"] == rfq.settings.llm_compose_model
        sent.append((purpose, json.loads(messages[1]["content"])))
        return payload(body, [1])

    monkeypatch.setattr(llm, "complete", complete)
    request = Rfq(origin="FRA", destination="DEL", weight_kg=736, customer_name="Sara")
    text = rfq.compose_quote_text(request, option(), 120000, None, 10)
    assert "Lovely to hear from you again" in text
    assert [p[0] for p in sent] == ["compose_quote"]
    assert sent[0][1]["verified_facts"]["exact_all_in_price"] == "1.200,00 €"
    assert sent[0][1]["conversation"]["earlier_messages"][0]["mail_id"] == 1
    event = [e for e in db.events_after(0) if e["kind"] == "personal_reply"][-1]
    assert event["payload"]["referenced_mail_ids"] == [1]


def test_first_contact_has_no_invented_history_and_dash_keeps_personal_copy(monkeypatch):
    incoming(10)
    received = []
    body = (
        "Hi Sara — I looked at your request. Test Air flies FRA to DEL for "
        "1.200,00 € all-in.\n\nYour austrian Power Team"
    )

    def complete(purpose, messages, *args, **kwargs):
        received.append(json.loads(messages[1]["content"]))
        return payload(body)

    monkeypatch.setattr(llm, "complete", complete)
    request = Rfq(origin="FRA", destination="DEL", weight_kg=736)
    text = rfq.compose_quote_text(request, option(), 120000, None, 10)
    assert "I looked at your request" in text and "—" not in text
    assert len(received) == 1
    assert received[0]["conversation"]["earlier_messages"] == []
    assert "first conversation" in received[0]["verified_facts"]["personal_opening_guidance"]


def test_composer_rejects_history_id_not_in_customer_context():
    request = Rfq(origin="FRA", destination="DEL", weight_kg=736)
    with pytest.raises(ValueError, match="reference was not supplied"):
        rfq._validate_composed(payload("irrelevant", [99]), request, option(), "1.200,00 €", {1})


def test_extraction_repair_leaves_budget_for_individual_composer(monkeypatch):
    from types import SimpleNamespace

    incoming(10)
    request = Rfq(origin="FRA", destination="DEL", weight_kg=736, volume_cbm=6.88)
    body = "Hello,\n\nTest Air flies FRA to DEL for 1.200,00 € all-in.\n\nYour austrian Power Team"
    replies = iter(["invalid", request.model_dump_json(), payload(body)])

    def create(**kwargs):
        return SimpleNamespace(
            model="gpt-5-nano",
            usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content=next(replies)))],
        )

    monkeypatch.setattr(llm._client.chat.completions, "create", create)
    mail = Mail(
        10,
        10,
        CUSTOMER,
        TEAM,
        "FRA to DEL",
        "Please quote 736 kg gross, 6.88 cbm.",
        "2026-11-21T12:00:00+00:00",
    )
    parsed = rfq.extract_rfq(mail, 10)
    assert parsed is not None
    assert "Test Air" in rfq.compose_quote_text(parsed, option(), 120000, None, 10)
    assert [c["purpose"] for c in db.session_llm_usage(10)["calls"]] == [
        "extract_rfq",
        "extract_rfq_retry",
        "compose_quote",
    ]
