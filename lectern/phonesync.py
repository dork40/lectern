"""Phone sync: lets Lectern for Android read and update book places over the home network.

The phone finds this PC through Zeroconf (``_lectern._tcp``) and talks to the media server
on port 8765 (already allowed through the firewall for casting). Every request carries a
6-digit pairing code shown in Preferences. Places are matched by the book's file name and
length, and the most recently played side wins; a book playing on the PC is never moved.
"""
import json
import logging
import os
import re
import secrets
import socket
import threading
import time

from gi.repository import GLib

log = logging.getLogger(__name__)

SERVICE_TYPE = "_lectern._tcp.local."


def book_key(book):
    """Same key the phone computes: the first audio file's name, letters and digits only."""
    path = book.tracks[0].path if book.tracks else book.path.split("::")[0]
    stem = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r"[^0-9a-z]+", "", stem.lower())


class PhoneSync:
    def __init__(self, app):
        self.app = app
        code = app.db.get_setting("phone_sync_code")
        if not code:
            code = f"{secrets.randbelow(1_000_000):06d}"
            app.db.set_setting("phone_sync_code", code)
        self.code = str(code)
        self._zconf = None
        self._info = None

    # ------------------------------------------------------------------ http
    def handle(self, method, path, headers, body):
        """Called from the media server's thread. Returns (status, dict)."""
        if headers.get("X-Lectern-Code", "").strip() != self.code:
            return 401, {"error": "wrong pairing code"}
        path = path.split("?")[0].rstrip("/")
        if method == "GET" and path == "/sync/books":
            return 200, self._on_main(self._books)
        if method == "POST" and path == "/sync/progress":
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return 400, {"error": "bad json"}
            return 200, self._on_main(lambda: self._apply(data))
        return 404, {"error": "not found"}

    def _on_main(self, fn):
        """Run ``fn`` on the GTK main loop (player and UI live there) and wait for its result."""
        done = threading.Event()
        box = {}

        def run():
            try:
                box["r"] = fn()
            except Exception as e:  # never take the server thread down
                log.exception("phone sync failed")
                box["r"] = {"error": str(e)}
            done.set()
            return False

        GLib.idle_add(run)
        done.wait(5)
        return box.get("r", {"error": "timeout"})

    def _books(self):
        p = self.app.player
        out = []
        for b in self.app.db.all_books():
            pos, last = b.position, b.last_played
            if p.book and p.book.id == b.id:
                pos = p.position
                if p.playing:
                    last = time.time()
            out.append({
                "key": book_key(b), "title": b.title, "author": b.author, "duration": b.duration,
                "position": pos, "finished": b.finished, "last_played": int(last * 1000),
                "playing": bool(p.book and p.book.id == b.id and p.playing),
            })
        return {"device": socket.gethostname(), "books": out}

    def _apply(self, d):
        key = d.get("key", "")
        dur = float(d.get("duration") or 0)
        book = next((b for b in self.app.db.all_books()
                     if book_key(b) == key and (not dur or abs(b.duration - dur) < 5)), None)
        if not book:
            return {"applied": False, "reason": "not in this library"}
        p = self.app.player
        if p.book and p.book.id == book.id and p.playing:
            return {"applied": False, "reason": "playing on this PC"}
        theirs = float(d.get("last_played") or 0) / 1000
        if theirs <= (book.last_played or 0) + 1:
            return {"applied": False, "reason": "this PC is newer"}
        pos = max(0.0, min(float(d.get("position") or 0), book.duration or 0))
        finished = bool(d.get("finished"))
        self.app.db.save_progress(book.id, pos, finished=finished, touch=False)
        self.app.db.set_last_played(book.id, theirs)
        if p.book and p.book.id == book.id:
            p.book.position = pos
            p.seek(pos)
        self.app.emit("book-updated", book.id)
        log.info("phone sync: %s → %.0f s", book.title, pos)
        return {"applied": True}

    # ------------------------------------------------------------- zeroconf
    def advertise(self, port):
        def go():
            try:
                import zeroconf
                ips = self._lan_ips()
                if not ips:
                    return
                host = socket.gethostname()
                self._info = zeroconf.ServiceInfo(
                    SERVICE_TYPE, f"Lectern on {host}.{SERVICE_TYPE}",
                    addresses=[socket.inet_aton(ip) for ip in ips], port=port,
                    properties={"v": "1", "host": host},
                )
                self._zconf = zeroconf.Zeroconf()
                self._zconf.register_service(self._info)
                log.info("phone sync advertised on %s port %d", ", ".join(ips), port)
            except Exception as e:
                log.warning("phone sync not advertised: %s", e)

        threading.Thread(target=go, daemon=True, name="phone-sync-zeroconf").start()

    @staticmethod
    def _lan_ips():
        """Every private IPv4 address, Wi-Fi first. The default route can be a USB tether, so
        the phone gets them all and uses whichever one answers."""
        import ipaddress
        import ifaddr
        found = []
        for adapter in ifaddr.get_adapters():
            if adapter.name.startswith(("lo", "docker", "veth", "br-", "virbr", "tailscale")):
                continue
            for ip in adapter.ips:
                if isinstance(ip.ip, str) and ipaddress.ip_address(ip.ip).is_private:
                    found.append((0 if adapter.name.startswith("wl") else 1, ip.ip))
        return [ip for _, ip in sorted(found)]

    def stop(self):
        try:
            if self._zconf and self._info:
                self._zconf.unregister_service(self._info)
            if self._zconf:
                self._zconf.close()
        except Exception:
            pass
