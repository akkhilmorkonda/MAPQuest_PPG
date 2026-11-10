"""Forward model for simulated return-to-flow runs (non-pulsatile study, phase 1).

``host/synth.py`` starts the fill when the cuff crosses ``--map``, so it
cannot tell us whether onset reads MAP: the answer is built in. Here the
onset pressure is an OUTPUT. Arterial pressure is a waveform, blood crosses
the cuff whenever that waveform is above cuff pressure, and the pulse decides
when that first happens. One set of equations covers a healthy subject (flow
returns at systolic) and an LVAD patient with no pulse (flow returns at MAP).

Lumped model, volume in the optical unit dV*L (relative um) so the pipeline's
output can be compared with it directly:

    P_art(t) --[artery under the cuff]--> distal artery --[arterioles]-->
    veins --[vein under the cuff]--> central veins

    distal artery   c_art * dP_d/dt = Q_cuff - Q_arteriole
    veins           c_ven * dP_v/dt = Q_arteriole - Q_vout
    Q_arteriole     (P_d - P_v) / r_art
    Q_cuff          g_cuff * open(P_art - P_cuff) * (P_art - max(P_d, P_cuff))
    Q_vout          g_ven * open_v(P_v - P_cuff) * (P_v - cvp)

The vessels under the cuff are Starling resistors: closed while the cuff is
above the pressure inside them, flow-limited by the cuff while it sits between
their upstream and downstream pressures, and opening further as the transmural
pressure grows (``open_span``). Tissue blood volume is c_art*P_d + c_ven*P_v.

The parameters are placeholders to be fitted to the real pulsatile runs in
phase 2. ``open_span``, ``open_power`` and ``g_cuff * r_art`` set how softly
the refill starts below MAP, which is the main thing a non-pulsatile result
will depend on.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal

from mapquest.pipeline import COEFF_IR_880, COEFF_RED_660

SIM_FS = 1000.0     # integration rate; decimated to the recording rate
SETTLE_S = 60.0     # pre-roll with the cuff off, discarded, so the record starts at rest
HM3_PERIOD_S = 2.0  # HeartMate 3 artificial pulse: once every 2 s


@dataclass
class Patient:
    map_mmhg: float = 85.0
    pulse_pressure: float = 0.0
    """Native pulse pressure, mmHg (SBP - DBP on ejecting beats). 0 = no pulse."""
    hr_bpm: float = 75.0
    pulse_every: int = 1
    """Only every n-th beat ejects: an LVAD patient whose aortic valve opens
    intermittently. The other beats leave arterial pressure flat."""
    hm3_pp_mmhg: float = 0.0
    """HeartMate 3 artificial pulse, peak-to-peak mmHg. 0 = off."""
    resp_mmhg: float = 0.0
    resp_hz: float = 0.25


@dataclass
class Cuff:
    pre_s: float = 20.0
    """Cuff off before inflation: the pulse-return heart-rate estimate needs it."""
    inflate_s: float = 2.0
    hold_mmhg: float = 160.0
    hold_s: float = 25.0
    deflate_rate: float = 2.0
    """mmHg/s, linear bleed."""
    dump_mmhg: float = 30.0
    """The valve opens fully here, as the automated monitor does."""
    dump_rate: float = 40.0
    post_s: float = 20.0
    noise_mmhg: float = 0.3


@dataclass
class Vessels:
    g_cuff: float = 1.0
    """Artery under the cuff, fully open, um/s per mmHg."""
    open_span: float = 20.0
    """Transmural pressure, mmHg, over which the cuffed artery opens fully."""
    open_power: float = 1.0
    """open(x) = clip(x / open_span, 0, 1) ** open_power."""
    open_offset: float = 0.0
    """Pressure above the cuff before the artery opens at all, mmHg."""
    r_art: float = 13.0
    """Arteriolar resistance, mmHg per um/s. Sets resting inflow ~6 um/s."""
    c_art: float = 0.3
    """Distal arterial compliance, um/mmHg. Sets the optical pulse size."""
    c_ven: float = 5.0
    """Venous compliance, um/mmHg. ~150 um of refill per 30 mmHg."""
    g_ven: float = 1.2
    ven_span: float = 5.0
    cvp: float = 5.0
    drain_rate: float = 0.0
    """Volume lost under occlusion (O5), um/s at inflation; negative."""
    drain_tau: float = 0.0
    """The drain slows with this time constant, s (0 = constant)."""


@dataclass
class Sensor:
    fs: float = 100.0
    base_ir: float = 106_000.0
    base_red: float = 136_000.0
    dark_ir: float = 8.0
    dark_red: float = 13.0
    noise_counts: float = 40.0
    od_ratio: float = 0.35
    """dOD_red / dOD_ir for well-oxygenated blood, as in synth.py."""
    lf_noise_um: float = 0.0
    """Slow dV*L fluctuation not caused by the cuff, um standard deviation."""
    lf_band: tuple[float, float] = (0.02, 0.5)


@dataclass
class SimRun:
    t: np.ndarray
    """Session time at the recording rate, s."""
    ir: np.ndarray
    red: np.ndarray
    p_mmhg: np.ndarray
    """Cuff pressure as the sensor reads it (with noise)."""
    p_true: np.ndarray
    p_art: np.ndarray
    q_cuff: np.ndarray
    """Inflow past the cuff, um/s."""
    dv: np.ndarray
    """True tissue blood volume change, dV*L um."""
    truth: dict
    patient: Patient
    cuff: Cuff
    vessels: Vessels
    sensor: Sensor


def beat_shape(phase: np.ndarray) -> np.ndarray:
    """One arterial beat on [0, 1): systolic peak, dicrotic wave, run-off.
    Periodic and smooth, scaled to 0..1."""
    def raw(ph):
        def bump(c, w):
            return np.exp(-(((ph - c + 0.5) % 1.0 - 0.5) / w) ** 2)
        return bump(0.15, 0.08) + 0.30 * bump(0.42, 0.06) + 0.25 * bump(0.55, 0.25)
    # Scale from a fixed grid, so a short or partial beat still spans 0..1 correctly.
    grid = raw(np.arange(0, 1, 1e-3))
    return (raw(phase) - grid.min()) / (grid.max() - grid.min())


def hm3_pulse(t: np.ndarray) -> np.ndarray:
    """HeartMate 3 artificial pulse as arterial pressure, zero-mean, unit
    peak-to-peak. Every 2 s the pump drops 2000 rpm for 0.15 s, then runs
    2000 rpm above its set speed for 0.2 s; the arterial tree smooths the
    speed steps (first order, 0.15 s)."""
    ph = t % HM3_PERIOD_S
    u = np.where(ph < 0.15, -1.0, np.where(ph < 0.35, 1.0, 0.0))
    a = np.exp(-1.0 / (SIM_FS * 0.15))
    y = signal.lfilter([1 - a], [1, -a], u)
    y = y - y.mean()
    return y / (y.max() - y.min())


def arterial_pressure(t: np.ndarray, pt: Patient) -> np.ndarray:
    p = np.zeros_like(t)
    if pt.pulse_pressure > 0:
        beats = t * pt.hr_bpm / 60.0
        g = beat_shape(beats % 1.0)
        if pt.pulse_every > 1:
            g = np.where(np.floor(beats).astype(int) % pt.pulse_every == 0, g, 0.0)
        p += pt.pulse_pressure * g
    if pt.hm3_pp_mmhg > 0:
        p += pt.hm3_pp_mmhg * hm3_pulse(t)
    if pt.resp_mmhg > 0:
        p += pt.resp_mmhg * np.sin(2 * np.pi * pt.resp_hz * t)
    return p - p.mean() + pt.map_mmhg


def cuff_pressure(t: np.ndarray, c: Cuff) -> tuple[np.ndarray, dict]:
    t_inf = c.pre_s
    t_top = t_inf + c.inflate_s
    t_defl = t_top + c.hold_s
    t_dump = t_defl + (c.hold_mmhg - c.dump_mmhg) / c.deflate_rate
    t_off = t_dump + c.dump_mmhg / c.dump_rate
    p = np.interp(t, [t_inf, t_top, t_defl, t_dump, t_off],
                  [0.0, c.hold_mmhg, c.hold_mmhg, c.dump_mmhg, 0.0], left=0.0, right=0.0)
    return p, {"inflation_s": t_inf, "hold_start_s": t_top, "deflation_s": t_defl,
               "dump_s": t_dump, "release_s": t_off, "end_s": t_off + c.post_s}


def _integrate(p_art, p_cuff, v: Vessels):
    """Explicit Euler at SIM_FS. Returns P_d, P_v and Q_cuff per step."""
    dt = 1.0 / SIM_FS
    n = len(p_art)
    pd_out = np.empty(n)
    pv_out = np.empty(n)
    q_out = np.empty(n)
    span, power, off = v.open_span, v.open_power, v.open_offset
    g, r, ca, cv = v.g_cuff, v.r_art, v.c_art, v.c_ven
    gv, vspan, cvp = v.g_ven, v.ven_span, v.cvp
    pa_l, pc_l = p_art.tolist(), p_cuff.tolist()
    pv = cvp + 5.0
    pd = pa_l[0] - 5.0
    for i in range(n):
        pa, pc = pa_l[i], pc_l[i]
        if pa >= pd:
            x = pa - pc - off
            if x > 0:
                o = x / span if span > 0 else 1.0
                o = 1.0 if o > 1.0 else o ** power
                q = g * o * (pa - (pd if pd > pc + off else pc + off))
            else:
                q = 0.0
        else:
            # Backflow once the cuff is low enough that the artery is open from the far side.
            x = pd - pc - off
            if x > 0:
                o = x / span if span > 0 else 1.0
                o = 1.0 if o > 1.0 else o ** power
                q = g * o * (pa - pd)
            else:
                q = 0.0
        qa = (pd - pv) / r
        y = pv - pc
        if y > 0 and pv > cvp:
            ov = y / vspan if vspan > 0 else 1.0
            qv = gv * (1.0 if ov > 1.0 else ov) * (pv - cvp)
        else:
            qv = 0.0
        pd += dt * (q - qa) / ca
        pv += dt * (qa - qv) / cv
        pd_out[i], pv_out[i], q_out[i] = pd, pv, q
    return pd_out, pv_out, q_out


def _decimate(x: np.ndarray, k: int) -> np.ndarray:
    """Block average, as the MAX30101's 4x averaging does."""
    m = len(x) // k
    return x[:m * k].reshape(m, k).mean(axis=1)


def respond(t0: float, t1: float, cuff_at, pt: Patient, v: Vessels) -> dict:
    """Integrate the vessels from t0 to t1 under cuff pressure ``cuff_at(t)``,
    after SETTLE_S with the cuff off. Returns the SIM_FS series; ``dv`` is
    zeroed on the first second and carries no drain."""
    n_set = int(SETTLE_S * SIM_FS)
    tt = t0 + (np.arange(n_set + int((t1 - t0) * SIM_FS)) - n_set) / SIM_FS
    p_art = arterial_pressure(tt, pt)
    p_cuff = np.where(tt < t0, 0.0, cuff_at(tt))
    pd, pv, q = _integrate(p_art, p_cuff, v)
    rec = slice(n_set, None)
    vol = v.c_art * pd[rec] + v.c_ven * pv[rec]
    return {"t": tt[rec], "p_art": p_art[rec], "p_cuff": p_cuff[rec], "pd": pd[rec],
            "pv": pv[rec], "q": q[rec], "dv": vol - vol[: int(SIM_FS)].mean()}


def drain(t: np.ndarray, since_s: float, v: Vessels) -> np.ndarray:
    """The O5 occlusion drain, added to dV*L from ``since_s`` on."""
    if not v.drain_rate:
        return np.zeros_like(t)
    since = np.maximum(t - since_s, 0.0)
    if v.drain_tau > 0:
        return v.drain_rate * v.drain_tau * (1 - np.exp(-since / v.drain_tau))
    return v.drain_rate * since


def coloured_noise(n: int, fs: float, std: float, band: tuple[float, float],
                   rng: np.random.Generator) -> np.ndarray:
    """Band-limited Gaussian noise with the given standard deviation: the
    slow fluctuation (breathing, vasomotion, contact) that white count noise
    does not capture and that sets the onset noise floor."""
    if std <= 0 or n < 64:
        return np.zeros(n)
    sos = signal.butter(2, band, "band", fs=fs, output="sos")
    x = signal.sosfiltfilt(sos, rng.normal(0, 1, n + 2000))[1000:-1000]
    return std * x / x.std()


def simulate(patient: Patient | None = None, cuff: Cuff | None = None,
             vessels: Vessels | None = None, sensor: Sensor | None = None,
             seed: int = 0) -> SimRun:
    pt, c = patient or Patient(), cuff or Cuff()
    v, s = vessels or Vessels(), sensor or Sensor()
    rng = np.random.default_rng(seed)

    _, phases = cuff_pressure(np.zeros(1), c)
    r = respond(0.0, phases["end_s"], lambda x: cuff_pressure(x, c)[0], pt, v)
    tt, p_art, p_cuff, q = r["t"], r["p_art"], r["p_cuff"], r["q"]
    dv = r["dv"] + drain(tt, phases["inflation_s"], v)

    truth = _truth(tt, p_art, p_cuff, q, pt, phases)

    k = int(round(SIM_FS / s.fs))
    t = _decimate(tt, k) - 0.5 / SIM_FS * (k - 1)
    dv_r, pc_r = _decimate(dv, k), _decimate(p_cuff, k)
    return _finish(t, dv_r, pc_r, c.noise_mmhg, s, rng,
                   lambda ir, red, p_meas, dv_seen: SimRun(
                       t=t, ir=ir, red=red, p_mmhg=p_meas, p_true=pc_r,
                       p_art=_decimate(p_art, k), q_cuff=_decimate(q, k), dv=dv_r,
                       truth=truth, patient=pt, cuff=c, vessels=v, sensor=s))


def _finish(t, dv_r, pc_r, noise_mmhg, s: "Sensor", rng, make):
    """Optics and sensor noise: dV*L -> counts, cuff pressure -> reading."""
    n = len(t)
    dv_seen = dv_r + coloured_noise(n, s.fs, s.lf_noise_um, s.lf_band, rng)
    d_od_ir = dv_seen / (COEFF_IR_880 + COEFF_RED_660 * s.od_ratio)
    d_od_red = s.od_ratio * d_od_ir
    ir = s.base_ir * np.exp(-d_od_ir) + s.dark_ir + rng.normal(0, s.noise_counts, n)
    red = s.base_red * np.exp(-d_od_red) + s.dark_red + rng.normal(0, s.noise_counts, n)
    full = (1 << 18) - 1
    ir = np.clip(np.rint(ir), 0, full)
    red = np.clip(np.rint(red), 0, full)
    p_meas = pc_r + rng.normal(0, noise_mmhg, n)
    return make(ir, red, p_meas, dv_seen)


def _truth(t, p_art, p_cuff, q, pt: Patient, phases: dict) -> dict:
    """Ground truth: the patient's pressures, and when blood really first
    crossed the cuff during the deflation."""
    after = t >= phases["deflation_s"]
    flow = after & (q > 0)
    onset_t = float(t[np.argmax(flow)]) if flow.any() else None
    return {
        "map_mmhg": float(p_art.mean()),
        "sbp_mmhg": float(p_art.max()),
        "dbp_mmhg": float(p_art.min()),
        "pulse_pressure": pt.pulse_pressure,
        "hm3_pp_mmhg": pt.hm3_pp_mmhg,
        "flow_onset_s": onset_t,
        "flow_onset_mmhg": None if onset_t is None else float(np.interp(onset_t, t, p_cuff)),
        **phases,
    }
