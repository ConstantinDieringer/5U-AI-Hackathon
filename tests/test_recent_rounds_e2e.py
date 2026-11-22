"""Offline RFQ flows for a conflicting packing list and a dangerous-goods declaration."""

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


def build_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **changes: Any) -> World:
    """A fresh database and every stub; `changes` override the settings under test."""
    db.init_db(str(tmp_path / "e2e.db"))
    w = World(tmp_path)
    settings = dataclasses.replace(
        config.settings,
        rfq_validation_mode="enforce",
        trucker_balance="ratio",
        clarify_once=False,
        self_delivery_recovery=False,
        ask_pickup_zip=False,
        bid_next_break=False,
        price_adjust_pct=0,
    )
    settings = dataclasses.replace(settings, **changes)

    def configure(**more: Any) -> None:
        for module in (config, rfq, clarification, bids):
            monkeypatch.setattr(
                module, "settings", dataclasses.replace(settings, **more), raising=False
            )

    configure()
    w.settings_clarify_once = settings.clarify_once  # type: ignore[attr-defined]
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


@pytest.fixture(params=[False, True], ids=["clarify_cap_two", "clarify_once"])
def world(request, tmp_path, monkeypatch) -> World:
    """Runs every scenario with today's two-question cap and with CLARIFY_ONCE."""
    return build_world(tmp_path, monkeypatch, clarify_once=request.param)


def _quote(sid: int) -> dict[str, Any] | None:
    quotes = (db.get_session(sid) or {}).get("quotes") or []
    return quotes[0] if quotes else None


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


# ---------------------------------------------------------------- session 56420 (Lindqvist)

SESSION_56420_CUSTOMER = "lindqvist-medtech@hackloop.mail"
SESSION_56420 = (
    "Hi,\n\nCould you quote the cheapest air freight option from FRA to JFK, all-in in EUR? "
    "We'll deliver the shipment to FRA ourselves. It'll be ready on 2026-11-28.\n\nIt's 4 "
    "pieces of touch-up paint for machine housings, general cargo (non-dangerous), with a "
    "gross weight of 1618 kg and a volume of 4.45 cbm. The packing list is attached.\n\nSara"
)
SESSION_56420_FACTS = {
    "origin": "FRA",
    "pickup_zip": None,
    "destination": "JFK",
    "weight_kg": 1618.0,
    "volume_cbm": 4.45,
    "pieces": 4,
    "preference": "cheapest",
    "ready_date": "2026-11-28",
    "language": "en",
    "customer_name": "Sara",
}
SESSION_56420_PROMISE = (
    "Thank you for asking. We'll send you the dangerous goods documents (shipper's "
    "declaration and safety data sheet) shortly.\n\nSara"
)


@pytest.mark.core
def test_session_56420_dg_promise_on_rescue_path_quotes_without_a_model(
    tmp_path, monkeypatch
) -> None:
    """The packing list's table header "Gross weight (kg)" read the first row's 4 pieces as
    the weight (false conflict), and the rescue's model plan turned the customer's DG
    document promise into "wait" until the deadline. The promise lifts the hold (round 41):
    the shared answer rules price it, no model call."""
    from app.agents import rescue

    w = build_world(tmp_path, monkeypatch, clarify_once=True)
    w._ids = 56_460  # replies after the RFQ, as in production
    w.extraction = SESSION_56420_FACTS
    w.packing_spec(
        [
            ("Packing List",),
            ("Pieces", "Width (cm)", "Height (cm)", "Length (cm)", "Gross weight (kg)"),
            (4, 80, 116, 120, 1618),
            ("Total pieces:", 4),
            ("Total gross weight (kg):", 1618),
            ("Total volume (cbm):", 4.45),
            ("Dangerous goods:", "UN1263, Class 3, Paint"),
        ]
    )
    root = w.rfq(56420, SESSION_56420_CUSTOMER, "Air freight quote FRA to JFK", SESSION_56420,
                 attachments=1)  # fmt: skip
    assert _quote(56420) is None
    issues = {i["field"] for i in db.validation_get(56420)["draft"]["issues"]}
    assert "weight_kg" not in issues and "dangerous_goods" in issues

    mid = w.next_id()
    now = datetime.now(UTC).isoformat()
    db.ingest_mail(mail_id=mid, sender=root.sender, recipient=TEAM,
                   subject=f"Re: {root.subject}", body=SESSION_56420_PROMISE,
                   category="customer", received_at=now, references=[root.id])  # fmt: skip
    reply = Mail(mid, root.id, root.sender, TEAM, f"Re: {root.subject}",
                 SESSION_56420_PROMISE, now, references=[root.id])  # fmt: skip
    assert rescue.take_over(reply, root.id)
    assert _quote(56420) is not None
    assert w.network_blocked == 0


def test_table_header_weight_is_not_a_total() -> None:
    from decimal import Decimal

    from app.mail.rfq_validation import numeric_facts

    text = (
        "Pieces | Width (cm) | Height (cm) | Length (cm) | Gross weight (kg)\n"
        "4 | 80 | 116 | 120 | 1618\nTotal pieces: | 4\nTotal gross weight (kg): | 1618\n"
    )
    assert numeric_facts(text)["weight_kg"] == Decimal(1618)
    assert numeric_facts("Gross weight:\n1674 kg")["weight_kg"] == Decimal(1674)


@pytest.mark.core
def test_session_56420_reply_during_takeover_is_answered_after_handback(
    tmp_path, monkeypatch
) -> None:
    """Mail 56535 arrived while a person held the thread and stayed unanswered after the
    handback: any manual reply hid the whole session from the rescue sweep. A manual reply
    covers only the input it answered; a decline notice still ends the thread."""
    from app.agents import rescue

    w = build_world(tmp_path, monkeypatch)
    w._ids = 56_460
    w.extraction = SESSION_56420_FACTS
    w.packing_spec([("Total gross weight (kg):", 1618), ("Total volume (cbm):", 4.45),
                    ("Dangerous goods:", "UN1263, Class 3, Paint")])  # fmt: skip
    root = w.rfq(56420, SESSION_56420_CUSTOMER, "Air freight quote FRA to JFK", SESSION_56420,
                 attachments=1)  # fmt: skip
    db.takeover(56420)
    db.record_outgoing(mail_id=w.next_id(), recipient=root.sender, subject="Air freight",
                       body="Please send the documents.", category="manual_reply",
                       session_id=56420, in_reply_to=root.id)  # fmt: skip
    mid = w.next_id()
    db.ingest_mail(mail_id=mid, sender=root.sender, recipient=TEAM,
                   subject=f"Re: {root.subject}", body=SESSION_56420_PROMISE,
                   category="customer", received_at=datetime.now(UTC).isoformat(),
                   references=[root.id])  # fmt: skip
    future = "2999-01-01T00:00:00+00:00"

    assert not db.rescue_candidates(future)  # still manual: the person owns it
    db.handback(56420)
    assert [c["id"] for c in db.rescue_candidates(future)] == [mid]
    assert rescue.resume(56420, background=False)
    assert _quote(56420) is not None

    db.record_outgoing(mail_id=w.next_id(), recipient=root.sender, subject="Air freight",
                       body="We cannot carry this.", category="notice", session_id=56420,
                       in_reply_to=mid)  # fmt: skip
    assert not db.rescue_candidates(future)
