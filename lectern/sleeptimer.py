"""Sleep timer.

``TimerLogic`` is pure (injectable clock) and unit tested; ``SleepTimer`` binds
it to the player: it only counts down while audio plays, fades the volume out
at the end, then pauses and rewinds a little.
"""
import logging
import time

from gi.repository import GLib, GObject

log = logging.getLogger(__name__)

PRESETS = (5, 10, 15, 30, 45, 60)
EXTEND_SECONDS = 5 * 60


class TimerLogic:
    DURATION = "duration"
    CHAPTER = "chapter"

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.mode = None
        self.fade_seconds = 0.0
        self._deadline_left = 0.0      # seconds left when last paused (duration mode)
        self._started_at = None        # clock() when last resumed, None while paused
        self.chapter_index = -1

    @property
    def active(self):
        return self.mode is not None

    def start_duration(self, seconds, fade_seconds=0.0, running=True):
        self.mode = self.DURATION
        self.fade_seconds = max(0.0, fade_seconds)
        self._deadline_left = max(0.0, float(seconds))
        self._started_at = self.clock() if running else None

    def start_end_of_chapter(self, chapter_index, fade_seconds=0.0):
        self.mode = self.CHAPTER
        self.fade_seconds = max(0.0, fade_seconds)
        self.chapter_index = chapter_index
        self._started_at = None

    def cancel(self):
        self.mode = None
        self._started_at = None

    def pause(self):
        if self.mode == self.DURATION and self._started_at is not None:
            self._deadline_left = self._left_now()
            self._started_at = None

    def resume(self):
        if self.mode == self.DURATION and self._started_at is None:
            self._started_at = self.clock()

    def _left_now(self):
        if self._started_at is None:
            return self._deadline_left
        return self._deadline_left - (self.clock() - self._started_at)

    def remaining(self, chapter_left=None):
        """Seconds of real time left, or None if inactive.

        ``chapter_left`` is the real time (already divided by the playback
        rate) until the current chapter ends; needed in chapter mode.
        """
        if self.mode == self.DURATION:
            return max(0.0, self._left_now())
        if self.mode == self.CHAPTER:
            return max(0.0, chapter_left or 0.0)
        return None

    def extend(self, seconds=EXTEND_SECONDS, chapter_left=None):
        """Add time. A chapter timer becomes a plain countdown from its current remaining time."""
        if self.mode is None:
            return
        left = self.remaining(chapter_left) or 0.0
        running = self.mode == self.CHAPTER or self._started_at is not None
        self.start_duration(left + seconds, self.fade_seconds, running=running)

    def fade_level(self, remaining):
        """Volume multiplier (0..1) for the fade-out."""
        if remaining is None or self.fade_seconds <= 0 or remaining >= self.fade_seconds:
            return 1.0
        x = max(0.0, remaining / self.fade_seconds)
        return x * x  # perceptually smoother than a linear ramp

    def expired(self, chapter_left=None, chapter_index=None):
        if self.mode == self.DURATION:
            return self._left_now() <= 0
        if self.mode == self.CHAPTER:
            if chapter_index is not None and chapter_index != self.chapter_index:
                return True
            return chapter_left is not None and chapter_left <= 0.25
        return False


class SleepTimer(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "tick": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "expired": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, player, settings):
        super().__init__()
        self.player = player
        self.settings = settings   # callable(key) -> value
        self.logic = TimerLogic()
        self._source = 0
        player.connect("state-changed", self._on_state)

    @property
    def active(self):
        return self.logic.active

    @property
    def mode(self):
        return self.logic.mode

    def _fade_seconds(self):
        return float(self.settings("sleep_fade_seconds")) if self.settings("sleep_fade") else 0.0

    def _chapter_left(self):
        ch = self.player.chapter
        if ch is None:
            return None
        return max(0.0, ch.end - self.player.position) / max(self.player.rate, 0.01)

    def start_minutes(self, minutes):
        self.start_seconds(minutes * 60)

    def start_seconds(self, seconds):
        self.logic.start_duration(seconds, self._fade_seconds(), running=self.player.playing)
        log.info("sleep timer: %d min %d s", seconds // 60, seconds % 60)
        self._started()

    def start_end_of_chapter(self):
        self.logic.start_end_of_chapter(self.player.chapter_index, self._fade_seconds())
        log.info("sleep timer: end of chapter %d", self.player.chapter_index)
        self._started()

    def extend(self):
        self.logic.extend(EXTEND_SECONDS, self._chapter_left())
        self.player.set_fade(1.0)
        self.emit("changed")
        self._tick()

    def cancel(self):
        self.logic.cancel()
        self.player.set_fade(1.0)
        self._stop_ticking()
        self.emit("changed")

    def remaining(self):
        return self.logic.remaining(self._chapter_left())

    def _started(self):
        if not self._source:
            self._source = GLib.timeout_add(250, self._tick)
        self.emit("changed")
        self._tick()

    def _stop_ticking(self):
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0

    def _on_state(self, player):
        if not self.logic.active:
            return
        if player.playing:
            self.logic.resume()
        else:
            self.logic.pause()
            player.set_fade(1.0)
        self.emit("changed")

    def _tick(self):
        if not self.logic.active:
            self._source = 0
            return False
        chapter_left = self._chapter_left()
        if self.player.playing:
            remaining = self.logic.remaining(chapter_left)
            if self.logic.expired(chapter_left, self.player.chapter_index):
                self._expire()
                return False
            self.player.set_fade(self.logic.fade_level(remaining))
        self.emit("tick", self.logic.remaining(chapter_left) or 0.0)
        return True

    def _expire(self):
        log.info("sleep timer expired")
        mode, chapter = self.logic.mode, self.logic.chapter_index
        self.logic.cancel()
        self._source = 0
        self.player.pause()
        book = self.player.book
        if mode == TimerLogic.CHAPTER and book and 0 <= chapter < len(book.chapters):
            self.player.seek(book.chapters[chapter].end)  # stop exactly at the boundary
        rewind = float(self.settings("sleep_rewind_seconds") or 0)
        if rewind > 0:
            self.player.skip(-rewind)
        self.player.set_fade(1.0)
        self.emit("changed")
        self.emit("expired")
