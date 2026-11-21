"""Every module under app/ must import, and the startup check must cover every agent.

Round 10 was lost to an agent that failed at its first mail, not at the start. CI now fails
on a module that does not import; at startup run.py logs `agents loaded: ...` or the error.
"""

import importlib
import logging
from pathlib import Path

import pytest

import run
from app import agents, db

ROOT = Path(__file__).resolve().parents[1]
MODULES = sorted(
    ".".join(p.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")
    for p in (ROOT / "app").rglob("*.py")
)
TOOLS = {"rfq_replay"}  # offline tools in app/agents, not reached by mail


def test_there_are_modules_to_import() -> None:
    assert (
        "app.agents.rfq" in MODULES
        and "app.mail.poller" in MODULES
        and len(MODULES) > 25
    )


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    importlib.import_module(name)


def test_startup_check_covers_every_agent() -> None:
    found = (
        {p.stem for p in (ROOT / "app" / "agents").glob("*.py")} - {"__init__"} - TOOLS
    )
    assert set(agents.AGENTS) == found  # a new agent module must be added to AGENTS


def test_startup_check_reports_a_broken_agent_and_goes_on(
    tmp_path, monkeypatch, caplog
) -> None:
    db.init_db(str(tmp_path / "t.db"))
    real = importlib.import_module

    def flaky(name: str, *args):
        if name == "app.agents.tracking":
            raise SyntaxError("broken on purpose")
        return real(name, *args)

    monkeypatch.setattr(agents.importlib, "import_module", flaky)
    with caplog.at_level(logging.INFO):
        run._check_agents(logging.getLogger("run"))  # must not raise

    assert "agents loaded: bids, booking, clarification, rfq, tariff" in caplog.text
    assert "agent tracking does not import: SyntaxError" in caplog.text
    events = db.events_after(0)
    assert [e["kind"] for e in events] == ["agent_import_failed"]
    assert events[0]["level"] == "error"


def test_startup_check_logs_all_agents_when_healthy(tmp_path, caplog) -> None:
    db.init_db(str(tmp_path / "t.db"))
    with caplog.at_level(logging.INFO):
        run._check_agents(logging.getLogger("run"))
    assert "agents loaded: " + ", ".join(agents.AGENTS) in caplog.text
    assert db.events_after(0) == []
