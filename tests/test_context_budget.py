"""Keep the AI context small so every change starts fast.

AGENTS.md routes each task to the files it needs; the always-read files and the area files
have byte budgets. Exceeding one means moving content out (to an area file or
context/history/), not raising the budget.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ON_DEMAND = {
    "context/progress-tracker.md": 6_000,
    "context/architecture-context.md": 5_000,
    "context/project-overview.md": 5_000,
    "context/ai-workflow-rules.md": 4_000,
    "context/ui-context.md": 4_000,
}
AREA_BUDGET = 6_000
TRACKER_SECTION_LIMITS = {
    "Pending Decisions and Rollouts": 10,
    "Open Questions": 10,
    "Session Notes": 6,
}


def _size(rel: str) -> int:
    return len((ROOT / rel).read_bytes())


def test_always_read_files_stay_within_budget() -> None:
    assert (ROOT / "CLAUDE.md").read_text().strip() == "@AGENTS.md"
    assert _size("AGENTS.md") + _size("CLAUDE.md") <= 4000
    assert not re.search(r"^@", (ROOT / "AGENTS.md").read_text(), re.MULTILINE)


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


def test_agent_entry_routes_only_to_existing_files() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    paths = set(re.findall(r"`((?:context/|\.agents/|app/|DESIGN|DEPLOY)[^`\s]*\.md)`", text))
    assert paths, "AGENTS.md must route to context files"
    missing = sorted(p for p in paths if "*" not in p and not (ROOT / p).is_file())
    assert not missing, f"AGENTS.md points to missing files: {missing}"


def test_every_area_has_a_path_rule_that_matches_real_files() -> None:
    rules = sorted((ROOT / ".claude" / "rules").glob("*.md"))
    targets: set[str] = set()
    for rule in rules:
        text = rule.read_text(encoding="utf-8")
        front = text.split("---")[1]
        globs = re.findall(r'^\s+- "([^"]+)"$', front, flags=re.MULTILINE)
        assert globs, f"{rule.name} needs a paths list"
        dead = [g for g in globs if not any(ROOT.glob(g))]
        assert not dead, f"{rule.name} paths match no file: {dead}"
        targets.update(re.findall(r"`(context/[^`]+\.md)`", text))
    areas = {f"context/areas/{a.name}" for a in (ROOT / "context" / "areas").glob("*.md")}
    assert not areas - targets, f"area files without a path rule: {sorted(areas - targets)}"
    assert all((ROOT / t).is_file() for t in targets), "a path rule points to a missing file"
