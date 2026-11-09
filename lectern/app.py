import logging
import os
import signal
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import metadata  # noqa: E402
from .audiofx import EffectSettings  # noqa: E402
from .cast import CastManager  # noqa: E402
from .const import APP_ID, APP_NAME, DB_PATH, SCAN_VERSION, VERSION, ensure_dirs  # noqa: E402
from .covers import CoverFetcher  # noqa: E402
from .db import DEFAULT_SETTINGS, Database  # noqa: E402
from .log import setup_logging  # noqa: E402
from .mpris import MprisService  # noqa: E402
from .player import Player  # noqa: E402
from .scanner import OPENED_ROOT, LibraryWatcher, Scanner, build_book, candidate_for  # noqa: E402
from .screen import ScreenControl, SleepInhibitor  # noqa: E402
from .sleeptimer import SleepTimer  # noqa: E402
from .util import format_time, run_in_thread  # noqa: E402

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent


class Application(Adw.Application):
    __gsignals__ = {
        # emitted when stored book data changed (covers, metadata, progress flags)
        "library-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "book-updated": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "bookmarks-changed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self):
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if os.environ.get("LECTERN_SCREENSHOT") or os.environ.get("LECTERN_NON_UNIQUE"):
            # dev/test runs must not hand off to (or disturb) a running Lectern
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.window = None
        self._settings = {}
        self._paused_at = None
        self._was_playing = False
        self._listen_mark = None      # monotonic time listening was last accounted
        self._held = False            # app kept alive with the window hidden
        self._undo_position = None    # (book id, position) before the last big jump
        self._firewall_fix = None
        self._last_bookmark = None
        self._played_since_load = False

    # ----------------------------------------------------------- lifecycle
    def do_startup(self):
        Adw.Application.do_startup(self)
        ensure_dirs()
        GLib.set_application_name(APP_NAME)
        self.db = Database(DB_PATH)
        self.player = Player()
        self.player.set_rate(float(self.setting("rate")))
        self.player.set_skip_silence(self.setting("skip_silence"))
        self.scanner = Scanner(self.db)
        self.watcher = LibraryWatcher()
        self.fetcher = CoverFetcher()
        self.sleep = SleepTimer(self.player, self.setting)
        self.mpris = MprisService(self, self.player)
        self.screen = ScreenControl()
        self.cast = CastManager()
        self.cast.connect("connected", self._on_cast_connected)
        self.cast.connect("failed", lambda _c, msg: self.toast(msg))
        self.cast.connect("pin-needed", self._on_pin_needed)
        self.player.connect("casting-changed", self._on_casting_changed)
        self.inhibitor = SleepInhibitor()

        self.player.connect("state-changed", self._on_player_state)
        self.player.connect("book-finished", self._on_book_finished)
        self.player.connect("seeked", lambda p, _pos: None if p.playing else self.save_progress())
        self.player.connect("error", lambda _p, msg: self.toast(f"Playback error: {msg}"))
        self.scanner.connect("finished", self._on_scan_finished)
        self.watcher.connect("changed", lambda *_: self.scanner.scan())
        self.fetcher.connect("cover-found", self._on_cover_found)
        self.fetcher.connect("cover-missed", self._on_cover_missed)
        self.sleep.connect("expired", self._on_sleep_expired)

        self._setup_style()
        self._setup_actions()
        # logout / pkill / systemd stop: quit cleanly so the position is written
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal, sig)
        self._autosave_id = GLib.timeout_add_seconds(3, self._autosave)  # worst case after a crash: 3 s lost

    def _setup_style(self):
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(str(HERE / "icons"))
        css = Gtk.CssProvider()
        css.load_from_path(str(HERE / "ui" / "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _setup_actions(self):
        def add(name, cb, accels=None, param=None):
            action = Gio.SimpleAction.new(name, param)
            action.connect("activate", cb)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

        add("quit", lambda *_: self.quit(), ["<Control>q"])
        add("preferences", lambda *_: self.window.show_preferences(), ["<Control>comma"])
        add("about", lambda *_: self._show_about())
        add("shortcuts", lambda *_: self.window.show_shortcuts(), ["<Control>question"])
        add("sleep-timer", lambda *_: self.window.show_sleep_timer(), ["<Control>t"])
        add("sleep-extend", lambda *_: self.sleep.extend())
        add("sleep-cancel", lambda *_: self.sleep.cancel())
        add("rescan", lambda *_: self.rescan(full=True), ["<Control>r", "F5"])
        add("add-folder", lambda *_: self.window.choose_folder())
        add("play-pause", lambda *_: self.toggle())
        add("screen-off", lambda *_: self.screen.turn_off(), ["<Control>b"])
        self.lookup_action("screen-off").set_enabled(self.screen.available)
        add("resume-last", lambda *_: self.resume_last())
        add("add-bookmark", lambda *_: self.add_bookmark(), ["<Control>d"])
        add("undo-jump", lambda *_: self.undo_jump(), ["<Control>z"])
        add("stats", lambda *_: self.window.show_stats())
        add("bookmark-note", lambda *_: self.window.edit_bookmark_note(self._last_bookmark))
        add("show-window", lambda *_: self.activate())
        add("stop-casting", lambda *_: self.stop_casting())
        add("copy-firewall-fix", lambda *_: self._copy_firewall_fix())

    def do_activate(self):
        if self.window is None:
            from .ui.window import MainWindow
            self.window = MainWindow(self)
            self._restore_last_book()
            if self.db.folders():
                self.watcher.watch(self.db.folders())
                full = self.setting("scan_version") != SCAN_VERSION  # metadata rules improved
                self.scanner.scan(full=full)
            self.set_setting("scan_version", SCAN_VERSION)
        if self._held:              # coming back from background playback
            self._held = False
            self.release()
        self.window.present()

    def _on_signal(self, sig):
        log.info("received signal %d, saving and quitting", sig)
        self.quit()
        return GLib.SOURCE_REMOVE

    def do_open(self, files, _n, _hint):
        """Open With → Lectern: play the book a file or folder belongs to, importing it if needed."""
        self.activate()
        path = files[0].get_path() if files else None
        if not path:
            return
        roots = self.db.folders()

        def find():
            c = candidate_for(path, roots)
            if c is None:
                return None
            book = next((b for b in self.db.all_books() if b.path == c.path), None)
            if book:
                return book.id
            data = build_book(c)
            if not any(c.path.startswith(r.rstrip("/") + "/") for r in roots):
                data["root"] = OPENED_ROOT
            return self.db.upsert_book(data)

        def opened(book_id):
            book = self.db.get_book(book_id) if book_id else None
            if book is None:
                self.toast("Lectern can't play that file")
                return
            self.emit("library-changed")
            self.play_book(book)
            self.window.open_now_playing()
        run_in_thread(find, callback=opened, error_callback=lambda e: self.toast(f"Could not open: {e}"))

    # ------------------------------------------------------------- casting
    def cast_to(self, device):
        self.cast.connect_device(device)
        self.toast(f"Connecting to {device.name}…")

    def stop_casting(self):
        self.player.stop_casting()
        self.player.stop_output()

    def _on_pin_needed(self, _mgr, name):
        dialog = Adw.AlertDialog(heading=f"Pair With {name}",
                                 body="Enter the 4-digit code shown on the TV. This is needed only once.")
        entry = Gtk.Entry(placeholder_text="0000", max_length=4, input_purpose=Gtk.InputPurpose.DIGITS,
                          activates_default=True, halign=Gtk.Align.CENTER, width_chars=6,
                          css_classes=["title-2", "numeric"])
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("pair", "Pair")
        dialog.set_response_appearance("pair", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("pair")
        dialog.connect("response", lambda _d, r: self.cast.submit_pin(
            entry.get_text().strip() if r == "pair" and entry.get_text().strip().isdigit() else None))
        dialog.present(self.window)

    def _on_cast_connected(self, _mgr, session):
        if getattr(session, "local_output", False):
            self.player.start_output(session)
            self.toast(f"Playing on {session.name}")
            from .airplay import firewall_blocks_airplay, ptp_unavailable
            fixes = []
            if ptp_unavailable():
                fixes.append("sudo setcap cap_net_bind_service=+ep /usr/bin/owntone")
            if firewall_blocks_airplay():
                fixes.append("sudo ufw allow from 192.168.0.0/16 to any port 6001:6002 proto udp && "
                             "sudo ufw allow from 192.168.0.0/16 to any port 319:320 proto udp")
            if fixes:
                self._firewall_fix = " && ".join(fixes)
                self.toast("AirPlay timing isn't set up — sound may be choppy or too fast", "Copy Fix",
                           "app.copy-firewall-fix")
            return
        self.player.start_casting(session)
        self.toast(f"Playing on {session.name}")
        started = time.monotonic()
        # If the device never fetches the audio, a firewall is almost certainly in the way.
        GLib.timeout_add_seconds(12, self._check_cast_reachable, session, started)

    def _check_cast_reachable(self, session, started):
        if self.player.remote is session and self.player.book and self.cast.server.last_request < started:
            hint = self.cast.firewall_hint(session.host_ip)
            self._firewall_fix = hint
            if hint:
                self.toast(f"{session.name} can't reach this computer — your firewall blocks port "
                           f"{self.cast.port}", "Copy Fix", "app.copy-firewall-fix")
            else:
                self.toast(f"{session.name} can't reach this computer on port {self.cast.port}")
        return False

    def _copy_firewall_fix(self):
        fix = self._firewall_fix
        if fix and self.window:
            self.window.get_clipboard().set(fix)
            self.toast("Command copied — paste it in a terminal, then try casting again")

    def _on_casting_changed(self, player):
        if not player.casting:
            self.toast("Playing on this computer")

    def _on_sleep_expired(self, _timer):
        self.toast("Sleep timer ended — sweet dreams")
        if self.setting("sleep_end_screen_off"):
            self.screen.turn_off()  # comes back on with the keyboard or mouse

    def do_shutdown(self):
        if not hasattr(self, "_autosave_id"):     # startup failed (e.g. no display)
            Adw.Application.do_shutdown(self)
            return
        GLib.source_remove(self._autosave_id)
        self._account_listening()
        self.save_progress()
        self.inhibitor.set(False)
        self.mpris.close()
        self.screen.restore_now()  # never leave the keyboard light off
        self.player.shutdown()     # stops casting before our media server goes away
        self.cast.shutdown()
        self.db.close()
        Adw.Application.do_shutdown(self)

    # ------------------------------------------------------------ settings
    def setting(self, key):
        if key not in self._settings:
            self._settings[key] = self.db.get_setting(key, DEFAULT_SETTINGS.get(key))
        return self._settings[key]

    def set_setting(self, key, value):
        self._settings[key] = value
        self.db.set_setting(key, value)

    def keep_playing_in_background(self):
        """Window closed while playing: hide it and keep the app (and MPRIS) alive."""
        if self._held:
            return
        self._held = True
        self.hold()
        if not self.setting("background_hint_shown"):
            self.set_setting("background_hint_shown", True)
            note = Gio.Notification.new("Lectern keeps playing")
            note.set_body("Use your media keys or the media controls to pause. "
                          "Open Lectern again to bring the window back, or quit it from its menu.")
            self.send_notification("background", note)

    def toast(self, text, button=None, action=None):
        if self.window:
            self.window.toast(text, button, action)

    # ------------------------------------------------------------- library
    def add_folder(self, path):
        path = str(Path(path).expanduser().resolve())
        self.db.add_folder(path)
        self.set_setting("onboarded", True)
        self.watcher.watch(self.db.folders())
        self.rescan()

    def remove_folder(self, path):
        if self.player.book and self.player.book.path.startswith(path.rstrip("/") + "/"):
            self.save_progress()
            self.player.unload()
        self.db.remove_folder(path)
        self.watcher.watch(self.db.folders())
        self.emit("library-changed")

    def rescan(self, full=False):
        if not self.db.folders():
            self.toast("Add a library folder first")
            return
        self.scanner.scan(full=full)

    def _on_scan_finished(self, _scanner, stats):
        self.watcher.watch(self.db.folders())
        if self.setting("cover_auto_fetch") and self.setting("cover_auto_pick"):
            misses = self.setting("cover_misses") or {}
            for book in self.db.all_books():
                # don't ask the APIs again every rescan for books they had nothing for (retry weekly)
                key = f"{book.id}|{book.title}|{book.author}"
                if not book.cover_path and time.time() - misses.get(key, 0) > 7 * 86400:
                    self.fetcher.enqueue(book.id, book.path, book.title, book.author)
        if self.player.book and not self.db.get_book(self.player.book.id):
            self.player.unload()
        self.emit("library-changed")
        n = len(stats["added"])
        if n:
            self.toast(f"Added {n} book{'s' if n != 1 else ''} to your library")

    def _on_cover_found(self, _fetcher, book_id, path):
        if self.db.cover_source(book_id):  # the user picked one in the meantime
            return
        self.set_cover(book_id, path, "online")

    def _on_cover_missed(self, _fetcher, book_id):
        book = self.db.get_book(book_id)
        if book:
            misses = dict(self.setting("cover_misses") or {})
            misses[f"{book.id}|{book.title}|{book.author}"] = time.time()
            self.set_setting("cover_misses", misses)

    def set_cover(self, book_id, path, source):
        self.db.set_cover(book_id, path, source)
        if self.player.book and self.player.book.id == book_id:
            self.player.book.cover_path = path
            self.player.emit("book-changed")
        self.emit("book-updated", book_id)

    def update_metadata(self, book, fields, write_files=False, done=None):
        overrides = dict(book.overrides)
        overrides.update(fields)
        self.db.set_overrides(book.id, overrides)
        if self.player.book and self.player.book.id == book.id:
            for k, v in fields.items():
                setattr(self.player.book, k, v)
            self.player.emit("book-changed")
        self.emit("book-updated", book.id)
        if write_files:
            def write():
                for t in book.tracks:
                    metadata.write_tags(t.path, fields, single_file=book.kind == "file")
            run_in_thread(write, callback=lambda _r: (self.toast("Tags written to files"), done and done()),
                          error_callback=lambda e: self.toast(f"Could not write tags: {e}"))

    def embed_cover(self, book):
        if not book.cover_path:
            return
        mime = "image/png" if book.cover_path.lower().endswith(".png") else "image/jpeg"

        def work():
            data = Path(book.cover_path).read_bytes()
            for t in book.tracks:
                metadata.embed_cover(t.path, data, mime)
        run_in_thread(work, callback=lambda _r: self.toast("Cover embedded into the audio files"),
                      error_callback=lambda e: self.toast(f"Could not embed cover: {e}"))

    def mark_finished(self, book, finished):
        pos = book.duration if finished else 0.0
        if self.player.book and self.player.book.id == book.id:
            if finished:
                self.player.pause()
            self.player.seek(0 if not finished else max(0, book.duration - 1))
        self.db.save_progress(book.id, pos, finished=finished, touch=False)
        self.emit("book-updated", book.id)

    # ------------------------------------------------------------ playback
    def effects_for(self, book):
        """(EffectSettings, per_book)"""
        if book and book.audio:
            return EffectSettings.from_dict(book.audio), True
        return EffectSettings.from_dict(self.setting("audio_global")), False

    def set_effects(self, settings, per_book):
        book = self.player.book
        if per_book and book:
            book.audio = settings.to_dict()
            self.db.set_audio(book.id, book.audio)
        else:
            if book and book.audio:
                book.audio = None
                self.db.set_audio(book.id, None)
            self.set_setting("audio_global", settings.to_dict())
        self.player.set_effects(settings)

    def set_skip_silence(self, on):
        self.set_setting("skip_silence", bool(on))
        self.player.set_skip_silence(on)

    def set_rate(self, rate):
        """Speed is remembered per book; the last speed used is the default for new books."""
        self.player.set_rate(rate)
        self.set_setting("rate", self.player.rate)
        book = self.player.book
        if book and book.id is not None:
            book.rate = self.player.rate
            self.db.set_rate(book.id, book.rate)

    def play_book(self, book, position=None, autoplay=True):
        p = self.player
        if p.book and p.book.id == book.id:
            if position is not None:
                p.seek(position)
            if autoplay:
                p.play()
            return
        if p.playing:
            p.pause()  # saves the previous book's position
        else:
            self.save_progress()
        fresh = self.db.get_book(book.id) or book
        if fresh.finished and position is None:
            fresh.position = 0.0
            self.db.save_progress(fresh.id, 0.0, finished=False, touch=False)
            fresh.finished = False
        start = fresh.position if position is None else position
        p.load(fresh, start, autoplay=False)
        self._played_since_load = False
        self._paused_at = fresh.last_played or None
        p.set_effects(self.effects_for(fresh)[0])
        p.set_rate(fresh.rate or float(self.setting("rate")))
        self.set_setting("last_book", fresh.id)
        if autoplay:
            p.play()

    def toggle(self):
        if self.player.book:
            self.player.toggle()
        else:
            self.resume_last()

    def resume_last(self):
        book_id = self.setting("last_book")
        book = self.db.get_book(book_id) if book_id else None
        if book:
            self.play_book(book)

    def _restore_last_book(self):
        book_id = self.setting("last_book")
        book = self.db.get_book(book_id) if book_id else None
        if book:
            self.play_book(book, autoplay=False)

    def _on_player_state(self, player):
        self.inhibitor.set(player.playing)  # no suspend in the middle of a chapter
        self._account_listening()
        if player.playing and not self._was_playing:
            self._played_since_load = True
            self._maybe_rewind_on_resume()
            self._paused_at = None
        elif not player.playing and self._was_playing:
            self._paused_at = time.time()
            self.save_progress()
        self._was_playing = player.playing

    def _maybe_rewind_on_resume(self):
        if not self.setting("rewind_on_resume") or not self._paused_at:
            return
        idle = time.time() - self._paused_at
        if idle >= float(self.setting("rewind_after_minutes")) * 60 and self.player.position > 1:
            seconds = float(self.setting("rewind_seconds"))
            log.info("resuming after %.0f min: rewinding %.0f s", idle / 60, seconds)
            self.player.skip(-seconds)

    def _on_book_finished(self, player):
        if player.book:
            self.db.save_progress(player.book.id, player.book.duration, finished=True)
            self.emit("book-updated", player.book.id)
            self.toast(f"Finished “{player.book.title}”")

    def save_progress(self):
        p = self.player
        if not p.book or p.book.id is None:
            return
        pos = p.position
        p.book.position = pos
        # Only bump "last played" for books actually played, not ones merely restored.
        self.db.save_progress(p.book.id, pos, touch=self._played_since_load)

    def _account_listening(self):
        """Add real listening time (wall clock while playing) to today's statistics."""
        now = time.monotonic()
        book = self.player.book
        if self._listen_mark is not None and book and book.id is not None:
            elapsed = now - self._listen_mark
            if 0 < elapsed < 600:
                self.db.add_listening(book.id, elapsed)
        self._listen_mark = now if self.player.playing else None

    # ------------------------------------------------------- jumps & marks
    def jump_to(self, position, label=None):
        """A deliberate jump (chapter, bookmark, scrubbing) that can be undone."""
        p = self.player
        if not p.book:
            return
        before = p.position
        p.seek(position)
        if abs(position - before) >= 60:
            self._undo_position = (p.book.id, before)
            text = f"Jumped to {label}" if label else "Jumped"
            self.toast(text, "Undo", "app.undo-jump")

    def undo_jump(self):
        undo = self._undo_position
        if undo and self.player.book and self.player.book.id == undo[0]:
            self._undo_position = None
            self.player.seek(undo[1])

    def add_bookmark(self, note=""):
        p = self.player
        if not p.book:
            return
        pos = p.position
        bookmark_id = self.db.add_bookmark(p.book.id, pos, note)
        self._last_bookmark = bookmark_id
        self.toast(f"Bookmark added at {format_time(pos, force_hours=p.duration >= 3600)}",
                   "Add Note", "app.bookmark-note")
        self.emit("bookmarks-changed", p.book.id)

    def _autosave(self):
        if self.player.playing:
            self.save_progress()
            self._account_listening()
        return True

    def _show_about(self):
        about = Adw.AboutDialog(
            application_name=APP_NAME, application_icon=APP_ID, version=VERSION,
            developer_name="Lectern contributors", license_type=Gtk.License.GPL_3_0,
            comments="A calm audiobook player for Linux",
            website="https://aur.archlinux.org/packages/lectern")
        about.present(self.window)


def main():
    setup_logging()
    log.info("starting %s %s (pid %d)", APP_NAME, VERSION, os.getpid())
    app = Application()
    return app.run(sys.argv)
