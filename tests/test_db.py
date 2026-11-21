"""Tests for app/db.py. No real 5U calls. Run: python -m pytest -q"""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from app import db

CUSTOMER = "anna@shipper.example"
TEAM = "austrian-power@hackloop.mail"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))
    yield


def rfq(mail_id=100, **kw):
    args = {
        "mail_id": mail_id,
        "sender": CUSTOMER,
        "recipient": TEAM,
        "subject": "RFQ FRA-DFW 40kg",
        "body": "Hi, cheapest please",
        "category": "rfq",
    }
    args.update(kw)
    return db.ingest_mail(**args)


def test_rfq_opens_session_with_deadline():
    sid = rfq()
    assert sid == 100
    s = db.get_session(sid)
    assert s["status"] == "new" and s["kind"] == "rfq" and s["customer"] == CUSTOMER
    created = datetime.fromisoformat(s["created_at"])
    assert datetime.fromisoformat(s["deadline_at"]) - created == timedelta(minutes=6)
    assert [e["kind"] for e in s["events"]] == [db.Ev.MAIL_IN]


def test_duplicate_mail_is_ignored():
    rfq()
    assert rfq() == 100
    s = db.get_session(100)
    assert len(s["mails"]) == 1 and len(s["events"]) == 1


def test_carrier_bid_reply_maps_to_customer_session():
    sid = rfq()
    # we ask a carrier for a bid in a NEW thread (mail 200)
    db.record_outgoing(
        mail_id=200,
        recipient="kestrel-airways@hackloop.mail",
        subject="Bid request FRA-DFW",
        body="...",
        category="bid_request",
        session_id=sid,
    )
    # carrier answers; its references point to OUR mail, not the RFQ
    got = db.ingest_mail(
        mail_id=201,
        sender="kestrel-airways@hackloop.mail",
        recipient=TEAM,
        subject="Re: Bid request",
        body="EUR 150, 2 days",
        category="carrier_bid",
        references=[200],
        in_reply_to=200,
    )
    assert got == sid
    assert {m["id"] for m in db.get_session(sid)["mails"]} == {100, 200, 201}


def test_customer_followup_stays_in_session():
    sid = rfq()
    db.record_outgoing(
        mail_id=101,
        recipient=CUSTOMER,
        subject="Re: RFQ",
        body="146.80 EUR",
        category="quote",
        in_reply_to=100,
        references=[100],
    )
    got = db.ingest_mail(
        mail_id=102,
        sender=CUSTOMER,
        recipient=TEAM,
        subject="Re: RFQ",
        body="Accepted",
        category="customer",
        references=[100, 101],
        in_reply_to=101,
    )
    assert got == sid
    assert len(db.list_sessions()) == 1


def test_unknown_carrier_mail_is_unassigned():
    got = db.ingest_mail(
        mail_id=300,
        sender="elbflug-air-freight@hackloop.mail",
        recipient=TEAM,
        subject="Tariff December",
        body="see attachment",
        category="tariff",
    )
    assert got is None
    assert [m["id"] for m in db.list_unassigned()] == [300]


def test_tracking_session_has_no_deadline():
    sid = db.ingest_mail(
        mail_id=400,
        sender="tracking@hackloop.mail",
        recipient=TEAM,
        subject="Where is BK-12345678?",
        body="status?",
        category="tracking",
    )
    s = db.get_session(sid)
    assert s["kind"] == "tracking" and s["deadline_at"] is None


def test_claim_quote_send_only_once():
    sid = rfq()
    assert db.claim_quote_send(sid) is True
    assert db.claim_quote_send(sid) is False


def test_claim_blocked_by_pause_takeover_and_global_pause():
    a, b, c = rfq(1), rfq(2), rfq(3)
    db.set_paused(a, True)
    db.takeover(b)
    assert db.claim_quote_send(a) is False
    assert db.claim_quote_send(b) is False
    db.set_global_pause(True)
    assert db.claim_quote_send(c) is False and db.is_paused(c)
    db.set_global_pause(False)
    assert db.claim_quote_send(c) is True


def test_claim_is_atomic_under_race():
    sid = rfq()
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(db.claim_quote_send(sid))

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1


def test_quote_booking_and_tracking_lookup():
    sid = rfq()
    db.record_quote(
        sid,
        total_minor=14680,
        currency="EUR",
        airline="Elbflug Air Freight",
        origin="FRA",
        destination="DFW",
        transit_days=3,
        source="sheet",
        alternatives=[{"airline": "Yellowtail", "total_minor": 17460}],
    )
    assert db.get_session(sid)["status"] == "quoted"
    db.record_booking(
        sid,
        booking_reference="BK-ABCDEFGH",
        rate_id="R-12345678",
        airline="Elbflug Air Freight",
        price_minor=14680,
        currency="EUR",
    )
    db.record_booking(
        sid,
        booking_reference="BK-ABCDEFGH",
        rate_id="R-12345678",
        airline="Elbflug Air Freight",
        price_minor=14680,
        currency="EUR",
    )  # retry
    assert len(db.get_session(sid)["bookings"]) == 1
    assert db.update_booking_status("BK-ABCDEFGH", "confirmed") == sid
    b = db.find_booking("BK-ABCDEFGH")
    assert b["status"] == "confirmed" and b["team_reference"] == "AP-100"
    assert db.get_session(sid)["status"] == "confirmed"


def test_events_after_and_kv():
    rfq()
    first = db.events_after(0)
    db.emit(db.Ev.PRICED, "3 Optionen", session_id=100, best="Elbflug")
    new = db.events_after(first[-1]["id"])
    assert len(new) == 1 and new[0]["payload"] == {"best": "Elbflug"}
    db.kv_set("since_id", "42")
    assert db.kv_get("since_id") == "42"


def test_air_price_migration_preserves_legacy_quotes(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy.db")
    legacy_schema = db.SCHEMA.replace("  air_total_minor   INTEGER,\n", "").replace(
        "  air_currency      TEXT,\n", ""
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(legacy_schema)
    db.init_db(path)
    sid = rfq()
    db.record_quote(
        sid,
        total_minor=90000,
        currency="EUR",
        airline="A",
        origin="FRA",
        destination="JFK",
        transit_days=2,
        source="sheet",
    )
    db.init_db(path)
    quote = db.get_session(sid)["quotes"][0]
    assert quote["total_minor"] == 90000
    assert quote["air_total_minor"] is None and quote["air_currency"] is None


def test_expire_overdue():
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat(
        timespec="seconds"
    )
    sid = rfq(received_at=old)
    assert db.expire_overdue() == [sid]
    assert db.get_session(sid)["status"] == "expired"


def test_list_sessions_attention_first():
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(
        timespec="seconds"
    )
    calm = rfq(1)
    urgent = rfq(2, received_at=old)  # 1 min left -> needs attention
    rows = db.list_sessions()
    assert rows[0]["id"] == urgent and rows[0]["needs_attention"] == 1
    assert rows[1]["id"] == calm and rows[1]["needs_attention"] == 0


def test_llm_cost_and_stats():
    sid = rfq()
    db.claim_quote_send(sid)
    db.record_llm_call(
        sid,
        purpose="reply",
        model="gpt-x",
        prompt_tokens=300,
        completion_tokens=120,
        cost_micro_usd=420,
        latency_ms=800,
    )
    db.record_quote(
        sid,
        total_minor=10000,
        currency="EUR",
        airline="X",
        origin="FRA",
        destination="DFW",
        transit_days=2,
        source="portal",
    )
    st = db.stats()
    assert st["avg_llm_cost_micro_usd_per_quote"] == 420
    assert st["by_status"] == {"quoted": 1}
    assert db.get_session(sid)["llm_cost_micro_usd"] == 420


def test_concurrent_writers_no_lock_errors():
    errors = []

    def writer(n):
        try:
            for i in range(100):
                mid = n * 1000 + i
                db.ingest_mail(
                    mail_id=mid,
                    sender=CUSTOMER,
                    recipient=TEAM,
                    subject="s",
                    body="b",
                    category="rfq",
                )
                db.emit(db.Ev.PRICED, "p", session_id=mid)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reader():
        last = 0
        for _ in range(200):
            rows = db.events_after(last)
            if rows:
                last = rows[-1]["id"]

    ts = [threading.Thread(target=writer, args=(n,)) for n in range(1, 7)]
    ts += [threading.Thread(target=reader) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    assert db.stats()["sessions_total"] == 600


def test_board_result_change_detection():
    sid = rfq()
    assert db.save_board_result(
        round_id="r1",
        verdict="pending",
        reason=None,
        points=None,
        raw={},
        session_id=sid,
    )
    assert not db.save_board_result(
        round_id="r1", verdict="pending", reason=None, points=None, raw={}
    )
    assert db.save_board_result(
        round_id="r1", verdict="accepted", reason=None, points={"total": 87}, raw={}
    )
    assert db.get_session(sid)["board"][0]["verdict"] == "accepted"


def test_ratedesk_reply_without_references_maps_by_quoted_subject():
    sid = rfq()
    db.record_outgoing(
        mail_id=500,
        recipient="ratedesk@hackloop.mail",
        subject="Rate request FRA-JFK 100 kg",
        body="...",
        category="ratedesk",
        session_id=sid,
    )
    body = 'portal rates for FRA-JFK (your mail "Rate request FRA-JFK 100 kg"):'
    got = db.ingest_mail(
        mail_id=501,
        sender="ratedesk@hackloop.mail",
        recipient=TEAM,
        subject="Portal rates FRA-JFK 100.0 kg",
        body=body,
        category="portal_reply",
    )
    assert got == sid


def test_reply_without_references_maps_by_re_subject():
    sid = rfq()
    db.record_outgoing(
        mail_id=600,
        recipient="kestrel-airways@hackloop.mail",
        subject="Bid request FRA-DFW 40 kg",
        body="...",
        category="bid_request",
        session_id=sid,
    )
    got = db.ingest_mail(
        mail_id=601,
        sender="kestrel-airways@hackloop.mail",
        recipient=TEAM,
        subject="RE: Bid request FRA-DFW 40 kg",
        body="EUR 150",
        category="carrier_bid",
    )
    assert got == sid


def test_session_by_created_at_matches_board_sent_at():
    sid = rfq(received_at="2026-11-21T15:00:59.986067+00:00")
    assert db.session_by_created_at("2026-11-21T15:00:59.986067+00:00") == sid
    assert db.session_by_created_at("2026-11-21T15:01:00+00:00") is None


def test_bid_reply_maps_by_ap_tag_when_request_was_not_stored():
    sid = rfq()
    # carrier answers a bid request we never stored: headers point to an unknown mail id
    got = db.ingest_mail(
        mail_id=300,
        sender="alpenblick-logistik@hackloop.mail",
        recipient=TEAM,
        subject=f"Re: [AP-{sid}] Pickup 24983 to HAM airport, 1619 kg",
        body="Our price: 122.76 EUR",
        category="carrier_bid",
        references=[299],
        in_reply_to=299,
    )
    assert got == sid
    # an unknown AP number stays unassigned
    assert (
        db.ingest_mail(
            mail_id=301,
            sender="x@hackloop.mail",
            recipient=TEAM,
            subject="Re: [AP-999] test",
            body="",
            category="carrier_bid",
        )
        is None
    )


def test_booking_confirmation_maps_by_bk_reference_and_startup_remap():
    sid = rfq()
    # stored before the fallback existed: unassigned
    with db.tx() as c:
        c.execute(
            "INSERT INTO mails(id, session_id, thread_root, direction, sender, recipient, subject,"
            " body, category, received_at) VALUES (400, NULL, 400, 'in', 'a@hackloop.mail', ?,"
            " 'Booking BK-FG57LGZ7 confirmed', 'Confirmed.', 'carrier_bid', ?)",
            (TEAM, db.now()),
        )
    db.record_booking(
        sid,
        booking_reference="BK-FG57LGZ7",
        rate_id="R-1",
        airline="A",
        price_minor=100,
        currency="EUR",
        status="confirmed",
    )
    assert db.remap_unassigned() == 1
    assert 400 in {m["id"] for m in db.get_session(sid)["mails"]}
    assert db.remap_unassigned() == 0


def test_backfill_booking_from_airline_confirmation():
    sid = rfq()
    body = (
        "Hello,\n\nyour booking BK-5LH10DOK is confirmed.\n\nBooking reference: BK-5LH10DOK\n"
        f"Your reference: AP-{sid}\nAirline: Ultraviolet Air Freight (UV)\nLane: FRA-DOH\n"
        "Price: 324.36 EUR\nStatus: booked and ready to ship\n"
    )
    db.ingest_mail(
        mail_id=500,
        sender="ultraviolet-air-freight@hackloop.mail",
        recipient=TEAM,
        subject="Booking BK-5LH10DOK confirmed",
        body=body,
        category="carrier_bid",
    )
    assert db.backfill_bookings_from_confirmations() == 1
    b = db.find_booking("BK-5LH10DOK")
    assert b["session_id"] == sid and b["status"] == "confirmed"
    rec = next(r for r in db.tracking_records() if r["session_id"] == sid)
    assert rec["airline"] == "Ultraviolet Air Freight" and rec["price_minor"] == 32436
    assert rec["request"] == {"origin": "FRA", "destination": "DOH"}
    assert db.backfill_bookings_from_confirmations() == 0


def test_loop_query_indexes_exist_and_are_used():
    c = db.conn()
    names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_mails_category", "ix_mails_reply", "ix_sessions_created"} <= names

    def plan(sql: str, *args: object) -> str:
        return " | ".join(r["detail"] for r in c.execute("EXPLAIN QUERY PLAN " + sql, args))

    seen: list[str] = []
    c.set_trace_callback(seen.append)
    db.rescue_candidates(db.now())
    db.session_by_created_at(db.now())
    c.set_trace_callback(None)
    rescue, by_created = (plan(sql) for sql in seen)
    assert "SCAN root" not in rescue and "ix_mails_category" in rescue
    assert "ix_mails_reply" in rescue
    assert "ix_sessions_created" in by_created


def test_indexes_are_added_to_an_existing_database(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    old_schema = "\n".join(
        line
        for line in db.SCHEMA.splitlines()
        if not any(
            n in line for n in ("ix_mails_category", "ix_mails_reply", "ix_sessions_created")
        )
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(old_schema)
    db.init_db(path)
    names = {
        r["name"] for r in db.conn().execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {"ix_mails_category", "ix_mails_reply", "ix_sessions_created"} <= names


def _statements(fn):
    """Run fn and return (result, SQL statements this thread's connection executed)."""
    seen: list[str] = []
    c = db.conn()
    c.set_trace_callback(seen.append)
    try:
        return fn(), seen
    finally:
        c.set_trace_callback(None)


def test_delivery_claim_on_empty_queue_opens_no_write_transaction():
    row, seen = _statements(lambda: db.delivery_claim(1000.0))
    assert row is None
    assert not any(s.startswith("BEGIN") for s in seen)
    assert db.conn().total_changes == 0

    assert db.delivery_enqueue(7, {"id": 7}, "webhook")
    db.delivery_state(7, "pending", retry_at=2000.0)  # backoff: not due yet
    row, seen = _statements(lambda: db.delivery_claim(1000.0))
    assert row is None and not any(s.startswith("BEGIN") for s in seen)


def test_delivery_claim_returns_the_same_row_as_before_and_only_once():
    assert db.delivery_enqueue(8, {"id": 8}, "poll")
    assert db.delivery_enqueue(7, {"id": 7}, "webhook")
    row = db.delivery_claim(1000.0)
    assert row is not None
    assert (row["mail_id"], row["status"], row["attempts"]) == (7, "pending", 1)
    stored = db.delivery_get(7)
    assert stored is not None
    assert (stored["status"], stored["attempts"]) == ("processing", 1)
    second = db.delivery_claim(1000.0)
    assert second is not None and second["mail_id"] == 8
    assert db.delivery_claim(1000.0) is None


def test_delivery_claim_is_atomic_under_race():
    assert db.delivery_enqueue(7, {"id": 7}, "webhook")
    barrier = threading.Barrier(8)
    claimed: list[dict | None] = []

    def worker():
        barrier.wait()
        claimed.append(db.delivery_claim(1000.0))
        db.close_thread_connection()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([r for r in claimed if r]) == 1
    stored = db.delivery_get(7)
    assert stored is not None and stored["attempts"] == 1


def test_expire_overdue_with_nothing_due_opens_no_write_transaction():
    rfq()  # open session, deadline still ahead
    ids, seen = _statements(db.expire_overdue)
    assert ids == []
    assert not any(s.startswith("BEGIN") for s in seen)


def test_expire_overdue_still_expires_a_waiting_question_of_a_closed_session():
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat(timespec="seconds")
    sid = rfq(received_at=old)
    db.set_status(sid, "error")  # not new/pricing: only the validation row is due
    db.validation_save(sid, "awaiting_clarification", {}, {})
    assert db.expire_overdue() == []
    validation = db.validation_get(sid)
    assert validation is not None and validation["state"] == "expired"
    ids, seen = _statements(db.expire_overdue)
    assert ids == [] and not any(s.startswith("BEGIN") for s in seen)


def test_init_db_without_path_runs_once_per_database(tmp_path, monkeypatch):
    runs: list[str] = []
    remap = db.remap_unassigned
    monkeypatch.setattr(db, "remap_unassigned", lambda: runs.append(db._db_path) or remap())

    _, seen = _statements(db.init_db)  # the fixture already initialised this path
    assert runs == [] and seen == []

    first = str(tmp_path / "test.db")
    db.init_db(first)  # an explicit path always initialises in full (import, tests)
    assert runs == [first]

    other = str(tmp_path / "other.db")
    db.init_db(other)
    db.init_db()
    assert runs == [first, other]
    assert rfq() == 100  # the new file has the schema and is the active database

    db.close_thread_connection()
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"other.db{suffix}").unlink(missing_ok=True)
    db.init_db()  # the file is gone: initialise again rather than trust the flag
    assert runs == [first, other, other]
    assert db.get_session(100) is None
