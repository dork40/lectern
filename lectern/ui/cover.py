"""Cover art widgets: async texture loading, generated placeholders, palette extraction."""
import colorsys
import hashlib
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor

import cairo

from gi.repository import GdkPixbuf, Gdk, GLib, Graphene, Gsk, Gtk, Pango, PangoCairo

from ..const import THUMB_CACHE

log = logging.getLogger(__name__)
_scale = None


def display_scale():
    """Largest monitor scale (fractional scales rounded up), so covers stay crisp."""
    global _scale
    if _scale is None:
        _scale = 1
        display = Gdk.Display.get_default()
        if display is not None:
            monitors = display.get_monitors()
            for i in range(monitors.get_n_items()):
                m = monitors.get_item(i)
                scale = m.get_scale() if hasattr(m, "get_scale") else m.get_scale_factor()
                _scale = max(_scale, math.ceil(scale))
    return _scale


def _thumbnail(path, size):
    """Scaled pixbuf for a cover, via a disk cache (decoding a 3000 px cover every launch is slow)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        raise GLib.Error("cover file missing")
    key = hashlib.sha1(f"{path}|{mtime}|{size}".encode()).hexdigest()
    cached = THUMB_CACHE / f"{key}.png"
    if cached.exists():
        return GdkPixbuf.Pixbuf.new_from_file(str(cached))
    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
    try:
        THUMB_CACHE.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(".tmp")
        pb.savev(str(tmp), "png", ["compression"], ["3"])
        tmp.replace(cached)
    except (GLib.Error, OSError) as e:
        log.debug("cannot cache thumbnail: %s", e)
    return pb


def _pixbuf_to_texture(pb):
    fmt = Gdk.MemoryFormat.R8G8B8A8 if pb.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(pb.get_width(), pb.get_height(), fmt,
                                 pb.read_pixel_bytes(), pb.get_rowstride())


def dominant_color(pb):
    """Pick a pleasant accent colour from a pixbuf: the most saturated well-lit hue cluster."""
    small = pb.scale_simple(24, 24, GdkPixbuf.InterpType.BILINEAR)
    data = small.get_pixels()
    n = small.get_n_channels()
    stride = small.get_rowstride()
    buckets = {}
    total = [0.0, 0.0, 0.0, 0.0]
    for y in range(24):
        for x in range(24):
            i = y * stride + x * n
            r, g, b = data[i] / 255, data[i + 1] / 255, data[i + 2] / 255
            h, s, v = colorsys.rgb_to_hsv(r, g, b)
            total[0] += r
            total[1] += g
            total[2] += b
            total[3] += 1
            weight = s * s * (1 - abs(v - 0.6))
            if weight <= 0.02:
                continue
            key = int(h * 12) % 12
            acc = buckets.setdefault(key, [0.0, 0.0, 0.0, 0.0])
            acc[0] += r * weight
            acc[1] += g * weight
            acc[2] += b * weight
            acc[3] += weight
    if buckets:
        acc = max(buckets.values(), key=lambda a: a[3])
        if acc[3] > 1.5:
            return acc[0] / acc[3], acc[1] / acc[3], acc[2] / acc[3]
    return total[0] / total[3], total[1] / total[3], total[2] / total[3]


class TextureCache:
    """Loads scaled cover textures on a small thread pool, cached in memory."""

    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="covers")
        self._textures = {}   # (path, size) -> texture | None
        self._colors = {}     # path -> (r, g, b)
        self._waiting = {}    # (path, size) -> [callbacks]

    def lookup(self, path, size):
        return self._textures.get((path, size))

    def color(self, path):
        return self._colors.get(path)

    def invalidate(self, path):
        for key in [k for k in self._textures if k[0] == path]:
            del self._textures[key]
        self._colors.pop(path, None)

    def load(self, path, size, callback):
        """callback(texture_or_None) on the main loop (possibly immediately)."""
        key = (path, size)
        if key in self._textures:
            callback(self._textures[key])
            return
        if key in self._waiting:
            self._waiting[key].append(callback)
            return
        self._waiting[key] = [callback]
        self._pool.submit(self._work, path, size)

    def _work(self, path, size):
        texture = color = None
        try:
            pb = _thumbnail(path, size)
            texture = _pixbuf_to_texture(pb)
            if path not in self._colors:
                color = dominant_color(pb)
        except (GLib.Error, ZeroDivisionError) as e:
            log.info("cannot load cover %s: %s", path, getattr(e, "message", e))

        def deliver():
            key = (path, size)
            self._textures[key] = texture
            if color:
                self._colors[path] = color
            for cb in self._waiting.pop(key, []):
                cb(texture)
            return False
        GLib.idle_add(deliver)


TEXTURES = TextureCache()


def placeholder_colors(title, author):
    h = int(hashlib.md5(f"{title}|{author}".encode()).hexdigest()[:8], 16)
    hue = (h % 360) / 360
    c1 = colorsys.hls_to_rgb(hue, 0.42, 0.55)
    c2 = colorsys.hls_to_rgb((hue + 0.09 + (h >> 12) % 40 / 400) % 1, 0.28, 0.6)
    return c1, c2


def draw_placeholder(cr, width, height, title, author):
    c1, c2 = placeholder_colors(title, author)
    grad = cairo.LinearGradient(0, 0, width, height)
    grad.add_color_stop_rgb(0, *c1)
    grad.add_color_stop_rgb(1, *c2)
    cr.set_source(grad)
    cr.paint()

    # soft decorative circle
    cr.set_source_rgba(1, 1, 1, 0.07)
    cr.arc(width * 0.85, height * 0.15, width * 0.45, 0, 2 * math.pi)
    cr.fill()

    if width < 90:
        # Too small for a title: show initials instead.
        words = [w for w in (title or "?").split() if w[:1].isalnum()]
        initials = "".join(w[0] for w in words[:2]).upper() or "?"
        layout = PangoCairo.create_layout(cr)
        font = Pango.FontDescription.from_string("Sans Bold")
        font.set_absolute_size(width * 0.38 * Pango.SCALE)
        layout.set_font_description(font)
        layout.set_text(initials, -1)
        _, ext = layout.get_pixel_extents()
        cr.move_to((width - ext.width) / 2 - ext.x, (height - ext.height) / 2 - ext.y)
        cr.set_source_rgba(1, 1, 1, 0.95)
        PangoCairo.show_layout(cr, layout)
        return

    pad = width * 0.1
    layout = PangoCairo.create_layout(cr)
    layout.set_width(int((width - 2 * pad) * Pango.SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_ellipsize(Pango.EllipsizeMode.END)
    layout.set_height(-4)  # at most four lines
    font = Pango.FontDescription.from_string("Sans Bold")
    font.set_absolute_size(max(8, width * 0.105) * Pango.SCALE)
    layout.set_font_description(font)
    layout.set_text(title or "Untitled", -1)
    cr.move_to(pad, pad * 1.2)
    cr.set_source_rgba(1, 1, 1, 0.96)
    PangoCairo.show_layout(cr, layout)

    if author:
        small = PangoCairo.create_layout(cr)
        small.set_width(int((width - 2 * pad) * Pango.SCALE))
        small.set_ellipsize(Pango.EllipsizeMode.END)
        f2 = Pango.FontDescription.from_string("Sans")
        f2.set_absolute_size(max(7, width * 0.07) * Pango.SCALE)
        small.set_font_description(f2)
        small.set_text(author, -1)
        _, l2 = small.get_pixel_extents()
        cr.move_to(pad, height - pad - l2.height)
        cr.set_source_rgba(1, 1, 1, 0.78)
        PangoCairo.show_layout(cr, small)


class CoverImage(Gtk.Widget):
    """Fixed-size square cover with rounded corners; gradient placeholder until art loads."""

    RADII = {"small": 6, "hero": 12, "large": 18}

    def __init__(self, size=160, css="", load_size=None):
        super().__init__(halign=Gtk.Align.CENTER, valign=Gtk.Align.START,
                         css_classes=["cover"] + ([css] if css else []))
        self.size = size
        self.load_size = (load_size or size) * display_scale()
        self.radius = self.RADII.get(css, 10)
        self._title = self._author = ""
        self._path = None
        self._texture = None

    def set_size(self, size):
        self.size = size
        self.queue_resize()

    def do_measure(self, _orientation, _for_size):
        return self.size, self.size, -1, -1

    def set_book(self, book):
        self.set_cover(book.cover_path if book else None, book.title if book else "", book.author if book else "")

    def set_cover(self, path, title="", author=""):
        self._title, self._author = title, author
        self._path = path
        self._texture = TEXTURES.lookup(path, self.load_size) if path else None
        if path and self._texture is None:
            TEXTURES.load(path, self.load_size, lambda tex, p=path: self._loaded(p, tex))
        self.queue_draw()

    def set_texture(self, texture, title=""):
        """Show an already-decoded texture (e.g. a downloaded search thumbnail)."""
        self._path, self._title, self._texture = None, title, texture
        self.queue_draw()

    def _loaded(self, path, texture):
        if path == self._path:
            self._texture = texture
            self.queue_draw()

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        rect = Graphene.Rect().init(0, 0, w, h)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(rect, self.radius)
        snapshot.push_rounded_clip(rounded)
        tex = self._texture
        if tex is not None:
            tw, th = tex.get_width(), tex.get_height()
            scale = max(w / tw, h / th)
            dw, dh = tw * scale, th * scale
            bounds = Graphene.Rect().init((w - dw) / 2, (h - dh) / 2, dw, dh)
            snapshot.append_scaled_texture(tex, Gsk.ScalingFilter.TRILINEAR, bounds)
        else:
            cr = snapshot.append_cairo(rect)
            draw_placeholder(cr, w, h, self._title, self._author)
        snapshot.pop()
