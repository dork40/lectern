import pytest

from lectern.audiofx import FALLBACK_MAX_DRIVE_DB, MAX_DRIVE_DB, EffectSettings, GainController, drive_db


def settle(ctrl, rms, peak, seconds=10.0):
    gain = 1.0
    for _ in range(int(seconds / 0.05)):
        gain = ctrl.update(rms, peak, 0.05)
    return gain


def test_unity_when_inactive():
    c = GainController()
    assert settle(c, -20, -6) == 1.0


def test_drive_curve():
    assert drive_db(1.0) == 0
    assert drive_db(2.0) == pytest.approx(12.6, abs=0.1)
    assert drive_db(3.0) == MAX_DRIVE_DB
    assert drive_db(9.0) == MAX_DRIVE_DB            # clamped
    assert drive_db(3.0, FALLBACK_MAX_DRIVE_DB) == FALLBACK_MAX_DRIVE_DB
    values = [drive_db(1 + i / 10) for i in range(21)]
    assert values == sorted(values)


def test_limiter_mode_never_pulls_boost_back():
    c = GainController()                            # ceiling None: a real limiter follows
    c.settings = EffectSettings(boost=3.0)
    assert settle(c, -18, -1) == 1.0


def test_fallback_pulls_back_peaks_to_ceiling():
    c = GainController()
    c.settings = EffectSettings(boost=3.0)
    c.post_gain_db, c.ceiling_db = 9.0, 3.0         # basic chain: amplify then soft limiter
    gain = settle(c, -18, -3)                       # peaks would reach +6 dBFS
    assert c.gain_db == pytest.approx(-3.0, abs=0.2)
    assert 0 < gain < 1


def test_night_mode_lifts_quiet_and_tames_loud():
    c = GainController()
    c.settings = EffectSettings(night_mode=True)
    assert settle(c, -35, -25) > 1.5                # quiet narration lifted
    c.reset()
    assert settle(c, -8, -2) < 0.5                  # loud passage reduced


def test_night_mode_holds_through_silence():
    c = GainController()
    c.settings = EffectSettings(night_mode=True)
    settle(c, -35, -25)
    before = c.gain_db
    settle(c, -80, -70, seconds=3)                  # pause between sentences
    assert abs(c.gain_db - before) < 0.5


def test_settings_roundtrip_and_clamp():
    s = EffectSettings.from_dict({"boost": 9, "voice_clarity": 1})
    assert s.boost == 3.0 and s.voice_clarity and not s.night_mode
    assert EffectSettings.from_dict(s.to_dict()) == s


def test_effects_keep_speed_for_22khz_books_into_a_44khz_sink():
    """A 22.05 kHz book through the effects chain into a fixed 44.1 kHz sink (AirPlay)
    must come out as long as it went in: the LADSPA stages once relabelled it as
    44.1 kHz, and the TV played the book twice as fast."""
    from gi.repository import Gst
    from lectern.audiofx import EffectsChain
    Gst.init(None)
    fx = EffectsChain()
    pipe = Gst.Pipeline.new()
    src = Gst.parse_bin_from_description(
        "audiotestsrc num-buffers=220 samplesperbuffer=1050 ! capsfilter caps=audio/x-raw,rate=22050,channels=2", True)
    out = Gst.parse_bin_from_description(
        "audioconvert ! audioresample ! capsfilter caps=audio/x-raw,format=S16LE,rate=44100,channels=2 ! "
        "appsink name=s sync=false emit-signals=true", True)
    for el in (src, fx.bin, out):
        pipe.add(el)
    src.link(fx.bin)
    fx.bin.link(out)
    got = [0]

    def on_sample(sink):
        got[0] += sink.emit("pull-sample").get_buffer().get_size()
        return Gst.FlowReturn.OK
    out.get_by_name("s").connect("new-sample", on_sample)
    pipe.set_state(Gst.State.PLAYING)
    pipe.get_bus().timed_pop_filtered(20 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipe.set_state(Gst.State.NULL)
    seconds_in = 220 * 1050 / 22050
    assert abs(got[0] / (44100 * 4) - seconds_in) < 0.1
