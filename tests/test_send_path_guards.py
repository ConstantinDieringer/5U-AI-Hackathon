"""The customer send path fails closed when a guard cannot record or verify its state."""

import pytest

from app import db, hackloop
from app.mail.style import SIGNATURE


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "send.db"))


def test_failed_transport_marker_stops_the_quote(monkeypatch):
    """Without the marker a restart would free the quote claim and allow a second quote."""
    monkeypatch.setattr(hackloop, "send", lambda **kw: pytest.fail("sent without marker"))

    def marker():
        raise RuntimeError("database is locked")

    with pytest.raises(hackloop.CustomerSendBlocked, match="Transport marker"):
        hackloop.send_customer(
            "customer@hackloop.mail",
            "FRA to MIA",
            f"Hello,\n\nThe total is 100 €.\n\n{SIGNATURE}",
            review=False,
            before_transport=marker,
        )


def test_transport_marker_is_written_before_the_send(monkeypatch):
    order = []
    monkeypatch.setattr(hackloop, "send", lambda **kw: order.append("send") or {"id": 1})
    hackloop.send_customer(
        "customer@hackloop.mail",
        "FRA to MIA",
        f"Hello,\n\nThe total is 100 €.\n\n{SIGNATURE}",
        review=False,
        before_transport=lambda: order.append("marker"),
    )
    assert order == ["marker", "send"]
