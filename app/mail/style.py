"""Presentation rules for automatically generated mail, independent of stored money."""

SIGNATURE = "Your austrian Power Team"


def amount(minor: int, currency: str = "EUR") -> str:
    """Format integer minor units without float conversion or currency conversion."""
    sign = "-" if minor < 0 else ""
    whole, cents = divmod(abs(minor), 100)
    number = f"{whole:,}".replace(",", ".") + f",{cents:02d}"
    return f"{sign}{number} {'€' if currency == 'EUR' else currency}"
