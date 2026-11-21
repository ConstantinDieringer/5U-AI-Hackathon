"""Round 20 (mail 4919): "truck from our warehouse", no ZIP, no city. The pickup site was taken
from the customer's previous request (61391, Bad Homburg -> FRA), although Weidner ships from
a different site each time (61391, 41007, 72379). The quote was rejected too_high.

ASK_PICKUP_ZIP=true asks once for the pickup ZIP instead. Only a site the customer named for
this shipment counts: the current text without signature, or earlier mail of this thread.
Default false keeps today's behaviour.
"""

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from app import config, db
from app.agents import bids, clarification, rfq
from app.mail.rfq_validation import validate
from app.models import Mail, Rfq, RfqDraft
from app.pricing.portal_sync import PortalResult
from app.ui import drafts

CUSTOMER = "weidner-spielwaren@hackloop.mail"
TEAM = "austrian-power@hackloop.mail"
ROUND_20 = (
    "Hi there,\n\nHope your week’s going well! Could you quote the fastest air freight option "
    "to RUH (Riyadh, SA) and let me know the earliest arrival you can offer?\n\nWe have 7 "
    "pieces of automotive sensors, with a gross weight of 970 kg and a total volume of 9.06 "
    "cbm. The shipment will be ready for pickup on 2026-11-27. We’ll need you to arrange the "
    "truck from our warehouse to the airport.\n\nCould you send me an all-in price in EUR, "
    "including pickup?\n\nThanks for checking!\n\nCheers,\nKlaus Weidner\n"
    "Weidner Spielwaren GmbH, Nuremberg"
)
EXTRACTED = {  # what the extraction returned in round 20
    "origin": None,
    "pickup_zip": None,
    "destination": "RUH",
    "weight_kg": 970.0,
    "volume_cbm": 9.06,
    "pieces": 7,
    "preference": "fastest",
    "ready_date": "2026-11-27",
    "language": "en",
    "customer_name": "Klaus",
}
EARLIER = (  # round 10 (2256), an older shipment of the same customer
    "Could you send me an all-in price in EUR for the cheapest air freight option to GRU? "
    "Please include pickup from our site in 61391 (Bad Homburg)."
)


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "t.db"))
    sent: list[dict] = []

    def send(to, subject, body, in_reply_to=None, **_k):
        sent.append({"id": 90_000 + len(sent), "to": to, "subject": subject, "body": body})
        return sent[-1]

    def complete(purpose, *_a, **_k):
        if purpose.startswith("extract"):
            return json.dumps(EXTRACTED)
        raise ValueError("no composer in this test")

    for module in (rfq.hackloop, bids.hackloop, clarification.hackloop):
        monkeypatch.setattr(module, "send", send)
    monkeypatch.setattr(rfq.llm, "complete", complete)
    monkeypatch.setattr(rfq, "portal_result", lambda *_a, **_k: PortalResult())
    monkeypatch.setattr(drafts, "portal_result", lambda *_a, **_k: PortalResult())
    monkeypatch.setattr(bids, "wait", lambda *_a, **_k: [])

    def configure(*, ask: bool, mode: str = "shadow") -> None:
        new = dataclasses.replace(config.settings, ask_pickup_zip=ask, rfq_validation_mode=mode)
        for module in (config, rfq, clarification):
            monkeypatch.setattr(module, "settings", new)

    # the customer's previous request, the source of today's guess
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    db.ingest_mail(
        mail_id=2256,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Air freight to GRU",
        body=EARLIER,
        category="rfq",
        received_at=old,
    )
    db.set_request(2256, {**EXTRACTED, "destination": "GRU", "pickup_zip": "61391"})
    return configure, sent


def _round_20(body: str = ROUND_20, mid: int = 4919) -> Mail:
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=mid,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Air freight quote to RUH",
        body=body,
        category="rfq",
        received_at=now,
    )
    return Mail(mid, mid, CUSTOMER, TEAM, "Air freight quote to RUH", body, now)


def test_default_still_prices_the_previous_shipments_site(setup) -> None:
    configure, sent = setup
    configure(ask=False)
    rfq.handle_rfq(_round_20(), 4919)
    assert db.get_session(4919)["request"]["pickup_zip"] == "61391"
    truck_requests = [m for m in sent if "Pickup 61391 to FRA" in m["subject"]]
    assert truck_requests and not [m for m in sent if m["to"] == CUSTOMER]


def test_round_20_asks_once_for_the_pickup_zip(setup) -> None:
    configure, sent = setup
    configure(ask=True)
    rfq.handle_rfq(_round_20(), 4919)
    assert [m["to"] for m in sent] == [CUSTOMER]  # no trucker, no airline, no quote
    assert "postal code" in sent[0]["body"]
    assert db.get_session(4919)["quotes"] == []
    row = db.validation_get(4919)
    assert row["state"] == "awaiting_clarification"
    assert [i["field"] for i in row["draft"]["issues"]] == ["pickup_zip"]


def test_answer_with_the_zip_resumes_the_quote(setup, monkeypatch) -> None:
    configure, _ = setup
    configure(ask=True)
    rfq.handle_rfq(_round_20(), 4919)
    resumed = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda mail, sid: resumed.append(sid))
    answer = Mail(
        99_999, 4919, CUSTOMER, TEAM, "Re: Air freight quote to RUH",
        "Sure, the warehouse is in 93055 Regensburg.\n\nCheers,\nKlaus",
        datetime.now(UTC).isoformat(), references=[4919],
    )  # fmt: skip
    db.ingest_mail(
        mail_id=answer.id,
        sender=CUSTOMER,
        recipient=TEAM,
        subject=answer.subject,
        body=answer.body,
        category="customer",
        received_at=answer.received_at,
        references=[4919],
    )
    assert clarification.handle_reply(answer, 4919)
    row = db.validation_get(4919)
    assert resumed == [4919], (row["state"], row["draft"]["issues"])
    request = db.get_session(4919)["request"]
    assert request["pickup_zip"] == "93055" and Rfq.model_validate(request).pickup_zone == "93"


def test_area_in_the_current_text_is_used(setup) -> None:
    configure, _ = setup
    configure(ask=True)
    body = ROUND_20.replace("from our warehouse", "from our warehouse in Bad Homburg")
    mail = _round_20(body)
    found = rfq.locate_pickup(Rfq.model_validate(EXTRACTED), mail, 4919)
    assert found.pickup_city == "Bad Homburg" and found.pickup_assumed


def test_signature_city_is_not_a_pickup_site(setup) -> None:
    configure, _ = setup
    body = ROUND_20.replace("GmbH, Nuremberg", "GmbH, Frankfurt")
    mail = _round_20(body)
    configure(ask=False)
    assert rfq.locate_pickup(Rfq.model_validate(EXTRACTED), mail, 4919).pickup_city
    configure(ask=True)
    found = rfq.locate_pickup(Rfq.model_validate(EXTRACTED), mail, 4919)
    assert not (found.pickup_city or found.pickup_zip)
    assert rfq.needs_pickup_site(found, mail)


def test_earlier_mail_in_the_same_thread_gives_the_zip(setup) -> None:
    configure, _ = setup
    configure(ask=True)
    root = _round_20("Please quote 970 kg / 9.06 cbm to RUH, pickup from our site in 72379.")
    follow = Mail(
        4930, 4919, CUSTOMER, TEAM, "Re: Air freight quote to RUH", ROUND_20,
        datetime.now(UTC).isoformat(), references=[4919],
    )  # fmt: skip
    db.ingest_mail(
        mail_id=follow.id,
        sender=CUSTOMER,
        recipient=TEAM,
        subject=follow.subject,
        body=follow.body,
        category="customer",
        received_at=follow.received_at,
        references=[root.id],
    )
    found = rfq.locate_pickup(Rfq.model_validate(EXTRACTED), follow, 4919)
    assert found.pickup_zip == "72379" and not found.pickup_assumed


def test_enforce_mode_history_zip_without_label(setup) -> None:
    """Enforce already asks for round 20; the setting lets an unlabelled ZIP from earlier mail
    of the same thread close the gap without a question."""
    configure, _ = setup
    root = _round_20("Pickup from our site in 72379, details follow.")
    follow = Mail(
        4930, 4919, CUSTOMER, TEAM, "Re: Air freight quote to RUH", ROUND_20,
        datetime.now(UTC).isoformat(), references=[4919],
    )  # fmt: skip
    db.ingest_mail(
        mail_id=follow.id,
        sender=CUSTOMER,
        recipient=TEAM,
        subject=follow.subject,
        body=follow.body,
        category="customer",
        received_at=follow.received_at,
        references=[root.id],
    )
    _, draft = validate(follow, RfqDraft(facts=EXTRACTED))
    assert [i["field"] for i in draft.issues] == ["pickup_zip"]
    configure(ask=False, mode="enforce")
    assert clarification.recover_history(follow, 4919, draft)[0] is None
    configure(ask=True, mode="enforce")
    request, checked = clarification.recover_history(follow, 4919, draft)
    assert checked.issues == [] and request.pickup_zip == "72379"


def test_emergency_draft_does_not_guess(setup) -> None:
    configure, _ = setup
    _round_20()
    db.set_request(4919, EXTRACTED)
    configure(ask=False)
    assert drafts.build(4919)["choice"]["origin"] == "FRA"  # today: 61391 of an older shipment
    configure(ask=True)
    with pytest.raises(ValueError, match="PLZ"):
        drafts.build(4919)


def test_replayed_round_20_asks_for_the_zip_in_every_mode(tmp_path, monkeypatch) -> None:
    from app.agents import rfq_replay

    corpus = json.loads(rfq_replay.CORPUS.read_text(encoding="utf-8"))
    case = next(c for c in corpus["cases"] if c["round_no"] == 20)
    assert case["llm_extraction"]["pickup_zip"] is None  # the stored guess is not LLM output
    for mode in ("shadow", "enforce"):
        new = dataclasses.replace(config.settings, ask_pickup_zip=True, rfq_validation_mode=mode)
        for module in (config, rfq, clarification):
            monkeypatch.setattr(module, "settings", new)
        got = rfq_replay.replay_case(case, tmp_path / mode, corpus["lane_observations"])
        assert (got["outcome"], got["open_issues"], got["carriers_asked"]) == (
            "clarification",
            ["pickup_zip"],
            [],
        ), mode
