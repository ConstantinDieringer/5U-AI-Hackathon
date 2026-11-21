"""Monthly updates and portal changes, using isolated data and fake transports only."""

import dataclasses
import json
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app import db, hackloop
from app.agents import bids, tariff
from app.models import Mail, Rfq
from app.pricing import fx, portal_sync, pricing
from app.pricing.tariff_parser import TariffDocument, parse_attachment

ROOT = Path(__file__).parents[1]
RATE = json.loads((ROOT / "tests/fixtures/portal_rate_eur.json").read_text())
REFRESH = portal_sync.refresh_if_stale


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "test.db"))
    monkeypatch.setattr(pricing, "RUNTIME_TARIFFS", tmp_path / "tariffs.json")
    monkeypatch.setattr(portal_sync, "_cache", {})
    monkeypatch.setattr(portal_sync, "_seen_dataset_version", None)
    monkeypatch.setattr(portal_sync, "refresh_if_stale", lambda *a: None)
    monkeypatch.setattr(hackloop, "_rate_used", 0)
    monkeypatch.setattr(hackloop, "_rate_window_start", 0)
    monkeypatch.setattr(hackloop, "_rate_blocked_until", 0)
    monkeypatch.setattr(hackloop, "_rate_cap", 10)
    pricing.reload_lanes()
    yield
    pricing.reload_lanes()


def document(currency="EUR"):
    return TariffDocument.model_validate(
        {
            "airline": "Yellowtail Air Freight",
            "currency": currency,
            "valid_from": "2026-12-01",
            "valid_to": "2026-12-31",
            "lanes": [
                {
                    "origin": "FRA",
                    "destination": "DFW",
                    "transit_days": 2,
                    "minimum": "10",
                    "fuel": "0.5",
                    "security": "0.1",
                    "breaks": [{"from_kg": 0, "rate": "2"}, {"from_kg": 100, "rate": "1"}],
                }
            ],
        }
    )


def update(doc=None, digest="abc", stamp="2026-11-21T16:00:00+00:00"):
    return tariff.TariffUpdate(doc or document(), 500, digest, stamp)


def test_all_text_formats_match_seed_prices():
    seed = json.loads((ROOT / "data/tariffs.json").read_text())
    count = 0
    for path in (ROOT / "data/start-kit").glob("*tariff*"):
        if path.suffix == ".jpg":
            continue
        doc = parse_attachment(path)
        count += len(doc.lanes)
        assert (doc.valid_from, doc.valid_to) == (date(2026, 11, 1), date(2026, 11, 30))
        for lane in doc.lanes:
            expected = next(
                x
                for x in seed["lanes"]
                if (x["airline"], x["origin"], x["destination"])
                == (doc.airline, lane.origin, lane.destination)
            )
            assert lane.minimum == Decimal(str(expected["minimum"]))
            assert lane.fuel == Decimal(str(expected["fuel"]))
            assert lane.security == Decimal(str(expected["security"]))
            assert [(b.from_kg, b.rate) for b in lane.breaks] == [
                (Decimal(str(b["from_kg"])), Decimal(str(b["rate"]))) for b in expected["breaks"]
            ]
    assert count == 149  # nine JPG lanes are covered by the existing seed regression tests


def test_next_month_atomic_reload_and_duplicate(tmp_path):
    path = pricing.RUNTIME_TARIFFS
    assert not pricing.sheet_options(
        Rfq(origin="FRA", destination="DFW", weight_kg=120), on=date(2026, 12, 1)
    )
    assert tariff.publish(update(), path)
    assert not tariff.publish(update(), path)
    options = pricing.sheet_options(
        Rfq(origin="FRA", destination="DFW", weight_kg=120), on=date(2026, 12, 1)
    )
    assert len(options) == 1 and options[0].total_minor == 19200
    assert options[0].tariff_version == "abc"
    data = json.loads(path.read_text())
    assert len(data["lanes"]) == 159
    # Simulates restart after publication but before finishing the durable job.
    pricing.reload_lanes()
    assert not tariff.publish(update(), path)
    assert len(pricing.load_lanes()) == 159


def test_older_update_never_replaces_newer():
    path = pricing.RUNTIME_TARIFFS
    tariff.publish(update(), path)
    old = document()
    old.lanes[0].minimum = Decimal(999)
    tariff.publish(update(old, "older", "2026-11-20T16:00:00+00:00"), path)
    assert next(x for x in pricing.load_lanes() if x.valid_from.month == 12).minimum == 10


def test_concurrent_updates_do_not_lose_lanes():
    path = pricing.RUNTIME_TARIFFS
    a, b = document(), document()
    b.lanes[0].destination = "JFK"
    threads = [
        threading.Thread(target=tariff.publish, args=(update(d, h), path))
        for d, h in [(a, "a"), (b, "b")]
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(json.loads(path.read_text())["lanes"]) == 160


def test_invalid_tariff_rejected():
    raw = document().model_dump(mode="json")
    raw["lanes"][0]["breaks"][0]["rate"] = "-1"
    with pytest.raises(ValueError):
        TariffDocument.model_validate(raw)
    raw = document().model_dump(mode="json")
    raw["valid_to"] = "2026-11-01"
    with pytest.raises(ValueError):
        TariffDocument.model_validate(raw)


def test_tariff_job_survives_database_reopen(tmp_path):
    db.queue_tariff(123, {"id": 123})
    db.queue_tariff(123, {"id": 123})
    db.init_db(str(tmp_path / "test.db"))
    assert len(db.pending_tariffs()) == 1
    db.finish_tariff(123)
    assert db.pending_tariffs() == []


def test_sender_mismatch_preserves_snapshot(monkeypatch, tmp_path):
    p = tmp_path / "tariff.txt"
    p.write_text("test")
    from app.mail import poller

    monkeypatch.setattr(poller, "download_attachments", lambda m: [p])
    monkeypatch.setattr(tariff, "parse_attachment", lambda p: document())
    mail = Mail(
        500,
        500,
        "kestrel-airways@hackloop.mail",
        "team",
        "tariff",
        "",
        datetime.now(UTC).isoformat(),
    )
    with pytest.raises(ValueError, match="sender"):
        tariff.process(mail)
    assert not pricing.RUNTIME_TARIFFS.exists()


def test_dataset_alert_persistent_and_cache_invalidated(monkeypatch):
    portal_sync._note_dataset_version("v1")
    portal_sync._cache[("x",)] = (0, {})
    portal_sync._note_dataset_version("v2")
    assert not portal_sync._cache
    monkeypatch.setattr(portal_sync, "_seen_dataset_version", None)
    portal_sync._note_dataset_version("v2")
    events = [x for x in db.events_after(0) if x["kind"] == "dataset_changed"]
    assert len(events) == 1 and events[0]["payload"] == {"old": "v1", "new": "v2"}


def test_board_all_nonaccepted_alert_once():
    for verdict in ("open", "pending", "rejected", "accepted"):
        for _ in range(2):
            db.save_board_result(round_id="1", verdict=verdict, reason="test", raw={}, points=None)
    events = [x for x in db.events_after(0) if x["kind"] == "board_verdict"]
    assert [x["level"] for x in events] == ["warn", "warn", "warn", "info"]


def test_503_returns_email_fallback():
    def fetch(*args):
        raise hackloop.HackloopError("GET", "/v1/portal/rates", 503, "Ask airline by email")

    result = portal_sync.portal_result(Rfq(origin="CGN", destination="DEL", weight_kg=120), fetch)
    assert set(result.email_fallbacks) == {"JP", "UV"} and not result.options


def test_retry_after_is_not_capped():
    assert hackloop._retry_wait(httpx.Response(429, headers={"Retry-After": "65"})) == 65


def test_429_blocks_other_calls_without_exceeding_deadline(monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(429, headers={"Retry-After": "65"})

    monkeypatch.setattr(
        hackloop,
        "_client",
        httpx.Client(transport=httpx.MockTransport(respond), base_url="https://test"),
    )
    for _ in range(2):
        with pytest.raises(hackloop.HackloopError):
            hackloop.get_rate("FRA", "JFK", "X", 100, deadline=hackloop.time.monotonic() + 1)
    assert len(seen) == 1


def test_rate_limit_header_prevents_next_request(monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200, json=RATE, headers={"X-RateLimit-Limit": "1", "X-RateLimit-Remaining": "0"}
        )

    monkeypatch.setattr(
        hackloop,
        "_client",
        httpx.Client(transport=httpx.MockTransport(respond), base_url="https://test"),
    )
    hackloop.get_rate("CGN", "DEL", "JP", 120, deadline=hackloop.time.monotonic() + 1)
    with pytest.raises(hackloop.HackloopError):
        hackloop.get_rate("CGN", "DEL", "JP", 120, deadline=hackloop.time.monotonic() + 1)
    assert len(seen) == 1


def test_usd_sheets_and_bids_keep_original_amount(monkeypatch):
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    monkeypatch.setattr(
        fx,
        "settings",
        dataclasses.replace(fx.settings, usd_eur_rate="0.92", usd_eur_valid_until=expiry),
    )
    tariff.publish(update(document("USD")), pricing.RUNTIME_TARIFFS)
    option = pricing.sheet_options(
        Rfq(origin="FRA", destination="DFW", weight_kg=120), on=date(2026, 12, 1)
    )[0]
    assert (option.total_minor, option.total_eur_minor, option.currency) == (19200, 17664, "USD")
    mail = Mail(
        7,
        7,
        "yellowtail-air-freight@hackloop.mail",
        "team",
        "[AP-3] bid",
        "Total USD 100.00, 2 days",
        datetime.now(UTC).isoformat(),
    )
    bids.on_bid_mail(mail, 3)
    offer = bids.offers(3)[-1]
    assert offer.total_eur == Decimal("92.00") and offer.original_amount == 100
    bids.close(3)


@pytest.mark.parametrize(
    "rate,expiry",
    [
        ("", ""),
        ("NaN", "2099-01-01T00:00:00+00:00"),
        ("0.9", "2000-01-01T00:00:00+00:00"),
        ("0.9", "2099-01-01"),
    ],
)
def test_missing_or_expired_fx_rejected(monkeypatch, rate, expiry):
    monkeypatch.setattr(
        fx,
        "settings",
        dataclasses.replace(fx.settings, usd_eur_rate=rate, usd_eur_valid_until=expiry),
    )
    with pytest.raises(ValueError):
        fx.eur_rate("USD")


def test_usd_fx_not_applied_to_other_currency():
    with pytest.raises(ValueError):
        portal_sync.parse_rate({**RATE, "currency": "GBP", "fx_eur_per_usd": "0.92"})


def test_image_extraction_uses_gateway_and_validates(monkeypatch, tmp_path):
    calls = []

    def complete(*args, **kwargs):
        calls.append((args, kwargs))
        return document().model_dump_json()

    from app.pricing import tariff_parser

    monkeypatch.setattr(tariff_parser.llm, "complete", complete)
    p = tmp_path / "december.jpg"
    p.write_bytes(b"test-image")
    assert parse_attachment(p).currency == "EUR"
    assert calls[0][0][0] == "tariff_extract"
    assert calls[0][0][1][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_original_kit_cli_retains_all_158_lanes():
    from app.pricing.parse_tariffs import parse_kit

    data = parse_kit(ROOT / "data/start-kit")
    assert len(data["lanes"]) == 158
    assert data["valid_from"] == "2026-11-01" and data["valid_to"] == "2026-11-30"


def test_tariff_mail_changes_next_rfq_and_replay_skips_parser(monkeypatch, tmp_path):
    from app.mail import poller

    runtime = tmp_path / "runtime"
    monkeypatch.setattr(
        tariff, "settings", dataclasses.replace(tariff.settings, runtime_dir=runtime)
    )
    monkeypatch.setattr(pricing, "RUNTIME_TARIFFS", runtime / "tariffs.json")
    source = next((ROOT / "data/start-kit").glob("yellowtail*tariff*.txt"))
    replacement = tmp_path / "next-month.txt"
    replacement.write_text(
        source.read_text().replace("01.11.2026", "01.12.2026").replace("30.11.2026", "31.12.2026")
    )
    monkeypatch.setattr(poller, "download_attachments", lambda m: [replacement])
    mail = Mail(
        999,
        999,
        "yellowtail-air-freight@hackloop.mail",
        "team",
        "December tariff",
        "",
        datetime.now(UTC).isoformat(),
    )
    tariff.process(mail)
    opts = pricing.sheet_options(
        Rfq(origin="FRA", destination="DFW", weight_kg=120), on=date(2026, 12, 1)
    )
    assert len(opts) == 1 and opts[0].total_minor == 42240
    monkeypatch.setattr(
        tariff, "parse_attachment", lambda p: pytest.fail("duplicate tariff was parsed again")
    )
    tariff.process(mail)


def test_failed_snapshot_refresh_preserves_seed(monkeypatch, tmp_path):
    old = portal_sync.load_portal_lanes()
    monkeypatch.setattr(portal_sync, "_last_sync_attempt", float("-inf"))
    monkeypatch.setattr(portal_sync, "_force_refresh", True)

    def fail():
        raise OSError("offline")

    monkeypatch.setattr(portal_sync, "sync_portal_lanes", fail)
    REFRESH()
    assert portal_sync.load_portal_lanes() == old


def test_full_app_start_with_isolated_mailbox(tmp_path):
    """Boot real threads/server with a fake 5U transport and a disposable database."""
    import os
    import socket
    import subprocess
    import sys
    import time

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {
        **os.environ,
        "TEAM_API_KEY": "smoke-only",
        "HACKLOOP_API_KEY": "smoke-only",
        "DB_PATH": str(tmp_path / "smoke.db"),
        "RUNTIME_DIR": str(tmp_path / "runtime"),
        "INBOX_DIR": str(tmp_path / "inbox"),
        "PORT": str(port),
        "UI_PASSWORD": "",
    }
    code = (
        "import httpx; from app import hackloop; "
        "hackloop._client=httpx.Client(base_url='https://fake-5u.invalid', "
        "transport=httpx.MockTransport(lambda request: httpx.Response(200, "
        "json={'messages':[], 'next_since_id':0, 'rounds':[]}))); "
        "from run import main; main()"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        healthy = False
        while time.monotonic() < deadline and child.poll() is None:
            try:
                healthy = (
                    httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5).status_code == 200
                )
                if healthy:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        assert healthy, f"isolated application did not start (exit={child.poll()})"
        c = __import__("sqlite3").connect(tmp_path / "smoke.db")
        try:
            assert c.execute("select count(*) from tariff_jobs").fetchone()[0] == 0
        finally:
            c.close()
    finally:
        child.terminate()
        child.wait(timeout=10)
