"""Which trucker bid is the balanced one (price and speed 50/50)?

Real bids from live rounds with the trucker the board's best price implies. The board scores
accuracy = 45 * (1 - |our total - best| / best / 10 %); with each round's air leg, only the
trucker below reproduces every board value to the hundredth (rounds 21, 22, 25, 26 lost
7 to 17 points with the min-max choice). TRUCKER_BALANCE=ratio (the default) picks it;
`minmax` keeps the old choice.
"""

import dataclasses
from decimal import Decimal

import pytest

from app.agents import bids, rfq
from app.config import settings

# round: (board accuracy, [(trucker, EUR, transit hours)], trucker behind the best price)
ROUNDS = {
    13: (
        45.0,
        [
            ("Maintal Transport Logistik", "190.33", 26),
            ("Alpenblick Logistik", "122.76", 30),
            ("Hansekogge Trucking", "201.52", 19),
            ("Neckarstrom Kurier", "348.85", 11),
            ("Flinkwagen Logistik", "273.28", 19),
        ],
        "Hansekogge Trucking",
    ),
    14: (
        45.0,
        [
            ("Rheinufer Spedition", "379.38", 7),
            ("Schwabenbote Transporte", "379.79", 6),
            ("Spessart Spedition", "113.05", 32),
            ("Westfalenroute", "321.74", 4),
        ],
        "Westfalenroute",
    ),
    18: (
        45.0,
        [
            ("Bodenseefracht", "254.84", 12),
            ("Elbkante Logistik", "128.18", 29),
            ("Maintal Transport Logistik", "212.14", 13),
            ("Westfalenroute", "208.88", 11),
        ],
        "Westfalenroute",
    ),
    19: (
        38.07,
        [
            ("Bodenseefracht", "451.14", 12),
            ("Elbkante Logistik", "214.06", 29),
            ("Maintal Transport Logistik", "347.09", 13),
            ("Westfalenroute", "356.10", 11),
        ],
        "Westfalenroute",
    ),
    21: (
        37.62,
        [
            ("Schwabenbote Transporte", "205.88", 12),
            ("Spessart Spedition", "165.36", 17),
            ("Waldrand Transporte", "184.36", 15),
        ],
        "Schwabenbote Transporte",
    ),
    22: (
        28.53,
        [
            ("Ruhrpott Express", "220.11", 31),
            ("Spessart Spedition", "268.45", 32),
            ("Sternmeile Logistik", "499.56", 11),
        ],
        "Sternmeile Logistik",
    ),
    24: (
        45.0,
        [
            ("Odenwald Cargo", "190.32", 26),
            ("Ruhrpott Express", "340.02", 11),
            ("Vogelsberg Haulage", "385.10", 13),
        ],
        "Ruhrpott Express",
    ),
    25: (
        28.96,
        [
            ("Isarwelle Spedition", "134.40", 21),
            ("Nordlicht Haulage", "203.04", 14),
            ("Rheinufer Spedition", "94.08", 28),
            ("Spessart Spedition", "225.04", 9),
        ],
        "Spessart Spedition",
    ),
    26: (
        28.05,
        [
            ("Alpenblick Logistik", "349.20", 15),
            ("Kranich Kurier", "242.72", 21),
            ("Neckarstrom Kurier", "245.80", 27),
            ("Sternmeile Logistik", "447.44", 4),
        ],
        "Sternmeile Logistik",
    ),
    27: (
        39.32,
        [
            ("Alpenblick Logistik", "92.36", 30),
            ("Flinkwagen Logistik", "182.08", 19),
            ("Hansekogge Trucking", "140.72", 19),
            ("Maintal Transport Logistik", "137.13", 26),
            ("Neckarstrom Kurier", "234.85", 11),
        ],
        "Hansekogge Trucking",
    ),
    29: (
        45.0,
        [("Domblick Kurierdienst", "202.75", 28), ("Westfalenroute", "137.50", 30)],
        "Westfalenroute",
    ),
}
# today's min-max choice where it differs from the scorer's (the lost points)
MINMAX_DIFFERS = {
    21: "Spessart Spedition",
    22: "Ruhrpott Express",
    25: "Isarwelle Spedition",
    26: "Kranich Kurier",
}


def _offers(rows: list[tuple[str, str, int]]) -> list[bids.Offer]:
    return [
        bids.Offer(
            bids.Carrier(name, "trucker", f"t{i}@hackloop.mail", ("51",)),
            Decimal(eur),
            hours / 24,
            i,
        )
        for i, (name, eur, hours) in enumerate(rows)
    ]


def _mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(
        rfq, "settings", dataclasses.replace(settings, trucker_balance=mode)
    )


@pytest.mark.parametrize("round_no", sorted(ROUNDS))
def test_ratio_balance_picks_the_trucker_behind_the_best_price(
    monkeypatch, round_no
) -> None:
    _mode(monkeypatch, "ratio")
    _, rows, expected = ROUNDS[round_no]
    assert rfq.best_truck(_offers(rows)).carrier.name == expected


@pytest.mark.parametrize("round_no", sorted(ROUNDS))
def test_minmax_keeps_todays_choice(monkeypatch, round_no) -> None:
    _mode(monkeypatch, "minmax")  # the old rule, still selectable
    _, rows, expected = ROUNDS[round_no]
    assert rfq.best_truck(_offers(rows)).carrier.name == MINMAX_DIFFERS.get(
        round_no, expected
    )


def test_ratio_balance_without_transit_or_zero_transit_stays_safe(monkeypatch) -> None:
    _mode(monkeypatch, "ratio")
    no_transit = _offers([("A", "300", 10), ("B", "250", 10)])
    no_transit[0] = dataclasses.replace(no_transit[0], transit_days=None)
    assert rfq.best_truck(no_transit).carrier.name == "B"  # cheapest, as before
    zero = _offers([("A", "300", 0), ("B", "250", 10)])
    assert rfq.best_truck(zero).carrier.name in {"A", "B"}  # no division by zero


@pytest.mark.parametrize(
    ("raw", "mode"),
    [
        (None, "ratio"),
        ("ratio", "ratio"),
        (" Ratio ", "ratio"),
        ("minmax", "minmax"),
        ("sometimes", "ratio"),
    ],
)
def test_trucker_balance_setting_falls_back_to_ratio(monkeypatch, raw, mode) -> None:
    from app import config

    if raw is None:
        monkeypatch.delenv("TRUCKER_BALANCE", raising=False)
    else:
        monkeypatch.setenv("TRUCKER_BALANCE", raw)
    assert config._trucker_balance() == mode  # a typo never stops the start
