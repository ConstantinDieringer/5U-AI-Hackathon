"""Board watch: failed verdicts become error events (UI attention list)."""

import pytest

from app import db
from app.ui import watchers


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "t.db"))


@pytest.mark.parametrize(
    ("verdict", "reason", "failed"),
    [
        ("accepted", None, False),
        ("open", None, False),
        ("pending", None, False),
        ("rejected", "too_high", True),  # real round 7 (session 1628)
        ("no answer", None, True),  # real rounds 1 and 4
        ("accepted", "late", True),  # any reason is worth a look
        (None, None, False),
    ],
)
def test_is_failure(verdict, reason, failed) -> None:
    assert watchers.is_failure(verdict, reason) is failed


def _errors() -> list[dict]:
    return [e for e in db.events_after(0) if e["level"] == "error"]


def test_board_loop_emits_error_once_per_failed_round(monkeypatch) -> None:
    rows = [
        {"round_no": 7, "sent_at": "x", "verdict": "rejected", "reason": "too_high"},
        {"round_no": 8, "sent_at": "y", "verdict": "accepted", "reason": None},
        {"round_no": 9, "sent_at": "z", "verdict": "open", "reason": None},
    ]
    from app import hackloop

    monkeypatch.setattr(hackloop, "get_board_me", lambda: {"requests": rows})

    def once(_s: float) -> None:
        raise SystemExit  # stop the loop after one pass

    monkeypatch.setattr(watchers.time, "sleep", once)
    for _ in range(2):  # second pass: same verdicts, no new alert
        with pytest.raises(SystemExit):
            watchers.board_loop()
    errs = _errors()
    assert len(errs) == 1 and "too_high" in errs[0]["message"]
