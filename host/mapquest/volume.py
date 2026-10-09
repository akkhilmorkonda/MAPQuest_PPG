"""Live blood-volume tracking (D34).

The streaming counterpart of the offline return-to-flow pipeline (D10, SPEC
section 5): raw counts -> dark subtraction -> low-pass -> delta-OD against a
reference -> two-wavelength blood volume -> rate of change. Same constants,
with the changes running live forces:

* **Causal low-pass.** 0.3 Hz, as offline, but causal: it cannot see the
  future, so the trace lags reality by roughly 0.75 s. Fine for watching; the
  offline pipeline remains the one to measure onset timing with (D8).
* **Reference set by the operator.** Offline, the reference is the last 3 s of
  occlusion. Live, it is the last 3 s before "Zero" is pressed, or set
  automatically 10 s after connecting or re-tuning, once the filter settles.
* **Re-tuning interrupts, never silently continues.** Changing LED current
  changes counts with no change in blood, so delta-OD across a re-tune is
  meaningless. The history is kept, a gap and a marker are inserted, and the
  baseline re-zeroes after settling.

Quantities are *relative* micrometres (D26): blood volume x an unknown optical
path length. Positive means more blood than at the reference.

Both two-wavelength and IR-only volume are tracked side by side, along with
each channel's percent change, because which to trust is open question O4:
red is the contact-sensitive channel.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy import signal

from .pipeline import blood_volume

LP_HZ = 0.3               # offline pipeline's corner (SPEC 5.1 step 7)
DECIM_HZ = 10.0           # history resolution
REF_S = 3.0               # reference window (SPEC 5.1 step 4)
AUTO_ZERO_AFTER_S = 10.0  # let the low-pass settle before zeroing
RATE_WINDOW_S = 2.0       # slope span, matching the offline 2 s derivative (D9)
FLOOR_WINDOW_S = 10.0     # quiet period after zeroing used for the noise floor
K_SIGMA = 3.0             # onset threshold, as SPEC 5.1 step 10
HOLD_S = 2.0              # sustain time, as SPEC 5.1 step 10
MOTION_PCT_S = 2.0        # same floor as D27, on the low-passed channels
DRIFT_WINDOW_S = 60.0


@dataclass
class VolumeState:
    t: float
    dv_um: float | None
    """Two-wavelength blood volume vs the reference, relative um."""

    dv_ir_um: float | None
    pct_ir: float | None
    pct_red: float | None
    rate_um_s: float | None
    mu0: float | None
    sigma0: float | None
    trend: str
    """NO BASELINE, SETTLING, MOTION, FILLING, DRAINING or STABLE."""

    reason: str
    baseline_age_s: float | None
    baseline_auto: bool
    drift_um_min: float | None
    red_ir_ratio: float | None
    """How much more red's level wanders than IR's (O4). ~1 is ideal."""

    @property
    def threshold(self) -> float | None:
        if self.mu0 is None or self.sigma0 is None:
            return None
        return self.mu0 + K_SIGMA * self.sigma0


class VolumeTracker:
    """Streaming blood-volume tracker. See the module docstring."""

    def __init__(self, fs: float, dark: dict[str, float] | None = None,
                 history_s: float = 900.0) -> None:
        self.fs = float(fs)
        self.dark = dict(dark or {})
        self.sos = signal.butter(2, LP_HZ / (self.fs / 2.0), btype="low",
                                 output="sos")
        self.decim = max(int(round(self.fs / DECIM_HZ)), 1)
        n_hist = int(history_s * DECIM_HZ)
        self.t: deque[float] = deque(maxlen=n_hist)
        self.dv: deque[float] = deque(maxlen=n_hist)
        self.dv_ir: deque[float] = deque(maxlen=n_hist)
        self.pct_ir: deque[float] = deque(maxlen=n_hist)
        self.pct_red: deque[float] = deque(maxlen=n_hist)
        self.rate: deque[float] = deque(maxlen=n_hist)
        self.motion: deque[bool] = deque(maxlen=n_hist)
        self.events: list[tuple[float, str]] = []
        self.n_total = 0

        self._zi = None
        self._lp_recent: deque[np.ndarray] = deque(maxlen=int(REF_S * self.fs))
        self._samples_since_interrupt = 0
        self.ref: np.ndarray | None = None
        self.ref_t: float | None = None
        self.ref_auto = False
        self._auto_zero_at = AUTO_ZERO_AFTER_S
        self.mu0: float | None = None
        self.sigma0: float | None = None
        self._n_marks = 0

    @property
    def t_now(self) -> float:
        return self.n_total / self.fs

    # -- control ----------------------------------------------------------

    def interrupt(self, label: str) -> None:
        """The optics changed (LED re-tune, reconnect): break the trace.

        Keeps the history, so 'over time' survives, but drops the filter state
        and the reference -- delta-OD across the change would be fiction.
        """
        if self._samples_since_interrupt == 0 and self.events and \
                self.events[-1][1] == label:
            return                      # repeated resets while disconnected
        self._zi = None
        self._lp_recent.clear()
        self._samples_since_interrupt = 0
        self.ref = None
        self.ref_t = None
        self.mu0 = self.sigma0 = None
        self._auto_zero_at = self.t_now + AUTO_ZERO_AFTER_S
        self.events.append((self.t_now, label))
        self._append_gap()

    def zero(self, auto: bool = False) -> bool:
        """Set the reference to the mean of the last 3 s. False if too soon."""
        if len(self._lp_recent) < int(REF_S * self.fs):
            return False
        self.ref = np.mean(np.asarray(self._lp_recent), axis=0)
        self.ref_t = self.t_now
        self.ref_auto = auto
        self.mu0 = self.sigma0 = None
        self.events.append((self.t_now, "zero (auto)" if auto else "zero"))
        return True

    def mark(self, label: str | None = None) -> str:
        self._n_marks += 1
        text = label or f"mark {self._n_marks}"
        self.events.append((self.t_now, text))
        return text

    # -- streaming ----------------------------------------------------------

    def push(self, ir, red=None) -> None:
        ir = np.asarray(ir, dtype=float).ravel() - self.dark.get("ir", 0.0)
        if red is None:
            x = ir[:, None]
        else:
            red = np.asarray(red, dtype=float).ravel() - self.dark.get("red", 0.0)
            x = np.column_stack([ir, red])
        if x.shape[0] == 0:
            return
        if self._zi is None or self._zi.shape[2] != x.shape[1]:
            self._zi = signal.sosfilt_zi(self.sos)[:, :, None] * x[0][None, None, :]
        y, self._zi = signal.sosfilt(self.sos, x, axis=0, zi=self._zi)

        # Step sample by sample. Auto-zero and decimation both key off the
        # *absolute* sample index, so how the stream happens to be chunked can
        # never change where the reference is taken or which samples land in
        # the history. (Checking only at block ends shifted the auto-zero by up
        # to one block.)
        for row in y:
            self.n_total += 1
            self._samples_since_interrupt += 1
            self._lp_recent.append(row)
            if self.ref is None and self.t_now >= self._auto_zero_at - 1e-9:
                self.zero(auto=True)
            if self.n_total % self.decim == 0:
                self._append(self.t_now, row)

    def _append_gap(self) -> None:
        nan = float("nan")
        self.t.append(self.t_now)
        for d in (self.dv, self.dv_ir, self.pct_ir, self.pct_red, self.rate):
            d.append(nan)
        self.motion.append(False)

    def _append(self, t: float, lp: np.ndarray) -> None:
        nan = float("nan")
        if self.ref is None or np.any(lp <= 0) or np.any(self.ref <= 0):
            dv = dv_ir = p_ir = p_red = nan
        else:
            d_od = {"ir": np.log(self.ref[0] / lp[0])}
            p_ir = (lp[0] / self.ref[0] - 1.0) * 100.0
            p_red = nan
            if len(lp) > 1:
                d_od["red"] = np.log(self.ref[1] / lp[1])
                p_red = (lp[1] / self.ref[1] - 1.0) * 100.0
            dv_ir = float(blood_volume(d_od, ir_only=True))
            dv = float(blood_volume(d_od, ir_only=False)) if "red" in d_od else dv_ir

        self.t.append(t)
        self.dv.append(dv)
        self.dv_ir.append(dv_ir)
        self.pct_ir.append(p_ir)
        self.pct_red.append(p_red)

        # Rate: least-squares slope over the last 2 s of the history, the live
        # analogue of the offline 2 s derivative. Lags by half the window.
        n = int(RATE_WINDOW_S * DECIM_HZ)
        rate = nan
        if len(self.dv) >= n:
            yv = np.array(list(self.dv)[-n:])
            if np.isfinite(yv).all():
                tv = np.array(list(self.t)[-n:])
                rate = float(np.polyfit(tv - tv[-1], yv, 1)[0])
        self.rate.append(rate)

        # Motion: either channel's low-passed level changing faster than D27's
        # floor. Low-passing first keeps a strong fingertip pulse from tripping
        # it (its raw slope alone reaches ~8 %/s).
        moving = False
        k = int(0.5 * DECIM_HZ)
        if len(self.pct_ir) > k:
            for series in (self.pct_ir, self.pct_red):
                a, b = series[-1], series[-1 - k]
                if np.isfinite(a) and np.isfinite(b) and abs(a - b) / 0.5 > MOTION_PCT_S:
                    moving = True
        self.motion.append(moving)

        # Noise floor: frozen once 10 s of quiet data after the zero exist.
        if (self.mu0 is None and self.ref_t is not None
                and t >= self.ref_t + FLOOR_WINDOW_S):
            ts = np.array(self.t)
            rs = np.array(self.rate)
            mv = np.array(self.motion)
            sel = (ts >= self.ref_t) & np.isfinite(rs) & ~mv
            if sel.sum() >= int(FLOOR_WINDOW_S * DECIM_HZ * 0.5):
                self.mu0 = float(np.mean(rs[sel]))
                self.sigma0 = float(np.std(rs[sel]))

    # -- state ----------------------------------------------------------------

    def arrays(self) -> dict[str, np.ndarray]:
        return {"t": np.array(self.t), "dv": np.array(self.dv),
                "dv_ir": np.array(self.dv_ir), "pct_ir": np.array(self.pct_ir),
                "pct_red": np.array(self.pct_red), "rate": np.array(self.rate),
                "motion": np.array(self.motion, dtype=bool)}

    def state(self) -> VolumeState:
        t = self.t_now

        def last(d):
            return None if not d or not np.isfinite(d[-1]) else float(d[-1])

        dv, dv_ir = last(self.dv), last(self.dv_ir)
        p_ir, p_red, rate = last(self.pct_ir), last(self.pct_red), last(self.rate)
        age = None if self.ref_t is None else t - self.ref_t

        drift = ratio = None
        if self.ref_t is not None and len(self.t) > 20:
            a = self.arrays()
            sel = (a["t"] >= max(self.ref_t, t - DRIFT_WINDOW_S)) & np.isfinite(a["dv"]) \
                & ~a["motion"]
            if sel.sum() >= 20:
                drift = float(np.polyfit(a["t"][sel], a["dv"][sel], 1)[0] * 60.0)
                si, sr = np.std(a["pct_ir"][sel]), np.std(a["pct_red"][sel])
                if si > 0 and np.isfinite(sr):
                    ratio = float(sr / si)

        def make(trend, reason):
            return VolumeState(t, dv, dv_ir, p_ir, p_red, rate, self.mu0,
                               self.sigma0, trend, reason, age, self.ref_auto,
                               drift, ratio)

        if self.ref is None:
            wait = max(0.0, self._auto_zero_at - t)
            return make("NO BASELINE", f"auto-zero in {wait:.0f} s — hold still")
        if self.motion and self.motion[-1]:
            return make("MOTION", "level moving fast — sensor or arm moving")
        if self.mu0 is None:
            return make("SETTLING", "measuring the noise floor (10 s after zero)")

        n = int(HOLD_S * DECIM_HZ)
        recent = np.array(list(self.rate)[-n:])
        thr_hi = self.mu0 + K_SIGMA * self.sigma0
        thr_lo = self.mu0 - K_SIGMA * self.sigma0
        if len(recent) == n and np.isfinite(recent).all():
            if np.all(recent > thr_hi):
                return make("FILLING", f"rising faster than {thr_hi:+.2f} um/s for {HOLD_S:g} s")
            if np.all(recent < thr_lo):
                return make("DRAINING", f"falling faster than {thr_lo:+.2f} um/s for {HOLD_S:g} s")
        return make("STABLE", "within the noise floor")
