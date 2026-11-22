"""Cargo we must refuse instead of quoting, found in code (no LLM).

- Damaged, defective or swollen lithium batteries: forbidden on passenger AND cargo aircraft
  (IATA DGR Special Provision A154). Asking for the Shipper's Declaration, as for ordinary
  UN3480/UN3481, would treat forbidden cargo as normal dangerous goods.
- Destinations or transit points under embargo (Iran, North Korea, Russia, Belarus).

A battery and a damage word must share a sentence; a negated mention ("not damaged") or an
insurance sentence ("cover for damaged goods") does not count. Only the current message and the request's attachments are read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LITHIUM = re.compile(
    r"\bUN\s*34(?:80|81|90|91)\b|\blithium\b|\bli-?ion\b|\blipo\b|\bakkus?\b|"
    r"\bbatter(?:y|ies|ie|ien)\b",
    re.IGNORECASE,
)
_DAMAGED = re.compile(
    r"\b(?:damaged|defective|defekt\w*|besch[äa]digt\w*|swollen|bloated|aufgebl[äa]ht\w*|"
    r"leaking|ausgelaufen\w*|recalled|r[üu]ckruf\w*|faulty|failure analysis|"
    r"for (?:disposal|recycling)|zur entsorgung|endommag\w*|d[ée]fectueu\w*|dañad\w*|"
    r"defectuos\w*|danneggiat\w*|difettos\w*)\b",
    re.IGNORECASE,
)
_NEGATED = re.compile(
    r"(?:\bnot|\bno|\bnicht|\bkeine?|\bnever|\bnon|\bun)[\s-]*(?:\w+\s+)?$", re.IGNORECASE
)
_SANCTIONED_CODES = {
    "IKA": "Iran",
    "THR": "Iran",
    "FNJ": "North Korea",
    "SVO": "Russia",
    "DME": "Russia",
    "VKO": "Russia",
    "MSQ": "Belarus",
}
_SANCTIONED_NAMES = re.compile(
    r"\b(?P<iran>iran|tehran|teheran)\b|\b(?P<nk>north korea|nordkorea|pyongyang|dprk)\b|"
    r"\b(?P<ru>russia|russland|moscow|moskau)\b|\b(?P<by>belarus|wei(?:ß|ss)russland|minsk)\b",
    re.IGNORECASE,
)
_NAME_GROUPS = {"iran": "Iran", "nk": "North Korea", "ru": "Russia", "by": "Belarus"}
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")
_INSURANCE = re.compile(r"insur|versicher|assuran|seguro|assicura", re.IGNORECASE)


@dataclass(frozen=True)
class Refusal:
    damaged_lithium: bool
    sanctioned: tuple[str, ...]  # countries, in order of mention

    @property
    def reason(self) -> str:
        parts = []
        if self.damaged_lithium:
            parts.append("forbidden_cargo: damaged lithium batteries (IATA DGR SP A154)")
        if self.sanctioned:
            parts.append(f"sanctions: {', '.join(self.sanctioned)}")
        return "; ".join(parts)


def _damaged_lithium(text: str) -> bool:
    """A damage word about the batteries: same sentence, not negated, not insurance cover."""
    for sentence in _SENTENCE.split(text):
        if not _LITHIUM.search(sentence) or _INSURANCE.search(sentence):
            continue
        for m in _DAMAGED.finditer(sentence):
            if not _NEGATED.search(sentence[max(0, m.start() - 20) : m.start()]):
                return True
    return False


def _sanctioned(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for code in re.findall(r"\b[A-Z]{3}\b", text):
        country = _SANCTIONED_CODES.get(code)
        if country and country not in found:
            found.append(country)
    for m in _SANCTIONED_NAMES.finditer(text):
        country = _NAME_GROUPS[next(k for k, v in m.groupdict().items() if v)]
        if country not in found:
            found.append(country)
    return tuple(found)


def check(*texts: str) -> Refusal | None:
    """A refusal when any text names forbidden cargo or a sanctioned place, else None."""
    text = "\n".join(t for t in texts if t)
    refusal = Refusal(_damaged_lithium(text), _sanctioned(text))
    return refusal if refusal.damaged_lithium or refusal.sanctioned else None


def decline_text(refusal: Refusal, name: str | None, language: str, signature: str) -> str:
    """The customer's answer: why we cannot fly it and what would let us quote."""
    german = (language or "").lower().startswith("de")
    places = " und ".join(refusal.sanctioned) if german else " and ".join(refusal.sanctioned)
    if german:
        lines = [f"{'Hallo ' + name if name else 'Guten Tag'},", "vielen Dank für Ihre Anfrage."]
        if refusal.damaged_lithium:
            lines.append(
                "Diese Sendung dürfen wir leider nicht per Luftfracht befördern. Beschädigte, "
                "defekte oder aufgeblähte Lithium-Batterien sind nach den IATA "
                "Gefahrgutvorschriften, Sondervorschrift A154, auf allen Flugzeugen verboten, "
                "auf Passagier- wie auf Frachtmaschinen.\n\nFür solche Rücksendungen ist ein "
                "Straßentransport mit einem zertifizierten Gefahrgutspediteur der übliche Weg. "
                "Sind die Batterien doch unbeschädigt und geprüft, schicken Sie uns gern das "
                "Sicherheitsdatenblatt und die Shipper's Declaration, dann prüfen wir es sofort."
            )
        if refusal.sanctioned:
            lines.append(
                f"Sendungen nach oder über {places} unterliegen aktuell Sanktionen und "
                "Exportkontrollen, daher können wir dafür kein Angebot machen. Ist ein anderes "
                "Ziel möglich, antworten Sie einfach auf diese Mail."
            )
    else:
        lines = [f"{'Hi ' + name if name else 'Hello'},", "thank you for your request."]
        if refusal.damaged_lithium:
            lines.append(
                "Unfortunately we cannot fly this shipment. Damaged, defective or swollen "
                "lithium batteries are forbidden on all aircraft, passenger and cargo alike, "
                "under the IATA Dangerous Goods Regulations, Special Provision A154.\n\nFor "
                "returns like this, road transport with a certified dangerous goods carrier is "
                "the usual way. If the batteries are in fact intact and tested, just send us the "
                "safety data sheet and the Shipper's Declaration and we will look at it again "
                "right away."
            )
        if refusal.sanctioned:
            lines.append(
                f"Shipments to or via {places} are subject to current sanctions and export "
                "controls, so we cannot offer transport there. If another destination works for "
                "you, just reply to this email."
            )
    return "\n\n".join(lines + [signature]) + "\n"
