"""Tests for host/gauge_video.py: reading cuff pressure off a gauge video (D38).

A synthetic gauge is drawn frame by frame with a known needle path, a dial
whose needle rests at 236 (like the real one), uneven tick spacing (an
off-axis view), and a hand-held camera that drifts, zooms and rolls.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "host"))

import gauge_video as gv  # noqa: E402

ZERO_DIAL = 236.0     # where the synthetic needle rests, like the real gauge
TURN = 320.0
FPS = 30.0


def dial_angle(value: float, warp: float = 0.0) -> float:
    """Dial value -> angle (deg clockwise from straight down), with an optional
    smooth stretch standing in for an off-axis view."""
    a = value * 360.0 / TURN
    return a + warp * np.sin(np.deg2rad(a))


def draw_gauge(needle_value: float, warp: float = 6.0, size: int = 520) -> np.ndarray:
    img = np.full((size, size, 3), 40, np.uint8)
    c = size // 2
    R = 200
    cv2.circle(img, (c, c), R + 14, (180, 180, 180), -1)          # chrome rim
    cv2.circle(img, (c, c), R, (235, 235, 230), -1)               # dial face

    def pt(angle, r):
        a = np.deg2rad(angle)
        return int(round(c - np.sin(a) * r)), int(round(c + np.cos(a) * r))

    for v in range(20, 301, 2):
        a = dial_angle(v, warp)
        major = v % 10 == 0
        cv2.line(img, pt(a, R - 8), pt(a, R - (40 if major else 20)), (20, 20, 20), 3 if major else 1,
                 cv2.LINE_AA)
        if v % 20 == 0:
            x, y = pt(a, R - 68)
            cv2.putText(img, str(v), (x - 16, y + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (20, 20, 20), 2,
                        cv2.LINE_AA)
    cv2.rectangle(img, (c - 18, c + R - 50), (c + 18, c + R - 36), (20, 20, 20), 2)   # zero box
    cv2.putText(img, "SPHYGMOMANOMETER", (c - 95, c + 45), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (30, 30, 30), 1, cv2.LINE_AA)
    cv2.putText(img, "1st", (c - 30, c - 60), cv2.FONT_HERSHEY_DUPLEX, 1.4, (30, 30, 30), 3, cv2.LINE_AA)
    a = dial_angle(needle_value, warp)
    cv2.line(img, pt(a + 180, 75), pt(a, 185), (15, 15, 15), 3, cv2.LINE_AA)       # pointer + tail
    cv2.circle(img, pt(a + 180, 75), 9, (15, 15, 15), 2, cv2.LINE_AA)               # counterweight
    cv2.circle(img, (c, c), 8, (15, 15, 15), -1)
    return img


def pressure_profile(t: np.ndarray) -> np.ndarray:
    """Rest 1.5 s, inflate to 180, bleed at 20 mmHg/s to 40, hold."""
    p = np.zeros_like(t)
    p = np.where(t >= 1.5, np.minimum(180.0, (t - 1.5) * 120.0), p)
    p = np.where(t >= 3.5, np.maximum(40.0, 180.0 - (t - 3.5) * 20.0), p)
    return p


def make_video(path: Path, seconds: float = 12.0, seed: int = 1):
    rng = np.random.default_rng(seed)
    n = int(seconds * FPS)
    t = np.arange(n) / FPS
    p = pressure_profile(t)
    W, H = 960, 640
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), FPS, (W, H))
    bgtex = (rng.random((H, W, 3)) * 60 + 60).astype(np.uint8)
    bgtex = cv2.GaussianBlur(bgtex, (0, 0), 3)
    # Slow random walk for the hand-held camera.
    roll = np.cumsum(rng.normal(0, 0.15, n)).clip(-4, 4)
    dx = np.cumsum(rng.normal(0, 1.5, n)).clip(-60, 60)
    dy = np.cumsum(rng.normal(0, 1.5, n)).clip(-40, 40)
    zoom = 1.0 + np.cumsum(rng.normal(0, 0.002, n)).clip(-0.08, 0.08)
    for i in range(n):
        g = draw_gauge((ZERO_DIAL + p[i]) % TURN)
        frame = bgtex.copy()
        x0, y0 = 300, 60
        frame[y0:y0 + g.shape[0], x0:x0 + g.shape[1]] = g
        # On-screen clock with the red REC dot, on the monitor's white window.
        cv2.rectangle(frame, (20, 565), (360, 620), (245, 245, 245), -1)
        cv2.putText(frame, f"t = {100 + t[i]:6.1f} s", (40, 600), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (30, 30, 30), 2)
        cv2.circle(frame, (300, 592), 6, (40, 40, 210), -1)
        M = cv2.getRotationMatrix2D((x0 + 260, y0 + 260), roll[i], zoom[i])
        M[:, 2] += (dx[i], dy[i])
        frame = cv2.warpAffine(frame, M, (W, H), borderMode=cv2.BORDER_REFLECT)
        out.write(frame)
    out.release()
    return t, p


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    path = tmp_path_factory.mktemp("gv") / "gauge.avi"
    t, p = make_video(path)
    res = gv.read_gauge(path, every=1, log=lambda *_: None)
    return path, t, p, res


def test_reads_pressure_through_camera_motion(synthetic):
    _path, t, p, res = synthetic
    keep = res.keep()
    truth = np.interp(res.video_t, t, p)
    err = res.mmhg()[keep] - truth[keep]
    assert keep.mean() > 0.8, f"only {keep.mean():.0%} of frames kept"
    assert np.median(np.abs(err)) < 1.0, f"median error {np.median(np.abs(err)):.2f} mmHg"
    assert np.percentile(np.abs(err), 95) < 2.5, f"95th pct error {np.percentile(np.abs(err), 95):.2f}"


def test_zero_offset_measured_from_rest(synthetic):
    *_, res = synthetic
    assert abs(res.zero - ZERO_DIAL) < 1.0, f"zero {res.zero:.2f}, expected {ZERO_DIAL}"


def test_tick_calibration_finds_every_tick(synthetic):
    *_, res = synthetic
    expected = np.array([dial_angle(v, 6.0) for v in range(20, 301, 10)])
    got = res.scale.tick_angles
    assert len(got) == 29
    # Canvas roll is relative to the reference frame, so compare spacing.
    assert np.allclose(np.diff(got), np.diff(expected), atol=0.6)


def test_walk_ticks_skips_strays_and_fills_gaps():
    true = np.array([dial_angle(v, 6.0) for v in range(20, 301, 10)])
    peaks = np.delete(true, [11])                         # one tick under glare
    peaks = np.append(peaks, [true[5] + 4.5, 2.0, 352.0])  # a stray mark, the zero box edges
    got = gv.walk_ticks(peaks, 29, 11.25)
    assert len(got) == 29
    assert np.allclose(got, true, atol=1.5)
    assert abs(got[11] - true[11]) < 1.5                  # interpolated, not the stray


def test_dial_scale_wraps_through_the_gap():
    ticks = np.array([dial_angle(v) for v in range(20, 301, 10)])
    sc = gv.DialScale(ticks, 20.0, 10.0, 320.0)
    assert np.isclose(sc.value(dial_angle(120)), 120.0)
    assert np.isclose(sc.value(dial_angle(310) % 360), 310.0, atol=0.01)
    assert np.isclose(sc.value(dial_angle(5)), 5.0, atol=0.01)


def test_clock_map_linear_and_slow_motion():
    f, desc = gv.clock_map([(0.5, 81.9), (24.5, 105.7), (48.5, 129.8), (78.5, 159.5)])
    assert desc.startswith("linear") and np.isclose(f(0.0), 81.4, atol=0.1)
    # Slow motion: 1x, then 0.25x, then 1x -- a line cannot fit, so piecewise.
    pts = [(0, 10), (10, 20), (50, 30), (90, 40), (100, 50)]
    f, desc = gv.clock_map(pts)
    assert desc.startswith("piecewise") and np.isclose(f(30), 25.0)
    assert np.isclose(f(105), 55.0)                       # end slope extrapolated


def test_parse_clock():
    assert gv.parse_clock("6.5=87.8, 0.5=81.9") == [(0.5, 81.9), (6.5, 87.8)]


def test_rec_dot_found(synthetic):
    path, *_ = synthetic
    cap = cv2.VideoCapture(str(path))
    ok, f = cap.read()
    dot = gv.rec_dot(f)
    assert ok and dot is not None
