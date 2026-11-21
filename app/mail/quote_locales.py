"""Verified quote fallback copy for the other languages supported by mail detection."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from app.mail.style import SIGNATURE, has_template_artifacts, written_date
from app.models import RateOption, Rfq

if TYPE_CHECKING:
    from app.agents.bids import Offer

COPY = {
    "fr": {
        "hello": "Bonjour",
        "intro": "Merci pour ta demande, voici l'option que nous pouvons proposer.",
        "offer": "Nous proposons le fret aérien de {origin} à {destination} avec {airline} pour un total tout compris de {price}.",
        "transit": "Le délai de transport aérien est de {days} jours à compter du départ.",
        "schedule": "Le premier départ possible est le {departure} et la première arrivée possible est le {arrival}.",
        "pickup": "La collecte à {pickup} et le transport jusqu'à l'aéroport de départ sont inclus.",
        "slot": "Le créneau exact de collecte reste à confirmer.",
        "collection": "Le transporteur indique un préavis de collecte d'environ {hours} heures.",
        "road": "Le transporteur indique un trajet routier d'environ {hours} heures.",
        "lead": "Le transporteur indique un délai d'environ {hours} heures.",
        "valid": "Cette offre reste réservable jusqu'à {valid}.",
        "cutoff": "L'heure limite exacte de remise du fret à l'aéroport de départ n'est pas encore confirmée.",
        "next": "Si cela te convient, réponds à ce message et nous organiserons la réservation.",
    },
    "es": {
        "hello": "Hola",
        "intro": "Gracias por tu consulta, esta es la opción que podemos ofrecerte.",
        "offer": "Ofrecemos transporte aéreo de {origin} a {destination} con {airline} por un total con todo incluido de {price}.",
        "transit": "El tránsito aéreo es de {days} días desde la salida.",
        "schedule": "La primera salida posible es el {departure} y la primera llegada posible es el {arrival}.",
        "pickup": "La recogida en {pickup} y el transporte hasta el aeropuerto de salida están incluidos.",
        "slot": "La hora exacta de recogida está pendiente de confirmación.",
        "collection": "El transportista indica unas {hours} horas de antelación para la recogida.",
        "road": "El transportista indica un trayecto por carretera de unas {hours} horas.",
        "lead": "El transportista indica un plazo de unas {hours} horas.",
        "valid": "La oferta se puede reservar hasta {valid}.",
        "cutoff": "La hora límite exacta de entrega de la carga en el aeropuerto de salida aún no está confirmada.",
        "next": "Si te parece bien, responde a este mensaje y organizaremos la reserva.",
    },
    "it": {
        "hello": "Buongiorno",
        "intro": "Grazie per la richiesta, ecco l'opzione che possiamo offrirti.",
        "offer": "Proponiamo il trasporto aereo da {origin} a {destination} con {airline} per un totale tutto incluso di {price}.",
        "transit": "Il transito aereo è di {days} giorni dalla partenza.",
        "schedule": "La prima partenza possibile è il {departure} e il primo arrivo possibile è il {arrival}.",
        "pickup": "Sono inclusi il ritiro a {pickup} e il trasporto fino all'aeroporto di partenza.",
        "slot": "L'orario esatto del ritiro deve ancora essere confermato.",
        "collection": "Il vettore indica un preavviso di circa {hours} ore per il ritiro.",
        "road": "Il vettore indica un viaggio su strada di circa {hours} ore.",
        "lead": "Il vettore indica un tempo di circa {hours} ore.",
        "valid": "L'offerta è prenotabile fino a {valid}.",
        "cutoff": "L'orario limite esatto per la consegna della merce all'aeroporto di partenza non è ancora confermato.",
        "next": "Se ti va bene, rispondi a questo messaggio e organizzeremo la prenotazione.",
    },
    "nl": {
        "hello": "Hallo",
        "intro": "Bedankt voor je aanvraag, dit is de optie die we je kunnen aanbieden.",
        "offer": "We bieden luchtvracht van {origin} naar {destination} met {airline} voor een totaalprijs van {price}, alles inbegrepen.",
        "transit": "De luchttransit duurt {days} dagen vanaf vertrek.",
        "schedule": "Het vroegst mogelijke vertrek is op {departure} en de vroegst mogelijke aankomst is op {arrival}.",
        "pickup": "Het ophalen in {pickup} en het transport naar de luchthaven van vertrek zijn inbegrepen.",
        "slot": "Het exacte ophaalmoment moet nog worden bevestigd.",
        "collection": "De vervoerder geeft een aanlooptijd van ongeveer {hours} uur voor het ophalen aan.",
        "road": "De vervoerder geeft een rijtijd over de weg van ongeveer {hours} uur aan.",
        "lead": "De vervoerder geeft een doorlooptijd van ongeveer {hours} uur aan.",
        "valid": "Deze offerte kan tot {valid} worden geboekt.",
        "cutoff": "Het exacte tijdstip waarop de vracht uiterlijk op de luchthaven van vertrek moet zijn, is nog niet bevestigd.",
        "next": "Als dit goed voor je is, beantwoord dan dit bericht en wij regelen de boeking.",
    },
    "pl": {
        "hello": "Dzień dobry",
        "intro": "Dziękujemy za Twoje zapytanie, oto opcja, którą możemy Ci zaproponować.",
        "offer": "Oferujemy fracht lotniczy z {origin} do {destination} z przewoźnikiem {airline} za łączną cenę {price} ze wszystkimi kosztami.",
        "transit": "Czas transportu lotniczego wynosi {days} dni od wylotu.",
        "schedule": "Najwcześniejszy możliwy wylot to {departure}, a najwcześniejszy możliwy przylot to {arrival}.",
        "pickup": "Odbiór z lokalizacji {pickup} oraz transport do lotniska wylotu są wliczone w cenę.",
        "slot": "Dokładny termin odbioru wymaga jeszcze potwierdzenia.",
        "collection": "Przewoźnik podaje około {hours} godzin wyprzedzenia na odbiór.",
        "road": "Przewoźnik podaje czas przejazdu drogowego około {hours} godzin.",
        "lead": "Przewoźnik podaje czas realizacji około {hours} godzin.",
        "valid": "Tę ofertę można zarezerwować do {valid}.",
        "cutoff": "Dokładny termin dostarczenia ładunku na lotnisko wylotu nie jest jeszcze potwierdzony.",
        "next": "Jeśli Ci to odpowiada, odpowiedz na tę wiadomość, a my zorganizujemy rezerwację.",
    },
    "tr": {
        "hello": "Merhaba",
        "intro": "Talebin için teşekkürler, sana sunabileceğimiz seçenek şu.",
        "offer": "{origin} çıkışlı ve {destination} varışlı hava kargosunu {airline} ile her şey dahil toplam {price} fiyatla sunuyoruz.",
        "transit": "Hava taşıma süresi kalkıştan itibaren {days} gün.",
        "schedule": "Mümkün olan en erken kalkış {departure} ve mümkün olan en erken varış {arrival}.",
        "pickup": "{pickup} adresinden alım ve kalkış havalimanına taşıma fiyata dahildir.",
        "slot": "Kesin alım saati henüz teyit edilmedi.",
        "collection": "Taşıyıcı alım için yaklaşık {hours} saat önceden bildirim istiyor.",
        "road": "Taşıyıcı yaklaşık {hours} saatlik bir karayolu süresi belirtiyor.",
        "lead": "Taşıyıcı yaklaşık {hours} saatlik bir süre belirtiyor.",
        "valid": "Bu teklifi {valid} öncesine kadar rezerve edebilirsin.",
        "cutoff": "Kargonun kalkış havalimanına en geç teslim saati henüz teyit edilmedi.",
        "next": "Sana uygunsa bu mesajı yanıtla, rezervasyonu biz ayarlayalım.",
    },
}


def quote(
    request: Rfq,
    option: RateOption,
    price: str,
    truck: Offer | None,
    when: tuple[date, date] | None,
    valid: str | None,
) -> str:
    language = request.language.lower().split("-")[0]
    copy = COPY[language]
    name = request.customer_name
    if name and has_template_artifacts(name):
        name = None
    greeting = copy["hello"] + (f" {name}" if name else "")
    paragraphs = [
        greeting + ",",
        copy["intro"],
        copy["offer"].format(
            origin=option.origin,
            destination=option.destination,
            airline=option.airline,
            price=price,
        ),
        copy["transit"].format(days=option.transit_days),
    ]
    if when:
        paragraphs.append(
            copy["schedule"].format(
                departure=written_date(when[0], language), arrival=written_date(when[1], language)
            )
        )
    if truck and request.door_to_port:
        paragraphs.append(copy["pickup"].format(pickup=request.pickup_label))
        if truck.transit_days is not None:
            kind = {"collection_lead": "collection", "road_transit": "road"}.get(
                truck.timing_kind, "lead"
            )
            paragraphs.append(copy[kind].format(hours=round(truck.transit_days * 24)))
        paragraphs.append(copy["slot"])
    if valid:
        paragraphs.append(copy["valid"].format(valid=valid))
    paragraphs.extend([copy["cutoff"], copy["next"], SIGNATURE])
    return "\n\n".join(paragraphs) + "\n"
