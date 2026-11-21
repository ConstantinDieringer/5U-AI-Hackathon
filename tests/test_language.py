"""Language of a mail from function words, never from names or places."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.mail.language import detect, detect_or_none

# The real RFQs the LLM tagged "de" although they are English (mails 421, 1223, 1460).
SONNBERG = (
    "Dear Sir or Madam,\n\nCould you please quote the fastest air freight option to JFK "
    "(New York, US)? The shipment will be ready for pickup at our site in 61018 (Bad Homburg), "
    "Germany, on 2026-11-25. Please arrange trucking from our site to the departure airport."
    "\n\nKind regards\nAnna Sonnberg\nHead of Purchasing and Logistics\n"
    "Sonnberg Kaffeeroesterei GmbH"
)


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        (SONNBERG, "en"),
        (
            "Thank you for your offer. That works for us, please go ahead and book it.\n\n"
            "Cheers,\nKlaus Weidner\nWeidner Spielwaren GmbH, Nuremberg",
            "en",
        ),
        (
            "Sehr geehrte Damen und Herren,\n\nbitte senden Sie uns ein Angebot für die "
            "Luftfracht von 500 kg nach JFK. Die Sendung ist ab Montag abholbereit.\n\n"
            "Viele Grüße\nMax",
            "de",
        ),
        (
            "Bonjour,\n\npouvez-vous nous envoyer une offre pour le fret aérien ? Merci.",
            "fr",
        ),
        ("Passt, bitte buchen.", "de"),
        ("", "en"),
    ],
)
def test_detect(text: str, lang: str) -> None:
    assert detect(text) == lang


def test_quoted_text_is_ignored() -> None:
    assert (
        detect("Thanks, please book it.\n\n> Sehr geehrte Damen und Herren, bitte ...")
        == "en"
    )


# Real RFQ 20150 (Chinese body, English sign-off) plus one mail per added language.
CHINESE = (
    "您好，\n\n希望您这周过得顺利！想请您帮忙看看一票发往 LIM（Lima, PE）的空运。"
    "货物是汽车传感器，共 11 件，毛重 704 kg，体积 3.08 cbm。\n\n"
    "Thanks a lot and have a lovely day!\nPetra Hofmann\nExport Desk, Hofmann Textilwerk KG"
)


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        (CHINESE, "zh"),
        ("こんにちは、MUCからJFKまでの航空輸送の見積もりをお願いします。", "ja"),
        ("안녕하세요, MUC에서 JFK까지 500 kg 항공 운송 견적을 부탁드립니다.", "ko"),
        ("Здравствуйте, пришлите, пожалуйста, предложение на 500 кг MUC JFK", "ru"),
        ("Добрий день, надішліть, будь ласка, пропозицію на 500 кг до JFK, дякуємо", "uk"),
        ("Καλησπέρα, θα θέλαμε προσφορά για 500 kg προς JFK. Ευχαριστώ", "el"),
        ("مرحبا، نرجو إرسال عرض سعر لشحنة 500 كغ إلى JFK. شكرا", "ar"),
        ("Hola, ¿podría enviarnos una cotización para 500 kg a JFK? Gracias, saludos", "es"),
        ("Olá, vocês podem enviar uma cotação para 500 kg? Obrigado", "pt"),
        ("Goedendag, graag een offerte voor 500 kg naar JFK. Met vriendelijke groeten", "nl"),
        ("Dzień dobry, proszę o wycenę przesyłki 500 kg do JFK. Pozdrawiam", "pl"),
        ("Hej, kan ni skicka en offert för 500 kg till JFK? Tack och vänliga hälsningar", "sv"),
        ("Dobrý den, prosím o nabídku na zásilku 500 kg. S pozdravem", "cs"),
        ("Merhaba, MUC JFK 500 kg için teklif rica ederiz, teşekkür ederiz", "tr"),
        ("Bună ziua, vă rugăm să ne trimiteți o ofertă pentru 500 kg. Mulțumim", "ro"),
    ],
)
def test_more_languages(text: str, lang: str) -> None:
    assert detect(text) == lang


def test_hint_only_fills_gaps() -> None:
    assert detect("ok", hint="fr") == "fr"  # no word decides: the extraction's guess
    assert detect(SONNBERG, hint="de") == "en"  # clear words beat the LLM guess
    assert detect_or_none("ok") is None


def test_stored_rfqs_have_a_language() -> None:
    """Every stored RFQ gets a decided language (5U now sends en, de, fr and zh)."""
    path = Path("data/forwarder.db")
    if not path.exists():
        pytest.skip("no local DB")
    c = sqlite3.connect(path)
    bodies = [
        r[0]
        for r in c.execute(
            "SELECT m.body FROM sessions s JOIN mails m ON m.id = s.id WHERE s.kind = 'rfq'"
        )
    ]
    assert bodies and all(detect_or_none(b) for b in bodies)
