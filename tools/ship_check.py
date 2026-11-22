"""Run the same diff-based checks locally and in CI: python -m tools.ship_check BASE."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC_TESTS = [
    "tests/test_context_budget.py",
    "tests/test_ops_guardrails.py::test_no_secrets_in_repo",
]
# Unmapped production files deliberately select full coverage. Narrow a path only with
# evidence of its consumers; shared pricing primitives, models, config and I/O stay full.
AREAS = {
    "app/pricing/selection.py": "arrival_constraints rfq_agent multi_shipment carrier_prefs carrier_exclusion_guard trucker_balance rfq_replay",
    "app/mail/selection_intent.py": "arrival_constraints rfq_validation_gate rfq_history recent_rounds_e2e multi_shipment rfq_replay",
    "app/agents/consent_check.py": "booking prebook thread_rescue reply_replay customer_reply_workflow",
    "app/agents/tracking.py": "tracking tracking_notice followup customer_reply_workflow",
    "app/ui/drafts.py": "server dashboard_problems cargo_insurance arrival_constraints manual_reply_targets",
}
DOC_ROOTS = {
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "DEPLOY.md",
    "DESIGN.md",
    ".github/pull_request_template.md",
}


def git_paths(*args: str) -> set[str]:
    data = subprocess.check_output(["git", *args], cwd=ROOT)
    return {p.decode("utf-8") for p in data.split(b"\0") if p}


def changed(base: str) -> set[str]:
    ancestor = subprocess.check_output(
        ["git", "merge-base", base, "HEAD"], cwd=ROOT, text=True
    ).strip()
    # No rename detection: examine both the deleted and the added path.
    return git_paths("diff", "--no-renames", "--name-only", "-z", ancestor, "--") | git_paths(
        "ls-files", "--others", "--exclude-standard", "-z"
    )


def selection(paths: set[str]) -> tuple[str, list[str]]:
    tests = set()
    for path in sorted(paths):
        if path in DOC_ROOTS or (
            path.startswith(("context/", ".claude/rules/")) and path.endswith(".md")
        ):
            continue
        if path in AREAS:
            tests.update("tests/test_" + n + ".py" for n in AREAS[path].split())
        elif path.startswith("tests/test_") and path.endswith(".py") and (ROOT / path).is_file():
            tests.add(path)
        else:
            return "full", []
    return ("area", sorted(tests)) if tests else ("docs", DOC_TESTS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", nargs="?", default="origin/main")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    mode, tests = ("full", []) if args.full else selection(changed(args.base))
    print("ship-check:", mode, *tests, flush=True)
    if args.list:
        return
    env = dict(os.environ, TEAM_API_KEY="test-only", OPENAI_API_KEY="test-only")
    commands = [["-m", "core"], ["-m", "not core", *tests]] if mode == "area" else [tests]
    for command in commands:
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *command], check=True, env=env, cwd=ROOT
        )
    subprocess.run(
        [sys.executable, "-m", "ruff", "check", "app", "tests", "tools"], check=True, cwd=ROOT
    )


if __name__ == "__main__":
    main()
