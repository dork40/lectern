import pytest

from lectern.sleeptimer import TimerLogic


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock():
    return Clock()


def test_inactive_by_default(clock):
    t = TimerLogic(clock)
    assert not t.active
    assert t.remaining() is None
    assert not t.expired()


def test_duration_counts_down_and_expires(clock):
    t = TimerLogic(clock)
    t.start_duration(600)
    clock.advance(120)
    assert t.remaining() == pytest.approx(480)
    assert not t.expired()
    clock.advance(480)
    assert t.remaining() == 0
    assert t.expired()


def test_pause_freezes_countdown(clock):
    t = TimerLogic(clock)
    t.start_duration(300)
    clock.advance(100)
    t.pause()
    clock.advance(1000)          # paused: time does not count
    assert t.remaining() == pytest.approx(200)
    t.resume()
    clock.advance(50)
    assert t.remaining() == pytest.approx(150)


def test_start_while_paused_waits_for_playback(clock):
    t = TimerLogic(clock)
    t.start_duration(60, running=False)
    clock.advance(500)
    assert t.remaining() == pytest.approx(60)
    t.resume()
    clock.advance(10)
    assert t.remaining() == pytest.approx(50)


def test_extend_adds_five_minutes(clock):
    t = TimerLogic(clock)
    t.start_duration(60)
    clock.advance(30)
    t.extend()
    assert t.remaining() == pytest.approx(330)


def test_extend_keeps_paused_state(clock):
    t = TimerLogic(clock)
    t.start_duration(60)
    t.pause()
    t.extend(60)
    clock.advance(100)
    assert t.remaining() == pytest.approx(120)


def test_cancel(clock):
    t = TimerLogic(clock)
    t.start_duration(60)
    t.cancel()
    assert not t.active and t.remaining() is None and not t.expired()


def test_fade_curve(clock):
    t = TimerLogic(clock)
    t.start_duration(120, fade_seconds=30)
    assert t.fade_level(120) == 1.0
    assert t.fade_level(30) == 1.0
    assert t.fade_level(15) == pytest.approx(0.25)   # quadratic: (15/30)^2
    assert t.fade_level(0) == 0.0
    levels = [t.fade_level(r) for r in range(30, -1, -1)]
    assert levels == sorted(levels, reverse=True)    # monotonic


def test_fade_disabled(clock):
    t = TimerLogic(clock)
    t.start_duration(120, fade_seconds=0)
    assert t.fade_level(1) == 1.0


def test_end_of_chapter(clock):
    t = TimerLogic(clock)
    t.start_end_of_chapter(chapter_index=3, fade_seconds=10)
    assert t.remaining(chapter_left=95) == 95
    assert not t.expired(chapter_left=95, chapter_index=3)
    assert t.expired(chapter_left=0.1, chapter_index=3)
    assert t.expired(chapter_left=300, chapter_index=4)     # chapter changed (e.g. seek past it)
    assert t.fade_level(t.remaining(chapter_left=5)) == pytest.approx(0.25)


def test_extend_chapter_timer_becomes_countdown(clock):
    t = TimerLogic(clock)
    t.start_end_of_chapter(chapter_index=0)
    t.extend(300, chapter_left=40)
    assert t.mode == TimerLogic.DURATION
    assert t.remaining() == pytest.approx(340)
