"""Keep the AI context small so every change starts fast.

CLAUDE.md routes each task to the files it needs; the always-read files and the area files
have byte budgets. Exceeding one means moving content out (to an area file or
context/history/), not raising the budget.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# bytes; always-read files are loaded on every task
ALWAYS_READ = {
    "CLAUDE.md": 4_000,
    "context/code-standards.md": 4_000,
    "context/progress-tracker.md": 6_000,
}
ON_DEMAND = {
    "context/architecture-context.md": 5_000,
    "context/project-overview.md": 5_000,
    "context/ai-workflow-rules.md": 4_000,
    "context/ui-context.md": 4_000,
}
AREA_BUDGET = 6_000
CHANGE_FILE_BUDGET = 600  # bytes per context/changes/ file; AI sessions read the newest eight
CHANGE_FILE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}-[a-z0-9][a-z0-9-]*\.md$")
TRACKER_SECTION_LIMITS = {
    "Pending Decisions and Rollouts": 10,
    "Open Questions": 10,
    "Session Notes": 6,
}


def _size(rel: str) -> int:
    return len((ROOT / rel).read_bytes())


def test_always_read_files_stay_within_budget() -> None:
    over = {f: _size(f) for f, cap in ALWAYS_READ.items() if _size(f) > cap}
    assert not over, f"always-read context over budget, move content out: {over}"


def test_on_demand_files_stay_within_budget() -> None:
    over = {f: _size(f) for f, cap in ON_DEMAND.items() if _size(f) > cap}
    assert not over, f"context file over budget, move content out: {over}"


def test_area_files_stay_within_budget() -> None:
    areas = sorted((ROOT / "context" / "areas").glob("*.md"))
    assert areas, "context/areas/ must hold the per-subsystem files"
    over = {a.name: len(a.read_bytes()) for a in areas if len(a.read_bytes()) > AREA_BUDGET}
    assert not over, f"area file over budget, replace outdated lines: {over}"


def test_tracker_sections_respect_entry_limits() -> None:
    text = (ROOT / "context/progress-tracker.md").read_text(encoding="utf-8")
    sections = re.split(r"^## ", text, flags=re.MULTILINE)
    counts: dict[str, int] = {}
    for section in sections[1:]:
        title, _, body = section.partition("\n")
        name = re.sub(r"\s*\(max \d+.*\)$", "", title.strip())
        counts[name] = sum(1 for line in body.splitlines() if re.match(r"^(- |\d+\. )", line))
    over = {n: c for n, c in counts.items() if c > TRACKER_SECTION_LIMITS.get(n, 10**6)}
    assert not over, f"tracker sections over limit, archive to context/history/: {over}"


def test_claude_md_routes_only_to_existing_files() -> None:
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    paths = set(re.findall(r"`((?:context/|\.agents/|app/|DESIGN|DEPLOY)[^`\s]*\.md)`", text))
    assert paths, "CLAUDE.md must route to context files"
    missing = sorted(p for p in paths if "*" not in p and not (ROOT / p).is_file())
    assert not missing, f"CLAUDE.md points to missing files: {missing}"


def test_tracker_has_no_recent_changes_section() -> None:
    """Recent changes live in context/changes/, one file per PR, so PRs never collide."""
    text = (ROOT / "context/progress-tracker.md").read_text(encoding="utf-8")
    assert not re.search(r"^## Recent Changes", text, flags=re.MULTILINE), (
        "add a file to context/changes/ instead of a Recent Changes section in the tracker"
    )


def test_change_files_are_named_and_small() -> None:
    changes = ROOT / "context" / "changes"
    assert (changes / "README.md").is_file(), "context/changes/README.md describes the format"
    files = [f for f in changes.iterdir() if f.name != "README.md"]
    bad_names = sorted(f.name for f in files if not CHANGE_FILE_NAME.match(f.name))
    assert not bad_names, f"name change files YYYY-MM-DD-HHMM-<branch-slug>.md: {bad_names}"
    over = {f.name: len(f.read_bytes()) for f in files if len(f.read_bytes()) > CHANGE_FILE_BUDGET}
    assert not over, f"change file over {CHANGE_FILE_BUDGET} bytes, shorten it: {over}"
