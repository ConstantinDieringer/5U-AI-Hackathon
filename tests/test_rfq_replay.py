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
    what production sent. Rounds 2 and 12 lack stored data; 18 to 20 now ask for the pickup;
    production never priced round 28. Rounds 21, 22, 25, 26 and 31 now pick another trucker
    (ratio balance); test_trucker_balance_evidence checks them under the old minmax rule."""
    by_round = {c["round_no"]: c for c in corpus["cases"]}
    for round_no in (3, 5, 6, 7, 8, 13, 14, 23, 24, 27, 29, 30):
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


def test_replay_cleanup_also_releases_database_on_failure(tmp_path, monkeypatch):
    from app import db

    temporary_directory = rfq_replay.tempfile.TemporaryDirectory
    monkeypatch.setattr(
        rfq_replay.tempfile,
        "TemporaryDirectory",
        lambda **kwargs: temporary_directory(dir=tmp_path, **kwargs),
    )

    def fail(case, workdir, seen):
        db.init_db(str(workdir / "failure.db"))
        db.kv_set("opened", "true")
        raise ValueError("synthetic replay failure")

    monkeypatch.setattr(rfq_replay, "replay_case", fail)
    with pytest.raises(ValueError, match="synthetic replay failure"):
        rfq_replay.replay_all({"cases": [{"llm_extraction": {}}]})
    assert list(tmp_path.iterdir()) == []


def test_replayed_answers_reach_the_clarification(results) -> None:
    """Round 28's first explicit airport answer now resolves without optional flags."""
    got = results[28]
    assert got["outcome"] == "quoted" and got["open_issues"] == []
    assert got["clarifications_sent"] == 1
    assert got["airline"] == "Ultraviolet Air Freight" and got["total_minor"] == 39045


def test_round_28_with_self_delivery_recovery_quotes_the_portal_price(
    corpus, tmp_path, monkeypatch
) -> None:
    """The corpus holds the later live portal read for round 28 (production never read it)."""
    import dataclasses

    from app import config

    case = next(c for c in corpus["cases"] if c["round_no"] == 28)
    assert case["portal"]["source"].startswith("manual:")
    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, self_delivery_recovery=True)
    )
    got = rfq_replay.replay_case(case, tmp_path, corpus["lane_observations"])
    assert (got["airline"], got["total_minor"], got["clarifications_sent"]) == (
        "Ultraviolet Air Freight",
        39045,
        0,
    )


def test_trucker_balance_evidence(corpus, results, tmp_path, monkeypatch) -> None:
    """TRUCKER_BALANCE=ratio is the default. Under the old minmax rule the harness still
    reproduces production on the rounds where the trucker differs, and the board accuracy of
    those production quotes fits 45 - 4.5 points per % deviation from today's (ratio) total
    (rounds 21, 22, 25, 26): ratio picks the trucker behind the board's best price. Round 31
    has no accuracy yet."""
    import dataclasses

    from app import config
    from app.agents import rfq

    minmax = dataclasses.replace(config.settings, trucker_balance="minmax")
    monkeypatch.setattr(config, "settings", minmax)
    monkeypatch.setattr(rfq, "settings", minmax)
    cases = {c["round_no"]: c for c in corpus["cases"]}
    moved = {}
    for round_no, today in results.items():
        if not today["trucker"]:
            continue
        old = rfq_replay.replay_case(cases[round_no], tmp_path, corpus["lane_observations"])
        if old["trucker"] != today["trucker"]:
            moved[round_no] = (old["total_minor"], today["total_minor"])
            sent = cases[round_no]["production_quote"]
            assert (old["airline"], old["total_minor"]) == (
                sent["airline"],
                sent["total_minor"],
            ), round_no
    assert sorted(moved) == [21, 22, 25, 26, 31]
    for round_no, (minmax_total, ratio_total) in moved.items():
        board = cases[round_no]["board"]["accuracy"]
        if board is not None:
            predicted = 45 - 4.5 * 100 * abs(minmax_total - ratio_total) / ratio_total
            assert abs(predicted - board) < 0.05, (round_no, predicted, board)
