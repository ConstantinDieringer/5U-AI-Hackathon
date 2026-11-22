import pytest

from tools.merge_pr import verify_pr


@pytest.fixture()
def pr():
    return {
        "state": "OPEN",
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
