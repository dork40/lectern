"""GStreamer playback engine.

A book is one continuous timeline made of one or more tracks. All public
positions are global seconds into the book; the engine maps them onto the
right file and keeps the playback rate (with pitch correction) across seeks
and file changes.
"""
import logging
import os

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst  # noqa: E402

from .audiofx import EffectsChain, EffectSettings  # noqa: E402

log = logging.getLogger(__name__)

PLAY_FLAG_AUDIO = 0x2
PLAY_FLAG_SOFT_VOLUME = 0x10
MIN_RATE, MAX_RATE = 0.5, 3.0


class Player(GObject.Object):
    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "position-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "chapter-changed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "book-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "book-finished": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "rate-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "seeked": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "casting-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        Gst.init(None)
        self.pipeline = Gst.ElementFactory.make("playbin", "lectern-player")
        if self.pipeline is None:
            raise RuntimeError("GStreamer 'playbin' is missing (install gst-plugins-base)")
        self.pipeline.set_property("flags", PLAY_FLAG_AUDIO | PLAY_FLAG_SOFT_VOLUME)
        fakevideo = Gst.ElementFactory.make("fakesink", None)
        if fakevideo is not None:
            self.pipeline.set_property("video-sink", fakevideo)
        sink = os.environ.get("LECTERN_AUDIO_SINK")  # e.g. "fakesink sync=true" for tests
        self._sink_desc = sink
        if sink:
            self.pipeline.set_property("audio-sink", Gst.parse_bin_from_description(sink, True))
        self.fx = EffectsChain()
        self.pipeline.set_property("audio-filter", self.fx.bin)
        # Gapless: queue the next file of a folder book just before this one ends.
        self.pipeline.connect("about-to-finish", self._on_about_to_finish)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

        self.book = None
        self.track_index = 0
        self._pending_seek = None     # local seconds to seek to once prerolled
        self._want_playing = False
        self._rate = 1.0
        self._last_pos = 0.0
        self._chapter = -1
        self._at_end = False
        self._tick_id = 0
        self._gapless_next = None     # track index queued by about-to-finish
        self.remote = None            # cast/renderer session while playing on another device
        self.output = None            # AirPlay: we keep playing, but into another device's audio feed
        self._output_anchor = 0.0     # AirPlay: where the TV (re)started playing from
        self._effects = EffectSettings()
        self._remote_local = 0.0      # last requested position inside the current track
        self._remote_waiting = False  # a load/seek was sent and no status has confirmed it yet

    # ------------------------------------------------------------------ state
    @property
    def playing(self):
        return self._want_playing

    @property
    def rate(self):
        return self._rate

    @property
    def duration(self):
        return self.book.duration if self.book else 0.0

    @property
    def volume(self):
        return self.pipeline.get_property("volume")

    @volume.setter
    def volume(self, v):
        self.pipeline.set_property("volume", max(0.0, min(1.0, v)))

    @property
    def position(self):
        if not self.book:
            return 0.0
        track = self.book.tracks[self.track_index]
        if self.remote is not None:
            if self._at_end:
                return self.book.duration
            pos = None if self._remote_waiting else self.remote.position()
            local = self._remote_local if pos is None else min(pos, track.duration)
            self._last_pos = track.offset + local
            return self._last_pos
        if self._pending_seek is not None:
            return track.offset + self._pending_seek
        if self._at_end:
            return self.book.duration
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        if ok and pos >= 0:
            self._last_pos = track.offset + pos / Gst.SECOND
        if self.output is not None and self._want_playing:
            # AirPlay: the TV plays what we produced a few seconds ago (OwnTone's buffers)
            return max(self._output_anchor, self._last_pos - self.output.latency() * self._rate)
        return self._last_pos

    @property
    def chapter_index(self):
        return self.book.chapter_index(self.position) if self.book else -1

    @property
    def chapter(self):
        i = self.chapter_index
        return self.book.chapters[i] if self.book and i >= 0 else None

    # -------------------------------------------------------------- control
    def load(self, book, position=0.0, autoplay=False):
        self.pipeline.set_state(Gst.State.NULL)
        self.book = book
        self._want_playing = autoplay
        self._at_end = False
        self._chapter = -1
        position = max(0.0, min(position, max(0.0, book.duration - 1.0)))
        idx, local = book.locate(position)
        self._last_pos = position
        if self.remote is not None:
            self._remote_load(position, autoplay)
        else:
            self._open_track(idx, local)
        self.emit("book-changed")
        self._update_tick()
        self.emit("state-changed")

    def unload(self):
        if self.remote is not None:
            self.remote.pause()
        self.pipeline.set_state(Gst.State.NULL)
        self.book = None
        self._want_playing = False
        self._update_tick()
        self.emit("book-changed")
        self.emit("state-changed")

    def play(self):
        if not self.book:
            return
        if self._at_end:
            self.seek(0)
        if self.output is not None:
            self._output_anchor = self.position
        self._want_playing = True
        if self.output is not None:
            self.output.on_play()
        if self.remote is not None:
            self.remote.play()
        elif self._pending_seek is None:
            self.pipeline.set_state(Gst.State.PLAYING)
        self._update_tick()
        self.emit("state-changed")

    def pause(self):
        if not self.book:
            return
        heard = self.position if self.output is not None and self._want_playing else None
        self._want_playing = False
        if self.output is not None:
            self.output.on_pause()
        if self.remote is not None:
            self._remote_local = self.position - self.book.tracks[self.track_index].offset
            self.remote.pause()
        else:
            self.pipeline.set_state(Gst.State.PAUSED)
            if heard is not None:
                # OwnTone drops its buffered audio on pause: go back to what the TV last played
                self.seek(heard)
        self._update_tick()
        self.emit("state-changed")
        self.emit("position-changed", self.position)

    def toggle(self):
        self.pause() if self._want_playing else self.play()

    def seek(self, pos):
        if not self.book:
            return
        pos = max(0.0, min(pos, self.book.duration - 0.25))
        self._at_end = False
        idx, local = self.book.locate(pos)
        if self.remote is not None:
            if idx != self.track_index:
                self._remote_load(pos, self._want_playing)
            else:
                self._remote_local, self._remote_waiting = local, True
                self.remote.seek(local)
        elif idx != self.track_index or self._gapless_next is not None:
            # (a next file already queued for gapless playback: reload to be exact)
            self._open_track(idx, local)
        elif self._pending_seek is not None:
            self._pending_seek = local
        else:
            self._do_seek(local)
        if self.output is not None:
            self.output.on_seek()
            self._output_anchor = pos
        self._last_pos = pos
        self.emit("seeked", pos)
        self.emit("position-changed", pos)
        self._check_chapter(pos)

    def skip(self, seconds):
        self.seek(self.position + seconds)

    def next_chapter(self):
        if not self.book or not self.book.chapters:
            return
        i = self.chapter_index
        if i + 1 < len(self.book.chapters):
            self.seek(self.book.chapters[i + 1].start)

    def previous_chapter(self):
        if not self.book or not self.book.chapters:
            return
        pos = self.position
        i = self.chapter_index
        ch = self.book.chapters[i]
        # Like a CD player: restart the chapter unless we're right at its start.
        if pos - ch.start > 3 or i == 0:
            self.seek(ch.start)
        else:
            self.seek(self.book.chapters[i - 1].start)

    def set_rate(self, rate):
        rate = round(max(MIN_RATE, min(MAX_RATE, rate)), 2)
        if rate == self._rate:
            return
        pos = self.position
        self._rate = rate
        if self.remote is not None:
            self.remote.set_rate(rate)
        elif self.book and self._pending_seek is None and not self._at_end:
            self._do_seek(pos - self.book.tracks[self.track_index].offset)
            if self.output is not None:
                self.output.on_seek()      # hear the new speed now, not after the queued audio
        self.emit("rate-changed", rate)

    def set_effects(self, settings: EffectSettings):
        self._effects = settings
        self.fx.apply(settings)
        if self.remote is not None and getattr(self.remote, "supports_effects", False):
            self.remote.set_effects(settings)   # stream devices: re-encode with the new settings

    @property
    def can_skip_silence(self):
        return self.fx.silence is not None

    def set_skip_silence(self, on):
        self.fx.set_skip_silence(on)

    def set_fade(self, value):
        self.fx.set_fade(value)
        if self.remote is not None:
            self.remote.set_fade(value)
        if self.output is not None:
            self.output.set_fade(value)

    # --------------------------------------------------------------- casting
    @property
    def casting(self):
        return self.remote is not None or self.output is not None

    @property
    def cast_name(self):
        target = self.remote or self.output
        return target.name if target else None

    @property
    def cast_supports_effects(self):
        if self.output is not None:
            return True           # our own pipeline still does all the processing
        return getattr(self.remote, "supports_effects", False) if self.remote else True

    def start_output(self, session):
        """Keep playing here, but send the audio into ``session.make_sink()`` (AirPlay via OwnTone)."""
        if self.remote is not None:
            self.stop_casting(resume_local=False)
        if self.output is not None:
            self.stop_output(restart=False)
        pos = self.position
        self.pipeline.set_state(Gst.State.NULL)
        self.output = session
        self._output_anchor = pos
        self.pipeline.set_property("audio-sink", session.make_sink())
        self._reopen(pos)
        if self._want_playing:
            session.on_play()
        if hasattr(session, "attach"):
            session.attach(self)
        self.emit("casting-changed")
        self.emit("state-changed")

    def stop_output(self, restart=True):
        """Back to this computer's speakers at the same position."""
        if self.output is None:
            return
        pos = self.position
        session, self.output = self.output, None
        self.pipeline.set_state(Gst.State.NULL)
        default = Gst.parse_bin_from_description(self._sink_desc, True) if self._sink_desc else None
        self.pipeline.set_property("audio-sink", default)
        session.stop()
        if restart:
            self._reopen(pos)
        self.emit("casting-changed")
        self.emit("state-changed")

    def _reopen(self, pos):
        self._pending_seek = None
        self._gapless_next = None
        if self.book:
            idx, local = self.book.locate(min(pos, max(0.0, self.book.duration - 0.5)))
            self._last_pos = pos
            self._open_track(idx, local)
        self._update_tick()

    def start_casting(self, session):
        """Hand playback to a Chromecast, continuing from the current position."""
        if self.remote is not None:
            self.stop_casting(resume_local=False)
        pos, playing = self.position, self._want_playing
        self.pipeline.set_state(Gst.State.NULL)
        self._pending_seek = None
        self._gapless_next = None
        self.remote = session
        session.on_status = self._on_remote_status
        if self.book:
            self._remote_load(pos, playing)
        self.emit("casting-changed")
        self.emit("state-changed")

    def stop_casting(self, resume_local=True):
        """Back to this computer's speakers at the same position."""
        if self.remote is None:
            return
        pos, playing = self.position, self._want_playing
        session, self.remote = self.remote, None
        session.on_status = None
        session.stop()
        if self.book and resume_local:
            self._want_playing = playing
            idx, local = self.book.locate(min(pos, self.book.duration - 0.5))
            self._last_pos = pos
            self._open_track(idx, local)
        self._update_tick()
        self.emit("casting-changed")
        self.emit("state-changed")

    def _remote_load(self, pos, playing):
        idx, local = self.book.locate(pos)
        self.track_index = idx
        self._remote_local, self._remote_waiting = local, True
        self._at_end = False
        self._last_pos = pos
        self.remote.load(self.book, self.book.tracks[idx], local, playing, self._rate, self._effects)

    def _on_remote_status(self, status):
        if self.remote is None or not self.book:
            return
        state, reason = status.player_state, status.idle_reason
        if state in ("PLAYING", "PAUSED", "BUFFERING"):
            self._remote_waiting = False
        if state == "IDLE" and reason == "FINISHED" and not self._remote_waiting:
            if self.track_index + 1 < len(self.book.tracks):
                self._remote_load(self.book.tracks[self.track_index + 1].offset, True)
            else:
                self._on_eos()
            return
        if state == "IDLE" and reason == "ERROR":
            self._want_playing = False
            self._update_tick()
            self.emit("state-changed")
            self.emit("error", getattr(status, "message", None) or "The cast device couldn't play this file")
            GLib.idle_add(lambda: (self.stop_casting(), False)[1])
            return
        # paused/resumed from the TV remote or another phone: follow along
        if state == "PLAYING" and not self._want_playing and not self._remote_waiting:
            self._want_playing = True
            self._update_tick()
            self.emit("state-changed")
        elif state == "PAUSED" and self._want_playing and not self._remote_waiting:
            self._want_playing = False
            self._update_tick()
            self.emit("state-changed")

    # ------------------------------------------------------------ internals
    def _on_about_to_finish(self, _playbin):
        # Runs on a streaming thread; setting "uri" here is what playbin expects.
        book, idx = self.book, self.track_index
        if book is None or self._pending_seek is not None or idx + 1 >= len(book.tracks):
            return
        self._gapless_next = idx + 1
        self.pipeline.set_property("uri", Gst.filename_to_uri(book.tracks[idx + 1].path))

    def _on_stream_start(self):
        nxt = self._gapless_next
        if nxt is None:
            return
        self._gapless_next = None
        self.track_index = nxt
        self._last_pos = self.book.tracks[nxt].offset
        if self._rate != 1.0:
            # a new stream starts at normal speed; re-apply ours
            self._do_seek(0.0)

    def _open_track(self, idx, local):
        self._gapless_next = None
        self.track_index = idx
        track = self.book.tracks[idx]
        self.pipeline.set_state(Gst.State.READY)
        self.pipeline.set_property("uri", Gst.filename_to_uri(track.path))
        self._pending_seek = local
        self.pipeline.set_state(Gst.State.PAUSED)

    def _do_seek(self, local):
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
        ok = self.pipeline.seek(self._rate, Gst.Format.TIME, flags,
                                Gst.SeekType.SET, int(max(0.0, local) * Gst.SECOND),
                                Gst.SeekType.NONE, -1)
        if not ok:
            log.debug("seek to %.2f refused", local)

    def _on_message(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.ELEMENT:
            s = msg.get_structure()
            if s is not None and s.get_name() == "level":
                self.fx.handle_level(s)
        elif t == Gst.MessageType.ASYNC_DONE:
            if self._pending_seek is not None:
                local = self._pending_seek
                self._pending_seek = None
                if local > 0.05 or self._rate != 1.0:
                    self._do_seek(local)
            if self._want_playing:
                self.pipeline.set_state(Gst.State.PLAYING)
        elif t == Gst.MessageType.STREAM_START:
            self._on_stream_start()
        elif t == Gst.MessageType.EOS:
            self._on_eos()
        elif t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.error("playback error: %s (%s)", err.message, debug)
            self._want_playing = False
            self.pipeline.set_state(Gst.State.PAUSED)
            self._update_tick()
            self.emit("state-changed")
            self.emit("error", err.message)
        elif t == Gst.MessageType.WARNING:
            w, debug = msg.parse_warning()
            log.warning("gstreamer: %s (%s)", w.message, debug)

    def _on_eos(self):
        if not self.book:
            return
        if self.track_index + 1 < len(self.book.tracks):
            self._open_track(self.track_index + 1, 0.0)
            return
        log.info("finished book %s", self.book.title)
        self._want_playing = False
        self._at_end = True
        self._last_pos = self.book.duration
        if self.remote is None:
            self.pipeline.set_state(Gst.State.PAUSED)
        self._update_tick()
        self.emit("state-changed")
        self.emit("position-changed", self.book.duration)
        self.emit("book-finished")

    def _update_tick(self):
        if self._want_playing and not self._tick_id:
            self._tick_id = GLib.timeout_add(250, self._tick)
        elif not self._want_playing and self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0

    def _tick(self):
        if not self._want_playing or not self.book:
            self._tick_id = 0
            return False
        pos = self.position
        self.emit("position-changed", pos)
        self._check_chapter(pos)
        return True

    def _check_chapter(self, pos):
        i = self.book.chapter_index(pos) if self.book else -1
        if i != self._chapter:
            self._chapter = i
            self.emit("chapter-changed", i)

    def shutdown(self):
        if self.output is not None:
            self.stop_output(restart=False)
        if self.remote is not None:
            self.remote.stop()   # our media server goes away with us
            self.remote = None
        self.pipeline.set_state(Gst.State.NULL)
