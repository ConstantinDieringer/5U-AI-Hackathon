from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tools import merge_pr
from tools.merge_pr import verify_pr


@pytest.fixture()
def pr():
    return {
        "state": "OPEN",
        "body": "## Customer behaviour\nQuote\n## Owner and extension point\npricing\n## Counterexample\nLate\n## Evidence\nReplay",
        "isDraft": False,
        "baseRefName": "main",
        "headRefOid": "abc1234",
        "isCrossRepository": False,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [
            {
                "name": "ship-check",
                "workflowName": "CI",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ],
    }


def test_green_current_head_can_ship(pr):
    assert verify_pr(pr) == "abc1234"


@pytest.mark.parametrize(
    "change", [{"conclusion": "FAILURE"}, {"status": "IN_PROGRESS"}, {"workflowName": "Other"}]
)
def test_failed_pending_or_missing_contract_blocks_merge(pr, change):
    pr["statusCheckRollup"][0].update(change)
    with pytest.raises(ValueError):
        verify_pr(pr)


def test_unfilled_review_template_blocks_merge(pr):
    pr["body"] = "## Evidence\n<!-- describe tests -->"
    with pytest.raises(ValueError, match="PR evidence missing"):
        verify_pr(pr)


@pytest.mark.parametrize(
    "age,safe,allowed",
    [(3600, True, True), (10, True, False), (None, True, False), (3600, False, False)],
)
def test_stale_pricing_matches_server_gate_and_active_or_unknown_work_stays_blocked(
    monkeypatch, age, safe, allowed
):
    rows = [
        {
            "updated_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat()
            if age is not None
            else "unknown"
        }
    ]
    original_client = httpx.Client

    def answer(request):
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json=rows)
        assert request.url.path == "/api/deploy-gate"
        return httpx.Response(200, json={"safe": safe, "reason": "server gate"})

    monkeypatch.setattr(
        merge_pr.httpx,
        "Client",
        lambda **kw: original_client(**kw, transport=httpx.MockTransport(answer)),
    )
    monkeypatch.setenv("UI_PASSWORD", "test-only")
    if allowed:
        merge_pr.deploy_window("https://production.example", None)
    else:
        with pytest.raises(ValueError):
            merge_pr.deploy_window("https://production.example", None)
