from datetime import date

from cookunity.dates import parse_iso_date, upcoming_mondays


def test_upcoming_mondays_from_wednesday_keeps_current_week():
    # 2026-04-22 is a Wednesday — next Monday is 2026-04-27.
    dates = upcoming_mondays(3, today=date(2026, 4, 22))
    assert dates == ["2026-04-27", "2026-05-04", "2026-05-11"]


def test_upcoming_mondays_from_monday_keeps_today():
    # 2026-04-27 is a Monday — order window may still be open, keep it.
    dates = upcoming_mondays(2, today=date(2026, 4, 27))
    assert dates == ["2026-04-27", "2026-05-04"]


def test_upcoming_mondays_from_sunday_jumps_one_day():
    # 2026-04-26 is a Sunday — next delivery is the very next day.
    dates = upcoming_mondays(1, today=date(2026, 4, 26))
    assert dates == ["2026-04-27"]


def test_upcoming_mondays_returns_iso_strings():
    dates = upcoming_mondays(4, today=date(2026, 4, 22))
    for d in dates:
        assert len(d) == 10 and d[4] == "-" and d[7] == "-"


def test_parse_iso_date_passthrough():
    assert parse_iso_date("2026-04-27") == "2026-04-27"


def test_parse_iso_date_rejects_bad_input():
    import pytest

    with pytest.raises(ValueError):
        parse_iso_date("not-a-date")
