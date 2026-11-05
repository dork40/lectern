"""Read QuickTime-style chapter tracks from MP4/M4B files.

Many audiobook tools write chapters as a text track referenced from the audio
track through a ``tref/chap`` atom instead of (or as well as) the Nero
``chpl`` atom that mutagen understands. This is a small read-only parser for
that layout.
"""
import struct

_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"tref", b"udta", b"edts", b"dinf"}


def _atoms(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr:
            return
        yield kind, pos + hdr, pos + size
        pos += size


def _find(f, start, end, kind):
    for k, s, e in _atoms(f, start, end):
        if k == kind:
            return s, e
    return None


def _path(f, start, end, *kinds):
    span = (start, end)
    for kind in kinds:
        span = _find(f, span[0], span[1], kind)
        if span is None:
            return None
    return span


def _read(f, span):
    f.seek(span[0])
    return f.read(span[1] - span[0])


def _parse_trak(f, s, e):
    info = {}
    tkhd = _find(f, s, e, b"tkhd")
    if tkhd:
        data = _read(f, tkhd)
        version = data[0]
        info["id"] = struct.unpack(">I", data[12:16] if version == 0 else data[20:24])[0]
    chap = _path(f, s, e, b"tref", b"chap")
    if chap:
        data = _read(f, chap)
        info["chap"] = [struct.unpack(">I", data[i:i + 4])[0] for i in range(0, len(data) - 3, 4)]
    mdhd = _path(f, s, e, b"mdia", b"mdhd")
    if mdhd:
        data = _read(f, mdhd)
        if data[0] == 1:
            info["timescale"] = struct.unpack(">I", data[20:24])[0]
        else:
            info["timescale"] = struct.unpack(">I", data[12:16])[0]
    stbl = _path(f, s, e, b"mdia", b"minf", b"stbl")
    if stbl:
        info["stbl"] = stbl
    return info


def _sample_table(f, stbl):
    s, e = stbl
    stts = _read(f, _find(f, s, e, b"stts"))
    durations = []
    count = struct.unpack(">I", stts[4:8])[0]
    for i in range(count):
        n, delta = struct.unpack(">II", stts[8 + i * 8:16 + i * 8])
        durations.extend([delta] * n)

    stsz = _read(f, _find(f, s, e, b"stsz"))
    fixed, n = struct.unpack(">II", stsz[4:12])
    sizes = [fixed] * n if fixed else list(struct.unpack(f">{n}I", stsz[12:12 + 4 * n]))

    co = _find(f, s, e, b"stco")
    if co:
        data = _read(f, co)
        n = struct.unpack(">I", data[4:8])[0]
        offsets = list(struct.unpack(f">{n}I", data[8:8 + 4 * n]))
    else:
        data = _read(f, _find(f, s, e, b"co64"))
        n = struct.unpack(">I", data[4:8])[0]
        offsets = list(struct.unpack(f">{n}Q", data[8:8 + 8 * n]))

    stsc = _read(f, _find(f, s, e, b"stsc"))
    n = struct.unpack(">I", stsc[4:8])[0]
    runs = [struct.unpack(">III", stsc[8 + i * 12:20 + i * 12]) for i in range(n)]

    # Expand chunk offsets into per-sample file offsets.
    sample_offsets = []
    sample = 0
    for ci, chunk_off in enumerate(offsets):
        chunk_no = ci + 1
        per_chunk = 1
        for first, spc, _desc in runs:
            if first <= chunk_no:
                per_chunk = spc
        off = chunk_off
        for _ in range(per_chunk):
            if sample >= len(sizes):
                break
            sample_offsets.append(off)
            off += sizes[sample]
            sample += 1
    return durations, sizes, sample_offsets


def _decode_text(raw):
    if len(raw) < 2:
        return ""
    length = struct.unpack(">H", raw[:2])[0]
    text = raw[2:2 + length]
    if text.startswith(b"\xfe\xff") or text.startswith(b"\xff\xfe"):
        return text.decode("utf-16", "replace").strip()
    return text.decode("utf-8", "replace").strip("\x00").strip()


def read_qt_chapters(path):
    """Return [(start_seconds, title), ...] or [] if the file has no chapter track."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            moov = _find(f, 0, size, b"moov")
            if not moov:
                return []
            traks = [_parse_trak(f, s, e) for k, s, e in _atoms(f, *moov) if k == b"trak"]
            chap_ids = {cid for t in traks for cid in t.get("chap", [])}
            for t in traks:
                if t.get("id") in chap_ids and "stbl" in t and t.get("timescale"):
                    durations, sizes, offsets = _sample_table(f, t["stbl"])
                    chapters = []
                    time = 0
                    for dur, sz, off in zip(durations, sizes, offsets):
                        f.seek(off)
                        chapters.append((time / t["timescale"], _decode_text(f.read(sz))))
                        time += dur
                    return chapters
    except (OSError, struct.error, TypeError, IndexError):
        return []
    return []
