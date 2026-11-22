"""QUOTE_PDF_ON_REQUEST: which customer words ask for our offer as a PDF."""

from __future__ import annotations

import pytest

from app.mail.pdf_request import asks_for_pdf, session_asks_for_pdf

CUSTOMER = "buyer@example.com"


@pytest.mark.core
@pytest.mark.parametrize(
    "body",
    [
        "Could you send the offer as a PDF?",
        "Can you send me a PDF?",
        "Please send me a PDF of the quote.",
        "We need a PDF for our records.",
        "Reply with a PDF please",
        "Bitte senden Sie uns ein PDF.",
        # their own file and our PDF in one sentence
        "Anbei die Packliste, bitte Angebot als PDF.",
        "Attached is the packing list, please send us a PDF quote.",
        "Here is our packing list, could you send the quote as a PDF?",
        "Our SLI is attached as PDF; please reply with your quote as a PDF too.",
        "Hier ist die Packliste als Excel. Könnten Sie uns ein PDF vom Angebot schicken?",
    ],
)
def test_pdf_ask(body: str) -> None:
    assert asks_for_pdf(body, "Air freight quote")


@pytest.mark.core
@pytest.mark.parametrize(
    "body",
    [
        "Please find attached the packing list as a PDF.",
        "I am sending you the packing list as a PDF.",
        "Here is the commercial invoice as a PDF.",
        "Ich sende Ihnen die Packliste als PDF.",
        "See the attached PDF file for dimensions.",
        "The SLI is a PDF.",
        "We don't need a PDF.",
        "Details in packing_list.pdf.",
    ],
)
def test_not_a_pdf_ask(body: str) -> None:
    assert not asks_for_pdf(body, "Air freight quote")


def test_pdf_ask_in_a_later_customer_mail_counts() -> None:
    """The ask in a clarification answer still counts; a carrier's PDF mention does not."""
    session = {
        "customer": f"Buyer <{CUSTOMER}>",
        "mails": [
            {"direction": "in", "sender": CUSTOMER, "subject": "RFQ", "body": "Quote please."},
            {"direction": "in", "sender": "carrier@x.com", "subject": "Re", "body": "Send as PDF?"},
        ],
    }
    assert not session_asks_for_pdf(session)
    session["mails"].append(
        {
            "direction": "in",
            "sender": CUSTOMER,
            "subject": "Re: RFQ",
            "body": "It is 120 kg. Please send the quote as PDF.",
        }
    )
    assert session_asks_for_pdf(session)
