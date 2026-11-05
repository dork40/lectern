"""Roku TVs (External Control Protocol) and DLNA/UPnP renderers (smart TVs, Sonos, Kodi...).

Both get a live MP3 stream from Lectern's media server that starts at the
wanted position with speed and effects applied (``MediaServer.stream_url``),
so seeking or changing speed reloads the stream. They implement the same
session interface as ``cast.CastSession`` and plug into ``Player``.
"""
import html
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree

from gi.repository import GLib

from .cast import local_ip_for

log = logging.getLogger(__name__)

PLAY_ON_ROKU = 15985     # Roku's built-in "Play on Roku" channel, accepts a media URL (gone since 2023)
MEDIA_ASSISTANT = 782875  # "Media Assistant", a free channel that plays a URL it's given (as Home Assistant uses)
ROKU_PORT = 8060
ROKU_PERMISSIVE_HELP = ("On the Roku: Settings → System → Advanced system settings → Control by mobile apps "
                        "→ Network access → Permissive")


class RemoteStatus:
    """The bits of a device status Player cares about (mirrors pychromecast's MediaStatus)."""

    def __init__(self, player_state, idle_reason=None, message=None):
        self.player_state = player_state
        self.idle_reason = idle_reason
        self.message = message

ROKU_NO_RECEIVER = ("This Roku needs the free “Media Assistant” channel to play books itself: on the TV, "
                    "Home → Streaming Store (or Search) → “Media Assistant” → Add channel. Then pick it again.")


class StreamSession:
    """Common logic: positions are base + device position × rate; seeks/speed reload the stream."""

    supports_effects = True
    POLL_S = 1.0

    def __init__(self, name, host, server):
        self.name = name
        self.host = host
        self.server = server
        self.host_ip = local_ip_for(host)
        self.on_status = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"renderer-{name}")
        self._base = 0.0               # position in the track where the current stream starts
        self._rate = 1.0
        self._dev_pos = 0.0            # seconds into the current stream, as reported
        self._dev_pos_at = 0.0         # monotonic time of that report
        self._state = "IDLE"
        self._track_len = 0.0
        self._loaded_at = 0.0
        self._seen_playing = False
        self._want_playing = False
        self._load_args = None
        self._stop = threading.Event()
        threading.Thread(target=self._poll_loop, daemon=True, name=f"poll-{name}").start()

    # -- interface used by Player
    def load(self, book, track, local_pos, autoplay, rate, effects=None):
        self._base, self._rate = float(local_pos), float(rate)
        self._dev_pos, self._dev_pos_at = 0.0, time.monotonic()
        self._track_len = track.duration
        self._want_playing = autoplay
        self._seen_playing = False
        self._loaded_at = time.monotonic()
        self._load_args = (book, track, effects)
        url = self.server.stream_url(track.path, self.host_ip, local_pos, rate, effects)
        cover = None
        if book.cover_path:
            cover = self.server.url(book.cover_path, self.host_ip)
        log.info("streaming %s from %.1f s at %.2fx to %s", track.path, local_pos, rate, self.name)
        self._submit(self._device_load, url, book, track, cover, autoplay)

    def play(self):
        self._want_playing = True
        self._submit(self._device_play)

    def pause(self):
        self._want_playing = False
        self._submit(self._device_pause)

    def seek(self, local_pos):
        if self._load_args:
            book, track, effects = self._load_args
            self.load(book, track, local_pos, self._want_playing, self._rate, effects)

    def set_rate(self, rate):
        pos = self.position()
        if self._load_args and pos is not None:
            book, track, effects = self._load_args
            self.load(book, track, pos, self._want_playing, rate, effects)

    def set_effects(self, effects):
        pos = self.position()
        if self._load_args and pos is not None:
            book, track, _ = self._load_args
            self.load(book, track, pos, self._want_playing, self._rate, effects)

    def position(self):
        dev = self._dev_pos
        if self._state == "PLAYING":
            dev += time.monotonic() - self._dev_pos_at
        return self._base + dev * self._rate

    def set_fade(self, level):
        pass  # only DLNA has a usable volume control (overridden there)

    def stop(self, quit_app=True):
        self._stop.set()
        self._submit(self._device_stop)
        self._pool.shutdown(wait=False)

    # -- helpers
    def _submit(self, fn, *args):
        def run():
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001 - network/device errors
                log.warning("%s: %s failed: %s", self.name, fn.__name__, e)
        self._pool.submit(run)

    def _report(self, state, dev_pos):
        """Called from the poll thread with the device's state and position in the stream."""
        now = time.monotonic()
        if dev_pos is not None:
            self._dev_pos, self._dev_pos_at = dev_pos, now
        reason = None
        if state == "PLAYING":
            self._seen_playing = True
        elif state == "IDLE" and self._seen_playing:
            # the stream ran out (or the user left the player on the TV)
            reached = self.position()
            reason = "FINISHED" if reached >= self._track_len - 3 else None
            if reason is None:
                state = "PAUSED"
        elif state == "IDLE" and now - self._loaded_at < 20:
            state = "BUFFERING"   # still starting up
        self._state = state
        status = RemoteStatus(state, reason)
        GLib.idle_add(lambda: (self.on_status and self.on_status(status), False)[1])

    def _poll_loop(self):
        while not self._stop.wait(self.POLL_S):
            try:
                state, pos = self._device_query()
            except Exception as e:  # noqa: BLE001
                log.debug("%s poll failed: %s", self.name, e)
                continue
            self._report(state, pos)

    # -- device specific
    def _device_load(self, url, book, track, cover, autoplay):
        raise NotImplementedError

    def _device_play(self):
        raise NotImplementedError

    def _device_pause(self):
        raise NotImplementedError

    def _device_stop(self):
        raise NotImplementedError

    def _device_query(self):
        """-> (PLAYING|PAUSED|BUFFERING|IDLE, seconds into the stream or None)"""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Roku (External Control Protocol, port 8060)
# --------------------------------------------------------------------------- #

class RokuLimitedMode(Exception):
    pass


def roku_request(host, path, method="GET", timeout=4):
    req = urllib.request.Request(f"http://{host}:{ROKU_PORT}{path}", method=method,
                                 data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise RokuLimitedMode(ROKU_PERMISSIVE_HELP) from e
        raise


def parse_roku_player(xml):
    """/query/media-player -> (state, seconds or None)."""
    state = (re.search(r'<player[^>]*\bstate="(\w+)"', xml) or [None, "none"])[1]
    pos = re.search(r"<position>\s*(\d+)\s*ms\s*</position>", xml)
    mapped = {"play": "PLAYING", "pause": "PAUSED", "buffer": "BUFFERING", "startup": "BUFFERING",
              "open": "BUFFERING"}.get(state, "IDLE")
    return mapped, (int(pos.group(1)) / 1000 if pos else None)


class RokuSession(StreamSession):
    """Roku TV via ECP.

    Works in both of Roku's network-control modes:
      * Permissive/Enabled: state and position are read from /query/media-player
      * Limited (the default; no access to the TV's settings needed): launching
        media and key presses still work, but the player can't be queried, so
        Lectern keeps the clock itself: it starts when the TV begins pulling
        the stream and stops/starts with every pause/play it sends. Whether the
        player is still on screen is checked via /query/active-app.
    """

    START_LATENCY = 1.2   # seconds between the TV fetching the stream and sound coming out

    app_id = PLAY_ON_ROKU    # the receiver channel the last launch went to

    def __init__(self, name, host, server, limited=False):
        self.limited = limited
        self._token = None
        super().__init__(name, host, server)

    def _device_load(self, url, book, track, cover, autoplay):
        if self.limited:
            self._limited_load(url, book, track, cover, autoplay)
            return
        self._launch(url, book, track, cover)
        if not autoplay:
            # Play on Roku always starts playing; pause as soon as it does
            for _ in range(20):
                time.sleep(0.5)
                if self._device_query()[0] == "PLAYING":
                    roku_request(self.host, "/keypress/Play", "POST")
                    break

    def _launch(self, url, book, track, cover):
        """The TV fetches and plays the stream itself: through Media Assistant, else old Play on Roku."""
        title = track.title or book.title
        assistant = {"t": "a", "u": url, "songName": title, "artistName": book.author or "",
                     "songFormat": "mp3"}
        legacy = {"t": "a", "u": url, "songname": title, "artistname": book.author or "",
                  "albumname": book.title, "songformat": "mp3"}
        if cover:
            assistant["albumArtUrl"] = legacy["albumarturl"] = cover
        try:
            roku_request(self.host, f"/launch/{MEDIA_ASSISTANT}?{urllib.parse.urlencode(assistant)}", "POST")
            self.app_id = MEDIA_ASSISTANT
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:   # 404: channel not installed
                raise
        try:
            roku_request(self.host, f"/input/{PLAY_ON_ROKU}?{urllib.parse.urlencode(legacy)}", "POST")
            self.app_id = PLAY_ON_ROKU
        except urllib.error.HTTPError as e:
            if e.code == 404:   # neither receiver on this TV
                self._stop.set()
                status = RemoteStatus("IDLE", "ERROR", ROKU_NO_RECEIVER)
                GLib.idle_add(lambda: (self.on_status and self.on_status(status), False)[1])
            raise

    def _limited_load(self, url, book, track, cover, autoplay):
        self._token = url.rsplit("/", 1)[1]
        self._state = "BUFFERING"
        self._dev_pos, self._dev_pos_at = 0.0, time.monotonic()
        self._launch(url, book, track, cover)
        started = None
        for _ in range(60):                 # wait for the TV to pull the stream
            started = self.server.stream_requests.get(self._token)
            if started:
                break
            time.sleep(0.25)
        if started is None:
            log.warning("%s never fetched the stream", self.name)
            return
        self._dev_pos, self._dev_pos_at = 0.0, started + self.START_LATENCY
        self._state = "PLAYING"
        if not autoplay:
            time.sleep(max(0.0, started + self.START_LATENCY - time.monotonic()))
            self._limited_pause()

    def _clock(self):
        """Seconds into the current stream by Lectern's own clock."""
        if self._state == "PLAYING":
            return self._dev_pos + max(0.0, time.monotonic() - self._dev_pos_at)
        return self._dev_pos

    def _limited_pause(self):
        if self._state == "PLAYING":
            self._dev_pos, self._dev_pos_at = self._clock(), time.monotonic()
            roku_request(self.host, "/keypress/Play", "POST")
            self._state = "PAUSED"

    def _limited_play(self):
        app = roku_request(self.host, "/query/active-app", timeout=3)
        if f'id="{self.app_id}"' not in app and self._load_args:
            # the player was closed on the TV: start the stream again where we are
            book, track, effects = self._load_args
            pos = self.position()
            self._base = pos
            url = self.server.stream_url(track.path, self.host_ip, pos, self._rate, effects)
            cover = self.server.url(book.cover_path, self.host_ip) if book.cover_path else None
            self._limited_load(url, book, track, cover, True)
            return
        if self._state == "PAUSED":
            roku_request(self.host, "/keypress/Play", "POST")
            self._dev_pos_at = time.monotonic()
            self._state = "PLAYING"

    def _limited_query(self):
        if self._state == "BUFFERING":
            return "BUFFERING", None
        app = roku_request(self.host, "/query/active-app", timeout=3)
        on_screen = f'id="{self.app_id}"' in app
        stream_len = (self._track_len - self._base) / max(self._rate, 0.01)
        if not on_screen or self._clock() >= stream_len:
            return "IDLE", None          # finished, or someone left the player on the TV
        return self._state, None

    def position(self):
        if self.limited:
            return self._base + self._clock() * self._rate
        return super().position()

    def _toggle_to(self, want):
        if self.limited:
            self._limited_play() if want else self._limited_pause()
            return
        state = self._device_query()[0]
        if (state == "PLAYING") != want and state in ("PLAYING", "PAUSED"):
            roku_request(self.host, "/keypress/Play", "POST")   # Play is a play/pause toggle

    def _device_play(self):
        self._toggle_to(True)

    def _device_pause(self):
        self._toggle_to(False)

    def _device_stop(self):
        try:
            if self.limited:
                if f'id="{self.app_id}"' in roku_request(self.host, "/query/active-app", timeout=3):
                    roku_request(self.host, "/keypress/Back", "POST")
            elif self._device_query()[0] != "IDLE":
                roku_request(self.host, "/keypress/Back", "POST")
        except Exception:  # noqa: BLE001
            pass

    def _device_query(self):
        if self.limited:
            return self._limited_query()
        return parse_roku_player(roku_request(self.host, "/query/media-player", timeout=3))


def roku_info(host):
    """{'name', 'model', 'limited'} for a Roku at host, or None if it isn't one."""
    try:
        xml = roku_request(host, "/query/device-info", timeout=2)
    except (OSError, RokuLimitedMode, urllib.error.URLError):
        return None
    def tag(k):
        m = re.search(f"<{k}>(.*?)</{k}>", xml)
        return html.unescape(m.group(1)) if m else ""
    return {"name": tag("user-device-name") or tag("friendly-device-name") or tag("default-device-name") or "Roku",
            "model": tag("friendly-model-name") or tag("model-name") or "Roku",
            "limited": tag("ecp-setting-mode") == "limited"}


# --------------------------------------------------------------------------- #
# DLNA / UPnP AV MediaRenderer
# --------------------------------------------------------------------------- #

AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"


def soap(control_url, service, action, args, timeout=5):
    body = "".join(f"<{k}>{html.escape(str(v))}</{k}>" for k, v in args.items())
    envelope = ('<?xml version="1.0" encoding="utf-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>')
    req = urllib.request.Request(control_url, data=envelope.encode(), method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service}#{action}"'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        xml = r.read().decode("utf-8", "replace")
    out = {}
    for el in ElementTree.fromstring(xml).iter():
        tag = el.tag.split("}")[-1]
        if el.text is not None and not list(el):
            out[tag] = el.text
    return out


def parse_hms(text):
    """'0:01:02.500' / 'NOT_IMPLEMENTED' -> seconds or None."""
    m = re.fullmatch(r"(\d+):(\d\d):(\d\d)(?:\.(\d+))?", (text or "").strip())
    if not m:
        return None
    h, mnt, s, frac = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + int(s) + (float(f"0.{frac}") if frac else 0.0)


def didl(url, title, artist, album, cover):
    item = (f'<item id="1" parentID="0" restricted="1"><dc:title>{html.escape(title)}</dc:title>'
            f'<upnp:artist>{html.escape(artist)}</upnp:artist><upnp:album>{html.escape(album)}</upnp:album>'
            '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
            + (f'<upnp:albumArtURI>{html.escape(cover)}</upnp:albumArtURI>' if cover else "")
            + f'<res protocolInfo="http-get:*:audio/mpeg:DLNA.ORG_PN=MP3;DLNA.ORG_OP=00;DLNA.ORG_CI=1">'
            f'{html.escape(url)}</res></item>')
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            + item + "</DIDL-Lite>")


def parse_renderer_description(xml, base_url):
    """Device description -> {'name', 'model', 'avtransport', 'rendering'} (control URLs) or None."""
    root = ElementTree.fromstring(xml)
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    base = root.findtext("d:URLBase", namespaces=ns) or base_url
    info = {"name": "", "model": "", "avtransport": None, "rendering": None}
    for dev in root.iter("{urn:schemas-upnp-org:device-1-0}device"):
        info["name"] = info["name"] or (dev.findtext("d:friendlyName", namespaces=ns) or "")
        info["model"] = info["model"] or (dev.findtext("d:modelName", namespaces=ns) or "")
        for svc in dev.iter("{urn:schemas-upnp-org:device-1-0}service"):
            stype = svc.findtext("d:serviceType", namespaces=ns) or ""
            ctl = urllib.parse.urljoin(base, svc.findtext("d:controlURL", namespaces=ns) or "")
            if "AVTransport" in stype and not info["avtransport"]:
                info["avtransport"] = ctl
            elif "RenderingControl" in stype and not info["rendering"]:
                info["rendering"] = ctl
    return info if info["avtransport"] else None


class DlnaSession(StreamSession):
    def __init__(self, name, host, server, avtransport, rendering=None):
        self.avtransport = avtransport
        self.rendering = rendering
        self._base_volume = None
        super().__init__(name, host, server)

    def _device_load(self, url, book, track, cover, autoplay):
        try:
            soap(self.avtransport, AVT, "Stop", {"InstanceID": 0})
        except (OSError, urllib.error.URLError, ElementTree.ParseError):
            pass
        meta = didl(url, track.title or book.title, book.author or "", book.title, cover)
        soap(self.avtransport, AVT, "SetAVTransportURI",
             {"InstanceID": 0, "CurrentURI": url, "CurrentURIMetaData": meta})
        if autoplay:
            soap(self.avtransport, AVT, "Play", {"InstanceID": 0, "Speed": 1})

    def _device_play(self):
        soap(self.avtransport, AVT, "Play", {"InstanceID": 0, "Speed": 1})

    def _device_pause(self):
        soap(self.avtransport, AVT, "Pause", {"InstanceID": 0})

    def _device_stop(self):
        soap(self.avtransport, AVT, "Stop", {"InstanceID": 0})

    def _device_query(self):
        t = soap(self.avtransport, AVT, "GetTransportInfo", {"InstanceID": 0}, timeout=3)
        p = soap(self.avtransport, AVT, "GetPositionInfo", {"InstanceID": 0}, timeout=3)
        state = {"PLAYING": "PLAYING", "PAUSED_PLAYBACK": "PAUSED", "TRANSITIONING": "BUFFERING"}.get(
            t.get("CurrentTransportState", ""), "IDLE")
        return state, parse_hms(p.get("RelTime"))

    def set_fade(self, level):
        if not self.rendering:
            return

        def go():
            if self._base_volume is None:
                self._base_volume = int(soap(self.rendering, RC, "GetVolume",
                                             {"InstanceID": 0, "Channel": "Master"}).get("CurrentVolume", 30))
            vol = int(round(self._base_volume * max(0.0, min(1.0, level))))
            soap(self.rendering, RC, "SetVolume", {"InstanceID": 0, "Channel": "Master", "DesiredVolume": vol})
            if level >= 1.0:
                self._base_volume = None
        self._submit(go)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def ssdp_search(target, timeout=3.0):
    """-> {location or ip: response headers dict}"""
    msg = (f'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 2\r\n'
           f"ST: {target}\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(0.5)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    found = {}
    try:
        for _ in range(2):
            s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            headers = {}
            for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            headers["_ip"] = addr[0]
            found[headers.get("location", addr[0])] = headers
    finally:
        s.close()
    return found


class Device:
    """A playback target shown in the "Play On" menu."""

    def __init__(self, kind, name, subtitle, host, key, **extra):
        self.kind, self.name, self.subtitle, self.host, self.key = kind, name, subtitle, host, key
        self.extra = extra

    @property
    def icon(self):
        return {"roku": "lectern-cast-symbolic", "dlna": "audio-speakers-symbolic"}.get(self.kind,
                                                                                         "lectern-cast-symbolic")


def discover_rokus(extra_hosts=()):
    hosts = {h["_ip"] for h in ssdp_search("roku:ecp", 2.5).values()} | set(extra_hosts)
    out = []
    for host in sorted(hosts):
        info = roku_info(host)
        if info:
            out.append(Device("roku", info["name"], "Roku app (Media Assistant) · plays on the TV itself",
                              host, f"roku:{host}",
                              limited=info["limited"]))
    return out


def discover_dlna():
    out = []
    for location, headers in ssdp_search("urn:schemas-upnp-org:device:MediaRenderer:1", 3.0).items():
        if not location.startswith("http"):
            continue
        try:
            xml = urllib.request.urlopen(location, timeout=3).read().decode("utf-8", "replace")
            info = parse_renderer_description(xml, location)
        except (OSError, urllib.error.URLError, ElementTree.ParseError):
            continue
        if info:
            out.append(Device("dlna", info["name"] or headers["_ip"], f"DLNA · {info['model'] or 'Media renderer'}",
                              headers["_ip"], f"dlna:{location}", avtransport=info["avtransport"],
                              rendering=info["rendering"]))
    return out
