"""Chromecast support.

The Chromecast fetches audio over HTTP, so Lectern runs a tiny media server on
the LAN that serves only the files of the book being cast (behind random,
unguessable URLs). Playback control goes through pychromecast's Default Media
Receiver. ``CastSession`` is driven by ``Player`` when casting, so the rest of
the app (UI, MPRIS, sleep timer, progress saving) works unchanged.

Audio effects (boost, EQ, night mode, skip silence) are not applied while
casting: the device decodes the original files itself.
"""
import ipaddress
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst  # noqa: E402

try:
    import pychromecast
    from pychromecast.controllers.media import MediaStatusListener
    from pychromecast.discovery import CastBrowser, SimpleCastListener
except ImportError:  # optional dependency
    pychromecast = None
    MediaStatusListener = object

log = logging.getLogger(__name__)

PORT = 8765   # fixed, so a firewall rule can allow it (see install.sh)

CONTENT_TYPES = {
    ".m4b": "audio/mp4", ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
}


def content_type(path):
    ext = os.path.splitext(path)[1].lower()
    return CONTENT_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def parse_range(header, size):
    """'bytes=100-199' -> (100, 199); None if absent or unsatisfiable."""
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    else:  # suffix range: last N bytes
        start, end = max(0, size - int(m.group(2))), size - 1
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


def transcode_mp3(path, start, rate, effects, write, stop_event=None):
    """Decode ``path`` from ``start`` seconds at ``rate`` (pitch kept) and write MP3 bytes.

    Used for devices that can only play a plain HTTP stream (Roku, DLNA): seeking
    and speed happen here, and Lectern's boost/EQ/night mode are applied too.
    """
    from .audiofx import EffectsChain
    Gst.init(None)
    pipe = Gst.parse_launch(
        "filesrc name=src ! decodebin ! audioconvert name=entry ! audioresample ! "
        "audio/x-raw,rate=44100 ! lamemp3enc target=bitrate bitrate=160 cbr=true ! "
        "appsink name=sink sync=false max-buffers=64 emit-signals=false")
    pipe.get_by_name("src").set_property("location", path)
    if effects is not None:
        # put the effects chain (tempo, EQ, boost, limiter) between decoder and encoder
        fx = EffectsChain()
        fx.apply(effects)
        entry = pipe.get_by_name("entry")
        nxt = entry.get_static_pad("src").get_peer().get_parent_element()
        entry.unlink(nxt)
        pipe.add(fx.bin)
        entry.link(fx.bin)
        fx.bin.link(nxt)
    sink = pipe.get_by_name("sink")
    pipe.set_state(Gst.State.PAUSED)
    if pipe.get_state(10 * Gst.SECOND)[0] == Gst.StateChangeReturn.FAILURE:
        pipe.set_state(Gst.State.NULL)
        raise OSError(f"cannot decode {path}")
    if start > 0.05 or abs(rate - 1.0) > 0.01:
        pipe.seek(rate, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                  Gst.SeekType.SET, int(start * Gst.SECOND), Gst.SeekType.NONE, -1)
        pipe.get_state(10 * Gst.SECOND)
    pipe.set_state(Gst.State.PLAYING)
    try:
        while not (stop_event and stop_event.is_set()):
            sample = sink.emit("try-pull-sample", Gst.SECOND)
            if sample is None:
                if sink.get_property("eos"):
                    break
                continue
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if ok:
                try:
                    write(bytes(info.data))
                finally:
                    buf.unmap(info)
    finally:
        pipe.set_state(Gst.State.NULL)


class MediaServer:
    """Serves registered files only, with byte-range support (Chromecasts seek with ranges),
    plus live MP3 streams for devices that need them (``stream_url``)."""

    def __init__(self, port=PORT):
        self._files = {}          # token -> path
        self._streams = {}        # token -> (path, start, rate, effects)
        self.stream_requests = {} # token -> monotonic time the device first fetched it
        self.last_request = 0.0
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                log.debug("http %s - " + fmt, self.client_address[0], *args)

            def do_HEAD(self):
                self._serve(head=True)

            def do_GET(self):
                self._serve(head=False)

            def do_POST(self):
                self.send_error(404)

            def _serve(self, head):
                server.last_request = time.monotonic()
                token = self.path.split("?")[0].strip("/").split("/")[-1]
                if token in server._streams:
                    if not head:
                        server.stream_requests.setdefault(token, time.monotonic())
                    self._stream(server._streams[token], head)
                    return
                path = server._files.get(token)
                if not path or not os.path.isfile(path):
                    self.send_error(404)
                    return
                size = os.path.getsize(path)
                rng = parse_range(self.headers.get("Range"), size)
                if self.headers.get("Range") and rng is None:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = rng or (0, size - 1)
                self.send_response(206 if rng else 200)
                self.send_header("Content-Type", content_type(path))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(end - start + 1))
                if rng:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if head:
                    return
                try:
                    with open(path, "rb") as f:
                        f.seek(start)
                        remaining = end - start + 1
                        while remaining > 0:
                            chunk = f.read(min(256 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the device closed the connection (normal when seeking)

            def _stream(self, spec, head):
                path, start, rate, effects = spec
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "close")
                # DLNA renderers want these to accept a live (non-seekable) stream
                self.send_header("transferMode.dlna.org", "Streaming")
                self.send_header("contentFeatures.dlna.org",
                                 "DLNA.ORG_PN=MP3;DLNA.ORG_OP=00;DLNA.ORG_CI=1;DLNA.ORG_FLAGS=01700000000000000000000000000000")
                self.end_headers()
                self.close_connection = True
                if head:
                    return
                try:
                    transcode_mp3(path, start, rate, effects, self.wfile.write)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except OSError as e:
                    log.warning("stream failed: %s", e)

        try:
            self.httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        except OSError:
            log.warning("port %d busy; using a random port (a firewall rule for %d won't apply)", port, port)
            self.httpd = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="cast-http").start()

    def register(self, path):
        for token, p in self._files.items():
            if p == path:
                return token
        token = secrets.token_urlsafe(18) + os.path.splitext(path)[1].lower()
        self._files[token] = path
        return token

    def clear(self):
        self._files.clear()
        self._streams.clear()
        self.stream_requests.clear()

    def stream_url(self, path, host_ip, start=0.0, rate=1.0, effects=None):
        """A one-off MP3 stream of ``path`` from ``start`` seconds at ``rate``."""
        token = secrets.token_urlsafe(18) + ".mp3"
        self._streams[token] = (path, float(start), float(rate), effects)
        return f"http://{host_ip}:{self.port}/stream/{token}"

    def url(self, path, host_ip):
        return f"http://{host_ip}:{self.port}/media/{self.register(path)}"

    def shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def local_ip_for(remote_host):
    """The address of this machine on the route to the device."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_host, 9))   # no packet is sent for UDP connect
        return s.getsockname()[0]
    finally:
        s.close()


class CastSession(MediaStatusListener):
    """One connected device. All blocking pychromecast calls run on a worker thread."""

    def __init__(self, cast, server, name):
        self.cast = cast
        self.mc = cast.media_controller
        self.server = server
        self.name = name
        self.host_ip = local_ip_for(cast.cast_info.host)
        self.on_status = None           # callback(status) on the main loop
        self.status = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cast")
        self._base_volume = None
        self._sent_volume = None
        self.mc.register_status_listener(self)

    # pychromecast thread -> main loop
    def new_media_status(self, status):
        def deliver():
            self.status = status
            if self.on_status:
                self.on_status(status)
            return False
        GLib.idle_add(deliver)

    def load_media_failed(self, queue_item_id, error_code):
        log.warning("cast load failed: %s", error_code)

    def _call(self, fn, *args, **kw):
        def run():
            try:
                fn(*args, **kw)
            except Exception as e:  # noqa: BLE001 - network/device errors
                log.warning("cast call %s failed: %s", getattr(fn, "__name__", fn), e)
        self._pool.submit(run)

    supports_effects = False   # the Chromecast decodes the original file itself

    def load(self, book, track, local_pos, autoplay, rate, effects=None):
        self.server.clear()
        url = self.server.url(track.path, self.host_ip)
        cover = self.server.url(book.cover_path, self.host_ip) if book.cover_path and os.path.isfile(book.cover_path) else None
        metadata = {"metadataType": 3, "title": book.title, "albumName": book.title,
                    "artist": book.author or "", "subtitle": track.title or ""}
        if cover:
            metadata["images"] = [{"url": cover}]
        log.info("casting %s (%s) at %.1f s to %s", track.path, url, local_pos, self.name)

        def go():
            self.mc.play_media(url, content_type(track.path), title=book.title, thumb=cover,
                               current_time=local_pos, autoplay=autoplay, stream_type="BUFFERED",
                               metadata=metadata)
            self.mc.block_until_active(timeout=15)
            if abs(rate - 1.0) > 0.01:
                self.mc.set_playback_rate(rate)
        self._call(go)

    def play(self):
        self._call(self.mc.play)

    def pause(self):
        self._call(self.mc.pause)

    def seek(self, local_pos):
        self._call(self.mc.seek, max(0.0, local_pos))

    def set_rate(self, rate):
        self._call(self.mc.set_playback_rate, rate)

    def position(self):
        s = self.status
        if s is None or s.current_time is None:
            return None
        return s.adjusted_current_time

    @property
    def playing(self):
        return self.status is not None and self.status.player_state in ("PLAYING", "BUFFERING")

    def set_fade(self, level):
        """Sleep-timer fade on the device volume; the original volume is restored at 1.0."""
        if self._base_volume is None:
            self._base_volume = self.cast.status.volume_level if self.cast.status else 1.0
        target = round(self._base_volume * max(0.0, min(1.0, level)), 2)
        if self._sent_volume is not None and abs(target - self._sent_volume) < 0.03 and level < 1.0:
            return
        self._sent_volume = target
        self._call(self.cast.socket_client.receiver_controller.set_volume, target)
        if level >= 1.0:
            self._base_volume = self._sent_volume = None

    def stop(self, quit_app=True):
        def go():
            try:
                self.mc.stop()
                if quit_app:
                    self.cast.quit_app()
            finally:
                self.cast.disconnect(timeout=5)
        self._call(go)
        self._pool.shutdown(wait=False)
        self.server.clear()


class CastManager(GObject.Object):
    """Finds playback targets (Chromecast, Roku, DLNA) and connects to them."""

    __gsignals__ = {
        "devices-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "connecting": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "connected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "failed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "pin-needed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),   # device name; answer with submit_pin
    }

    def __init__(self):
        super().__init__()
        self.available = True   # Roku/DLNA need nothing extra; Chromecast needs pychromecast
        self.chromecast = pychromecast is not None
        self.browser = None
        self.zconf = None
        self.server = None
        self._stop_id = 0
        self._others = []       # Roku + DLNA + AirPlay devices from the last network scan
        self._scanning = False
        self._pin = None
        self._pin_event = threading.Event()
        self._owntone = None

    def devices(self):
        from .renderers import Device
        out = []
        if self.browser:
            for info in self.browser.devices.values():
                kind = {"audio": "Speaker", "group": "Speaker group"}.get(info.cast_type, info.model_name or "TV")
                out.append(Device("chromecast", info.friendly_name or "Chromecast", f"Chromecast · {kind}",
                                  info.host, f"cc:{info.uuid}", info=info))
        out += self._others
        return sorted(out, key=lambda d: d.name.casefold())

    @property
    def scanning(self):
        return self._scanning

    def discover(self, seconds=60):
        """Look for devices for a while (called when the "Play On" menu opens)."""
        self._ensure_zeroconf()
        if self.chromecast and self.browser is None:
            changed = lambda *_: GLib.idle_add(lambda: (self.emit("devices-changed"), False)[1])  # noqa: E731
            self.browser = CastBrowser(SimpleCastListener(changed, changed, changed), self.zconf)
            self.browser.start_discovery()
        if not self._scanning:
            self._scanning = True
            threading.Thread(target=self._scan_others, daemon=True, name="renderer-scan").start()
        if self._stop_id:
            GLib.source_remove(self._stop_id)
        self._stop_id = GLib.timeout_add_seconds(seconds, self._stop_discovery)

    def _ensure_zeroconf(self):
        if self.zconf is None:
            import zeroconf
            self.zconf = zeroconf.Zeroconf()

    def _scan_others(self):
        from .renderers import Device, discover_dlna, discover_rokus
        import zeroconf
        airplay_hosts = set()
        airplay_devices = {}

        class Listener(zeroconf.ServiceListener):
            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name, 2000)
                if info:
                    hosts = [socket.inet_ntoa(a) for a in info.addresses if len(a) == 4]
                    airplay_hosts.update(hosts)
                    props = {k.decode(): (v or b"").decode("utf-8", "replace") for k, v in info.properties.items()}
                    if hosts and props.get("deviceid"):
                        label = name.split("._airplay")[0]
                        model = props.get("model", "")
                        airplay_devices[props["deviceid"]] = Device(
                            "airplay", label, f"AirPlay · {model}" if model else "AirPlay", hosts[0],
                            f"airplay:{props['deviceid']}", device_id=props["deviceid"],
                            integrator=props.get("integrator", ""))

            def update_service(self, *a):
                pass

            def remove_service(self, *a):
                pass
        try:
            # Roku TVs announce themselves over AirPlay mDNS even when SSDP is quiet
            browser = zeroconf.ServiceBrowser(self.zconf, "_airplay._tcp.local.", Listener())
            dlna = discover_dlna()
            time.sleep(0.5)
            rokus = discover_rokus(airplay_hosts)
            browser.cancel()
            # Rokus that do AirPlay are reached that way only (their Roku-app route needs an
            # extra channel on the TV)
            rokus = [r for r in rokus if r.host not in {d.host for d in airplay_devices.values()}]
            airplay = list(airplay_devices.values()) if self.airplay_available else []
            self._others = rokus + dlna + airplay
        except Exception:  # noqa: BLE001
            log.exception("device scan failed")
        finally:
            self._scanning = False
            GLib.idle_add(lambda: (self.emit("devices-changed"), False)[1])

    def _stop_discovery(self):
        self._stop_id = 0
        if self.browser is not None:
            self.browser.stop_discovery()
            self.browser = None   # devices list is rebuilt on the next discovery
        return False

    @property
    def airplay_available(self):
        from .airplay import find_owntone
        return find_owntone() is not None

    @property
    def owntone(self):
        if self._owntone is None:
            from .airplay import OwnTone
            self._owntone = OwnTone()
        return self._owntone

    def submit_pin(self, pin):
        """Answer a "pin-needed" signal (None cancels)."""
        self._pin = pin
        self._pin_event.set()

    def _connect_airplay(self, device):
        from . import airplay
        if device.extra["device_id"] not in airplay.load_pairings():
            pairing = airplay.Pairing(device.host, airplay.owntone_pairing_id())
            pairing.begin()                       # the TV shows a 4-digit code now
            self._pin, self._pin_event = None, threading.Event()
            GLib.idle_add(lambda: (self.emit("pin-needed", device.name), False)[1])
            if not self._pin_event.wait(300) or not self._pin:
                pairing.close()
                raise airplay.PairingError("Pairing cancelled")
            seed, _dev_id, ltpk = pairing.finish(int(self._pin))
            airplay.save_pairing(device.extra["device_id"], device.name, airplay.owntone_auth_key(seed, ltpk))
            log.info("paired with %s", device.name)
        ot = self.owntone
        ot.stop()                                 # restart so OwnTone loads the keys
        ot.start(sample_rate=airplay.device_sample_rate(device.extra.get("integrator", "")))
        if not ot.wait_for_pipe():
            raise OSError("OwnTone didn't pick up Lectern's audio feed")
        out = ot.wait_for_output(airplay.speaker_id(device.extra["device_id"]))
        if out is None:
            raise OSError(f"OwnTone can't see {device.name} on the network")
        session = airplay.AirPlayOutput(device.name, device.extra["device_id"], ot)
        ot.play_pipe_to(session.sid)
        return session

    def _ensure_server(self):
        if self.server is None:
            self.server = MediaServer()

    def connect_device(self, device):
        name = device.name
        self.emit("connecting", name)
        try:
            self._ensure_server()
        except OSError as e:
            self.emit("failed", f"Could not start the media server: {e}")
            return
        threading.Thread(target=self._connect, args=(device,), daemon=True, name="cast-connect").start()

    def _connect(self, device):
        from . import renderers
        name = device.name
        try:
            if device.kind == "chromecast":
                cast = pychromecast.get_chromecast_from_cast_info(device.extra["info"], self.zconf,
                                                                  tries=2, timeout=10)
                cast.wait(timeout=15)
                session = CastSession(cast, self.server, name)
            elif device.kind == "roku":
                try:
                    renderers.roku_request(device.host, "/query/media-player", timeout=4)
                    limited = False
                except renderers.RokuLimitedMode:
                    limited = True   # still works: Lectern keeps the playback clock itself
                session = renderers.RokuSession(name, device.host, self.server, limited=limited)
            elif device.kind == "airplay":
                session = self._connect_airplay(device)
            else:
                session = renderers.DlnaSession(name, device.host, self.server,
                                                device.extra["avtransport"], device.extra.get("rendering"))
            GLib.idle_add(lambda: (self.emit("connected", session), False)[1])
        except Exception as e:  # noqa: BLE001
            from .airplay import PairingError
            log.warning("cannot connect to %s: %s", name, e)
            msg = f"{name}: {e}" if isinstance(e, (PairingError, OSError)) else f"Couldn't connect to {name}"
            GLib.idle_add(lambda: (self.emit("failed", msg), False)[1])

    def firewall_hint(self, host_ip=None):
        """A command that opens the media port to the local network, if a firewall is likely blocking it."""
        subnet = "192.168.0.0/16"
        if host_ip:
            subnet = str(ipaddress.ip_interface(f"{host_ip}/24").network)
        if shutil.which("ufw") and os.path.exists("/etc/ufw/ufw.conf"):
            with open("/etc/ufw/ufw.conf", encoding="utf-8", errors="replace") as f:
                if re.search(r"^ENABLED=yes", f.read(), re.M):
                    return f"sudo ufw allow from {subnet} to any port {self.port} proto tcp"
        if shutil.which("firewall-cmd"):
            return f"sudo firewall-cmd --permanent --add-port={self.port}/tcp && sudo firewall-cmd --reload"
        return None

    @property
    def port(self):
        return self.server.port if self.server else PORT

    def shutdown(self):
        if self._owntone is not None:
            self._owntone.stop()
        self._stop_discovery()
        if self.zconf is not None:
            self.zconf.close()
        if self.server is not None:
            self.server.shutdown()
