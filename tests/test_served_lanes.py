"""Airlines that refused a lane: parse their "We fly ..." list and stop asking them."""

from datetime import UTC, datetime, timedelta

import pytest

from app import db
from app.agents import bids, rfq
from app.models import Mail, Rfq
from app.pricing import served_lanes
from app.pricing.lanes import LaneAirline
from app.pricing.reference import carrier_by_email

TEAM = "austrian-power@hackloop.mail"
COBALT = "cobalt-skylift@hackloop.mail"
JUNIPER = "juniper-cargo-lines@hackloop.mail"

MAIL_1336 = (  # real refusal, Cobalt Skylift, for STR-EZE
    "Hello,\n\nthank you for your request. We do not serve this lane/zone: STR-EZE.\n"
    "We fly CGN-EZE, CGN-SGN, CGN-SIN, DUS-BOM, DUS-DEL, DUS-JFK, DUS-SYD, FRA-BOG, FRA-ICN, "
    "FRA-JNB, FRA-SYD, FRA-TPE, HAM-BOG, HAM-RUH, MUC-BOG, MUC-LIM, MUC-MEL, MUC-MEX, MUC-MIA, "
    "MUC-YYZ, STR-YYZ.\n\nBest regards\nCobalt Skylift\ncobalt-skylift@hackloop.mail"
)
MAIL_1338 = (  # real refusal, Juniper Cargo Lines, for STR-EZE
    "Hello,\n\nthank you for your request. We do not serve this lane/zone: STR-EZE.\n"
    "We fly CGN-DEL, CGN-SGN, DUS-SYD, DUS-YYZ, FRA-EZE, FRA-GRU, FRA-MEL, FRA-NBO, FRA-TPE, "
    "HAM-SGN, HAM-YYZ, MUC-RUH, STR-SCL.\n\nBest regards\nJuniper Cargo Lines\n"
    "juniper-cargo-lines@hackloop.mail"
)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "test.db"))
    yield
    bids.close(1223)


def test_parse_real_refusals() -> None:
    r = served_lanes.parse_refusal(MAIL_1336)
    assert r is not None and r.lane == "STR-EZE"
    assert len(r.served) == 21 and "CGN-EZE" in r.served and "STR-YYZ" in r.served
    j = served_lanes.parse_refusal(MAIL_1338)
    assert j is not None and "FRA-EZE" in j.served and "STR-EZE" not in j.served
    assert (
        served_lanes.parse_refusal("Our all-in price is EUR 2,100.00, 3 days.") is None
    )


def test_serves_known_unknown_and_stale() -> None:
    served_lanes.remember(COBALT, served_lanes.parse_refusal(MAIL_1336))
    assert served_lanes.serves(COBALT, "STR", "EZE") is False
    assert served_lanes.serves(COBALT, "cgn", "eze") is True
    assert served_lanes.serves(JUNIPER, "STR", "EZE") is None  # never asked
    later = datetime.now(UTC) + timedelta(hours=2)
    assert (
        served_lanes.serves(COBALT, "STR", "EZE", now=later) is None
    )  # stale: ask again


def test_older_refusal_does_not_overwrite_newer() -> None:
    r = served_lanes.parse_refusal(MAIL_1336)
    served_lanes.remember(COBALT, r, at="2026-11-21T15:40:00+00:00")
    old = served_lanes.Refusal("STR-EZE", frozenset({"STR-EZE"}))
    served_lanes.remember(COBALT, old, at="2026-11-21T15:00:00+00:00")
    now = datetime(2026, 11, 21, 15, 45, tzinfo=UTC)
    assert served_lanes.serves(COBALT, "STR", "EZE", now=now) is False


def test_backfill_from_stored_mails() -> None:
    now = datetime.now(UTC).isoformat()
    for mid, sender, body in ((1336, COBALT, MAIL_1336), (1338, JUNIPER, MAIL_1338)):
        db.ingest_mail(
            mail_id=mid,
            sender=sender,
            recipient=TEAM,
            subject="Re: [AP-1223] Air freight STR-EZE, 1228 kg / 5.37 cbm",
            body=body,
            category="carrier_bid",
            received_at=now,
        )
    assert served_lanes.backfill() == 2
    assert served_lanes.serves(JUNIPER, "FRA", "EZE") is True
    assert served_lanes.serves(JUNIPER, "STR", "EZE") is False


def test_refusal_counts_as_answer_and_is_not_asked_again(monkeypatch) -> None:
    cobalt, juniper = carrier_by_email(COBALT), carrier_by_email(JUNIPER)
    assert cobalt is not None and juniper is not None
    sent: list[str] = []
    monkeypatch.setattr(
        bids.hackloop,
        "send",
        lambda to, s, b, **k: sent.append(to) or {"id": 5000 + len(sent)},
    )
    db.ingest_mail(
        mail_id=1223,
        sender="weidner-spielwaren@hackloop.mail",
        recipient=TEAM,
        subject="Air freight STR to EZE",
        body="Please quote STR to EZE, 1228 kg, 5.37 cbm.",
        category="rfq",
        received_at=datetime.now(UTC).isoformat(),
    )
    r = Rfq(origin="STR", destination="EZE", weight_kg=1228, volume_cbm=5.37)
    assert bids.request(1223, r, "STR", [cobalt, juniper]) == 2

    for mid, sender, body in ((1336, COBALT, MAIL_1336), (1338, JUNIPER, MAIL_1338)):
        now = datetime.now(UTC).isoformat()
        bids.on_bid_mail(
            Mail(mid, mid, sender, TEAM, "Re: [AP-1223] x", body, now), None
        )
    t0 = datetime.now(UTC)
    bids.wait(1223, 30)  # both declined: returns at once instead of waiting 30 s
    assert (datetime.now(UTC) - t0).total_seconds() < 1
    assert bids.offers(1223) == []
    t0 = datetime.now(UTC)
    bids.wait(1223, 30, until_kinds={"airline"})  # no airline left to answer: no waiting either
    assert (datetime.now(UTC) - t0).total_seconds() < 1

    lane = [
        LaneAirline(cobalt.name, "email_only", email=COBALT),
        LaneAirline(juniper.name, "email_only", email=JUNIPER),
        LaneAirline("Fjordline Air Cargo", "portal", "FQ"),
    ]
    monkeypatch.setattr(rfq, "airlines_for", lambda o, d: lane)
    assert rfq._bid_carriers(r, "STR") == ([], [])  # both said they do not fly STR-EZE
    fra = r.model_copy(update={"origin": "FRA"})
    assert [c.email for c in rfq._bid_carriers(fra, "FRA")[1]] == [
        JUNIPER
    ]  # flies FRA-EZE
