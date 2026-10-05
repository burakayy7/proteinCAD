"""A stand-in generator, so the whole design workflow can be exercised with no
GPU in sight.

It builds a real helical bundle: the helices come from ideal backbone torsions
via NeRF, which means they have correct N/CA/C/O geometry and are recognised as
helices by the viewer's secondary-structure assignment, and the bundle is placed
where the user drew the volume. That makes it useful for checking the plumbing
end to end -- job, transport, placement, rendering -- rather than just returning
a placeholder blob.

It is not a design method. Nothing here knows anything about sequence, binding
or energy; swap in RFdiffusion (see design.py) for that.
"""

from __future__ import annotations

import math
import random

# Ideal backbone geometry (Engh & Huber-ish values).
N_CA, CA_C, C_N, C_O = 1.458, 1.525, 1.329, 1.231
ANGLE_N_CA_C, ANGLE_CA_C_N, ANGLE_C_N_CA, ANGLE_CA_C_O = 111.2, 116.2, 121.7, 120.8
HELIX_PHI, HELIX_PSI, OMEGA = -57.0, -47.0, 180.0
CA_SPACING = 3.8


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def scale(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a):
    return math.sqrt(dot(a, a))


def unit(a):
    length = norm(a)
    return (0.0, 0.0, 1.0) if length < 1e-9 else scale(a, 1.0 / length)


def place_atom(a, b, c, bond, angle_deg, torsion_deg):
    """NeRF: the position of D given A-B-C plus |CD|, angle BCD and torsion ABCD."""
    angle = math.radians(angle_deg)
    torsion = math.radians(torsion_deg)

    bc = unit(sub(c, b))
    n = unit(cross(sub(b, a), bc))
    m = cross(n, bc)

    d2 = (
        -bond * math.cos(angle),
        bond * math.cos(torsion) * math.sin(angle),
        bond * math.sin(torsion) * math.sin(angle),
    )
    return (
        c[0] + d2[0] * bc[0] + d2[1] * m[0] + d2[2] * n[0],
        c[1] + d2[0] * bc[1] + d2[1] * m[1] + d2[2] * n[1],
        c[2] + d2[0] * bc[2] + d2[1] * m[2] + d2[2] * n[2],
    )


def build_chain(torsions):
    """Backbone residues from a list of (phi, psi) pairs."""
    residues = []
    n0 = (0.0, 0.0, 0.0)
    ca0 = (N_CA, 0.0, 0.0)
    c0 = place_atom((0.0, 1.0, 0.0), n0, ca0, CA_C, ANGLE_N_CA_C, torsions[0][0])
    residues.append({"N": n0, "CA": ca0, "C": c0})

    for index in range(1, len(torsions)):
        previous = residues[-1]
        psi = torsions[index - 1][1]
        phi = torsions[index][0]
        n = place_atom(previous["N"], previous["CA"], previous["C"], C_N, ANGLE_CA_C_N, psi)
        ca = place_atom(previous["CA"], previous["C"], n, N_CA, ANGLE_C_N_CA, OMEGA)
        c = place_atom(previous["C"], n, ca, CA_C, ANGLE_N_CA_C, phi)
        residues.append({"N": n, "CA": ca, "C": c})

    add_carbonyls(residues)
    return residues


def add_carbonyls(residues):
    """O sits in the peptide plane, bisecting the external angle at C (sp2)."""
    for index, residue in enumerate(residues):
        if index + 1 < len(residues):
            toward = unit(sub(residue["C"], residues[index + 1]["N"]))
        else:
            toward = unit(sub(residue["C"], residue["CA"]))
        from_ca = unit(sub(residue["C"], residue["CA"]))
        direction = unit(add(toward, from_ca)) if index + 1 < len(residues) else toward
        residue["O"] = add(residue["C"], scale(direction, C_O))


def axis_of(residues):
    """Helix axis from the CA trace: the mean of successive i -> i+4 vectors."""
    cas = [r["CA"] for r in residues]
    if len(cas) < 5:
        return unit(sub(cas[-1], cas[0]))
    total = (0.0, 0.0, 0.0)
    for i in range(len(cas) - 4):
        total = add(total, sub(cas[i + 4], cas[i]))
    return unit(total)


def centroid(points):
    if not points:
        return (0.0, 0.0, 0.0)
    total = (0.0, 0.0, 0.0)
    for point in points:
        total = add(total, point)
    return scale(total, 1.0 / len(points))


def rotation_between(a, b):
    """3x3 rotation taking unit vector a onto unit vector b."""
    v = cross(a, b)
    c = dot(a, b)
    if c < -0.999999:
        # Antiparallel: rotate a half turn about any perpendicular.
        perpendicular = unit(cross(a, (1.0, 0.0, 0.0)))
        if norm(cross(a, (1.0, 0.0, 0.0))) < 1e-6:
            perpendicular = unit(cross(a, (0.0, 1.0, 0.0)))
        x, y, z = perpendicular
        return [
            [2 * x * x - 1, 2 * x * y, 2 * x * z],
            [2 * x * y, 2 * y * y - 1, 2 * y * z],
            [2 * x * z, 2 * y * z, 2 * z * z - 1],
        ]
    k = 1.0 / (1.0 + c)
    vx, vy, vz = v
    return [
        [vx * vx * k + c, vx * vy * k - vz, vx * vz * k + vy],
        [vx * vy * k + vz, vy * vy * k + c, vy * vz * k - vx],
        [vx * vz * k - vy, vy * vz * k + vx, vz * vz * k + c],
    ]


def apply_matrix(matrix, point):
    return (
        matrix[0][0] * point[0] + matrix[0][1] * point[1] + matrix[0][2] * point[2],
        matrix[1][0] * point[0] + matrix[1][1] * point[1] + matrix[1][2] * point[2],
        matrix[2][0] * point[0] + matrix[2][1] * point[1] + matrix[2][2] * point[2],
    )


def transform_residues(residues, matrix, offset):
    for residue in residues:
        for name, point in residue.items():
            residue[name] = add(apply_matrix(matrix, point), offset)
    return residues


# Backbone torsions a turn can plausibly use: right-handed helix, extended,
# left-handed helix and polyproline II.
TURN_TORSIONS = [(-57.0, -47.0), (-120.0, 130.0), (60.0, 45.0), (-75.0, 150.0), (-90.0, 0.0)]


def radius_of_gyration(residues):
    cas = [r["CA"] for r in residues]
    centre = centroid(cas)
    total = sum(dot(sub(ca, centre), sub(ca, centre)) for ca in cas)
    return math.sqrt(total / len(cas))


def clashes(residues, cutoff=4.0):
    cas = [r["CA"] for r in residues]
    cutoff2 = cutoff * cutoff
    count = 0
    for i in range(len(cas)):
        for j in range(i + 3, len(cas)):
            if dot(sub(cas[i], cas[j]), sub(cas[i], cas[j])) < cutoff2:
                count += 1
    return count


def helical_bundle(length, seed=0, helices=None, attempts=80):
    """A compact helical bundle.

    The whole chain is built by NeRF from ideal torsions, so every bond length
    and peptide bond is correct and the viewer sees one continuous ribbon. Only
    the turn torsions vary; the bundle shape falls out of picking turns that
    fold the helices back onto each other, which is what a random search over a
    handful of torsion regions finds quickly.
    """
    rng = random.Random(seed)
    length = max(24, int(length))
    if helices is None:
        helices = min(5, max(2, round(length / 32)))
    turn = 4
    per_helix = max(8, (length - turn * (helices - 1)) // helices)

    candidates = []
    for _ in range(attempts):
        torsions = []
        for index in range(helices):
            if index:
                torsions.extend(rng.choice(TURN_TORSIONS) for _ in range(turn))
            torsions.extend([(HELIX_PHI, HELIX_PSI)] * per_helix)
        residues = build_chain(torsions)
        candidates.append((radius_of_gyration(residues), residues))

    # Compactness first, then reject the ones that fold through themselves.
    candidates.sort(key=lambda item: item[0])
    for _, residues in candidates[:10]:
        if clashes(residues) == 0:
            return recentre(residues)
    return recentre(candidates[0][1])


def recentre(residues):
    offset = scale(centroid([r["CA"] for r in residues]), -1.0)
    for residue in residues:
        for name, point in residue.items():
            residue[name] = add(point, offset)
    return residues


def orient_and_place(residues, axis, centre):
    """Stand the bundle along `axis` and drop it at `centre`."""
    matrix = rotation_between((0.0, 0.0, 1.0), unit(axis))
    transform_residues(residues, matrix, (0.0, 0.0, 0.0))
    shift = sub(centre, centroid([r["CA"] for r in residues]))
    for residue in residues:
        for name, point in residue.items():
            residue[name] = add(point, shift)
    return residues


GLY_ORDER = ("N", "CA", "C", "O")


def to_pdb(residues, chain_id="A", resname="GLY", title=""):
    lines = []
    if title:
        lines.append(f"TITLE     {title}"[:80])
    lines.append("REMARK   1 Backbone generated by proteinCAD mock runner (not a design)")
    serial = 1
    for index, residue in enumerate(residues, start=1):
        for name in GLY_ORDER:
            point = residue.get(name)
            if point is None:
                continue
            lines.append(
                "ATOM  "
                + f"{serial % 100000:5d}"
                + " "
                + f" {name:<3}"
                + " "
                + f"{resname:>3}"
                + " "
                + f"{chain_id:1}"
                + f"{index:4d}"
                + "    "
                + f"{point[0]:8.3f}{point[1]:8.3f}{point[2]:8.3f}"
                + "  1.00  0.00"
                + " " * 10
                + f"{name[0]:>2}  "
            )
            serial += 1
    lines.append(f"TER   {serial % 100000:5d}")
    lines.append("END")
    return "\n".join(lines) + "\n"
