"""AirPlay pairing and helpers. The pairing test talks to pyatv's simulated AirPlay
device (a test-only dependency); it is skipped if pyatv isn't installed."""
import asyncio
import os
import threading

import pytest

from lectern import airplay


def test_tlv_roundtrip_with_fragmentation():
    big = bytes(range(256)) * 2          # > 255 bytes: must be split into fragments
    data = airplay.tlv_write([(6, b"\x01"), (3, big), (7, b"")])
    assert data[:3] == b"\x06\x01\x01" and data[3:5] == b"\x03\xff"
    out = airplay.tlv_read(data)
    assert out[6] == b"\x01" and out[3] == big and out[7] == b""


def test_owntone_identity_and_keys():
    assert airplay.owntone_pairing_id() == "929D3B75E92FEDA6"     # matches OwnTone's C murmur hash
    assert airplay.speaker_id("C7:C1:8D:67:A9:19") == 219634114996505
    seed, ltpk = b"\x01" * 32, b"\x02" * 32
    key = airplay.owntone_auth_key(seed, ltpk)
    assert len(key) == 192 and key.startswith("01" * 32) and key.endswith("02" * 32)


def test_pipe_writer_writes_whole_messages_or_none(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    w = airplay.PipeWriter(str(fifo))
    room = w.free_space()
    assert room >= 200_000                    # a cover picture fits in one go
    assert w.write(b"a" * (room - 10))
    assert not w.write(b"b" * 100, timeout=0.2)   # doesn't fit: dropped whole, never half-written
    assert w.dropped == 100
    r = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    data = b""
    try:
        while chunk := os.read(r, 1 << 16):
            data += chunk
    except BlockingIOError:
        pass
    assert data == b"a" * (room - 10)
    os.close(r)
    w.close()
    assert not w.write(b"late")               # after close: ignored, no error


def test_audio_pipe_paces_playback_without_dropping(tmp_path):
    """fdsink waits while the pipe is full; nothing is lost and pause/stop never hang."""
    import time
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    fifo = tmp_path / "audio"
    os.mkfifo(fifo)
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    pipe = Gst.parse_launch("audiotestsrc num-buffers=100 samplesperbuffer=441 ! "
                            "audio/x-raw,format=S16LE,rate=44100,channels=2 ! fdsink name=s sync=false")
    pipe.get_by_name("s").set_property("fd", fd)
    pipe.set_state(Gst.State.PLAYING)
    time.sleep(0.5)
    ahead = pipe.query_position(Gst.Format.TIME)[1] / Gst.SECOND
    assert ahead < 0.6                        # held back by the full pipe, not racing ahead
    r = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    got, end = 0, time.monotonic() + 5
    while got < 100 * 441 * 4 and time.monotonic() < end:
        try:
            got += len(os.read(r, 1 << 16))
        except BlockingIOError:
            time.sleep(0.01)
    assert got == 100 * 441 * 4               # every sample arrived
    pipe.set_state(Gst.State.NULL)
    airplay.drain_fifo(fd)
    os.close(r)
    os.close(fd)


pyatv = pytest.importorskip("pyatv")


@pytest.fixture
def fake_tv():
    from pyatv.protocols.airplay.server_auth import AirPlayServerAuth, BaseAirPlayServerAuth
    from pyatv.support.http import BasicHttpServer, http_server
    loop = asyncio.new_event_loop()
    started = threading.Event()
    state = {}

    def run():
        asyncio.set_event_loop(loop)
        class FakeTV(AirPlayServerAuth):
            def enable_encryption(self, output_key, input_key):
                pass
        router = FakeTV("Fake TV", pin=1111)

        async def start():
            server, port = await http_server(lambda: BasicHttpServer(router), "127.0.0.1")
            state["port"], state["server"] = port, server
            started.set()
        loop.run_until_complete(start())
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    started.wait(5)
    yield state["port"], BaseAirPlayServerAuth.keys
    loop.call_soon_threadsafe(loop.stop)


def test_pair_setup_against_simulated_device(fake_tv):
    port, server_keys = fake_tv
    p = airplay.Pairing("127.0.0.1", airplay.owntone_pairing_id(), port=port)
    p.begin()
    seed, dev_id, dev_ltpk = p.finish(1111)
    assert len(seed) == 32
    assert dev_id == b"5D797FD3-3538-427E-A47B-A32FC6CF3A6A"
    assert dev_ltpk == server_keys.auth_pub          # the device's real long-term key


def test_wrong_code_is_reported(fake_tv):
    port, _ = fake_tv
    p = airplay.Pairing("127.0.0.1", airplay.owntone_pairing_id(), port=port)
    p.begin()
    with pytest.raises(airplay.PairingError):
        p.finish(2222)


def test_metadata_item_format():
    import base64
    import re
    item = airplay.metadata_item("core", "minm", "83. Gary Goes To Work").decode()
    assert "<type>636f7265</type><code>6d696e6d</code><length>21</length>" in item
    data = re.search(r'<data encoding="base64">\s*(.*?)</data>', item, re.S).group(1)
    assert base64.b64decode(data).decode() == "83. Gary Goes To Work"


def test_cover_sent_once_per_book_and_again_after_resume(tmp_path, monkeypatch):
    """Each picture replaces OwnTone's cover file; sending it again and again
    leaves the TV with a deleted file, so text updates go without it."""
    from types import SimpleNamespace
    from lectern.models import Book, Chapter
    sent = []
    monkeypatch.setattr(airplay, "cover_jpeg", lambda path: b"\xff\xd8jpeg" if path else None)
    monkeypatch.setattr(airplay.AirPlayOutput, "send_metadata", lambda self, delay=0.5: None)
    ot = SimpleNamespace(fifo=tmp_path / "lectern", metadata_fifo=tmp_path / "lectern.metadata",
                         set_cover=lambda path: None)
    os.mkfifo(ot.fifo)
    os.mkfifo(ot.metadata_fifo)
    out = airplay.AirPlayOutput("TV", "00:00:00:00:00:01", ot)
    out.meta_writer = SimpleNamespace(write=lambda data, timeout=5.0: sent.append(data) or True)
    out._meta_pool = SimpleNamespace(submit=lambda fn, *a: fn(*a))
    book = Book(1, "/b.m4b", "file", "Book", author="A", duration=100, cover_path="/c.jpg",
                chapters=[Chapter("One", 0, 50), Chapter("Two", 50, 100)])
    player = SimpleNamespace(book=book, chapter=book.chapters[0])
    out._player = player
    from gi.repository import GLib
    monkeypatch.setattr(GLib, "timeout_add", lambda ms, fn: 0)     # no follow-up text refresh here
    out._send_metadata_now()
    player.chapter = book.chapters[1]
    out._send_metadata_now()
    assert b"50494354" in sent[0] and b"50494354" not in sent[1]       # "PICT" hex code
    out._pict_time -= airplay.PICT_SPACING
    out._pict_book = out._sent_meta = None                             # what on_play does
    out._send_metadata_now()
    assert len(sent) == 3 and b"50494354" in sent[2]
    os.close(out.audio_fd)


def test_sample_rate_and_patch_detection(tmp_path, monkeypatch):
    monkeypatch.delenv("LECTERN_AIRPLAY_RATE", raising=False)
    assert airplay.device_sample_rate("Roku") == 44100          # 48 kHz played wrong on real Roku TVs
    monkeypatch.setenv("LECTERN_AIRPLAY_RATE", "48000")
    assert airplay.device_sample_rate("Roku") == 48000
    stock, patched = tmp_path / "stock", tmp_path / "patched"
    stock.write_bytes(b"\x7fELF...AirPlay...")
    patched.write_bytes(b"\x7fELF...OWNTONE_AIRPLAY_SAMPLE_RATE...")
    assert not airplay.owntone_patched(str(stock)) and airplay.owntone_patched(str(patched))
