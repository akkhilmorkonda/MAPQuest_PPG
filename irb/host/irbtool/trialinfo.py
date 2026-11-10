"""Trial info: checking what the operator types, run IDs, inflation targets.

DESIGN 5.2 lists the fields; DESIGN 2.1 explains the inflation targets.
Every function here is pure, so it is tested without a GUI or a board.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SUBJECT_RE = re.compile(r"^S\d{3}$")
RUN_RE = re.compile(r"^(S\d{3})-R(\d{2,})(?:-(\d+))?$")

FITZPATRICK = ("I", "II", "III", "IV", "V", "VI", "not recorded")

# AHA cuff sizes by mid-upper-arm circumference, cm (DESIGN 2.1).
CUFF_SIZES = {
    "small adult": (22.0, 26.0),
    "adult": (27.0, 34.0),
    "large adult": (35.0, 44.0),
    "adult thigh": (45.0, 52.0),
}

# (low, high, unit) for every numeric field.
LIMITS = {
    "distal_cm": (0.0, 30.0, "cm"),
    "proximal_cm": (0.0, 30.0, "cm"),
    "height_cm": (100.0, 230.0, "cm"),
    "weight_kg": (25.0, 300.0, "kg"),
    "arm_circumference_cm": (15.0, 60.0, "cm"),
    "bladder_width_cm": (5.0, 25.0, "cm"),
    "session_systolic": (70.0, 250.0, "mmHg"),
    "session_diastolic": (40.0, 150.0, "mmHg"),
    "ref_systolic": (70.0, 250.0, "mmHg"),
    "ref_diastolic": (40.0, 150.0, "mmHg"),
    "ref_map": (40.0, 200.0, "mmHg"),
}

# Needed before Start. The after-run reference may come later (D45 flow).
REQUIRED_BEFORE_START = (
    "subject_id", "run_id", "distal_cm", "proximal_cm", "height_cm", "weight_kg",
    "arm_circumference_cm", "cuff_size", "fitzpatrick", "session_systolic",
)

# Inflation margins over the occlusion point (DESIGN 2.1, AHA 20-30 mmHg).
TARGET_MARGIN = 30.0
RUN_MIN_MARGIN = 20.0


@dataclass
class Checked:
    values: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)     # block the action
    warnings: list = field(default_factory=list)   # shown, do not block

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Run IDs
# ---------------------------------------------------------------------------

def first_run_id(subject: str) -> str:
    return f"{subject}-R01"


def next_run_id(run_id: str) -> str:
    """S004-R03 -> S004-R04; a suffixed S004-R03-2 -> S004-R04."""
    m = RUN_RE.match(run_id or "")
    if not m:
        return run_id
    subject, n = m.group(1), m.group(2)
    return f"{subject}-R{int(n) + 1:0{len(n)}d}"


def unique_run_id(run_id: str, taken: set[str]) -> str:
    """The run ID, or with -2, -3 ... appended if it is already used."""
    if run_id not in taken:
        return run_id
    k = 2
    while f"{run_id}-{k}" in taken:
        k += 1
    return f"{run_id}-{k}"


# ---------------------------------------------------------------------------
# Cuff size and inflation targets
# ---------------------------------------------------------------------------

def recommended_cuff(circumference_cm: float) -> str | None:
    for name, (lo, hi) in CUFF_SIZES.items():
        if lo <= circumference_cm <= hi:
            return name
    return None


def cuff_size_warning(circumference_cm: float, cuff_size: str) -> str | None:
    rec = recommended_cuff(circumference_cm)
    if rec is None:
        return (f"arm circumference {circumference_cm:g} cm is outside every AHA cuff band "
                f"(22-52 cm): check the cuff")
    if cuff_size != rec:
        lo, hi = CUFF_SIZES[rec]
        return (f"{cuff_size!r} cuff on a {circumference_cm:g} cm arm: AHA sizing says "
                f"{rec!r} ({lo:g}-{hi:g} cm). An undersized cuff needs more pressure to occlude")
    return None


@dataclass(frozen=True)
class Targets:
    target: float      # inflate to about this
    run_min: float     # RUN accepted at or above this
    basis: str         # what they were computed from
    clamped: bool      # limited by the firmware's p_max / p_stop


def inflation_targets(reference: float, basis: str, p_max: float, p_stop: float) -> Targets:
    """Target = reference + 30, Run minimum = reference + 20 (DESIGN 2.1).

    ``reference`` is the session-start systolic before the first Doppler
    occlusion mark, then the pressure at which the Doppler went silent.
    Both are kept inside what the firmware will accept: Run minimum in
    [p_stop + 10, p_max - 10], target no higher than p_max - 10.
    """
    target = reference + TARGET_MARGIN
    run_min = reference + RUN_MIN_MARGIN
    lo, hi = p_stop + 10.0, p_max - 10.0
    clamped = False
    if target > hi:
        target, clamped = hi, True
    if run_min > hi:
        run_min, clamped = hi, True
    if run_min < lo:
        run_min, clamped = lo, True
    return Targets(round(target, 1), round(run_min, 1), basis, clamped)


# ---------------------------------------------------------------------------
# Field checks
# ---------------------------------------------------------------------------

def _number(raw: dict, key: str, out: Checked) -> None:
    text = str(raw.get(key, "") or "").strip()
    if not text:
        return
    try:
        v = float(text)
    except ValueError:
        out.errors.append(f"{key}: '{text}' is not a number")
        return
    lo, hi, unit = LIMITS[key]
    if not lo <= v <= hi:
        out.errors.append(f"{key}: {v:g} {unit} is outside {lo:g}-{hi:g} {unit}")
        return
    out.values[key] = v


def check(raw: dict, *, before_start: bool) -> Checked:
    """Check the typed trial info. ``before_start`` requires the pre-run fields."""
    out = Checked()

    subject = str(raw.get("subject_id", "") or "").strip().upper()
    if subject:
        if SUBJECT_RE.match(subject):
            out.values["subject_id"] = subject
        else:
            out.errors.append(f"subject_id: '{subject}' must look like S004 (coded ID, never a name)")

    run_id = str(raw.get("run_id", "") or "").strip().upper()
    if run_id:
        m = RUN_RE.match(run_id)
        if not m:
            out.errors.append(f"run_id: '{run_id}' must look like S004-R01")
        elif subject and m.group(1) != subject:
            out.errors.append(f"run_id: '{run_id}' does not belong to subject {subject}")
        else:
            out.values["run_id"] = run_id

    for key in LIMITS:
        _number(raw, key, out)

    cuff = str(raw.get("cuff_size", "") or "").strip().lower()
    if cuff:
        if cuff in CUFF_SIZES:
            out.values["cuff_size"] = cuff
        else:
            out.errors.append(f"cuff_size: '{cuff}' must be one of {', '.join(CUFF_SIZES)}")

    fitz = str(raw.get("fitzpatrick", "") or "").strip()
    if fitz:
        if fitz in FITZPATRICK:
            out.values["fitzpatrick"] = fitz
        else:
            out.errors.append(f"fitzpatrick: '{fitz}' must be one of {', '.join(FITZPATRICK)}")

    for prefix in ("session", "ref"):
        s, d = out.values.get(f"{prefix}_systolic"), out.values.get(f"{prefix}_diastolic")
        if s is not None and d is not None and s <= d:
            out.errors.append(f"{prefix}: systolic must be higher than diastolic")

    v = out.values
    if "ref_systolic" in v and "ref_diastolic" in v and "ref_map" not in v:
        v["ref_map_computed"] = round(v["ref_diastolic"] + (v["ref_systolic"] - v["ref_diastolic"]) / 3.0, 1)

    if "arm_circumference_cm" in v and "cuff_size" in v:
        w = cuff_size_warning(v["arm_circumference_cm"], v["cuff_size"])
        if w:
            out.warnings.append(w)

    if before_start:
        missing = [k for k in REQUIRED_BEFORE_START if k not in v]
        if missing:
            out.errors.append("needed before Start: " + ", ".join(missing))

    notes = str(raw.get("notes", "") or "").strip()
    if notes:
        v["notes"] = notes
    return out


def header_lines(values: dict) -> list[str]:
    """Trial header lines. The keys ``cuff_trial.py`` already reads keep their
    names: run_id, ref_systolic_mmhg, ref_diastolic_mmhg, ref_map_mmhg and
    cuff_to_sensor_cm (the distal distance)."""
    v = values
    out = []

    def put(key, value, fmt="{:g}"):
        if value is not None:
            out.append(f"# {key}={fmt.format(value) if not isinstance(value, str) else value}")

    put("subject_id", v.get("subject_id"))
    put("run_id", v.get("run_id"))
    put("cuff_to_sensor_cm", v.get("distal_cm"))
    put("proximal_cuff_to_sensor_cm", v.get("proximal_cm"))
    put("height_cm", v.get("height_cm"))
    put("weight_kg", v.get("weight_kg"))
    put("arm_circumference_cm", v.get("arm_circumference_cm"))
    put("cuff_size", (v.get("cuff_size") or "").replace(" ", "_") or None)
    put("bladder_width_cm", v.get("bladder_width_cm"))
    put("fitzpatrick", (v.get("fitzpatrick") or "").replace(" ", "_") or None)
    put("session_systolic_mmhg", v.get("session_systolic"))
    put("session_diastolic_mmhg", v.get("session_diastolic"))
    put("ref_method", "standard_cuff_after_run" if "ref_systolic" in v else None)
    put("ref_systolic_mmhg", v.get("ref_systolic"))
    put("ref_diastolic_mmhg", v.get("ref_diastolic"))
    if "ref_map" in v:
        out.append(f"# ref_map_mmhg={v['ref_map']:g}  (from the monitor)")
    elif "ref_map_computed" in v:
        out.append(f"# ref_map_mmhg={v['ref_map_computed']:.1f}  (diastolic + pulse pressure / 3)")
    # Live operator marks (O / D keys); the video reading is the reference.
    put("doppler_occlusion_mark_mmhg", v.get("doppler_occlusion_mark_mmhg"))
    put("doppler_return_mark_mmhg", v.get("doppler_return_mark_mmhg"))
    if v.get("notes"):
        # The existing header parser splits on whitespace and reads key=value
        # tokens: an '=' inside a note would forge a key. Full text is in run.json.
        out.append("# notes=" + v["notes"].replace("=", ":").replace("\n", " "))
    return out
