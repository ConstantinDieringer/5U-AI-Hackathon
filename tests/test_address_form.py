"""German customer mails keep one address form per thread (du by default, Sie when the
customer writes with Sie). Every German template literal must convert cleanly both ways."""

import ast
import re
from pathlib import Path

import pytest

from app import db
from app.mail import address_form

APP = Path(__file__).resolve().parents[1] / "app"
# German customer prose: a capitalized du/Sie marker plus typical German words.
_GERMAN = re.compile(r"\b(?:Sendung|Anfrage|Angebot|Buchung|antworten|Mail|Preis|bitte|einfach)\b")
_ADDRESS = re.compile(r"\b(?:Sie|Ihnen|Ihre?[nmrs]?|du|dir|dich|deine?[nmrs]?)\b")
_SKIP = {"screening.py", "language.py", "address_form.py", "pdf_request.py", "ai_context.py"}


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "X" for v in node.values)
    return None


def _german_templates() -> list[tuple[str, str]]:
    found = []
    for path in APP.rglob("*.py"):
        if path.name in _SKIP:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            text = _literal(node)
            if text and _GERMAN.search(text) and _ADDRESS.search(text) and " " in text:
                if "prompt" in text.lower() or re.search(r"\(\?|\\b", text):
                    continue  # regexes and prompts are not customer copy
                found.append((f"{path.name}:{node.lineno}", text))
    return found


TEMPLATES = _german_templates()


def test_templates_were_found():
    assert len(TEMPLATES) >= 20


@pytest.mark.core
@pytest.mark.parametrize(("where", "text"), TEMPLATES, ids=[w for w, _ in TEMPLATES])
@pytest.mark.parametrize("target", ["du", "Sie"])
def test_every_german_template_converts_to_one_form(where, text, target):
    converted = address_form.convert(text, target)
    assert address_form.mixed(converted, target) == [], (where, converted)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Hallo, können Sie mir ein Angebot nach BKK schicken?", "Sie"),
        ("Sehr geehrte Damen und Herren, anbei unsere Anfrage.", "Sie"),
        ("Hey, kannst du mir ein Angebot nach BKK schicken?", "du"),
        # counterexample: our own quoted Sie mail below the customer's du line does not count
        (
            (
                "Passt, buch das bitte für mich, danke dir!\n\n-----Original Message-----\n"
                "Wenn das für Sie passt, antworten Sie einfach auf diese Mail."
            ),
            "du",
        ),
        ("Bitte Angebot MUC nach JFK, 200 kg, 1 cbm.", None),
        # "Sie" opening a sentence about the cargo is no address
        ("Die Ware ist fertig. Sie steht in Augsburg bereit.", None),
    ],
)
def test_customer_form_reads_only_the_customers_own_words(body, expected):
    assert address_form.customer_form(body) == expected


def test_conversion_examples():
    sie = (
        "Guten Tag,\n\nvielen Dank für Ihre Anfrage, gerne machen wir Ihnen ein Angebot.\n\n"
        "Ihre Mail nennt keinen Abholort. Wenn das für Sie passt, antworten Sie einfach auf "
        "diese Mail und wir buchen sofort."
    )
    du = address_form.convert(sie, "du")
    assert du == (
        "Guten Tag,\n\nvielen Dank für deine Anfrage, gerne machen wir dir ein Angebot.\n\n"
        "Deine Mail nennt keinen Abholort. Wenn das für dich passt, antworte einfach auf "
        "diese Mail und wir buchen sofort."
    )
    assert address_form.convert(du, "Sie") == sie
    # the shipment pronoun is untouched and not flagged
    text = "Hier ist der Trackingstand zu deiner Sendung BK-1. Sie ist gebucht."
    assert address_form.mixed(address_form.convert(text, "du"), "du") == []


@pytest.fixture
def store(tmp_path):
    db.init_db(str(tmp_path / "address.db"))


def _ingest(mid, body, sid=None, direction_out=False):
    from datetime import UTC, datetime

    if direction_out:
        db.record_outgoing(
            mail_id=mid,
            recipient="kunde@hackloop.mail",
            subject="Re",
            body=body,
            category="quote",
            session_id=sid,
            in_reply_to=sid,
            references=[sid],
        )
        return
    db.ingest_mail(
        mail_id=mid,
        sender="kunde@hackloop.mail",
        recipient="austrian-power@hackloop.mail",
        subject="Anfrage",
        body=body,
        category="customer" if sid else "rfq",
        references=[sid] if sid else [],
        received_at=datetime.now(UTC).isoformat(),
    )


@pytest.mark.core
def test_form_is_fixed_for_the_whole_thread(store):
    _ingest(1, "Guten Tag, könnten Sie uns ein Angebot nach BKK machen? 200 kg, 1 cbm.")
    assert address_form.form(1) == "Sie"
    # a later du from the customer does not flip our replies mid-thread
    _ingest(3, "Super, danke dir, buch das bitte.", sid=1)
    assert address_form.form(1) == "Sie"


@pytest.mark.core
def test_du_is_the_default_without_a_signal(store):
    _ingest(1, "Angebot bitte MUC nach JFK, 200 kg, 1 cbm.")
    assert address_form.form(1) == "du"


def _wire(monkeypatch, customer_body, our_body):
    """Send one German mail through the real send gate; return the wire body."""
    from app import hackloop

    sent = []

    def send(to, subject, body, in_reply_to=None, references=None):
        sent.append({"id": 999, "subject": subject, "body": body})
        return sent[-1]

    monkeypatch.setattr(hackloop, "send", send)
    _ingest(1, customer_body)
    hackloop.send_customer(
        "kunde@hackloop.mail",
        "Ihre Sendung",
        our_body,
        in_reply_to=1,
        session_id=1,
        review=False,
        language_code="de",
    )
    return sent[0]


QUOTE = (
    "Hallo Anna,\n\nvielen Dank für Ihre Anfrage, gerne machen wir Ihnen ein Angebot.\n\n"
    "Wir fliegen mit Nova Atlas Cargo, die Laufzeit beträgt 3 Tage ab Abflug.\n\n"
    "Wenn das für Sie passt, antworten Sie einfach auf diese Mail und wir buchen sofort.\n\n"
    "Moritz\nYour austrian Power Team"
)
FOLLOWUP = (
    "Hallo Anna,\n\nkurze Nachfrage zu unserem Angebot. Wenn es für dich passt, sag mir "
    "einfach Bescheid und ich kümmere mich um die Buchung.\n\nMoritz\nYour austrian Power Team"
)


@pytest.mark.core
def test_du_customer_gets_the_sie_quote_template_in_du(store, monkeypatch):
    """Live mix: quote template in Sie, follow-ups and questions in du in the same thread."""
    wire = _wire(monkeypatch, "Hey, kannst du mir MUC nach JFK anbieten? 200 kg, 1 cbm.", QUOTE)
    body = wire["body"]
    assert "gerne machen wir dir ein Angebot" in body
    assert "Wenn das für dich passt, antworte einfach" in body
    assert address_form.mixed(body, "du") == [] and address_form.mixed(wire["subject"], "du") == []
    assert not any(e["kind"] == "address_form_mixed" for e in db.get_session(1)["events"])


@pytest.mark.core
def test_sie_customer_gets_the_du_follow_up_in_sie(store, monkeypatch):
    wire = _wire(
        monkeypatch, "Guten Tag, könnten Sie uns MUC nach JFK anbieten? 200 kg, 1 cbm.", FOLLOWUP
    )
    assert "Wenn es für Sie passt, sagen Sie mir einfach Bescheid" in wire["body"]
    assert address_form.mixed(wire["body"], "Sie") == []


@pytest.mark.core
def test_unknown_phrasing_is_reported_not_silently_sent(store, monkeypatch):
    """Counterexample: free text the table does not know stays as written and raises an alert."""
    _wire(
        monkeypatch,
        "Guten Tag, könnten Sie uns MUC nach JFK anbieten?",
        "Hallo Anna,\n\nschreib mir, wann du fertig bist.\n\nMoritz\nYour austrian Power Team",
    )
    events = db.get_session(1)["events"]
    assert any(e["kind"] == "address_form_mixed" and e["level"] == "error" for e in events)
