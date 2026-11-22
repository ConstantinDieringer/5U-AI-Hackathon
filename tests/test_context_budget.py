"""Keep the AI context small so every change starts fast.

CONTEXT.md is the only project summary; CHANGELOG.md keeps only major changes.
Detailed history belongs in Git, not additional summaries or change fragments.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _size(rel: str) -> int:
    return len((ROOT / rel).read_bytes())


def test_always_read_files_stay_within_budget() -> None:
    assert (ROOT / "CLAUDE.md").read_text().strip() == "@AGENTS.md"
    assert _size("AGENTS.md") + _size("CLAUDE.md") <= 4000
    assert not re.search(r"^@", (ROOT / "AGENTS.md").read_text(), re.MULTILINE)
    assert len((ROOT / "CONTEXT.md").read_text(encoding="utf-8").split()) <= 500
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    entries = re.findall(r"^\* (.+)$", changelog, re.MULTILINE)
    assert len(entries) <= 8
    assert all(len(entry.split()) <= 40 for entry in entries)
    assert _size("CHANGELOG.md") <= 2500
    assert not [p for p in (ROOT / "context").rglob("*.md") if "docs" not in p.parts]
