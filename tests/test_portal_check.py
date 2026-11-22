"""Portal-Check: a person solves the portal page sum in our UI; we only relay it. No network."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, hackloop
from app.ui import portal_check, server

TEAM = "austrian-power@hackloop.mail"
IMAGE = "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode()
LOAD = {
    "id": "chal-1",
    "fields": [
        {"name": "e7487c00e", "label": "Origin (IATA)", "kind": "airport"},
        {"name": "p4593fea4", "label": "Team key", "kind": "password"},
        {"name": "s89fb17a7", "label": "Destination (IATA)", "kind": "airport"},
        {"name": "c36eb84d0", "label": "Volume (cbm)", "kind": "number"},
        {"name": "y1ccd04eb", "label": "Airline (code or name)", "kind": "airline"},
        {"name": "g0d5b7857", "label": "Weight (kg)", "kind": "number"},
    ],
    "answer": {"name": "a1b2", "image": IMAGE},
    "consent": {"name": "k9", "value": "necessary"},
}


class FakePage:
    """MockTransport for the portal page: records the submitted form."""

    def __init__(self, result: dict) -> None:
        self.result = result
        self.forms: list[bytes] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        if request.headers.get("x-portal-step") == "load":
            return httpx.Response(200, json=LOAD, headers={"set-cookie": "pc=abc; Path=/"})
        self.forms.append(request.read())
        return httpx.Response(200, json=self.result)


@pytest.fixture()
def page(monkeypatch):
    fake = FakePage(
        {"html": "<p>Juniper Cargo Lines</p><p>Total 412.50 EUR</p><p>R-abc12345xyz</p>"}
    )
    monkeypatch.setattr(hackloop, "_page_transport", httpx.MockTransport(fake))
    portal_check._pending.clear()
    return fake


@pytest.fixture()
def client(tmp_path):
    db.init_db(str(tmp_path / "pc.db"))
    with TestClient(server.app) as c:
        yield c


def _session() -> int:
    sid = db.ingest_mail(
        mail_id=1,
        sender="kunde@hackloop.mail",
        recipient=TEAM,
        subject="RFQ CGN-DEL",
        body="Please quote 120 kg CGN to DEL.",
        category="rfq",
    )
    db.set_request(
        sid, {"origin": "CGN", "destination": "DEL", "weight_kg": 120, "volume_cbm": 0.5}
    )
    return sid


def test_load_maps_fields_by_label_and_keeps_cookies(page):
    ch = hackloop.portal_ui_load()
    roles = dict(ch.fields)
    assert roles["e7487c00e"] == "origin" and roles["p4593fea4"] == "team key"
    assert ch.image == IMAGE and ("pc", "abc") in ch.cookies


def test_unknown_field_label_is_a_setup_change(monkeypatch):
    changed = {**LOAD, "fields": [{"name": "x", "label": "Shoe size", "kind": "number"}]}
    monkeypatch.setattr(
        hackloop,
        "_page_transport",
        httpx.MockTransport(lambda r: httpx.Response(200, json=changed)),
    )
    with pytest.raises(hackloop.HackloopError, match="unknown portal page field"):
        hackloop.portal_ui_load()


def test_non_svg_image_is_refused(monkeypatch):
    bad = {**LOAD, "answer": {"name": "a", "image": "javascript:alert(1)"}}
    monkeypatch.setattr(
        hackloop, "_page_transport", httpx.MockTransport(lambda r: httpx.Response(200, json=bad))
    )
    with pytest.raises(hackloop.HackloopError, match="sum image"):
        hackloop.portal_ui_load()


def test_human_check_end_to_end(client, page):
    sid = _session()
    opts = client.get(f"/api/sessions/{sid}/portal-check").json()
    assert (opts["origin"], opts["destination"]) == ("CGN", "DEL")
    assert "JP" in {a["code"] for a in opts["airlines"]}

    started = client.post(
        f"/api/sessions/{sid}/portal-check",
        json={"airline": "jp"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    ).json()
    assert started["image"] == IMAGE and started["airline"] == "Juniper Cargo Lines"

    r = client.post(
        f"/api/portal-check/{started['token']}",
        json={"answer": "18"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    ).json()
    assert r["ok"] and r["rate_id"] == "R-abc12345xyz" and "412.50 EUR" in r["text"]
    form = page.forms[0].decode()
    # the person's answer, consent and the lane go under the server's field names
    for name, value in [
        ("a1b2", "18"),
        ("k9", "necessary"),
        ("e7487c00e", "CGN"),
        ("y1ccd04eb", "JP"),
    ]:
        assert f'name="{name}"\r\n\r\n{value}\r\n' in form
    assert page.headers[-1]["x-portal-challenge"] == "chal-1"
    assert "pc=abc" in page.headers[-1].get("cookie", "")
    kinds = [e["kind"] for e in db.events_after(0) if e["session_id"] == sid]
    assert "portal_check_started" in kinds and "portal_check_rate" in kinds


def test_check_is_single_use(client, page):
    sid = _session()
    tok = portal_check.start(sid, "JP")["token"]
    portal_check.solve(tok, "18")
    with pytest.raises(LookupError):
        portal_check.solve(tok, "18")


def test_wrong_answer_is_reported_not_raised(client, monkeypatch, page):
    page.result = {"error": "Wrong sum. Try a new one."}
    sid = _session()
    tok = portal_check.start(sid, "JP")["token"]
    r = portal_check.solve(tok, "17")
    assert r == {"ok": False, "error": "Wrong sum. Try a new one."}
    assert any(e["kind"] == "portal_check_failed" for e in db.events_after(0))


def test_answer_must_be_a_number(client, page):
    sid = _session()
    tok = portal_check.start(sid, "JP")["token"]
    with pytest.raises(ValueError):
        portal_check.solve(tok, "18; drop")


def test_airline_not_on_lane_is_refused(client, page):
    sid = _session()
    with pytest.raises(ValueError, match="nicht im Portal"):
        portal_check.start(sid, "ZZ")


def test_result_html_becomes_plain_text():
    assert portal_check.html_text("<b>Total</b> <script>x</script>1 EUR") == "Total · 1 EUR"
    assert json.dumps(portal_check.html_text("<p>a</p>"))  # serialisable
