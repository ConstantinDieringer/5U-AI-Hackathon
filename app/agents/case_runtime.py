"""Case-local contracts, ownership guards and verified business snapshots."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import Field

from app import db
from app.agents import bids, rfq
from app.agents.case_models import (
    Citation,
    Contract,
    Shipment,
    Task,
    ToolError,
    Workspace,
)
from app.agents.case_sources import cited
from app.config import settings
from app.models import Mail, RateOption, Rfq
from app.pricing.reference import Carrier


class Empty(Contract):
    pass


class Read(Contract):
    source: str
    offset: int = Field(default=0, ge=0)


class Search(Contract):
    query: str = Field(min_length=1, max_length=200)


class Prepare(Contract):
    shipment_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    request_json: str
    evidence: list[Citation] = Field(min_length=1, max_length=30)
    task_ids: list[str] = Field(min_length=1, max_length=40)


class Select(Contract):
    shipment_ids: list[str] = Field(min_length=1, max_length=10)


class Choose(Contract):
    shipment_id: str
    eligibility_json: str = Field(
        description="Boolean expression over candidate/cheapest/fastest total_eur and transit_days"
    )
    score_json: str = Field(
        description="Numeric expression to minimize among eligible real options"
    )
    evidence: list[Citation] = Field(min_length=1, max_length=12)


class Deliver(Select):
    artifact_ids: list[str] = Field(default_factory=list, max_length=10)


class Ask(Contract):
    recipient: str = Field(description="customer or an exact carrier email from inspect_case")
    question: str = Field(min_length=1, max_length=2000)
    task_ids: list[str] = Field(min_length=1, max_length=40)
    evidence: list[Citation] = Field(min_length=1, max_length=12)


class Supersede(Contract):
    task_id: str
    evidence: Citation


class ReplaceSource(Contract):
    source_id: str
    evidence: Citation


class Answer(Contract):
    body: str = Field(min_length=1, max_length=6000)
    artifact_ids: list[str] = Field(default_factory=list, max_length=10)


class Delegate(Contract):
    workflow: str = Field(pattern=r"^(booking|tracking|customer_reply)$")


class Pause(Contract):
    reason: str = Field(min_length=1, max_length=1000)


@dataclass
class Runtime:
    job: dict
    owner: str
    mail: Mail
    workspace: Workspace
    step_id: int = 0
    stop_state: str | None = None

    @property
    def sid(self) -> int:
        return int(self.job["session_id"])

    def guard(self) -> None:
        if not db.case_current(self.job["id"], self.owner, time.time()):
            raise ToolError("New input, expired ownership or human controls invalidate this action")
        if db.reply_blocked(self.mail.id):
            raise ToolError("Message is quarantined")
        session = db.get_session(self.sid) or {}
        if (
            session.get("kind") == "rfq"
            and not session.get("quotes")
            and datetime.fromisoformat(session["deadline_at"]) <= datetime.now(UTC)
        ):
            raise ToolError("The original quote deadline expired")

    def effect(self) -> None:
        self.guard()
        # Only the shared send/book boundary marks dispatching. A translation,
        # consent check or style failure before transport remains safe to replan.

    def artifact_path(self, key: str) -> Path:
        # Both identifiers originate in code, never from a model filename or path.
        if key not in self.workspace.artifacts:
            raise ToolError("Unknown artifact")
        return settings.runtime_dir / "cases" / str(self.job["id"]) / f"{key}.pdf"


def add_tasks(workspace: Workspace, tasks: list[Task]) -> None:
    proposed = {**workspace.tasks}
    for task in tasks:
        cited(workspace, task.evidence)
        if task.id in proposed and proposed[task.id] != task:
            raise ToolError(
                "Existing tasks are immutable; supersede with current customer evidence"
            )
        proposed[task.id] = task
    if len(proposed) > 40:
        raise ToolError("Case has too many tasks; request a split or human review")
    visited: set[str] = set()

    def visit(key: str, path: set[str]) -> None:
        if key in path:
            raise ToolError("Task dependencies contain a cycle")
        if key not in proposed:
            raise ToolError("Task dependency is unknown")
        if key in visited:
            return
        for parent in proposed[key].depends_on:
            visit(parent, path | {key})
        visited.add(key)

    for key in proposed:
        visit(key, set())
    workspace.tasks = proposed


def unpack(raw: dict) -> rfq.Priced:
    truck = raw.get("truck")
    offer = None
    if truck:
        truck = dict(truck)
        carrier = Carrier(**truck.pop("carrier"))
        truck["total_eur"] = Decimal(truck["total_eur"])
        if truck.get("original_amount") is not None:
            truck["original_amount"] = Decimal(truck["original_amount"])
        offer = bids.Offer(carrier=carrier, **truck)
    return rfq.Priced(
        Rfq.model_validate(raw["rfq"]),
        RateOption(**raw["choice"]),
        raw["total"],
        offer,
        [RateOption(**o) for o in raw["options"]],
        raw["insurance_minor"],
    )


def selected(ctx: Runtime, ids: list[str], *, priced: bool = False) -> list[Shipment]:
    if len(ids) != len(set(ids)) or not set(ids) <= ctx.workspace.shipments.keys():
        raise ToolError("Unknown or repeated shipment ID")
    if priced and not set(ids) <= ctx.workspace.prices.keys():
        raise ToolError("Every requested shipment needs a successful pricing result")
    return [ctx.workspace.shipments[key] for key in ids]
