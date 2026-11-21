from app.mail.router import classify, route
from app.models import Category, Mail

US = "austrian-power@hackloop.mail"
ATT = [{"id": 1, "filename": "tariff.xlsx", "content_type": "application/xlsx", "size": 10}]


def mail(sender: str, to: str = US, attachments: list | None = None, mid: int = 10) -> Mail:
    return Mail(
        id=mid,
        thread_root=mid,
        sender=sender,
        to=to,
        subject="s",
        body="b",
        received_at="2026-11-21T15:00:00+00:00",
        attachments=attachments or [],
    )


def test_ratedesk_is_portal_reply() -> None:
    assert route(mail("ratedesk@hackloop.mail")) == Category.PORTAL_REPLY


def test_tracking_sender() -> None:
    assert route(mail("Tracking Desk <tracking@hackloop.mail>")) == Category.TRACKING


def test_reply_to_our_carrier_mail_is_bid_even_from_unknown_address() -> None:
    parent = mail(US, to="elbflug-air-freight@hackloop.mail", mid=5)
    cat, rule = classify(mail("new-address-elbflug@hackloop.mail"), parent)
    assert cat == Category.CARRIER_BID
    assert "Elbflug" in rule


def test_reply_to_our_carrier_mail_with_attachment_is_bid_not_tariff() -> None:
    parent = mail(US, to="kestrel-airways@hackloop.mail", mid=5)
    assert route(mail("kestrel-airways@hackloop.mail", attachments=ATT), parent) == (
        Category.CARRIER_BID
    )


def test_airline_with_attachment_is_tariff() -> None:
    assert route(mail("Kestrel Airways <kestrel-airways@hackloop.mail>", attachments=ATT)) == (
        Category.TARIFF
    )


def test_airline_without_attachment_is_bid() -> None:
    assert route(mail("kestrel-airways@hackloop.mail")) == Category.CARRIER_BID


def test_trucker_with_attachment_is_bid() -> None:
    assert route(mail("taunusfracht@hackloop.mail", attachments=ATT)) == Category.CARRIER_BID


def test_unknown_sender_new_thread_is_rfq() -> None:
    assert route(mail("Anna Weber <anna.weber@acme-industries.hackloop.mail>")) == Category.RFQ


def test_unknown_sender_reply_is_customer() -> None:
    m = mail("anna.weber@acme-industries.hackloop.mail", mid=12)
    m.references = [10, 11]
    assert route(m) == Category.CUSTOMER
    parent = mail(US, to="anna.weber@acme-industries.hackloop.mail", mid=11)
    assert route(mail("anna.weber@acme-industries.hackloop.mail", mid=13), parent) == (
        Category.CUSTOMER
    )


def test_customer_reply_in_customer_thread_stays_customer() -> None:
    parent = mail(US, to="anna@acme.hackloop.mail", mid=5)
    assert route(mail("anna@acme.hackloop.mail"), parent) == Category.CUSTOMER


def test_own_mail_is_ignored() -> None:
    assert route(mail(US)) == Category.IGNORE
