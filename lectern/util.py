import re
import threading

from gi.repository import GLib

_num = re.compile(r"(\d+)")


def natural_key(s):
    """Sort key that orders 'Part 2' before 'Part 10'."""
    return [int(t) if t.isdigit() else t.casefold() for t in _num.split(str(s))]


def format_time(seconds, force_hours=False):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h or force_hours:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_duration(seconds):
    """Human readable, e.g. '7 h 12 min'."""
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h} h {m} min" if m else f"{h} h"
    if m:
        return f"{m} min"
    return f"{seconds} s"


def idle(func, *args):
    """Schedule func(*args) once on the main loop, whatever it returns."""
    def cb():
        func(*args)
        return False
    GLib.idle_add(cb)


def run_in_thread(func, *args, callback=None, error_callback=None):
    """Run func(*args) on a worker thread; deliver the result on the main loop."""
    def worker():
        try:
            result = func(*args)
        except Exception as e:  # noqa: BLE001 - reported to caller
            import logging
            logging.getLogger(__name__).exception("background task failed")
            if error_callback:
                idle(error_callback, e)
            return
        if callback:
            idle(callback, result)
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t
