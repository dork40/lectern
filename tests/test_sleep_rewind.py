"""SleepTimer's end-of-timer rewind: a few seconds back, or back to where the timer started."""
from gi.repository import GObject

from lectern.sleeptimer import SleepTimer


class FakePlayer(GObject.Object):
    __gsignals__ = {"state-changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__()
        self.book, self.position, self.playing, self.rate = object(), 600.0, True, 1.0
        self.chapter, self.chapter_index = None, 0

    def skip(self, s):
        self.position = max(0.0, self.position + s)

    def seek(self, p):
        self.position = p

    def pause(self):
        self.playing = False

    def set_fade(self, _level):
        pass


def make(**settings):
    base = {"sleep_fade": False, "sleep_fade_seconds": 30, "sleep_rewind_seconds": 30, "sleep_rewind_whole": False}
    base.update(settings)
    player = FakePlayer()
    return player, SleepTimer(player, base.get)


def test_rewinds_seconds_by_default():
    player, timer = make()
    timer.start_seconds(900)
    player.position = 1500.0              # 15 minutes later
    timer._expire()
    assert player.position == 1470.0 and not player.playing


def test_whole_timer_goes_back_to_start():
    player, timer = make(sleep_rewind_whole=True)
    timer.start_seconds(900)
    timer.extend()                         # changing the timer keeps the original start
    player.position = 1800.0
    timer._expire()
    assert player.position == 600.0


def test_whole_timer_falls_back_when_the_book_changed():
    player, timer = make(sleep_rewind_whole=True)
    timer.start_seconds(900)
    player.book, player.position = object(), 100.0
    timer._expire()
    assert player.position == 70.0


def test_cancel_forgets_the_start():
    player, timer = make(sleep_rewind_whole=True)
    timer.start_seconds(900)
    timer.cancel()
    player.position = 2000.0
    timer.start_seconds(60)
    player.position = 2060.0
    timer._expire()
    assert player.position == 2000.0
