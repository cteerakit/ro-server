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

with K < 1.0 flatter than official and K = 1.0 reproducing the official curve
(K > 1.0 is steeper than official). The base curve uses BASE_FLATTEN_K. Job
curves use a *per-tier* K: tiers listed in JOB_TARGETS have their K solved so the
job cap is reached at a chosen base level, tiers in JOB_FLATTEN_K_MIRROR copy
another tier's solved K, and everything else uses JOB_FLATTEN_K_DEFAULT.

Per-tier job K is solved from base-level targets. A kill grants base and job EXP
at the same time and job level resets on each job change while base level keeps
climbing, so each job is leveled over a base-level segment [base_start, base_end].
Assuming a mob gives base:job EXP in ratio MOB_BASE_JOB_RATIO (R), the job cap is
reached at base_end when:

    cumulative_job_exp(job_cap) = ( cum_base(base_end) - cum_base(base_start) ) * R

Both sides use flattened curves; because flattened EXP is monotonic in K, K is
found by binary search.

Cap level "follows the curve": the source stores each list's max level as a
sentinel (e.g. 999 / 99999 / 9999999 / 999999999 / 999999999999). Flattening
that sentinel directly would leave an artificial jump at the top, so the cap is
first replaced with a value extrapolated from the geometric trend of the two
preceding levels, then flattened like any other level. (The cap level is not
counted when summing cumulative job EXP "to reach" the cap.)

The script always reads from db/re/job_exp.yml, so it is idempotent and safe to
re-run with different targets. Note: regenerating rewrites
db/import/job_stats.yml verbatim from the source structure, so any manual header
edits in that file are not preserved.
"""

import os
import re

# --- Tunables --------------------------------------------------------------
# Base curve compression exponent. 1.0 = official shape, lower = flatter.
BASE_FLATTEN_K = 0.5

# Assumed mob base:job EXP accrual ratio when mapping job pace to base level.
# R = 1.0 means a kill grants equal base and job EXP. Raise it if your mobs give
# less job EXP than base EXP (which pushes job caps to higher base levels).
MOB_BASE_JOB_RATIO = 1.0

# K applied to any job tier that has neither a target nor a mirror. Every tier
# currently has one, so this is an unused safety fallback.
JOB_FLATTEN_K_DEFAULT = 0.5

# Per-tier base-level targets, keyed by a representative job in the tier's group.
#   (base_curve_rep, base_start, base_end)
# The tier's job cap should be reached at base_end, having entered the tier at
# base_start. base_curve_rep names a job in the base-exp group used while
# leveling this tier (Novice = normal base-99, Novice_High = trans base-99,
# Rune_Knight = 3rd base-200, Dragon_Knight = 4th base-275, Summoner = base-200).
JOB_TARGETS = {
    "Swordman":      ("Novice",        10,  60),
    "Knight":        ("Novice",        60,  90),
    "Lord_Knight":   ("Novice_High",   60,  90),
    "Rune_Knight":   ("Rune_Knight",   99,  180),
    "Summoner":      ("Summoner",      1,   180),
    "Dragon_Knight": ("Dragon_Knight", 200, 272),
    "Swordman_High": ("Novice_High",   10,  60),
    "Super_Novice":  ("Novice",        1,   90),
    "Super_Novice_E":("Rune_Knight",   1,   160),
}

# Tiers that copy another tier's solved K. rep -> source rep (a JOB_TARGETS key).
JOB_FLATTEN_K_MIRROR = {
    "Star_Gladiator": "Knight",
    "Gunslinger":     "Lord_Knight",
    "Kagerou":        "Lord_Knight",
}

# Binary-search bounds for the solved K.
K_SEARCH_LO, K_SEARCH_HI = 0.05, 5.0
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Original curve and single source of truth. Must hold the un-flattened values;
# this script only reads it and never writes to it.
SOURCE = os.path.join(REPO_ROOT, "db", "re", "job_exp.yml")
# Flattened output goes to the JobDatabase import override slot.
TARGET = os.path.join(REPO_ROOT, "db", "import", "job_stats.yml")

JOBS_RE = re.compile(r"^\s*-\s*Jobs:\s*$")
JOBNAME_RE = re.compile(r"^\s+(\w+):\s*(true|false)\s*$")
LEVEL_RE = re.compile(r"^(\s*)-\s*Level:\s*(\d+)\s*$")
EXP_RE = re.compile(r"^(\s*)Exp:\s*(\d+)\s*$")
MAXBASE_RE = re.compile(r"^\s*MaxBaseLevel:\s*(\d+)\s*$")
MAXJOB_RE = re.compile(r"^\s*MaxJobLevel:\s*(\d+)\s*$")
BASEEXP_RE = re.compile(r"^\s*BaseExp:\s*$")
JOBEXP_RE = re.compile(r"^\s*JobExp:\s*$")


def flatten(orig, anchor, k):
    """Single-exponent compression anchored on the level-1 value. Returns int >= 1."""
    if orig <= anchor:
        return orig
    return max(1, round(anchor * (orig / anchor) ** k))


def effective_cap_value(level, cap, orig, prev, prev2):
    """Replace the sentinel cap value with a geometric extrapolation of the trend."""
    if cap is not None and level == cap and prev and prev2:
        return max(prev + 1, round(prev * prev / prev2))
    return orig


def flattened_pairs(pairs, cap, k):
    """Yield (level, flattened_exp) for a curve, applying cap extrapolation."""
    anchor = None
    prev = prev2 = None
    out = []
    for level, orig in pairs:
        if anchor is None:
            anchor = orig
        oe = effective_cap_value(level, cap, orig, prev, prev2)
        out.append((level, flatten(oe, anchor, k)))
        prev2 = prev
        prev = oe
    return out


def parse_groups(lines):
    """Parse every ``- Jobs:`` block into its job set, caps, and exp lists."""
    groups = []
    cur = None
    section = None
    pending = None
    for line in lines:
        if JOBS_RE.match(line):
            cur = {"jobs": set(), "maxbase": None, "maxjob": None, "base": [], "job": []}
            groups.append(cur)
            section = None
            pending = None
            continue
        if cur is None:
            continue
        m = MAXBASE_RE.match(line)
        if m:
            cur["maxbase"] = int(m.group(1))
            continue
        m = MAXJOB_RE.match(line)
        if m:
            cur["maxjob"] = int(m.group(1))
            continue
        if BASEEXP_RE.match(line):
            section = "base"
            pending = None
            continue
        if JOBEXP_RE.match(line):
            section = "job"
            pending = None
            continue
        m = LEVEL_RE.match(line)
        if m and section:
            pending = int(m.group(2))
            continue
        m = EXP_RE.match(line)
        if m and section and pending is not None:
            cur[section].append((pending, int(m.group(2))))
            pending = None
            continue
        m = JOBNAME_RE.match(line)
        if m and section is None and m.group(2) == "true":
            cur["jobs"].add(m.group(1))
    return groups


def find_group(groups, rep, kind):
    """First group whose job set contains ``rep`` and that has a ``kind`` curve."""
    for g in groups:
        if rep in g["jobs"] and g[kind]:
            return g
    raise SystemExit(f"No group with job '{rep}' and a {kind} curve")


def base_cumulative(groups, rep):
    """Cumulative flattened base EXP. cum[L] = EXP earned reaching base level L."""
    g = find_group(groups, rep, "base")
    fl = dict(flattened_pairs(g["base"], g["maxbase"], BASE_FLATTEN_K))
    maxl = max(fl)
    cum = [0] * (maxl + 2)
    for L in range(2, maxl + 2):
        cum[L] = cum[L - 1] + fl.get(L - 1, 0)
    return cum


def job_cumulative_to_cap(g, cap, k):
    """Flattened job EXP needed to reach the job cap (cap level excluded)."""
    return sum(v for level, v in flattened_pairs(g["job"], cap, k) if level < cap)


def solve_k(g, cap, rhs):
    """Binary-search K so job EXP to cap == rhs. Returns (k, feasible)."""
    lo, hi = K_SEARCH_LO, K_SEARCH_HI
    if job_cumulative_to_cap(g, cap, hi) < rhs:
        return hi, False
    if job_cumulative_to_cap(g, cap, lo) > rhs:
        return lo, False
    for _ in range(80):
        mid = (lo + hi) / 2
        if job_cumulative_to_cap(g, cap, mid) < rhs:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, True


def crossover_base(cum, start, job_exp):
    """Base level at which cumulative base EXP from ``start`` first covers job_exp."""
    for L in range(start, len(cum)):
        if cum[L] - cum[start] >= job_exp:
            return L
    return None


def group_match(g, table):
    """Return the first rep in ``table`` that is present in the group's job set."""
    for rep in table:
        if rep in g["jobs"]:
            return rep
    return None


def main():
    if not os.path.exists(SOURCE):
        raise SystemExit(f"Cannot find source curve {SOURCE}")

    with open(SOURCE, "r", encoding="utf-8", newline="") as fh:
        lines = fh.readlines()

    groups = parse_groups(lines)

    # Solve K for every targeted tier.
    solved = {}        # rep -> k
    report = []        # (rep, cap, k, feasible, job_exp, base_seg, reached_base, target_base)
    for rep, (bcurve, start, end) in JOB_TARGETS.items():
        g = find_group(groups, rep, "job")
        cap = g["maxjob"]
        cum = base_cumulative(groups, bcurve)
        rhs = (cum[end] - cum[start]) * MOB_BASE_JOB_RATIO
        k, ok = solve_k(g, cap, rhs)
        solved[rep] = k
        job_exp = job_cumulative_to_cap(g, cap, k)
        reached = crossover_base(cum, start, job_exp)
        report.append((rep, cap, k, ok, job_exp, rhs, reached, end))

    # Resolve K per group: target, then mirror, then default.
    group_k = []
    for g in groups:
        if not g["job"]:
            group_k.append(None)
            continue
        rep = group_match(g, JOB_TARGETS)
        if rep is not None:
            group_k.append(solved[rep])
            continue
        rep = group_match(g, JOB_FLATTEN_K_MIRROR)
        if rep is not None:
            group_k.append(solved.get(JOB_FLATTEN_K_MIRROR[rep], JOB_FLATTEN_K_DEFAULT))
            continue
        group_k.append(JOB_FLATTEN_K_DEFAULT)

    # Rewrite the file, flattening each curve with its resolved K.
    out = []
    gi = -1
    section = None
    cap = None
    anchor = None
    pending_level = None
    prev = prev2 = None
    for line in lines:
        if JOBS_RE.match(line):
            gi += 1
            section = None
            cap = None
            anchor = None
            pending_level = None
            prev = prev2 = None
            out.append(line)
            continue

        if BASEEXP_RE.match(line):
            section = "base"
            cap = groups[gi]["maxbase"]
            anchor = None
            pending_level = None
            prev = prev2 = None
            out.append(line)
            continue

        if JOBEXP_RE.match(line):
            section = "job"
            cap = groups[gi]["maxjob"]
            anchor = None
            pending_level = None
            prev = prev2 = None
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
                anchor = orig
            oe = effective_cap_value(level, cap, orig, prev, prev2)
            k = BASE_FLATTEN_K if section == "base" else group_k[gi]
            out.append(f"{indent}Exp: {flatten(oe, anchor, k)}\n")
            prev2 = prev
            prev = oe
            continue

        out.append(line)

    with open(TARGET, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(out)

    print(f"Rewrote {TARGET}")
    print(f"BASE_FLATTEN_K={BASE_FLATTEN_K}  MOB_BASE_JOB_RATIO={MOB_BASE_JOB_RATIO}  "
          f"JOB_FLATTEN_K_DEFAULT={JOB_FLATTEN_K_DEFAULT}\n")

    print(f"{'tier':16}{'cap':>5}{'K':>8}{'feasible':>10}"
          f"{'job EXP':>16}{'base seg EXP':>18}{'reached':>9}{'target':>8}")
    for rep, cap, k, ok, job_exp, rhs, reached, end in report:
        print(f"{rep:16}{cap:>5}{k:>8.3f}{('yes' if ok else 'CLAMP'):>10}"
              f"{job_exp:>16,}{int(rhs):>18,}{str(reached):>9}{end:>8}")

    print("\nMirrors (copy solved K):")
    for rep, src in JOB_FLATTEN_K_MIRROR.items():
        print(f"  {rep:16} -> {src:14} K={solved.get(src, JOB_FLATTEN_K_DEFAULT):.3f}")

    print(f"\nAll other job tiers use JOB_FLATTEN_K_DEFAULT={JOB_FLATTEN_K_DEFAULT}")


if __name__ == "__main__":
    main()
