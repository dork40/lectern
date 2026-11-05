"""Bookmark list widget (book page and now-playing popover) and the note dialog."""
from gi.repository import Adw, GLib, Gtk

from ..util import format_time


def edit_note(app, parent, bookmark_id, note="", on_done=None):
    dialog = Adw.AlertDialog(heading="Bookmark Note")
    entry = Gtk.Entry(text=note or "", placeholder_text="What happened here?", activates_default=True)
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("save", "Save")
    dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("save")

    def response(_d, r):
        if r == "save":
            app.db.update_bookmark(bookmark_id, entry.get_text().strip())
            if app.player.book:
                app.emit("bookmarks-changed", app.player.book.id)
            if on_done:
                on_done()
    dialog.connect("response", response)
    dialog.present(parent)


class BookmarkList(Gtk.ListBox):
    """Rows: time · chapter, note; click to jump, buttons to edit the note or delete."""

    def __init__(self, app, book, on_jump=None, compact=False):
        super().__init__(selection_mode=Gtk.SelectionMode.NONE,
                         css_classes=["navigation-sidebar"] if compact else ["boxed-list"])
        self.app = app
        self.book = book
        self.on_jump = on_jump
        self.connect("row-activated", self._activated)
        self.refill()

    def refill(self):
        self.remove_all()
        marks = self.app.db.bookmarks(self.book.id)
        hours = self.book.duration >= 3600
        for m in marks:
            idx = self.book.chapter_index(m["position"])
            chapter = self.book.chapters[idx].title if 0 <= idx < len(self.book.chapters) else ""
            row = Adw.ActionRow(activatable=True,
                                title=GLib.markup_escape_text(m["note"] or chapter or "Bookmark"),
                                subtitle=GLib.markup_escape_text(
                                    format_time(m["position"], force_hours=hours) + (f" · {chapter}" if m["note"] and chapter else "")))
            row.bookmark = m
            row.add_prefix(Gtk.Image(icon_name="lectern-bookmarks-symbolic", css_classes=["accent"]))
            edit = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                              tooltip_text="Edit Note")
            edit.connect("clicked", lambda _b, m=m: edit_note(self.app, self.get_root(), m["id"], m["note"], self.refill))
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text="Delete Bookmark")
            delete.connect("clicked", lambda _b, m=m: self._delete(m["id"]))
            row.add_suffix(edit)
            row.add_suffix(delete)
            self.append(row)
        if not marks:
            self.append(Adw.ActionRow(title="No bookmarks yet", subtitle="Press Ctrl+D while listening",
                                      activatable=False, sensitive=False))
        return bool(marks)

    def _delete(self, bookmark_id):
        self.app.db.delete_bookmark(bookmark_id)
        self.app.emit("bookmarks-changed", self.book.id)
        self.refill()

    def _activated(self, _lb, row):
        m = getattr(row, "bookmark", None)
        if m is None:
            return
        app = self.app
        if app.player.book and app.player.book.id == self.book.id:
            app.jump_to(m["position"], "bookmark")
            app.player.play()
        else:
            app.play_book(self.book, position=m["position"])
        if self.on_jump:
            self.on_jump()
