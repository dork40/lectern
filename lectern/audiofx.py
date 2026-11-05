"""Audio processing chain: tempo, EQ, loudness boost, night mode and limiter.

Preferred chain (needs gst-plugins-bad's LADSPA bridge + swh-plugins)::

    audioconvert ! scaletempo ! audioconvert ! F32 stereo caps ! equalizer-10bands
      ! level ! volume(dyn) ! SC4 compressor ! fast look-ahead limiter ! volume(fade) ! audioconvert

The boost drives the look-ahead limiter (input gain up to +20 dB, ceiling
-1 dBFS), so it really gets louder without clipping; the SC4 compressor in
front evens out narration so the limiter doesn't pump. Night mode uses the
same compressor with stronger settings plus the slow AGC below.

Fallback chain when those plugins are missing::

    ... ! level ! volume(dyn) ! audioamplify(boost) ! rglimiter ! volume(fade) ! ...

Here ``GainController`` also keeps peaks from running far past full scale
and ``rglimiter`` soft-limits the rest.

``level`` measures the signal before the gain stages; ``GainController``
turns those measurements into a smoothed gain for ``volume(dyn)`` (the slow
night-mode AGC that lifts quiet passages and tames loud ones).
"""
import array
import logging
import math
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

log = logging.getLogger(__name__)

# equalizer-10bands centre frequencies: 29 59 119 237 474 947 1889 3770 7523 15011 Hz
VOICE_CLARITY_BANDS = (-9.0, -6.0, -2.0, 0.0, 0.0, 0.5, 2.5, 4.0, 1.5, 0.0)
LEVEL_INTERVAL_NS = 50_000_000

FX_RATE = 44100                # processing rate for the LADSPA stages
LADSPA_LIMITER = "ladspa-fast-lookahead-limiter-1913-so-fastlookaheadlimiter"
LADSPA_COMPRESSOR = "ladspa-sc4-1882-so-sc4"
MAX_DRIVE_DB = 20.0           # the look-ahead limiter's input gain range
FALLBACK_MAX_DRIVE_DB = 9.0   # rglimiter only soft-clips; don't push it further
FALLBACK_CEILING_DB = 3.0     # let peaks overshoot this much into rglimiter


def boost_amount(boost):
    """Slider value 1.0-3.0 (100-300%) -> 0..1 on a loudness (log) scale."""
    return min(1.0, math.log2(max(1.0, boost)) / math.log2(3.0))


def drive_db(boost, max_db=MAX_DRIVE_DB):
    """Gain driven into the limiter: 0 dB at 100%, ~12.6 dB at 200%, max at 300%."""
    return max_db * boost_amount(boost)


@dataclass
class EffectSettings:
    boost: float = 1.0          # 1.0 .. 3.0 (100% .. 300% on the slider)
    voice_clarity: bool = False
    night_mode: bool = False

    @property
    def active(self):
        return self.boost > 1.001 or self.voice_clarity or self.night_mode

    def to_dict(self):
        return {"boost": self.boost, "voice_clarity": self.voice_clarity, "night_mode": self.night_mode}

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(boost=min(3.0, max(1.0, float(d.get("boost", 1.0)))),
                   voice_clarity=bool(d.get("voice_clarity", False)),
                   night_mode=bool(d.get("night_mode", False)))


class GainController:
    """Envelope-following gain computer (pure Python, driven by level messages)."""

    TARGET_RMS_DB = -18.0     # night mode loudness target
    MAX_LIFT_DB = 12.0
    MAX_CUT_DB = 12.0
    SILENCE_DB = -50.0        # don't pump up silence / room noise

    def __init__(self):
        self.gain_db = 0.0
        self.settings = EffectSettings()
        self.post_gain_db = 0.0   # gain applied after this stage (fallback boost)
        self.ceiling_db = None    # None: a real limiter follows, no peak pull-back

    def reset(self):
        self.gain_db = 0.0

    def desired_gain_db(self, rms_db, peak_db):
        s = self.settings
        desired = 0.0
        if s.night_mode and rms_db > self.SILENCE_DB:
            desired = max(-self.MAX_CUT_DB, min(self.MAX_LIFT_DB, self.TARGET_RMS_DB - rms_db))
        elif s.night_mode:
            desired = self.gain_db  # hold through pauses
        if self.ceiling_db is not None:
            predicted_peak = peak_db + self.post_gain_db + desired + (2.0 if s.voice_clarity else 0.0)
            if predicted_peak > self.ceiling_db:
                desired -= predicted_peak - self.ceiling_db
        return desired

    def update(self, rms_db, peak_db, dt):
        desired = self.desired_gain_db(rms_db, peak_db)
        # fast attack when the gain has to drop, slow release when it rises
        tau = 0.03 if desired < self.gain_db else (1.5 if self.settings.night_mode else 0.6)
        self.gain_db += (desired - self.gain_db) * (1 - math.exp(-dt / tau))
        return 10 ** (self.gain_db / 20)


def _make(factory, name=None):
    el = Gst.ElementFactory.make(factory, name)
    if el is None:
        log.info("GStreamer element %s is not available", factory)
    return el


def ladspa_available():
    return all(Gst.ElementFactory.find(f) is not None for f in (LADSPA_LIMITER, LADSPA_COMPRESSOR))


class SilenceSkipper:
    """Shortens long pauses in the narration (the "skip silence" switch).

    GStreamer's ``removesilence`` only handles mono and its voice detector
    chops quiet speech, so this measures the loudness of each S16 buffer
    itself. Once the signal has stayed below ``THRESHOLD_DB`` for longer than
    ``MAX_PAUSE``, further silent buffers are dropped and the src pad's
    running-time offset is pulled back by the same amount, so playback
    carries on without a gap. Reset on every new segment (seeks, gapless
    file changes).
    """

    THRESHOLD_DB = -40.0
    MAX_PAUSE = 0.4   # seconds of silence that are kept

    def __init__(self, element):
        self.enabled = False
        self._src = element.get_static_pad("src")
        self._sink = element.get_static_pad("sink")
        self._limit = 10 ** (self.THRESHOLD_DB / 20) * 32768
        self._reset()
        self._sink.add_probe(Gst.PadProbeType.BUFFER, self._buffer_probe)
        self._sink.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._event_probe)

    def _reset(self):
        self._run = 0.0     # seconds of continuous silence so far
        self._shift = 0     # ns removed since the last segment
        self._src.set_offset(0)

    def _event_probe(self, _pad, info):
        if info.get_event().type in (Gst.EventType.SEGMENT, Gst.EventType.FLUSH_STOP):
            self._reset()
        return Gst.PadProbeReturn.OK

    def _buffer_probe(self, pad, info):
        if not self.enabled:
            self._run = 0.0
            return Gst.PadProbeReturn.OK
        buf = info.get_buffer()
        caps = pad.get_current_caps()
        if caps is None or buf.get_size() == 0:
            return Gst.PadProbeReturn.OK
        st = caps.get_structure(0)
        ok, rate = st.get_int("rate")
        ok2, channels = st.get_int("channels")
        if not (ok and ok2) or st.get_string("format") != "S16LE":
            return Gst.PadProbeReturn.OK
        ok, mapped = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        try:
            samples = array.array("h")
            samples.frombytes(mapped.data[: len(mapped.data) // 2 * 2])
        finally:
            buf.unmap(mapped)
        seen = samples[::7]
        rms = math.sqrt(sum(x * x for x in seen) / len(seen)) if seen else 0.0
        seconds = len(samples) / (rate * channels)
        if rms >= self._limit:
            self._run = 0.0
            return Gst.PadProbeReturn.OK
        self._run += seconds
        if self._run <= self.MAX_PAUSE:
            return Gst.PadProbeReturn.OK
        self._shift += int(seconds * Gst.SECOND)
        self._src.set_offset(-self._shift)
        return Gst.PadProbeReturn.DROP


class EffectsChain:
    def __init__(self, use_ladspa=None):
        self.bin = Gst.Bin.new("lectern-fx")
        self.controller = GainController()
        self.pro = ladspa_available() if use_ladspa is None else use_ladspa
        self.s16 = self.tempo = self.caps = self.eq = self.level = self.dyn = self.silence = None
        self.boost = self.limiter = self.compressor = self.fade = None
        if self.pro:
            stages = (("ladspa", LADSPA_COMPRESSOR, "compressor"), ("ladspa", LADSPA_LIMITER, "limiter"))
            # A fixed rate: GStreamer's LADSPA wrapper can end up with a different
            # rate on each side (a 22 kHz book then played 2x fast on AirPlay).
            caps = f"audio/x-raw,format=F32LE,rate={FX_RATE},channels=2,layout=interleaved"
        else:
            log.warning("swh-plugins (LADSPA) not found: using the basic boost/limiter")
            stages = (("", "audioamplify", "boost"), ("", "rglimiter", "limiter"))
            caps = "audio/x-raw,format=F32LE"
        layout = [("audioconvert", None), ("capsfilter", "s16"), ("identity", "silence"), ("audioconvert", None),
                  ("scaletempo", "tempo"), ("audioconvert", None), ("audioresample", None),
                  ("capsfilter", "caps"), ("equalizer-10bands", "eq"), ("level", "level"),
                  ("volume", "dyn"), *((f, n) for _, f, n in stages),
                  ("volume", "fade"), ("audioconvert", None)]
        chain = []
        for factory, name in layout:
            el = _make(factory, name)
            if el is None:
                continue
            chain.append(el)
            if name:
                setattr(self, name, el)
        self.has_pitch_correction = self.tempo is not None
        self.skipper = None
        if self.s16 is not None and self.silence is not None:
            self.s16.set_property("caps", Gst.Caps.from_string("audio/x-raw,format=S16LE,layout=interleaved"))
            self.skipper = SilenceSkipper(self.silence)
        if self.caps is not None:
            self.caps.set_property("caps", Gst.Caps.from_string(caps))
        if self.level is not None:
            self.level.set_property("interval", LEVEL_INTERVAL_NS)
        if self.boost is not None:
            self.boost.set_property("clipping-method", "none")
        if self.pro:
            self.limiter.set_property("limit", -1.0)
            self.limiter.set_property("release-time", 0.12)
        for el in chain:
            self.bin.add(el)
        for a, b in zip(chain, chain[1:]):
            if not a.link(b):
                log.error("cannot link %s -> %s", a.get_name(), b.get_name())
        self.bin.add_pad(Gst.GhostPad.new("sink", chain[0].get_static_pad("sink")))
        self.bin.add_pad(Gst.GhostPad.new("src", chain[-1].get_static_pad("src")))
        self.apply(EffectSettings())

    def apply(self, settings: EffectSettings):
        self.controller.settings = settings
        if self.eq is not None:
            gains = VOICE_CLARITY_BANDS if settings.voice_clarity else (0.0,) * 10
            for i, g in enumerate(gains):
                self.eq.set_property(f"band{i}", g)
        if self.pro:
            self._apply_pro(settings)
        else:
            drive = drive_db(settings.boost, FALLBACK_MAX_DRIVE_DB)
            if self.boost is not None:
                self.boost.set_property("amplification", 10 ** (drive / 20))
            if self.limiter is not None:
                self.limiter.set_property("enabled", settings.active)
            self.controller.post_gain_db = drive
            self.controller.ceiling_db = FALLBACK_CEILING_DB if drive > 0 else -1.0
        if self.level is not None:
            self.level.set_property("post-messages", settings.night_mode or (not self.pro and settings.active))
        if not settings.night_mode and (self.pro or not settings.active):
            self.controller.reset()
            if self.dyn is not None:
                self.dyn.set_property("volume", 1.0)

    def _apply_pro(self, settings):
        """Calibrated on real narration (quiet LibriVox and mastered audio):
        300% -> +19 dB on a quiet recording, +10 dB on a mastered one, peaks at -1 dBFS.
        """
        self.controller.post_gain_db = 0.0
        self.controller.ceiling_db = None
        t = boost_amount(settings.boost)
        drive = MAX_DRIVE_DB * t
        if t > 0:
            params = {"threshold-level": -18.0 - 8.0 * t, "ratio": 1.5 + 2.5 * t, "makeup-gain": 12.0 * t,
                      "attack-time": 5.0, "release-time": 150.0, "knee-radius": 6.0, "rms-peak": 0.3}
        else:
            params = {"ratio": 1.0, "makeup-gain": 0.0}
        if settings.night_mode:
            # stronger, slower compression that evens out the narration
            params.update({"threshold-level": -30.0, "ratio": max(3.0, params["ratio"]),
                           "makeup-gain": max(8.0, params["makeup-gain"]), "attack-time": 10.0,
                           "release-time": 300.0, "knee-radius": 6.0, "rms-peak": 0.5})
            drive = max(drive, 3.0)
        for k, v in params.items():
            self.compressor.set_property(k, v)
        self.limiter.set_property("input-gain", min(MAX_DRIVE_DB, drive))

    def handle_level(self, structure):
        """Feed a 'level' element message; returns True if consumed."""
        if self.dyn is None:
            return True
        try:
            rms = max(structure.get_value("rms"))
            peak = max(structure.get_value("peak"))
        except (TypeError, ValueError):
            return True
        gain = self.controller.update(rms, peak, LEVEL_INTERVAL_NS / 1e9)
        self.dyn.set_property("volume", min(gain, 10.0))
        return True

    def set_skip_silence(self, on):
        if self.skipper is not None:
            self.skipper.enabled = bool(on)

    def set_fade(self, value):
        if self.fade is not None:
            self.fade.set_property("volume", max(0.0, min(1.0, value)))
