"""Listening statistics helpers (pure, unit tested)."""
import datetime as dt


def streak(days, today=None):
    """Consecutive listening days ending today (or yesterday, if today hasn't started yet)."""
    today = today or dt.date.today()
    have = {dt.date.fromisoformat(d) for d in days}
    day = today if today in have else today - dt.timedelta(days=1)
    n = 0
    while day in have:
        n += 1
        day -= dt.timedelta(days=1)
    return n


def last_days(by_day, n=14, today=None):
    """[(date, seconds)] for the last n days, oldest first, zero-filled."""
    today = today or dt.date.today()
    out = []
    for i in range(n - 1, -1, -1):
        d = today - dt.timedelta(days=i)
        out.append((d, float(by_day.get(d.isoformat(), 0.0))))
    return out


def hours_minutes(seconds):
    seconds = int(seconds)
    h, m = seconds // 3600, (seconds % 3600) // 60
    if h:
        return f"{h} h {m} min"
    return f"{m} min"
