"""Acceptance tests for the return-to-flow pipeline (docs/SPEC.md section 6).

Run with::

    python -m pytest tests/ -v

Each test builds a synthetic recording with a known answer via host/synth.py
and checks the pipeline recovers it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
HOST = ROOT / "host"
sys.path.insert(0, str(HOST))

from mapquest.pipeline import (  # noqa: E402
    COEFF_IR_880,
    COEFF_RED_660,
    PipelineConfig,
    blood_volume,
    delta_od,
    derive,
)
import synth  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SYNTH_DEFAULTS = dict(
    seconds=90.0, fs=100.0, map=90.0, hold_mmhg=150.0, hold_s=10.0,
    deflate_rate=2.0, fill_rate=3.0, noise=40.0, drift=0.0,
    cardiac=False, cardiac_hz=1.2, cardiac_frac=0.008,
    artificial_pulse=False, ap_frac=0.004,
    no_filling=False, constant_volume=False, desaturate=False,
    desat_to=0.70, desat_od660=0.03,
    p_fs=40.0, p_raw0=1_200_000.0, p_counts_per_mmhg=4200.0, seed=0,
)


def make(**overrides):
    """Build a synthetic recording in memory and run the pipeline on it."""
    args = argparse.Namespace(**{**SYNTH_DEFAULTS,
                                 **{k: v for k, v in overrides.items() if not k.startswith("_")}})
    rng = np.random.default_rng(args.seed)
    t, ir, red, p_mmhg, _p_idx, _p_raw, dv_true = synth.build(args, rng)

    cfg = PipelineConfig(
        ir_only=overrides.get("_ir_only", False),
        onset_method=overrides.get("_onset_method", "intersect"),
        baseline=overrides.get("_baseline", "sloped"),
        floor_mu0=overrides.get("_floor_mu0", True),
    )
    d = derive(
        {"ir": ir.astype(float), "red": red.astype(float)},
        fs=args.fs,
        dark={"ir": synth.DARK_IR, "red": synth.DARK_RED},
        deflation_start_s=args.hold_s,
        t=t,
        config=cfg,
    )
    return d, t, p_mmhg, dv_true, args


def onset_mmhg(d, t, p_mmhg) -> float:
    assert d.onset_time is not None, "no onset detected"
    return float(np.interp(d.onset_time, t, p_mmhg))


# ---------------------------------------------------------------------------
# SPEC 6: required tests
# ---------------------------------------------------------------------------

def test_onset_within_1mmhg_clean():
    """Onset detected within +/-1 mmHg of MAP_true at clean SNR."""
    d, t, p, _, args = make()
    err = onset_mmhg(d, t, p) - args.map
    assert abs(err) <= 1.0, f"onset error {err:+.2f} mmHg exceeds +/-1"


@pytest.mark.parametrize("map_true", [70.0, 85.0, 90.0, 105.0])
def test_onset_accurate_across_pressures(map_true):
    """The +/-1 mmHg result is not a fluke of one MAP value."""
    d, t, p, _, args = make(map=map_true)
    err = onset_mmhg(d, t, p) - map_true
    assert abs(err) <= 1.0, f"MAP {map_true}: error {err:+.2f} mmHg"


def test_onset_within_2mmhg_with_pulses():
    """Within +/-2 mmHg with cardiac AND artificial-pulse components."""
    d, t, p, _, args = make(cardiac=True, artificial_pulse=True)
    err = onset_mmhg(d, t, p) - args.map
    assert abs(err) <= 2.0, f"onset error {err:+.2f} mmHg exceeds +/-2"


def test_no_false_onset_without_filling():
    """A recording with no filling must produce no onset."""
    d, _t, _p, _, _args = make(no_filling=True)
    assert d.onset_time is None, (
        f"false onset at t={d.onset_time} on a recording with no filling"
    )


def test_two_wavelength_rejects_desaturation():
    """Desaturation at constant volume: two-wavelength stays flat, IR-only drifts.

    This is the whole reason the solve uses two wavelengths. Trapped blood
    desaturating changes red absorption steeply and IR barely at all, which an
    IR-only reading cannot distinguish from blood arriving.
    """
    d2, _t, _p, _, _ = make(desaturate=True, constant_volume=True)
    d1, _t1, _p1, _, _ = make(desaturate=True, constant_volume=True, _ir_only=True)

    span2 = float(np.ptp(d2.dv_lp))
    span1 = float(np.ptp(d1.dv_lp))

    assert span1 > 3 * span2, (
        f"IR-only should drift far more than two-wavelength under desaturation; "
        f"got IR-only span {span1:.3f} um vs two-wavelength {span2:.3f} um"
    )
    assert d2.onset_time is None, (
        f"two-wavelength reported a false onset at t={d2.onset_time} on a "
        "constant-volume desaturation recording"
    )


def test_delta_od_of_one_percent_drop():
    """delta-OD of a 1% count drop is approximately 0.010."""
    ref = 100_000.0
    counts = np.full(16, ref * 0.99)
    od = delta_od(counts, ref)
    assert np.allclose(od, 0.01005, atol=1e-4), f"got {od[0]:.6f}"


# ---------------------------------------------------------------------------
# Unit-level checks
# ---------------------------------------------------------------------------

def test_blood_volume_two_wavelength_formula():
    """dV*L = 229*dOD_660 + 1555*dOD_880, exactly as SPEC 5.1 step 6 states."""
    d_od = {"red": np.array([0.01]), "ir": np.array([0.02])}
    expected = COEFF_RED_660 * 0.01 + COEFF_IR_880 * 0.02
    assert np.isclose(blood_volume(d_od, ir_only=False)[0], expected)


def test_blood_volume_ir_only_fallback_is_in_micrometres():
    """SPEC states the fallback as delta-OD/6.13 cm^-1, which is CENTIMETRES.

    The two-wavelength branch yields micrometres, so the fallback is converted
    to match. Without that the two paths sit 1e4 apart and any threshold,
    comparison or plot mixing them is meaningless.
    """
    d_od = {"red": np.array([0.01]), "ir": np.array([0.02])}
    expected_um = 0.02 / 6.13 * 1e4
    assert np.isclose(blood_volume(d_od, ir_only=True)[0], expected_um)

    # Sanity check that the conversion is the right way round: the effective
    # per-OD gain should sit near the two-wavelength IR coefficient.
    gain = 1e4 / 6.13
    assert 0.5 * COEFF_IR_880 < gain < 2.0 * COEFF_IR_880


def test_delta_od_handles_nonpositive_counts():
    """A dark-subtracted count at or below zero must not poison the chain."""
    od = delta_od(np.array([-5.0, 0.0, 100.0]), reference=100.0)
    assert np.all(np.isfinite(od)), "delta_od produced non-finite values"


def test_ramp_slope_recovered():
    """The fitted ramp slope should match the synthetic fill rate."""
    d, _t, _p, _, args = make(fill_rate=3.0)
    assert np.isclose(d.ramp_slope, args.fill_rate, rtol=0.1), (
        f"fitted slope {d.ramp_slope:.3f} vs true {args.fill_rate}"
    )


def test_threshold_method_is_biased_early():
    """Document the structural bias that motivates the intersect default.

    This is a characterisation test, not an aspiration: the SPEC 5.1 threshold
    method reads high because symmetric filters smear the ramp corner
    backwards. If this ever stops holding, the intersect default should be
    revisited.
    """
    d, t, p, _, args = make()
    assert d.threshold_time is not None
    thr_err = float(np.interp(d.threshold_time, t, p)) - args.map
    int_err = onset_mmhg(d, t, p) - args.map
    assert thr_err > 1.0, f"expected threshold method to read high, got {thr_err:+.2f}"
    assert abs(int_err) < abs(thr_err), "intersect should be the more accurate of the two"


# ---------------------------------------------------------------------------
# O5 / D39: blood volume drains under occlusion
# ---------------------------------------------------------------------------

# Shape of the drain on the 2026-10-09 20:35 trial: ~-1.4 um/s just after
# inflation, slowing with a time constant of ~18 s. Fill rate ~1.1 um/s (O2).
SLOWING_DRAIN = dict(drain_rate=-1.4, drain_tau=18.0, fill_rate=1.1)


def test_sloped_baseline_with_constant_drain():
    d, t, p, _, args = make(drain_rate=-0.29, fill_rate=1.1)
    err = onset_mmhg(d, t, p) - args.map
    assert abs(err) <= 1.0, f"sloped-baseline error {err:+.2f} mmHg under a constant drain"


@pytest.mark.parametrize("map_true", [70.0, 90.0, 105.0])
def test_sloped_baseline_with_slowing_drain(map_true):
    """A curving drain: the line fitted just before onset is still close enough."""
    d, t, p, _, args = make(map=map_true, **SLOWING_DRAIN)
    err = onset_mmhg(d, t, p) - map_true
    assert abs(err) <= 1.0, f"MAP {map_true}: sloped-baseline error {err:+.2f} mmHg"


def test_flat_baseline_reads_low_under_drain():
    """Characterises the bias D39 removes: a rising ramp meets a level left
    behind by a falling baseline late, so the pressure reads low."""
    d, t, p, _, args = make(_baseline="flat", **SLOWING_DRAIN)
    err = onset_mmhg(d, t, p) - args.map
    assert err < -5.0, f"expected the flat baseline to read well low, got {err:+.2f} mmHg"
    assert d.onset_time_flat == d.onset_time
    assert d.onset_time_sloped is not None
    sloped_err = float(np.interp(d.onset_time_sloped, t, p)) - args.map
    assert abs(sloped_err) < abs(err)


def test_sloped_matches_flat_without_drain():
    """No drain: the fitted slope is ~0 and both baselines agree."""
    d, t, p, _, _ = make()
    a = float(np.interp(d.onset_time_sloped, t, p))
    b = float(np.interp(d.onset_time_flat, t, p))
    assert abs(a - b) < 0.5, f"sloped {a:.2f} vs flat {b:.2f} mmHg on a flat baseline"
    assert abs(d.baseline_fit[0]) < 0.05


def test_no_false_onset_from_slowing_drain():
    """A drain that slows makes dV/dt rise towards zero; that is not filling."""
    d, *_ = make(no_filling=True, cardiac=True, drain_rate=-1.4, drain_tau=18.0)
    assert d.onset_time is None, f"false onset at t={d.onset_time} from a slowing drain"


def test_unfloored_threshold_fires_on_slowing_drain():
    """Why floor_mu0 exists: with mu0 from the steep start of the drain, the
    literal SPEC threshold is crossed by the drain decelerating -- long before
    any blood arrives."""
    d, t, p, _, args = make(_floor_mu0=False, cardiac=True, **SLOWING_DRAIN)
    assert d.threshold_time is not None
    early = float(np.interp(d.threshold_time, t, p)) - args.map
    assert early > 10.0, f"expected a crossing well before onset, got {early:+.2f} mmHg"


def test_fit_baseline_recovers_known_line():
    from mapquest.pipeline import fit_baseline
    fs = 100.0
    t = np.arange(0, 60, 1 / fs)
    y = 5.0 - 0.3 * t
    (slope, intercept), (t0, t1) = fit_baseline(y, t, fs, slice(500, 800), 4000, 10.0, 2.0)
    assert np.isclose(slope, -0.3) and np.isclose(intercept, 5.0)
    assert np.isclose(t1, 38.0, atol=0.02) and np.isclose(t0, 28.0, atol=0.02)
    # Too little room before the crossing: falls back to the reference window.
    (_, _), (t0, t1) = fit_baseline(y, t, fs, slice(500, 800), 900, 10.0, 2.0)
    assert np.isclose(t0, 5.0) and np.isclose(t1, 7.99)


def test_reference_window_defaults_before_deflation():
    d, t, _p, _, args = make()
    assert t[d.ref_slice.stop - 1] <= args.hold_s + 1e-6
    assert d.ref_slice.stop - d.ref_slice.start > 100


def test_derive_rejects_short_input():
    with pytest.raises(ValueError, match="at least 64"):
        derive({"ir": np.ones(10)}, fs=100.0)


def test_derive_requires_ir():
    with pytest.raises(ValueError, match="'ir' channel"):
        derive({"red": np.ones(500)}, fs=100.0)


# ---------------------------------------------------------------------------
# End-to-end through the CLI
# ---------------------------------------------------------------------------

def test_cli_roundtrip(tmp_path):
    """synth.py -> analyze.py runs clean and reports the right pressure."""
    csv = tmp_path / "s.csv"
    subprocess.run(
        [sys.executable, str(HOST / "synth.py"), "--map", "95",
         "--out", str(csv)],
        check=True, capture_output=True, cwd=HOST,
    )
    assert csv.exists()

    out = subprocess.run(
        [sys.executable, str(HOST / "analyze.py"), str(csv),
         "--no-show", "--no-results", "--save", str(tmp_path / "s.png")],
        check=True, capture_output=True, text=True, cwd=HOST,
        env={**os.environ, "MPLBACKEND": "Agg"},
    )
    assert "ONSET PRESSURE" in out.stdout
    line = next(ln for ln in out.stdout.splitlines() if "ONSET PRESSURE" in ln)
    value = float(line.split(":")[1].strip().split()[0])
    assert abs(value - 95.0) <= 1.0, f"CLI reported {value} mmHg for a 95 mmHg truth"
