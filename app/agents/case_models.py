"""Persistent, evidence-bound workbench contracts, independent of request wording."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Citation(Contract):
    source: str
    text: str = Field(min_length=1, max_length=16000)


class Task(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str = Field(min_length=1, max_length=1200)
    evidence: list[Citation] = Field(min_length=1, max_length=12)
    depends_on: list[str] = Field(max_length=30)


class Decision(Contract):
    tasks: list[Task] = Field(max_length=40)
    tool: str = Field(min_length=1, max_length=64)
    arguments_json: str = Field(max_length=50000)
    reason: str = Field(max_length=1200)


class Source(Contract):
    id: str
    mail_id: int
    role: Literal["customer", "carrier", "outgoing"]
    name: str
    text: str
    attachment_id: int | None = None
    read: bool = True
    problem: str | None = None
    superseded_by: Citation | None = None


class Observation(Contract):
    id: str
    tool: str
    ok: bool
    result: dict[str, JsonValue]


class Shipment(Contract):
    id: str
    request: dict[str, JsonValue]
    evidence: list[Citation]
    task_ids: list[str]
    slot: int


class Artifact(Contract):
    id: str
    filename: str
    sha256: str
    shipment_ids: list[str]


class Workspace(Contract):
    sources: dict[str, Source] = Field(default_factory=dict)
    tasks: dict[str, Task] = Field(default_factory=dict)
    superseded: dict[str, Citation] = Field(default_factory=dict)
    shipments: dict[str, Shipment] = Field(default_factory=dict)
    prices: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    observations: list[Observation] = Field(default_factory=list)
    last_audit: dict[str, JsonValue] = Field(default_factory=dict)


class Finding(Contract):
    task_id: str
    status: Literal["satisfied", "unmet", "unknown"]
    evidence_refs: list[str] = Field(max_length=20)
    reason: str = Field(max_length=1200)


class Audit(Contract):
    findings: list[Finding] = Field(max_length=40)
    missing_tasks: list[Task] = Field(max_length=40)
    unsupported_claims: list[str] = Field(max_length=20)


class ToolError(ValueError):
    """Recoverable tool feedback; the planner can choose another approach."""
