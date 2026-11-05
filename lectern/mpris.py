"""MPRIS2 D-Bus interface so media keys, desktop widgets and playerctl work.

Tracks map to chapters: Next/Previous jump chapters and Position/Length are
relative to the current chapter, which is what media widgets display best.
"""
import logging
import os

from gi.repository import Gio, GLib

from .const import APP_ID, APP_NAME, APP_SLUG

log = logging.getLogger(__name__)

XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
US = 1_000_000


class MprisService:
    def __init__(self, app, player):
        self.app = app
        self.player = player
        self._conn = None
        self._registrations = []
        self._node = Gio.DBusNodeInfo.new_for_xml(XML)
        # test/dev instances get their own name so tooling never controls the user's real player
        suffix = f".test{os.getpid()}" if os.environ.get("LECTERN_NON_UNIQUE") or os.environ.get("LECTERN_SCREENSHOT") else ""
        self.bus_name = f"org.mpris.MediaPlayer2.{APP_SLUG}{suffix}"
        self._owner = Gio.bus_own_name(Gio.BusType.SESSION, self.bus_name,
                                       Gio.BusNameOwnerFlags.NONE, self._on_bus, None,
                                       lambda *_: log.warning("could not own MPRIS bus name"))
        player.connect("state-changed", lambda *_: self._changed(PLAYER_IFACE, "PlaybackStatus", "CanPlay", "CanPause", "CanSeek"))
        player.connect("book-changed", lambda *_: self._changed(PLAYER_IFACE, "Metadata", "CanGoNext", "CanGoPrevious"))
        player.connect("chapter-changed", lambda *_: self._changed(PLAYER_IFACE, "Metadata", "CanGoNext", "CanGoPrevious"))
        player.connect("rate-changed", lambda *_: self._changed(PLAYER_IFACE, "Rate"))
        player.connect("seeked", self._on_seeked)

    def _on_bus(self, conn, _name):
        self._conn = conn
        for iface in self._node.interfaces:
            self._registrations.append(conn.register_object(
                OBJECT_PATH, iface, self._method_call, self._get_property, self._set_property))

    def close(self):
        if self._conn is not None:
            for reg in self._registrations:
                self._conn.unregister_object(reg)
        self._registrations = []
        self._conn = None
        Gio.bus_unown_name(self._owner)

    # ------------------------------------------------------------ helpers
    def _chapter_bounds(self):
        p = self.player
        ch = p.chapter
        if ch is not None:
            return ch.start, ch.end
        return 0.0, p.duration

    def _track_id(self):
        p = self.player
        if not p.book:
            return "/org/mpris/MediaPlayer2/TrackList/NoTrack"
        return f"/app/lectern/Lectern/book{p.book.id or 0}/chapter{max(0, p.chapter_index)}"

    def _metadata(self):
        p = self.player
        if not p.book:
            return {"mpris:trackid": GLib.Variant("o", self._track_id())}
        b = p.book
        start, end = self._chapter_bounds()
        ch = p.chapter
        title = ch.title if ch is not None and len(b.chapters) > 1 else b.title
        md = {
            "mpris:trackid": GLib.Variant("o", self._track_id()),
            "mpris:length": GLib.Variant("x", int((end - start) * US)),
            "xesam:title": GLib.Variant("s", title),
            "xesam:album": GLib.Variant("s", b.title),
            "xesam:artist": GLib.Variant("as", [b.author] if b.author else []),
            "xesam:genre": GLib.Variant("as", ["Audiobook"]),
        }
        if b.cover_path:
            md["mpris:artUrl"] = GLib.Variant("s", Gio.File.new_for_path(b.cover_path).get_uri())
        if len(b.chapters) > 1:
            md["xesam:trackNumber"] = GLib.Variant("i", p.chapter_index + 1)
        return md

    def _value(self, iface, prop):
        p = self.player
        if iface == ROOT_IFACE:
            return {
                "CanQuit": GLib.Variant("b", True),
                "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False),
                "Identity": GLib.Variant("s", APP_NAME),
                "DesktopEntry": GLib.Variant("s", APP_ID),
                "SupportedUriSchemes": GLib.Variant("as", ["file"]),
                "SupportedMimeTypes": GLib.Variant("as", ["audio/mpeg", "audio/mp4", "audio/x-m4b", "audio/ogg",
                                                          "audio/opus", "audio/flac"]),
            }.get(prop)
        start, _ = self._chapter_bounds()
        has_book = p.book is not None
        n = len(p.book.chapters) if has_book else 0
        return {
            "PlaybackStatus": GLib.Variant("s", "Playing" if p.playing else ("Paused" if has_book else "Stopped")),
            "LoopStatus": GLib.Variant("s", "None"),
            "Rate": GLib.Variant("d", p.rate),
            "Shuffle": GLib.Variant("b", False),
            "Metadata": GLib.Variant("a{sv}", self._metadata()),
            "Volume": GLib.Variant("d", p.volume),
            "Position": GLib.Variant("x", int(max(0.0, p.position - start) * US)),
            "MinimumRate": GLib.Variant("d", 0.5),
            "MaximumRate": GLib.Variant("d", 3.0),
            "CanGoNext": GLib.Variant("b", has_book and p.chapter_index + 1 < n),
            "CanGoPrevious": GLib.Variant("b", has_book),
            "CanPlay": GLib.Variant("b", has_book),
            "CanPause": GLib.Variant("b", has_book),
            "CanSeek": GLib.Variant("b", has_book),
            "CanControl": GLib.Variant("b", True),
        }.get(prop)

    def _changed(self, iface, *props):
        if self._conn is None:
            return
        changed = {k: v for k in props if (v := self._value(iface, k)) is not None}
        self._conn.emit_signal(None, OBJECT_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                               GLib.Variant("(sa{sv}as)", (iface, changed, [])))

    def _on_seeked(self, _player, pos):
        if self._conn is None:
            return
        start, _ = self._chapter_bounds()
        self._conn.emit_signal(None, OBJECT_PATH, PLAYER_IFACE, "Seeked",
                               GLib.Variant("(x)", (int(max(0.0, pos - start) * US),)))

    # ------------------------------------------------------------ D-Bus
    def _get_property(self, _conn, _sender, _path, iface, prop):
        return self._value(iface, prop)

    def _set_property(self, _conn, _sender, _path, iface, prop, value):
        if prop == "Rate":
            self.player.set_rate(value.unpack())
        elif prop == "Volume":
            self.player.volume = value.unpack()
            self._changed(PLAYER_IFACE, "Volume")
        return True

    def _method_call(self, _conn, _sender, _path, iface, method, params, invocation):
        p = self.player
        args = params.unpack()
        if method == "Raise":
            self.app.activate()
        elif method == "Quit":
            self.app.quit()
        elif method == "PlayPause":
            p.toggle() if p.book else self.app.activate_action("resume-last", None)
        elif method == "Play":
            p.play() if p.book else self.app.activate_action("resume-last", None)
        elif method in ("Pause", "Stop"):
            p.pause()
        elif method == "Next":
            p.next_chapter()
        elif method == "Previous":
            p.previous_chapter()
        elif method == "Seek":
            p.skip(args[0] / US)
        elif method == "SetPosition":
            if args[0] == self._track_id():
                start, _ = self._chapter_bounds()
                p.seek(start + args[1] / US)
        invocation.return_value(None)
