import logging
import os
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk

from ..const import APP_NAME
from .detail import BookDetailPage
from .bookmarks import edit_note
from .dialogs import (CoverPickerDialog, EditBookDialog, PreferencesDialog, SleepTimerDialog, StatsDialog,
                      choose_cover_file, confirm_embed)
from .library import LibraryPage
from .nowplaying import MiniPlayer, NowPlayingView

log = logging.getLogger(__name__)

TILING = ("hyprland", "sway", "river", "niri", "i3", "dwl", "qtile", "bspwm")


def tiling_compositor():
    """True on tiling window managers, where apps shouldn't manage their own size/maximize."""
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or os.environ.get("SWAYSOCK") or \
            os.environ.get("NIRI_SOCKET") or os.environ.get("I3SOCK"):
        return True
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    return any(t in desktop for t in TILING)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        w, h = app.setting("window_size") or (1100, 760)
        super().__init__(application=app, title=APP_NAME, default_width=w, default_height=h)
        self.app = app
        self.set_size_request(360, 520)
        # On tiling compositors (Hyprland, Sway…) the compositor decides the layout. Restoring
        # "maximized" there makes the window cover the whole workspace above other apps.
        if app.setting("window_maximized") and not tiling_compositor():
            self.maximize()

        self.nav = Adw.NavigationView()
        self.library = LibraryPage(app, self)
        self.nav.add(self.library)
        self.now_playing = NowPlayingView(app, self)
        self.mini = MiniPlayer(app)
        # Library (with the mini player as a bottom bar) and a full-window now-playing view
        # that slides up over it. (Adw.BottomSheet always leaves a strip of the page behind
        # visible at the top, which looked broken in fullscreen.)
        self.main = Adw.ToolbarView(content=self.nav, bottom_bar_style=Adw.ToolbarStyle.RAISED_BORDER)
        self.main.add_bottom_bar(self.mini)
        open_click = Gtk.GestureClick()
        open_click.connect("released", self._on_mini_clicked)
        self.mini.add_controller(open_click)
        self.mini.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self.stack = Gtk.Stack(transition_duration=320)
        self.stack.add_named(self.main, "library")
        self.stack.add_named(self.now_playing, "now")
        self.toasts = Adw.ToastOverlay(child=self.stack)
        self.set_content(self.toasts)

        app.player.connect("book-changed", lambda p: self.main.set_reveal_bottom_bars(p.book is not None))
        self.main.set_reveal_bottom_bars(app.player.book is not None)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 500sp"))
        narrow.add_setter(self.library.view_toggle, "visible", False)
        self.add_breakpoint(narrow)

        self._setup_actions()
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda *_: app.screen.off and app.screen.user_active())
        self.add_controller(motion)
        self.connect("close-request", self._on_close)
        if os.environ.get("LECTERN_SCREENSHOT"):
            # no compositor drives the frame clock under broadway: skip animations
            Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
            GLib.timeout_add(1500, self._dev_screenshot)

    # ------------------------------------------------------------ actions
    def _setup_actions(self):
        def add(name, cb):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("x"))
            action.connect("activate", lambda _a, v: self._with_book(v.get_int64(), cb))
            self.add_action(action)

        add("play-book", lambda b: self.app.play_book(b))
        add("toggle-finished", lambda b: self.app.mark_finished(b, not b.finished))
        add("find-cover", lambda b: CoverPickerDialog(self.app, b).present(self))
        add("cover-from-file", lambda b: choose_cover_file(self.app, b, self))
        add("embed-cover", self._embed_cover)
        add("edit-book", lambda b: EditBookDialog(self.app, b).present(self))
        add("reset-progress", self._reset_progress)
        add("open-folder", self._open_folder)

        search = Gio.SimpleAction.new("search", None)
        search.connect("activate", lambda *_: self._focus_search())
        self.add_action(search)
        self.app.set_accels_for_action("win.search", ["<Control>f"])

    def _with_book(self, book_id, cb):
        book = self.app.db.get_book(book_id)
        if book:
            cb(book)

    def _embed_cover(self, book):
        if not book.cover_path:
            self.toast("This book has no cover yet")
            return
        confirm_embed(self.app, book)

    def _reset_progress(self, book):
        if self.app.player.book and self.app.player.book.id == book.id:
            self.app.player.pause()
            self.app.player.seek(0)
        self.app.db.save_progress(book.id, 0.0, finished=False, touch=False)
        self.app.emit("book-updated", book.id)

    def _open_folder(self, book):
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(book.path if book.kind == "file" else book.folder))
        if book.kind == "file":
            launcher.open_containing_folder(self, None, None)
        else:
            launcher.launch(self, None, None)

    def _focus_search(self):
        self.close_now_playing()
        self.nav.pop_to_tag("library")
        self.library.focus_search()

    # ----------------------------------------------------------- navigation
    def show_book(self, book):
        tag = f"book-{book.id}"
        if self.nav.find_page(tag):
            self.nav.pop_to_tag(tag)
            return
        if self.nav.get_visible_page() is not self.library:
            self.nav.pop_to_tag("library")
        self.nav.push(BookDetailPage(self.app, self, book))

    @property
    def now_playing_open(self):
        return self.stack.get_visible_child_name() == "now"

    def open_now_playing(self):
        if self.app.player.book and not self.now_playing_open:
            self.stack.set_transition_type(Gtk.StackTransitionType.OVER_UP)
            self.stack.set_visible_child_name("now")

    def close_now_playing(self):
        if self.now_playing_open:
            self.stack.set_transition_type(Gtk.StackTransitionType.UNDER_DOWN)
            self.stack.set_visible_child_name("library")

    def _on_mini_clicked(self, gesture, _n, x, y):
        # clicks on the mini player's own buttons are theirs; anywhere else opens now playing
        target = self.mini.pick(x, y, Gtk.PickFlags.DEFAULT)
        while target is not None and target is not self.mini:
            if isinstance(target, Gtk.Button):
                return
            target = target.get_parent()
        self.open_now_playing()

    def choose_folder(self, on_done=None):
        dialog = Gtk.FileDialog(title="Choose Audiobook Folder")
        default = Path.home() / "Audiobooks"
        dialog.set_initial_folder(Gio.File.new_for_path(str(default if default.is_dir() else Path.home())))

        def finished(d, res):
            try:
                folder = d.select_folder_finish(res)
            except GLib.Error:
                return
            self.app.add_folder(folder.get_path())
            if on_done:
                on_done()
        dialog.select_folder(self, None, finished)

    def show_preferences(self):
        PreferencesDialog(self.app, self).present(self)

    def show_stats(self):
        StatsDialog(self.app).present(self)

    def edit_bookmark_note(self, bookmark_id):
        if bookmark_id is not None:
            edit_note(self.app, self, bookmark_id)

    def show_sleep_timer(self):
        if self.get_visible_dialog() is None:
            SleepTimerDialog(self.app).present(self)

    def show_shortcuts(self):
        dialog = Adw.ShortcutsDialog()
        section = Adw.ShortcutsSection(title="Playback")
        for title, accel in (("Play / Pause", "space"), ("Skip Back", "Left"), ("Skip Forward", "Right"),
                             ("Sleep Timer", "<Control>t"), ("Turn Off Screen", "<Control>b"),
                             ("Add Bookmark", "<Control>d"), ("Undo Jump", "<Control>z")):
            section.add(Adw.ShortcutsItem.new(title, accel))
        dialog.add(section)
        section = Adw.ShortcutsSection(title="Library")
        for title, accel in (("Search", "<Control>f"), ("Rescan Library", "<Control>r"),
                             ("Preferences", "<Control>comma"), ("Keyboard Shortcuts", "<Control>question"),
                             ("Quit", "<Control>q")):
            section.add(Adw.ShortcutsItem.new(title, accel))
        dialog.add(section)
        dialog.present(self)

    def toast(self, text, button=None, action=None):
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=4)
        if button and action:
            toast.set_button_label(button)
            toast.set_action_name(action)
        self.toasts.add_toast(toast)

    # ------------------------------------------------------------- input
    def _on_key(self, _ctrl, keyval, _keycode, state):
        if self.app.screen.off:
            self.app.screen.user_active()
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        if self.get_visible_dialog() is not None:
            return False
        focus = self.get_focus()
        if isinstance(focus, Gtk.Editable):
            return False
        p = self.app.player
        if keyval == Gdk.KEY_space:
            self.app.toggle()
            return True
        if isinstance(focus, Gtk.Range):
            return False
        if keyval == Gdk.KEY_Left and p.book:
            p.skip(-float(self.app.setting("skip_back")))
            return True
        if keyval == Gdk.KEY_Right and p.book:
            p.skip(float(self.app.setting("skip_forward")))
            return True
        if keyval == Gdk.KEY_Escape and self.now_playing_open:
            self.close_now_playing()
            return True
        return False

    def _on_close(self, *_):
        self._save_geometry()
        if self.app.player.playing and self.app.setting("background_playback"):
            self.set_visible(False)
            self.app.keep_playing_in_background()
            return True
        return False

    def _save_geometry(self):
        if tiling_compositor():
            return            # sizes there come from the tiling layout, not from us
        if not self.is_maximized():
            self.app.set_setting("window_size", [self.get_width(), self.get_height()])
        self.app.set_setting("window_maximized", self.is_maximized())

    # ----------------------------------------------------- dev screenshots
    def _dev_screenshot(self):
        """LECTERN_SCREENSHOT=out.png [LECTERN_VIEW=nowplaying|book:<id>|sleep|prefs|picker:<id>]"""
        view = os.environ.get("LECTERN_VIEW", "")
        if os.environ.get("LECTERN_FULLSCREEN"):
            self.fullscreen()
        for name in os.environ.get("LECTERN_FILTER", "").split():
            self.library.filters.set_active_name(name)
        if view == "nowplaying":
            self.open_now_playing()
        elif view.startswith("book:"):
            self._with_book(self._dev_book_id(view[5:]), self.show_book)
        elif view == "sleep":
            self.show_sleep_timer()
        elif view == "prefs":
            self.show_preferences()
        elif view == "stats":
            self.show_stats()
        elif view.startswith("picker:"):
            self._with_book(self._dev_book_id(view[7:]), lambda b: CoverPickerDialog(self.app, b).present(self))
        elif view == "search":
            self.library.focus_search()
            self.library.search_entry.set_text(os.environ.get("LECTERN_QUERY", ""))
        GLib.timeout_add(int(os.environ.get("LECTERN_DELAY", "2500")), self._dev_capture)
        return False

    def _dev_book_id(self, ref):
        """Book id, or the id of the first book whose title contains ref."""
        if ref.isdigit():
            return int(ref)
        return next((b.id for b in self.app.db.all_books() if ref.casefold() in b.title.casefold()), -1)

    def _dev_capture(self):
        w, h = self.get_width(), self.get_height()
        paintable = Gtk.WidgetPaintable.new(self)
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, w, h)
        node = snap.to_node()
        if node is not None:
            texture = self.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
            texture.save_to_png(os.environ["LECTERN_SCREENSHOT"])
        self.app.quit()
        return False
