"""Turn the screen and keyboard backlight off until the keyboard or mouse is used.

Keyboard backlight: UPower's KbdBacklight D-Bus interface (no root needed);
the previous brightness is restored on wake.

Screen: per compositor
  * Hyprland: dpms off; mouse/key wake is switched on just for this, then the
    user's own setting is restored; wake is noticed by polling dpmsStatus
  * GNOME: Mutter's PowerSaveMode (wakes on input by itself)
  * KDE Plasma: PowerDevil's "Turn Off Screen" (wakes on input by itself)

Also: an idle-time sleep inhibitor while audio plays (systemd-logind), so the
computer doesn't suspend in the middle of a book.
"""
import json
import logging
import os
import shutil
import subprocess

from gi.repository import Gio, GLib, GObject

log = logging.getLogger(__name__)

OFF_DELAY_MS = 800      # let the click that triggered us finish, so it doesn't wake the screen
POLL_MS = 500


def _run(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        log.info("%s failed: %s", args[0], e)
        return ""


class KeyboardBacklight:
    def __init__(self):
        self._saved = None
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
                "org.freedesktop.UPower", "/org/freedesktop/UPower/KbdBacklight",
                "org.freedesktop.UPower.KbdBacklight", None)
            self.available = self.proxy.call_sync("GetMaxBrightness", None, 0, 2000, None)[0] > 0
        except GLib.Error as e:
            log.info("no keyboard backlight control: %s", e.message)
            self.proxy, self.available = None, False

    def off(self):
        if not self.available:
            return
        try:
            level = self.proxy.call_sync("GetBrightness", None, 0, 2000, None)[0]
            if level > 0:           # don't overwrite a saved level with 0
                self._saved = level
            self.proxy.call_sync("SetBrightness", GLib.Variant("(i)", (0,)), 0, 2000, None)
        except GLib.Error as e:
            log.warning("keyboard backlight off failed: %s", e.message)

    def restore(self):
        if not self.available or self._saved is None:
            return
        try:
            self.proxy.call_sync("SetBrightness", GLib.Variant("(i)", (self._saved,)), 0, 2000, None)
        except GLib.Error as e:
            log.warning("keyboard backlight restore failed: %s", e.message)
        self._saved = None


class HyprlandScreen:
    name = "Hyprland"
    WAKE_OPTIONS = ("misc:key_press_enables_dpms", "misc:mouse_move_enables_dpms")

    def __init__(self):
        # Hyprland with a Lua config only accepts Lua dispatchers / eval; older ones use keywords.
        # A no-op Lua statement tells them apart without changing anything.
        self.lua = _run("hyprctl", "eval", "local _ = 1") == "ok"
        self._saved = {}

    def _set(self, option, value):
        if self.lua:
            section, key = option.split(":")
            _run("hyprctl", "eval", f"hl.config({{ {section} = {{ {key} = {str(value).lower()} }} }})")
        else:
            _run("hyprctl", "keyword", option, str(value).lower())

    def _get(self, option):
        try:
            data = json.loads(_run("hyprctl", "-j", "getoption", option))
        except ValueError:
            return False
        return bool(data.get("bool", data.get("int", 0)))

    def off(self):
        self._saved = {o: self._get(o) for o in self.WAKE_OPTIONS}
        for o in self.WAKE_OPTIONS:
            self._set(o, True)
        if self.lua:
            _run("hyprctl", "dispatch", 'hl.dsp.dpms({ action = "off" })')
        else:
            _run("hyprctl", "dispatch", "dpms", "off")

    def is_off(self):
        try:
            monitors = json.loads(_run("hyprctl", "-j", "monitors") or "[]")
        except ValueError:
            return False
        return any(not m.get("dpmsStatus", True) for m in monitors)

    def woke(self):
        for o, v in self._saved.items():
            self._set(o, v)


class GnomeScreen:
    name = "GNOME"

    def __init__(self):
        self.proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None, "org.gnome.Mutter.DisplayConfig",
            "/org/gnome/Mutter/DisplayConfig", "org.gnome.Mutter.DisplayConfig", None)
        if self.proxy.get_name_owner() is None:
            raise RuntimeError("Mutter not running")

    def _props(self, method, *args):
        return self.proxy.get_connection().call_sync(
            "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
            "org.freedesktop.DBus.Properties", method, GLib.Variant(*args) if args else None,
            None, 0, 2000, None)

    def off(self):
        self._props("Set", "(ssv)", ("org.gnome.Mutter.DisplayConfig", "PowerSaveMode", GLib.Variant("i", 3)))

    def is_off(self):
        try:
            return self._props("Get", "(ss)", ("org.gnome.Mutter.DisplayConfig", "PowerSaveMode"))[0] != 0
        except GLib.Error:
            return False

    def woke(self):
        pass


class KdeScreen:
    name = "KDE Plasma"

    def __init__(self):
        if not shutil.which("gdbus"):
            raise RuntimeError("gdbus missing")
        self._off = False

    def off(self):
        _run("gdbus", "call", "--session", "--dest", "org.kde.kglobalaccel", "--object-path",
             "/component/org_kde_powerdevil", "--method", "org.kde.kglobalaccel.Component.invokeShortcut",
             "Turn Off Screen")
        self._off = True

    def is_off(self):
        # Plasma gives no simple signal for this; Lectern restores the keyboard
        # light the next time its window sees input (see ScreenControl.user_active).
        return self._off

    def woke(self):
        self._off = False


def _detect_screen():
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    candidates = []
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
        candidates.append(HyprlandScreen)
    if "gnome" in desktop:
        candidates.append(GnomeScreen)
    if "kde" in desktop:
        candidates.append(KdeScreen)
    for cls in candidates:
        try:
            return cls()
        except (RuntimeError, GLib.Error) as e:
            log.info("%s screen control unavailable: %s", cls.name, e)
    return None


class ScreenControl(GObject.Object):
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__()
        self.screen = _detect_screen()
        self.keyboard = KeyboardBacklight()
        self.off = False
        self._poll = 0
        log.info("screen control: %s, keyboard backlight: %s",
                 self.screen.name if self.screen else "none", self.keyboard.available)

    @property
    def available(self):
        return self.screen is not None or self.keyboard.available

    def turn_off(self):
        if self.off or not self.available:
            return
        self.off = True
        self.emit("changed")
        GLib.timeout_add(OFF_DELAY_MS, self._do_off)

    def _do_off(self):
        log.info("turning screen and keyboard light off")
        self.keyboard.off()
        if self.screen:
            self.screen.off()
        self._poll = GLib.timeout_add(POLL_MS, self._check_wake)
        return False

    def _check_wake(self):
        if not self.detects_wake or self.screen.is_off():
            return True  # still dark (or waiting for user_active())
        self._woke()
        return False

    def restore_now(self):
        """Turn everything back on immediately (used when Lectern quits)."""
        if not self.off:
            return
        if self._poll:
            GLib.source_remove(self._poll)
        self._woke()

    @property
    def detects_wake(self):
        return self.screen is not None and not isinstance(self.screen, KdeScreen)

    def user_active(self):
        """Called on input in Lectern's window; only used where wake can't be detected.

        On Hyprland/GNOME the compositor's own DPMS state is authoritative:
        powering the display off sends the window a stray pointer event, which
        must not count as the user coming back.
        """
        if self.detects_wake:
            return
        if self.off and self._poll:
            GLib.source_remove(self._poll)
            self._woke()

    def _woke(self):
        log.info("screen woke up; restoring keyboard light")
        self._poll = 0
        self.off = False
        if self.screen:
            self.screen.woke()
        self.keyboard.restore()
        self.emit("changed")


class SleepInhibitor:
    """Hold a systemd-logind 'sleep' block inhibitor while audio is playing."""

    def __init__(self):
        self._fd = None

    def set(self, active):
        if active and self._fd is None:
            try:
                bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
                result, fds = bus.call_with_unix_fd_list_sync(
                    "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                    "Inhibit", GLib.Variant("(ssss)", ("sleep", "Lectern", "Playing an audiobook", "block")),
                    GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, 3000, None, None)
                self._fd = fds.get(result[0])
                log.debug("sleep inhibitor taken")
            except GLib.Error as e:
                log.info("cannot inhibit sleep: %s", e.message)
        elif not active and self._fd is not None:
            os.close(self._fd)
            self._fd = None
            log.debug("sleep inhibitor released")
