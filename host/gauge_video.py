#!/usr/bin/env python3
"""Read cuff pressure from a phone video of the gauge beside the monitor (D38).

Until the HX710B pressure sensor is fitted, a trial is filmed with the analogue
gauge and the monitor's on-screen session clock in the same frame. This reads
every sampled frame's needle and writes (session time, mmHg) pairs that line up
with the recording, so onset times can be converted to cuff pressure.

    # 1. first run: writes a contact sheet of the on-screen clock
    python gauge_video.py ../data/video/trial.mov --recording ../data/hr_wrist_X.csv

    # 2. read a handful of clock values off the sheet, then run again with them
    python gauge_video.py ../data/video/trial.mov --recording ../data/hr_wrist_X.csv \\
        --clock "0.5=81.9,24.5=105.7,48.5=129.8,78.5=159.5"

Outputs, next to the recording:
    <recording>_gauge.csv        t_s, mmhg, dial, video_frame
    <recording>_gauge_check.png  needle overlays, tick calibration, pressure trace
    <recording>_clock_sheet.png  clock crops for reading the time mapping

How it works:

1. **Dial.** Found by a Hough circle in each frame and aligned to a reference
   frame by ORB features on the dial face (similarity: shift, scale, roll), so
   a hand-held camera works; a 3 deg roll would otherwise read as ~3 mmHg.
2. **Background.** A high percentile over aligned frames gives the dial with no
   needle. The needle is where a frame is darker than that, high-passed to keep
   the thin needle and drop its soft shadow.
3. **Calibration.** The major-tick ring is found by its angular periodicity and
   every tick angle is measured, because an off-axis view stretches parts of
   the dial (10.2-13.1 deg per 10 mmHg seen on 2026-10-10). Ticks hidden by
   glare are interpolated; stray marks are skipped.
4. **Needle.** The pointer end is found on a ring the pointer crosses but the
   counterweight tail does not, then refined along its length.
5. **Zero.** Measured from the opening seconds, before inflation. The gauge in
   use rests at ~236 on its dial, not in the zero box, so pressure is
   (dial - zero) mod `turn`. Its span is unverified: readings are provisional.
6. **Clock.** Session time from hand-read clock values: a straight line when
   it fits (normal-speed video), otherwise piecewise-linear through the points
   (slow motion, which ramps speed at each end).
"""

from __future__ import annotations

import argparse
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - optional dependency
    cv2 = None

from scipy.ndimage import median_filter
from scipy.signal import find_peaks, medfilt

# Canvas every aligned dial is drawn into. The Hough rim radius is scaled to
# DIAL_R, so radii below are in canvas pixels for any camera distance.
CANVAS = 300
DIAL_R = 110.0

# Needle and tick radii as fractions of the major-tick radius. Measured on the
# gauge filmed 2026-10-09 and 2026-10-10 (tick radius 152 px and 88 px): the
# counterweight tail ends at ~0.5, the pointer tip at ~0.97.
CLEAR_RING = (0.56, 0.69)
NEEDLE_SPAN = (0.20, 0.95)
HIGHPASS_SIGMA = 0.033

ANGLE_STEP = 0.25            # deg, needle search resolution (refined sub-step)
MAX_JUMP_MMHG = 4.0          # cleaning: distance from a 9-frame median
MAX_RUNNER_UP = 0.6          # cleaning: second-best needle candidate / best


def to_gray(frame: np.ndarray, invert: bool = False) -> np.ndarray:
    """Grayscale with a dark needle on a light face, as everything below expects.

    A gauge with a light needle and markings on a dark face (the Omron used
    on 2026-10-10 15:18) is inverted first.
    """
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return 255 - g if invert else g


def _need_cv2():
    if cv2 is None:
        raise SystemExit("gauge_video needs OpenCV: pip install opencv-python-headless")


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def ray_maps(cx: float, cy: float, radii: np.ndarray, step: float = ANGLE_STEP):
    """remap() grids sampling rays from (cx, cy).

    Angles run clockwise from straight DOWN as the viewer sees the dial, so a
    gauge's zero (bottom) is 0 deg and values increase with angle.
    """
    th = np.deg2rad(np.arange(0.0, 360.0, step))
    xs = cx - np.sin(th)[:, None] * radii[None, :]
    ys = cy + np.cos(th)[:, None] * radii[None, :]
    return xs.astype(np.float32), ys.astype(np.float32)


def polar(img: np.ndarray, cx: float, cy: float, radii: np.ndarray,
          step: float = ANGLE_STEP) -> np.ndarray:
    """Rows = angle, columns = radius."""
    xs, ys = ray_maps(cx, cy, radii, step)
    return cv2.remap(img.astype(np.float32), xs, ys, cv2.INTER_LINEAR)


def find_dial(gray: np.ndarray, hint: tuple[float, float, float] | None = None):
    """(x, y, r) of the gauge dial, or None.

    With a hint from the previous frame the search is limited to a window
    around it, which keeps other round things in the scene from winning.
    """
    h, w = gray.shape
    if hint is not None:
        hx, hy, hr = hint
        m = int(hr * 2.2)
        x0, y0 = max(0, int(hx - m)), max(0, int(hy - m))
        x1, y1 = min(w, int(hx + m)), min(h, int(hy + m))
        rmin, rmax = int(hr * 0.75), int(hr * 1.3)
    else:
        x0, y0, x1, y1 = 0, 0, w, h
        rmin, rmax = int(0.05 * h), int(0.45 * h)
    region = gray[y0:y1, x0:x1]
    if region.size == 0:
        return None
    cs = cv2.HoughCircles(cv2.medianBlur(region, 5), cv2.HOUGH_GRADIENT, dp=1.2,
                          minDist=max(rmin, 10), param1=120, param2=45,
                          minRadius=rmin, maxRadius=rmax)
    if cs is None:
        return None
    x, y, r = (float(v) for v in cs[0][0])
    return x + x0, y + y0, r


def circle_inside(c, shape, margin: float = 1.05) -> bool:
    x, y, r = c
    h, w = shape[:2]
    rr = r * margin
    return x - rr >= 0 and y - rr >= 0 and x + rr <= w and y + rr <= h


def _crop_scaled(gray: np.ndarray, circle) -> tuple[np.ndarray, np.ndarray]:
    """Dial crop resampled to canvas scale, plus the 3x3 frame->crop transform."""
    x, y, r = circle
    s = DIAL_R / r
    half = CANVAS / 2
    T = np.array([[s, 0, half - s * x], [0, s, half - s * y], [0, 0, 1.0]])
    crop = cv2.warpAffine(gray, T[:2], (CANVAS, CANVAS), flags=cv2.INTER_AREA)
    return crop, T


class Aligner:
    """Maps any frame's dial onto the reference frame's canvas."""

    def __init__(self, ref_gray: np.ndarray, ref_circle):
        self.ref, _ = _crop_scaled(ref_gray, ref_circle)
        self.orb = cv2.ORB_create(3000)
        self.mask = np.zeros((CANVAS, CANVAS), np.uint8)
        cv2.circle(self.mask, (CANVAS // 2, CANVAS // 2), int(DIAL_R * 1.05), 255, -1)
        self.kr, self.dr = self.orb.detectAndCompute(self.ref, self.mask)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        if self.dr is None or len(self.kr) < 30:
            raise RuntimeError("too few features on the reference dial")

    def align(self, gray: np.ndarray, circle):
        """(2x3 frame->canvas, inliers, roll_deg) or None."""
        crop, T = _crop_scaled(gray, circle)
        k2, d2 = self.orb.detectAndCompute(crop, self.mask)
        if d2 is None or len(k2) < 30:
            return None
        pairs = self.bf.knnMatch(d2, self.dr, k=2)
        good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
        if len(good) < 20:
            return None
        src = np.float32([k2[m.queryIdx].pt for m in good])
        dst = np.float32([self.kr[m.trainIdx].pt for m in good])
        M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                             ransacReprojThreshold=2.0, maxIters=5000)
        if M is None or inl.sum() < 15:
            return None
        full = np.vstack([M, [0, 0, 1]]) @ T
        roll = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
        return full[:2], int(inl.sum()), roll


# ---------------------------------------------------------------------------
# Needle
# ---------------------------------------------------------------------------

def highpassed_darkening(canvas: np.ndarray, bg: np.ndarray, r_tick: float) -> np.ndarray:
    rel = np.clip((bg - canvas.astype(np.float32)) / (bg + 1.0), 0, None)
    return np.clip(rel - cv2.GaussianBlur(rel, (0, 0), HIGHPASS_SIGMA * r_tick), 0, None)


class NeedleReader:
    def __init__(self, bg: np.ndarray, pivot: tuple[float, float], r_tick: float):
        self.bg, self.pivot, self.r_tick = bg, pivot, r_tick
        cx, cy = pivot
        self.ring = ray_maps(cx, cy, np.arange(*[f * r_tick for f in CLEAR_RING], 0.5))
        self.full = ray_maps(cx, cy, np.arange(*[f * r_tick for f in NEEDLE_SPAN], 0.5))

    def read(self, canvas: np.ndarray) -> tuple[float, float, float]:
        """(angle_deg, strength, runner_up_ratio)."""
        rel = highpassed_darkening(canvas, self.bg, self.r_tick)
        pc = cv2.remap(rel, *self.ring, cv2.INTER_LINEAR).mean(1)
        k0 = int(np.argmax(pc))
        pf = cv2.remap(rel, *self.full, cv2.INTER_LINEAR).mean(1)
        n = len(pf)
        win = [(k0 + j) % n for j in range(-12, 13)]          # +-3 deg
        k = win[int(np.argmax(pf[win]))]
        a, b, c = pf[(k - 1) % n], pf[k], pf[(k + 1) % n]
        den = a - 2 * b + c
        off = 0.5 * (a - c) / den if den != 0 else 0.0
        far = [(k0 + j) % n for j in range(n) if min(abs(j), n - abs(j)) > 40]
        runner = float(pc[far].max() / pc[k0]) if pc[k0] > 0 else 1.0
        a = float(((k + off) * ANGLE_STEP) % 360)
        return self._crossing(rel, a), float(pc[k0]), runner

    def _crossing(self, rel: np.ndarray, a: float) -> float:
        """Angle, round the pivot, at which the needle crosses the tick ring.

        The ray search assumes the needle passes through the pivot estimate;
        a pivot a few px off then tilts the reading (~0.5 mmHg per px). Here a
        line is fitted to the needle's own pixels in a narrow wedge, and the
        reading is where it crosses the tick ring. The ticks were measured on
        that same ring, so pivot error largely cancels.
        """
        cx, cy = self.pivot
        h, w = rel.shape
        r0, r1 = 0.3 * self.r_tick, 0.98 * self.r_tick
        th = np.deg2rad(a + np.linspace(-2.5, 2.5, 21))
        rs = np.arange(r0, r1, 0.5)
        xs = (cx - np.sin(th)[:, None] * rs[None, :]).ravel()
        ys = (cy + np.cos(th)[:, None] * rs[None, :]).ravel()
        wts = cv2.remap(rel, xs.astype(np.float32)[None], ys.astype(np.float32)[None], cv2.INTER_LINEAR)[0]
        if wts.sum() <= 0 or np.count_nonzero(wts > 0.3 * wts.max()) < 10:
            return a
        wts = np.where(wts > 0.3 * wts.max(), wts, 0.0)
        mx, my = np.average(xs, weights=wts), np.average(ys, weights=wts)
        cov = np.cov(np.vstack([xs - mx, ys - my]), aweights=wts)
        evals, evecs = np.linalg.eigh(cov)
        dx, dy = evecs[:, 1]
        if evals[1] < 4 * max(evals[0], 1e-9):
            return a
        # Point on the line at distance r_tick from the pivot, on the pointer side.
        fx, fy = mx - cx, my - cy
        b = fx * dx + fy * dy
        c = fx * fx + fy * fy - self.r_tick ** 2
        disc = b * b - c
        if disc < 0:
            return a
        best = None
        for s in (-b + np.sqrt(disc), -b - np.sqrt(disc)):
            px, py = mx + s * dx, my + s * dy
            ang = float(np.degrees(np.arctan2(-(px - cx), py - cy)) % 360)
            d = abs((ang - a + 180) % 360 - 180)
            if best is None or d < best[0]:
                best = (d, ang)
        return best[1] if best[0] < 5 else a


def find_pivot(bg: np.ndarray, canvases: list[np.ndarray], r_tick: float,
               centre: tuple[float, float] | None = None, search: float = 0.15 * DIAL_R
               ) -> tuple[float, float]:
    """Pivot that makes the needle line up best across sample frames.

    The pivot need not be at the centre of the circle Hough found: the rim is
    seen in perspective and the needle hub can sit ~12 px off it. Coarse 2 px
    grid over +-search, then 0.5 px around the best.
    """
    cx, cy = centre or (CANVAS / 2, CANVAS / 2)

    def best_of(xs, ys):
        best = None
        for px in xs:
            for py in ys:
                rd = NeedleReader(bg, (px, py), r_tick)
                score = sum(rd.read(cv)[1] for cv in canvases)
                if best is None or score > best[0]:
                    best = (score, px, py)
        return best[1], best[2]

    px, py = best_of(np.arange(cx - search, cx + search + 0.1, 2.0),
                     np.arange(cy - search, cy + search + 0.1, 2.0))
    return best_of(np.arange(px - 2, px + 2.1, 0.5), np.arange(py - 2, py + 2.1, 0.5))


# ---------------------------------------------------------------------------
# Dial calibration
# ---------------------------------------------------------------------------

def detect_tick_peaks(bg: np.ndarray, pivot, r: float, spacing: float, band: float = 3.0) -> np.ndarray:
    """Angles of tick-like dark marks on a ring of radius r round the pivot.

    The ring is averaged over +-band px, which absorbs the few pixels by which
    the tick circle is off-centre from the needle hub (parallax: the hub
    stands above the dial face). The median window is about half a tick
    spacing, so brightness steps from glare or the bezel are flattened while
    tick peaks stand.
    """
    step = 0.1
    pol = polar(bg, *pivot, np.arange(r - band, r + band + 0.1, 0.5), step)
    prof = -pol.mean(1)
    prof = prof - median_filter(prof, size=int(0.55 * spacing / step), mode="wrap")
    pk, _ = find_peaks(prof, distance=int(0.5 * spacing / step), prominence=0.3 * prof.std())
    return pk * step


def calibrate_ticks(bg: np.ndarray, pivot, n_ticks: int, spacing: float):
    """Tick angles round the pivot, the ring radius used, and fit quality.

    Every ring radius from 0.45 to 0.9 of the dial is tried. On the tick ring
    the walk finds nearly every tick as a real mark with smoothly changing
    spacing; through the printed numbers or the bezel it does not. The radius
    with the best (ticks matched - 4 x roughness) wins. Roughness is the mean
    change in spacing from one tick to the next, in degrees.

    On 2026-10-09 (tripod) this matched 29/29 ticks; 2026-10-10 (hand-held,
    viewed from below) 28/29, the 29th under glare.
    """
    best = None
    for r in np.arange(0.45 * DIAL_R, 0.9 * DIAL_R, 1.0):
        peaks = detect_tick_peaks(bg, pivot, r, spacing)
        try:
            ticks = walk_ticks(peaks, n_ticks, spacing)
        except ValueError:
            continue
        ext = np.concatenate([peaks, peaks + 360.0])
        matched = int(sum(np.min(np.abs(ext - a)) <= 0.2 * spacing for a in ticks))
        rough = float(np.abs(np.diff(np.diff(ticks))).mean())
        q = matched - 4.0 * rough
        if best is None or q > best[0]:
            best = (q, ticks, float(r), matched, rough)
    if best is None:
        raise SystemExit("could not find the dial's tick marks")
    return best[1], best[2], best[3], best[4]


def walk_ticks(peaks: np.ndarray, n_ticks: int, spacing: float, tol: float = 0.3) -> np.ndarray:
    """Assign n_ticks consecutive major ticks to detected peaks.

    Every peak is tried as the first tick; from it the walk steps clockwise by
    the local spacing, taking a peak within tol*spacing of where the next tick
    should be and interpolating when there is none (a tick under glare). The
    start that matches the most peaks wins, which rejects the zero box and
    other marks outside the scale. Returns tick angles in increasing order,
    unwrapped (may exceed 360).
    """
    peaks = np.sort(np.asarray(peaks, float))
    if len(peaks) < max(3, n_ticks // 3):
        raise ValueError(f"only {len(peaks)} tick-like marks found")
    ext = np.concatenate([peaks, peaks + 360.0])
    best = None
    for p0 in peaks:
        angles, matched, local = [p0], 1, spacing
        for _ in range(n_ticks - 1):
            target = angles[-1] + local
            d = np.abs(ext - target)
            j = int(np.argmin(d))
            if d[j] <= tol * local:
                new = ext[j]
                matched += 1
                local = 0.5 * local + 0.5 * (new - angles[-1])
            else:
                new = target
            angles.append(new)
        before = np.min(np.abs(ext - (p0 - spacing)))
        score = (matched, before > tol * spacing)
        if best is None or score > best[0]:
            best = (score, np.array(angles))
    return refine_ticks(best[1], ext, spacing)


def refine_ticks(angles: np.ndarray, peaks: np.ndarray, spacing: float, iters: int = 4) -> np.ndarray:
    """Snap ticks to marks near a smooth curve through them.

    Off-axis distortion varies smoothly round the dial, so tick angle against
    tick index is a smooth curve. Fitting one and taking, for each tick, the
    nearest mark within a quarter spacing of it (or the curve where there is
    none) stops a stray mark dragging the step-by-step walk off course.
    """
    n = len(angles)
    idx = np.arange(n, dtype=float)
    cur = angles.copy()
    for _ in range(iters):
        real = np.array([np.min(np.abs(peaks - a)) <= 0.25 * spacing for a in cur])
        if real.sum() < 6:
            break
        deg = 5 if real.sum() >= 15 else 3
        fit = np.polyval(np.polyfit(idx[real], cur[real], deg), idx)
        new = fit.copy()
        for i, a in enumerate(fit):
            d = np.abs(peaks - a)
            j = int(np.argmin(d))
            if d[j] <= 0.25 * spacing:
                new[i] = peaks[j]
        if np.allclose(new, cur):
            break
        cur = new
    return cur


@dataclass
class DialScale:
    """Angle (deg, clockwise from straight down) to dial value."""

    tick_angles: np.ndarray       # n_ticks, increasing, unwrapped
    first_value: float
    step: float
    turn: float                   # dial value per full revolution

    def __post_init__(self):
        n = len(self.tick_angles)
        vals = self.first_value + self.step * np.arange(n)
        # Across the unprinted gap (last tick round to the first) the spacing
        # is shared out evenly; it is short and nearly linear.
        gap_ticks = int(round((self.turn - (vals[-1] - vals[0])) / self.step))
        end = self.tick_angles[0] + 360.0
        gap_a = np.linspace(self.tick_angles[-1], end, gap_ticks + 1)[1:]
        gap_v = vals[-1] + self.step * np.arange(1, gap_ticks + 1)
        self._a = np.concatenate([self.tick_angles, gap_a])
        self._v = np.concatenate([vals, gap_v])

    def value(self, angle):
        a = np.asarray(angle, float) % 360.0
        a = np.where(a < self._a[0], a + 360.0, a)
        a = np.where(a >= self._a[0] + 360.0, a - 360.0, a)
        return np.interp(a, self._a, self._v) % self.turn


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------

def parse_clock(spec: str) -> list[tuple[float, float]]:
    pts = []
    for item in spec.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        v, t = item.split("=")
        pts.append((float(v), float(t)))
    return sorted(pts)


def clock_map(points: list[tuple[float, float]], linear_tol: float = 0.3):
    """video seconds -> session seconds, and a description of the fit.

    A straight line when every point lies within linear_tol of it (normal
    speed: readings scatter ~0.1 s from display rounding and frame seeking);
    otherwise piecewise-linear through the points (slow motion, off by
    seconds).
    """
    if len(points) < 2:
        raise ValueError("need at least two clock readings")
    v, t = np.array(points).T
    b, a = np.polyfit(v, t, 1)
    resid = t - (a + b * v)
    if np.abs(resid).max() <= linear_tol:
        desc = (f"linear: session_t = {a:.3f} + {b:.5f} * video_t "
                f"(residual sd {resid.std():.3f} s, max {np.abs(resid).max():.3f} s)")
        return (lambda x: a + b * np.asarray(x, float)), desc
    if np.any(np.diff(t) <= 0):
        raise ValueError("clock readings must increase with video time")
    sl0 = (t[1] - t[0]) / (v[1] - v[0])
    sl1 = (t[-1] - t[-2]) / (v[-1] - v[-2])

    def f(x):
        x = np.asarray(x, float)
        y = np.interp(x, v, t)
        y = np.where(x < v[0], t[0] + sl0 * (x - v[0]), y)
        return np.where(x > v[-1], t[-1] + sl1 * (x - v[-1]), y)

    return f, (f"piecewise-linear through {len(v)} points (slope {np.min(np.diff(t) / np.diff(v)):.3f}"
               f"–{np.max(np.diff(t) / np.diff(v)):.3f}; slow motion or variable speed)")


def rec_dot(bgr: np.ndarray):
    """(x, y, diameter) of the red REC marker next to the monitor clock."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    m = (((h < 8) | (h > 170)) & (s > 90) & (v > 120)).astype(np.uint8)
    n, _lab, st, cen = cv2.connectedComponentsWithStats(m)
    best = None
    for i in range(1, n):
        x, y, w, hh, a = st[i]
        if 6 <= a <= 600 and 0.6 < w / max(hh, 1) < 1.6 and a > 0.5 * w * hh:
            pad = max(w, hh) * 2
            patch = v[max(0, y - pad):y + hh + pad, max(0, x - pad):x + w + pad]
            if patch.size and patch.mean() > 170 and (best is None or a > best[0]):
                best = (a, cen[i][0], cen[i][1], max(w, hh))
    return None if best is None else best[1:]


def clock_sheet(video: Path, out: Path, samples: int = 16) -> None:
    """Contact sheet of the on-screen clock at evenly spaced video times."""
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    tiles, last = [], None
    for idx in np.linspace(0, n - 2, samples).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, f = cap.read()
        if not ok:
            continue
        dot = rec_dot(f) or last
        if dot is None:
            continue
        last = dot
        x, y, d = dot
        d = max(d, 6)
        x0, x1 = int(max(0, x - 30 * d)), int(min(f.shape[1], x + 2 * d))
        y0, y1 = int(max(0, y - 3 * d)), int(min(f.shape[0], y + 3 * d))
        crop = f[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        crop = cv2.resize(crop, (480, max(40, int(480 * crop.shape[0] / crop.shape[1]))))
        label = np.full((crop.shape[0], 110, 3), 255, np.uint8)
        cv2.putText(label, f"v={idx / fps:.2f}", (4, crop.shape[0] // 2 + 6), 0, 0.6, (0, 0, 255), 2)
        tiles.append(np.hstack([label, crop]))
    if not tiles:
        raise SystemExit("could not find the on-screen clock (no red REC marker in view)")
    w = max(t.shape[1] for t in tiles)
    tiles = [cv2.copyMakeBorder(t, 0, 4, 0, w - t.shape[1], cv2.BORDER_CONSTANT, value=(255, 255, 255))
             for t in tiles]
    cv2.imwrite(str(out), np.vstack(tiles))


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

@dataclass
class GaugeResult:
    frame: np.ndarray
    video_t: np.ndarray
    angle: np.ndarray
    dial: np.ndarray
    runner_up: np.ndarray
    roll: np.ndarray
    zero: float
    zero_sd: float
    end_dial: float | None
    scale: DialScale
    r_tick: float
    pivot: tuple[float, float]
    bg: np.ndarray
    fps: float
    n_frames: int
    transforms: dict
    invert: bool = False

    def mmhg(self) -> np.ndarray:
        p = (self.dial - self.zero) % self.scale.turn
        return np.where(p > self.scale.turn - 20, p - self.scale.turn, p)

    def keep(self) -> np.ndarray:
        """Frames that pass cleaning: near their neighbours and unambiguous."""
        p = self.mmhg()
        if len(p) < 9:
            return self.runner_up < MAX_RUNNER_UP
        return (np.abs(p - medfilt(p, 9)) < MAX_JUMP_MMHG) & (self.runner_up < MAX_RUNNER_UP)


def _frames(cap, every: int):
    i = 0
    while True:
        if not cap.grab():
            return
        if i % every == 0:
            ok, f = cap.retrieve()
            if ok:
                yield i, f
        i += 1


def read_gauge(video: Path, every: int = 2, first_value: float = 20.0, last_value: float = 300.0,
               step: float = 10.0, turn: float = 320.0, rest_s: float = 2.0,
               zero: float | None = None, ref_time: float | None = None,
               bg_percentile: float = 85.0, invert: bool = False, log=print) -> GaugeResult:
    _need_cv2()
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise SystemExit(f"cannot open {video}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    t_start = time.time()
    log(f"Video     : {video.name}, {n} frames at {fps:.2f} fps ({n / fps:.1f} s)")

    # -- reference frame: a sharp frame with the whole dial in view ----------
    cands = [int(ref_time * fps)] if ref_time is not None else np.linspace(0, n - 2, 24).astype(int)
    best = None
    for idx in cands:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, f = cap.read()
        if not ok:
            continue
        g = to_gray(f, invert)
        c = find_dial(g)
        if c is None or not circle_inside(c, g.shape):
            continue
        sharp = cv2.Laplacian(_crop_scaled(g, c)[0], cv2.CV_64F).var()
        if best is None or sharp > best[0]:
            best = (sharp, idx, g, c)
    if best is None:
        raise SystemExit("could not find the gauge dial in the video")
    _, ref_idx, ref_g, ref_c = best
    aligner = Aligner(ref_g, ref_c)
    log(f"Reference : frame {ref_idx} (t={ref_idx / fps:.1f} s), dial r={ref_c[2]:.0f} px"
        f"  [{time.time() - t_start:.0f} s]")

    # -- pass 1: align every sampled frame; keep transforms, sample canvases --
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    rows, hint = [], None
    bg_stack, stride = [], max(1, (n // every) // 300)
    for k, (idx, f) in enumerate(_frames(cap, every)):
        g = to_gray(f, invert)
        c = find_dial(g, hint) if hint is not None else None
        if c is None:
            c = find_dial(g)
        if c is None or not circle_inside(c, g.shape):
            continue
        hint = c
        al = aligner.align(g, c)
        if al is None:
            continue
        M, inl, roll = al
        rows.append((idx, M, roll))
        if k % stride == 0:
            bg_stack.append(cv2.warpAffine(g, M, (CANVAS, CANVAS), flags=cv2.INTER_LINEAR))
    if len(rows) < 10:
        raise SystemExit(f"only {len(rows)} frames could be aligned")
    rolls = np.array([r[2] for r in rows])
    log(f"Aligned   : {len(rows)} of {int(np.ceil(n / every))} sampled frames; "
        f"camera roll {np.percentile(rolls, 1):+.1f} to {np.percentile(rolls, 99):+.1f} deg"
        f"  [{time.time() - t_start:.0f} s]")

    # -- background and calibration ------------------------------------------
    # Stage 1: a bright percentile hides the needle wherever it is absent most
    # of the time. Stage 2: with rough needle angles from stage 1, a median of
    # the frames with each frame's needle masked out. The median is not
    # thrown by glare that moves across the dial with a hand-held camera, and
    # masking keeps a needle that rests in one place for half the video (the
    # 2026-10-09 trial) out of the background.
    n_ticks = int(round((last_value - first_value) / step)) + 1
    n_major = turn / step
    spacing = 360.0 / n_major
    stack = np.array(bg_stack, np.float32)
    sample = bg_stack[:: max(1, len(bg_stack) // 25)]
    bg = np.percentile(stack, bg_percentile, axis=0).astype(np.float32)
    r_tick = 0.7 * DIAL_R            # provisional; the tick ring is measured below
    pivot = find_pivot(bg, sample, r_tick)
    log(f"  stage-1 background, pivot ({pivot[0]:.0f}, {pivot[1]:.0f})  [{time.time() - t_start:.0f} s]")
    rd0 = NeedleReader(bg, pivot, r_tick)
    yy, xx = np.mgrid[0:CANVAS, 0:CANVAS].astype(np.float32)
    ang_map = np.degrees(np.arctan2(-(xx - pivot[0]), yy - pivot[1])) % 360
    rad_map = np.hypot(xx - pivot[0], yy - pivot[1])
    for k in range(len(stack)):
        a = rd0.read(stack[k])[0]
        da = np.abs((ang_map - a + 180) % 360 - 180)
        tail = np.abs((ang_map - a) % 360 - 180)
        hide = (((da < 8) & (rad_map < 1.05 * r_tick))
                | ((tail < 14) & (rad_map < 0.62 * r_tick))
                | (rad_map < 0.12 * r_tick))
        stack[k][hide] = np.nan
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)     # hub pixels: masked in every frame
        bg = np.nanmedian(stack, axis=0)
    bg = np.where(np.isnan(bg), np.percentile(np.array(bg_stack, np.float32), bg_percentile, axis=0),
                  bg).astype(np.float32)
    pivot = find_pivot(bg, sample, r_tick, centre=pivot, search=2.0)
    ticks, r_tick, matched, rough = calibrate_ticks(bg, pivot, n_ticks, spacing)
    if matched < 0.8 * n_ticks:
        log(f"  ! only {matched}/{n_ticks} ticks matched real marks: check the calibration strip")
    scale = DialScale(ticks, first_value, step, turn)
    sp = np.diff(ticks)
    log(f"Dial      : pivot ({pivot[0]:.0f}, {pivot[1]:.0f}), tick ring r={r_tick:.0f}; "
        f"{matched}/{n_ticks} ticks on real marks ({first_value:g}-{last_value:g}), roughness {rough:.2f} deg, "
        f"{sp.min() * 10 / step:.1f}-{sp.max() * 10 / step:.1f} deg per 10 units"
        f"  [{time.time() - t_start:.0f} s]")

    # -- pass 2: read the needle --------------------------------------------
    reader = NeedleReader(bg, pivot, r_tick)
    lookup = {idx: M for idx, M, _ in rows}
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    out = []
    for idx, f in _frames(cap, every):
        M = lookup.get(idx)
        if M is None:
            continue
        g = to_gray(f, invert)
        cv_ = cv2.warpAffine(g, M, (CANVAS, CANVAS), flags=cv2.INTER_LINEAR)
        a, _s, ru = reader.read(cv_)
        out.append((idx, a, ru))
    cap.release()
    fr, ang, ru = (np.array(x) for x in zip(*out))
    vt = fr / fps
    dial = scale.value(ang)
    roll_by = {idx: r for idx, _, r in rows}

    # -- zero ---------------------------------------------------------------
    if zero is None:
        rest = vt <= vt[0] + rest_s
        if rest.sum() < 3:
            raise SystemExit("not enough frames in the rest window to measure the zero")
        z, zsd = float(np.median(dial[rest])), float(dial[rest].std())
    else:
        z, zsd = float(zero), 0.0
    end = vt >= vt[-1] - rest_s
    end_dial = float(np.median(dial[end])) if end.sum() >= 3 and dial[end].std() < 1.5 else None
    log(f"Zero      : dial {z:.2f} (sd {zsd:.2f})" + ("" if zero is None else " (given)")
        + f"  [{time.time() - t_start:.0f} s]")

    return GaugeResult(fr, vt, ang, dial, ru, np.array([roll_by[i] for i in fr]), z, zsd,
                       end_dial, scale, r_tick, pivot, bg, fps, n, lookup, invert)


def check_figure(video: Path, res: GaugeResult, t_session, out: Path, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    p = res.mmhg()
    keep = res.keep()
    fig = plt.figure(figsize=(14, 11))
    gs = fig.add_gridspec(3, 6, height_ratios=[1.25, 0.55, 1.0])

    # needle overlays on 6 frames across the trace
    cap = cv2.VideoCapture(str(video))
    kept = np.where(keep)[0]
    picks = kept[np.linspace(0, len(kept) - 1, 6).astype(int)] if len(kept) else []
    for j, k in enumerate(picks):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(res.frame[k]))
        ok, f = cap.read()
        ax = fig.add_subplot(gs[0, j])
        ax.axis("off")
        if not ok:
            continue
        # The aligned canvas the needle was read from, with the reading drawn on.
        g = to_gray(f, res.invert)
        img = cv2.warpAffine(g, res.transforms[int(res.frame[k])], (CANVAS, CANVAS))
        ax.imshow(img, cmap="gray")
        a = np.deg2rad(res.angle[k])
        cx, cy = res.pivot
        L = NEEDLE_SPAN[1] * res.r_tick
        ax.plot([cx, cx - np.sin(a) * L], [cy, cy + np.cos(a) * L], color="#e8432f", lw=1.2)
        ax.set_title(f"t={t_session(res.video_t[k]):.1f} s\ndial {res.dial[k]:.1f} → {p[k]:.0f} mmHg",
                     fontsize=8.5)
    cap.release()

    # tick calibration on the unwrapped background
    ax = fig.add_subplot(gs[1, :])
    radii = np.arange(0.5 * res.r_tick, 1.15 * res.r_tick, 0.5)
    pol = polar(res.bg, *res.pivot, radii, 0.25)
    ax.imshow(pol.T, cmap="gray", aspect="auto", extent=[0, 360, radii[-1], radii[0]])
    vals = res.scale.first_value + res.scale.step * np.arange(len(res.scale.tick_angles))
    for a_, v_ in zip(res.scale.tick_angles % 360, vals):
        ax.axvline(a_, color="#e8432f", lw=0.7, alpha=0.8)
        if int(v_) % int(2 * res.scale.step) == 0:
            ax.text(a_, radii[0], f"{v_:g}", color="#e8432f", fontsize=7, ha="center", va="bottom")
    ax.axhline(res.r_tick, color="#2f7be8", lw=0.6, ls=":")
    ax.set_xlabel("angle (deg, clockwise from straight down)", fontsize=8)
    ax.set_ylabel("radius", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.set_title("tick calibration: each red line is a tick the dial value is interpolated from", fontsize=9)

    ax = fig.add_subplot(gs[2, :])
    ts = t_session(res.video_t)
    ax.plot(ts[~keep], p[~keep], "x", color="0.6", ms=3, label=f"dropped ({(~keep).sum()})")
    ax.plot(ts[keep], p[keep], ".", color="k", ms=2.5, label=f"kept ({keep.sum()})")
    ax.set_xlabel("session time (s)")
    ax.set_ylabel("cuff pressure (mmHg, provisional)")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    fig.savefig(out, dpi=100)
    plt.close(fig)


def recording_span(path: Path) -> tuple[float, float] | None:
    t0 = fs = None
    last = None
    with path.open() as fh:
        for line in fh:
            if line.startswith("#"):
                if line.startswith("# session_t0="):
                    t0 = float(line.split("=", 1)[1])
                elif line.startswith("# fs="):
                    fs = float(line.split("=", 1)[1])
            elif line[:1].isdigit():
                last = line
    if t0 is None or fs is None or last is None:
        return None
    return t0, t0 + int(last.split(",")[0]) / fs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--recording", type=Path, help="monitor recording the video goes with; "
                    "outputs are written next to it as <recording>_gauge.csv etc.")
    ap.add_argument("--out", type=Path, help="output CSV (default: from --recording)")
    ap.add_argument("--clock", help='clock readings "video_s=session_s,..." from the clock sheet')
    ap.add_argument("--clock-samples", type=int, default=16)
    ap.add_argument("--every", type=int, default=2, help="read every Nth frame")
    ap.add_argument("--scale", default="20:300:10",
                    help="first:last:step of the labelled major ticks (default 20:300:10)")
    ap.add_argument("--turn", type=float, default=320.0, help="dial value per full revolution")
    ap.add_argument("--rest-s", type=float, default=2.0,
                    help="seconds at the start (cuff at rest) used to measure the zero")
    ap.add_argument("--zero", type=float, help="dial value at 0 mmHg, instead of measuring it")
    ap.add_argument("--ref-time", type=float, help="video second to use as the alignment reference")
    ap.add_argument("--invert", action="store_true",
                    help="light needle and markings on a dark face (e.g. the blue Omron gauge)")
    args = ap.parse_args(argv)
    _need_cv2()

    if args.out is None and args.recording is None:
        ap.error("give --recording or --out")
    base = (args.out.with_suffix("") if args.out else
            args.recording.with_name(args.recording.stem + "_gauge"))
    out_csv = base.with_suffix(".csv")
    sheet = base.with_name(base.name.replace("_gauge", "") + "_clock_sheet.png")
    check = base.with_name(base.name + "_check.png")

    if not args.clock:
        clock_sheet(args.video, sheet, args.clock_samples)
        print(f"Clock sheet written to {sheet}")
        print('Read a few values off it (more for slow motion) and run again with\n'
              '  --clock "v1=t1,v2=t2,..."   (v = video seconds on the left, t = clock reading)')
        return 0

    t_session, desc = clock_map(parse_clock(args.clock))
    print(f"Clock     : {desc}")

    first, last, step = (float(x) for x in args.scale.split(":"))
    res = read_gauge(args.video, every=args.every, first_value=first, last_value=last, step=step,
                     turn=args.turn, rest_s=args.rest_s, zero=args.zero, ref_time=args.ref_time,
                     invert=args.invert)
    p = res.mmhg()
    keep = res.keep()
    ts = t_session(res.video_t)
    if res.end_dial is not None:
        drift = (res.end_dial - res.zero + args.turn / 2) % args.turn - args.turn / 2
        end_note = f"needle steady at the end: dial {res.end_dial:.2f}, {drift:+.2f} mmHg from zero"
    else:
        end_note = "needle not steady at the end of the video: no after-release zero check"
    print(f"End       : {end_note}")
    print(f"Kept      : {keep.sum()} of {len(p)} frames "
          f"(dropped: >{MAX_JUMP_MMHG:g} mmHg from a 9-frame median, or ambiguous needle)")
    k = int(np.argmax(np.where(keep, p, -np.inf)))
    print(f"Peak      : {p[k]:.1f} mmHg at t = {ts[k]:.1f} s")

    if args.recording is not None:
        span = recording_span(args.recording)
        if span:
            lo, hi = max(span[0], ts[keep].min()), min(span[1], ts[keep].max())
            print(f"Recording : {span[0]:.1f}–{span[1]:.1f} s; gauge covers "
                  f"{ts[keep].min():.1f}–{ts[keep].max():.1f} s; overlap {max(0.0, hi - lo):.1f} s")

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as fh:
        fh.write("# source=gauge_video.py (D38): needle read from a phone video of the cuff gauge\n")
        fh.write(f"# clock={desc}\n")
        fh.write(f"# frames_read={len(p)} kept={int(keep.sum())} every={args.every}\n")
        fh.write(f"# camera_roll_deg={np.percentile(res.roll, 1):+.1f}..{np.percentile(res.roll, 99):+.1f}"
                 " (corrected by alignment)\n")
        fh.write(f"# scale={args.scale} turn={args.turn:g} tick_spacing_deg="
                 f"{np.diff(res.scale.tick_angles).min():.1f}..{np.diff(res.scale.tick_angles).max():.1f}\n")
        fh.write(f"# gauge_zero_dial={res.zero:.2f} (sd {res.zero_sd:.2f}); mmhg = (dial - zero) mod turn\n")
        fh.write(f"# end_of_video: {end_note}\n")
        fh.write("# PROVISIONAL: gauge span unverified (offset-corrected only)\n")
        fh.write("t_s,mmhg,dial,video_frame\n")
        for a, b, c, e in zip(ts[keep], p[keep], res.dial[keep], res.frame[keep]):
            fh.write(f"{a:.3f},{b:.1f},{c:.1f},{int(e)}\n")
    print(f"Wrote {out_csv}")

    # Titled by the recording, not the video file: video names can carry a
    # subject's name, and check figures are committed next to the data.
    label = args.recording.stem if args.recording else out_csv.stem
    check_figure(args.video, res, t_session, check,
                 f"{label}: gauge read check — zero {res.zero:.1f} on the dial, "
                 f"pressure provisional (span unverified)")
    print(f"Check figure: {check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
