"""Keep the AI context small so every change starts fast.

AGENTS.md routes each task to the files it needs; the always-read files and the area files
have byte budgets. Exceeding one means moving content out (to an area file or
context/history/), not raising the budget.
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
