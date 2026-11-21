"""TOTALS_SAME_LINE: a table header ending in "Gross weight (kg)" is no total.

Kessler BOM thread (21:59, RE: FW: BRUSSELS AIRPORT - PPT): the mail said "gross weight of
1674 kg", yet our first question asked for the weight. The packing specification itself is
not stored here; the attachment below is rebuilt in 5U's XLSX layout (mail 2256: table before
the totals, columns shuffled per mail). Whenever "Gross weight (kg)" is the last header cell,
today's pattern crosses the line break and reads the first cell of the data row as the
attachment total, a false conflict with the mail. The forwarded "5,390 KGS" plays no part:
current_message cuts it off and the RE/FW subject is dropped.
"""

from __future__ import annotations

import dataclasses
import itertools
from decimal import Decimal

import pytest

from app import config
from app.mail.content import current_message
from app.mail.rfq_validation import numeric_facts, validate
from app.models import Mail, Rfq

KESSLER = "kessler-feinmechanik@hackloop.mail"
KESSLER_SUBJECT = "RE: FW: BRUSSELS AIRPORT - PPT / 104 PCS 5,390 KGS"
KESSLER_RFQ = (  # verbatim as shown in the thread
    "Dear Sir or Madam,\n\n"
    "Could you please quote the cheapest air freight option to BOM (Mumbai, IN)? Please arrange "
    "truck pickup from our site in 73342 (Esslingen), Germany, and include transport to the "
    "departure airport in your all-in price in EUR.\n\n"
    "The shipment will be ready for pickup on 2026-11-27. It consists of 11 pieces of cleaning "
    "agent, with a gross weight of 1674 kg and a total volume of 4.36 cbm. The packing "
    "specification is attached.\n\n"
    "Thank you. I look forward to your quote.\n\n"
    "Mit freundlichen Gruessen / Kind regards\nJonas Kessler\nLogistics Coordinator\n"
    "Kessler Feinmechanik GmbH, Ulm\n\n\n"
    "-----Original Message-----\nSent: 2026-09-16\n"
    "Subject: BRUSSELS AIRPORT - PPT / 104 PCS 5,390 KGS\n\n"
    "Dear team,\n\npls quote air freight ex BRUSSELS AIRPORT to PPT:\n"
    "104 PCS / 5,390 KGS / 1.6 CBM, machine parts\nFCA, cargo ready next week.\n\nThanks\n"
)
KESSLER_FACTS = {
    "origin": None,
    "pickup_zip": "73342",
    "destination": "BOM",
    "weight_kg": 1674.0,
    "volume_cbm": 4.36,
    "pieces": 11,
    "preference": "cheapest",
    "ready_date": "2026-11-27",
    "language": "en",
    "customer_name": "Jonas",
}
COLUMNS = ("Height (cm)", "Gross weight (kg)", "Width (cm)", "Length (cm)", "Pieces")
ROW = {
    "Height (cm)": 130,
    "Gross weight (kg)": 1674,
    "Width (cm)": 100,
    "Length (cm)": 120,
    "Pieces": 11,
}


def packing_rows(columns: tuple[str, ...] = COLUMNS) -> list[tuple[object, ...]]:
    """5U's XLSX packing specification (layout of mail 2256), one data row."""
    return [
        ("Packing Specification",),
        ("No. 48123",),
        (),
        ("Date:", "2026-11-27"),
        ("Shipper:", "Kessler Feinmechanik GmbH, Ulm, Germany"),
        ("Consignee:", "Mumbai Imports Ltd., Mumbai, IN"),
        ("Destination airport:", "BOM"),
        ("Goods description:", "cleaning agent"),
        (),
        tuple(columns),
        tuple(ROW[c] for c in columns),
        (),
        ("Total pieces:", 11),
        ("Total gross weight (kg):", 1674),
        ("Total volume (cbm):", 4.36),
        ("Dangerous goods:", "UN1760, Class 8, Corrosive liquid, n.o.s."),
    ]


def serialize(rows: list[tuple[object, ...]]) -> str:
    """The same text app.mail.rfq_sources.read_sources makes of an XLSX sheet."""
    return "\n".join(" | ".join(str(v) for v in row if v is not None) for row in rows)


def kessler_mail() -> Mail:
    return Mail(1, 1, KESSLER, "team", KESSLER_SUBJECT, KESSLER_RFQ, "2026-11-21T20:59:11Z")


@pytest.fixture
def same_line(monkeypatch):
    def set_(on: bool) -> None:
        monkeypatch.setattr(
            config, "settings", dataclasses.replace(config.settings, totals_same_line=on)
        )

    return set_


HEADER_LAST = sorted(p for p in itertools.permutations(COLUMNS) if p[-1] == "Gross weight (kg)")


def test_forwarded_history_is_not_evidence():
    current = current_message(KESSLER_RFQ, KESSLER_SUBJECT)
    assert "5,390" not in current.facts and current.subject == ""
    assert numeric_facts(current.facts) == {
        "weight_kg": Decimal(1674),
        "volume_cbm": Decimal("4.36"),
        "pieces": Decimal(11),
    }


@pytest.mark.parametrize("columns", HEADER_LAST, ids=lambda c: "/".join(x[:2] for x in c))
def test_today_header_cell_reads_the_next_row(same_line, columns):
    """Root cause, pinned as today's behaviour with the setting off."""
    same_line(False)
    _, draft = validate(
        kessler_mail(), Rfq(**KESSLER_FACTS), {"spec.xlsx": serialize(packing_rows(columns))}
    )
    assert {(i["field"], i["code"]) for i in draft.issues} == {
        ("weight_kg", "conflict"),
        ("dangerous_goods", "documents_required"),
    }
    observed = dict(draft.evidence["weight_kg"]["observations"])
    assert observed["body"] == "1674" and observed["spec.xlsx"] == str(ROW[columns[0]])


@pytest.mark.parametrize("columns", HEADER_LAST, ids=lambda c: "/".join(x[:2] for x in c))
def test_same_line_reads_the_total(same_line, columns):
    same_line(True)
    _, draft = validate(
        kessler_mail(), Rfq(**KESSLER_FACTS), {"spec.xlsx": serialize(packing_rows(columns))}
    )
    assert {(i["field"], i["code"]) for i in draft.issues} == {
        ("dangerous_goods", "documents_required")
    }
    assert draft.evidence["weight_kg"]["observations"] == [
        ("body", "1674"),
        ("spec.xlsx", "1674"),
    ]


def test_pdf_header_row_without_totals_is_no_total(same_line):
    """pdfplumber layout of mail 2530 without the totals block: no figure, not a wrong one."""
    text = (
        "Pieces Length (cm) Height (cm) Width (cm) Gross weight (kg)\n4 120 130 60 855\n"
        "Volume (cbm)\n3.74"
    )
    same_line(False)
    assert numeric_facts(text) == {"weight_kg": Decimal(4), "volume_cbm": Decimal("3.74")}
    same_line(True)
    assert numeric_facts(text) == {}


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Total gross weight is 1234 kg. Total volume is 2.5 cbm.", (1234, "2.5")),
        ("Total gross weight of 1234 kg, total volume of 2.5 cbm", (1234, "2.5")),
        ("Total gross weight: 1234 kg\nTotal volume: 2.5 cbm", (1234, "2.5")),
        ("Total gross weight (kg): | 1234\nTotal volume (cbm): | 2.5", (1234, "2.5")),
        ("Total gross weight (kg)|: 1234\nTotal volume (cbm) | 2.5", (1234, "2.5")),
        ("Gross weight:\n1234 kg\nVolume:\n2.5 cbm", (1234, "2.5")),  # value on its own line
        ("with a gross weight\nof 1234 kg and a total volume\nof 2.5 cbm", (1234, "2.5")),
    ],
)
def test_same_line_keeps_every_label_form(same_line, text, want):
    for on in (False, True):
        same_line(on)
        facts = numeric_facts(text)
        assert (facts["weight_kg"], facts["volume_cbm"]) == (Decimal(want[0]), Decimal(want[1]))


@pytest.mark.parametrize(
    "text",
    [
        "Total gross weight (kg): | unknown\n840",
        "Net weight (kg): | 840",
        "Total gross weight (kg): | not 840",
        "gross weight is not 500 kg",
        "Gross weight (kg) | Width (cm)\n840 | 100",
    ],
)
def test_same_line_still_refuses_non_totals(same_line, text):
    same_line(True)
    assert "weight_kg" not in numeric_facts(text)
