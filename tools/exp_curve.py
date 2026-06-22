#!/usr/bin/env python3
"""Flatten the EXP curves into the JobDatabase import override.

The official renewal curve grows near-exponentially, so high levels take
disproportionately longer than low levels. This script reads the original curves
from ``db/re/job_exp.yml`` and writes the flattened ``BaseExp`` / ``JobExp``
values to ``db/import/job_stats.yml`` (the JobDatabase import override, loaded
after the base ``db/re/job_exp.yml``), compressing each curve's dynamic range
while preserving its monotonic shape and level-1 anchor.

``db/re/job_exp.yml`` is the single source of truth and must hold the original
(un-flattened) curve. This script only reads it and only writes the separate
import override, so it can never compound the flattening on top of its own
output, mirroring how config overrides live under ``conf/import/``.

For every BaseExp / JobExp list, anchored on its level-1 value E1:

    NewExp(n) = round( E1 * (OrigExp(n) / E1) ** K )

with K < 1.0 (lower = flatter; 1.0 reproduces the official curve). The base curve
uses BASE_FLATTEN_K and all job curves use JOB_FLATTEN_K, so job leveling can be
paced separately from base leveling.

Cap level "follows the curve": the source stores each list's max level as a
sentinel (e.g. 999 / 99999 / 9999999 / 999999999 / 999999999999). Flattening
that sentinel directly would leave an artificial jump at the top, so the cap is
first replaced with a value extrapolated from the geometric trend of the two
preceding levels, then flattened like any other level.

The script always reads from db/re/job_exp.yml, so it is idempotent and safe to
re-run with different exponents. Note: regenerating rewrites
db/import/job_stats.yml verbatim from the source structure, so any manual header
edits in that file are not preserved.
"""

import os
import re

# --- Tunables --------------------------------------------------------------
# Compression exponents. 1.0 = official shape, lower = flatter. Base and job
# curves are flattened independently so job leveling can be paced separately from
# base leveling.
BASE_FLATTEN_K = 0.5
JOB_FLATTEN_K = 0.475
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Original curve and single source of truth. Must hold the un-flattened values;
# this script only reads it and never writes to it.
SOURCE = os.path.join(REPO_ROOT, "db", "re", "job_exp.yml")
# Flattened output goes to the JobDatabase import override slot.
TARGET = os.path.join(REPO_ROOT, "db", "import", "job_stats.yml")

JOBS_RE = re.compile(r"^\s*-\s*Jobs:\s*$")
LEVEL_RE = re.compile(r"^(\s*)-\s*Level:\s*(\d+)\s*$")
EXP_RE = re.compile(r"^(\s*)Exp:\s*(\d+)\s*$")
MAXBASE_RE = re.compile(r"^\s*MaxBaseLevel:\s*(\d+)\s*$")
MAXJOB_RE = re.compile(r"^\s*MaxJobLevel:\s*(\d+)\s*$")
BASEEXP_RE = re.compile(r"^\s*BaseExp:\s*$")
JOBEXP_RE = re.compile(r"^\s*JobExp:\s*$")

# Job curves to preview after a run, keyed by (MaxJobLevel, level-1 anchor).
JOB_PREVIEW_LABELS = {
    (50, 100): "50-cap job curve (High 1st, anchor 100)",
    (70, 1354): "70-cap job curve (High 2nd trans, anchor 1354)",
}


def flatten(orig, anchor, k):
    """Single-exponent compression anchored on the level-1 value. Returns int >= 1."""
    if orig <= anchor:
        return orig
    return max(1, round(anchor * (orig / anchor) ** k))


def main():
    if not os.path.exists(SOURCE):
        raise SystemExit(f"Cannot find source curve {SOURCE}")

    with open(SOURCE, "r", encoding="utf-8", newline="") as fh:
        lines = fh.readlines()

    out = []
    section = None        # "base" or "job"
    max_base = None       # MaxBaseLevel for the current group
    max_job = None        # MaxJobLevel for the current group
    cap = None            # cap level for the current list
    anchor = None         # level-1 Exp for the current list
    pending_level = None  # level number whose Exp line we expect next
    prev_orig = None      # original Exp of the previous level
    prev2_orig = None     # original Exp two levels back

    preview = []          # (level, orig, new) rows for the 275-cap base curve
    job_preview = {}      # {(cap, anchor): [(level, orig, new), ...]} for select job curves

    for line in lines:
        if JOBS_RE.match(line):
            section = None
            max_base = max_job = None
            cap = None
            anchor = None
            pending_level = None
            prev_orig = prev2_orig = None
            out.append(line)
            continue

        m = MAXBASE_RE.match(line)
        if m:
            max_base = int(m.group(1))
            out.append(line)
            continue

        m = MAXJOB_RE.match(line)
        if m:
            max_job = int(m.group(1))
            out.append(line)
            continue

        if BASEEXP_RE.match(line):
            section = "base"
            cap = max_base
            anchor = None
            pending_level = None
            prev_orig = prev2_orig = None
            out.append(line)
            continue

        if JOBEXP_RE.match(line):
            section = "job"
            cap = max_job
            anchor = None
            pending_level = None
            prev_orig = prev2_orig = None
            out.append(line)
            continue

        m = LEVEL_RE.match(line)
        if m and section:
            pending_level = int(m.group(2))
            out.append(line)
            continue

        m = EXP_RE.match(line)
        if m and section and pending_level is not None:
            indent = m.group(1)
            orig = int(m.group(2))
            level = pending_level
            pending_level = None

            if anchor is None:
                # First Exp value in this list is the level-1 anchor.
                anchor = orig

            # The cap level is a sentinel in the source; replace it with a
            # curve-following extrapolation of the prior two levels before
            # flattening, so the top of the curve stays smooth.
            orig_eff = orig
            if cap is not None and level == cap and prev_orig and prev2_orig:
                orig_eff = max(prev_orig + 1, round(prev_orig * prev_orig / prev2_orig))

            k = BASE_FLATTEN_K if section == "base" else JOB_FLATTEN_K
            new = flatten(orig_eff, anchor, k)
            out.append(f"{indent}Exp: {new}\n")

            prev2_orig = prev_orig
            prev_orig = orig_eff

            if section == "base" and cap == 275:
                preview.append((level, orig, new))
            elif section == "job" and (cap, anchor) in JOB_PREVIEW_LABELS:
                job_preview.setdefault((cap, anchor), []).append((level, orig, new))
            continue

        out.append(line)

    with open(TARGET, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(out)

    print(
        f"Rewrote {TARGET} curves with "
        f"BASE_FLATTEN_K={BASE_FLATTEN_K}, JOB_FLATTEN_K={JOB_FLATTEN_K}"
    )

    if preview:
        sample_levels = {1, 50, 99, 150, 199, 274, 275}
        print("\nPreview (275-cap base curve): level | original -> new")
        for level, orig, new in preview:
            if level in sample_levels:
                print(f"  {level:>4} | {orig:>15,} -> {new:>15,}")

    for (cap, anchor), label in JOB_PREVIEW_LABELS.items():
        rows = job_preview.get((cap, anchor))
        if not rows:
            continue
        # Show level 1, the cap, and a spread in between.
        sample_levels = {1, 10, 25, cap // 2, cap - 1, cap}
        total = sum(new for _, _, new in rows[:-1])  # cumulative to reach the cap
        print(f"\nPreview ({label}): level | original -> new")
        for level, orig, new in rows:
            if level in sample_levels:
                print(f"  {level:>4} | {orig:>15,} -> {new:>15,}")
        print(f"  cumulative EXP to reach Job {cap}: {total:,}")


if __name__ == "__main__":
    main()
