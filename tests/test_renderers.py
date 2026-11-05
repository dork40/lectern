"""Roku and DLNA sessions against simulated devices (real HTTP/SOAP, real MP3 stream)."""
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from gi.repository import GLib

from lectern import renderers
from lectern.cast import MediaServer
from lectern.models import Book, Chapter, Track
from lectern.player import Player

SAMPLE = Path(__file__).parent / "data" / "tone.ogg"


@pytest.fixture(scope="module", autouse=True)
def sample_audio():
    """A 6 s audio file made with GStreamer (no fixtures in the repo needed)."""
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    SAMPLE.parent.mkdir(exist_ok=True)
    pipe = Gst.parse_launch(f"audiotestsrc num-buffers=260 samplesperbuffer=1024 ! audio/x-raw,rate=44100 ! "
                            f"audioconvert ! vorbisenc ! oggmux ! filesink location={SAMPLE}")
    pipe.set_state(Gst.State.PLAYING)
    pipe.get_bus().timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipe.set_state(Gst.State.NULL)
    yield
    SAMPLE.unlink(missing_ok=True)


def wait_for(cond, timeout=10.0):
    ctx = GLib.MainContext.default()
    end = time.time() + timeout
    while time.time() < end:
        while ctx.pending():
            ctx.iteration(False)
        if cond():
            return True
        time.sleep(0.05)
    return False


def fetch_start(url, n=4096):
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.headers.get("Content-Type"), r.read(n)


# ----------------------------------------------------------------- fake Roku
class FakeRoku:
    def __init__(self, limited=False, assistant=False):
        self.limited, self.calls, self.state, self.pos0, self.t0 = limited, [], "none", 0.0, None
        self.app = "782875" if assistant else "15985"     # Media Assistant, or the old Play on Roku
        self.streams = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body=b""):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/query/active-app":
                    app = f'<app id="{fake.app}">Player</app>' if fake.state in ("play", "pause") else \
                          '<app>Roku</app>'
                    return self._reply(200, f"<active-app>{app}</active-app>".encode())
                if self.path == "/query/media-player":
                    if fake.limited:
                        return self._reply(403, b"ECP command not allowed in Limited mode.")
                    ms = int(fake.position() * 1000)
                    xml = f'<player error="false" state="{fake.state}"><position>{ms} ms</position></player>'
                    return self._reply(200, xml.encode())
                self._reply(404)

            def do_POST(self):
                u = urllib.parse.urlparse(self.path)
                fake.calls.append((u.path, dict(urllib.parse.parse_qsl(u.query))))
                receiver = "/launch/782875" if fake.app == "782875" else "/input/15985"
                if u.path.startswith(("/input/", "/launch/")) and u.path != receiver:
                    return self._reply(404)
                if u.path == receiver:
                    url = dict(urllib.parse.parse_qsl(u.query))["u"]
                    fake.streams.append(fetch_start(url))      # the TV pulls the stream
                    fake.state, fake.pos0, fake.t0 = "play", 0.0, time.monotonic()
                elif u.path == "/keypress/Play":
                    if fake.state == "play":
                        fake.pos0, fake.state = fake.position(), "pause"
                    else:
                        fake.state, fake.t0 = "play", time.monotonic()
                elif u.path == "/keypress/Back":
                    fake.state = "close"
                self._reply(200)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def position(self):
        if self.state == "play":
            return self.pos0 + time.monotonic() - self.t0
        return self.pos0


@pytest.fixture
def media_server():
    srv = MediaServer(port=0)
    yield srv
    srv.shutdown()


@pytest.fixture
def book():
    t = Track(str(SAMPLE), 6.0, 0.0, "Part 1")
    return Book(7, "/x", "file", "Tone Book", author="Tester", duration=6.0, tracks=[t],
                chapters=[Chapter("One", 0, 6.0)])


def make_player(book):
    p = Player()
    p.book = book
    p.track_index = 0
    p._last_pos = 2.0
    p._want_playing = True
    return p


def test_roku_plays_stream_from_position(monkeypatch, media_server, book):
    roku = FakeRoku()
    monkeypatch.setattr(renderers, "ROKU_PORT", roku.httpd.server_address[1])
    session = renderers.RokuSession("Test Roku", "127.0.0.1", media_server)
    session.POLL_S = 0.2
    p = make_player(book)
    p.set_rate(1.5)
    p.start_casting(session)
    assert wait_for(lambda: roku.streams)
    assert roku.calls[0][0] == "/launch/782875"            # Media Assistant tried first
    path, params = roku.calls[1]
    assert path == "/input/15985" and params["t"] == "a" and params["songformat"] == "mp3"
    assert params["songname"] == "Part 1" and params["artistname"] == "Tester"
    ctype, head = roku.streams[0]
    assert ctype == "audio/mpeg" and (head[:3] == b"ID3" or head[0] == 0xFF)     # a real MP3 stream
    assert "/stream/" in params["u"]
    assert wait_for(lambda: p.position > 2.4)          # 2.0 + elapsed × 1.5
    p.pause()
    assert wait_for(lambda: roku.state == "pause")
    p.seek(4.0)                                        # seeking = a new stream from 4.0 s
    assert wait_for(lambda: len(roku.streams) == 2)
    assert abs(p.position - 4.0) < 0.6
    p.stop_casting(resume_local=False)
    assert wait_for(lambda: ("/keypress/Back", {}) in roku.calls)
    session._stop.set()


def test_roku_finish_detected(monkeypatch, media_server, book):
    roku = FakeRoku()
    monkeypatch.setattr(renderers, "ROKU_PORT", roku.httpd.server_address[1])
    session = renderers.RokuSession("Test Roku", "127.0.0.1", media_server)
    session.POLL_S = 0.2
    p = make_player(book)
    finished = []
    p.connect("book-finished", lambda *_: finished.append(True))
    p.start_casting(session)
    assert wait_for(lambda: roku.streams and session._seen_playing)
    roku.state, roku.pos0 = "close", 4.0               # stream from 2.0 s ran its 4 s out
    assert wait_for(lambda: finished)
    session._stop.set()


def test_roku_limited_mode_needs_no_tv_settings(monkeypatch, media_server, book):
    """Roku's default 'Limited' mode: no player queries, Lectern keeps the clock itself."""
    roku = FakeRoku(limited=True)
    monkeypatch.setattr(renderers, "ROKU_PORT", roku.httpd.server_address[1])
    session = renderers.RokuSession("Limited Roku", "127.0.0.1", media_server, limited=True)
    session.POLL_S = 0.2
    session.START_LATENCY = 0.0
    p = make_player(book)
    finished = []
    p.connect("book-finished", lambda *_: finished.append(True))
    p.start_casting(session)
    assert wait_for(lambda: roku.streams and session._state == "PLAYING")
    t0 = time.monotonic()
    assert wait_for(lambda: p.position > 2.7)          # 2.0 + elapsed, from Lectern's own clock
    assert p.position - 2.0 <= time.monotonic() - t0 + 0.5
    p.pause()
    assert wait_for(lambda: roku.state == "pause")
    frozen = p.position
    wait_for(lambda: False, timeout=0.6)
    assert abs(p.position - frozen) < 0.05             # clock stopped with the pause
    p.play()
    assert wait_for(lambda: roku.state == "play")
    roku.state = "close"                               # someone backed out on the TV
    assert wait_for(lambda: not p.playing)
    p.play()                                           # relaunch at the same spot
    assert wait_for(lambda: len(roku.streams) == 2)
    assert wait_for(lambda: finished, timeout=8)       # stream from ~3 s ends after ~3 s
    session._stop.set()


def test_roku_limited_mode_reported(monkeypatch):
    roku = FakeRoku(limited=True)
    monkeypatch.setattr(renderers, "ROKU_PORT", roku.httpd.server_address[1])
    with pytest.raises(renderers.RokuLimitedMode) as e:
        renderers.roku_request("127.0.0.1", "/query/media-player")
    assert "Permissive" in str(e.value)


# ----------------------------------------------------------------- fake DLNA
DESC = """<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<friendlyName>Kitchen Speaker</friendlyName><modelName>Sonos One</modelName><serviceList>
<service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType><controlURL>/rc</controlURL></service>
<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/avt</controlURL></service>
</serviceList></device></root>"""


class FakeRenderer:
    def __init__(self):
        self.actions, self.state, self.t0, self.volume, self.streams = [], "STOPPED", None, 40, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = DESC.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                action = self.headers["SOAPACTION"].strip('"').split("#")[1]
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                fake.actions.append(action)
                out = ""
                if action == "SetAVTransportURI":
                    import html
                    import re
                    uri = html.unescape(re.search(r"<CurrentURI>(.*?)</CurrentURI>", body).group(1))
                    fake.meta = html.unescape(re.search(r"<CurrentURIMetaData>(.*?)</CurrentURIMetaData>",
                                                        body, re.S).group(1))
                    fake.streams.append(fetch_start(uri))
                elif action == "Play":
                    fake.state, fake.t0 = "PLAYING", time.monotonic()
                elif action == "Pause":
                    fake.state = "PAUSED_PLAYBACK"
                elif action == "Stop":
                    fake.state = "STOPPED"
                elif action == "GetTransportInfo":
                    out = f"<CurrentTransportState>{fake.state}</CurrentTransportState>"
                elif action == "GetPositionInfo":
                    secs = int(time.monotonic() - fake.t0) if fake.t0 else 0
                    out = f"<RelTime>0:00:{secs:02d}</RelTime>"
                elif action == "GetVolume":
                    out = f"<CurrentVolume>{fake.volume}</CurrentVolume>"
                elif action == "SetVolume":
                    import re
                    fake.volume = int(re.search(r"<DesiredVolume>(\d+)", body).group(1))
                resp = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                        f'<u:{action}Response xmlns:u="x">{out}</u:{action}Response></s:Body></s:Envelope>').encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def test_dlna_description_and_playback(media_server, book):
    fake = FakeRenderer()
    xml = urllib.request.urlopen(fake.base + "/desc.xml").read().decode()
    info = renderers.parse_renderer_description(xml, fake.base + "/desc.xml")
    assert info["name"] == "Kitchen Speaker" and info["avtransport"] == fake.base + "/avt"
    session = renderers.DlnaSession(info["name"], "127.0.0.1", media_server, info["avtransport"], info["rendering"])
    session.POLL_S = 0.2
    p = make_player(book)
    p.start_casting(session)
    assert wait_for(lambda: "Play" in fake.actions)
    assert fake.actions[:3] == ["Stop", "SetAVTransportURI", "Play"]
    assert fake.streams[0][0] == "audio/mpeg"
    assert "Tone Book" in fake.meta and "audio/mpeg" in fake.meta
    p.pause()
    assert wait_for(lambda: fake.state == "PAUSED_PLAYBACK")
    p.set_fade(0.5)
    assert wait_for(lambda: fake.volume == 20)
    p.set_fade(1.0)
    assert wait_for(lambda: fake.volume == 40)
    p.stop_casting(resume_local=False)
    assert wait_for(lambda: fake.state == "STOPPED")
    session._stop.set()


@pytest.mark.parametrize("text,expected", [("0:01:02", 62), ("1:00:00.5", 3600.5), ("NOT_IMPLEMENTED", None)])
def test_parse_hms(text, expected):
    assert renderers.parse_hms(text) == expected


def test_parse_roku_player():
    assert renderers.parse_roku_player('<player state="play"><position>1500 ms</position></player>') == ("PLAYING", 1.5)
    assert renderers.parse_roku_player('<player state="close"/>') == ("IDLE", None)


def test_didl_escapes_titles():
    d = renderers.didl("http://x/a?b=1&c=2", "Tom & Jerry <3", "A", "B", None)
    assert "Tom &amp; Jerry &lt;3" in d and "b=1&amp;c=2" in d


def test_roku_without_play_on_roku_reports_clearly(monkeypatch, media_server, book):
    """Current Roku OS has no 'Play on Roku' channel: /input/15985 is a 404."""
    roku = FakeRoku(limited=True)
    monkeypatch.setattr(renderers, "ROKU_PORT", roku.httpd.server_address[1])
    monkeypatch.setattr(renderers, "PLAY_ON_ROKU", 999999)   # a channel the fake TV doesn't have
    session = renderers.RokuSession("New Roku", "127.0.0.1", media_server, limited=True)
    p = make_player(book)
    errors = []
    p.connect("error", lambda _p, msg: errors.append(msg))
    p.start_casting(session)
    assert wait_for(lambda: errors)
    assert "Media Assistant" in errors[0] and not p.playing


def test_roku_media_assistant(monkeypatch, media_server, book):
    roku = FakeRoku(limited=True, assistant=True)
    monkeypatch.setattr(renderers, "ROKU_PORT", roku.httpd.server_address[1])
    session = renderers.RokuSession("Test Roku", "127.0.0.1", media_server, limited=True)
    session.POLL_S = 0.2
    p = make_player(book)
    p.start_casting(session)
    assert wait_for(lambda: roku.streams and session._state == "PLAYING")
    path, params = roku.calls[0]
    assert path == "/launch/782875" and params["t"] == "a" and params["songFormat"] == "mp3"
    assert params["songName"] == "Part 1" and params["artistName"] == "Tester" and "/stream/" in params["u"]
    p.pause()
    assert wait_for(lambda: roku.state == "pause")
    p.stop_casting(resume_local=False)
