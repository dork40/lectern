import logging
import logging.handlers
import os

from .const import STATE_DIR


def setup_logging():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if os.environ.get("LECTERN_DEBUG") else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    fh = logging.handlers.RotatingFileHandler(
        STATE_DIR / "lectern.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(level if os.environ.get("LECTERN_DEBUG") else logging.WARNING)
    root.addHandler(sh)
