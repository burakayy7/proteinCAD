#!/usr/bin/env python3
"""Rebuild the cross-check fixture: a D8 axle with a C4 rotor on it.

    python3 tools/fixtures/make-landscape-fixture.py

The geometric scan exists twice -- in `proteincad/landscape.py` and in
`web/src/core/landscape.js` -- because it runs in the browser on a static
deployment and PyRosetta cannot. Two implementations of the same arithmetic
drift, so both test suites compute the landscape of this one structure and
assert this one curve. If either side moves, a test fails.

The step is 15 degrees on purpose: symmetry forces a 45 degree period here, and
45 has to be a whole number of steps or no scan ever compares the curve with
itself a period apart.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from proteincad import landscape as L  # noqa: E402

HERE = Path(__file__).resolve().parent
STEP = 15.0


def blob(seed, count=10):
    """A deterministic asymmetric lump. Asymmetric so the two directions round
    the axis are not equivalent for reasons that have nothing to do with the
    assembly."""
    state, points = seed, []
    for _ in range(count):
        values = []
        for _ in range(3):
            state = (state * 1103515245 + 12345) % 2147483648
            values.append(state / 2147483648.0)
        points.append((values[0] * 6 - 3, values[1] * 6 - 3, values[2] * 9 - 4.5))
    return points


def c_ring(local, fold, radius, height=0.0):
    return [[L.apply(L.rotation_about((0, 0, 1), k * 360.0 / fold),
                     (p[0] + radius, p[1], p[2] + height)) for p in local]
            for k in range(fold)]


def d_ring(local, fold, radius):
    rings = c_ring(local, fold, radius, height=+6.0)
    flip = L.rotation_about((1, 0, 0), 180.0)
    return rings + c_ring([L.apply(flip, p) for p in local], fold, radius, height=-6.0)


def main() -> int:
    axle_chains = d_ring(blob(7), 8, 7.0)
    rotor_chains = c_ring(blob(99), 4, 14.5)
    pool = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

    lines = [
        "REMARK   1 proteinCAD cross-check fixture: a D8 axle with a C4 rotor on it.",
        "REMARK   2 Chains A-P are the axle, Q-T the rotor.",
        "REMARK   3 Symmetry forces a 45 degree period: 360 / lcm(4, 8).",
        "REMARK   4 Rebuild with tools/fixtures/make-landscape-fixture.py",
    ]
    serial, ids = 1, []
    for group in (axle_chains, rotor_chains):
        for chain in group:
            name = pool[len(ids)]
            ids.append(name)
            for index, (x, y, z) in enumerate(chain):
                lines.append(f"ATOM  {serial:5d}  CA  ALA {name}{index + 1:4d}    "
                             f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           C  ")
                serial += 1
            lines.append(f"TER   {serial:5d}")
            serial += 1
    lines.append("END")
    pdb = "\n".join(lines) + "\n"
    (HERE / "landscape-d8c4.pdb").write_text(pdb)

    parsed = L.parse(pdb, "d8c4")
    rotor, axle = L.split(parsed, ids[16:], ids[:16])
    axis = L.detect_axis(rotor, axle)
    expected = L.expected_period(axis["rotor_fold"], axis["axle_fold"])
    rows = list(L.scan(rotor, axle, axis, L.angle_list(STEP)))
    said = L.descriptors(rows, expected)

    fixture = {
        "_comment": [
            "The landscape of landscape-d8c4.pdb, as proteincad/landscape.py computes it.",
            "web/src/core/landscape.js has to reproduce it: tools/check.mjs asserts that,",
            "and tools/check_server.py asserts the Python still produces it.",
            "Rebuild with: python3 tools/fixtures/make-landscape-fixture.py",
        ],
        "rotor": ids[16:],
        "axle": ids[:16],
        "step": STEP,
        "axis": {
            "direction": axis["direction"],
            "rotor_fold": axis["rotor_fold"],
            "axle_fold": axis["axle_fold"],
            "expected_period": expected,
        },
        "points": [{"angle": r["angle"], "score": r["score"], "bsa": r["bsa"],
                    "clashes": r["clashes"], "contacts": r["contacts"]} for r in rows],
        "descriptors": {k: said[k] for k in (
            "period", "period_order", "period_resolvable", "period_matches_symmetry",
            "deep_count", "lesser_count", "range", "barrier_mean", "asymmetry",
            "minima_per_period", "clash_free_fraction", "best")},
    }
    (HERE / "landscape-d8c4.json").write_text(json.dumps(fixture, indent=1) + "\n")
    print(f"{len(ids)} chains, {parsed.atom_count} atoms, {len(rows)} angles")
    print(f"  folds {axis['rotor_fold']}/{axis['axle_fold']}, expected period {expected}")
    print(f"  {json.dumps(fixture['descriptors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
