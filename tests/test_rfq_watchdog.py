"""RFQ watchdog (RFQ_WATCHDOG): an RFQ thread still without our quote ~200 s after arrival gets
the best quote real prices allow, once, through the one-quote claim. Never for a paused or
taken-over session, never on open dangerous goods, an excluded carrier or a phishing mail; no
real price means nothing is sent and an alert is raised. Also: a restart in the middle of a
round (rounds 4, 9, 15 to 17 had pushes inside the round)."""

from __future__ import annotations

import dataclasses
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

from app import config, db
from app.agents import bids, clarification, rfq, watchdog
from app.models import Mail, RateOption, RfqDraft
from app.pricing.portal_sync import PortalResult
from app.pricing.reference import carrier_by_email

TEAM = "austrian-power@hackloop.mail"
CUSTOMER = "anna@kunde.example"
TRUCKER = "hansekogge-trucking@hackloop.mail"
SID = 5000
BODY = (
    "Hello,\n\nplease quote air freight for a pickup at our site in 61018 to JFK.\n"
    "3 pieces, gross weight 500 kg, volume 2.0 cbm. Cheapest option please.\n\n"
    "Thanks,\nAnna"
)
REQUEST = {
    "origin": None,
    "pickup_zip": "61018",
    "destination": "JFK",
    "weight_kg": 500.0,
    "volume_cbm": 2.0,
    "pieces": 3,
    "preference": "cheapest",
    "ready_date": None,
    "language": "en",
    "customer_name": "Anna",
    "customer_questions": [],
    "service_constraints": [],
}
AIRPORT_REQUEST = {**REQUEST, "origin": "FRA", "pickup_zip": None}


def _rate(airline: str, minor: int, days: int = 2) -> RateOption:
    valid = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
    return RateOption(
        airline, "FRA", "JFK", days, minor, "EUR", minor, "portal", "R-1", valid, 500.0, 500.0
    )


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """Isolated DB, captured sends, portal with two real-looking rates, no LLM."""
    db.init_db(str(tmp_path / "w.db"))
    sent: list[dict] = []
    state = {"portal": [_rate("Solace Air Cargo", 400_000), _rate("Gannet Airways", 450_000)]}

    def send(to, subject, body, in_reply_to=None, **_k):
        sent.append(
            {"id": 9_000 + len(sent), "to": to, "subject": subject, "body": body,
             "in_reply_to": in_reply_to}
        )  # fmt: skip
        return sent[-1]

    def complete(purpose, *_a, **_k):
        if purpose.startswith("extract"):
            return json.dumps({**REQUEST, "attachment_text": ""})
        raise ValueError("no composer in this test")  # deterministic quote copy

    for module in (rfq.hackloop, bids.hackloop, clarification.hackloop):
        monkeypatch.setattr(module, "send", send)
    monkeypatch.setattr(rfq.llm, "complete", complete)
    monkeypatch.setattr(rfq, "portal_result", lambda *_a, **_k: PortalResult(state["portal"]))
    monkeypatch.setattr(rfq, "_sheets", lambda *_a: [])
    trucker = carrier_by_email(TRUCKER)
    monkeypatch.setattr(rfq, "_bid_carriers", lambda *_a: ([trucker], []))
    monkeypatch.setattr(rfq, "_portal_bid_carriers", lambda *_a: {})
    monkeypatch.setattr(bids, "wait", lambda *_a, **_k: [])
    rfq._working.clear()
    yield {"sent": sent, "state": state}
    rfq._working.clear()


def configure(monkeypatch, **changes) -> None:
    new = dataclasses.replace(config.settings, **changes)
    for module in (config, rfq, clarification, watchdog):
        monkeypatch.setattr(module, "settings", new)


def rfq_mail(age_s: float, sid: int = SID, category: str = "rfq") -> Mail:
    at = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    db.ingest_mail(
        mail_id=sid,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Air freight to JFK",
        body=BODY,
        category=category,
        received_at=at,
    )
    return Mail(sid, sid, CUSTOMER, TEAM, "Air freight to JFK", BODY, at)


def trucker_reply(sid: int = SID, price: str = "EUR 180.00") -> None:
    db.ingest_mail(
        mail_id=sid + 100,
        sender=TRUCKER,
        recipient=TEAM,
        subject=f"Re: [AP-{sid}] Pickup 61018 to FRA airport",
        body=f"Our all-in price: {price}. Pickup within 24 hours.",
        category="carrier_bid",
    )


def bid_request_out(sid: int = SID) -> None:
    db.record_outgoing(
        mail_id=sid + 50,
        recipient=TRUCKER,
        subject=f"[AP-{sid}] Pickup 61018 to FRA airport",
        body="Please quote.",
        category="bid_request",
        session_id=sid,
    )


def stored(sid: int = SID, request: dict | None = None, status: str = "error") -> None:
    db.set_request(sid, request or REQUEST)
    db.set_status(sid, status, "test")


def quotes(sid: int = SID) -> list[dict]:
    return (db.get_session(sid) or {}).get("quotes", [])


def kinds(sid: int = SID) -> list[str]:
    return [e["kind"] for e in (db.get_session(sid) or {}).get("events", [])]


# ---------------------------------------------------------------- forcing


def test_forces_best_real_quote_after_a_dead_end(world, monkeypatch) -> None:
    """A crash left the session in error at 210 s: the watchdog prices once more from the
    portal and the trucker bid already received, and sends one quote in the thread."""
    configure(monkeypatch, rfq_watchdog=True)
    rfq_mail(210)
    stored()
    bid_request_out()
    trucker_reply()
    assert watchdog.check_once(background=False) == [SID]
    [quote] = quotes()
    assert quote["airline"] == "Solace Air Cargo"
    assert quote["total_minor"] == 400_000 + 18_000  # air + truck, nothing invented
    customer = [m for m in world["sent"] if m["to"] == CUSTOMER]
    assert len(customer) == 1 and customer[0]["in_reply_to"] == SID
    assert not [m for m in world["sent"] if m["to"] == TRUCKER]  # no new bid mail
    assert {"watchdog_forced", "watchdog_sent"} <= set(kinds())


def test_respects_the_one_quote_claim(world, monkeypatch) -> None:
    configure(monkeypatch, rfq_watchdog=True)
    rfq_mail(210)
    stored()
    bid_request_out()
    trucker_reply()
    watchdog.check_once(background=False)
    watchdog.check_once(background=False)  # quoted: no longer a candidate
    assert watchdog.force_quote(SID) == "answered"  # a direct call cannot send twice
    assert len(quotes()) == 1
    db.conn().execute("UPDATE sessions SET first_reply_at = NULL WHERE id = ?", (SID,))
    db.conn().execute("DELETE FROM quotes WHERE session_id = ?", (SID,))
    assert watchdog.due() == []  # the quote mail itself still ends the thread
    assert len([m for m in world["sent"] if m["to"] == CUSTOMER]) == 1


def test_a_claim_whose_send_may_have_happened_is_never_released(world, monkeypatch) -> None:
    configure(monkeypatch, rfq_watchdog=True)
    rfq_mail(210)
    stored()
    assert db.claim_quote_send(SID)
    db.kv_set(f"quote.transport.{SID}", db.now())  # the HTTP send had started
    assert watchdog.due()[0]["action"] == "claimed"
    assert watchdog.check_once(background=False) == []
    assert watchdog.check_once(background=False) == []
    assert world["sent"] == []
    assert kinds().count("watchdog_alert") == 1  # a person checks the mailbox, once


def test_a_claim_that_never_reached_the_mailbox_is_released(world, monkeypatch) -> None:
    """Restart during the style review: claim taken, transport never started."""
    configure(monkeypatch, rfq_watchdog=True)
    rfq_mail(210)
    stored()
    bid_request_out()
    trucker_reply()
    assert db.claim_quote_send(SID)
    assert watchdog.check_once(background=False) == [SID]
    assert len(quotes()) == 1
    assert "quote_claim_released" in kinds()


@pytest.mark.parametrize("control", ["paused", "manual", "global"])
def test_skips_paused_and_manual_sessions(world, monkeypatch, control) -> None:
    configure(monkeypatch, rfq_watchdog=True)
    rfq_mail(210)
    stored()
    {
        "paused": lambda: db.set_paused(SID, True),
        "manual": lambda: db.takeover(SID),
        "global": lambda: db.set_global_pause(True),
    }[control]()
    assert watchdog.check_once(background=False) == []
    assert watchdog.force_quote(SID) == "paused"  # a direct call checks again
    assert world["sent"] == [] and quotes() == []


def test_waits_for_a_live_worker_and_ignores_young_or_expired_rfqs(world, monkeypatch) -> None:
    configure(monkeypatch, rfq_watchdog=True)
    rfq_mail(210)
    rfq_mail(120, sid=SID + 1)  # before the watchdog time
    rfq_mail(340, sid=SID + 2)  # no longer answerable (TOO_OLD_S)
    with rfq.working(SID):
        assert [r["action"] for r in watchdog.due()] == ["worker_alive"]
    assert [r["session_id"] for r in watchdog.due()] == [SID]


def test_sends_nothing_without_a_real_price(world, monkeypatch) -> None:
    configure(monkeypatch, rfq_watchdog=True)
    world["state"]["portal"] = []
    rfq_mail(210)
    stored(request=AIRPORT_REQUEST)
    assert watchdog.check_once(background=False) == [SID]
    assert world["sent"] == [] and quotes() == []
    alert = [e for e in db.get_session(SID)["events"] if e["kind"] == "watchdog_alert"]
    assert alert and alert[0]["level"] == "error"


def test_door_quote_without_trucker_price_follows_the_setting(world, monkeypatch) -> None:
    configure(monkeypatch, rfq_watchdog=True, allow_incomplete_door_quote=False)
    rfq_mail(210)
    stored()
    bid_request_out()  # the trucker never answered
    assert watchdog.check_once(background=False) == [SID]
    assert world["sent"] == [] and "watchdog_alert" in kinds()


def test_open_dangerous_goods_issue_blocks(world, monkeypatch) -> None:
    configure(monkeypatch, rfq_watchdog=True)
    mail = rfq_mail(210)
    draft = RfqDraft(
        facts=AIRPORT_REQUEST,
        issues=[{"field": "dangerous_goods", "code": "conflict"}],
    )
    db.validation_save(SID, "awaiting_clarification", draft.model_dump(), dataclasses.asdict(mail))
    assert watchdog.check_once(background=False) == [SID]
    assert world["sent"] == [] and quotes() == []
    assert "watchdog_alert" in kinds()


def test_excluded_carrier_is_never_quoted_and_all_excluded_sends_nothing(
    world, monkeypatch
) -> None:
    configure(monkeypatch, rfq_watchdog=True, all_excluded_notice=True)
    rfq_mail(210)
    stored(request=AIRPORT_REQUEST)
    db.add_carrier_pref(CUSTOMER, "solace air cargo", "exclude", SID)
    db.add_carrier_pref(CUSTOMER, "gannet airways", "exclude", SID)
    monkeypatch.setattr(rfq, "_all_excluded", lambda *_a: True)
    assert watchdog.check_once(background=False) == [SID]
    assert world["sent"] == []  # not even the exclusion notice: a person decides
    assert "watchdog_alert" in kinds()


def test_customer_exclusion_still_applies_to_a_forced_quote(world, monkeypatch) -> None:
    configure(monkeypatch, rfq_watchdog=True)
    rfq_mail(210)
    stored(request=AIRPORT_REQUEST)
    db.add_carrier_pref(CUSTOMER, "solace air cargo", "exclude", SID)
    watchdog.check_once(background=False)
    assert quotes()[0]["airline"] == "Gannet Airways"


def test_unanswered_question_quotes_what_the_mail_states(world, monkeypatch) -> None:
    """A question about the volume is still open at 200 s: quote from the stated facts."""
    configure(monkeypatch, rfq_watchdog=True)
    mail = rfq_mail(210)
    draft = RfqDraft(facts=AIRPORT_REQUEST, issues=[{"field": "volume_cbm", "code": "conflict"}])
    db.validation_save(SID, "awaiting_clarification", draft.model_dump(), dataclasses.asdict(mail))
    watchdog.check_once(background=False)
    assert len(quotes()) == 1
    assert db.validation_get(SID)["state"] == "ready"  # a late answer cannot reprice


def test_open_route_fields_are_not_trusted(world, monkeypatch) -> None:
    """Round 15: origin AMS came from a forwarded subject and was flagged; the watchdog drops
    it, and a pickup request without a site has no trucking price: nothing is sent."""
    request, why = watchdog.stated_request(
        {**REQUEST, "origin": "AMS", "pickup_zip": None},
        [{"field": "origin", "code": "unsupported"}, {"field": "pickup_zip", "code": "missing"}],
    )
    assert request is None and why == "Abholort offen"
    request, _ = watchdog.stated_request(
        {**AIRPORT_REQUEST, "origin": "AMS"}, [{"field": "origin", "code": "unproven"}]
    )
    assert request is not None and request.origin is None


def test_phishing_root_mail_never_starts_the_watchdog(world, monkeypatch) -> None:
    configure(monkeypatch, rfq_watchdog=True)
    rfq_mail(210)
    stored(request=AIRPORT_REQUEST)
    db.conn().execute("UPDATE mails SET category = 'phishing' WHERE id = ?", (SID,))
    assert watchdog.due() == []
    assert watchdog.force_quote(SID) == "phishing"
    assert world["sent"] == []


def test_only_mails_routed_as_rfq_are_candidates(world, monkeypatch) -> None:
    """A customer mail that opened its own thread (acceptance, question) never started the
    RFQ flow, so the watchdog never quotes it either."""
    configure(monkeypatch, rfq_watchdog=True)
    rfq_mail(210, category="customer")
    assert watchdog.due() == []


def test_extracts_again_when_nothing_is_stored(world, monkeypatch) -> None:
    configure(monkeypatch, rfq_watchdog=True)
    rfq_mail(210)
    bid_request_out()
    trucker_reply()
    calls: list[str] = []

    def complete(purpose, *_a, **_k):
        calls.append(purpose)
        if purpose.startswith("extract"):
            return json.dumps({**REQUEST, "attachment_text": ""})
        raise ValueError("no composer in this test")

    monkeypatch.setattr(rfq.llm, "complete", complete)
    assert watchdog.force_quote(SID) == "sent"
    assert calls[0] == "extract_rfq"
    assert len(quotes()) == 1


def test_no_extraction_once_the_llm_budget_is_spent(world, monkeypatch) -> None:
    configure(monkeypatch, rfq_watchdog=True, llm_max_calls_per_session=1)
    rfq_mail(210)
    db.kv_set(f"llm_calls:{SID}", "1")
    monkeypatch.setattr(rfq.llm, "complete", lambda *_a, **_k: pytest.fail("LLM called"))
    assert watchdog.force_quote(SID) == "no_request"


# ---------------------------------------------------------------- restarts


class Killed(BaseException):
    """The process dies: no `except Exception` in the flow catches it."""


def _kill_after_bids(*_a, **_k):
    raise Killed


def _inline_threads(monkeypatch) -> None:
    class Inline:
        def __init__(self, target, args=(), **_k):
            self.run = lambda: target(*args)

        def start(self):
            self.run()

    monkeypatch.setattr(rfq.threading, "Thread", Inline)


def test_restart_after_bid_requests_still_quotes(world, monkeypatch) -> None:
    """Kill the flow right after the bid requests went out, start again: the round still
    gets exactly one quote, from the stored request and the stored trucker reply."""
    configure(monkeypatch, rfq_watchdog=True)
    mail = rfq_mail(30)
    monkeypatch.setattr(bids, "wait", _kill_after_bids)
    with pytest.raises(Killed):
        rfq.handle_rfq(mail, SID)
    assert db.bid_request_recipients(SID) == {TRUCKER}  # the requests went out
    assert db.get_session(SID)["status"] == "pricing" and quotes() == []
    # --- the process is gone: in-memory bids and workers are lost; the trucker answers
    bids._open.clear()
    rfq._working.clear()
    trucker_reply()
    # --- start again
    monkeypatch.setattr(bids, "wait", lambda *_a, **_k: [])
    _inline_threads(monkeypatch)
    sent_before = len(world["sent"])
    assert rfq.catch_up() == 1
    assert len(quotes()) == 1 and quotes()[0]["total_minor"] == 418_000
    assert [m["to"] for m in world["sent"][sent_before:]] == [CUSTOMER]  # no new bid mail
    assert watchdog.check_once(background=False) == []  # answered: nothing more to do


def test_restart_found_late_is_handled_at_once(world, monkeypatch) -> None:
    """The process comes back at 250 s while a clarification is open (rfq.catch_up skips
    it): the first watchdog pass forces the quote from what the mail states."""
    configure(monkeypatch, rfq_watchdog=True)
    mail = rfq_mail(250)
    db.set_request(SID, REQUEST)
    bid_request_out()
    trucker_reply()
    draft = RfqDraft(facts=REQUEST, issues=[{"field": "pieces", "code": "conflict"}])
    db.validation_save(SID, "awaiting_clarification", draft.model_dump(), dataclasses.asdict(mail))
    _inline_threads(monkeypatch)
    assert rfq.catch_up() == 0
    assert watchdog.check_once(background=False) == [SID]
    assert len(quotes()) == 1


def test_resumed_worker_counts_as_live_before_its_thread_runs(world, monkeypatch) -> None:
    configure(monkeypatch, rfq_watchdog=True)
    rfq_mail(250)
    stored(status="pricing")
    started: list[threading.Thread] = []

    class Deferred:
        def __init__(self, target, args=(), **_k):
            self.target, self.args = target, args

        def start(self):
            started.append(self)

    monkeypatch.setattr(rfq.threading, "Thread", Deferred)
    assert rfq.catch_up() == 1
    assert watchdog.due()[0]["action"] == "worker_alive"
    rfq._drop(SID)


# ---------------------------------------------------------------- causes and deploy gate


def test_unanswered_round_cause_from_our_records(world, monkeypatch) -> None:
    from app.ui import watchers

    mail = rfq_mail(100)
    db.set_status(SID, "error", "kein Preis")
    db.emit("process_start", "Prozess gestartet", None)  # a restart inside the round
    rows = [
        {"round_no": 7, "sent_at": mail.received_at, "verdict": "no answer"},
        {"round_no": 8, "sent_at": "2026-11-21T20:00:00+00:00", "verdict": "no answer"},
    ]
    for r in rows:
        sid = watchers._match_session(r)
        db.save_board_result(
            round_id=str(r["round_no"]),
            verdict=r["verdict"],
            reason=None,
            points=None,
            raw=r,
            session_id=sid,
            failed=True,
            cause=db.round_cause(sid, r["sent_at"]),
        )
    by_round = {r["round_id"]: r for r in db.unanswered_rounds()}
    assert by_round["7"]["cause"]["status"] == "error"
    assert "kein Preis" in by_round["7"]["cause"]["summary"]
    assert by_round["8"]["cause"]["kind"] == "mail_not_received"
    assert 95 <= by_round["7"]["cause"]["restarts_s"][0] <= 105  # restart 100 s into the round
    lines = watchdog.report_lines()  # newest round first
    assert len(lines) == 2 and "Mail nicht empfangen" in lines[0] and "restart at" in lines[1]
    attention = {i.get("round_id"): i for i in db.attention_items() if i["kind"] == "board"}
    assert "Mail nicht empfangen" in attention["8"]["reason"]
    assert attention["8"]["session_id"] is None


def test_deploy_gate(world) -> None:
    from app.ui import deploy_gate

    t = datetime(2026, 11, 21, 20, 0, tzinfo=UTC)
    for i in range(4):  # every 364 s, the last one at t
        at = (t - timedelta(seconds=364 * (3 - i))).isoformat()
        db.ingest_mail(
            mail_id=100 + i,
            sender=CUSTOMER,
            recipient=TEAM,
            subject="RFQ",
            body="x",
            category="rfq",
            received_at=at,
        )
    for sid in (100, 101, 102):
        assert db.claim_quote_send(sid)
    # newest RFQ 60 s old and not answered: wait
    g = deploy_gate.gate(t + timedelta(seconds=60))
    assert not g["safe"] and g["interval_s"] == 364
    assert db.claim_quote_send(103)
    g = deploy_gate.gate(t + timedelta(seconds=60))
    assert g["safe"] and g["seconds_to_next"] == 304
    assert not deploy_gate.gate(t + timedelta(seconds=300))["safe"]  # next RFQ in 64 s
    # a missed round leaves a double gap; the phase still comes from the newest arrival
    assert deploy_gate.interval_s([t, t + timedelta(seconds=728), t + timedelta(seconds=1092),
                                   t + timedelta(seconds=1456)]) == 364  # fmt: skip


def test_health_reports_process_start(world) -> None:
    from fastapi.testclient import TestClient

    from app.ui import server

    body = TestClient(server.app).get("/api/health").json()
    assert body["process_started_at"] and "rfq_watchdog" in body
