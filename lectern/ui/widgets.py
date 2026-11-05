"""Small widgets shared by the library header and the now-playing view."""
from gi.repository import Adw, GLib, Gtk

from ..audiofx import EffectSettings
from ..util import format_time

SPEED_PRESETS = (0.8, 1.0, 1.25, 1.5, 1.75, 2.0)


class SkipButton(Gtk.Button):
    """Circular-arrow button with the (configurable) number of seconds drawn inside."""

    def __init__(self, app, forward, css_classes=()):
        super().__init__(css_classes=["circular", "flat", *css_classes], valign=Gtk.Align.CENTER)
        self.app = app
        self.forward = forward
        overlay = Gtk.Overlay(child=Gtk.Image(icon_name="lectern-forward-symbolic" if forward
                                              else "lectern-rewind-symbolic"))
        self.number = Gtk.Label(css_classes=["skip-seconds", "numeric"], halign=Gtk.Align.CENTER,
                                valign=Gtk.Align.CENTER, can_target=False)
        overlay.add_overlay(self.number)
        self.set_child(overlay)
        self.connect("clicked", lambda *_: app.player.skip(self.seconds if forward else -self.seconds))
        self.refresh()

    @property
    def seconds(self):
        return float(self.app.setting("skip_forward" if self.forward else "skip_back"))

    def refresh(self):
        n = int(self.seconds)
        self.number.set_label(str(n))
        verb = "Forward" if self.forward else "Back"
        self.set_tooltip_text(f"{verb} {n} Seconds ({'→' if self.forward else '←'})")


def fmt_rate(rate):
    return f"{rate:.2f}".rstrip("0").rstrip(".") + "×" if rate % 1 else f"{rate:.1f}×"


class TimerIndicator(Gtk.Stack):
    """Moon button when idle; live countdown with +5 min and cancel while running."""

    def __init__(self, app):
        super().__init__(transition_type=Gtk.StackTransitionType.CROSSFADE, hhomogeneous=False,
                         interpolate_size=True, valign=Gtk.Align.CENTER)
        self.app = app
        idle = Gtk.Button(icon_name="lectern-sleep-symbolic", action_name="app.sleep-timer",
                          tooltip_text="Sleep Timer (Ctrl+T)")
        self.add_named(idle, "idle")

        running = Gtk.Box(css_classes=["timer-pill"], spacing=2)
        self.label = Gtk.Label(css_classes=["numeric", "timer-label"])
        open_btn = Gtk.Button(action_name="app.sleep-timer", tooltip_text="Change Sleep Timer",
                              css_classes=["flat", "timer-open"])
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image(icon_name="lectern-sleep-symbolic"))
        content.append(self.label)
        open_btn.set_child(content)
        running.append(open_btn)
        running.append(Gtk.Button(label="+5", action_name="app.sleep-extend",
                                  tooltip_text="Add 5 Minutes", css_classes=["flat", "timer-extend"]))
        running.append(Gtk.Button(icon_name="window-close-symbolic", action_name="app.sleep-cancel",
                                  tooltip_text="Cancel Sleep Timer", css_classes=["flat", "circular"]))
        self.add_named(running, "running")

        app.sleep.connect("changed", lambda *_: self._sync())
        app.sleep.connect("tick", lambda _t, remaining: self._show(remaining))
        self._sync()

    def _sync(self):
        timer = self.app.sleep
        self.set_visible_child_name("running" if timer.active else "idle")
        if timer.active:
            self._show(timer.remaining() or 0)

    def _show(self, remaining):
        text = format_time(remaining)
        if self.app.sleep.mode == "chapter":
            text = f"Chapter end · {text}"
        self.label.set_label(text)


class SpeedButton(Gtk.MenuButton):
    def __init__(self, app):
        super().__init__(tooltip_text="Playback Speed", valign=Gtk.Align.CENTER, css_classes=["flat", "numeric"])
        self.app = app
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12, width_request=280)
        self.value_label = Gtk.Label(css_classes=["title-2", "numeric"])
        box.append(self.value_label)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.5, 3.0, 0.05)
        self.scale.set_draw_value(False)
        for mark in (1.0, 1.5, 2.0, 2.5):
            self.scale.add_mark(mark, Gtk.PositionType.BOTTOM, None)
        self.scale.connect("value-changed", self._on_scale)
        box.append(self.scale)
        presets = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                              min_children_per_line=3, homogeneous=True, column_spacing=6, row_spacing=6)
        for r in SPEED_PRESETS:
            b = Gtk.Button(label=fmt_rate(r), css_classes=["pill", "small-pill"])
            b.connect("clicked", lambda _b, r=r: self.app.set_rate(r))
            presets.append(b)
        box.append(presets)
        if app.player.can_skip_silence:
            group = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
            skip = Adw.SwitchRow(title="Skip Silence", subtitle="Shorten long pauses",
                                 active=bool(app.setting("skip_silence")))
            skip.connect("notify::active", lambda r, _p: app.set_skip_silence(r.get_active()))
            group.append(skip)
            box.append(group)
        pop.set_child(box)
        self.set_popover(pop)
        self._syncing = False
        app.player.connect("rate-changed", lambda *_: self._sync())
        self._sync()

    def _sync(self):
        rate = self.app.player.rate
        self._syncing = True
        self.scale.set_value(rate)
        self._syncing = False
        self.set_label(fmt_rate(rate))
        self.value_label.set_label(fmt_rate(rate))

    def _on_scale(self, scale):
        if not self._syncing:
            self.app.set_rate(round(scale.get_value() * 20) / 20)


class AudioButton(Gtk.MenuButton):
    """Volume boost, voice clarity, night mode; global or per book."""

    def __init__(self, app):
        super().__init__(icon_name="audio-volume-high-symbolic", tooltip_text="Audio Boost & Effects",
                         valign=Gtk.Align.CENTER)
        self.app = app
        pop = Gtk.Popover()
        pop.connect("show", lambda *_: self._sync())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12, width_request=320)
        head = Gtk.Box(spacing=6)
        head.append(Gtk.Label(label="Volume Boost", xalign=0, hexpand=True, css_classes=["heading"]))
        self.boost_label = Gtk.Label(css_classes=["numeric", "dim-label"])
        head.append(self.boost_label)
        box.append(head)
        self.boost = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 100, 300, 5)
        self.boost.set_draw_value(False)
        for m in (100, 150, 200, 250, 300):
            self.boost.add_mark(m, Gtk.PositionType.BOTTOM, f"{m}%" if m in (100, 200, 300) else None)
        self.boost.connect("value-changed", self._changed)
        box.append(self.boost)

        group = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE, margin_top=6)
        self.clarity = Adw.SwitchRow(title="Voice Clarity", subtitle="Lift speech, cut low rumble")
        self.night = Adw.SwitchRow(title="Night Mode", subtitle="Even out quiet and loud passages")
        self.per_book = Adw.SwitchRow(title="Only for This Book", subtitle="Otherwise applies to all books")
        for row in (self.clarity, self.night, self.per_book):
            row.connect("notify::active", self._changed)
            group.append(row)
        box.append(group)
        pop.set_child(box)
        self.set_popover(pop)
        self._syncing = False
        app.player.connect("book-changed", lambda *_: self._sync())
        self._sync()

    def _sync(self):
        settings, per_book = self.app.effects_for(self.app.player.book)
        self._syncing = True
        self.boost.set_value(settings.boost * 100)
        self.clarity.set_active(settings.voice_clarity)
        self.night.set_active(settings.night_mode)
        self.per_book.set_active(per_book)
        self.per_book.set_sensitive(self.app.player.book is not None)
        self._syncing = False
        self._update_label(settings)

    def _update_label(self, settings):
        self.boost_label.set_label(f"{round(settings.boost * 100)}%")
        if settings.active:
            self.add_css_class("accent")
        else:
            self.remove_css_class("accent")

    def _changed(self, *_):
        if self._syncing:
            return
        settings = EffectSettings(boost=self.boost.get_value() / 100, voice_clarity=self.clarity.get_active(),
                                  night_mode=self.night.get_active())
        self.app.set_effects(settings, self.per_book.get_active())
        self._update_label(settings)


class CastButton(Gtk.MenuButton):
    """Pick where to play: this computer, a Chromecast, a Roku TV or a DLNA renderer."""

    def __init__(self, app):
        super().__init__(icon_name="lectern-cast-symbolic", tooltip_text="Play On…", valign=Gtk.Align.CENTER)
        self.app = app
        self._connecting = None
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10,
                      margin_start=10, margin_end=10, width_request=330)
        box.append(Gtk.Label(label="Play On", xalign=0, css_classes=["heading"], margin_start=6))
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.list.connect("row-activated", self._activated)
        box.append(self.list)
        self.note = Gtk.Label(wrap=True, xalign=0, css_classes=["caption", "dim-label"], margin_start=6,
                              margin_end=6, max_width_chars=40)
        box.append(self.note)
        pop.set_child(box)
        pop.connect("show", self._on_show)
        self.set_popover(pop)
        cast = app.cast
        cast.connect("devices-changed", lambda *_: self._fill())
        cast.connect("connecting", self._on_connecting)
        cast.connect("connected", lambda *_: self._done_connecting())
        cast.connect("failed", lambda *_: self._done_connecting())
        app.player.connect("casting-changed", lambda *_: self._sync())
        self._sync()

    def _on_show(self, *_):
        self.app.cast.discover()
        self._fill()

    def _on_connecting(self, _mgr, name):
        self._connecting = name
        self._fill()

    def _done_connecting(self):
        self._connecting = None
        self._fill()
        self.get_popover().popdown()

    def _sync(self):
        if self.app.player.casting:
            self.add_css_class("accent")
        else:
            self.remove_css_class("accent")
        self._fill()

    def _row(self, title, subtitle, icon, active, target, busy=False):
        row = Adw.ActionRow(title=GLib.markup_escape_text(title), activatable=not active and not busy)
        if subtitle:
            row.set_subtitle(GLib.markup_escape_text(subtitle))
        row.add_prefix(Gtk.Image(icon_name=icon))
        if busy:
            row.add_suffix(Adw.Spinner())
        elif active:
            row.add_suffix(Gtk.Image(icon_name="lectern-finished-symbolic", css_classes=["accent"]))
        row.target = target
        return row

    def _fill(self):
        self.list.remove_all()
        p = self.app.player
        current = p.cast_name
        self.list.append(self._row("This Computer", None, "audio-speakers-symbolic", current is None, "local"))
        devices = self.app.cast.devices()
        for d in devices:
            self.list.append(self._row(d.name, d.subtitle, d.icon, d.name == current, d,
                                       busy=d.name == self._connecting))
        if self.app.cast.scanning or not devices:
            text = "Looking for TVs and speakers…" if self.app.cast.scanning else "No TVs or speakers found"
            searching = Adw.ActionRow(title=text, activatable=False, sensitive=False)
            if self.app.cast.scanning:
                searching.add_suffix(Adw.Spinner())
            self.list.append(searching)
        notes = ["Chromecast, Google TV, Roku TVs and DLNA/UPnP devices (smart TVs, Sonos, Kodi) are supported."]
        if p.casting and not p.cast_supports_effects:
            notes = ["Audio boost and effects don't apply on Chromecast."]
        self.note.set_label(" ".join(notes))

    def _activated(self, _lb, row):
        target = getattr(row, "target", None)
        if target == "local":
            self.app.stop_casting()
            self.get_popover().popdown()
        elif target is not None:
            self.app.cast_to(target)
