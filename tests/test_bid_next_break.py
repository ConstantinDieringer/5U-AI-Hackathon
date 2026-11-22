"""Round 27 (mail 6882, HAM to EZE, 859 kg / 3.50 cbm, fastest): Meridian's portal rate gave a
503, so we asked it by email. Its bid priced exactly 859 kg at 3.35 €/kg = 2,877.65 €. The board
gave 39.32 accuracy points: 1.26 % above a best of 2,980.75 € = Hansekogge's truck 140.72 € plus
2,840.00 € = 1000 kg at 2.84 €/kg, Meridian's next weight break. The portal path already asks
for exact weight and next break; email bids did not.

BID_NEXT_BREAK=true sends an airline with known portal brackets a second request billed at its
next break; the cheaper offer counts. Default false keeps today's behaviour.
"""

import copy
import dataclasses
import json
from datetime import UTC, datetime

import pytest

from app import config, db
from app.agents import bids, rfq_replay
from app.models import Rfq
from app.pricing.reference import carrier_by_email

MERIDIAN = "meridian-freightways@hackloop.mail"
REQUEST = Rfq(
    origin="HAM",
    pickup_zip="24489",
    destination="EZE",
    weight_kg=859,
    volume_cbm=3.5,
    pieces=7,
    preference="fastest",
)


@pytest.fixture()
def flag(monkeypatch):
    def set_(on: bool) -> None:
        monkeypatch.setattr(
            config, "settings", dataclasses.replace(config.settings, bid_next_break=on)
        )

    return set_


@pytest.fixture()
def corpus() -> dict:
    return json.loads(rfq_replay.CORPUS.read_text(encoding="utf-8"))


def test_requests(flag, tmp_path, monkeypatch) -> None:
    db.init_db(str(tmp_path / "t.db"))
    db.ingest_mail(
        mail_id=6882,
        sender="kessler-feinmechanik@hackloop.mail",
        recipient="austrian-power@hackloop.mail",
        subject="Air freight to EZE",
        body="859 kg",
        category="rfq",
        received_at=datetime.now(UTC).isoformat(),
    )
    sent: list[dict] = []

    def send(to, subject, body, **_k):
        sent.append({"id": 50_000 + len(sent), "to": to, "subject": subject, "body": body})
        return sent[-1]

    monkeypatch.setattr(bids.hackloop, "send", send)
    meridian = carrier_by_email(MERIDIAN)
    flag(False)
    assert bids.request(6882, REQUEST, "HAM", [meridian]) == 1
    bids.close(6882)
    sent.clear()
    db.init_db(str(tmp_path / "t2.db"))
    db.ingest_mail(
        mail_id=6882,
        sender="kessler-feinmechanik@hackloop.mail",
        recipient="austrian-power@hackloop.mail",
        subject="Air freight to EZE",
        body="859 kg",
        category="rfq",
        received_at=datetime.now(UTC).isoformat(),
    )
    flag(True)
    assert bids.request(6882, REQUEST, "HAM", [meridian]) == 2
    assert [m["subject"].endswith("billed at 1000 kg") for m in sent] == [False, True]
    assert "billed at 1000 kg, your next weight break" in sent[1]["body"]
    bids.close(6882)


def test_email_only_airline_gets_no_second_request(flag) -> None:
    flag(True)
    trucker = bids.Carrier("T", "trucker", "t@hackloop.mail", ("24",))
    assert bids._next_break(trucker, REQUEST, "HAM") is None
    # an airline without portal brackets on this lane
    unknown = bids.Carrier("X Air", "airline", "x-air@hackloop.mail", ("EZE",))
    assert bids._next_break(unknown, REQUEST, "HAM") is None


def _with_break_reply(case: dict) -> dict:
    """Round 27 plus the reply our second request would get at Meridian's 1000 kg rate."""
    case = copy.deepcopy(case)
    stored = next(m for m in case["mails"] if m["sender"] == MERIDIAN)
    case["mails"].append(
        {
            **stored,
            "id": stored["id"] + 1,
            "subject": f"{stored['subject']}, billed at 1000 kg",
            "body": stored["body"].replace("2877.65 EUR", "2840.00 EUR"),
        }
    )
    return case


def test_round_27_quotes_the_next_break(flag, corpus, tmp_path) -> None:
    case = next(c for c in corpus["cases"] if c["round_no"] == 27)
    flag(False)
    today = rfq_replay.replay_case(case, tmp_path / "off", corpus["lane_observations"])
    assert (today["airline"], today["total_minor"]) == ("Meridian Freightways", 301837)
    flag(True)
    got = rfq_replay.replay_case(
        _with_break_reply(case), tmp_path / "on", corpus["lane_observations"]
    )
    # 2,840.00 € air + 140.72 € Hansekogge = 2,980.72 €, the board's implied best 2,980.75 €
    assert (got["airline"], got["total_minor"]) == ("Meridian Freightways", 298072)
    assert "carrier_without_stored_reply" not in got["notes"]
