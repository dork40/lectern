"""Library page: bookshelf grid / compact list, continue listening, search, filters, grouping."""
import re
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from ..util import format_duration, natural_key
from .cover import CoverImage
from .widgets import TimerIndicator

FILTERS = (("all", "All"), ("in-progress", "In Progress"), ("not-started", "Not Started"), ("finished", "Finished"))
SORTS = (("recent", "Recently Played"), ("title", "Title"), ("author", "Author"), ("progress", "Progress"),
         ("added", "Recently Added"))
GROUPS = (("none", "No Grouping"), ("author", "Author"), ("series", "Series"), ("narrator", "Narrator"))


def _title_key(t):
    return natural_key(re.sub(r"^(the|a|an)\s+", "", t or "", flags=re.I))


def _series_num(b):
    try:
        return float(b.series_index)
    except (TypeError, ValueError):
        return 1e9


SORT_KEYS = {
    "title": (lambda b: _title_key(b.title), False),
    "author": (lambda b: (natural_key(b.author or "~"), natural_key(b.series or ""), _series_num(b), _title_key(b.title)), False),
    "recent": (lambda b: (b.last_played, b.added), True),
    "progress": (lambda b: (b.progress, b.last_played), True),
    "added": (lambda b: b.added, True),
}


def time_left_text(book):
    if book.finished:
        return "Finished"
    if book.position <= 1:
        return format_duration(book.duration)
    return f"{format_duration(book.duration - book.position)} left"


class BookTile(Gtk.FlowBoxChild):
    """Grid cell: cover, thin progress bar, title, author."""

    def __init__(self, book, size=164):
        super().__init__(css_classes=["book-tile-child"])
        self.book = book
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["book-tile"],
                      halign=Gtk.Align.CENTER)
        overlay = Gtk.Overlay()
        self.cover = CoverImage(size)
        self.cover.set_book(book)
        overlay.set_child(self.cover)
        self.badge = Gtk.Image(icon_name="lectern-finished-symbolic", css_classes=["finished-badge"],
                               halign=Gtk.Align.END, valign=Gtk.Align.START, margin_top=8, margin_end=8)
        overlay.add_overlay(self.badge)
        box.append(overlay)
        self.progress = Gtk.ProgressBar(css_classes=["thin"], width_request=size)
        box.append(self.progress)
        title = Gtk.Label(label=book.title, xalign=0, ellipsize=3, lines=2, wrap=True, max_width_chars=1,
                          css_classes=["tile-title"], width_request=size)
        title.set_wrap_mode(2)
        box.append(title)
        box.append(Gtk.Label(label=book.author or "Unknown author", xalign=0, ellipsize=3, max_width_chars=1,
                             css_classes=["tile-author", "dim-label", "caption"], width_request=size))
        self.set_child(box)
        self.set_tooltip_text(f"{book.title}\n{book.author}" if book.author else book.title)
        self.update(book)

    def update(self, book):
        self.book = book
        self.progress.set_fraction(book.progress)
        self.progress.set_visible(0 < book.progress < 1)
        self.badge.set_visible(book.finished)


class BookRow(Adw.ActionRow):
    """Compact list row."""

    def __init__(self, book):
        super().__init__(activatable=True, title_lines=1, subtitle_lines=1)
        self.book = book
        self.set_title(GLib.markup_escape_text(book.title))
        parts = [book.author or "Unknown author"]
        if book.series:
            parts.append(f"{book.series} {book.series_index}".strip())
        if book.narrator:
            parts.append(f"read by {book.narrator}")
        self.set_subtitle(GLib.markup_escape_text(" · ".join(parts)))
        cover = CoverImage(48, css="small")
        cover.set_valign(Gtk.Align.CENTER)
        cover.set_book(book)
        cover.set_margin_top(6)
        cover.set_margin_bottom(6)
        self.add_prefix(cover)
        suffix = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER, width_request=110)
        self.left = Gtk.Label(xalign=1, css_classes=["caption", "dim-label", "numeric"])
        self.progress = Gtk.ProgressBar(css_classes=["thin"])
        suffix.append(self.left)
        suffix.append(self.progress)
        self.add_suffix(suffix)
        self.update(book)

    def update(self, book):
        self.book = book
        self.left.set_label(time_left_text(book))
        self.progress.set_fraction(book.progress)
        self.progress.set_visible(0 < book.progress < 1)


class HeroCard(Gtk.Box):
    """The most recently played book, large."""

    def __init__(self, app, book):
        super().__init__(spacing=18, css_classes=["hero-card"])
        self.app = app
        self.book = book
        cover = CoverImage(132, css="hero")
        cover.set_book(book)
        cover.set_valign(Gtk.Align.CENTER)
        self.append(cover)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER, hexpand=True)
        info.append(Gtk.Label(label="Continue Listening", xalign=0, css_classes=["caption-heading", "accent"]))
        info.append(Gtk.Label(label=book.title, xalign=0, wrap=True, css_classes=["title-2"], lines=2, ellipsize=3))
        if book.author:
            info.append(Gtk.Label(label=book.author, xalign=0, css_classes=["dim-label"], ellipsize=3))
        idx = book.chapter_index(book.position)
        if 0 <= idx < len(book.chapters) and len(book.chapters) > 1:
            info.append(Gtk.Label(label=book.chapters[idx].title, xalign=0, ellipsize=3,
                                  css_classes=["caption", "dim-label"], margin_top=4))
        self.progress = Gtk.ProgressBar(css_classes=["thin"], margin_top=8)
        info.append(self.progress)
        self.left = Gtk.Label(xalign=0, css_classes=["caption", "dim-label", "numeric"])
        info.append(self.left)
        self.append(info)
        self.play = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                               css_classes=["circular", "suggested-action", "hero-play"], tooltip_text="Resume")
        self.play.connect("clicked", lambda *_: app.play_book(self.book))
        self.append(self.play)
        self.update(book)

    def update(self, book):
        self.book = book
        self.progress.set_fraction(book.progress)
        self.left.set_label(time_left_text(book))
        playing = self.app.player.book and self.app.player.book.id == book.id and self.app.player.playing
        self.play.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")


class SmallCard(Gtk.Button):
    def __init__(self, book, on_open):
        super().__init__(css_classes=["flat", "small-card"])
        self.book = book
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        cover = CoverImage(104)
        cover.set_book(book)
        box.append(cover)
        self.progress = Gtk.ProgressBar(css_classes=["thin"], width_request=104)
        box.append(self.progress)
        box.append(Gtk.Label(label=book.title, xalign=0, ellipsize=3, max_width_chars=1, width_request=104,
                             css_classes=["caption-heading"]))
        self.set_child(box)
        self.set_tooltip_text(book.title)
        self.connect("clicked", lambda *_: on_open(self.book))
        self.update(book)

    def update(self, book):
        self.book = book
        self.progress.set_fraction(book.progress)


class LibraryPage(Adw.NavigationPage):
    def __init__(self, app, window):
        super().__init__(title="Library", tag="library")
        self.app = app
        self.window = window
        self.books = []
        self._widgets = {}       # book id -> [widgets with update()]
        self._cache = {}         # (view, id, display fields) -> reusable tile/row widget
        self._search_source = 0
        s = app.setting
        self.view = s("view")
        self.sort = s("sort")
        self.group = s("group")
        self.filter = s("filter")

        toolbar = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.FLAT)
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="Library")
        header.set_title_widget(self.title_widget)

        self.search_button = Gtk.ToggleButton(icon_name="system-search-symbolic", tooltip_text="Search (Ctrl+F)")
        header.pack_start(self.search_button)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Add Folder…", "app.add-folder")
        section.append("Rescan Library", "app.rescan")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Listening Stats", "app.stats")
        section.append("Turn Off Screen", "app.screen-off")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Preferences", "app.preferences")
        section.append("Keyboard Shortcuts", "app.shortcuts")
        section.append("About Lectern", "app.about")
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Main Menu"))

        sort_menu = Gio.Menu()
        sect = Gio.Menu()
        for key, label in SORTS:
            sect.append(label, f"library.sort::{key}")
        sort_menu.append_section("Sort By", sect)
        sect = Gio.Menu()
        for key, label in GROUPS:
            sect.append(label, f"library.group::{key}")
        sort_menu.append_section("Group By", sect)
        header.pack_end(Gtk.MenuButton(icon_name="lectern-sort-symbolic", menu_model=sort_menu,
                                       tooltip_text="Sort and Group"))

        self.view_toggle = Adw.ToggleGroup(valign=Gtk.Align.CENTER, css_classes=["flat"])
        self.view_toggle.add(Adw.Toggle(name="grid", icon_name="view-grid-symbolic", tooltip="Grid"))
        self.view_toggle.add(Adw.Toggle(name="list", icon_name="view-list-symbolic", tooltip="List"))
        self.view_toggle.set_active_name(self.view)
        self.view_toggle.connect("notify::active-name", self._on_view_toggled)
        header.pack_end(self.view_toggle)
        header.pack_end(TimerIndicator(app))
        toolbar.add_top_bar(header)

        # search bar
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search title, author, narrator, series",
                                            hexpand=True)
        self.search_entry.connect("search-changed", self._on_search_changed)
        clamp = Adw.Clamp(maximum_size=520, child=self.search_entry)
        self.search_bar = Gtk.SearchBar(child=clamp, show_close_button=False)
        self.search_bar.connect_entry(self.search_entry)
        self.search_button.bind_property("active", self.search_bar, "search-mode-enabled",
                                         GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        toolbar.add_top_bar(self.search_bar)

        # filter strip + scan progress
        strip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.filters = Adw.ToggleGroup(halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6,
                                       css_classes=["round", "filters"])
        for key, label in FILTERS:
            self.filters.add(Adw.Toggle(name=key, label=label))
        self.filters.set_active_name(self.filter)
        self.filters.connect("notify::active-name", self._on_filter)
        strip.append(self.filters)
        self.scan_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        scan_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=24, margin_end=24,
                           margin_bottom=6)
        self.scan_label = Gtk.Label(label="Scanning library…", css_classes=["caption", "dim-label"])
        self.scan_progress = Gtk.ProgressBar(css_classes=["thin"])
        scan_box.append(self.scan_label)
        scan_box.append(self.scan_progress)
        self.scan_revealer.set_child(scan_box)
        strip.append(self.scan_revealer)
        self.strip = strip
        toolbar.add_top_bar(strip)

        # content
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_empty(), "empty")
        nobooks = Adw.StatusPage(icon_name="folder-music-symbolic", title="No Audiobooks Yet",
                                 description="Put .m4b, .mp3, .opus, .flac or .ogg books (or folders of them) "
                                             "into your library folder. Lectern adds them automatically.")
        box = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        box.append(Gtk.Button(label="Rescan", action_name="app.rescan", css_classes=["pill"]))
        box.append(Gtk.Button(label="Add Another Folder…", action_name="app.add-folder", css_classes=["pill"]))
        nobooks.set_child(box)
        self.nobooks = nobooks
        self.stack.add_named(nobooks, "nobooks")
        self.stack.add_named(Adw.StatusPage(icon_name="system-search-symbolic", title="No Results",
                                            description="Try a different search or filter"), "nomatch")
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=12,
                               margin_bottom=24, margin_start=24, margin_end=24)
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
                                           child=self.content)
        self.stack.add_named(self.scroller, "library")
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        loading.append(Adw.Spinner(width_request=48, height_request=48))
        loading.append(Gtk.Label(label="Looking for audiobooks…", css_classes=["dim-label"]))
        self.stack.add_named(loading, "loading")
        toolbar.set_content(self.stack)
        # Wide windows (maximised / fullscreen): bigger covers, Continue Listening laid out side by side
        self.wide = False
        bp_bin = Adw.BreakpointBin(width_request=360, height_request=400, child=toolbar)
        wide = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("min-width: 1100sp"))
        wide.connect("apply", lambda *_: self._set_wide(True))
        wide.connect("unapply", lambda *_: self._set_wide(False))
        bp_bin.add_breakpoint(wide)
        self.set_child(bp_bin)

        group = Gio.SimpleActionGroup()
        for name, value in (("sort", self.sort), ("group", self.group)):
            action = Gio.SimpleAction.new_stateful(name, GLib.VariantType.new("s"), GLib.Variant("s", value))
            action.connect("change-state", self._on_menu_state)
            group.add_action(action)
        search = Gio.SimpleAction.new("search", None)
        search.connect("activate", lambda *_: self.focus_search())
        group.add_action(search)
        self.insert_action_group("library", group)

        app.connect("library-changed", lambda *_: self.reload())
        app.connect("book-updated", lambda *_: self.reload())
        app.scanner.connect("started", self._on_scan_started)
        app.scanner.connect("progress", self._on_scan_progress)
        app.scanner.connect("finished", lambda *_: self.scan_revealer.set_reveal_child(False))
        app.player.connect("state-changed", lambda *_: self._refresh_playing())
        app.player.connect("position-changed", self._on_position)
        self.reload()

    # ----------------------------------------------------------- building
    def _build_empty(self):
        page = Adw.StatusPage(icon_name="folder-music-symbolic", title="Add a Folder to Get Started",
                              description="Lectern finds the audiobooks in your folders and keeps the library "
                                          "up to date as you add more.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        default = Path.home() / "Audiobooks"
        if default.is_dir():
            use = Gtk.Button(label="Use ~/Audiobooks", css_classes=["pill", "suggested-action"])
            use.connect("clicked", lambda *_: self.app.add_folder(default))
            box.append(use)
            choose = Gtk.Button(label="Choose Another Folder…", css_classes=["pill"])
        else:
            choose = Gtk.Button(label="Choose Folder…", css_classes=["pill", "suggested-action"])
        choose.set_action_name("app.add-folder")
        box.append(choose)
        page.set_child(box)
        return page

    def focus_search(self):
        self.search_button.set_active(True)
        self.search_entry.grab_focus()

    def reload(self):
        self.books = self.app.db.all_books()
        live = {self._cache_key(v, b) for b in self.books for v in ("grid", "list")}
        live |= {("grid", size) + k[2:] for k in live for size in (164, 196)}
        self._cache = {k: w for k, w in self._cache.items() if k in live}
        self.rebuild()

    def _set_wide(self, wide):
        if wide != self.wide:
            self.wide = wide
            self.rebuild()

    @property
    def tile_size(self):
        return 196 if self.wide else 164

    def _cache_key(self, view, b):
        size = self.tile_size if view == "grid" else 0
        return (view, size, b.id, b.title, b.author, b.narrator, b.series, b.series_index, b.cover_path)

    def _cached(self, b, make):
        """Reuse the widget for a book across rebuilds (searching re-attaches, never re-creates)."""
        key = self._cache_key(self.view, b)
        w = self._cache.get(key)
        if w is None:
            w = self._cache[key] = make(b)
        else:
            parent = w.get_parent()
            if parent is not None:
                parent.remove(w)
            w.update(b)
        return w

    def _visible_books(self):
        query = self.search_entry.get_text().strip().casefold()
        terms = query.split()
        books = [b for b in self.books
                 if (self.filter == "all" or b.status == self.filter)
                 and all(t in b.search_text for t in terms)]
        key, reverse = SORT_KEYS.get(self.sort, SORT_KEYS["recent"])
        return sorted(books, key=key, reverse=reverse)

    def rebuild(self):
        self._widgets.clear()
        child = self.content.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.content.remove(child)
            child = nxt
        n = len(self.books)
        self.title_widget.set_subtitle(f"{n} book{'s' if n != 1 else ''}" if n else "")
        self.strip.set_visible(n > 0)
        if not self.books:
            folders = self.app.db.folders()
            if folders and self.app.scanner.running:
                self.stack.set_visible_child_name("loading")
            elif folders:
                home = str(Path.home())
                where = ", ".join(f.replace(home, "~", 1) for f in folders)
                self.nobooks.set_description(
                    f"Put .m4b, .mp3, .opus, .flac or .ogg books (or folders of them) into {where}. "
                    "Lectern adds them automatically.")
                self.stack.set_visible_child_name("nobooks")
            else:
                self.stack.set_visible_child_name("empty")
            return
        visible = self._visible_books()
        searching = bool(self.search_entry.get_text().strip())
        if self.filter == "all" and not searching:
            self._build_continue()
        if not visible:
            self.stack.set_visible_child_name("nomatch")
            return
        self.stack.set_visible_child_name("library")
        for title, books in self._grouped(visible):
            section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            if title is not None:
                header = Gtk.Box(spacing=8)
                header.append(Gtk.Label(label=title, xalign=0, css_classes=["title-4"]))
                header.append(Gtk.Label(label=str(len(books)), css_classes=["dim-label", "caption", "count-badge"],
                                        valign=Gtk.Align.CENTER))
                section.append(header)
            elif self.content.get_first_child() is not None:
                section.append(Gtk.Label(label="All Books", xalign=0, css_classes=["title-4"]))
            section.append(self._build_books(books))
            # the list view is clamped; keep its headers aligned with it
            self.content.append(Adw.Clamp(maximum_size=1000, child=section) if self.view == "list" else section)

    def _grouped(self, books):
        if self.group == "none":
            return [(None, books)]
        groups = {}
        for b in books:
            key = getattr(b, self.group) or ""
            groups.setdefault(key, []).append(b)
        fallback = {"author": "Unknown Author", "series": "Standalone", "narrator": "Unknown Narrator"}[self.group]
        ordered = sorted(groups.items(), key=lambda kv: (kv[0] == "", natural_key(kv[0])))
        out = []
        for key, items in ordered:
            if self.group == "series" and key:
                items = sorted(items, key=lambda b: (_series_num(b), _title_key(b.title)))
            out.append((key or fallback, items))
        return out

    def _build_books(self, books):
        if self.view == "list":
            lb = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
            for b in books:
                row = self._cached(b, BookRow)
                lb.append(row)
                self._widgets.setdefault(b.id, []).append(row)
            lb.connect("row-activated", lambda _lb, row: self.window.show_book(row.book))
            return lb
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, valign=Gtk.Align.START,
                           column_spacing=12, row_spacing=18, min_children_per_line=2,
                           max_children_per_line=12, activate_on_single_click=True)
        for b in books:
            tile = self._cached(b, self._make_tile)
            flow.append(tile)
            self._widgets.setdefault(b.id, []).append(tile)
        flow.connect("child-activated", lambda _f, tile: self.window.show_book(tile.book))
        return flow

    def _build_continue(self):
        recent = sorted((b for b in self.books if b.status == "in-progress"),
                        key=lambda b: b.last_played, reverse=True)[:12]
        if not recent:
            return
        hero = HeroCard(self.app, recent[0])
        self._widgets.setdefault(recent[0].id, []).append(hero)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.window.show_book(hero.book))
        hero.add_controller(click)
        section = Gtk.Box(spacing=24)
        if self.wide:
            hero.set_size_request(620, -1)       # don't stretch across a fullscreen window
            hero.set_hexpand(False)
        else:
            section.set_orientation(Gtk.Orientation.VERTICAL)
        section.append(hero)
        self.content.append(section)
        if len(recent) > 1:
            row = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER)
            for b in recent[1:]:
                card = SmallCard(b, self.window.show_book)
                row.append(card)
                self._widgets.setdefault(b.id, []).append(card)
            scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, child=row, hexpand=True,
                                          propagate_natural_height=True, css_classes=["continue-row"])
            section.append(scroller)

    def _make_tile(self, book):
        tile = BookTile(book, self.tile_size)
        self._attach_context_menu(tile)
        return tile

    def _attach_context_menu(self, tile):
        click = Gtk.GestureClick(button=3)

        def pressed(gesture, _n, x, y):
            book = tile.book
            menu = Gio.Menu()
            menu.append("Play", f"win.play-book({book.id})")
            menu.append("Mark as Unfinished" if book.finished else "Mark as Finished",
                        f"win.toggle-finished({book.id})")
            menu.append("Find Cover…", f"win.find-cover({book.id})")
            menu.append("Edit Details…", f"win.edit-book({book.id})")
            pop = Gtk.PopoverMenu(menu_model=menu, has_arrow=False)
            pop.set_parent(tile)
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            pop.set_pointing_to(rect)
            pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
            pop.popup()
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        click.connect("pressed", pressed)
        tile.add_controller(click)

    # ------------------------------------------------------------ updates
    def _update_book(self, book):
        for w in self._widgets.get(book.id, []):
            w.update(book)

    def _refresh_playing(self):
        book = self.app.player.book
        if book:
            for b in self.books:
                if b.id == book.id:
                    b.position = self.app.player.position
                    self._update_book(b)

    def _on_position(self, player, pos):
        book = player.book
        if not book or book.id not in self._widgets:
            return
        for b in self.books:
            if b.id == book.id:
                if abs(b.position - pos) > 5:
                    b.position = pos
                    self._update_book(b)
                break

    def _on_scan_started(self, _scanner):
        self.scan_progress.set_fraction(0)
        self.scan_label.set_label("Scanning library…")
        self.scan_revealer.set_reveal_child(True)
        if not self.books:
            self.stack.set_visible_child_name("loading")

    def _on_scan_progress(self, _scanner, done, total):
        self.scan_progress.set_fraction(done / total if total else 0)
        self.scan_label.set_label(f"Scanning library… {done} of {total}")

    def _on_search_changed(self, _entry):
        if self._search_source:
            GLib.source_remove(self._search_source)
        self._search_source = GLib.timeout_add(120, self._do_search)

    def _do_search(self):
        self._search_source = 0
        self.rebuild()
        return False

    def _on_filter(self, toggles, _pspec):
        self.filter = toggles.get_active_name() or "all"
        self.app.set_setting("filter", self.filter)
        self.rebuild()

    def _on_view_toggled(self, toggles, _pspec):
        self.view = toggles.get_active_name() or "grid"
        self.app.set_setting("view", self.view)
        self.rebuild()

    def _on_menu_state(self, action, value):
        action.set_state(value)
        setattr(self, action.get_name(), value.get_string())
        self.app.set_setting(action.get_name(), value.get_string())
        self.rebuild()

