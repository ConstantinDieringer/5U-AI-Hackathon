"""History-first recovery and repeated clarification on the unified validation workflow."""

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from app import db
from app.agents import clarification, rfq
from app.mail.rfq_validation import validate
from app.models import Mail, RfqDraft

OWNER = "owner@hackloop.mail"


def mail(mid, body, sid=None, sender=OWNER):
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(
        mail_id=mid,
        sender=sender,
        recipient="austrian-power@hackloop.mail",
        subject="RFQ",
        body=body,
        category="customer" if sid else "rfq",
        references=[sid] if sid else [],
        received_at=now,
    )
    return Mail(mid, sid or mid, sender, "austrian-power@hackloop.mail", "RFQ", body, now)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "history.db"))
    monkeypatch.setattr(
        clarification,
        "settings",
        dataclasses.replace(clarification.settings, rfq_validation_mode="enforce"),
    )
    monkeypatch.setattr(
        clarification.llm,
        "complete",
        lambda *args, **kwargs: json.dumps({"corrections": []}),
    )


@pytest.fixture
def sent(monkeypatch):
    items = []

    def send(*args, **kwargs):
        items.append((args, kwargs))
        mid = db.conn().execute("SELECT COALESCE(MAX(id),0)+1 FROM mails").fetchone()[0]
        return {"id": mid}

    monkeypatch.setattr(clarification.hackloop, "send", send)
    return items


@pytest.mark.core
def test_partial_answers_accumulate_and_repeat_only_for_new_mail(sent, monkeypatch):
    root = mail(1, "Origin: FRA\nDestination: JFK")
    _, draft = validate(root, RfqDraft(facts={"origin": "FRA", "destination": "JFK"}))
    clarification.request(root, 1, draft)
    first = mail(3, "Gross weight: 100 kg", sid=1)
    assert clarification.handle_reply(first, 1)
    assert len(sent) == 2
    assert db.validation_get(1)["state"] == "awaiting_clarification"
    assert clarification.handle_reply(first, 1)  # duplicate delivered again
    assert len(sent) == 2
    second = mail(5, "Volume: 2 cbm", sid=1)
    resumed = []
    monkeypatch.setattr(rfq, "handle_rfq", lambda *args: resumed.append(args))
    assert clarification.handle_reply(second, 1)
    assert db.validation_get(1)["state"] == "ready" and len(resumed) == 1
    request = db.get_session(1)["request"]
    assert request["weight_kg"] == 100 and request["volume_cbm"] == 2
    assert db.get_session(1)["first_reply_at"] is None


ANNA_ROOT = (
    "Could you please quote the same shipment as in my earlier request? The cargo and pickup"
    " details are unchanged. The new destination is LAX, ready for pickup on 2026-11-29."
    " Please send me an all-in price in EUR for the cheapest option."
)
ANNA_REPLY = (
    "The departure airport is Stuttgart (STR), and the destination for this shipment is LAX."
    " The shipment consists of 6 pieces, with a total gross weight of 1,841 kg and a total"
    " volume of 4.63 cbm. We will deliver the shipment to STR ourselves, so no pickup is required."
)


def _rescue_plan(corrections):
    from app.agents import rescue

    return rescue.Plan.model_validate(
        {
            "action": "retry_pricing",
            "reason": "customer answered",
            "corrections": [{"field": f, "value": v, "evidence": e} for f, v, e in corrections],
            "releases": [],
            "question_en": "",
            "question_de": "",
        }
    )


@pytest.mark.core
def test_rescue_plan_names_open_fields():
    """Session 30351: a blocked rescue plan expired silently. It must say what stays open;
    a complete answer still builds the RFQ."""
    from app.agents import rescue

    root = mail(1, ANNA_ROOT)
    reply = mail(3, ANNA_REPLY, sid=1)
    facts = {"destination": "LAX", "preference": "cheapest", "ready_date": "2026-11-29"}
    _, draft = validate(root, RfqDraft(facts=facts))
    evidence = rescue._normal(ANNA_ROOT + "\n" + ANNA_REPLY)
    full = [
        ("origin", "STR", "The departure airport is Stuttgart (STR)"),
        ("weight_kg", "1841", "with a total gross weight of 1,841 kg"),
        ("volume_cbm", "4.63", "a total volume of 4.63 cbm"),
        ("pieces", "6", "The shipment consists of 6 pieces"),
        ("no_pickup", "true", "so no pickup is required"),
    ]
    request, _ = rescue._apply(
        reply, 1, root, draft.model_copy(deep=True), _rescue_plan(full), evidence
    )
    assert request and request.origin == "STR" and request.weight_kg == 1841

    # counterexample: the origin quote is not in the customer's words, so it stays open
    partial = [("origin", "STR", "we ship from Stuttgart")] + full[1:]
    request, checked = rescue._apply(
        reply, 1, root, draft.model_copy(deep=True), _rescue_plan(partial), evidence
    )
    assert request is None
    assert "origin" in {i["field"] for i in checked.issues}


@pytest.mark.core
def test_rescue_reads_grouped_weight_from_customer_answer():
    """Session 30351 again: the live plan only released holds and left the values to the
    customer's own words. "1,841 kg" next to 6 pieces and 4.63 cbm is 1841 kg, never 1.841."""
    from app.agents import rescue

    root = mail(1, ANNA_ROOT)
    reply = mail(3, ANNA_REPLY, sid=1)
    facts = {"destination": "LAX", "preference": "cheapest", "ready_date": "2026-11-29"}
    _, draft = validate(root, RfqDraft(facts=facts))
    evidence = rescue._normal(ANNA_ROOT + "\n" + ANNA_REPLY)
    plan = _rescue_plan([])
    plan.releases = [
        rescue.Release(
            field="shipment",
            code="invalid",
            evidence="with a total gross weight of 1,841 kg",
            reason="the customer gave the weight",
        )
    ]
    request, _ = rescue._apply(reply, 1, root, draft.model_copy(deep=True), plan, evidence)
    assert request and request.origin == "STR"
    assert request.weight_kg == 1841 and request.volume_cbm == 4.63


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("6 pieces, total gross weight 1,841 kg, 4.63 cbm", 1841.0),
        ("one small parcel of 2,5 kg", 2.5),  # counterexample: a real decimal stays a decimal
    ],
)
def test_free_text_weight_never_takes_the_blind_decimal(body, expected):
    from app.mail.rfq_validation import free_text_answer

    assert free_text_answer(body, {"weight_kg"}, "LAX").get("weight_kg") == expected


WEIDNER_IMPERIAL = (
    "Could you send me an all-in price in EUR for the cheapest air freight option from MUC to"
    " YYZ. The import shipment consists of 5 pieces of automotive sensors, with a gross weight"
    " of 3258.44 lb and a total volume of 306.18 cubic feet. The goods will be ready on"
    " 2026-11-25, and we'll deliver them to MUC ourselves."
)


@pytest.mark.core
def test_cubic_feet_volume_is_converted_before_pricing():
    """Round 109 (session 32120, too_high): "306.18 cubic feet" was priced as 306.18 cbm,
    51 t chargeable instead of about 1.5 t. Code totals override the model's number."""
    root = mail(1, WEIDNER_IMPERIAL)
    model = {"origin": "MUC", "destination": "YYZ", "weight_kg": 1478.0, "volume_cbm": 306.18}
    checked, _ = validate(root, RfqDraft(facts=model))
    assert checked.volume_cbm == pytest.approx(8.6701, abs=0.001)
    assert checked.weight_kg == pytest.approx(1478.0, abs=0.1)


@pytest.mark.parametrize(
    ("text", "cbm"),
    [
        ("total volume of 306.18 cubic feet", 8.6701),
        ("total volume of 120 cu ft", 3.3980),
        ("total volume of 4.63 cbm", 4.63),  # counterexample: metric stays as written
        ("total volume of 4.63 cubic metres", 4.63),
        ("total volume of 850 litres", 0.85),
        ("total volume of 3 cubic yards", None),  # unknown unit is asked, never guessed
    ],
)
def test_volume_units(text, cbm):
    from app.mail.rfq_validation import numeric_facts

    value = numeric_facts(text).get("volume_cbm")
    assert (value is None) if cbm is None else float(value) == pytest.approx(cbm, abs=0.001)


def test_implausible_volume_is_confirmed_before_pricing():
    """Round 109: 1478 kg at 306.18 cbm (4.8 kg/cbm) was quoted at 102,709 EUR, too_high."""
    facts = {"origin": "MUC", "destination": "YYZ", "weight_kg": 1478, "volume_cbm": 306.18}
    root = mail(1, "Air freight MUC to YYZ, 1478 kg gross, 306.18 cbm")
    _, draft = validate(root, RfqDraft(facts=facts))
    assert {"field": "volume_cbm", "code": "unproven"}.items() <= draft.issues[-1].items()

    plausible = {**facts, "volume_cbm": 3.06}
    root = mail(2, "Air freight MUC to YYZ, 1478 kg gross, 3.06 cbm")
    _, draft = validate(root, RfqDraft(facts=plausible))
    assert not [i for i in draft.issues if i["field"] == "volume_cbm"]


AUER_ROOT = (
    "I would like a quote for an air freight shipment to BKK (Bangkok, TH). The goods must be"
    " picked up from our warehouse and trucked to the departure airport by you. The cargo will"
    " be ready for pickup on 2026-12-04. The shipment consists of 12 pieces of machine parts."
    " Total gross weight 1885 kg, volume 11.96 cbm. We want the cheapest option.\n\n"
    "Best greetings from Regensburg\nTobias Auer"
)
AUER_REPLY = (
    "Hello Moritz,\n\nSorry I left out the pickup location. The goods are to be collected in"
    " Augsburg, Germany.\n\nBest greetings from Regensburg\nTobias Auer"
)


def _auer_thread(questions):
    """Root RFQ, `questions` clarifications sent, each answered with the same city."""
    root = mail(1, AUER_ROOT)
    _, draft = validate(root, RfqDraft(facts={"weight_kg": 1885.0, "volume_cbm": 11.96}))
    db.validation_save(1, "awaiting_clarification", draft.model_dump(mode="json"), {})
    reply_to = 1
    for n in range(questions):
        out_id, in_id = 100 + 2 * n, 101 + 2 * n
        db.record_outgoing(
            mail_id=out_id,
            recipient=OWNER,
            subject="Re: RFQ",
            body="Could you please send the pickup ZIP code?",
            category="clarification",
            session_id=1,
            in_reply_to=reply_to,
            references=[1],
        )
        if n < questions - 1:
            mail(in_id, AUER_REPLY, sid=1)
        reply_to = in_id
    return root, mail(reply_to, AUER_REPLY, sid=1), draft


def _ask_plan(pickup_evidence):
    from app.agents import rescue

    return rescue.Plan.model_validate(
        {
            "action": "ask_customer",
            "reason": "pricing still requires the pickup ZIP",
            "corrections": [
                {"field": "destination", "value": "BKK", "evidence": "BKK (Bangkok, TH)"},
                {"field": "pickup_city", "value": "Augsburg", "evidence": pickup_evidence},
                {"field": "preference", "value": "cheapest", "evidence": "the cheapest option"},
            ],
            "releases": [],
            "question_en": "Could you please send the pickup ZIP code in Augsburg?",
            "question_de": "Kannst du uns bitte die PLZ in Augsburg schicken?",
        }
    )


def _run_take_over(monkeypatch, root, reply, plan):
    from app.agents import rescue
    from app.mail import multilingual

    evidence = rescue._normal(AUER_ROOT + "\n" + AUER_REPLY)
    monkeypatch.setattr(multilingual, "prepare", lambda m, *a, **k: m)
    monkeypatch.setattr(rescue, "_decision", lambda *a: (plan, "key", evidence))
    asked, priced = [], []
    monkeypatch.setattr(clarification, "request", lambda *a: asked.append(a))
    monkeypatch.setattr(rfq, "handle_rfq", lambda *a: priced.append(a))
    assert rescue.take_over(reply, 1)
    return asked, priced


@pytest.mark.core
def test_answered_pickup_city_is_priced_instead_of_asking_for_the_zip_again(monkeypatch):
    """Session 38891: the customer named Augsburg after our first question and then ten
    more times; each plan cited Augsburg and BKK yet asked for the ZIP until the deadline.
    A complete cited answer is priced by zone, the quote confirms the exact address."""
    root, reply, _ = _auer_thread(questions=1)
    plan = _ask_plan("The goods are to be collected in Augsburg, Germany.")
    asked, priced = _run_take_over(monkeypatch, root, reply, plan)
    assert not asked and len(priced) == 1
    request = db.get_session(1)["request"]
    assert request["pickup_city"] == "Augsburg" and request["pickup_assumed"] is True
    assert request["destination"] == "BKK"


@pytest.mark.core
def test_uncited_pickup_city_still_asks(monkeypatch):
    """Counterexample: a city the customer never wrote is no answer, so we ask."""
    root, reply, _ = _auer_thread(questions=1)
    plan = _ask_plan("We collect from Augsburg")  # not in the customer's words
    asked, priced = _run_take_over(monkeypatch, root, reply, plan)
    assert len(asked) == 1 and not priced


@pytest.mark.core
def test_question_loop_stops_after_the_limit(monkeypatch):
    """Even an unresolvable gap gets at most THREAD_RESCUE_MAX_QUESTIONS automatic questions."""
    root, reply, _ = _auer_thread(questions=3)
    plan = _ask_plan("We collect from Augsburg")
    asked, priced = _run_take_over(monkeypatch, root, reply, plan)
    assert not asked and not priced
    events = db.get_session(1)["events"]
    assert any(e["kind"] == "thread_rescue_blocked" and e["level"] == "error" for e in events)
