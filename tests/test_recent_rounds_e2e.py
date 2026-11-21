"""End to end through the real RFQ flow for the three newest rules (rounds 35, 39 and 42).

Each test drives `rfq.handle_rfq` and `clarification.handle_reply` with stubbed mail, portal
and gateway; nothing leaves the machine. The real mails of rounds 32 to 46 are not stored in
this repo, so the bodies are rebuilt around the values verified against the real mail:

- round 42 (mail 10820): the exclusion sentence is verbatim, the four bids are the real ones
  (Zephyr 2780.00, Lagoon Isles 3210.00 / 3 d, Himalaya Bridge 3220.00 / 6 d, Pampero
  3780.00 / 5 d, FRA to ULN); pickup site, cargo and trucker bids are placeholders;
- round 35 (mail 8859): mail 2644 kg, attached packing specification 1751 kg, same volume,
  answer "the attached packing specification is correct" (rounds 33 and 34);
- round 39 (mail 10132): UN3481 declared only in the attachment; the answer is round 41's.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import openpyxl
import pytest

from app import config, db, hackloop, llm
from app.agents import bids, clarification, rfq
from app.mail import poller
from app.models import Mail
from app.pricing.portal_sync import PortalResult

TEAM = "austrian-power@hackloop.mail"
ZEPHYR = "zephyr-steppe-cargo@hackloop.mail"
LAGOON = "lagoon-isles-airways@hackloop.mail"
HIMALAYA = "himalaya-bridge-air@hackloop.mail"
PAMPERO = "pampero-air-cargo@hackloop.mail"
TAUNUS = "taunusfracht@hackloop.mail"
ISARWELLE = "isarwelle-spedition@hackloop.mail"


class World:
    """One customer, the carriers' stored answers and every mail we send."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.sent: list[dict[str, Any]] = []
        self.extraction: dict[str, Any] = {}
        self.attachments: list[Path] = []
        self.replies: dict[str, str] = {}  # carrier email -> reply body
        self.network_blocked = 0
        self._ids = 50_000

    def next_id(self) -> int:
        self._ids += 1
        return self._ids

    # ------------------------------------------------------------ stubs

    def send(self, to: str, subject: str, body: str, in_reply_to: int | None = None, **_k: Any):
        out = {
            "id": self.next_id(),
            "to": to,
            "subject": subject,
            "body": body,
            "in_reply_to": in_reply_to,
        }
        self.sent.append(out)
        return out

    def blocked(self, *_a: Any, **_k: Any) -> Any:
        self.network_blocked += 1
        raise RuntimeError("test: network disabled")

    def complete(self, purpose: str, *_a: Any, **_k: Any) -> str:
        if purpose.startswith("extract"):
            return json.dumps(self.extraction)
        raise ValueError(f"test: no gateway for {purpose}")  # composer -> deterministic text

    def deliver(self, sid: int, carrier_email: str, body: str) -> None:
        mail = Mail(
            self.next_id(),
            self.next_id(),
            carrier_email,
            TEAM,
            f"Re: [AP-{sid}] bid",
            body,
            datetime.now(UTC).isoformat(),
        )
        bids.on_bid_mail(mail, sid)

    # ------------------------------------------------------------ customer side

    def rfq(self, mid: int, sender: str, subject: str, body: str, attachments: int = 0) -> Mail:
        now = datetime.now(UTC).isoformat()
        files = [{"id": i + 1, "filename": f"spec{i + 1}.xlsx"} for i in range(attachments)]
        db.ingest_mail(
            mail_id=mid,
            sender=sender,
            recipient=TEAM,
            subject=subject,
            body=body,
            category="rfq",
            received_at=now,
        )
        mail = Mail(mid, mid, sender, TEAM, subject, body, now, attachments=files)
        rfq.handle_rfq(mail, mid)
        return mail

    def answer(self, root: Mail, body: str) -> None:
        mid = self.next_id()
        now = datetime.now(UTC).isoformat()
        db.ingest_mail(
            mail_id=mid,
            sender=root.sender,
            recipient=TEAM,
            subject=f"Re: {root.subject}",
            body=body,
            category="customer",
            received_at=now,
            references=[root.id],
        )
        clarification.handle_reply(
            Mail(
                mid,
                root.id,
                root.sender,
                TEAM,
                f"Re: {root.subject}",
                body,
                now,
                references=[root.id],
            ),
            root.id,
        )

    def to(self, address: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["to"] == address]

    def packing_spec(self, rows: list[tuple[str, object]]) -> None:
        book = openpyxl.Workbook()
        sheet = book.active
        for row in rows:
            sheet.append(list(row))
        path = self.tmp / f"{len(self.attachments) + 1}-packing.xlsx"
        book.save(path)
        self.attachments.append(path)


@pytest.fixture(params=[False, True], ids=["clarify_cap_two", "clarify_once"])
def world(request, tmp_path, monkeypatch) -> World:
    """Runs every scenario with today's two-question cap and with CLARIFY_ONCE."""
    db.init_db(str(tmp_path / "e2e.db"))
    w = World(tmp_path)
    settings = dataclasses.replace(
        config.settings,
        rfq_validation_mode="enforce",
        trucker_balance="ratio",
        clarify_once=request.param,
        self_delivery_recovery=False,
        ask_pickup_zip=False,
        bid_next_break=False,
        price_adjust_pct=0,
    )

    def configure(**changes: Any) -> None:
        for module in (config, rfq, clarification, bids):
            monkeypatch.setattr(
                module, "settings", dataclasses.replace(settings, **changes), raising=False
            )

    configure()
    w.settings_clarify_once = request.param  # type: ignore[attr-defined]
    w.configure = configure  # type: ignore[attr-defined]
    monkeypatch.setattr(hackloop, "_request", w.blocked)
    monkeypatch.setattr(hackloop, "send", w.send)
    monkeypatch.setattr(llm, "complete", w.complete)
    monkeypatch.setattr(rfq, "portal_result", lambda *_a, **_k: PortalResult())
    monkeypatch.setattr(poller, "download_attachments", lambda m: list(w.attachments))
    original = bids.request

    def bid_request(sid: int, rfq_: Any, origin: str, carriers: list[bids.Carrier]) -> int:
        n = original(sid, rfq_, origin, carriers)
        for c in carriers:  # a carrier answers only when we asked it
            if c.email in w.replies:
                w.deliver(sid, c.email, w.replies[c.email])
        return n

    monkeypatch.setattr(bids, "request", bid_request)
    monkeypatch.setattr(rfq.bids, "wait", lambda *_a, **_k: [])
    return w


def _quote(sid: int) -> dict[str, Any] | None:
    quotes = (db.get_session(sid) or {}).get("quotes") or []
    return quotes[0] if quotes else None


# ---------------------------------------------------------------- round 42 (mail 10820)

MEYER = "meyer-industrie@hackloop.mail"
ROUND_42 = (
    "Hello,\n\nplease send us your all-in price in EUR for the cheapest air freight from our "
    "plant in 60311 Frankfurt am Main to Ulaanbaatar (ULN), Mongolia. 6 pieces, 640 kg gross, "
    "3.10 cbm, ready on 2026-11-26.\n\nPlease do not offer Zephyr Steppe Cargo (ZS) for this "
    "shipment or for any of our future inquiries.\n\nBest regards,\nAnna Meyer\nMeyer Industrie"
)
ROUND_42_FACTS = {
    "origin": None,
    "pickup_zip": "60311",
    "destination": "ULN",
    "weight_kg": 640.0,
    "volume_cbm": 3.1,
    "pieces": 6,
    "preference": "cheapest",
    "ready_date": "2026-11-26",
    "language": "en",
    "customer_name": "Anna",
}
BIDS_42 = {  # the four real airline bids of round 42, FRA to ULN
    ZEPHYR: "Our all-in price is EUR 2780.00, transit time 4 days.",
    LAGOON: "Our all-in price is EUR 3210.00, transit time 3 days.",
    HIMALAYA: "Our all-in price is EUR 3220.00, transit time 6 days.",
    PAMPERO: "Our all-in price is EUR 3780.00, transit time 5 days.",
    TAUNUS: "Our all-in price is EUR 180.00, pickup within 1 day.",
    ISARWELLE: "Our all-in price is EUR 160.00, pickup within 2 days.",
}


def test_round_42_exclusion_holds_now_and_for_the_next_request(world: World) -> None:
    w = world
    w.extraction = ROUND_42_FACTS
    w.replies = dict(BIDS_42)
    w.rfq(10820, MEYER, "Air freight Frankfurt to Ulaanbaatar", ROUND_42)

    quote = _quote(10820)
    assert quote is not None, db.get_session(10820)["events"][-3:]
    assert quote["airline"] == "Lagoon Isles Airways"
    # Lagoon Isles 3210.00 plus the balanced trucker (Taunusfracht 180.00, 1 day, beats
    # Isarwelle 160.00, 2 days, on price/cheapest + time/fastest)
    assert quote["total_minor"] == 321_000 + 18_000
    trucker = next(a["trucker"] for a in quote["alternatives"] if "trucker" in a)
    assert trucker == "Taunusfracht"
    body = next(m["body"] for m in w.to(MEYER) if m["id"] == quote["mail_id"])
    assert "Lagoon Isles Airways" in body and "3.390 €" in body
    assert "Zephyr" not in body
    assert w.to(ZEPHYR) == []  # never asked for a bid
    assert {LAGOON, HIMALAYA, PAMPERO} <= {m["to"] for m in w.sent}

    # a later RFQ of the same customer that does not repeat the exclusion
    w.extraction = {**ROUND_42_FACTS, "weight_kg": 410.0, "volume_cbm": 2.0, "pieces": 4}
    later = (
        ROUND_42.split("\n\nPlease do not")[0]
        .replace("640 kg", "410 kg")
        .replace("3.10 cbm", "2.00 cbm")
        + "\n\nBest regards,\nAnna Meyer\nMeyer Industrie"
    )
    assert "Zephyr" not in later
    w.rfq(11500, MEYER, "Next shipment to Ulaanbaatar", later)
    assert w.to(ZEPHYR) == []
    # even an unasked Zephyr bid that arrives for the session is never quoted
    second = _quote(11500)
    assert second is not None and second["airline"] == "Lagoon Isles Airways"
    assert all(o.get("airline") != "Zephyr Steppe Cargo" for o in second["alternatives"])
    assert w.network_blocked == 0


# ---------------------------------------------------------------- round 35 (mail 8859)

ROUND_35_CUSTOMER = "brandt-medizintechnik@hackloop.mail"
ROUND_35 = (
    "Good morning,\n\nwe need an all-in price in EUR for the cheapest air freight from "
    "Frankfurt (FRA) to New York (JFK). We deliver to FRA ourselves. 14 pieces, gross weight "
    "2644 kg, total volume 7.20 cbm, ready on 2026-11-26. The packing specification is "
    "attached.\n\nKind regards,\nJonas Brandt"
)
ROUND_35_FACTS = {
    "origin": "FRA",
    "pickup_zip": None,
    "destination": "JFK",
    "weight_kg": 2644.0,
    "volume_cbm": 7.2,
    "pieces": 14,
    "preference": "cheapest",
    "ready_date": "2026-11-26",
    "language": "en",
    "customer_name": "Jonas",
}
ANSWER_35 = (
    "Hello,\n\nthe attached packing specification is correct.\n\nKind regards,\nJonas Brandt"
)


def _round_35(w: World) -> Mail:
    w.extraction = ROUND_35_FACTS
    w.packing_spec(
        [
            ("Packing specification", ""),
            ("Pieces", 14),
            ("Total gross weight (kg):", 1751),
            ("Total volume (cbm):", 7.2),
        ]
    )
    root = w.rfq(8859, ROUND_35_CUSTOMER, "Air freight FRA to JFK", ROUND_35, attachments=1)
    assert _quote(8859) is None
    questions = w.to(ROUND_35_CUSTOMER)
    assert len(questions) == 1
    assert "weight" in questions[0]["body"].lower()
    w.answer(root, ANSWER_35)
    return root


def test_round_35_asks_once_then_prices_the_attachment_weight(world: World) -> None:
    w = world
    w.configure(clarify_once=w.settings_clarify_once, conflict_source_answer=True)
    _round_35(w)

    quote = _quote(8859)
    assert len(w.to(ROUND_35_CUSTOMER)) == 2, [m["body"][:120] for m in w.to(ROUND_35_CUSTOMER)]
    assert quote is not None
    assert db.get_session(8859)["request"]["weight_kg"] == 1751.0
    audit = [e for e in db.get_session(8859)["events"] if e["kind"] == "pricing_audit"][-1]
    assert audit["payload"]["selected"]["chargeable_kg"] == 1751.0
    assert w.network_blocked == 0


def test_round_35_today_without_the_setting(world: World) -> None:
    """CONFLICT_SOURCE_ANSWER=false (default): the answer names no number, so today's cap asks
    a second time and CLARIFY_ONCE stops for a person. Neither quotes."""
    w = world
    _round_35(w)
    assert _quote(8859) is None
    events = {e["kind"] for e in db.get_session(8859)["events"]}
    if w.settings_clarify_once:
        assert len(w.to(ROUND_35_CUSTOMER)) == 1 and "needs_human" in events
    else:
        assert len(w.to(ROUND_35_CUSTOMER)) == 2


# ---------------------------------------------------------------- round 39 (mail 10132)

ROUND_39_CUSTOMER = "seidel-elektronik@hackloop.mail"
ROUND_39 = (
    "Hello,\n\nplease quote the cheapest air freight from Frankfurt (FRA) to Miami (MIA), all "
    "in, in EUR. We deliver to FRA ourselves. 5 pieces, 378 kg gross, 1.86 cbm, ready on "
    "2026-11-27. Packing specification attached.\n\nBest regards,\nLena Seidel"
)
ROUND_39_FACTS = {
    "origin": "FRA",
    "pickup_zip": None,
    "destination": "MIA",
    "weight_kg": 378.0,
    "volume_cbm": 1.86,
    "pieces": 5,
    "preference": "cheapest",
    "ready_date": "2026-11-27",
    "language": "en",
    "customer_name": "Lena",
}
ANSWER_41 = (  # round 41 (mail 10580): accepted after this answer
    "Hello,\n\nthank you. We will send you the shipper's declaration and the safety data sheet "
    "shortly.\n\nBest regards,\nLena Seidel"
)


def test_round_39_dg_in_attachment_asks_for_documents_then_quotes(world: World) -> None:
    w = world
    w.extraction = ROUND_39_FACTS
    w.packing_spec(
        [
            ("Total gross weight (kg):", 378),
            ("Total volume (cbm):", 1.86),
            ("Dangerous goods:", "UN3481, Class 9, lithium ion batteries packed with equipment"),
        ]
    )
    root = w.rfq(10132, ROUND_39_CUSTOMER, "Air freight FRA to MIA", ROUND_39, attachments=1)

    assert _quote(10132) is None
    first = w.to(ROUND_39_CUSTOMER)
    assert len(first) == 1
    text = first[0]["body"]
    assert "Shipper's Declaration" in text or "DGD" in text
    assert "safety data sheet" in text.lower() or "SDS" in text
    assert "UN3481" in text

    w.answer(root, ANSWER_41)

    quote = _quote(10132)
    assert quote is not None
    mails = w.to(ROUND_39_CUSTOMER)
    assert len(mails) == 2 and mails[1]["id"] == quote["mail_id"]
    assert db.get_session(10132)["request"]["weight_kg"] == 378.0
    assert w.network_blocked == 0


# ---------------------------------------------------------------- the answer itself


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("the attached packing specification is correct.", {"weight_kg": 1751.0}),
        ("The packing list is right, sorry for the typo.", {"weight_kg": 1751.0}),
        ("Die Angaben im Anhang sind korrekt.", {"weight_kg": 1751.0}),
        ("Die Packliste stimmt.", {"weight_kg": 1751.0}),
        ("The figures in my email are correct.", {"weight_kg": 2644.0}),
        ("The attachment is not correct.", {}),
        ("The attachment is incorrect, my email is correct.", {"weight_kg": 2644.0}),
        ("Both the attachment and my email are correct.", {}),
        ("Thanks, we will check with the warehouse.", {}),
    ],
)
def test_confirmed_source(text: str, want: dict[str, float]) -> None:
    from app.mail.rfq_validation import confirmed_source
    from app.models import RfqDraft

    draft = RfqDraft(
        evidence={
            "weight_kg": {
                "source": "body",
                "value": "2644",
                "observations": [["body", "2644"], ["spec1.xlsx", "1751"]],
            }
        }
    )
    assert confirmed_source(text, draft, {"weight_kg"}) == want
