"""Fresh cargo facts must not borrow airports or lot totals from obsolete mail history."""

import json

import pytest

from app.agents import rfq
from app.models import Mail


@pytest.mark.parametrize(
    "header",
    [
        "-----Original Message-----",
        "---------- Forwarded message ---------",
        "Begin forwarded message:",
        "On Monday someone wrote:",
        "Am Montag schrieb jemand:",
    ],
)
def test_current_shipment_excludes_old_data_and_subject(header, monkeypatch):
    body = (
        "Please quote to SGN with pickup from our warehouse. 11 pieces, 572 kg, 2.29 cbm.\n"
        f"{header}\nLot 1: 128 pieces, 165 kg gross, 11.8 cbm\nAMS to HKG"
    )
    mail = Mail(1, 1, "x", "y", "RE: FW: AMS-HKG / 165 KGS", body, "2026-11-21T16:26:00Z")
    calls = []

    def complete(purpose, messages, *args, **kwargs):
        calls.append(messages)
        return json.dumps({"destination": "SGN", "weight_kg": 572, "volume_cbm": 2.29})

    monkeypatch.setattr(rfq.llm, "complete", complete)
    parsed = rfq.extract_rfq(mail, 1)
    assert parsed.origin is None and parsed.pickup_zip is None
    assert parsed.weight_kg == 572 and parsed.volume_cbm == 2.29
    source = calls[0][1]["content"]
    assert "572 kg" in source
    assert "AMS" not in source and "165" not in source and "Lot 1" not in source


def test_forward_only_request_keeps_its_specification():
    body = "Please quote the request below.\n-----Original Message-----\nFRA to NRT, 500 kg."
    assert "FRA to NRT, 500 kg" in rfq._clean(body)


def test_unquoted_request_and_inline_quotes_remain_readable():
    body = "Please quote DUS to JFK, 120 kg.\n> previous quote 900 kg\nReady tomorrow."
    cleaned = rfq._clean(body)
    assert "DUS to JFK, 120 kg" in cleaned and "Ready tomorrow" in cleaned
    assert "900 kg" not in cleaned
