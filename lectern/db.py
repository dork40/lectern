"""SQLite storage for the library, listening progress and settings."""
import json
import logging
import sqlite3
import threading
import time

from .models import Book, Chapter, Track

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS books(
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    root TEXT,
    kind TEXT NOT NULL,
    title TEXT, author TEXT, narrator TEXT, series TEXT, series_index TEXT, year TEXT,
    duration REAL DEFAULT 0,
    cover_path TEXT, cover_source TEXT,
    tracks TEXT, chapters TEXT,
    signature TEXT, fingerprint TEXT,
    added REAL,
    overrides TEXT DEFAULT '{}',
    audio TEXT
);
CREATE TABLE IF NOT EXISTS progress(
    book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    position REAL DEFAULT 0,
    last_played REAL DEFAULT 0,
    finished INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS bookmarks(
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    position REAL NOT NULL,
    note TEXT DEFAULT '',
    created REAL
);
CREATE TABLE IF NOT EXISTS listening(
    day TEXT NOT NULL,            -- local date, YYYY-MM-DD
    book_id INTEGER NOT NULL,     -- kept even if the book is removed later
    seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(day, book_id)
);
CREATE TABLE IF NOT EXISTS folders(path TEXT PRIMARY KEY);
"""

EDITABLE = ("title", "author", "narrator", "series", "series_index", "year")
# Covers chosen by the user survive rescans; scanned ones may be refreshed.
USER_COVER_SOURCES = ("online", "user")

DEFAULT_SETTINGS = {
    "skip_back": 15,
    "skip_forward": 30,
    "rewind_on_resume": True,
    "rewind_after_minutes": 10,
    "rewind_seconds": 10,
    "sleep_fade": True,
    "sleep_fade_seconds": 30,
    "sleep_rewind_seconds": 30,
    "sleep_screen_off": False,       # when the timer starts
    "sleep_end_screen_off": False,   # when the timer ends (opt-in)
    "sleep_last": {"mode": "duration", "minutes": 30},
    "cover_auto_fetch": True,
    "cover_auto_pick": True,
    "cover_embed": False,
    "audio_global": {"boost": 1.0, "voice_clarity": False, "night_mode": False},
    "view": "grid",
    "sort": "recent",
    "group": "none",
    "filter": "all",
    "rate": 1.0,
    "last_book": None,
    "onboarded": False,
    "window_size": None,
    "scan_version": 0,
    "cover_misses": {},
    "background_playback": True,
    "skip_silence": False,
    "background_hint_shown": False,
    "window_maximized": False,
}


class Database:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self):
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(books)")}
        if "rate" not in cols:
            self._conn.execute("ALTER TABLE books ADD COLUMN rate REAL")  # per-book speed

    def close(self):
        with self._lock:
            self._conn.close()

    def _exec(self, sql, args=(), commit=True):
        with self._lock:
            cur = self._conn.execute(sql, args)
            if commit:
                self._conn.commit()
            return cur

    def _query(self, sql, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # -------------------------------------------------------------- settings
    def get_setting(self, key, default=None):
        rows = self._query("SELECT value FROM settings WHERE key=?", (key,))
        if rows:
            try:
                return json.loads(rows[0]["value"])
            except ValueError:
                pass
        return DEFAULT_SETTINGS.get(key, default) if default is None else default

    def set_setting(self, key, value):
        self._exec("INSERT INTO settings(key, value) VALUES(?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    # --------------------------------------------------------------- folders
    def folders(self):
        return [r["path"] for r in self._query("SELECT path FROM folders ORDER BY path")]

    def add_folder(self, path):
        self._exec("INSERT OR IGNORE INTO folders(path) VALUES(?)", (str(path),))

    def remove_folder(self, path):
        self._exec("DELETE FROM folders WHERE path=?", (str(path),))
        self._exec("DELETE FROM books WHERE root=?", (str(path),))

    # ----------------------------------------------------------------- books
    def _row_to_book(self, r):
        overrides = json.loads(r["overrides"] or "{}")
        fields = {k: (overrides.get(k) if overrides.get(k) not in (None, "") else (r[k] or "")) for k in EDITABLE}
        return Book(
            id=r["id"], path=r["path"], kind=r["kind"], duration=r["duration"] or 0.0,
            cover_path=r["cover_path"],
            tracks=[Track(**t) for t in json.loads(r["tracks"] or "[]")],
            chapters=[Chapter(**c) for c in json.loads(r["chapters"] or "[]")],
            added=r["added"] or 0.0,
            position=r["position"] or 0.0, last_played=r["last_played"] or 0.0,
            finished=bool(r["finished"]),
            audio=json.loads(r["audio"]) if r["audio"] else None,
            rate=r["rate"],
            overrides=overrides, **fields)

    _SELECT = ("SELECT b.*, p.position, p.last_played, p.finished FROM books b "
               "LEFT JOIN progress p ON p.book_id = b.id")

    def all_books(self):
        return [self._row_to_book(r) for r in self._query(self._SELECT)]

    def get_book(self, book_id):
        rows = self._query(self._SELECT + " WHERE b.id=?", (book_id,))
        return self._row_to_book(rows[0]) if rows else None

    def known_books(self, root):
        """{path: (id, signature)} for books scanned from ``root``."""
        return {r["path"]: (r["id"], r["signature"])
                for r in self._query("SELECT id, path, signature FROM books WHERE root=?", (str(root),))}

    def roots(self):
        return [r["root"] for r in self._query("SELECT DISTINCT root FROM books")]

    def upsert_book(self, data):
        """Insert or update a scanned book. Returns its id.

        User edits (overrides), per-book audio settings and user-chosen covers
        are preserved across rescans.
        """
        tracks = json.dumps([t.__dict__ for t in data["tracks"]])
        chapters = json.dumps([c.__dict__ for c in data["chapters"]])
        with self._lock:
            row = self._conn.execute("SELECT id, cover_source, cover_path FROM books WHERE path=?",
                                     (data["path"],)).fetchone()
            cols = ("root", "kind", "title", "author", "narrator", "series", "series_index", "year",
                    "duration", "signature", "fingerprint")
            values = [data.get(c) for c in cols] + [tracks, chapters]
            if row:
                keep_cover = row["cover_source"] in USER_COVER_SOURCES or not data.get("cover_path")
                sets = ", ".join(f"{c}=?" for c in cols) + ", tracks=?, chapters=?"
                args = values
                if not keep_cover:
                    sets += ", cover_path=?, cover_source=?"
                    args = values + [data.get("cover_path"), data.get("cover_source")]
                self._conn.execute(f"UPDATE books SET {sets} WHERE id=?", (*args, row["id"]))
                book_id = row["id"]
            else:
                cur = self._conn.execute(
                    f"INSERT INTO books(path, {', '.join(cols)}, tracks, chapters, cover_path, cover_source, added) "
                    f"VALUES(?, {', '.join('?' * len(cols))}, ?, ?, ?, ?, ?)",
                    (data["path"], *values, data.get("cover_path"), data.get("cover_source"), time.time()))
                book_id = cur.lastrowid
            self._conn.commit()
            return book_id

    def adopt(self, old_id, new_id):
        """A book moved on disk: carry progress, edits, audio and cover over to its new row."""
        with self._lock:
            old = self._conn.execute("SELECT * FROM books WHERE id=?", (old_id,)).fetchone()
            if old is None:
                return
            self._conn.execute("UPDATE books SET overrides=?, audio=?, added=? WHERE id=?",
                               (old["overrides"], old["audio"], old["added"], new_id))
            if old["cover_source"] in USER_COVER_SOURCES:
                self._conn.execute("UPDATE books SET cover_path=?, cover_source=? WHERE id=?",
                                   (old["cover_path"], old["cover_source"], new_id))
            self._conn.execute("DELETE FROM progress WHERE book_id=?", (new_id,))
            self._conn.execute("UPDATE progress SET book_id=? WHERE book_id=?", (new_id, old_id))
            self._conn.execute("DELETE FROM books WHERE id=?", (old_id,))
            self._conn.commit()

    def fingerprints(self, ids):
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        return {r["id"]: r["fingerprint"] for r in self._query(f"SELECT id, fingerprint FROM books WHERE id IN ({q})", tuple(ids))}

    def delete_books(self, ids):
        with self._lock:
            self._conn.executemany("DELETE FROM books WHERE id=?", [(i,) for i in ids])
            self._conn.commit()

    def set_cover(self, book_id, path, source):
        self._exec("UPDATE books SET cover_path=?, cover_source=? WHERE id=?", (path, source, book_id))

    def cover_source(self, book_id):
        rows = self._query("SELECT cover_source FROM books WHERE id=?", (book_id,))
        return rows[0]["cover_source"] if rows else None

    def set_overrides(self, book_id, overrides):
        clean = {k: v for k, v in overrides.items() if k in EDITABLE}
        self._exec("UPDATE books SET overrides=? WHERE id=?", (json.dumps(clean), book_id))

    def set_audio(self, book_id, audio):
        self._exec("UPDATE books SET audio=? WHERE id=?", (json.dumps(audio) if audio else None, book_id))

    def set_rate(self, book_id, rate):
        self._exec("UPDATE books SET rate=? WHERE id=?", (rate, book_id))

    # ------------------------------------------------------------- bookmarks
    def bookmarks(self, book_id):
        return [dict(r) for r in self._query(
            "SELECT id, position, note, created FROM bookmarks WHERE book_id=? ORDER BY position", (book_id,))]

    def add_bookmark(self, book_id, position, note=""):
        return self._exec("INSERT INTO bookmarks(book_id, position, note, created) VALUES(?, ?, ?, ?)",
                          (book_id, position, note, time.time())).lastrowid

    def update_bookmark(self, bookmark_id, note):
        self._exec("UPDATE bookmarks SET note=? WHERE id=?", (note, bookmark_id))

    def delete_bookmark(self, bookmark_id):
        self._exec("DELETE FROM bookmarks WHERE id=?", (bookmark_id,))

    # ------------------------------------------------------------ statistics
    def add_listening(self, book_id, seconds, day=None):
        day = day or time.strftime("%Y-%m-%d")
        self._exec("INSERT INTO listening(day, book_id, seconds) VALUES(?, ?, ?) "
                   "ON CONFLICT(day, book_id) DO UPDATE SET seconds = seconds + excluded.seconds",
                   (day, book_id, seconds))

    def listening_by_day(self, since_day):
        return {r["day"]: r["s"] for r in self._query(
            "SELECT day, SUM(seconds) AS s FROM listening WHERE day >= ? GROUP BY day", (since_day,))}

    def listening_days(self):
        return [r["day"] for r in self._query("SELECT DISTINCT day FROM listening WHERE seconds >= 60 ORDER BY day")]

    def listening_total(self):
        rows = self._query("SELECT COALESCE(SUM(seconds), 0) AS s FROM listening")
        return rows[0]["s"]

    def listening_by_book(self, limit=5):
        return [(r["book_id"], r["title"], r["s"]) for r in self._query(
            "SELECT l.book_id, COALESCE(b.title, 'Removed book') AS title, SUM(l.seconds) AS s "
            "FROM listening l LEFT JOIN books b ON b.id = l.book_id "
            "GROUP BY l.book_id HAVING s >= 60 ORDER BY s DESC LIMIT ?", (limit,))]

    def finished_count(self):
        return self._query("SELECT COUNT(*) AS n FROM progress WHERE finished=1")[0]["n"]

    # -------------------------------------------------------------- progress
    def set_last_played(self, book_id, when):
        with self._lock:
            self._conn.execute("UPDATE progress SET last_played=? WHERE book_id=?", (when, book_id))
            self._conn.commit()

    def save_progress(self, book_id, position, finished=None, touch=True):
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO progress(book_id) VALUES(?)", (book_id,))
            self._conn.execute("UPDATE progress SET position=? WHERE book_id=?", (position, book_id))
            if touch:
                self._conn.execute("UPDATE progress SET last_played=? WHERE book_id=?", (time.time(), book_id))
            if finished is not None:
                self._conn.execute("UPDATE progress SET finished=? WHERE book_id=?", (int(finished), book_id))
            self._conn.commit()
