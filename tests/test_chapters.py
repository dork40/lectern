import struct

import pytest
from mutagen.id3 import CHAP, CTOC, ID3, TIT2, CTOCFlags

from lectern.metadata import clean_chapters, parse_id3_chapters, parse_timestamp, parse_vorbis_chapters
from lectern.models import Book, Chapter, Track
from lectern.mp4chapters import read_qt_chapters


@pytest.mark.parametrize("text,expected", [
    ("00:00:00.000", 0.0), ("01:02:03.500", 3723.5), ("02:03", 123.0), ("12.25", 12.25),
    ("00:10:00,5", 600.5), ("nonsense", None), ("", None),
])
def test_parse_timestamp(text, expected):
    assert parse_timestamp(text) == expected


def test_vorbis_chapters_sorted_and_named():
    tags = {
        "CHAPTER002": ["00:10:00.000"], "CHAPTER002NAME": ["Two"],
        "chapter001": ["00:00:00.000"], "chapter001name": ["One"],
        "CHAPTER003": ["00:20:00.000"],            # no NAME tag
        "TITLE": ["Book"],
    }
    assert parse_vorbis_chapters(tags) == [(0.0, "One"), (600.0, "Two"), (1200.0, "Chapter 3")]


def test_id3_chapters():
    id3 = ID3()
    id3.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED, child_element_ids=["c1", "c0"]))
    id3.add(CHAP(element_id="c1", start_time=30000, end_time=60000, sub_frames=[TIT2(text=["Second"])]))
    id3.add(CHAP(element_id="c0", start_time=0, end_time=30000, sub_frames=[TIT2(text=["First"])]))
    id3.add(CHAP(element_id="c2", start_time=60000, end_time=90000, sub_frames=[]))
    assert parse_id3_chapters(id3) == [(0.0, "First"), (30.0, "Second"), (60.0, "Chapter 3")]


def test_clean_chapters():
    raw = [(100.0, "B"), (0.0, "A"), (100.0, "dup"), (5000.0, "past the end"), (200.0, " ")]
    assert clean_chapters(raw, duration=300) == [(0.0, "A"), (100.0, "B"), (200.0, "Chapter 3")]
    # chapters not starting at zero get an introduction
    assert clean_chapters([(12.0, "One")], duration=100) == [(0.0, "Introduction"), (12.0, "One")]


# --- QuickTime chapter track (hand-built MP4, no ffmpeg needed) ---

def atom(kind, payload=b""):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def full(kind, payload):
    return atom(kind, b"\0\0\0\0" + payload)


def build_m4b(titles, durations_ms):
    samples = [struct.pack(">H", len(t.encode())) + t.encode() for t in titles]
    ftyp = atom(b"ftyp", b"M4B \0\0\0\0")
    mdat_offset = len(ftyp) + 8
    offsets, pos = [], mdat_offset
    for s in samples:
        offsets.append(pos)
        pos += len(s)
    mdat = atom(b"mdat", b"".join(samples))

    def tkhd(tid):
        return full(b"tkhd", b"\0" * 8 + struct.pack(">I", tid) + b"\0" * 68)

    def mdhd(scale):
        return full(b"mdhd", b"\0" * 8 + struct.pack(">II", scale, sum(durations_ms)) + b"\0" * 4)

    audio = atom(b"trak", tkhd(1) + atom(b"tref", atom(b"chap", struct.pack(">I", 2))) +
                 atom(b"mdia", mdhd(44100)))
    stbl = atom(b"stbl",
                full(b"stts", struct.pack(">I", len(durations_ms)) +
                     b"".join(struct.pack(">II", 1, d) for d in durations_ms)) +
                full(b"stsz", struct.pack(">II", 0, len(samples)) +
                     b"".join(struct.pack(">I", len(s)) for s in samples)) +
                full(b"stsc", struct.pack(">I", 1) + struct.pack(">III", 1, 1, 1)) +
                full(b"stco", struct.pack(">I", len(offsets)) + b"".join(struct.pack(">I", o) for o in offsets)))
    text = atom(b"trak", tkhd(2) + atom(b"mdia", mdhd(1000) + atom(b"minf", stbl)))
    return ftyp + mdat + atom(b"moov", audio + text)


def test_qt_chapter_track(tmp_path):
    f = tmp_path / "book.m4b"
    f.write_bytes(build_m4b(["Opening", "Chapter 1", "Ünïcode"], [40000, 60000, 50000]))
    assert read_qt_chapters(str(f)) == [(0.0, "Opening"), (40.0, "Chapter 1"), (100.0, "Ünïcode")]


def test_qt_chapters_absent_or_garbage(tmp_path):
    f = tmp_path / "x.m4b"
    f.write_bytes(b"not an mp4 at all")
    assert read_qt_chapters(str(f)) == []
    assert read_qt_chapters(str(tmp_path / "missing.m4b")) == []


# --- global timeline over multi-file books ---

def make_book():
    tracks = [Track("a.mp3", 100, 0), Track("b.mp3", 50, 100), Track("c.mp3", 70, 150)]
    chapters = [Chapter("A", 0, 100), Chapter("B", 100, 150), Chapter("C", 150, 220)]
    return Book(1, "/x", "folder", "Book", duration=220, tracks=tracks, chapters=chapters)


@pytest.mark.parametrize("pos,expected", [(0, (0, 0)), (99.5, (0, 99.5)), (100, (1, 0)), (160, (2, 10)),
                                          (500, (2, 350))])
def test_locate(pos, expected):
    assert make_book().locate(pos) == expected


@pytest.mark.parametrize("pos,idx", [(0, 0), (99.9, 0), (100, 1), (149, 1), (150, 2), (219, 2)])
def test_chapter_index(pos, idx):
    assert make_book().chapter_index(pos) == idx
