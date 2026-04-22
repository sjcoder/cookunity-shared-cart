"""Date helpers. CookUnity delivers on Mondays — carts are keyed by delivery date."""

from __future__ import annotations

from datetime import date, timedelta


def upcoming_mondays(n: int = 4, today: date | None = None) -> list[str]:
    """Return the next N Monday delivery dates as ``YYYY-MM-DD`` strings.

    Starts from the nearest upcoming Monday. If today is a Monday it keeps
    today (the order window may still be open); otherwise it skips forward.
    """
    today = today or date.today()
    days = (0 - today.weekday()) % 7  # 0 = Monday
    first = today + timedelta(days=days)
    return [(first + timedelta(days=7 * i)).isoformat() for i in range(n)]


def parse_iso_date(value: str) -> str:
    """Validate an ISO date and return it unchanged. Raises ``ValueError``."""
    date.fromisoformat(value)
    return value
