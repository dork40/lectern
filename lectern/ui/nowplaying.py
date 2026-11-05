"""Full now-playing view (blurred, cover-tinted background) and the mini player bar."""
import colorsys

from gi.repository import Adw, Gdk, GLib, Graphene, Gtk

from ..util import format_duration, format_time
from .bookmarks import BookmarkList
from .cover import TEXTURES, CoverImage
from .widgets import AudioButton, CastButton, SkipButton, SpeedButton, TimerIndicator

BLUR_TEXTURE_SIZE = 96


def _tint(rgb, dark):
    """Derive a background tint from the cover colour that keeps text readable."""
    h, l, s = colorsys.rgb_to_hls(*rgb)
    if dark:
        l, s = min(l, 0.22), min(s, 0.55)
    else:
        l, s = max(l, 0.82), min(s, 0.45)
    return colorsys.hls_to_rgb(h, l, s)


class BlurredBackground(Gtk.Widget):
    def __init__(self):
        super().__init__(hexpand=True, vexpand=True, can_target=False)
        self.texture = None
        self.color = None
        self._style = Adw.StyleManager.get_default()
        self._style.connect("notify::dark", lambda *_: self.queue_draw())

    def set_cover(self, path):
        self.texture = None
        self.color = TEXTURES.color(path) if path else None
        if path:
            TEXTURES.load(path, BLUR_TEXTURE_SIZE, lambda tex, p=path: self._loaded(p, tex))
        self.queue_draw()

    def _loaded(self, path, texture):
        self.texture = texture
        self.color = TEXTURES.color(path)
        self.queue_draw()

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        dark = self._style.get_dark()
        rect = Graphene.Rect().init(0, 0, w, h)
        snapshot.push_clip(rect)
        if self.texture is not None:
            tw, th = self.texture.get_width(), self.texture.get_height()
            scale = max(w / tw, h / th) * 1.35
            dw, dh = tw * scale, th * scale
            snapshot.push_blur(min(w, h) * 0.12 + 30)
            snapshot.save()
            snapshot.translate(Graphene.Point().init((w - dw) / 2, (h - dh) / 2))
            self.texture.snapshot(snapshot, dw, dh)
            snapshot.restore()
            snapshot.pop()
        tint = Gdk.RGBA()
        r, g, b = _tint(self.color, dark) if self.color else ((0.12, 0.12, 0.14) if dark else (0.94, 0.94, 0.95))
        tint.red, tint.green, tint.blue = r, g, b
        tint.alpha = 0.72 if self.texture is not None else 1.0
        snapshot.append_color(tint, rect)
        snapshot.pop()


class NowPlayingView(Adw.BreakpointBin):
    def __init__(self, app, window):
        super().__init__(width_request=320, height_request=480)
        self.app = app
        self.window = window
        self._scrubbing = False
        self._seek_source = 0
        self._chapter = None
        p = app.player

        overlay = Gtk.Overlay()
        self.background = BlurredBackground()
        overlay.set_child(self.background)

        toolbar = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.FLAT, css_classes=["nowplaying"])
        header = Adw.HeaderBar(show_title=False)
        close = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Back to Library")
        close.connect("clicked", lambda *_: window.close_now_playing())
        header.pack_start(close)
        details = Gtk.Button(icon_name="view-list-bullet-symbolic", tooltip_text="Book Details & Chapters")
        details.connect("clicked", lambda *_: self._open_details())
        header.pack_end(details)
        if app.screen.available:
            header.pack_end(Gtk.Button(icon_name="lectern-screen-off-symbolic", action_name="app.screen-off",
                                       tooltip_text="Turn Off Screen and Keyboard Light (Ctrl+B)"))
        header.pack_end(TimerIndicator(app))
        header.pack_end(CastButton(app))
        toolbar.add_top_bar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, valign=Gtk.Align.CENTER,
                       margin_start=24, margin_end=24, margin_bottom=24)
        self.cover = CoverImage(300, css="large", load_size=460)
        self.cover.set_margin_bottom(28)
        body.append(self.cover)
        self.title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, css_classes=["np-title"], lines=2,
                               ellipsize=3)
        self.author = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, css_classes=["np-author"])
        self.chapter_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, css_classes=["np-chapter"],
                                       margin_top=10, lines=2, ellipsize=3)
        self.casting_label = Gtk.Label(css_classes=["accent", "caption-heading"], margin_top=6, visible=False)
        for w in (self.title, self.author, self.casting_label, self.chapter_label):
            body.append(w)

        prog = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=18)
        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, draw_value=False, css_classes=["np-scale"])
        self.scale.set_adjustment(Gtk.Adjustment(lower=0, upper=1, step_increment=5, page_increment=30))
        self.scale.connect("change-value", self._on_scrub)
        prog.append(self.scale)
        times = Gtk.Box(margin_start=10, margin_end=10)
        self.elapsed = Gtk.Label(xalign=0, hexpand=True, css_classes=["numeric", "caption", "dim-label"])
        self.remaining = Gtk.Label(xalign=1, css_classes=["numeric", "caption", "dim-label"])
        times.append(self.elapsed)
        times.append(self.remaining)
        prog.append(times)
        self.book_left = Gtk.Label(css_classes=["numeric", "caption", "np-book-left"], margin_top=6)
        prog.append(self.book_left)
        body.append(prog)

        controls = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=14)
        self.prev_ch = Gtk.Button(icon_name="media-skip-backward-symbolic", tooltip_text="Previous Chapter",
                                  css_classes=["circular", "flat", "np-small"], valign=Gtk.Align.CENTER)
        self.prev_ch.connect("clicked", lambda *_: p.previous_chapter())
        self.back = SkipButton(app, forward=False, css_classes=["np-skip"])
        self.play = Gtk.Button(icon_name="media-playback-start-symbolic", css_classes=["circular", "np-play"],
                               tooltip_text="Play/Pause (Space)", valign=Gtk.Align.CENTER)
        self.play.connect("clicked", lambda *_: app.toggle())
        self.fwd = SkipButton(app, forward=True, css_classes=["np-skip"])
        self.next_ch = Gtk.Button(icon_name="media-skip-forward-symbolic", tooltip_text="Next Chapter",
                                  css_classes=["circular", "flat", "np-small"], valign=Gtk.Align.CENTER)
        self.next_ch.connect("clicked", lambda *_: p.next_chapter())
        for b in (self.prev_ch, self.back, self.play, self.fwd, self.next_ch):
            controls.append(b)
        body.append(controls)

        extras = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, margin_top=14)
        extras.append(SpeedButton(app))
        self.audio_button = AudioButton(app)
        extras.append(self.audio_button)
        self.chapters_button = Gtk.MenuButton(icon_name="view-list-symbolic", tooltip_text="Chapters",
                                              valign=Gtk.Align.CENTER)
        self.chapters_pop = Gtk.Popover()
        self.chapters_pop.connect("show", lambda *_: self._fill_chapters())
        self.chapters_button.set_popover(self.chapters_pop)
        extras.append(self.chapters_button)
        extras.append(Gtk.Button(icon_name="lectern-bookmark-add-symbolic", action_name="app.add-bookmark",
                                 tooltip_text="Add Bookmark (Ctrl+D)", valign=Gtk.Align.CENTER))
        self.bookmarks_button = Gtk.MenuButton(icon_name="lectern-bookmarks-symbolic", tooltip_text="Bookmarks",
                                               valign=Gtk.Align.CENTER)
        self.bookmarks_pop = Gtk.Popover()
        self.bookmarks_pop.connect("show", lambda *_: self._fill_bookmarks())
        self.bookmarks_button.set_popover(self.bookmarks_pop)
        extras.append(self.bookmarks_button)
        body.append(extras)

        clamp = Adw.Clamp(maximum_size=520, child=body)
        self.clamp = clamp
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp,
                                      propagate_natural_height=True)
        toolbar.set_content(scroller)
        overlay.add_overlay(toolbar)
        self.set_child(overlay)

        # cover size follows the window: small windows shrink it, maximised/fullscreen ones grow it
        for size, cond in ((220, "max-height: 640px"), (170, "max-height: 540px"), (240, "max-width: 400px"),
                           (380, "min-height: 820px and min-width: 800px"),
                           (460, "min-height: 980px and min-width: 1000px")):
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(cond))
            bp.connect("apply", lambda _b, s=size: self._set_cover_size(s))
            bp.connect("unapply", lambda _b: self._set_cover_size(300))
            self.add_breakpoint(bp)

        p.connect("book-changed", lambda *_: self._sync_book())
        p.connect("state-changed", lambda *_: self._sync_state())
        p.connect("position-changed", lambda _p, pos: self._sync_position(pos))
        p.connect("chapter-changed", lambda *_: self._sync_chapter())
        p.connect("rate-changed", lambda *_: self._sync_position(p.position))
        p.connect("casting-changed", lambda *_: self._sync_casting())
        self._sync_book()
        self._sync_casting()

    def _set_cover_size(self, size):
        self.cover.set_size(size)
        self.clamp.set_maximum_size(max(520, size + 160))

    def update_skip_labels(self):
        for b in (self.back, self.fwd, self.window.mini.back, self.window.mini.fwd):
            b.refresh()

    def _open_details(self):
        if self.app.player.book:
            self.window.close_now_playing()
            self.window.show_book(self.app.player.book)

    def _sync_book(self):
        book = self.app.player.book
        if not book:
            return
        self.cover.set_book(book)
        self.background.set_cover(book.cover_path)
        self.title.set_label(book.title)
        self.author.set_label(book.author or "")
        multi = len(book.chapters) > 1
        self.prev_ch.set_visible(multi)
        self.next_ch.set_visible(multi)
        self.chapters_button.set_visible(multi)
        self._chapter = None
        self._sync_chapter()
        self._sync_state()

    def _sync_casting(self):
        p = self.app.player
        self.casting_label.set_visible(p.casting)
        if p.casting:
            self.casting_label.set_label(f"Playing on {p.cast_name}")
        effects_ok = p.cast_supports_effects
        self.audio_button.set_sensitive(effects_ok)
        self.audio_button.set_tooltip_text("Audio Boost & Effects" if effects_ok
                                           else "Audio effects aren't available on Chromecast")

    def _sync_state(self):
        playing = self.app.player.playing
        self.play.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")

    def _sync_chapter(self):
        p = self.app.player
        if not p.book:
            return
        ch = p.chapter
        self._chapter = ch
        multi = len(p.book.chapters) > 1
        self.chapter_label.set_visible(multi and ch is not None)
        if ch is not None:
            idx = p.chapter_index
            self.chapter_label.set_label(f"{ch.title}  ·  {idx + 1} of {len(p.book.chapters)}" if multi else ch.title)
        self._sync_position(p.position)

    def _bounds(self):
        p = self.app.player
        ch = self._chapter
        if ch is not None and len(p.book.chapters) > 1:
            return ch.start, ch.end
        return 0.0, p.duration

    def _sync_position(self, pos):
        p = self.app.player
        if not p.book or self._scrubbing:
            return
        start, end = self._bounds()
        adj = self.scale.get_adjustment()
        adj.configure(min(max(pos, start), end), start, end, 5, 30, 0)
        self._set_time_labels(pos, start, end)

    def _set_time_labels(self, pos, start, end):
        p = self.app.player
        self.elapsed.set_label(format_time(pos - start, force_hours=end - start >= 3600))
        self.remaining.set_label("-" + format_time(end - pos, force_hours=end - start >= 3600))
        left = max(0.0, p.duration - pos)
        pct = int(100 * pos / p.duration) if p.duration else 0
        text = f"{format_duration(left)} left in book · {pct}%"
        if abs(p.rate - 1.0) > 0.01:
            text += f" · {format_duration(left / p.rate)} at {p.rate:g}×"
        self.book_left.set_label(text)

    def _on_scrub(self, scale, _scroll, value):
        start, end = self._bounds()
        value = min(max(value, start), end)
        self._scrubbing = True
        self._set_time_labels(value, start, end)
        if self._seek_source:
            GLib.source_remove(self._seek_source)
        self._seek_source = GLib.timeout_add(120, self._commit_scrub, value)
        return False

    def _commit_scrub(self, value):
        self._seek_source = 0
        self._scrubbing = False
        self.app.jump_to(value, "the new position")
        return False

    def _fill_bookmarks(self):
        book = self.app.player.book
        if not book:
            return
        lb = BookmarkList(self.app, book, on_jump=self.bookmarks_pop.popdown, compact=True)
        self.bookmarks_pop.set_child(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                                        propagate_natural_height=True, max_content_height=420,
                                                        min_content_width=340, child=lb))

    def _fill_chapters(self):
        p = self.app.player
        if not p.book:
            return
        lb = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.SINGLE)
        current = p.chapter_index
        for i, ch in enumerate(p.book.chapters):
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12, margin_start=6, margin_end=6, margin_top=4, margin_bottom=4)
            box.append(Gtk.Label(label=ch.title, xalign=0, hexpand=True, ellipsize=3, max_width_chars=36))
            box.append(Gtk.Label(label=format_time(ch.duration), css_classes=["dim-label", "numeric", "caption"]))
            row.set_child(box)
            row.chapter = ch
            lb.append(row)
            if i == current:
                lb.select_row(row)

        def activated(_lb, row):
            self.app.jump_to(row.chapter.start, row.chapter.title)
            self.chapters_pop.popdown()
        lb.connect("row-activated", activated)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                      max_content_height=420, min_content_width=320, child=lb)
        self.chapters_pop.set_child(scroller)
        sel = lb.get_selected_row()
        if sel:
            GLib.idle_add(lambda: (sel.grab_focus(), False)[1])


class MiniPlayer(Gtk.Box):
    """Bottom bar: thumbnail, title/chapter, skip and play controls, thin progress."""

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["miniplayer"])
        self.app = app
        p = app.player
        self.progress = Gtk.ProgressBar(css_classes=["thin", "mini-progress"])
        self.append(self.progress)
        row = Gtk.Box(spacing=12, margin_start=12, margin_end=8, margin_top=8, margin_bottom=8)
        self.cover = CoverImage(44, css="small")
        self.cover.set_valign(Gtk.Align.CENTER)
        row.append(self.cover)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.title = Gtk.Label(xalign=0, ellipsize=3, css_classes=["heading"])
        self.subtitle = Gtk.Label(xalign=0, ellipsize=3, css_classes=["caption", "dim-label"])
        text.append(self.title)
        text.append(self.subtitle)
        row.append(text)
        self.back = SkipButton(app, forward=False)
        self.play = Gtk.Button(icon_name="media-playback-start-symbolic", css_classes=["circular", "mini-play"],
                               valign=Gtk.Align.CENTER, tooltip_text="Play/Pause")
        self.play.connect("clicked", lambda *_: app.toggle())
        self.fwd = SkipButton(app, forward=True)
        for b in (self.back, self.play, self.fwd):
            row.append(b)
        self.append(row)
        p.connect("book-changed", lambda *_: self._sync())
        p.connect("chapter-changed", lambda *_: self._sync())
        p.connect("state-changed", lambda *_: self._sync_state())
        p.connect("position-changed", lambda _p, pos: self._sync_pos(pos))
        p.connect("casting-changed", lambda *_: self._sync())
        self._sync()

    def _sync(self):
        p = self.app.player
        if not p.book:
            return
        self.cover.set_book(p.book)
        self.title.set_label(p.book.title)
        ch = p.chapter
        sub = ch.title if ch is not None and len(p.book.chapters) > 1 else (p.book.author or "")
        if p.casting:
            sub = f"On {p.cast_name} · {sub}"
        self.subtitle.set_label(sub)
        self._sync_state()
        self._sync_pos(p.position)

    def _sync_state(self):
        self.play.set_icon_name("media-playback-pause-symbolic" if self.app.player.playing
                                else "media-playback-start-symbolic")

    def _sync_pos(self, pos):
        d = self.app.player.duration
        self.progress.set_fraction(pos / d if d else 0)
