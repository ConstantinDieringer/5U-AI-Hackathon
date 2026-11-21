"""This process: its start time, recorded once as an event so a restart inside a round can be
seen afterwards (health endpoint, timeline, unanswered-round report)."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from app import db
from app.config import settings

STARTED_AT = datetime.now(UTC)


def record_start(agents_enabled: bool) -> None:
    db.emit(
        "process_start",
        f"Prozess gestartet (Build {settings.build_revision[:12]})",
        None,
        started_at=STARTED_AT.isoformat(timespec="seconds"),
        build_revision=settings.build_revision,
        agents_enabled=agents_enabled,
        pid=os.getpid(),
    )
