#!/usr/bin/env python3
"""Flatten the EXP curves in db/re/job_exp.yml from the pristine original.

The official renewal curve grows near-exponentially, so high levels take
disproportionately longer than low levels. This script reads the official curves
from the pristine ``db/re/job_exp.yml.orig`` backup and rewrites the ``BaseExp``
and ``JobExp`` values, compressing each curve's dynamic range while preserving
its monotonic shape and each list's level-1 anchor.

For every BaseExp / JobExp list, anchored on its level-1 value E1:

    NewExp(n) = round( E1 * (OrigExp(n) / E1) ** FLATTEN_K )

with FLATTEN_K < 1.0 (lower = flatter; 1.0 reproduces the official curve).

Cap level "follows the curve": the source stores each list's max level as a
sentinel (e.g. 999 / 99999 / 9999999 / 999999999 / 999999999999). Flattening
that sentinel directly would leave an artificial jump at the top, so the cap is
first replaced with a value extrapolated from the geometric trend of the two
preceding levels, then flattened like any other level.

The script always reads from the ".orig" copy (created on first run), so it is
idempotent and safe to re-run with a different FLATTEN_K.
"""

import os
import re
import shutil

# --- Tunables --------------------------------------------------------------
# Compression exponent applied to both base and job curves. 1.0 = official
# shape, lower = flatter.
FLATTEN_K = 0.5
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(REPO_ROOT, "db", "re", "job_exp.yml")
PRISTINE = TARGET + ".orig"

JOBS_RE = re.compile(r"^\s*-\s*Jobs:\s*$")
LEVEL_RE = re.compile(r"^(\s*)-\s*Level:\s*(\d+)\s*$")
EXP_RE = re.compile(r"^(\s*)Exp:\s*(\d+)\s*$")
MAXBASE_RE = re.compile(r"^\s*MaxBaseLevel:\s*(\d+)\s*$")
MAXJOB_RE = re.compile(r"^\s*MaxJobLevel:\s*(\d+)\s*$")
BASEEXP_RE = re.compile(r"^\s*BaseExp:\s*$")
JOBEXP_RE = re.compile(r"^\s*JobExp:\s*$")


def flatten(orig, anchor):
    """Single-exponent compression anchored on the level-1 value. Returns int >= 1."""
    if orig <= anchor:
        return orig
    return max(1, round(anchor * (orig / anchor) ** FLATTEN_K))


def main():
    if not os.path.exists(PRISTINE):
        if not os.path.exists(TARGET):
            raise SystemExit(f"Cannot find {TARGET}")
        shutil.copyfile(TARGET, PRISTINE)
        print(f"Created pristine backup: {PRISTINE}")

    with open(PRISTINE, "r", encoding="utf-8", newline="") as fh:
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

            new = flatten(orig_eff, anchor)
            out.append(f"{indent}Exp: {new}\n")

            prev2_orig = prev_orig
            prev_orig = orig_eff

            if section == "base" and cap == 275:
                preview.append((level, orig, new))
            continue

        out.append(line)

    with open(TARGET, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(out)

    print(f"Rewrote {TARGET} base + job curves with FLATTEN_K={FLATTEN_K}")

    if preview:
        sample_levels = {1, 50, 99, 150, 199, 274, 275}
        print("\nPreview (275-cap base curve): level | original -> new")
        for level, orig, new in preview:
            if level in sample_levels:
                print(f"  {level:>4} | {orig:>15,} -> {new:>15,}")


if __name__ == "__main__":
    main()
