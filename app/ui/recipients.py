"""Thread participants and safe reply targets for the human mail composer."""

from __future__ import annotations

from email.utils import parseaddr
from typing import Any

from app.config import settings
from app.pricing.reference import carrier_by_email


def address(value: str | None) -> str:
    return parseaddr(value or "")[1].strip().lower()


def starter(session: dict[str, Any]) -> str:
    incoming = [m for m in session["mails"] if m["direction"] == "in"]
    root = next((m for m in incoming if m["id"] == session["id"]), None)
    return address(root["sender"] if root else session.get("customer"))


def party(mail: dict[str, Any]) -> str:
    return address(mail["sender"] if mail["direction"] == "in" else mail["recipient"])


def for_session(session: dict[str, Any]) -> list[dict[str, Any]]:
    original = starter(session)
    participants = {}
    for mail in session["mails"]:
        value = mail["sender"] if mail["direction"] == "in" else mail["recipient"]
        peer = address(value)
        if peer and peer != address(settings.team_mailbox):
            participants.setdefault(peer, parseaddr(value or "")[0])
    result = []
    for peer, name in participants.items():
        carrier = carrier_by_email(peer)
        if carrier:
            role = carrier.kind
            label = "Frachtführer · Airline" if role == "airline" else "Frachtführer · Lkw"
            name = carrier.name
        elif peer == address(settings.ratedesk_address):
            role, label = "ratedesk", "Rate Desk"
        elif peer == address(settings.tracking_address):
            role, label = "tracking", "Tracking"
        elif peer == original:
            role, label = "customer", "Anfragesteller"
        else:
            role, label = "participant", "Threadteilnehmer"
        if peer == original:
            label += " · Thread gestartet"
        messages = [m for m in session["mails"] if party(m) == peer]
        incoming = [m for m in messages if m["direction"] == "in"]
        parent = (incoming or messages)[-1]
        result.append(
            {
                "address": peer,
                "name": name,
                "role": role,
                "role_label": label,
                "is_thread_starter": peer == original,
                "parent_mail_id": parent["id"],
                "mail_ids": [m["id"] for m in messages],
            }
        )
    return sorted(result, key=lambda r: (not r["is_thread_starter"], r["address"]))


def resolve(
    session: dict[str, Any], to: str | None, mail_id: int | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a selected participant and its own parent; reject mismatched/cross-session IDs."""
    choices = for_session(session)
    if to is not None and (not to.strip() or "\r" in to or "\n" in to):
        raise ValueError("Bitte eine gültige Empfängeradresse auswählen.")
    selected = next((m for m in session["mails"] if m["id"] == mail_id), None)
    # Older clients submit only an incoming mail ID; keep that explicit choice working.
    if to is None and mail_id is not None:
        if selected is None or selected["direction"] != "in":
            raise ValueError("Die ausgewählte Nachricht gehört nicht zu diesem Thread.")
        peer = party(selected)
    else:
        peer = address(to) if to is not None else starter(session)
    recipient = next((r for r in choices if r["address"] == peer), None)
    if not recipient:
        raise ValueError("Der Empfänger ist kein Teilnehmer dieses Threads.")
    if mail_id is None:
        selected = next(m for m in session["mails"] if m["id"] == recipient["parent_mail_id"])
    if selected is None or party(selected) != peer:
        raise ValueError("Die ausgewählte Nachricht gehört zu einem anderen Empfänger.")
    return recipient, selected
