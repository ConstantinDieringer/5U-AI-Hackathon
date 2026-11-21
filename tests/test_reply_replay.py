"""Replies after our quote, replayed offline with PREBOOK_ANSWER off and on."""

from __future__ import annotations

import pytest

from app.agents import reply_replay


@pytest.fixture(scope="module")
def runs() -> tuple[list[dict], list[dict]]:
    return reply_replay.replay(False), reply_replay.replay(True)


def test_no_round_gets_worse_and_stored_replies_are_unchanged(runs):
    off, on = runs
    changes, worse = reply_replay.compare(off, on)
    assert worse == []
    unchanged = {"stored", "round55", "round56", "round54+yes"}
    assert not [c for c in changes if c.split()[2].rstrip(":") in unchanged]
    stored = [r for r in on if r["scenario"] == "stored"]
    assert {r["round_no"] for r in stored if r["booked"]} == {12, 13}  # real acceptances


def test_round_54_questions_get_an_answer_and_the_yes_books(runs):
    off, on = runs
    for runs_, first in ((off, "clarification"), (on, "prebook_answer")):
        own = [r for r in runs_ if r["scenario"] == "round54-own+yes"]
        assert own and all(r["after_quote"][0] == first and r["booked"] for r in own)
        assert all(r["kinds"] == ["question", "yes"] for r in own)


def test_deferral_and_foreign_lane_question_keep_todays_path(runs):
    _, on = runs
    for r in on:
        if r["scenario"] in ("round55", "round56"):
            assert r["kinds"] == ["deferral"] and r["after_quote"] == ["clarification"]
        if r["scenario"] == "round54+yes":  # names DEL and 22842: not this round's shipment
            assert "prebook_answer" not in r["after_quote"]
