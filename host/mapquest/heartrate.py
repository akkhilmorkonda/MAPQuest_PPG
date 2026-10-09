"""Real-time heart rate from PPG.

A streaming estimator: feed it raw counts in blocks as they arrive, ask it for
the current state whenever you like. It follows the standard PPG heart-rate
pipeline, with two safeguards that turned out to be necessary (D33):

1. **Band-pass 0.5-4 Hz** (30-240 bpm), 4th-order Butterworth, *causal*, with
   filter state carried between blocks so block boundaries are invisible.
   Zero-phase filtering (used offline, D8) needs the future and cannot run live.
2. **Invert.** In reflectance PPG more blood means fewer counts, so systole is
   a trough in the raw signal. Inverting makes each beat a positive peak.
3. **Pulse SNR gate.** In-band power against the white-noise floor measured
   above 10 Hz. Below 3, no peaks are accepted: a detector whose threshold is
   relative to the signal will find "beats" in pure noise, and those would
   poison the interval history.
4. **Peak detection** with a 0.3 s refractory period (200 bpm ceiling) and a
   prominence threshold that adapts to the recent pulse amplitude. A peak is
   accepted only once it is old enough for its prominence to be settled, and
   its time is refined below one sample by parabolic interpolation.
5. **Interval gating.** Intervals outside 0.3-2.0 s are discarded; once a
   rhythm is established, so is any interval more than 30% from the running
   median, rejecting missed and doubled beats. A run of rejections means the
   rhythm really changed, so the history resets and re-acquires.
6. **Heart rate = 60 / median of recent good intervals.**
7. **Spectral heart rate** over the last 12 s, as an independent check. Its
   confidence is *spectral concentration* -- the fraction of in-band power
   within 0.15 Hz of the peak -- not peak prominence. On a 12 s window Welch
   averages only a few segments, its dips are deep and random, and prominence
   measured against them passed pure noise 85% of the time even at 10 dB.
   Concentration separates them: noise never exceeded 0.30.
8. **Signal quality.** No contact, saturation and motion outrank everything;
   then GOOD requires the beat and spectral estimates to agree.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy import signal

FULL_SCALE = (1 << 18) - 1

BAND_HZ = (0.5, 4.0)
MIN_BPM, MAX_BPM = 30.0, 200.0
IBI_MIN_S, IBI_MAX_S = 60.0 / MAX_BPM, 60.0 / MIN_BPM

CONFIRM_S = 0.6          # a peak must be this old before it is accepted
IBI_TOLERANCE = 0.30     # max fractional deviation from the running median
IBI_MEDIAN_N = 9         # intervals in the running median
REJECT_RESET = 4         # consecutive rejections that trigger re-acquisition
MIN_GOOD_BEATS = 4       # intervals needed before a heart rate is reported

SNR_MIN = 3.0            # pulse SNR below which beats are not timed
SNR_WINDOW_S = 5.0

SPECTRAL_WINDOW_S = 12.0
SPECTRAL_HALF_HZ = 0.15
CONC_AGREE = 0.35        # concentration needed to corroborate the beat estimate
CONC_ALONE = 0.60        # concentration needed to report spectral HR on its own
AGREE_BPM = 8.0
CONFIRM_RUN_S = 3      # corroboration must persist this many seconds

NO_CONTACT_FRAC = 0.05
SATURATED_FRAC = 0.98
MOTION_BASELINE_PCT_S = 2.0   # same floor as D27, applied to the baseline
MOTION_AMP_RATIO = 3.0

QUALITY_COLORS = {
    "GOOD": "#0a7d2c",
    "LOW": "#d08700",
    "SEARCHING": "#6b7280",
    "MOTION": "#6b7280",
    "SATURATED": "#c43b2f",
    "NO_CONTACT": "#c43b2f",
}


@dataclass
class HRState:
    """Snapshot of the estimator, safe to read from a GUI."""

    t: float
    hr_display: float | None
    """What to show: beat-based if available, else spectral if confident."""

    hr_source: str | None
    """'beats', 'spectrum' or None."""

    hr_beats: float | None
    hr_spectral: float | None
    spectral_conc: float | None
    quality: str
    """GOOD, LOW, SEARCHING, MOTION, SATURATED or NO_CONTACT."""

    reason: str
    perfusion_pct: float
    pulse_snr: float
    dc_frac: float
    rmssd_ms: float | None
    beats_10s: int
    ibi_cv: float | None


def pulse_snr(raw: np.ndarray, fs: float) -> float:
    """In-band (0.5-4 Hz) rms against the white-noise floor.

    The floor is measured above 10 Hz, where a pulse has almost no energy,
    then scaled to the pulse band's width. Pure noise reads ~1.3; a fingertip
    pulse ~50; the real 2026-10-09 wrist capture ~10.
    """
    if len(raw) < int(fs * 2):
        return 0.0
    nyq = fs / 2.0
    hp = signal.butter(2, min(10.0, nyq * 0.8) / nyq, btype="high", output="sos")
    bp = signal.butter(2, [BAND_HZ[0] / nyq, min(BAND_HZ[1], nyq * 0.9) / nyq],
                       btype="band", output="sos")
    x = raw - np.mean(raw)
    noise_hp = float(np.std(signal.sosfiltfilt(hp, x)))
    noise_bp = noise_hp * np.sqrt((BAND_HZ[1] - BAND_HZ[0]) / max(nyq - 10.0, 1.0))
    return float(np.std(signal.sosfiltfilt(bp, x)) / max(noise_bp, 1e-9))


class HeartRateEstimator:
    """Streaming heart-rate estimator. See the module docstring."""

    def __init__(self, fs: float, buffer_s: float = 12.0,
                 full_scale: int = FULL_SCALE) -> None:
        self.fs = float(fs)
        self.buffer_s = buffer_s
        self.full_scale = full_scale
        nyq = self.fs / 2.0
        lo, hi = BAND_HZ[0] / nyq, min(BAND_HZ[1], nyq * 0.9) / nyq
        self.sos = signal.butter(2, [lo, hi], btype="band", output="sos")  # 4th order
        self.sos_lp = signal.butter(2, 0.4 / nyq, btype="low", output="sos")
        self.reset()

    # -- streaming ----------------------------------------------------------

    def reset(self) -> None:
        """Forget everything. Call after the optics change (LED tune, re-seat)."""
        n = int(self.buffer_s * self.fs)
        self._zi = None
        self._zi_lp = None
        self.raw: deque[float] = deque(maxlen=n)
        self.filt: deque[float] = deque(maxlen=n)
        self.base: deque[float] = deque(maxlen=n)
        self.n_total = 0
        self.last_peak_t: float | None = None
        self.beats: deque[tuple[float, bool]] = deque(maxlen=800)
        self.ibis: deque[tuple[float, float, bool]] = deque(maxlen=400)
        self._reject_run = 0
        self.snr = 0.0
        self.history: list[HRState] = []
        self._next_history_t = 1.0
        self._run = 0           # consecutive corroborated seconds
        self._spec_t = -np.inf
        self._spec: tuple[float | None, float | None] = (None, None)
        self.spectrum: tuple[np.ndarray, np.ndarray] | None = None

    def push(self, x) -> None:
        """Feed a block of raw counts, oldest first."""
        x = np.asarray(x, dtype=float).ravel()
        if x.size == 0:
            return
        if self._zi is None:
            # Start at steady state for the first sample, so the opening
            # seconds are not a filter transient.
            self._zi = signal.sosfilt_zi(self.sos) * x[0]
            self._zi_lp = signal.sosfilt_zi(self.sos_lp) * x[0]
        y, self._zi = signal.sosfilt(self.sos, x, zi=self._zi)
        b, self._zi_lp = signal.sosfilt(self.sos_lp, x, zi=self._zi_lp)
        self.raw.extend(x)
        self.filt.extend(-y)          # inverted: each beat a positive peak
        self.base.extend(b)
        self.n_total += x.size

        if self.t_now - self._spec_t >= 1.0:
            self._spectral()
            n = int(self.fs * SNR_WINDOW_S)
            self.snr = pulse_snr(np.fromiter(self.raw, dtype=float)[-n:], self.fs)
            self._spec_t = self.t_now
        self._detect_peaks()
        while self.t_now >= self._next_history_t:
            st = self._raw_state()
            self._run = self._run + 1 if st.hr_display is not None else 0
            self.history.append(self._confirmed(st))
            self._next_history_t += 1.0

    @property
    def t_now(self) -> float:
        return self.n_total / self.fs

    def buffer_times(self) -> np.ndarray:
        start = self.n_total - len(self.filt)
        return (np.arange(len(self.filt)) + start) / self.fs

    # -- beats ----------------------------------------------------------------

    def _detect_peaks(self) -> None:
        n = len(self.filt)
        if n < int(self.fs * 3):
            return
        if self.snr < SNR_MIN:
            # Too weak to time beats. Also forget the last peak, so the first
            # interval after the signal returns is not measured across a gap.
            self.last_peak_t = None
            return
        f = np.fromiter(self.filt, dtype=float, count=n)
        recent = f[-int(self.fs * 5):]
        amp = float(np.percentile(recent, 95) - np.percentile(recent, 5))
        if amp <= 0:
            return
        start = self.n_total - n
        peaks, _ = signal.find_peaks(
            f, distance=max(int(self.fs * IBI_MIN_S), 1), prominence=0.35 * amp
        )
        confirm_before = n - int(self.fs * CONFIRM_S)
        for i in peaks:
            if i < 1 or i >= n - 1 or i >= confirm_before:
                continue
            y0, y1, y2 = f[i - 1], f[i], f[i + 1]
            denom = y0 - 2.0 * y1 + y2
            off = 0.5 * (y0 - y2) / denom if denom != 0 else 0.0
            t = (start + i + float(np.clip(off, -0.5, 0.5))) / self.fs
            # Peaks already handled reappear on every call; skip them.
            if self.last_peak_t is not None and t < self.last_peak_t + 0.2:
                continue
            self._accept(t)

    def _accept(self, t: float) -> None:
        if self.last_peak_t is None:
            self.last_peak_t = t
            self.beats.append((t, True))
            return
        ibi = t - self.last_peak_t
        self.last_peak_t = t
        ok = IBI_MIN_S <= ibi <= IBI_MAX_S
        good = [v for _t, v, a in self.ibis if a][-IBI_MEDIAN_N:]
        if ok and len(good) >= 3:
            med = float(np.median(good))
            ok = abs(ibi - med) <= IBI_TOLERANCE * med
        if ok:
            self._reject_run = 0
        else:
            self._reject_run += 1
            if self._reject_run >= REJECT_RESET:
                # Either the rhythm really changed or we had locked onto
                # something wrong. Start again rather than defend a stale median.
                self.ibis.clear()
                self._reject_run = 0
        self.ibis.append((t, ibi, ok))
        self.beats.append((t, ok))

    # -- spectrum ---------------------------------------------------------

    def _spectral(self) -> None:
        n_win = int(self.fs * SPECTRAL_WINDOW_S)
        if len(self.raw) < int(self.fs * 8):
            self._spec = (None, None)
            return
        # Raw and detrended, never band-passed: filtered white noise takes on
        # the filter's in-band hump, which reads as a spurious peak.
        x = signal.detrend(np.fromiter(self.raw, dtype=float)[-n_win:])
        nper = int(self.fs * 4)
        freqs, pxx = signal.welch(x, fs=self.fs, nperseg=nper,
                                  noverlap=nper // 2, nfft=4096)
        self.spectrum = (freqs, pxx)
        band = (freqs >= BAND_HZ[0]) & (freqs <= MAX_BPM / 60.0)
        fb, pb = freqs[band], pxx[band]
        peaks, _ = signal.find_peaks(pb)
        if not len(peaks) or pb.sum() <= 0:
            self._spec = (None, None)
            return
        i = int(peaks[np.argmax(pb[peaks])])
        near = np.abs(fb - fb[i]) <= SPECTRAL_HALF_HZ
        conc = float(pb[near].sum() / pb.sum())
        hz = float(fb[i])
        if 0 < i < len(pb) - 1:
            y0, y1, y2 = np.log(pb[i - 1:i + 2] + 1e-30)
            d = y0 - 2 * y1 + y2
            if d != 0:
                hz += 0.5 * (y0 - y2) / d * (fb[1] - fb[0])
        self._spec = (hz * 60.0, conc)

    # -- state ----------------------------------------------------------------

    def rmssd_ms(self, window_s: float = 60.0) -> float | None:
        """RMSSD over consecutive good intervals in the window.

        Only pairs of *adjacent* accepted intervals count: a difference across
        a rejected beat would mix two unrelated intervals and inflate it.
        """
        cutoff = self.t_now - window_s
        seq = [(v, a) for t, v, a in self.ibis if t >= cutoff]
        diffs = [seq[j][0] - seq[j - 1][0] for j in range(1, len(seq))
                 if seq[j][1] and seq[j - 1][1]]
        if len(diffs) < 10:
            return None
        return float(np.sqrt(np.mean(np.square(diffs))) * 1000.0)

    def state(self) -> HRState:
        """Current state, with the persistence requirement applied."""
        return self._confirmed(self._raw_state())

    def _confirmed(self, st: HRState) -> HRState:
        # Isolated seconds of corroboration happen by chance when drift lines
        # up with a beat estimate; requiring a short run removes them, at the
        # cost of the brief "confirming" delay every HR monitor has.
        if st.hr_display is None or self._run >= CONFIRM_RUN_S:
            return st
        return HRState(st.t, None, None, st.hr_beats, st.hr_spectral,
                       st.spectral_conc, "SEARCHING",
                       f"confirming ({self._run}/{CONFIRM_RUN_S} s)",
                       st.perfusion_pct, st.pulse_snr, st.dc_frac, st.rmssd_ms,
                       st.beats_10s, st.ibi_cv)

    def _raw_state(self) -> HRState:
        t = self.t_now
        n1 = int(self.fs)
        if len(self.raw) < n1:
            return HRState(t, None, None, None, None, None, "SEARCHING",
                           "starting up", 0.0, 0.0, 0.0, None, 0, None)

        raw = np.fromiter(self.raw, dtype=float)
        filt = np.fromiter(self.filt, dtype=float)
        base = np.fromiter(self.base, dtype=float)
        last1 = raw[-n1:]
        dc = float(np.mean(last1))
        dc_frac = dc / self.full_scale
        n5 = int(self.fs * 5)
        dc5 = float(np.mean(raw[-n5:]))
        perfusion = (float(np.sqrt(np.mean(filt[-n5:] ** 2)) / dc5 * 100.0)
                     if dc5 > 0 else 0.0)

        good = [v for _t, v, a in self.ibis if a][-IBI_MEDIAN_N:]
        recent_beat = bool(self.beats) and (t - self.beats[-1][0]) <= 3.0
        hr_beats = (60.0 / float(np.median(good))
                    if len(good) >= MIN_GOOD_BEATS and recent_beat
                    and self.snr >= SNR_MIN else None)
        cv = (float(np.std(good) / np.mean(good))
              if len(good) >= MIN_GOOD_BEATS else None)
        beats_10s = sum(1 for bt, ok in self.beats if ok and bt >= t - 10.0)
        hr_spec, conc = self._spec
        rmssd = self.rmssd_ms()

        def make(q, why, shown=None, source=None):
            return HRState(t, shown, source, hr_beats, hr_spec, conc, q, why,
                           perfusion, self.snr, dc_frac, rmssd, beats_10s, cv)

        # A physical fault outranks anything the signal says.
        if dc_frac < NO_CONTACT_FRAC:
            return make("NO_CONTACT", "no tissue in front of the sensor")
        if float(np.max(last1)) >= SATURATED_FRAC * self.full_scale:
            return make("SATURATED", "ADC at full scale — lower LED current")

        n2 = int(self.fs * 2)
        if len(base) >= n2:
            seg = base[-n2:]
            rate = (float(np.max(seg) - np.min(seg)) / 2.0) / max(dc, 1.0) * 100.0
            if rate > MOTION_BASELINE_PCT_S:
                return make("MOTION", "baseline moving — hold still")
        n10 = (int(self.fs * 10) // 5) * 5
        if len(filt) >= n10:
            blocks = filt[-n10:].reshape(5, -1)
            amps = np.percentile(blocks, 95, axis=1) - np.percentile(blocks, 5, axis=1)
            ref = float(np.median(amps[:-1]))
            if ref > 0 and amps[-1] > MOTION_AMP_RATIO * ref:
                return make("MOTION", "pulse amplitude jumped — hold still")

        # A number is only shown when two independent estimates corroborate
        # it. Beat timing alone is not enough: slow drift reaches into the
        # pulse band, raises the SNR, and its wiggles time as "beats". Shown
        # uncorroborated, drift produced a heart rate in 310 of 390 seconds of
        # a pulse-free recording.
        spec_ok = hr_spec is not None and conc is not None
        if hr_beats is not None:
            if not spec_ok or conc < CONC_AGREE:
                return make("SEARCHING", "beats found but no rhythm in the "
                                         "spectrum to confirm them")
            if abs(hr_beats - hr_spec) > AGREE_BPM:
                return make("SEARCHING", f"unconfirmed — beats ({hr_beats:.0f}) "
                                         f"and spectrum ({hr_spec:.0f}) disagree")
            if cv is not None and cv > 0.15:
                return make("LOW", "intervals irregular", hr_beats, "beats")
            return make("GOOD", "beats and spectrum agree", hr_beats, "beats")

        if spec_ok and conc >= CONC_ALONE:
            return make("LOW", "spectral estimate only — beats too weak to time",
                        hr_spec, "spectrum")
        if self.snr < SNR_MIN:
            return make("SEARCHING", f"pulse too weak (SNR {self.snr:.1f})")
        return make("SEARCHING", "locking onto beats")
