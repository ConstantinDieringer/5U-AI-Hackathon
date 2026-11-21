"""Door-to-port requests without a ZIP (rounds 3627, 3930) and quoted old mails."""

import pytest

from app import db
from app.agents import rfq as agent
from app.models import Mail, Rfq
from app.pricing.pricing import resolve_origin
from app.pricing.reference import find_area, zone_for_area

BODY_3627 = (
    "Hello,\n\nCould you please quote your cheapest air freight option to SGN (Ho Chi Minh City,"
    " VN)? The goods will be ready for pickup from our warehouse on 2026-11-30. Please arrange"
    " the truck from our warehouse to the departure airport and include this in your all-in"
    " price in EUR.\n\nThe shipment consists of 11 pieces of precision instruments, with a gross"
    " weight of 572 kg and a total volume of 2.29 cbm.\n\nThank you in advance.\n\nBest greetings"
    " from Regensburg\nTobias Auer\nVersand / Shipping\nAuer Maschinenbau GmbH\n\n\n"
    "-----Original Message-----\nSent: 2026-10-25\nSubject: AMSTERDAM SCHIPHOL - HKG / 128 PCS"
    " 165 KGS\n\nDear team,\n\npls quote air freight ex AMSTERDAM SCHIPHOL to HKG:\n128 PCS /"
    " 165 KGS / 11.8 CBM, furniture samples\nFCA, cargo ready next week.\n\nThanks"
)
BODY_3930 = (
    "Hi,\n\nCould you quote the fastest air freight option to PVG (Shanghai, CN), including"
    " truck pickup from our warehouse to the airport?\n\nThe sports equipment will be ready for"
    " pickup on 2026-12-02: 11 pieces, 349 kg gross, 2.40 cbm.\n\nPlease send your all-in price"
    " in EUR.\n\nMira Petersen\nNordlicht Audio"
)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "t.db"))


def mail(mid, sender, subject, body):
    return Mail(
        id=mid,
        thread_root=mid,
        sender=sender,
        to="austrian-power@hackloop.mail",
        subject=subject,
        body=body,
        received_at=db.now(),
    )


def test_quoted_old_mail_is_cut_before_pickup_lookup():
    assert "AMSTERDAM" not in agent._clean(BODY_3627) and "Regensburg" in agent._clean(
        BODY_3627
    )


def test_area_lookup():
    assert find_area("Best greetings from Regensburg") == "Regensburg"
    assert (
        find_area("Lager in München") == "Munich"
        and zone_for_area("Munich South") == "81"
    )
    assert find_area("Kind regards, Ulm") is None


def test_3627_pickup_from_signature_city_gives_muc_and_truck_plus_air():
    db.ingest_mail(
        mail_id=3627,
        sender="auer-maschinenbau@hackloop.mail",
        recipient="austrian-power@hackloop.mail",
        subject="x",
        body=BODY_3627,
        category="rfq",
    )
    m = mail(3627, "auer-maschinenbau@hackloop.mail", "RE: FW: AMSTERDAM", BODY_3627)
    # what the LLM returned live: origin AMS from the quoted mail, no ZIP
    r = Rfq(origin="AMS", destination="SGN", weight_kg=572, volume_cbm=2.29, pieces=11)
    r = agent.locate_pickup(r, m, 3627)
    assert r.pickup_city == "Regensburg" and r.pickup_zone == "93" and r.door_to_port
    assert resolve_origin(r) == "MUC"
    truckers, _ = agent._bid_carriers(r, "MUC")
    assert truckers and all("93" in t.serves for t in truckers)
    # never email airlines for an origin outside our network
    assert (
        agent._bid_carriers(r.model_copy(update={"pickup_city": None}), "AMS")[1] == []
    )


def test_3930_pickup_from_customers_last_request():
    old = db.ingest_mail(
        mail_id=1967,
        sender="nordlicht-audio@hackloop.mail",
        recipient="austrian-power@hackloop.mail",
        subject="RFQ LOS",
        body="b",
        category="rfq",
    )
    db.set_request(old, {"destination": "LOS", "weight_kg": 900, "pickup_zip": "36627"})
    db.ingest_mail(
        mail_id=3930,
        sender="nordlicht-audio@hackloop.mail",
        recipient="austrian-power@hackloop.mail",
        subject="x",
        body=BODY_3930,
        category="rfq",
    )
    m = mail(
        3930, "nordlicht-audio@hackloop.mail", "Air freight quote to PVG", BODY_3930
    )
    r = agent.locate_pickup(
        Rfq(destination="PVG", weight_kg=349, volume_cbm=2.4), m, 3930
    )
    assert r.pickup_zip == "36627" and resolve_origin(r) == "FRA"


def test_self_delivery_stays_airport_to_airport():
    body = (
        "Please quote MUC to LAX. The cargo will be ready for pickup on 2026-11-29, and we"
        " will deliver it to Munich airport (MUC) ourselves. Greetings from Regensburg"
    )
    r = agent.locate_pickup(
        Rfq(origin="MUC", destination="LAX", weight_kg=10),
        mail(5, "x@y.example", "RFQ", body),
        5,
    )
    assert not r.door_to_port


def test_assumed_pickup_is_named_in_the_quote_before_the_signature():
    from app.mail.style import SIGNATURE

    r = Rfq(destination="SGN", weight_kg=874, pickup_zip="51447", pickup_assumed=True)
    out = agent.with_pickup_note(
        f"Hello,\n\nAir freight: 1.234 € all-in.\n\n{SIGNATURE}\n", r
    )
    assert "postal code 51447" in out and out.rstrip().endswith(SIGNATURE)
    de = r.model_copy(
        update={"language": "de", "pickup_zip": None, "pickup_city": "Regensburg"}
    )
    assert "Raum Regensburg" in agent.with_pickup_note(f"Hallo,\n\n{SIGNATURE}", de)


def test_last_pickup_falls_back_to_zip_in_earlier_mail_text():
    db.ingest_mail(
        mail_id=937,
        sender="rheintal-elektronik@hackloop.mail",
        recipient="austrian-power@hackloop.mail",
        subject="RFQ ORD",
        body="Please arrange trucking from our site in 51896 Bergisch. 1795 kg.",
        category="rfq",
    )
    assert (
        db.last_pickup("rheintal-elektronik@hackloop.mail", before_session=4639)
        == "51896"
    )
    assert (
        db.last_pickup("rheintal-elektronik@hackloop.mail", before_session=937) is None
    )


def test_5844_origin_from_text_when_llm_left_it_empty():
    body = (
        "Could you quote an all-in price in EUR for the cheapest air freight option to DEL"
        " (Delhi, IN)?\n\nWe'll deliver the shipment to FRA ourselves. 736 kg gross."
    )
    m = mail(
        5844, "lindqvist-medtech@hackloop.mail", "Air freight quote: FRA–DEL", body
    )
    r = agent.locate_origin(Rfq(destination="DEL", weight_kg=736), m, 5844)
    assert r.origin == "FRA"
    two = mail(1, "x@y.example", "FRA or MUC to DEL", "either works")
    assert (
        agent.locate_origin(Rfq(destination="DEL", weight_kg=1), two, 1).origin is None
    )


def test_origin_ignores_subject_of_quoted_old_request():
    body = ("Please quote cheapest to DEL, 300 kg, we deliver to the airport ourselves.\n\n"
            "-----Original Message-----\nSubject: HAM - HKG\n\nold request 120 kg")
    m = mail(7, "x@y.example", "RE: FW: HAM - HKG / 120 KGS", body)
    assert agent.locate_origin(Rfq(destination="DEL", weight_kg=300), m, 7).origin is None


def test_7237_self_delivery_is_not_a_pickup_request():
    from app.mail.rfq_validation import _NO_PICKUP, _PICKUP

    t = ("We'll deliver the textile samples to Cologne airport (CGN) ourselves, so there's no"
         " need to arrange pickup.")
    assert _PICKUP.search(t) and _NO_PICKUP.search(t)
    assert not _NO_PICKUP.search("Please arrange the truck from our warehouse to the airport.")
