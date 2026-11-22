import ast
from pathlib import Path

import pytest

from tools.ship_check import selection


@pytest.mark.parametrize(
    ("paths", "mode"),
    [
        ({"AGENTS.md"}, "docs"),
        ({"app/pricing/selection.py"}, "area"),
        ({"app/db.py"}, "full"),
        ({"app/new/decision.py", "app/pricing/selection.py"}, "full"),
        ({"tools/ship_check.py"}, "full"),
        ({"tests/test_removed_module.py"}, "full"),
    ],
)
def test_selects_checks_for_both_sides_of_a_change(paths, mode):
    actual, tests = selection(paths)
    assert actual == mode
    if mode == "area":
        assert "tests/test_arrival_constraints.py" in tests


@pytest.mark.core
def test_air_selection_depends_only_on_value_types_and_pricing():
    path = Path(__file__).resolve().parents[1] / "app/pricing/selection.py"
    allowed = {"dataclasses", "datetime", "typing", "app.models", "app.pricing.pricing"}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed, (
                f"Unexpected selection dependency: {node.module}"
            )
        elif isinstance(node, ast.Import):
            assert all(n.name in allowed for n in node.names)
