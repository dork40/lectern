"""AirPlay 2 playback (e.g. Roku TVs, Samsung/LG TVs, HomePods) through a private OwnTone.

How it fits together:

* Pairing (one time per device): Apple's HomeKit-style pair-setup with the
  4-digit code the TV shows (SRP-6a/3072/SHA-512, Ed25519, ChaCha20-Poly1305).
  Lectern pairs under the identity its private OwnTone uses, then stores the
  keys in OwnTone's database, so a stock OwnTone (AUR: owntone-server) works.
* Streaming: Lectern keeps all audio processing (speed, effects, skip silence,
  seeking, exact progress) in its own player and writes the processed PCM into
  a pipe that OwnTone plays to the AirPlay device. The pipe's backpressure paces
  Lectern: OwnTone reads at the device's clock, so no audio is ever dropped or
  rushed (dropping it made voices sound fast and choppy).
* Now playing: title, author and cover go through OwnTone's metadata pipe
  (shairport-sync format); OwnTone forwards them to the TV.
"""
import fcntl
import hashlib
import logging
import os
import secrets
import select
import shutil
import socket
import sqlite3
import struct
import subprocess
import termios
import time
import json
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .const import DATA_DIR

log = logging.getLogger(__name__)

OWNTONE_LIBRARY_NAME = "Lectern"
OWNTONE_PORT = 3690
AIRPLAY_CONTROL_PORT, AIRPLAY_TIMING_PORT = 6001, 6002
PCM_RATE, PCM_CHANNELS = 44100, 2
# Audio OwnTone holds before the TV plays it: its input buffer (96000 samples)
# plus its output start buffer (start_buffer_ms, default 2250).
OWNTONE_BUFFERED = 96000 / PCM_RATE + 2.25
PICT_SPACING = 4.0      # s between covers sent to OwnTone (it shows the TV a cover ~2 s late)


class PairingError(Exception):
    pass


# --------------------------------------------------------------------------- #
# TLV8 and SRP-6a (HAP flavour), pure functions
# --------------------------------------------------------------------------- #

TLV_METHOD, TLV_IDENTIFIER, TLV_SALT, TLV_PUBLIC_KEY, TLV_PROOF = 0, 1, 2, 3, 4
TLV_ENCRYPTED_DATA, TLV_SEQ, TLV_ERROR, TLV_SIGNATURE = 5, 6, 7, 10


def tlv_write(items):
    out = bytearray()
    for tag, value in items:
        value = bytes(value)
        if not value:
            out += bytes((tag, 0))
        for i in range(0, len(value), 255):
            chunk = value[i:i + 255]
            out += bytes((tag, len(chunk))) + chunk
    return bytes(out)


def tlv_read(data):
    out, i, last = {}, 0, None
    while i + 2 <= len(data):
        tag, length = data[i], data[i + 1]
        value = data[i + 2:i + 2 + length]
        if tag == last and tag in out:
            out[tag] += value          # continuation of a fragmented item
        else:
            out[tag] = value
        last = tag
        i += 2 + length
    return out


# RFC 5054 3072-bit group
N_3072 = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF", 16)
G = 5


def _H(*parts):
    h = hashlib.sha512()
    for p in parts:
        h.update(p)
    return h.digest()


def _bytes(n, length=None):
    length = length or (n.bit_length() + 7) // 8
    return n.to_bytes(length, "big")


def _pad(n):
    return _bytes(n, 384)


class SrpClient:
    """SRP-6a client as used by HomeKit/AirPlay pair-setup (username "Pair-Setup", password = PIN)."""

    def __init__(self, pin, a=None):
        self.I, self.p = b"Pair-Setup", str(pin).encode()
        self.a = a if a is not None else int.from_bytes(secrets.token_bytes(32), "big")
        self.A = pow(G, self.a, N_3072)

    def process(self, salt, B_bytes):
        B = int.from_bytes(B_bytes, "big")
        if B % N_3072 == 0:
            raise PairingError("bad server public key")
        k = int.from_bytes(_H(_bytes(N_3072), _pad(G)), "big")
        u = int.from_bytes(_H(_pad(self.A), _pad(B)), "big")
        x = int.from_bytes(_H(salt, _H(self.I + b":" + self.p)), "big")
        S = pow((B - k * pow(G, x, N_3072)) % N_3072, self.a + u * x, N_3072)
        self.K = _H(_bytes(S))
        hn, hg = _H(_bytes(N_3072)), _H(_bytes(G))
        self.M1 = _H(bytes(a ^ b for a, b in zip(hn, hg)), _H(self.I), salt, _bytes(self.A), B_bytes, self.K)
        self.M2_expected = _H(_bytes(self.A), self.M1, self.K)
        return _bytes(self.A), self.M1


def hkdf(salt, info, secret):
    return HKDF(algorithm=hashes.SHA512(), length=32, salt=salt.encode(), info=info.encode()).derive(secret)


def _raw(key):
    if isinstance(key, Ed25519PrivateKey):
        return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def murmur_hash64a(data, seed=0):
    """MurmurHash64A, as OwnTone uses to derive its AirPlay identity from the library name."""
    m, r, mask = 0xC6A4A7935BD1E995, 47, (1 << 64) - 1
    h = (seed ^ (len(data) * m)) & mask
    full = len(data) - len(data) % 8
    for i in range(0, full, 8):
        k = struct.unpack_from("<Q", data, i)[0]
        k = (k * m) & mask
        k ^= k >> r
        k = (k * m) & mask
        h ^= k
        h = (h * m) & mask
    tail = data[full:]
    for i in range(len(tail) - 1, -1, -1):
        h ^= tail[i] << (8 * i)
    if tail:
        h = (h * m) & mask
    h ^= h >> r
    h = (h * m) & mask
    h ^= h >> r
    return h


def owntone_pairing_id(library_name=OWNTONE_LIBRARY_NAME):
    return f"{murmur_hash64a(library_name.encode()):016X}"


# --------------------------------------------------------------------------- #
# Minimal HTTP/1.1 over one socket (the TV must see one connection throughout)
# --------------------------------------------------------------------------- #

class RawHttp:
    def __init__(self, host, port=7000, timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.host = host
        self.buf = b""

    def send(self, method, path, body=b"", headers=None):
        h = {"Host": self.host, "User-Agent": "AirPlay/320.20", "Connection": "keep-alive",
             "X-Apple-HKP": "3", "Content-Type": "application/octet-stream", "Content-Length": str(len(body))}
        h.update(headers or {})
        req = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
        self.sock.sendall(req.encode() + body)

    def _fill(self, timeout):
        self.sock.settimeout(timeout)
        chunk = self.sock.recv(65536)
        if not chunk:
            raise PairingError("the device closed the connection")
        self.buf += chunk

    def receive(self, timeout=15.0):
        """-> (status, body). Raises socket.timeout if nothing arrives."""
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in self.buf:
            self._fill(max(0.1, deadline - time.monotonic()))
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1])
        length = 0
        for line in lines[1:]:
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1])
        while len(self.buf) < length:
            self._fill(max(0.1, deadline - time.monotonic()))
        body, self.buf = self.buf[:length], self.buf[length:]
        return status, body

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Pair-setup
# --------------------------------------------------------------------------- #

class Pairing:
    """Two-step pairing: begin() makes the device show its code, finish(pin) completes."""

    def __init__(self, host, identifier, port=7000):
        self.host, self.port = host, port
        self.identifier = identifier.encode() if isinstance(identifier, str) else identifier
        self.http = None
        self.salt = self.B = None

    def begin(self):
        self.http = RawHttp(self.host, self.port)
        self.http.send("POST", "/pair-pin-start")
        try:
            status, _ = self.http.receive(timeout=2.5)   # some TVs (Roku) never answer this one
            log.debug("pair-pin-start -> %s", status)
        except (socket.timeout, TimeoutError):
            pass
        self.http.send("POST", "/pair-setup", tlv_write([(TLV_METHOD, b"\x00"), (TLV_SEQ, b"\x01")]))
        m2 = self._tlv_response()
        if TLV_ERROR in m2:
            raise PairingError("the device refused pairing (it may be busy, or locked after wrong codes)")
        self.salt, self.B = m2[TLV_SALT], m2[TLV_PUBLIC_KEY]

    def _tlv_response(self):
        while True:
            status, body = self.http.receive(timeout=20)
            if status == 200 and body:
                return tlv_read(body)
            if status != 200:
                raise PairingError(f"device answered HTTP {status}")
            # an empty 200 is the late answer to /pair-pin-start: skip it

    def finish(self, pin):
        """-> (client_secret_seed 32 B, device_identifier, device_ltpk 32 B)"""
        try:
            srp = SrpClient(pin)
            A, M1 = srp.process(self.salt, self.B)
            self.http.send("POST", "/pair-setup", tlv_write([(TLV_SEQ, b"\x03"), (TLV_PUBLIC_KEY, A),
                                                             (TLV_PROOF, M1)]))
            m4 = self._tlv_response()
            if TLV_ERROR in m4 or m4.get(TLV_PROOF) != srp.M2_expected:
                raise PairingError("wrong code")

            signing = Ed25519PrivateKey.generate()
            ltpk = _raw(signing.public_key())
            x = hkdf("Pair-Setup-Controller-Sign-Salt", "Pair-Setup-Controller-Sign-Info", srp.K)
            sig = signing.sign(x + self.identifier + ltpk)
            sub = tlv_write([(TLV_IDENTIFIER, self.identifier), (TLV_PUBLIC_KEY, ltpk), (TLV_SIGNATURE, sig)])
            key = hkdf("Pair-Setup-Encrypt-Salt", "Pair-Setup-Encrypt-Info", srp.K)
            aead = ChaCha20Poly1305(key)
            enc = aead.encrypt(b"\x00\x00\x00\x00PS-Msg05", sub, None)
            self.http.send("POST", "/pair-setup", tlv_write([(TLV_SEQ, b"\x05"), (TLV_ENCRYPTED_DATA, enc)]))
            m6 = self._tlv_response()
            if TLV_ERROR in m6:
                raise PairingError("the device rejected the pairing")
            dev = tlv_read(aead.decrypt(b"\x00\x00\x00\x00PS-Msg06", m6[TLV_ENCRYPTED_DATA], None))
            dev_id, dev_ltpk, dev_sig = dev[TLV_IDENTIFIER], dev[TLV_PUBLIC_KEY], dev[TLV_SIGNATURE]
            dx = hkdf("Pair-Setup-Accessory-Sign-Salt", "Pair-Setup-Accessory-Sign-Info", srp.K)
            Ed25519PublicKey.from_public_bytes(dev_ltpk).verify(dev_sig, dx + dev_id + dev_ltpk)
            return _raw(signing), dev_id, dev_ltpk
        except (InvalidTag, InvalidSignature) as e:
            raise PairingError("the device's answer didn't check out") from e
        finally:
            self.close()

    def close(self):
        if self.http:
            self.http.close()
            self.http = None


def owntone_auth_key(client_seed, device_ltpk):
    """OwnTone stores: client Ed25519 secret key (64 B = seed + public) + device public key, as hex."""
    pub = _raw(Ed25519PrivateKey.from_private_bytes(client_seed).public_key())
    return (client_seed + pub + device_ltpk).hex()


def speaker_id(device_id):
    """OwnTone's speaker id: the AirPlay 'deviceid' (a MAC address) read as a number."""
    return int(device_id.replace(":", ""), 16)


# --------------------------------------------------------------------------- #
# Stored pairings
# --------------------------------------------------------------------------- #

PAIRINGS_FILE = DATA_DIR / "airplay-pairings.json"


def load_pairings():
    try:
        return json.loads(PAIRINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_pairing(device_id, name, auth_key):
    pairings = load_pairings()
    pairings[device_id] = {"name": name, "auth_key": auth_key, "paired": time.time()}
    PAIRINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAIRINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(pairings, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(PAIRINGS_FILE)


# --------------------------------------------------------------------------- #
# Private OwnTone instance
# --------------------------------------------------------------------------- #

def find_owntone():
    for candidate in (os.environ.get("LECTERN_OWNTONE"), shutil.which("owntone"), "/usr/sbin/owntone",
                      "/usr/bin/owntone"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def owntone_patched(binary=None):
    """True if OwnTone has the fixes Lectern ships in packaging/owntone (install.sh builds it)."""
    binary = binary or find_owntone()
    try:
        return binary is not None and b"OWNTONE_AIRPLAY_SAMPLE_RATE" in Path(binary).read_bytes()
    except OSError:
        return False


def device_sample_rate(integrator=""):
    """AirPlay stream rate: 44.1 kHz everywhere (tested on Roku TVs: 48 kHz played wrong there).
    LECTERN_AIRPLAY_RATE=48000 overrides it for receivers that need it."""
    forced = os.environ.get("LECTERN_AIRPLAY_RATE")
    if forced in ("44100", "48000"):
        return int(forced)
    return PCM_RATE


class OwnTone:
    """Runs and drives Lectern's own OwnTone (separate config, port and library)."""

    def __init__(self, base=None):
        self.base = Path(base or DATA_DIR / "owntone")
        self.music = self.base / "music"
        # The pipe needs an extension: OwnTone finds a pipe's own cover file by
        # cutting its path at the last "." (else it cuts at ~/.local).
        self.fifo = self.music / "lectern.pcm"
        self.metadata_fifo = self.music / "lectern.pcm.metadata"   # title / artist / cover for the TV
        self.cover_file = self.music / "lectern.jpg"               # the cover OwnTone falls back to
        self.db = self.base / "owntone.db"
        self.proc = None
        self.api = f"http://127.0.0.1:{OWNTONE_PORT}/api"

    @property
    def available(self):
        return find_owntone() is not None

    def _write_config(self, devices):
        self.music.mkdir(parents=True, exist_ok=True)
        dev_sections = "".join(f'airplay "{name}" {{\n\traop_disable = true\n}}\n' for name in devices)
        conf = f"""general {{
\tuid = "{os.environ.get('USER', '')}"
\tdb_path = "{self.db}"
\tcache_dir = "{self.base}"
\tlogfile = "{self.base / 'owntone.log'}"
\tloglevel = {os.environ.get('LECTERN_OWNTONE_LOGLEVEL', 'info')}
\twebsocket_port = 0
\tipv6 = no
\ttrusted_networks = {{ "localhost" }}
}}
library {{
\tname = "{OWNTONE_LIBRARY_NAME}"
\tport = {OWNTONE_PORT}
\tdirectories = {{ "{self.music}" }}
\tpipe_autostart = true
\tpipe_sample_rate = {PCM_RATE}
\tpipe_bits_per_sample = 16
}}
audio {{
\ttype = "disabled"
}}
airplay_shared {{
\tcontrol_port = {AIRPLAY_CONTROL_PORT}
\ttiming_port = {AIRPLAY_TIMING_PORT}
}}
{dev_sections}mpd {{
\tport = 0
}}
"""
        (self.base / "owntone.conf").write_text(conf)

    def install_keys(self):
        """Copy stored pairings into OwnTone's speaker table (OwnTone must not be running)."""
        pairings = load_pairings()
        if not pairings:
            return
        con = sqlite3.connect(self.db)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS speakers(id INTEGER PRIMARY KEY NOT NULL, "
                        "selected INTEGER NOT NULL, volume INTEGER NOT NULL, name VARCHAR(255) DEFAULT NULL, "
                        "auth_key VARCHAR(2048) DEFAULT NULL, format INTEGER DEFAULT 0, offset_ms INTEGER DEFAULT 0)")
            for dev_id, p in pairings.items():
                sid = speaker_id(dev_id)
                row = con.execute("SELECT volume FROM speakers WHERE id=?", (sid,)).fetchone()
                vol = row[0] if row else 60
                con.execute("INSERT OR REPLACE INTO speakers(id, selected, volume, name, auth_key, format, offset_ms) "
                            "VALUES(?, 0, ?, ?, ?, 0, 0)", (sid, vol, p["name"], p["auth_key"]))
            con.commit()
        finally:
            con.close()

    def set_cover(self, path):
        """Put the book's cover beside the pipe, where OwnTone looks when its metadata cover is gone."""
        jpeg = cover_jpeg(path) if path else None
        try:
            if jpeg is None:
                self.cover_file.unlink(missing_ok=True)
                return
            tmp = self.music / ".lectern-cover.tmp"
            tmp.write_bytes(jpeg)
            tmp.replace(self.cover_file)
        except OSError as e:
            log.info("couldn't write the AirPlay cover: %s", e)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout=30, sample_rate=PCM_RATE):
        if self.running():
            return
        binary = find_owntone()
        if not binary:
            raise OSError("OwnTone isn't installed (AUR package: owntone-server)")
        self._write_config([p["name"] for p in load_pairings().values()])
        self.install_keys()
        patched = owntone_patched(binary)
        for old in (self.music / "lectern", self.music / "lectern.metadata"):   # Lectern < 0.7.4
            if old.exists():
                old.unlink()
        if not self.fifo.exists():
            os.mkfifo(self.fifo)
        # A stock OwnTone crashes when playback restarts while it reads the
        # metadata pipe, so title and cover only go to a patched one.
        if patched and not self.metadata_fifo.exists():
            os.mkfifo(self.metadata_fifo)
        elif not patched and self.metadata_fifo.exists():
            self.metadata_fifo.unlink()
        env = dict(os.environ, OWNTONE_AIRPLAY_SAMPLE_RATE=str(sample_rate))
        log.info("starting OwnTone (%s, %s, AirPlay at %d Hz)", binary,
                 "with Lectern's fixes" if patched else "stock: no cover art", sample_rate)
        self.proc = subprocess.Popen([binary, "-f", "-c", str(self.base / "owntone.conf")],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, env=env)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise OSError("OwnTone stopped right after starting; see " + str(self.base / "owntone.log"))
            try:
                self.call("GET", "/config")
                return
            except OSError:
                time.sleep(0.3)
        raise OSError("OwnTone didn't start")

    def stop(self):
        if self.running():
            try:
                self.call("PUT", "/player/stop")
                self.call("PUT", "/outputs/set", {"outputs": []})
            except OSError:
                pass
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def call(self, method, path, body=None, timeout=5):
        data = json.dumps(body).encode() if body is not None else (b"" if method in ("PUT", "POST") else None)
        req = urllib.request.Request(self.api + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise OSError(f"OwnTone {method} {path}: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise OSError(str(e.reason)) from e
        return json.loads(raw) if raw[:1] in (b"{", b"[") else None

    def wait_for_output(self, sid, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for o in self.call("GET", "/outputs")["outputs"]:
                if int(o["id"]) == sid:
                    return o
            time.sleep(0.5)
        return None

    def play_pipe_to(self, sid, volume=None):
        """Select the AirPlay output and queue our pipe."""
        tracks = self.call("GET", "/search?type=tracks&query=lectern")["tracks"]["items"]
        pipe = next((t for t in tracks if t.get("data_kind") == "pipe"), None)
        if pipe is None:
            raise OSError("OwnTone hasn't picked up Lectern's audio pipe")
        self.call("PUT", "/outputs/set", {"outputs": [str(sid)]})
        if volume is not None:
            self.call("PUT", f"/outputs/{sid}", {"volume": int(volume)})
        self.call("POST", f"/queue/items/add?uris=library:track:{pipe['id']}&clear=true&playback=start")

    def set_volume(self, sid, volume):
        self.call("PUT", f"/outputs/{sid}", {"volume": int(max(0, min(100, volume)))})

    def wait_for_pipe(self, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                items = self.call("GET", "/search?type=tracks&query=lectern")["tracks"]["items"]
                if any(t.get("data_kind") == "pipe" for t in items):
                    return True
            except OSError:
                pass
            time.sleep(0.5)
        return False


# --------------------------------------------------------------------------- #
# Feeding OwnTone's pipes
# --------------------------------------------------------------------------- #

class PipeWriter:
    """Writes whole messages into a FIFO that OwnTone reads (the metadata pipe).

    Opening the FIFO read-write never blocks. A message is written only once it
    fits completely, so a reader that is slow or not running yet never sees a
    half-written item (OwnTone stops reading the pipe for good after one).
    """

    def __init__(self, path, size=1 << 20):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            fcntl.fcntl(self.fd, fcntl.F_SETPIPE_SZ, size)   # room for a cover picture
        except OSError:
            pass
        self.dropped = 0

    def free_space(self):
        if self.fd < 0:
            raise OSError("closed")
        size = fcntl.fcntl(self.fd, fcntl.F_GETPIPE_SZ)
        queued = struct.unpack("i", fcntl.ioctl(self.fd, termios.FIONREAD, b"\0\0\0\0"))[0]
        return size - queued

    def write(self, data, timeout=5.0):
        """-> True if all of ``data`` went in, False if it was dropped (pipe stayed full)."""
        deadline = time.monotonic() + timeout
        try:
            while self.free_space() < len(data):
                if time.monotonic() > deadline:
                    self.dropped += len(data)
                    return False
                time.sleep(0.05)
            view = memoryview(data)
            while view:
                try:
                    view = view[os.write(self.fd, view):]
                except BlockingIOError:
                    select.select([], [self.fd], [], 0.05)
        except OSError:          # closed meanwhile (casting stopped)
            return False
        return True

    def close(self):
        fd, self.fd = self.fd, -1
        try:
            os.close(fd)
        except OSError:
            pass


def drain_fifo(fd):
    """Throw away whatever is queued in a FIFO we hold open read-write (stale audio after a seek)."""
    try:
        while os.read(fd, 1 << 16):
            pass
    except (BlockingIOError, OSError):
        pass


# --------------------------------------------------------------------------- #
# The session Player uses (Player.start_output)
# --------------------------------------------------------------------------- #

def metadata_item(kind, code, data=b""):
    """One item in the shairport-sync metadata format OwnTone's pipe input reads."""
    import base64
    if isinstance(data, str):
        data = data.encode()
    head = f"<item><type>{kind.encode().hex()}</type><code>{code.encode().hex()}</code><length>{len(data)}</length>"
    body = f'\n<data encoding="base64">\n{base64.b64encode(data).decode()}</data>' if data else ""
    return (head + body + "</item>\n").encode()


def cover_jpeg(path, size=500):
    """The cover scaled down for the TV, as JPEG bytes (None if unavailable)."""
    if not path:
        return None
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
        ok, data = pb.save_to_bufferv("jpeg", ["quality"], ["88"])
        return bytes(data) if ok else None
    except Exception:  # noqa: BLE001 - a missing cover is not an error
        return None


class AirPlayOutput:
    """Lectern keeps playing locally; its processed audio goes to OwnTone → AirPlay device."""

    local_output = True

    def __init__(self, name, device_id, owntone):
        from concurrent.futures import ThreadPoolExecutor
        self.name = name
        self.device_id = device_id
        self.sid = speaker_id(device_id)
        self.owntone = owntone
        # OwnTone's audio pipe. GStreamer's fdsink writes it: it waits while the
        # pipe is full (that is what paces playback) yet stays responsive to
        # pause, seek and stop.
        self.audio_fd = os.open(owntone.fifo, os.O_RDWR | os.O_NONBLOCK)
        self.meta_writer = PipeWriter(str(owntone.metadata_fifo)) if owntone.metadata_fifo.exists() else None
        self._handlers = []
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="airplay")
        self._meta_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="airplay-meta")
        self._base_volume = None
        self._sent_volume = None
        self._watchdog = 0
        self._sent_meta = None          # (book id, title) last sent
        self._pict_book = None          # book whose cover OwnTone holds
        self._pict_time = 0.0
        self._meta_timer = 0
        self._cover_book = None         # book whose cover file is beside the pipe

    def make_sink(self):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        # sync=false: never play by Lectern's clock. OwnTone reads at the TV's
        # pace and the full pipe holds the player back.
        b = Gst.parse_bin_from_description(
            "audioconvert ! audioresample ! "
            f"audio/x-raw,format=S16LE,rate={PCM_RATE},channels={PCM_CHANNELS},layout=interleaved ! "
            "fdsink name=out sync=false", True)
        b.get_by_name("out").set_property("fd", self.audio_fd)
        return b

    def latency(self):
        """Seconds between the audio Lectern has produced and what the TV plays now."""
        try:
            queued = struct.unpack("i", fcntl.ioctl(self.audio_fd, termios.FIONREAD, b"\0\0\0\0"))[0]
        except OSError:
            queued = 0
        return queued / (PCM_RATE * PCM_CHANNELS * 2) + OWNTONE_BUFFERED

    def _submit(self, fn, *args):
        def run():
            try:
                fn(*args)
            except OSError as e:
                log.info("OwnTone call failed: %s", e)
        self._pool.submit(run)

    def attach(self, player):
        """Send the book/chapter title and cover to the TV, now and whenever they change."""
        from gi.repository import GLib
        self._player = player
        self._handlers = [player.connect("book-changed", lambda *_: self.send_metadata()),
                          player.connect("chapter-changed", lambda *_: self.send_metadata())]
        self.send_metadata()
        self._watchdog = GLib.timeout_add_seconds(2, self._check_owntone)

    def _check_owntone(self):
        """If OwnTone dies, nothing reads the pipe and playback would just hang: say so instead."""
        if self.owntone.running():
            return True
        self._watchdog = 0
        p = getattr(self, "_player", None)
        if p is not None and p.output is self:
            log.warning("OwnTone stopped while playing to %s", self.name)
            p.stop_output()
            p.pause()
            p.emit("error", f"Lost the connection to {self.name}")
        return False

    def send_metadata(self, delay=0.5):
        """Title (chapter), author, book and cover for the TV's now-playing screen, soon.

        Requests are coalesced: OwnTone keeps the cover in a temp file that every
        new picture replaces, so two pictures close together leave the TV asking
        for a file that is already gone (and showing no cover).
        """
        from gi.repository import GLib
        if self._meta_timer:
            GLib.source_remove(self._meta_timer)
        self._meta_timer = GLib.timeout_add(int(delay * 1000), self._send_metadata_now)

    def _send_metadata_now(self):
        self._meta_timer = 0
        p = getattr(self, "_player", None)
        if p is None or p.book is None:
            return False
        if self._cover_book != p.book.id:
            self._cover_book = p.book.id
            self._meta_pool.submit(self.owntone.set_cover, p.book.cover_path)
        if self.meta_writer is None:
            return False
        book, ch = p.book, p.chapter
        title = ch.title if ch is not None and len(book.chapters) > 1 else book.title
        with_pict = self._pict_book != book.id
        if with_pict and time.monotonic() - self._pict_time < PICT_SPACING:
            self.send_metadata(PICT_SPACING - (time.monotonic() - self._pict_time))
            return False
        key = (book.id, title)
        if key == self._sent_meta and not with_pict:
            return False
        self._sent_meta = key
        if with_pict:
            self._pict_book, self._pict_time = book.id, time.monotonic()
            # OwnTone tells the TV about a cover only on the next metadata change
            # after it has stored it, so follow up with the text once more.
            from gi.repository import GLib
            GLib.timeout_add(int(PICT_SPACING * 1000), self._refresh_text)
        items = [metadata_item("core", "minm", title), metadata_item("core", "asar", book.author or ""),
                 metadata_item("core", "asal", book.title)]
        cover = book.cover_path if with_pict else None

        def write():
            jpeg = cover_jpeg(cover) if cover else None
            data = b"".join(items) + (metadata_item("ssnc", "PICT", jpeg) if jpeg else b"")
            if not self.meta_writer.write(data):
                log.info("AirPlay metadata dropped: OwnTone isn't reading it")
        self._meta_pool.submit(write)
        return False

    def _refresh_text(self):
        if getattr(self, "_player", None) is not None and self.meta_writer is not None and self.meta_writer.fd >= 0:
            self._sent_meta = None
            self._send_metadata_now()
        return False

    def on_play(self):
        self._submit(self.owntone.call, "PUT", "/player/play")
        # OwnTone deletes the cover when it pauses; the text resend makes it tell the TV again
        self._pict_book = self._sent_meta = None
        self.send_metadata(0.3)

    def on_pause(self):
        self._submit(self.owntone.call, "PUT", "/player/pause")

    def on_seek(self):
        """After a jump: drop the old audio still queued in the pipe and in OwnTone."""
        drain_fifo(self.audio_fd)
        if self.meta_writer is not None:
            self._meta_pool.submit(self.meta_writer.write, metadata_item("ssnc", "pfls"), 0.5)
        self._sent_meta = None          # the flush clears the TV's now-playing info: send it again
        self.send_metadata(0.3)

    def set_fade(self, level):
        def go():
            if self._base_volume is None:
                outputs = self.owntone.call("GET", "/outputs")["outputs"]
                self._base_volume = next((o["volume"] for o in outputs if int(o["id"]) == self.sid), 60)
            vol = int(round(self._base_volume * max(0.0, min(1.0, level))))
            if vol != self._sent_volume:
                self._sent_volume = vol
                self.owntone.set_volume(self.sid, vol)
            if level >= 1.0:
                self._base_volume = self._sent_volume = None
        self._submit(go)

    def stop(self):
        p = getattr(self, "_player", None)
        for h in self._handlers:
            p.disconnect(h)
        self._handlers = []
        from gi.repository import GLib
        if self._watchdog:
            GLib.source_remove(self._watchdog)
            self._watchdog = 0
        try:
            os.close(self.audio_fd)
        except OSError:
            pass
        if self.meta_writer:
            self.meta_writer.close()
        self._submit(self.owntone.stop)
        if self._meta_timer:
            GLib.source_remove(self._meta_timer)
            self._meta_timer = 0
        self._pool.shutdown(wait=False)
        self._meta_pool.shutdown(wait=False)


def firewall_blocks_airplay():
    """True if ufw is on and lacks rules for the AirPlay timing/clock ports (the TV must reach them)."""
    try:
        if "ENABLED=yes" not in Path("/etc/ufw/ufw.conf").read_text():
            return False
        rules = Path("/etc/ufw/user.rules").read_text()
        return f"{AIRPLAY_CONTROL_PORT}:{AIRPLAY_TIMING_PORT}" not in rules or "319:320" not in rules
    except OSError:
        return False


def ptp_unavailable(binary=None):
    """True if OwnTone can't bind the PTP clock ports (319/320): AirPlay 2 TVs then get NTP
    timing, which some (e.g. Roku) handle badly: fast, chipmunk-like, stuttering audio."""
    binary = binary or find_owntone()
    if not binary:
        return False
    try:
        out = subprocess.run(["getcap", binary], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    start = int(Path("/proc/sys/net/ipv4/ip_unprivileged_port_start").read_text().strip() or 1024)
    return "cap_net_bind_service" not in out and start > 319
