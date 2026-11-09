"""Sleep timer, cover picker, metadata editor and preferences dialogs."""
import datetime as dt
import logging
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, PangoCairo

from .. import covers, stats
from ..sleeptimer import PRESETS
from ..util import run_in_thread
from .cover import CoverImage, TEXTURES

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Sleep timer
# --------------------------------------------------------------------------- #

class SleepTimerDialog(Adw.Dialog):
    def __init__(self, app):
        super().__init__(title="Sleep Timer", content_width=420)
        self.app = app
        last = app.setting("sleep_last") or {}
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()

        presets = Adw.PreferencesGroup(title="Stop Playback In")
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, max_children_per_line=3,
                           min_children_per_line=3, column_spacing=8, row_spacing=8)
        for m in PRESETS:
            b = Gtk.Button(label=f"{m} min", css_classes=["pill", "timer-preset"])
            if last.get("mode") == "duration" and last.get("minutes") == m:
                b.add_css_class("suggested-action")
            b.connect("clicked", lambda _b, m=m: self._start(minutes=m))
            flow.append(b)
        presets.add(flow)
        chapter = Gtk.Button(label="End of Current Chapter", css_classes=["pill"], margin_top=8)
        if last.get("mode") == "chapter":
            chapter.add_css_class("suggested-action")
        chapter.set_sensitive(app.player.book is not None and len(app.player.book.chapters) > 0)
        chapter.connect("clicked", lambda *_: self._start(chapter=True))
        presets.add(chapter)
        page.add(presets)

        custom = Adw.PreferencesGroup(title="Custom")
        total = 30 * 60
        if last.get("mode") == "duration":
            total = int(last.get("seconds", int(last.get("minutes", 30)) * 60))
        self.hours = Adw.SpinRow.new_with_range(0, 12, 1)
        self.hours.set_title("Hours")
        self.hours.set_value(total // 3600)
        self.minutes = Adw.SpinRow.new_with_range(0, 59, 1)
        self.minutes.set_title("Minutes")
        self.minutes.set_value(total // 60 % 60)
        self.seconds = Adw.SpinRow.new_with_range(0, 59, 1)
        self.seconds.set_title("Seconds")
        self.seconds.set_value(total % 60)
        for row in (self.hours, self.minutes, self.seconds):
            custom.add(row)
        start = Adw.ButtonRow(title="Start Custom Timer", css_classes=["suggested-action"])
        start.connect("activated", lambda *_: self._start(seconds=int(self.hours.get_value()) * 3600
                                                          + int(self.minutes.get_value()) * 60
                                                          + int(self.seconds.get_value())))
        custom.add(start)
        page.add(custom)

        opts = Adw.PreferencesGroup(title="When the Timer Ends")
        fade = Adw.SwitchRow(title="Fade Out", subtitle="Lower the volume gradually at the end",
                             active=bool(app.setting("sleep_fade")))
        fade.connect("notify::active", lambda r, _p: app.set_setting("sleep_fade", r.get_active()))
        fade_len = Adw.SpinRow.new_with_range(5, 300, 5)
        fade_len.set_title("Fade Length")
        fade_len.set_subtitle("Seconds")
        fade_len.set_value(float(app.setting("sleep_fade_seconds")))
        fade_len.connect("notify::value", lambda r, _p: app.set_setting("sleep_fade_seconds", int(r.get_value())))
        fade.bind_property("active", fade_len, "sensitive", GObject.BindingFlags.SYNC_CREATE)
        rewind = Adw.SpinRow.new_with_range(0, 600, 5)
        rewind.set_title("Rewind on Stop")
        rewind.set_subtitle("Seconds to go back, so you don't miss what you dozed through")
        rewind.set_value(float(app.setting("sleep_rewind_seconds")))
        rewind.connect("notify::value", lambda r, _p: app.set_setting("sleep_rewind_seconds", int(r.get_value())))
        rows = [fade, fade_len, rewind]
        if app.screen.available:
            end_dark = Adw.SwitchRow(title="Turn Off Screen and Keyboard Light",
                                     subtitle="They come back when you touch the keyboard or mouse",
                                     active=bool(app.setting("sleep_end_screen_off")))
            end_dark.connect("notify::active", lambda r, _p: app.set_setting("sleep_end_screen_off", r.get_active()))
            rows.append(end_dark)
        for row in rows:
            opts.add(row)
        page.add(opts)

        if app.screen.available:
            dark = Adw.PreferencesGroup(title="When the Timer Starts")
            screen = Adw.SwitchRow(title="Turn Off Screen and Keyboard Light",
                                   subtitle="They come back when you touch the keyboard or mouse",
                                   active=bool(app.setting("sleep_screen_off")))
            screen.connect("notify::active", lambda r, _p: app.set_setting("sleep_screen_off", r.get_active()))
            dark.add(screen)
            page.add(dark)

        if app.sleep.active:
            cancel_group = Adw.PreferencesGroup()
            cancel = Adw.ButtonRow(title="Cancel Running Timer", css_classes=["destructive-action"])
            cancel.connect("activated", lambda *_: (app.sleep.cancel(), self.close()))
            cancel_group.add(cancel)
            page.add(cancel_group)

        toolbar.set_content(page)
        self.set_child(toolbar)

    def _start(self, minutes=None, seconds=None, chapter=False):
        if minutes:
            seconds = minutes * 60
        if chapter:
            self.app.sleep.start_end_of_chapter()
            self.app.set_setting("sleep_last", {"mode": "chapter"})
        elif seconds:
            self.app.sleep.start_seconds(seconds)
            last = {"mode": "duration", "seconds": seconds}
            if seconds % 60 == 0:
                last["minutes"] = seconds // 60      # highlights the matching preset next time
            self.app.set_setting("sleep_last", last)
        else:
            return
        self.close()
        if self.app.setting("sleep_screen_off"):
            self.app.screen.turn_off()


# --------------------------------------------------------------------------- #
# Cover picker
# --------------------------------------------------------------------------- #

class ResultTile(Gtk.FlowBoxChild):
    def __init__(self, result):
        super().__init__()
        self.result = result
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["cover-result"])
        self.picture = CoverImage(140)
        self.picture.set_cover(None, result.title, result.author)
        box.append(self.picture)
        box.append(Gtk.Label(label=result.title, xalign=0, ellipsize=3, max_width_chars=1, width_request=140,
                             css_classes=["caption-heading"]))
        box.append(Gtk.Label(label=result.author or "—", xalign=0, ellipsize=3, max_width_chars=1,
                             width_request=140, css_classes=["caption", "dim-label"]))
        box.append(Gtk.Label(label=f"{result.source} · {int(result.score * 100)}% match", xalign=0,
                             css_classes=["caption", "dim-label"]))
        self.set_child(box)
        self.set_tooltip_text(f"{result.title}\n{result.author}\n{result.source}")
        run_in_thread(lambda: covers.fetch_image([result.thumb_url], min_bytes=200),
                      callback=self._show, error_callback=lambda _e: None)

    def _show(self, found):
        data, _mime = found
        try:
            self.picture.set_texture(Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)), self.result.title)
        except GLib.Error:
            pass


class CoverPickerDialog(Adw.Dialog):
    def __init__(self, app, book):
        super().__init__(title="Find Cover", content_width=680, content_height=620)
        self.app = app
        self.book = book
        self._generation = 0
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.apply = Gtk.Button(label="Use Cover", css_classes=["suggested-action"], sensitive=False)
        self.apply.connect("clicked", self._on_apply)
        header.pack_end(self.apply)
        toolbar.add_top_bar(header)

        search_box = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_bottom=6)
        self.entry = Gtk.SearchEntry(hexpand=True, text=" ".join(filter(None, (book.title, book.author))))
        self.entry.connect("activate", lambda *_: self.search())
        search_box.append(self.entry)
        file_btn = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Choose Image File…")
        file_btn.connect("clicked", lambda *_: self._choose_file())
        search_box.append(file_btn)
        toolbar.add_top_bar(search_box)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        spinner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        spinner.append(Adw.Spinner(width_request=40, height_request=40))
        spinner.append(Gtk.Label(label="Searching iTunes, Open Library and Google Books…", css_classes=["dim-label"]))
        self.stack.add_named(spinner, "loading")
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, homogeneous=True, valign=Gtk.Align.START,
                                max_children_per_line=6, min_children_per_line=2, halign=Gtk.Align.CENTER, column_spacing=12,
                                row_spacing=12, margin_start=12, margin_end=12, margin_top=6, margin_bottom=12)
        self.flow.connect("selected-children-changed", lambda f: self.apply.set_sensitive(bool(f.get_selected_children())))
        self.flow.connect("child-activated", lambda *_: self._on_apply())
        self.stack.add_named(Gtk.ScrolledWindow(child=self.flow, hscrollbar_policy=Gtk.PolicyType.NEVER), "results")
        self.empty = Adw.StatusPage(icon_name="image-missing-symbolic", title="No Covers Found",
                                    description="Try different search words, or choose an image file.")
        self.stack.add_named(self.empty, "empty")
        offline = Adw.StatusPage(icon_name="network-offline-symbolic", title="You're Offline",
                                 description="Cover search needs an internet connection.")
        retry = Gtk.Button(label="Try Again", css_classes=["pill"], halign=Gtk.Align.CENTER)
        retry.connect("clicked", lambda *_: self.search())
        offline.set_child(retry)
        self.stack.add_named(offline, "offline")
        toolbar.set_content(self.stack)
        self.set_child(toolbar)
        self.search()

    def search(self):
        query = self.entry.get_text().strip()
        if not query:
            return
        self._generation += 1
        gen = self._generation
        self.stack.set_visible_child_name("loading")
        title, author = self.book.title, self.book.author
        if query != " ".join(filter(None, (title, author))):
            title, author = query, ""
        run_in_thread(covers.search, title, author,
                      callback=lambda res: self._show(gen, res),
                      error_callback=lambda e: self._failed(gen, e))

    def _show(self, gen, results):
        if gen != self._generation:
            return
        self.flow.remove_all()
        for r in results:
            self.flow.append(ResultTile(r))
        self.stack.set_visible_child_name("results" if results else "empty")
        if results:
            self.flow.select_child(self.flow.get_child_at_index(0))

    def _failed(self, gen, error):
        if gen != self._generation:
            return
        self.stack.set_visible_child_name("offline" if isinstance(error, covers.NetworkError) else "empty")

    def _on_apply(self, *_):
        selected = self.flow.get_selected_children()
        if not selected:
            return
        result = selected[0].result
        self.apply.set_sensitive(False)
        self.stack.set_visible_child_name("loading")
        book = self.book

        def work():
            data, mime = covers.fetch_image(result.full_urls)
            return covers.save_to_cache(book.path, data, mime)

        def done(path):
            TEXTURES.invalidate(path)
            self.app.set_cover(book.id, path, "online")
            self.close()
            maybe_offer_embed(self.app, book.id)

        def failed(e):
            self.apply.set_sensitive(True)
            self.stack.set_visible_child_name("results")
            self.app.toast("Could not download that cover" + (" — you seem to be offline"
                                                              if isinstance(e, covers.NetworkError) else ""))
        run_in_thread(work, callback=done, error_callback=failed)

    def _choose_file(self):
        choose_cover_file(self.app, self.book, parent=self.app.window, on_done=self.close)


def choose_cover_file(app, book, parent, on_done=None):
    dialog = Gtk.FileDialog(title="Choose Cover Image")
    filt = Gtk.FileFilter(name="Images")
    filt.add_mime_type("image/*")
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(filt)
    dialog.set_filters(filters)
    dialog.set_initial_folder(Gio.File.new_for_path(book.folder))

    def finished(d, res):
        try:
            f = d.open_finish(res)
        except GLib.Error:
            return
        data = Path(f.get_path()).read_bytes()
        mime = covers.sniff_mime(data) or "image/jpeg"
        path = covers.save_to_cache(book.path, data, mime)
        app.set_cover(book.id, path, "user")
        if on_done:
            on_done()
        maybe_offer_embed(app, book.id)
    dialog.open(parent, None, finished)


def maybe_offer_embed(app, book_id):
    if app.setting("cover_embed"):
        book = app.db.get_book(book_id)
        if book:
            confirm_embed(app, book)


def confirm_embed(app, book):
    n = len(book.tracks)
    alert = Adw.AlertDialog(
        heading="Embed Cover Into Files?",
        body=f"This modifies {n} audio file{'s' if n != 1 else ''} of “{book.title}”. "
             "Lectern shows the cover either way.")
    alert.add_response("cancel", "Only Use in Lectern")
    alert.add_response("embed", "Embed")
    alert.set_response_appearance("embed", Adw.ResponseAppearance.SUGGESTED)
    alert.set_default_response("cancel")
    alert.connect("response", lambda _d, r: app.embed_cover(book) if r == "embed" else None)
    alert.present(app.window)


# --------------------------------------------------------------------------- #
# Edit metadata
# --------------------------------------------------------------------------- #

class EditBookDialog(Adw.Dialog):
    FIELDS = (("title", "Title"), ("author", "Author"), ("narrator", "Narrator"), ("series", "Series"),
              ("series_index", "Number in Series"), ("year", "Year"))

    def __init__(self, app, book):
        super().__init__(title="Edit Details", content_width=460)
        self.app = app
        self.book = book
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label="Save", css_classes=["suggested-action"])
        save.connect("clicked", self._save)
        header.pack_end(save)
        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()
        top = Adw.PreferencesGroup()
        cover = CoverImage(120)
        cover.set_book(book)
        cover.set_margin_bottom(6)
        top.add(cover)
        page.add(top)
        group = Adw.PreferencesGroup(description="Changes are stored in Lectern's library and survive rescans.")
        self.rows = {}
        for key, label in self.FIELDS:
            row = Adw.EntryRow(title=label, text=getattr(book, key) or "")
            group.add(row)
            self.rows[key] = row
        page.add(group)
        files = Adw.PreferencesGroup()
        self.write = Adw.SwitchRow(title="Also Write to Audio Files",
                                   subtitle="Update the tags inside the files as well")
        files.add(self.write)
        page.add(files)
        toolbar.set_content(page)
        self.set_child(toolbar)

    def _save(self, *_):
        fields = {k: r.get_text().strip() for k, r in self.rows.items()}
        changed = {k: v for k, v in fields.items() if v != (getattr(self.book, k) or "")}
        write = self.write.get_active()
        if not changed and not write:
            self.close()
            return
        if write:
            n = len(self.book.tracks)
            alert = Adw.AlertDialog(heading="Write Tags to Files?",
                                    body=f"This modifies {n} audio file{'s' if n != 1 else ''}.")
            alert.add_response("cancel", "Cancel")
            alert.add_response("write", "Write Tags")
            alert.set_response_appearance("write", Adw.ResponseAppearance.SUGGESTED)

            def resp(_d, r):
                if r == "write":
                    self.app.update_metadata(self.book, fields, write_files=True)
                    self.close()
            alert.connect("response", resp)
            alert.present(self)
        else:
            self.app.update_metadata(self.book, changed)
            self.close()


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #

def _spin(title, subtitle, lo, hi, step, value, on_change):
    row = Adw.SpinRow.new_with_range(lo, hi, step)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_value(float(value))
    row.connect("notify::value", lambda r, _p: on_change(int(r.get_value())))
    return row


def _switch(title, subtitle, value, on_change):
    row = Adw.SwitchRow(title=title, subtitle=subtitle or "", active=bool(value))
    row.connect("notify::active", lambda r, _p: on_change(r.get_active()))
    return row


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, app, window):
        super().__init__(search_enabled=False)
        self.app = app
        self.window = window
        s, setv = app.setting, app.set_setting

        page = Adw.PreferencesPage(title="Playback", icon_name="media-playback-start-symbolic")
        skip = Adw.PreferencesGroup(title="Skip Buttons")

        def set_skip(key, v):
            setv(key, v)
            window.now_playing.update_skip_labels()
        skip.add(_spin("Skip Back", "Seconds", 5, 300, 5, s("skip_back"), lambda v: set_skip("skip_back", v)))
        skip.add(_spin("Skip Forward", "Seconds", 5, 300, 5, s("skip_forward"), lambda v: set_skip("skip_forward", v)))
        page.add(skip)
        resume = Adw.PreferencesGroup(title="Resuming",
                                      description="Go back a little when you return after a long break.")
        resume.add(_switch("Rewind After Long Pauses", None, s("rewind_on_resume"),
                           lambda v: setv("rewind_on_resume", v)))
        resume.add(_spin("Pause Length", "Minutes before rewinding kicks in", 1, 720, 1,
                         s("rewind_after_minutes"), lambda v: setv("rewind_after_minutes", v)))
        resume.add(_spin("Rewind By", "Seconds", 5, 300, 5, s("rewind_seconds"), lambda v: setv("rewind_seconds", v)))
        page.add(resume)
        sleep = Adw.PreferencesGroup(title="Sleep Timer")
        sleep.add(_switch("Fade Out", "Lower the volume gradually before stopping", s("sleep_fade"),
                          lambda v: setv("sleep_fade", v)))
        sleep.add(_spin("Fade Length", "Seconds", 5, 300, 5, s("sleep_fade_seconds"),
                        lambda v: setv("sleep_fade_seconds", v)))
        sleep.add(_spin("Rewind When Timer Ends", "Seconds", 0, 600, 5, s("sleep_rewind_seconds"),
                        lambda v: setv("sleep_rewind_seconds", v)))
        if app.screen.available:
            sleep.add(_switch("Screen and Keyboard Light Off When Timer Ends",
                              "Back on when you touch the keyboard or mouse", s("sleep_end_screen_off"),
                              lambda v: setv("sleep_end_screen_off", v)))
            sleep.add(_switch("Screen and Keyboard Light Off When Timer Starts", None, s("sleep_screen_off"),
                              lambda v: setv("sleep_screen_off", v)))
        page.add(sleep)
        general = Adw.PreferencesGroup(title="Window")
        general.add(_switch("Keep Playing When Closed", "Closing the window while a book plays keeps it playing; "
                            "use media keys or reopen Lectern", s("background_playback"),
                            lambda v: setv("background_playback", v)))
        page.add(general)
        self.add(page)

        page = Adw.PreferencesPage(title="Library", icon_name="folder-music-symbolic")
        self.folders = Adw.PreferencesGroup(title="Library Folders",
                                            description="Watched for new, moved and removed books.")
        self._folder_rows = []
        page.add(self.folders)
        self._fill_folders()
        actions = Adw.PreferencesGroup()
        rescan = Adw.ButtonRow(title="Rescan Library", start_icon_name="view-refresh-symbolic")
        rescan.connect("activated", lambda *_: app.rescan(full=True))
        actions.add(rescan)
        page.add(actions)

        art = Adw.PreferencesGroup(title="Cover Art", description="Searches iTunes, Open Library and Google Books.")
        art.add(_switch("Find Missing Covers Online", "Search for covers when books without art are imported",
                        s("cover_auto_fetch"), lambda v: setv("cover_auto_fetch", v)))
        art.add(_switch("Automatically Pick Best Match", "Otherwise use “Find Cover” to choose yourself",
                        s("cover_auto_pick"), lambda v: setv("cover_auto_pick", v)))
        art.add(_switch("Offer to Embed Covers Into Files", "Ask after choosing a cover; files are never "
                        "changed without asking", s("cover_embed"), lambda v: setv("cover_embed", v)))
        page.add(art)
        self.add(page)

    def _fill_folders(self):
        for row in self._folder_rows:
            self.folders.remove(row)
        self._folder_rows = []
        for path in self.app.db.folders():
            row = Adw.ActionRow(title=GLib.markup_escape_text(path.replace(str(Path.home()), "~", 1)))
            row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                css_classes=["flat"], tooltip_text="Remove From Library")
            remove.connect("clicked", lambda _b, p=path: self._remove(p))
            row.add_suffix(remove)
            self.folders.add(row)
            self._folder_rows.append(row)
        add = Adw.ButtonRow(title="Add Folder…", start_icon_name="list-add-symbolic")
        add.connect("activated", lambda *_: self.window.choose_folder(on_done=self._fill_folders))
        self.folders.add(add)
        self._folder_rows.append(add)

    def _remove(self, path):
        alert = Adw.AlertDialog(heading="Remove Folder?",
                                body="Its books disappear from the library. Your files are not touched.")
        alert.add_response("cancel", "Cancel")
        alert.add_response("remove", "Remove")
        alert.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r == "remove":
                self.app.remove_folder(path)
                self._fill_folders()
        alert.connect("response", resp)
        alert.present(self)


# --------------------------------------------------------------------------- #
# Listening statistics
# --------------------------------------------------------------------------- #

class _BarChart(Gtk.DrawingArea):
    def __init__(self, days):
        super().__init__(content_height=150, hexpand=True)
        self.days = days
        self.set_draw_func(self._draw)
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             ["Listening time per day, last two weeks: " +
                              ", ".join(f"{d.strftime('%a %d')}: {int(s // 60)} min" for d, s in days)])

    def _draw(self, _area, cr, w, h):
        style = Adw.StyleManager.get_default()
        accent = style.get_accent_color_rgba()
        fg = self.get_color()
        label_h = 18
        top = 14
        peak = max([s for _, s in self.days] + [1800])  # at least a 30 min scale
        n = len(self.days)
        slot = w / n
        bar_w = min(26, slot * 0.6)
        layout = self.create_pango_layout("")
        for i, (day, secs) in enumerate(self.days):
            x = i * slot + (slot - bar_w) / 2
            bh = (h - label_h - top) * secs / peak
            if secs > 0:
                bh = max(bh, 3)
                r = min(6, bar_w / 2, bh / 2)
                y = h - label_h - bh
                cr.new_sub_path()
                cr.arc(x + r, y + r, r, 3.1416, 1.5 * 3.1416)
                cr.arc(x + bar_w - r, y + r, r, 1.5 * 3.1416, 0)
                cr.line_to(x + bar_w, h - label_h)
                cr.line_to(x, h - label_h)
                cr.close_path()
                is_today = i == n - 1
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 1.0 if is_today else 0.55)
                cr.fill()
            layout.set_text(day.strftime("%a")[0], -1)
            _ink, logical = layout.get_pixel_extents()
            cr.move_to(i * slot + (slot - logical.width) / 2, h - label_h + 2)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            PangoCairo.show_layout(cr, layout)


class StatsDialog(Adw.Dialog):
    def __init__(self, app):
        super().__init__(title="Listening Stats", content_width=520)
        db = app.db
        today = dt.date.today()
        by_day = db.listening_by_day((today - dt.timedelta(days=30)).isoformat())
        days = stats.last_days(by_day, 14, today)
        week = sum(s for d, s in days[-7:])
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()

        summary = Adw.PreferencesGroup()
        grid = Gtk.Grid(column_homogeneous=True, column_spacing=12, row_spacing=12, margin_bottom=6)
        tiles = (("Today", stats.hours_minutes(by_day.get(today.isoformat(), 0))),
                 ("This Week", stats.hours_minutes(week)),
                 ("Day Streak", str(stats.streak(db.listening_days(), today))),
                 ("Total", stats.hours_minutes(db.listening_total())),
                 ("Books Finished", str(db.finished_count())))
        for i, (label, value) in enumerate(tiles):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["card", "stat-tile"])
            box.append(Gtk.Label(label=value, css_classes=["title-2", "numeric"]))
            box.append(Gtk.Label(label=label, css_classes=["caption", "dim-label"]))
            grid.attach(box, i % 3, i // 3, 1, 1)
        summary.add(grid)
        page.add(summary)

        chart = Adw.PreferencesGroup(title="Last Two Weeks")
        frame = Gtk.Box(css_classes=["card"], margin_bottom=6)
        inner = _BarChart(days)
        inner.set_margin_top(12)
        inner.set_margin_bottom(8)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        frame.append(inner)
        chart.add(frame)
        page.add(chart)

        top = db.listening_by_book(5)
        if top:
            books = Adw.PreferencesGroup(title="Most Listened")
            for _id, title, secs in top:
                row = Adw.ActionRow(title=GLib.markup_escape_text(title))
                row.add_suffix(Gtk.Label(label=stats.hours_minutes(secs), css_classes=["dim-label", "numeric"]))
                books.add(row)
            page.add(books)
        toolbar.set_content(page)
        self.set_child(toolbar)
