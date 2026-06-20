#!/usr/bin/env python3
"""Flatten the rAthena leveling curve in db/re/job_exp.yml.

The official renewal curve grows near-exponentially, so high levels take
disproportionately longer than low levels. This script compresses the curve's
dynamic range while preserving its natural monotonic shape, the level-1 anchor,
and the max-level sentinel ("cap") values.

For each BaseExp / JobExp list, anchored on its level-1 value E1:

    NewExp(n) = round( E1 * (OrigExp(n) / E1) ** FLATTEN_K )

FLATTEN_K = 1.0 reproduces the official curve; lower values flatten it more.

The script always reads from a pristine ".orig" copy (created on first run),
so it is idempotent and safe to re-run with a different FLATTEN_K.
"""

import os
import re
import shutil

# --- Tunables --------------------------------------------------------------
# Curve compression strength. 1.0 = official shape, lower = flatter tail.
FLATTEN_K = 0.80

# Paths are resolved relative to the repo root (parent of this tools/ dir).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(REPO_ROOT, "db", "re", "job_exp.yml")
PRISTINE = TARGET + ".orig"
# ---------------------------------------------------------------------------

LEVEL_RE = re.compile(r"^(\s*)-\s*Level:\s*(\d+)\s*$")
EXP_RE = re.compile(r"^(\s*)Exp:\s*(\d+)\s*$")
MAXBASE_RE = re.compile(r"^\s*MaxBaseLevel:\s*(\d+)\s*$")
MAXJOB_RE = re.compile(r"^\s*MaxJobLevel:\s*(\d+)\s*$")
BASEEXP_RE = re.compile(r"^\s*BaseExp:\s*$")
JOBEXP_RE = re.compile(r"^\s*JobExp:\s*$")


def flatten(orig, anchor):
    """Apply the compression formula. Returns an int >= 1."""
    if orig <= anchor:
        return orig
    new = anchor * (orig / anchor) ** FLATTEN_K
    return max(1, round(new))


def main():
    if not os.path.exists(PRISTINE):
        if not os.path.exists(TARGET):
            raise SystemExit(f"Cannot find {TARGET}")
        shutil.copyfile(TARGET, PRISTINE)
        print(f"Created pristine backup: {PRISTINE}")

    with open(PRISTINE, "r", encoding="utf-8", newline="") as fh:
        lines = fh.readlines()

    out = []
    # Per-group / per-list state
    max_base = None
    max_job = None
    section = None        # "base" or "job"
    cap = None            # cap level for the current section
    anchor = None         # level-1 Exp for the current section
    pending_level = None  # level number whose Exp line we expect next

    preview = []          # (level, orig, new) rows for the 275-cap base curve

    for line in lines:
        # New job group resets per-group state.
        if re.match(r"^\s*-\s*Jobs:\s*$", line):
            max_base = max_job = None
            section = None
            cap = None
            anchor = None
            pending_level = None
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
            out.append(line)
            continue

        if JOBEXP_RE.match(line):
            section = "job"
            cap = max_job
            anchor = None
            pending_level = None
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

            if cap is not None and level >= cap:
                new = orig  # preserve the max-level sentinel
            else:
                new = flatten(orig, anchor)

            out.append(f"{indent}Exp: {new}\n")

            # Record preview rows for the 275-cap base curve if present.
            if section == "base" and cap == 275:
                preview.append((level, orig, new))
            continue

        out.append(line)

    with open(TARGET, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(out)

    print(f"Rewrote {TARGET} with FLATTEN_K={FLATTEN_K}")

    if preview:
        sample_levels = {1, 50, 100, 150, 200, 250, 256, 270}
        print("\nPreview (275-cap base curve): level | original -> new")
        for level, orig, new in preview:
            if level in sample_levels:
                print(f"  {level:>4} | {orig:>15,} -> {new:>15,}")


if __name__ == "__main__":
    main()
