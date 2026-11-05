import datetime as dt

from lectern.stats import hours_minutes, last_days, streak

TODAY = dt.date(2026, 10, 24)


def test_streak_counts_back_from_today():
    assert streak(["2026-10-22", "2026-10-23", "2026-10-24"], TODAY) == 3


def test_streak_still_alive_if_today_not_started():
    assert streak(["2026-10-22", "2026-10-23"], TODAY) == 2


def test_streak_broken():
    assert streak(["2026-10-20", "2026-10-24"], TODAY) == 1
    assert streak(["2026-10-01"], TODAY) == 0
    assert streak([], TODAY) == 0


def test_last_days_zero_filled_oldest_first():
    days = last_days({"2026-10-24": 600, "2026-10-20": 60}, n=7, today=TODAY)
    assert [d.isoformat() for d, _ in days][0] == "2026-10-18"
    assert days[-1] == (TODAY, 600.0)
    assert dict((d.isoformat(), s) for d, s in days)["2026-10-20"] == 60.0
    assert sum(s for _, s in days) == 660


def test_hours_minutes():
    assert hours_minutes(59) == "0 min"
    assert hours_minutes(3600 * 5 + 60 * 7) == "5 h 7 min"
