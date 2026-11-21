"""Every stored live round replayed offline through the RFQ flow (app/agents/rfq_replay.py).

A failing baseline test means a change moved a quote: option, airline, total, carriers asked,
clarification or mail text. If that is intended, regenerate the baseline and justify every
changed price with board evidence in the PR:

    python -m app.agents.rfq_replay run --compare tests/fixtures/replay/baseline.json
    python -m app.agents.rfq_replay run --out tests/fixtures/replay/baseline.json
"""

import json

import pytest

from app.agents import rfq_replay

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(scope="module")
def corpus() -> dict:
    return json.loads(rfq_replay.CORPUS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def results(corpus) -> dict[int, dict]:
    return {r["round_no"]: r for r in rfq_replay.replay_all(corpus)}


def test_replay_matches_baseline(results) -> None:
    baseline = json.loads(rfq_replay.BASELINE.read_text(encoding="utf-8"))
    changes = rfq_replay.diff(baseline, list(results.values()))
    assert not changes, "replay differs from baseline:\n" + "\n".join(changes)


def test_replay_never_reaches_the_network(results) -> None:
    assert results and all(r["network_blocked"] == 0 for r in results.values())


def test_replay_reproduces_production_quotes(corpus, results) -> None:
    """The harness itself stays faithful: rounds whose stored data is complete quote exactly
    what production sent. Rounds 2 and 12 lack stored data; 18 to 20 now ask for the pickup."""
    by_round = {c["round_no"]: c for c in corpus["cases"]}
    for round_no in (3, 5, 6, 7, 8, 13, 14, 21, 22, 23, 24, 25, 26, 27):
        sent = by_round[round_no]["production_quote"]
        got = results[round_no]
        assert (got["airline"], got["total_minor"]) == (
            sent["airline"],
            sent["total_minor"],
        ), round_no


@pytest.mark.parametrize(
    ("round_no", "airport", "weight_kg"),
    [
        (4, "CGN", 679.0),  # ICAO EDDL from the LLM: the pickup ZIP decides the airport
        (9, "FRA", 1952.0),  # "each" lots: weights summed in code, not the LLM's 0
        (11, "HAM", 855.0),  # LLM origin YYZ equals the destination: ZIP zone wins
    ],
)
def test_lost_rounds_now_resolve_the_request(
    results, round_no, airport, weight_kg
) -> None:
    request = results[round_no]["request"]
    assert request["airport"] == airport and request["weight_kg"] == weight_kg


@pytest.mark.parametrize("round_no", [15, 16, 17])
def test_rounds_without_pickup_ask_instead_of_failing(results, round_no) -> None:
    assert results[round_no]["outcome"] == "clarification"
