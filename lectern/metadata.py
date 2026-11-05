"""Tag, chapter and cover reading (and optional writing) via mutagen."""
import base64
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TXXX, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from .const import COVER_NAMES, IMAGE_EXTS
from .mp4chapters import read_qt_chapters
from .util import natural_key

log = logging.getLogger(__name__)

TAG_KEYS = ("title", "album", "artist", "albumartist", "composer", "narrator",
            "series", "series_index", "year", "genre", "track", "disc")


@dataclass
class FileInfo:
    path: str
    duration: float = 0.0
    tags: dict = field(default_factory=dict)
    chapters: list = field(default_factory=list)  # [(start_seconds, title)]
    has_cover: bool = False


# --------------------------------------------------------------------------- #
# Chapter parsing helpers (pure functions, unit tested)
# --------------------------------------------------------------------------- #

_ts = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:[.,]\d+)?)$")


def parse_timestamp(text):
    """'01:02:03.500' / '02:03' / '123.5' -> seconds (float) or None."""
    text = (text or "").strip()
    m = _ts.match(text)
    if m:
        h, mnt, s = m.groups()
        return int(h or 0) * 3600 + int(mnt) * 60 + float(s.replace(",", "."))
    try:
        return float(text)
    except ValueError:
        return None


def parse_vorbis_chapters(comments):
    """Parse CHAPTERxxx / CHAPTERxxxNAME vorbis comments.

    ``comments`` maps (case-insensitive) keys to lists of values.
    """
    norm = {k.upper(): (v[0] if isinstance(v, (list, tuple)) else v) for k, v in comments.items() if v}
    chapters = []
    for key, value in norm.items():
        m = re.fullmatch(r"CHAPTER(\d+)", key)
        if not m:
            continue
        start = parse_timestamp(value)
        if start is None:
            continue
        title = norm.get(f"{key}NAME") or f"Chapter {int(m.group(1))}"
        chapters.append((start, title))
    return sorted(chapters)


def parse_id3_chapters(id3):
    """Return [(start, title)] from ID3 CHAP frames, honouring a CTOC order if present."""
    chaps = {f.element_id: f for f in id3.getall("CHAP")}
    if not chaps:
        return []
    result = []
    for i, frame in enumerate(sorted(chaps.values(), key=lambda f: f.start_time)):
        title = None
        tit2 = frame.sub_frames.get("TIT2") if frame.sub_frames else None
        if tit2 and tit2.text:
            title = str(tit2.text[0])
        result.append((frame.start_time / 1000.0, title or f"Chapter {i + 1}"))
    return result


def clean_chapters(chapters, duration):
    """Sort, drop duplicates / out-of-range starts and ensure the first starts at 0."""
    out = []
    for start, title in sorted(chapters, key=lambda c: c[0]):
        if duration and start >= duration:
            continue
        if out and abs(out[-1][0] - start) < 0.01:
            continue
        out.append((max(0.0, float(start)), (title or "").strip() or f"Chapter {len(out) + 1}"))
    if out and out[0][0] > 0.5:
        out.insert(0, (0.0, "Introduction"))
    elif out:
        out[0] = (0.0, out[0][1])
    return out


# --------------------------------------------------------------------------- #
# Name parsing (pure)
# --------------------------------------------------------------------------- #

_junk = re.compile(r"\s*[\(\[](?:unabridged|abridged|audiobook|mp3|m4b|\d+\s*k(?:bps)?|retail)[\)\]]", re.I)
_year = re.compile(r"\s*[\(\[]((?:19|20)\d\d)[\)\]]")
_book_num = re.compile(r"^(?:book|vol(?:ume)?|part|#)\.?\s*(\d+(?:\.\d+)?)\s*(?:[-:._]\s*|\s+)(.+)$", re.I)
_lead_num = re.compile(r"^(\d{1,3}(?:\.\d+)?)\s*(?:[-._:)]\s*|\s+-\s+)(.+)$|^(0\d)\s+(\D.*)$")
_narrated = re.compile(r"\s*[\(\[]?\s*(?:narrated|read)\s+by\s+([^\)\]]+)[\)\]]?\s*$", re.I)
_series_num = re.compile(r"^(.*?)[\s,]*(?:book|vol(?:ume)?|#)?\s*(\d+(?:\.\d+)?)$", re.I)


def parse_name(name):
    """Pull author / title / series info out of a file or folder name.

    Handles e.g. 'Author - Title', 'Author - Series 3 - Title', 'Book 3 - Title',
    '03 - Title', 'Title (Narrated by X)', 'Title (2019)'.
    """
    info = {}
    name = name.replace("_", " ").strip()
    m = _year.search(name)
    if m:
        info["year"] = m.group(1)
        name = _year.sub("", name)
    name = _junk.sub("", name)
    m = _narrated.search(name)
    if m:
        info["narrator"] = m.group(1).strip()
        name = name[:m.start()]
    name = re.sub(r"\s+", " ", name).strip(" -")

    split = _split_number(name)
    if split:
        info["series_index"], name = split

    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", name) if p.strip()]
    if len(parts) >= 3:
        info["author"] = parts[0]
        middle = " - ".join(parts[1:-1])
        sm = _series_num.match(middle)
        if sm and sm.group(1).strip():
            info["series"] = sm.group(1).strip(" ,#")
            info["series_index"] = _fmt_index(sm.group(2))
        elif _book_num.match(middle + " x"):
            info["series_index"] = _fmt_index(_book_num.match(middle + " x").group(1))
        else:
            info["series"] = middle
        info["title"] = parts[-1]
    elif len(parts) == 2:
        first, second = parts
        bm = _book_num.match(f"{second} x") if re.fullmatch(r"(?:book|vol(?:ume)?|#)\.?\s*\d+(?:\.\d+)?", second, re.I) else None
        if bm:
            info["series"] = first
            info["series_index"] = _fmt_index(bm.group(1))
            info["title"] = first
        else:
            info["author"], info["title"] = first, second
    else:
        info["title"] = name
    if "title" in info:
        split = _split_number(info["title"])
        if split and "series_index" not in info:
            info["series_index"], info["title"] = split
    return info


_series_suffix = re.compile(
    r"\s*[\(\[]\s*([^()\[\]]+?)[,:]?\s+(?:book|volume|vol\.?|part|#)\s*(\d+(?:\.\d+)?)\s*[\)\]]\s*$", re.I)


def split_series_suffix(title):
    """'Title: Subtitle (Some Series, Book 9)' -> ('Title: Subtitle', 'Some Series', '9').

    Audible-style titles carry the series in a trailing parenthesis; returns
    (title, None, None) when there is none.
    """
    m = _series_suffix.search(title or "")
    if not m or m.start() == 0:
        return title, None, None
    return title[:m.start()].rstrip(" :-"), m.group(1).strip(), _fmt_index(m.group(2))


def _split_number(text):
    """'Book 3 - Title' / '03 - Title' / '03 Title' -> ('3', 'Title') or None."""
    m = _book_num.match(text) or _lead_num.match(text)
    if not m:
        return None
    num, rest = (g for g in m.groups() if g is not None)
    return _fmt_index(num), rest.strip()


def _fmt_index(s):
    try:
        f = float(s)
    except ValueError:
        return s
    return str(int(f)) if f.is_integer() else str(f)


_generic_dirs = re.compile(r"^(?:audiobooks?|books|library|media|new|unsorted|downloads?)$", re.I)


def guess_from_path(book_path, root):
    """Combine name parsing with the Author/Series/Book folder hierarchy."""
    book_path, root = Path(book_path), Path(root)
    try:
        rel = book_path.relative_to(root)
    except ValueError:
        rel = Path(book_path.name)
    name = book_path.stem if book_path.is_file() or book_path.suffix.lower() in {".m4b", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".oga"} else book_path.name
    info = parse_name(name)
    parents = [p for p in rel.parts[:-1] if not _generic_dirs.match(p)]
    if parents:
        if "author" not in info:
            info["author"] = parents[0]
        if len(parents) >= 2 and "series" not in info:
            series_dir = parse_name(parents[1])
            info["series"] = series_dir.get("title", parents[1])
    return info


def clean_track_title(stem):
    """'01 - Intro' -> 'Intro'; 'Part 3' stays 'Part 3'."""
    m = re.match(r"^\d{1,3}\s*[-._)]\s*(.+)$", stem.replace("_", " "))
    return (m.group(1) if m else stem.replace("_", " ")).strip()


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _first(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if isinstance(v, MP4FreeForm) or isinstance(v, bytes):
        v = bytes(v).decode("utf-8", "replace")
    if isinstance(v, tuple):
        v = v[0]
    v = str(v).strip() if v is not None else None
    return v or None


def _mp4_tags(f):
    t = f.tags or {}
    g = lambda k: _first(t.get(k))  # noqa: E731
    ff = lambda name: _first(t.get(f"----:com.apple.iTunes:{name}"))  # noqa: E731
    track = t.get("trkn")
    return {
        "title": g("\xa9nam"), "album": g("\xa9alb"), "artist": g("\xa9ART"),
        "albumartist": g("aART"), "composer": g("\xa9wrt"),
        "narrator": g("\xa9nrt") or ff("NARRATOR") or ff("narrator"),
        "series": ff("SERIES") or g("\xa9mvn") or ff("series"),
        "series_index": ff("SERIES-PART") or ff("series-part") or (str(t["\xa9mvi"][0]) if t.get("\xa9mvi") else None),
        "year": g("\xa9day"), "genre": g("\xa9gen"),
        "track": track[0][0] if track else None,
    }


def _id3_tags(id3):
    def txt(fid):
        fr = id3.get(fid)
        return _first(fr.text) if fr is not None and getattr(fr, "text", None) else None

    def txxx(*names):
        for frame in id3.getall("TXXX"):
            if frame.desc.upper() in names and frame.text:
                return _first(frame.text)
        return None
    track = txt("TRCK")
    return {
        "title": txt("TIT2"), "album": txt("TALB"), "artist": txt("TPE1"),
        "albumartist": txt("TPE2"), "composer": txt("TCOM"),
        "narrator": txxx("NARRATOR", "NARRATEDBY", "READER"),
        "series": txxx("SERIES") or txt("MVNM"),
        "series_index": txxx("SERIES-PART", "SERIES_PART", "SERIESPART") or txt("MVIN"),
        "year": txt("TDRC") or txt("TYER"), "genre": txt("TCON"),
        "track": int(re.match(r"\d+", track).group()) if track and re.match(r"\d+", track) else None,
    }


def _vorbis_tags(f):
    t = {k.lower(): v for k, v in (f.tags or {}).items()} if f.tags else {}
    g = lambda *ks: next((_first(t.get(k)) for k in ks if t.get(k)), None)  # noqa: E731
    track = g("tracknumber")
    return {
        "title": g("title"), "album": g("album"), "artist": g("artist"),
        "albumartist": g("albumartist", "album artist"), "composer": g("composer"),
        "narrator": g("narrator", "performer", "reader"),
        "series": g("series", "grouping"), "series_index": g("series-part", "seriespart", "series_part"),
        "year": g("date", "year"), "genre": g("genre"),
        "track": int(re.match(r"\d+", track).group()) if track and re.match(r"\d+", track) else None,
    }


def read_file(path):
    """Read duration, tags and chapters of one audio file. Never raises for bad files."""
    info = FileInfo(path=str(path))
    try:
        f = mutagen.File(path)
    except Exception as e:  # noqa: BLE001 - mutagen raises many types
        log.warning("cannot read tags of %s: %s", path, e)
        return info
    if f is None:
        return info
    info.duration = float(getattr(f.info, "length", 0) or 0)
    try:
        if isinstance(f, MP4):
            info.tags = _mp4_tags(f)
            chapters = [(c.start, c.title) for c in (f.chapters or [])] if getattr(f, "chapters", None) else []
            info.chapters = chapters or read_qt_chapters(path)
            info.has_cover = bool(f.tags and f.tags.get("covr"))
        elif f.tags is not None and isinstance(f.tags, ID3):
            info.tags = _id3_tags(f.tags)
            info.chapters = parse_id3_chapters(f.tags)
            info.has_cover = bool(f.tags.getall("APIC"))
        else:
            info.tags = _vorbis_tags(f)
            info.chapters = parse_vorbis_chapters(dict(f.tags or {}))
            pictures = getattr(f, "pictures", None)
            info.has_cover = bool(pictures) or bool(f.tags and f.tags.get("metadata_block_picture"))
    except Exception as e:  # noqa: BLE001
        log.warning("error parsing tags of %s: %s", path, e)
    info.chapters = clean_chapters(info.chapters, info.duration)
    info.tags = {k: v for k, v in info.tags.items() if v not in (None, "")}
    return info


def extract_cover(path):
    """Return (bytes, mime) of the embedded front cover, or None."""
    try:
        f = mutagen.File(path)
    except Exception:  # noqa: BLE001
        return None
    if f is None or f.tags is None:
        return None
    try:
        if isinstance(f, MP4):
            covers = f.tags.get("covr") or []
            if covers:
                c = covers[0]
                mime = "image/png" if c.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
                return bytes(c), mime
        elif isinstance(f.tags, ID3):
            pics = sorted(f.tags.getall("APIC"), key=lambda p: p.type != 3)
            if pics:
                return pics[0].data, pics[0].mime or "image/jpeg"
        else:
            pics = list(getattr(f, "pictures", []) or [])
            for b64 in (f.tags.get("metadata_block_picture") or []):
                try:
                    pics.append(Picture(base64.b64decode(b64)))
                except Exception:  # noqa: BLE001
                    pass
            pics.sort(key=lambda p: p.type != 3)
            if pics:
                return pics[0].data, pics[0].mime or "image/jpeg"
    except Exception as e:  # noqa: BLE001
        log.warning("cannot extract cover from %s: %s", path, e)
    return None


def find_local_cover(directory, stem=None, exclusive=True):
    """Find a cover image next to the audio.

    ``exclusive`` means the folder belongs to a single book, so any image in it
    may be used; otherwise only images named after the book (``stem``) or a
    conventional cover name are accepted.
    """
    try:
        images = [p for p in Path(directory).iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    except OSError:
        return None
    if not images:
        return None
    by_stem = {p.stem.casefold(): p for p in images}
    if stem and stem.casefold() in by_stem:
        return str(by_stem[stem.casefold()])
    if exclusive:
        for name in COVER_NAMES:
            if name in by_stem:
                return str(by_stem[name])
        for p in sorted(images, key=lambda p: natural_key(p.name)):
            if any(n in p.stem.casefold() for n in COVER_NAMES):
                return str(p)
        return str(max(images, key=lambda p: p.stat().st_size))
    return None


# --------------------------------------------------------------------------- #
# Writing (only ever on explicit user request)
# --------------------------------------------------------------------------- #

def embed_cover(path, data, mime="image/jpeg"):
    f = mutagen.File(path)
    if f is None:
        raise ValueError(f"unsupported file: {path}")
    if isinstance(f, MP4):
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        if f.tags is None:
            f.add_tags()
        f.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
        f.save()
    elif path.lower().endswith(".mp3"):
        try:
            id3 = ID3(path)
        except ID3NoHeaderError:
            id3 = ID3()
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        id3.save(path)
    else:
        pic = Picture()
        pic.type, pic.mime, pic.data, pic.desc = 3, mime, data, "Cover"
        if isinstance(f, FLAC):
            f.clear_pictures()
            f.add_picture(pic)
        else:
            f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        f.save()


def write_tags(path, fields, single_file=False):
    """Write title/author/narrator/series/year into one file's tags.

    The book title goes into the album tag; for single-file books it is also
    the track title (for folder books the track titles are chapter names).
    """
    f = mutagen.File(path)
    if f is None:
        raise ValueError(f"unsupported file: {path}")
    if isinstance(f, MP4):
        if f.tags is None:
            f.add_tags()
        m = {"title": "\xa9alb", "author": "\xa9ART", "narrator": "\xa9nrt", "year": "\xa9day"}
        for k, atom in m.items():
            if fields.get(k):
                f.tags[atom] = [fields[k]]
        if fields.get("author"):
            f.tags["aART"] = [fields["author"]]
        if single_file and fields.get("title"):
            f.tags["\xa9nam"] = [fields["title"]]
        if fields.get("series"):
            f.tags["----:com.apple.iTunes:SERIES"] = [MP4FreeForm(fields["series"].encode())]
        if fields.get("series_index"):
            f.tags["----:com.apple.iTunes:SERIES-PART"] = [MP4FreeForm(str(fields["series_index"]).encode())]
        f.save()
    elif path.lower().endswith(".mp3"):
        try:
            id3 = ID3(path)
        except ID3NoHeaderError:
            id3 = ID3()
        if fields.get("title"):
            id3.setall("TALB", [TALB(encoding=3, text=fields["title"])])
            if single_file:
                id3.setall("TIT2", [TIT2(encoding=3, text=fields["title"])])
        if fields.get("author"):
            id3.setall("TPE1", [TPE1(encoding=3, text=fields["author"])])
        if fields.get("year"):
            id3.setall("TDRC", [TDRC(encoding=3, text=fields["year"])])
        for desc, key in (("NARRATOR", "narrator"), ("SERIES", "series"), ("SERIES-PART", "series_index")):
            if fields.get(key):
                id3.delall(f"TXXX:{desc}")
                id3.add(TXXX(encoding=3, desc=desc, text=str(fields[key])))
        id3.save(path)
    else:
        m = {"title": "album", "author": "artist", "narrator": "narrator", "year": "date",
             "series": "series", "series_index": "series-part"}
        for k, tag in m.items():
            if fields.get(k):
                f[tag] = [str(fields[k])]
        if single_file and fields.get("title"):
            f["title"] = [fields["title"]]
        f.save()

