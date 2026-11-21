"""Language boundaries and agent wiring; gateway answers are stubbed, never live sends."""

import json
import re
from datetime import UTC, date, datetime

import pytest

from app import db, hackloop, llm
from app.agents import booking, followup, rfq
from app.mail import language, multilingual
from app.mail.content import current_message
from app.mail.rfq_validation import validate
from app.mail.style import SIGNATURE, written_date
from app.models import Mail, RateOption, Rfq

SAMPLES = [
    (
        "fr",
        "Bonjour, pouvez-vous nous proposer le transport aérien de FRA vers MIA pour 100 kg et 2 cbm ? Merci beaucoup.",
    ),
    (
        "es",
        "Hola, por favor envíenos una oferta de transporte aéreo de FRA a MIA para 100 kg y 2 cbm. Muchas gracias.",
    ),
    (
        "it",
        "Buongiorno, potreste inviarci un preventivo per il trasporto aereo da FRA a MIA per 100 kg e 2 cbm? Grazie mille.",
    ),
    (
        "pt",
        "Olá, precisamos de uma cotação de transporte aéreo de FRA para MIA para 100 kg e 2 cbm. Muito obrigado.",
    ),
    (
        "nl",
        "Goedemorgen, kunt u ons een offerte sturen voor luchtvracht van FRA naar MIA voor 100 kg en 2 cbm? Alvast bedankt.",
    ),
    (
        "pl",
        "Dzień dobry, prosimy o wycenę transportu lotniczego z FRA do MIA dla 100 kg i 2 cbm. Bardzo dziękujemy.",
    ),
    (
        "tr",
        "Merhaba, lütfen FRA ile MIA arasında 100 kg ve 2 cbm için hava kargo fiyat teklifi gönderir misiniz? Teşekkür ederiz.",
    ),
    (
        "uk",
        "Добрий день, надішліть нам пропозицію авіаперевезення з FRA до MIA для 100 кг та 2 кубометрів. Дуже дякуємо.",
    ),
    (
        "ru",
        "Здравствуйте, пожалуйста, пришлите предложение авиаперевозки из FRA в MIA для 100 кг и 2 кубометров. Большое спасибо.",
    ),
    (
        "ar",
        "مرحباً، نرجو إرسال عرض أسعار للشحن الجوي من FRA إلى MIA لوزن 100 كجم وحجم 2 متر مكعب. شكراً جزيلاً.",
    ),
    (
        "hi",
        "नमस्ते, कृपया FRA से MIA तक 100 किलो और 2 घन मीटर के लिए हवाई माल ढुलाई की कीमत भेजें। बहुत धन्यवाद।",
    ),
    (
        "zh",
        "您好，请为我们提供从 FRA 到 MIA 的空运报价，货物重 100 公斤，体积为 2 立方米。非常感谢您的帮助。",
    ),
    (
        "ja",
        "こんにちは、FRA から MIA までの航空貨物の見積もりをお願いします。重量は 100 キロ、容積は 2 立方メートルです。よろしくお願いします。",
    ),
    (
        "ko",
        "안녕하세요, FRA 에서 MIA 까지 항공 화물 운송 견적을 보내 주세요. 무게는 100 킬로그램이고 부피는 2 세제곱미터입니다. 감사합니다.",
    ),
]


@pytest.fixture(autouse=True)
def isolated(tmp_path):
    db.init_db(str(tmp_path / "multilingual.db"))


def mail(body: str, mid: int = 100, subject: str = "") -> Mail:
    value = Mail(
        mid,
        100,
        "anna@example.test",
        "team@example.test",
        subject,
        body,
        datetime.now(UTC).isoformat(),
    )
    db.ingest_mail(
        mail_id=mid,
        sender=value.sender,
        recipient=value.to,
        subject=subject,
        body=body,
        category="rfq" if mid == 100 else "customer",
        received_at=value.received_at,
        in_reply_to=100 if mid != 100 else None,
    )
    return value


@pytest.mark.parametrize(("lang", "body"), SAMPLES)
def test_detects_languages_and_scripts(lang, body):
    assert language.detect(body, default="und") == lang


@pytest.mark.parametrize(("lang", "body"), SAMPLES)
def test_foreign_request_reaches_existing_validation_with_original_unchanged(
    monkeypatch, lang, body
):
    original = mail(body)
    calls = []

    def complete(purpose, messages, sid, **kwargs):
        calls.append(purpose)
        data = json.loads(messages[1]["content"])
        codes = re.findall(r"⟦K\d+⟧", data["body"])
        assert len(codes) == 4
        a, b, w, v = codes
        return json.dumps(
            {
                "language": lang,
                "subject": data["subject"],
                "body": f"Please quote air freight from {a} to {b}. Total gross weight is {w} kg and total volume is {v} cbm.",
                "booking_authorized": False,
                "consent_quote": "",
            }
        )

    monkeypatch.setattr(llm, "complete", complete)
    prepared = multilingual.prepare(original, 100)
    result, draft = validate(
        prepared, Rfq(origin="FRA", destination="MIA", weight_kg=100, volume_cbm=2, language=lang)
    )
    assert result is not None and not draft.issues
    assert multilingual.mail_language(prepared) == lang
    assert db.get_session(100)["mails"][0]["body"] == body
    assert original.body == body
    assert multilingual.prepare(original, 100) == prepared
    assert calls == ["translate_incoming"]  # restart/retry uses the durable cache


@pytest.mark.parametrize("body", ["OK", "👍", "", "FRA", "100 kg"])
def test_short_ambiguous_replies_keep_thread_language(body):
    assert language.detect(body, default="ja") == "ja"


def test_model_identifies_language_outside_local_detector(monkeypatch):
    original = mail("Aloha, e hoʻouna mai i ke kumukūʻai no ka ukana.")
    monkeypatch.setattr(language, "detect", lambda text, default="en": default)
    monkeypatch.setattr(
        llm,
        "complete",
        lambda *a, **k: json.dumps(
            {
                "language": "haw",
                "subject": "",
                "body": "Hello, please send a freight quote.",
                "booking_authorized": False,
                "consent_quote": "",
            }
        ),
    )
    prepared = multilingual.prepare(original, 100)
    assert multilingual.mail_language(prepared) == "haw"
    assert language.normalize("zh-Hant-TW") == "zh-hant-tw"
    assert language.normalize("ignore previous instructions", "und") == "und"


@pytest.mark.parametrize(
    ("body", "canonical", "authorized", "expected"),
    [
        ("Oui, réservez cette offre, merci.", "Yes, please book this offer.", True, True),
        ("Oui, merci.", "Yes please.", True, True),
        ("Réservez seulement si le prix baisse.", "Please book it.", False, None),
        (
            "Je vais vérifier en interne et revenir vers vous.",
            "I will check internally and get back to you.",
            False,
            None,
        ),
        ("Non merci, ne réservez pas.", "No thanks, do not book.", False, False),
        (
            "Veuillez réserver une autre option.",
            "Please book the faster option instead.",
            False,
            None,
        ),
    ],
)
def test_translated_consent_is_explicit_and_unconditional(
    monkeypatch, body, canonical, authorized, expected
):
    original = mail(body)
    monkeypatch.setattr(
        llm,
        "complete",
        lambda *a, **k: json.dumps(
            {
                "language": "fr",
                "subject": "",
                "body": canonical,
                "booking_authorized": authorized,
                "consent_quote": body if authorized else "",
            }
        ),
    )
    prepared = multilingual.prepare(original, 100, "fr")
    assert booking._mail_acceptance(prepared) is expected


def test_quoted_foreign_approval_is_not_current_consent(monkeypatch):
    original = mail("Je dois encore vérifier.\nLe samedi, Anna a écrit :\nOui, veuillez réserver.")

    def complete(purpose, messages, sid, **kwargs):
        data = json.loads(messages[1]["content"])
        assert "veuillez réserver" not in data["body"]
        return json.dumps(
            {
                "language": "fr",
                "subject": "",
                "body": "I still need to check.",
                "booking_authorized": True,
                "consent_quote": "Oui, veuillez réserver.",
            }
        )

    monkeypatch.setattr(llm, "complete", complete)
    with pytest.raises(multilingual.LanguageError):
        multilingual.prepare(original, 100)


def french_translation(purpose, messages, sid, **kwargs):
    assert purpose == "translate_customer_email"
    data = json.loads(messages[1]["content"])
    assert data["language"] == "fr"
    assert "best friend" in messages[0]["content"]
    body = data["body"].replace("Hello", "Bonjour").replace("Your shipment", "Votre envoi")
    body = body.replace("is booked", "est réservé").replace("The total is", "Le montant total est")
    body = body.replace("Your booking reference is", "Votre référence de réservation est")
    body = body.replace("from", "de").replace(" to ", " vers ").replace(" with ", " avec ")
    body = body.replace("The earliest departure is", "Le départ le plus tôt possible est le")
    return json.dumps(
        {"language": "fr", "subject": data["subject"].replace(" to ", " vers "), "body": body},
        ensure_ascii=False,
    )


def test_every_customer_send_localizes_templates_subject_and_full_dates(monkeypatch):
    original = mail(SAMPLES[0][1])
    db.set_request(100, {"language": "fr"})
    monkeypatch.setattr(llm, "complete", french_translation)
    wire = []
    monkeypatch.setattr(hackloop, "send", lambda **kw: wire.append(kw) or {"id": 900})
    body = (
        "Hello,\n\nYour shipment with Test Air from FRA to MIA is booked.\n\n"
        "The total is 911,68 €. Your booking reference is BK-TEST.\n\n"
        f"The earliest departure is 29 November 2026.\n\n{SIGNATURE}\n"
    )
    sent = hackloop.send_customer(
        original.sender,
        "FRA to MIA",
        body,
        in_reply_to=100,
        session_id=100,
        protected_terms=("Test Air",),
        review=False,
    )
    assert len(wire) == 1
    assert "Bonjour" in sent["body"] and "29 novembre 2026" in sent["body"]
    assert "911,68 €" in sent["body"] and "BK-TEST" in sent["body"]
    assert "Test Air" in sent["body"] and sent["body"].rstrip().endswith(SIGNATURE)
    assert sent["subject"] == "FRA vers MIA" and wire[0]["in_reply_to"] == 100


@pytest.mark.parametrize(
    "fault", ["missing_reference", "invented_price", "wrong_language", "gateway"]
)
def test_invalid_translation_never_reaches_transport_or_quote_send_claim(monkeypatch, fault):
    original = mail(SAMPLES[0][1])
    claims = []
    wire = []

    def complete(*args, **kwargs):
        if fault == "gateway":
            raise llm.LlmBudgetError("budget exhausted")
        result = json.loads(french_translation(*args, **kwargs))
        if fault == "missing_reference":
            result["body"] = re.sub(r"⟦K\d+⟧", "", result["body"], count=1)
        elif fault == "invented_price":
            result["body"] += " 50 €"
        else:
            result["language"] = "es"
        return json.dumps(result)

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(hackloop, "send", lambda **kw: wire.append(kw) or {"id": 900})
    with pytest.raises(hackloop.CustomerSendBlocked):
        hackloop.send_customer(
            original.sender,
            "FRA to MIA",
            f"Hello,\n\nThe total is 100 €.\n\n{SIGNATURE}",
            in_reply_to=100,
            session_id=100,
            review=False,
            before_transport=lambda: claims.append(True),
        )
    assert not wire and not claims
    assert any(e["kind"] == "language_error" for e in db.get_session(100)["events"])


def test_pause_during_translation_is_rechecked(monkeypatch):
    original = mail(SAMPLES[0][1])

    def complete(*args, **kwargs):
        result = french_translation(*args, **kwargs)
        db.set_paused(100, True)
        return result

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(hackloop, "send", lambda **kw: pytest.fail("paused mail sent"))
    with pytest.raises(hackloop.CustomerSendBlocked, match="paused"):
        hackloop.send_customer(
            original.sender,
            "FRA to MIA",
            f"Hello,\n\nThe total is 100 €.\n\n{SIGNATURE}",
            in_reply_to=100,
            session_id=100,
            review=False,
        )


def test_multilingual_compose_receives_target_language_and_profile(monkeypatch):
    mail(SAMPLES[0][1])
    request = Rfq(origin="FRA", destination="MIA", weight_kg=100, volume_cbm=2, language="fr")
    option = RateOption("Test Air", "FRA", "MIA", 2, 10000, "EUR", 10000, "sheet")

    def complete(purpose, messages, sid, **kwargs):
        assert purpose == "compose_quote"
        facts = json.loads(messages[1]["content"])["verified_facts"]
        assert facts["language"] == "fr"
        assert "best friend" in messages[0]["content"]
        return json.dumps(
            {
                "body": "Bonjour Anna,\n\nTest Air vole de FRA vers MIA pour 100 €.\n\nYour austrian Power Team",
                "answered_questions": [],
                "unavailable_questions": [],
                "addressed_constraints": [],
                "unavailable_constraints": [],
                "referenced_mail_ids": [],
            }
        )

    monkeypatch.setattr(llm, "complete", complete)
    assert "Bonjour" in rfq.compose_quote_text(request, option, 10000, None, 100)


@pytest.mark.parametrize(
    ("lang", "month"),
    [
        ("fr", "novembre"),
        ("es", "noviembre"),
        ("tr", "Kasım"),
        ("ru", "ноября"),
        ("ar", "نوفمبر"),
        ("ja", "11月"),
        ("zh", "11月"),
    ],
)
def test_dates_are_localized(lang, month):
    assert month in written_date(date(2026, 11, 29), lang)


def test_document_translations_are_batched_and_keep_dangerous_goods(monkeypatch):
    calls = []
    texts = {
        "fr.txt": "Marchandises dangereuses UN1133. Poids brut 100 kg.",
        "zh.txt": "货物体积为 2 立方米。请提供安全数据表。",
    }

    def complete(purpose, messages, sid, **kwargs):
        calls.append(purpose)
        data = json.loads(messages[1]["content"])
        codes = re.findall(r"⟦K\d+⟧", data["doc0"])
        assert len(codes) == 3
        return json.dumps(
            {
                "doc0": f"Dangerous goods {codes[0]}{codes[1]}. Gross weight {codes[2]} kg.",
                "doc1": data["doc1"]
                .replace("货物体积为", "Total volume")
                .replace(
                    "立方米。请提供安全数据表。", "cbm. Please provide the safety data sheet."
                ),
            }
        )

    monkeypatch.setattr(llm, "complete", complete)
    translated = multilingual.english_sources(texts, 100)
    assert "UN1133" in translated["fr.txt"] and "100 kg" in translated["fr.txt"]
    assert "safety data sheet" in translated["zh.txt"]
    assert multilingual.english_sources(texts, 100, local_only=True) == translated
    assert calls == ["translate_documents"]


def test_rfq_agent_normalizes_before_extraction_and_keeps_reply_language(monkeypatch):
    original = mail(SAMPLES[0][1])
    reached = []

    def complete(purpose, messages, sid, **kwargs):
        data = json.loads(messages[1]["content"])
        a, b, w, v = re.findall(r"⟦K\d+⟧", data["body"])
        return json.dumps(
            {
                "language": "fr",
                "subject": "",
                "body": f"Please quote from {a} to {b}. Gross weight {w} kg. Total volume {v} cbm.",
                "booking_authorized": False,
                "consent_quote": "",
            }
        )

    def extract(prepared, sid):
        assert prepared.body.startswith("Please quote from FRA to MIA")
        return Rfq(origin="FRA", destination="MIA", weight_kg=100, volume_cbm=2, language="en")

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(rfq, "extract_rfq", extract)
    monkeypatch.setattr(rfq, "_price_and_quote", lambda m, sid, r, *args: reached.append(r))
    rfq._handle(original, 100)
    assert len(reached) == 1 and reached[0].language == "fr"
    assert db.get_session(100)["request"]["language"] == "fr"


def test_language_changes_follow_latest_meaningful_reply_and_acknowledgements():
    first = mail("Could you please send us a quote for our shipment?")
    db.set_request(100, {"language": "en"})
    latest = mail(SAMPLES[9][1], mid=101)
    assert multilingual.reply_language(first.sender, 100, None, "en") == "ar"
    assert multilingual.reply_language(first.sender, 100, 101, "en") == "ar"
    db.set_request(100, {"language": "ar"})
    mail("OK", mid=102)
    assert multilingual.reply_language(latest.sender, 100, None, "en") == "ar"


def test_foreign_decline_stops_followups(monkeypatch):
    mail(SAMPLES[0][1])
    db.set_request(100, {"language": "fr"})
    db.record_quote(
        100,
        total_minor=10000,
        currency="EUR",
        airline="Test Air",
        origin="FRA",
        destination="MIA",
        transit_days=2,
        source="sheet",
        mail_id=200,
    )
    reply = mail("Non merci, c'est trop cher, nous refusons votre offre.", mid=201)
    monkeypatch.setattr(
        llm,
        "complete",
        lambda *a, **k: json.dumps(
            {
                "language": "fr",
                "subject": "",
                "body": "No thanks, too expensive, we decline your offer.",
                "booking_authorized": False,
                "consent_quote": "",
            }
        ),
    )
    multilingual.prepare(reply, 100)
    assert followup.due(db.get_session(100)) is None


def test_translated_dangerous_goods_still_require_documents(monkeypatch):
    original = mail(
        "Bonjour, veuillez transporter nos marchandises dangereuses de FRA vers MIA. Poids brut 100 kg et volume total 2 cbm."
    )

    def complete(purpose, messages, sid, **kwargs):
        data = json.loads(messages[1]["content"])
        a, b, w, v = re.findall(r"⟦K\d+⟧", data["body"])
        return json.dumps(
            {
                "language": "fr",
                "subject": "",
                "body": f"Please ship from {a} to {b}. Gross weight {w} kg. Total volume {v} cbm. Dangerous goods: yes.",
                "booking_authorized": False,
                "consent_quote": "",
            }
        )

    monkeypatch.setattr(llm, "complete", complete)
    prepared = multilingual.prepare(original, 100)
    _, draft = validate(prepared, Rfq(origin="FRA", destination="MIA", weight_kg=100, volume_cbm=2))
    assert any(
        i["field"] == "dangerous_goods" and i["code"] == "documents_required" for i in draft.issues
    )


def test_template_localization_cache_binds_the_actual_protected_facts(monkeypatch):
    calls = []

    def complete(*args, **kwargs):
        calls.append(True)
        return french_translation(*args, **kwargs)

    monkeypatch.setattr(llm, "complete", complete)
    a = multilingual.localize(
        "FRA to MIA", f"Hello,\n\nThe total is 100 €.\n\n{SIGNATURE}", "fr", 100
    )
    b = multilingual.localize(
        "FRA to MIA", f"Hello,\n\nThe total is 200 €.\n\n{SIGNATURE}", "fr", 100
    )
    assert "100 €" in a[1] and "200 €" in b[1]
    assert len(calls) == 2


def test_false_language_label_cannot_allow_english_output(monkeypatch):
    monkeypatch.setattr(
        llm,
        "complete",
        lambda purpose, messages, sid, **kw: json.dumps(
            {**json.loads(messages[1]["content"]), "language": "fr"}
        ),
    )
    with pytest.raises(multilingual.LanguageError, match="does not match"):
        multilingual.localize(
            "",
            f"Hello, we can offer the shipment for you. Please confirm if you would like us to proceed.\n\n{SIGNATURE}",
            "fr",
            100,
        )


@pytest.mark.parametrize(
    ("lang", "native"),
    [
        (
            "fr",
            "Bonjour, votre envoi est réservé et voici le montant total convenu pour le transport.",
        ),
        (
            "es",
            "Hola, tu envío está reservado y este es el precio total acordado para el transporte.",
        ),
        (
            "it",
            "Ciao, la tua spedizione è prenotata e questo è il prezzo totale concordato per il trasporto.",
        ),
        (
            "pt",
            "Olá, a sua remessa está reservada e este é o valor total combinado para o transporte.",
        ),
        (
            "nl",
            "Hallo, je zending is geboekt en dit is het afgesproken totaalbedrag voor het vervoer.",
        ),
        (
            "pl",
            "Cześć, Twoja przesyłka została zarezerwowana i podajemy uzgodnioną całkowitą cenę transportu.",
        ),
        (
            "tr",
            "Merhaba, gönderiniz için rezervasyon yapıldı ve taşıma için anlaşılan toplam tutar aşağıdadır.",
        ),
        (
            "uk",
            "Вітаю, ваше відправлення заброньовано, нижче вказана узгоджена загальна вартість перевезення.",
        ),
        (
            "ru",
            "Здравствуйте, ваша отправка забронирована, ниже указана согласованная полная стоимость перевозки.",
        ),
        ("ar", "مرحباً، تم حجز شحنتك ويسعدنا تأكيد السعر الإجمالي المتفق عليه لعملية النقل."),
        ("hi", "नमस्ते, आपके माल की बुकिंग हो गई है और परिवहन की सहमत कुल कीमत नीचे दी गई है।"),
        (
            "zh",
            "您好，您的货物已经预订好了，以下是我们约定的全部运输费用。如果您有任何问题，请随时联系我们。",
        ),
        (
            "ja",
            "こんにちは、お荷物の予約が完了しました。以下が合意済みの輸送費用の総額です。ご不明な点がございましたら、お気軽にご連絡ください。",
        ),
        (
            "ko",
            "안녕하세요, 화물 예약이 완료되었습니다. 아래는 운송에 대해 합의한 총 금액입니다. 궁금한 점이 있으면 언제든지 연락해 주세요.",
        ),
    ],
)
def test_native_script_replies_reach_transport_with_facts_unchanged(monkeypatch, lang, native):
    def complete(purpose, messages, sid, **kwargs):
        data = json.loads(messages[1]["content"])
        markers = re.findall(r"⟦K\d+⟧", data["body"])
        return json.dumps(
            {
                "language": lang,
                "subject": data["subject"],
                "body": native + "\n\n" + " ".join(markers[:-1]) + "\n\n" + markers[-1],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(llm, "complete", complete)
    wire = []
    monkeypatch.setattr(hackloop, "send", lambda **kw: wire.append(kw) or {"id": 900})
    body = f"Hello, your shipment is booked. The total is 911,68 €. The reference is BK-TEST.\n\n{SIGNATURE}"
    sent = hackloop.send_customer(
        "anna@example.test", "FRA MIA", body, language_code=lang, review=False
    )
    assert len(wire) == 1 and native in sent["body"]
    assert "911,68 €" in sent["body"] and "BK-TEST" in sent["body"]
    assert sent["body"].rstrip().endswith(SIGNATURE)


@pytest.mark.parametrize(
    ("weight", "volume"), [("١٠٠", "٢٫٥"), ("１００", "２．５"), ("१००", "२.५")]
)
def test_native_digits_and_decimal_separators_reach_numeric_validation(monkeypatch, weight, volume):
    original = mail(SAMPLES[9][1].replace("100", weight).replace("2", volume))

    def complete(purpose, messages, sid, **kwargs):
        data = json.loads(messages[1]["content"])
        assert data["protected_values"]["body"] == ["FRA", "MIA", "100", "2.5"]
        a, b, w, v = re.findall(r"⟦K\d+⟧", data["body"])
        return json.dumps(
            {
                "language": "ar",
                "subject": "",
                "body": f"Please quote from {a} to {b}. Gross weight {w} kg. Total volume {v} cbm.",
                "booking_authorized": False,
                "consent_quote": "",
            }
        )

    monkeypatch.setattr(llm, "complete", complete)
    prepared = multilingual.prepare(original, 100)
    result, draft = validate(
        prepared, Rfq(origin="FRA", destination="MIA", weight_kg=100, volume_cbm=2.5)
    )
    assert result is not None and not draft.issues
    assert weight in db.get_session(100)["mails"][0]["body"]


def test_regional_language_tags_keep_german_templates():
    request = Rfq(origin="FRA", destination="MIA", weight_kg=100, language="de-DE")
    option = RateOption("Test Air", "FRA", "MIA", 2, 10000, "EUR", 10000, "sheet")
    body = rfq.quote_text(request, option, 10000, None, 1)
    assert "Gesamtpreis" in body and "100 €" in body


def test_manual_native_reply_does_not_get_automatically_rewritten(monkeypatch):
    original = mail(SAMPLES[0][1])
    monkeypatch.setattr(llm, "complete", lambda *a, **k: pytest.fail("manual mail rewritten"))
    wire = []
    monkeypatch.setattr(hackloop, "send", lambda **kw: wire.append(kw) or {"id": 900})
    body = f"Bonjour Anna,\n\nMerci beaucoup pour votre message.\n\n{SIGNATURE}\n"
    sent = hackloop.send_customer(
        original.sender,
        "Votre envoi",
        body,
        session_id=100,
        in_reply_to=100,
        allow_manual=True,
        review=False,
    )
    assert sent["body"] == body and len(wire) == 1


def test_long_quoted_history_does_not_block_a_short_current_reply(monkeypatch):
    original = mail("Non merci, nous refusons votre offre.\n" + "> Please book it.\n" * 2500)
    monkeypatch.setattr(
        llm,
        "complete",
        lambda *a, **k: json.dumps(
            {
                "language": "fr",
                "subject": "",
                "body": "No thanks, we decline your offer.",
                "booking_authorized": False,
                "consent_quote": "",
            }
        ),
    )
    prepared = multilingual.prepare(original, 100)
    assert booking._mail_acceptance(prepared) is False


@pytest.mark.parametrize("lang", ["fr", "es", "ru", "ar", "zh", "ja"])
def test_subject_and_localized_history_never_import_old_approval(lang):
    headers = {
        "fr": "Le samedi, Anna a écrit :",
        "es": "El sábado, Anna escribió:",
        "ru": "Анна писала:",
        "ar": "من: Anna",
        "zh": "在昨天 Anna 写道：",
        "ja": "Anna が書きました：",
    }
    current = current_message("Need to check.\n" + headers[lang] + "\nPlease book it.")
    assert current.text == "Need to check."
