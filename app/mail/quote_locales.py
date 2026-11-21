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
