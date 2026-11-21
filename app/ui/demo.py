"""Fill a separate demo database with realistic sessions so the UI can be built and shown
before real mail arrives.

    DB_PATH=data/demo.db python -m app.ui.demo            # seed once
    DB_PATH=data/demo.db python -m app.ui.demo --live     # keep adding events (for streaming)
    DB_PATH=data/demo.db uvicorn app.ui.server:app --port 8000

Never point this at data/forwarder.db.
"""

from __future__ import annotations

import os
import random
import sys
import time
from datetime import UTC, datetime, timedelta

from app import db

TEAM = "austrian-power@hackloop.mail"
LANES = [
    ("FRA", "DFW", 40, "cheapest"),
    ("MUC", "LAX", 320, "fastest"),
    ("HAM", "MEX", 95, "cheapest"),
    ("CGN", "LOS", 44, "cheapest"),
    ("STR", "PVG", 610, "fastest"),
    ("DUS", "JFK", 180, "cheapest"),
    ("FRA", "SGN", 1200, "cheapest"),
]
AIRLINES = [
    "Elbflug Air Freight",
    "Yellowtail Air Freight",
    "Kestrel Airways",
    "Helix Air Freight",
]


def ts(minutes_ago: float) -> str:
    t = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return t.isoformat(timespec="seconds")


def seed() -> None:
    if os.path.basename(db._db_path) == "forwarder.db":
        sys.exit("Refusing to seed the real database. Set DB_PATH=data/demo.db")
    db.init_db()
    rnd = random.Random(7)
    mid = 1000
    companies = [
        "weidner-spielwaren",
        "auer-maschinenbau",
        "sonnberg-roesterei",
        "lindqvist-medtech",
        "kessler-feinmechanik",
        "rheintal-elektronik",
    ]
    n = 26
    for i in range(n):
        o, d, kg, pref = LANES[i % len(LANES)]
        mid += 10
        ago = 355 - i * 13.5
        customer = f"{companies[i % len(companies)]}@hackloop.mail"
        sid = db.ingest_mail(
            mail_id=mid,
            sender=customer,
            recipient=TEAM,
            subject=f"RFQ {o}-{d} {kg} kg",
            body=f"Hello,\nplease quote {kg} kg from {o} to {d}, {pref}.\nThanks",
            category="rfq",
            received_at=ts(ago),
        )
        assert sid is not None
        db.set_request(
            sid, {"origin": o, "destination": d, "weight_kg": kg, "mode": pref}
        )
        if i == n - 1:
            continue  # still pricing (fresh)
        alts = []
        for a in rnd.sample(AIRLINES, 3):
            alts.append(
                {
                    "airline": a,
                    "source": rnd.choice(["sheet", "portal", "bid"]),
                    "transit_days": rnd.randint(1, 6),
                    "total_minor": int(kg * rnd.uniform(2.2, 5.5) * 100) + 9000,
                }
            )
        best = min(
            alts,
            key=lambda a: a["total_minor"] if pref == "cheapest" else a["transit_days"],
        )
        db.record_llm_call(
            sid,
            purpose="extract",
            model="gpt-4o-mini",
            prompt_tokens=420,
            completion_tokens=60,
            cost_micro_usd=rnd.randint(80, 160),
            latency_ms=700,
        )
        db.emit(
            db.Ev.PRICED,
            f"{len(alts)} Optionen, beste: {best['airline']}",
            sid,
            duration_ms=rnd.randint(40, 300),
            options=len(alts),
        )
        db.record_llm_call(
            sid,
            purpose="reply",
            model="gpt-4o-mini",
            prompt_tokens=380,
            completion_tokens=140,
            cost_micro_usd=rnd.randint(150, 300),
            latency_ms=900,
        )
        db.claim_quote_send(sid)
        with db.tx() as c:  # backdate first reply to make response times realistic
            c.execute(
                "UPDATE sessions SET first_reply_at = ? WHERE id = ?",
                (ts(ago - rnd.uniform(0.6, 3.5)), sid),
            )
        db.record_outgoing(
            mail_id=mid + 1,
            recipient=customer,
            subject=f"Re: RFQ {o}-{d} {kg} kg",
            body=f"Hi,\nall-in {best['total_minor'] / 100:.2f} EUR via {best['airline']}.",
            category="quote",
            session_id=sid,
            in_reply_to=mid,
            references=[mid],
        )
        db.record_quote(
            sid,
            total_minor=best["total_minor"],
            currency="EUR",
            airline=best["airline"],
            origin=o,
            destination=d,
            transit_days=best["transit_days"],
            source=best["source"],
            alternatives=alts,
            mail_id=mid + 1,
        )
        db.save_board_result(
            round_id=f"demo-{sid}",
            verdict=rnd.choice(["accepted"] * 4 + ["rejected"]),
            reason=None,
            points={"total": rnd.randint(60, 95)},
            raw={},
            session_id=sid,
        )
        if i % 3 == 0:
            db.ingest_mail(
                mail_id=mid + 2,
                sender=customer,
                recipient=TEAM,
                subject=f"Re: RFQ {o}-{d} {kg} kg",
                body="Accepted, please book.",
                category="customer",
                references=[mid, mid + 1],
                in_reply_to=mid + 1,
            )
            db.record_booking(
                sid,
                booking_reference=f"BK-DEMO{i:04d}",
                rate_id=f"R-DEMO{i:06d}",
                airline=best["airline"],
                price_minor=best["total_minor"],
                currency="EUR",
            )
    db.set_paused(1000 + 10 * (n - 3), True)
    db.ingest_mail(
        mail_id=5000,
        sender="elbflug-air-freight@hackloop.mail",
        recipient=TEAM,
        subject="Tariff December 2026",
        body="Please find attached.",
        category="tariff",
    )
    db.ingest_mail(
        mail_id=6000,
        sender="tracking@hackloop.mail",
        recipient=TEAM,
        subject="Where is BK-DEMO0000?",
        body="Status please.",
        category="tracking",
    )
    print(f"Demo data written to {db._db_path}")


def live() -> None:
    db.init_db()
    rnd = random.Random()
    n = 9000
    while True:
        n += 10
        o, d, kg, pref = rnd.choice(LANES)
        sid = db.ingest_mail(
            mail_id=n,
            sender=f"live{n}@shipper.example",
            recipient=TEAM,
            subject=f"RFQ {o}-{d} {kg} kg",
            body="please quote",
            category="rfq",
        )
        db.set_request(
            sid, {"origin": o, "destination": d, "weight_kg": kg, "mode": pref}
        )
        time.sleep(3)
        db.emit(
            db.Ev.PRICED, "3 Optionen geprüft", sid, duration_ms=rnd.randint(50, 400)
        )
        time.sleep(3)


if __name__ == "__main__":
    live() if "--live" in sys.argv else seed()
