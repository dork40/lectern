import os
from pathlib import Path

APP_ID = "app.lectern.Lectern"
APP_NAME = "Lectern"
APP_SLUG = "lectern"
VERSION = "0.8.0"
# bump when scanning/metadata rules change: existing libraries get one full rescan
SCAN_VERSION = 2


def _xdg(var, fallback):
    return Path(os.environ.get(var) or Path.home() / fallback)


DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / APP_SLUG
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / APP_SLUG
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state") / APP_SLUG
COVER_CACHE = CACHE_DIR / "covers"
THUMB_CACHE = CACHE_DIR / "thumbs"
DB_PATH = DATA_DIR / "library.db"

AUDIO_EXTS = {".m4b", ".m4a", ".mp3", ".ogg", ".oga", ".opus", ".flac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
COVER_NAMES = ("cover", "folder", "front", "album", "art", "poster")

USER_AGENT = f"{APP_NAME}/{VERSION} (audiobook player; +https://aur.archlinux.org/packages/{APP_SLUG})"


def ensure_dirs():
    for d in (DATA_DIR, CACHE_DIR, STATE_DIR, COVER_CACHE, THUMB_CACHE):
        d.mkdir(parents=True, exist_ok=True)
