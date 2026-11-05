"""Find books on disk, read their metadata into the database, watch for changes."""
import hashlib
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from gi.repository import Gio, GLib, GObject

from . import metadata
from .const import AUDIO_EXTS, COVER_CACHE
from .models import Chapter, Track
from .util import natural_key

log = logging.getLogger(__name__)

_disc_dir = re.compile(r"^(?:cd|dis[ck]|part|vol(?:ume)?)\s*[-_.]?\s*\d+\b.*$", re.I)


@dataclass
class Candidate:
    path: str
    kind: str                         # "file" | "folder"
    files: list = field(default_factory=list)
    root: str = ""
    exclusive_dir: bool = True        # folder holds only this book (for local cover lookup)


def _audio_in(directory, names):
    return sorted((n for n in names if os.path.splitext(n)[1].lower() in AUDIO_EXTS
                   and not n.startswith(".")), key=natural_key)


_numbered = re.compile(r"^\s*(?:\d{1,4}\b|\d{1,4}[-_.)]|(?:part|pt|chapter|chap|ch|track|disc|cd|side)"
                       r"\s*[-_.]?\s*\d+)", re.I)
_album_cache = {}


def _is_standalone(path):
    return path.lower().endswith((".m4b", ".m4a"))


def looks_like_parts(paths):
    """True if file names look like the numbered parts of a single book."""
    stems = [Path(p).stem for p in paths]
    if all(_numbered.match(s) for s in stems):
        return True
    # "Dune 01", "Dune 02" / "Dune - Part 1", "Dune - Part 2"
    prefix = os.path.commonprefix([s.casefold() for s in stems])
    if len(prefix.strip(" -_.")) >= 3 and all(re.search(r"\d", s[len(prefix):]) for s in stems):
        return True
    return False


def album_of(path):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    key = (path, mtime)
    if key not in _album_cache:
        tags = metadata.read_file(path).tags
        _album_cache[key] = (tags.get("album") or "").strip().casefold()
    return _album_cache[key]


def group_files(files, album=album_of):
    """Split the audio files of one folder into books (lists of files).

    * .m4b/.m4a files are always books of their own
    * numbered names ("01 ...", "Part 2", "Dune 03") are the parts of one book
    * otherwise files sharing an album tag form one book; anything else is a
      single-file book (e.g. Author/Book One.mp3, Author/Book Two.mp3)
    """
    books = [[f] for f in files if _is_standalone(f)]
    rest = [f for f in files if not _is_standalone(f)]
    if len(rest) == 1 or (rest and looks_like_parts(rest)):
        return books + [rest]
    by_album = {}
    for f in rest:
        by_album.setdefault(album(f), []).append(f)
    for key, group in by_album.items():
        if key and len(group) > 1:
            books.append(group)
        else:
            books.extend([f] for f in group)
    return books


def discover(root):
    """Walk ``root`` and return a Candidate per book (see group_files for the rules).

    CD1/Disc 2/... subfolders are merged into their parent folder's book.
    """
    root = str(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted((d for d in dirnames if not d.startswith(".")), key=natural_key)
        audio = _audio_in(dirpath, filenames)

        # Merge disc subfolders into this folder's book.
        disc_files = []
        for d in list(dirnames):
            if _disc_dir.match(d):
                sub = os.path.join(dirpath, d)
                try:
                    sub_audio = _audio_in(sub, os.listdir(sub))
                except OSError:
                    continue
                if sub_audio:
                    disc_files += [os.path.join(sub, n) for n in sub_audio]
                    dirnames.remove(d)
        files = [os.path.join(dirpath, n) for n in audio]
        if not files and not disc_files:
            continue

        groups = group_files(files) if files else []
        if disc_files:
            multi = [g for g in groups if len(g) > 1 or not _is_standalone(g[0])]
            if multi:
                multi[0].extend(disc_files)
            else:
                groups.append(disc_files)
        is_root = os.path.normpath(dirpath) == os.path.normpath(root)
        alone = len(groups) == 1 and not is_root
        folder_groups = [g for g in groups if len(g) > 1]
        for g in groups:
            if len(g) == 1:
                out.append(Candidate(g[0], "file", g, root, exclusive_dir=alone and not dirnames))
            else:
                # A second multi-file book in the same folder needs a distinct identity.
                path = dirpath if g is folder_groups[0] else f"{dirpath}::{album_of(g[0]) or len(out)}"
                out.append(Candidate(path, "folder", g, root, exclusive_dir=alone))
    return out


def signature(files):
    h = hashlib.sha1()
    for f in files:
        try:
            st = os.stat(f)
        except OSError:
            continue
        h.update(f"{f}|{st.st_size}|{int(st.st_mtime)}\n".encode())
    return h.hexdigest()


def fingerprint(files):
    """Identity that survives renames/moves: the sizes of the audio files."""
    sizes = []
    for f in files:
        try:
            sizes.append(os.path.getsize(f))
        except OSError:
            pass
    return hashlib.sha1(",".join(map(str, sizes)).encode()).hexdigest()


def cover_cache_path(book_path, ext=".jpg"):
    return str(COVER_CACHE / (hashlib.sha1(book_path.encode()).hexdigest() + ext))


def build_book(c: Candidate):
    """Read all tags for a candidate and return the dict stored by Database.upsert_book."""
    infos = [metadata.read_file(f) for f in c.files]
    guess = metadata.guess_from_path(c.path.split("::")[0], c.root)

    def tag(*keys):
        for k in keys:
            for i in infos:
                if i.tags.get(k):
                    return str(i.tags[k])
        return None

    if c.kind == "file":
        title = tag("title", "album")
        author = tag("artist", "albumartist")
    else:
        title = tag("album")
        author = tag("albumartist", "artist")
    title = title or guess.get("title") or Path(c.path).stem
    title, suffix_series, suffix_index = metadata.split_series_suffix(title)
    author = author or guess.get("author") or ""
    narrator = tag("narrator") or guess.get("narrator") or ""
    composer = tag("composer")
    if not narrator and composer and composer.casefold() != author.casefold():
        narrator = composer

    tracks, chapters, offset = [], [], 0.0
    for i in infos:
        stem = Path(i.path).stem
        track_title = i.tags.get("title")
        if not track_title or (c.kind == "folder" and track_title == title):
            track_title = metadata.clean_track_title(stem)
        tracks.append(Track(path=i.path, duration=i.duration, offset=offset, title=track_title))
        if len(i.chapters) > 1:
            for start, ch_title in i.chapters:
                chapters.append([ch_title, offset + start])
        else:
            chapters.append([track_title if c.kind == "folder" else (i.chapters[0][1] if i.chapters else title),
                             offset])
        offset += i.duration
    chapter_objs = []
    for idx, (ch_title, start) in enumerate(chapters):
        end = chapters[idx + 1][1] if idx + 1 < len(chapters) else offset
        if end - start > 0.05 or idx == len(chapters) - 1:
            chapter_objs.append(Chapter(title=ch_title, start=start, end=end))

    data = {
        "path": c.path, "root": c.root, "kind": c.kind, "title": title, "author": author,
        "narrator": narrator,
        "series": tag("series") or suffix_series or guess.get("series") or "",
        "series_index": tag("series_index") or suffix_index or guess.get("series_index") or "",
        "year": (tag("year") or guess.get("year") or "")[:4],
        "duration": offset, "tracks": tracks, "chapters": chapter_objs,
        "signature": signature(c.files),
        "fingerprint": fingerprint(c.files),
    }
    data.update(_find_cover(c, infos))
    return data


def _find_cover(c, infos):
    folder = os.path.dirname(c.files[0]) if c.kind == "file" else c.path.split("::")[0]
    stem = None if c.kind == "folder" else Path(c.path).stem
    local = metadata.find_local_cover(folder, stem=stem, exclusive=c.exclusive_dir)
    if local:
        return {"cover_path": local, "cover_source": "local"}
    for i in infos:
        if not i.has_cover:
            continue
        found = metadata.extract_cover(i.path)
        if found:
            data, mime = found
            dest = cover_cache_path(c.path, ".png" if mime == "image/png" else ".jpg")
            try:
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(data)
                return {"cover_path": dest, "cover_source": "embedded"}
            except OSError as e:
                log.warning("cannot cache cover: %s", e)
            break
    return {"cover_path": None, "cover_source": None}


OPENED_ROOT = ""   # root of books opened from a file manager, outside the library folders


def candidate_for(path, roots):
    """The Candidate (book) a file or folder opened from outside belongs to, or None."""
    path = os.path.realpath(path)
    root = next((r for r in roots if path == r or path.startswith(r.rstrip("/") + "/")), None)
    if os.path.isdir(path):
        found = discover(path)
        for c in found:
            c.root = root or os.path.dirname(path)
        # a folder book itself, or the first book inside it
        return next((c for c in found if c.path == path), found[0] if found else None)
    if os.path.splitext(path)[1].lower() not in AUDIO_EXTS:
        return None
    directory = os.path.dirname(path)
    files = [os.path.join(directory, n) for n in _audio_in(directory, os.listdir(directory))]
    for group in group_files(files):
        if path in group:
            if len(group) == 1:
                return Candidate(path, "file", group, root or directory, exclusive_dir=len(files) == 1)
            return Candidate(directory, "folder", group, root or os.path.dirname(directory))
    return Candidate(path, "file", [path], root or directory, exclusive_dir=False)


class Scanner(GObject.Object):
    """Runs library scans on a worker thread and reports back on the main loop."""

    __gsignals__ = {
        "started": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        "finished": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, db):
        super().__init__()
        self.db = db
        self._thread = None
        self._again = False

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def scan(self, full=False):
        if self.running:
            self._again = True
            return
        self._again = False
        self.emit("started")
        self._thread = threading.Thread(target=self._run, args=(full,), daemon=True)
        self._thread.start()

    def _progress(self, done, total):
        GLib.idle_add(lambda: (self.emit("progress", done, total), False)[1])

    def _run(self, full):
        stats = {"added": [], "updated": 0, "removed": 0, "errors": 0}
        try:
            roots = self.db.folders()
            # books opened directly ("Open With") have no library root; drop them once their files go
            missing = [i for path, (i, _) in self.db.known_books(OPENED_ROOT).items()
                       if not os.path.exists(path.split("::")[0])]
            if missing:
                self.db.delete_books(missing)
                stats["removed"] += len(missing)
            for stale in set(self.db.roots()) - set(roots) - {OPENED_ROOT}:
                ids = [i for i, _ in self.db.known_books(stale).values()]
                self.db.delete_books(ids)
                stats["removed"] += len(ids)
            for root in roots:
                self._scan_root(root, full, stats)
        except Exception:  # noqa: BLE001
            log.exception("library scan failed")
            stats["errors"] += 1
        GLib.idle_add(self._done, stats)

    def _scan_root(self, root, full, stats):
        if not os.path.isdir(root):
            log.warning("library folder %s is not available; keeping its books", root)
            return
        candidates = discover(root)
        known = self.db.known_books(root)
        found = set()
        total = len(candidates)
        new_ids = {}
        for n, c in enumerate(candidates, 1):
            found.add(c.path)
            old = known.get(c.path)
            if old and not full and old[1] == signature(c.files):
                self._progress(n, total)
                continue
            try:
                data = build_book(c)
                book_id = self.db.upsert_book(data)
                if old:
                    stats["updated"] += 1
                else:
                    new_ids[data["fingerprint"]] = book_id
                    stats["added"].append(book_id)
            except Exception:  # noqa: BLE001
                log.exception("failed to import %s", c.path)
                stats["errors"] += 1
            self._progress(n, total)

        gone = {path: i for path, (i, _) in known.items() if path not in found}
        if gone:
            fps = self.db.fingerprints(list(gone.values()))
            for old_id in gone.values():
                new_id = new_ids.get(fps.get(old_id))
                if new_id:
                    log.info("book %s moved; keeping its progress", old_id)
                    self.db.adopt(old_id, new_id)
                    stats["added"].remove(new_id)
                    stats["updated"] += 1
                else:
                    self.db.delete_books([old_id])
                    stats["removed"] += 1

    def _done(self, stats):
        log.info("scan finished: %d added, %d updated, %d removed",
                 len(stats["added"]), stats["updated"], stats["removed"])
        self.emit("finished", stats)
        if self._again:
            self.scan()
        return False


class LibraryWatcher(GObject.Object):
    """Recursive Gio.FileMonitor wrapper that emits a debounced 'changed' signal."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}
    DEBOUNCE_MS = 2500
    MAX_DIRS = 5000

    def __init__(self):
        super().__init__()
        self._monitors = {}
        self._timeout = 0

    def watch(self, roots):
        for m in self._monitors.values():
            m.cancel()
        self._monitors.clear()
        log.debug("watching %s", roots)
        for root in roots:
            for dirpath, dirnames, _ in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                self._add(dirpath)
                if len(self._monitors) >= self.MAX_DIRS:
                    log.warning("not watching more than %d folders", self.MAX_DIRS)
                    return

    def _add(self, path):
        try:
            mon = Gio.File.new_for_path(path).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error as e:
            log.debug("cannot watch %s: %s", path, e.message)
            return
        mon.connect("changed", self._on_changed)
        self._monitors[path] = mon

    def _on_changed(self, _mon, file, other, event):
        relevant = (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.DELETED,
                    Gio.FileMonitorEvent.MOVED_IN, Gio.FileMonitorEvent.MOVED_OUT,
                    Gio.FileMonitorEvent.RENAMED, Gio.FileMonitorEvent.CHANGES_DONE_HINT)
        if event not in relevant:
            return
        name = file.get_basename() or ""
        log.debug("watch event %s on %s", event.value_nick, file.get_path())
        if name.startswith("."):
            return
        if event in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.MOVED_IN) and os.path.isdir(file.get_path() or ""):
            self._add(file.get_path())
        if self._timeout:
            GLib.source_remove(self._timeout)
        self._timeout = GLib.timeout_add(self.DEBOUNCE_MS, self._fire)

    def _fire(self):
        self._timeout = 0
        log.info("library folders changed; rescanning")
        self.emit("changed")
        return False
