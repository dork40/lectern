"""Book detail page: cover, metadata, progress and a clickable chapter list."""
from gi.repository import Adw, Gio, GLib, Gtk

from ..util import format_duration, format_time
from .bookmarks import BookmarkList
from .cover import CoverImage
from .library import time_left_text
from .widgets import TimerIndicator


class BookDetailPage(Adw.NavigationPage):
    def __init__(self, app, window, book):
        super().__init__(title=book.title, tag=f"book-{book.id}")
        self.app = app
        self.window = window
        self.book = book
        self._chapter_rows = []

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="", css_classes=["title"]))
        menu = Gio.Menu()
        sect = Gio.Menu()
        sect.append("Edit Details…", f"win.edit-book({book.id})")
        sect.append("Find Cover Online…", f"win.find-cover({book.id})")
        sect.append("Set Cover From File…", f"win.cover-from-file({book.id})")
        sect.append("Embed Cover Into Files…", f"win.embed-cover({book.id})")
        menu.append_section(None, sect)
        sect = Gio.Menu()
        sect.append("Mark as Unfinished" if book.finished else "Mark as Finished", f"win.toggle-finished({book.id})")
        sect.append("Reset Progress", f"win.reset-progress({book.id})")
        sect.append("Open Containing Folder", f"win.open-folder({book.id})")
        menu.append_section(None, sect)
        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="Book Menu"))
        header.pack_end(TimerIndicator(app))
        toolbar.add_top_bar(header)

        bp_bin = Adw.BreakpointBin(width_request=320, height_request=300)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=30, margin_top=18, margin_bottom=36,
                        margin_start=18, margin_end=18)
        clamp = Adw.Clamp(maximum_size=860, child=outer)
        scroller.set_child(clamp)
        bp_bin.set_child(scroller)
        toolbar.set_content(bp_bin)
        self.set_child(toolbar)

        top = Gtk.Box(spacing=30)
        self.cover = CoverImage(240, css="large", load_size=320)
        top.append(self.cover)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        self.series_label = Gtk.Label(xalign=0, css_classes=["caption-heading", "accent"], wrap=True)
        # selectable for copying, but not focusable, or it opens with the whole title highlighted
        self.title_label = Gtk.Label(xalign=0, wrap=True, css_classes=["title-1"], selectable=True, focusable=False)
        self.author_label = Gtk.Label(xalign=0, wrap=True, css_classes=["title-4"])
        self.narrator_label = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        self.meta_label = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label", "caption", "numeric"])
        for w in (self.series_label, self.title_label, self.author_label, self.narrator_label, self.meta_label):
            info.append(w)
        self.progress = Gtk.ProgressBar(css_classes=["thin"], margin_top=12)
        info.append(self.progress)
        self.left_label = Gtk.Label(xalign=0, css_classes=["caption", "dim-label", "numeric"])
        info.append(self.left_label)

        buttons = Gtk.Box(spacing=12, margin_top=12)
        self.play_button = Gtk.Button(css_classes=["pill", "suggested-action"])
        self.play_button.connect("clicked", self._on_play)
        buttons.append(self.play_button)
        info.append(buttons)
        top.append(info)
        outer.append(top)

        self.chapters = Adw.PreferencesGroup(title="Chapters")
        self.chapter_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.chapter_list.connect("row-activated", self._on_chapter)
        self.chapters.add(self.chapter_list)
        outer.append(self.chapters)

        self.bookmarks_group = Adw.PreferencesGroup(title="Bookmarks")
        self.bookmarks = BookmarkList(app, book)
        self.bookmarks_group.add(self.bookmarks)
        outer.append(self.bookmarks_group)

        # Narrow windows: stack the cover above the details.
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 620sp"))
        bp.add_setter(top, "orientation", Gtk.Orientation.VERTICAL)
        bp.add_setter(self.cover, "halign", Gtk.Align.CENTER)
        bp.add_setter(buttons, "halign", Gtk.Align.CENTER)
        for lbl in (self.series_label, self.title_label, self.author_label, self.narrator_label,
                    self.meta_label, self.left_label):
            bp.add_setter(lbl, "xalign", 0.5)
            bp.add_setter(lbl, "justify", Gtk.Justification.CENTER)
        bp_bin.add_breakpoint(bp)

        self._handlers = [
            (app.player, app.player.connect("state-changed", lambda *_: self._sync_playing())),
            (app.player, app.player.connect("chapter-changed", lambda *_: self._sync_playing())),
            (app.player, app.player.connect("position-changed", self._on_position)),
            (app, app.connect("book-updated", self._on_book_updated)),
            (app, app.connect("bookmarks-changed", self._on_bookmarks_changed)),
        ]
        self.connect("hidden", self._on_hidden)
        self._fill()

    def _on_hidden(self, *_):
        # Pages are rebuilt each time they are opened; drop signal handlers once gone.
        if self.window.nav.find_page(self.get_tag()) is None:
            for obj, hid in self._handlers:
                obj.disconnect(hid)
            self._handlers = []

    def _on_bookmarks_changed(self, _app, book_id):
        if book_id == self.book.id:
            self.bookmarks.refill()

    def _on_book_updated(self, _app, book_id):
        if book_id == self.book.id:
            fresh = self.app.db.get_book(book_id)
            if fresh:
                self.book = fresh
                self._fill()

    def _fill(self):
        b = self.book
        self.set_title(b.title)
        self.cover.set_book(b)
        self.title_label.set_label(b.title)
        self.author_label.set_label(b.author or "Unknown author")
        self.narrator_label.set_label(f"Narrated by {b.narrator}" if b.narrator else "")
        self.narrator_label.set_visible(bool(b.narrator))
        series = b.series + (f" · Book {b.series_index}" if b.series_index else "") if b.series else ""
        self.series_label.set_label(series)
        self.series_label.set_visible(bool(series))
        meta = [format_duration(b.duration)]
        if len(b.chapters) > 1:
            meta.append(f"{len(b.chapters)} chapters")
        if b.year:
            meta.append(b.year)
        if b.kind == "folder":
            meta.append(f"{len(b.tracks)} files")
        self.meta_label.set_label(" · ".join(meta))

        self._chapter_rows = []
        self.chapter_list.remove_all()
        for i, ch in enumerate(b.chapters):
            row = Adw.ActionRow(title=GLib.markup_escape_text(ch.title), activatable=True)
            row.chapter = ch
            row.num = Gtk.Label(label=str(i + 1), css_classes=["dim-label", "numeric"], width_chars=3)
            row.add_prefix(row.num)
            row.indicator = Gtk.Image(icon_name="media-playback-start-symbolic", css_classes=["accent"],
                                      visible=False, width_request=30)
            row.add_prefix(row.indicator)
            row.add_suffix(Gtk.Label(label=format_time(ch.duration), css_classes=["dim-label", "numeric"]))
            self.chapter_list.append(row)
            self._chapter_rows.append(row)
        self.chapters.set_visible(len(b.chapters) > 1)
        self._sync_playing()

    def _is_current(self):
        p = self.app.player
        return p.book is not None and p.book.id == self.book.id

    def _sync_playing(self):
        p = self.app.player
        current = self._is_current()
        pos = p.position if current else self.book.position
        if current:
            self.book.position = pos
        if current and p.playing:
            label = "Pause"
        elif self.book.finished:
            label = "Listen Again"
        elif self.book.position > 1:
            label = "Resume"
        else:
            label = "Play"
        self.play_button.set_label(label)
        self.progress.set_fraction(self.book.progress)
        self.progress.set_visible(self.book.position > 1 or self.book.finished)
        self.left_label.set_label(time_left_text(self.book))
        idx = self.book.chapter_index(pos) if (current or self.book.position > 1) else -1
        for i, row in enumerate(self._chapter_rows):
            active = i == idx
            row.indicator.set_visible(active)
            row.num.set_visible(not active)
            if active:
                row.add_css_class("current-chapter")
            else:
                row.remove_css_class("current-chapter")

    def _on_position(self, _player, pos):
        if self._is_current():
            self.book.position = pos
            self.progress.set_fraction(self.book.progress)
            self.left_label.set_label(time_left_text(self.book))

    def _on_play(self, _btn):
        if self._is_current():
            self.app.player.toggle()
        else:
            self.app.play_book(self.book)

    def _on_chapter(self, _lb, row):
        if self._is_current():
            self.app.jump_to(row.chapter.start, row.chapter.title)
            self.app.player.play()
        else:
            self.app.play_book(self.book, position=row.chapter.start)
