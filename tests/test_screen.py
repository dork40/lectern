from lectern.screen import KdeScreen, ScreenControl


class FakeScreen:
    name = "fake"

    def __init__(self):
        self.dark = False

    def off(self):
        self.dark = True

    def is_off(self):
        return self.dark

    def woke(self):
        pass


class FakeKeyboard:
    available = True

    def __init__(self):
        self.level, self.saved = 3, None

    def off(self):
        self.saved, self.level = self.level, 0

    def restore(self):
        self.level = self.saved


def make(screen):
    sc = ScreenControl.__new__(ScreenControl)
    from gi.repository import GObject
    GObject.Object.__init__(sc)
    sc.screen, sc.keyboard, sc.off, sc._poll = screen, FakeKeyboard(), False, 0
    return sc


def test_stray_pointer_event_does_not_wake_on_detectable_compositors():
    sc = make(FakeScreen())
    sc.off = True
    sc._do_off()
    sc.user_active()                    # e.g. the pointer event Hyprland sends on DPMS off
    assert sc.off and sc.keyboard.level == 0
    sc.screen.dark = False              # the compositor reports the screen is back
    assert sc._check_wake() is False
    assert not sc.off and sc.keyboard.level == 3


def test_kde_wakes_on_input_in_window():
    kde = KdeScreen.__new__(KdeScreen)
    kde._off = False
    kde.off = lambda: setattr(kde, "_off", True)
    sc = make(kde)
    sc.off = True
    sc._do_off()
    sc.user_active()
    assert not sc.off and sc.keyboard.level == 3
