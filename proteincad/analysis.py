"""Server-side computation on structures.

Everything here works on plain lists so it stays dependency free. This module is
the seam where heavier work belongs: swap these functions for numpy, or call a
model, and the browser side does not change -- it posts atoms to /api/analyze
and renders whatever comes back.

To add a model:

    1. write a function here that takes atoms (or a Structure) and returns a
       JSON-serialisable dict,
    2. register a route for it in api.py,
    3. call it from the browser with fetch('api/your-route', ...).
"""

from __future__ import annotations

import math

from .structure import MASSES, Structure


def geometry(elements: list[str], coords: list[tuple[float, float, float]]) -> dict:
    """Centre of mass, radius of gyration, extent and composition."""
    count = len(coords)
    if count == 0:
        return {"count": 0}

    masses = [MASSES.get((e or "C").upper(), 12.0) for e in elements] if elements else [12.0] * count
    total_mass = sum(masses)

    cx = sum(m * p[0] for m, p in zip(masses, coords)) / total_mass
    cy = sum(m * p[1] for m, p in zip(masses, coords)) / total_mass
    cz = sum(m * p[2] for m, p in zip(masses, coords)) / total_mass

    inertia = sum(
        m * ((p[0] - cx) ** 2 + (p[1] - cy) ** 2 + (p[2] - cz) ** 2)
        for m, p in zip(masses, coords)
    )
    gyration = math.sqrt(inertia / total_mass)

    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    zs = [p[2] for p in coords]

    composition: dict[str, int] = {}
    for element in elements or []:
        key = (element or "?").upper()
        composition[key] = composition.get(key, 0) + 1

    return {
        "count": count,
        "centre_of_mass": [round(cx, 3), round(cy, 3), round(cz, 3)],
        "radius_of_gyration": round(gyration, 3),
        "molecular_weight": round(total_mass, 1),
        "extent": [
            round(max(xs) - min(xs), 2),
            round(max(ys) - min(ys), 2),
            round(max(zs) - min(zs), 2),
        ],
        "bounds": {
            "min": [round(min(xs), 2), round(min(ys), 2), round(min(zs), 2)],
            "max": [round(max(xs), 2), round(max(ys), 2), round(max(zs), 2)],
        },
        "composition": dict(sorted(composition.items(), key=lambda kv: -kv[1])),
    }


def analyse_structure(structure: Structure) -> dict:
    """Summary plus geometry for a parsed structure."""
    result = structure.summary()
    result["geometry"] = geometry(structure.elements, structure.coords)
    return result


def contacts(
    coords: list[tuple[float, float, float]],
    groups: list[int],
    cutoff: float = 4.0,
) -> list[dict]:
    """Pairs of groups (chain indices, say) that come within `cutoff`.

    A plain O(n^2) sweep over a grid; fine for interface-sized problems and a
    reasonable template for the kind of geometry query a design model needs.
    """
    cell = max(cutoff, 1e-3)
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for index, (x, y, z) in enumerate(coords):
        key = (int(x // cell), int(y // cell), int(z // cell))
        buckets.setdefault(key, []).append(index)

    seen: dict[tuple[int, int], float] = {}
    cutoff2 = cutoff * cutoff
    for (bx, by, bz), members in buckets.items():
        neighbours: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    neighbours.extend(buckets.get((bx + dx, by + dy, bz + dz), ()))
        for i in members:
            gi = groups[i]
            xi, yi, zi = coords[i]
            for j in neighbours:
                gj = groups[j]
                if gj <= gi:
                    continue
                xj, yj, zj = coords[j]
                d2 = (xi - xj) ** 2 + (yi - yj) ** 2 + (zi - zj) ** 2
                if d2 > cutoff2:
                    continue
                key = (gi, gj)
                distance = math.sqrt(d2)
                if key not in seen or distance < seen[key]:
                    seen[key] = distance

    return [
        {"a": a, "b": b, "min_distance": round(distance, 2)}
        for (a, b), distance in sorted(seen.items())
    ]
