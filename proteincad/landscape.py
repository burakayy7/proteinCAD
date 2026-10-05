"""The rotational energy landscape of a two-component assembly.

Given an assembly made of a *rotor* and an *axle* that share a symmetry axis --
the shape Courbet et al. designed in Science 2022 -- this turns the rotor about
that axis in fixed steps and measures the interface at every step. What comes
out is a curve: how the two parts like each other as a function of angle.

The curve is the point. A flat one means the rotor diffuses freely. Wells mean
it has preferred orientations and has to climb out of them. Wells whose two
sides are different heights mean it is easier to leave one way than the other,
which is what a ratchet is.

Three things are worth knowing before reading the numbers.

**The axis is measured, not assumed.** Symmetric assemblies are usually
deposited with their axis on z, but "usually" is not a thing to build on, and a
scan about the wrong axis is a curve that means nothing while looking fine. So
the axis is recovered from each component's own internal symmetry: superpose a
chain onto its symmetry mates, and the rotation that does it has the axis in it.
Two components give two independent estimates, and `axis_agreement` reports the
angle between them -- a large one means the parts do not actually share an axis
and the scan is not measuring what it claims to.

**Period is forced by symmetry, depth is not.** A Cn rotor on a Cm axle has a
landscape of period 360/lcm(n, m) degrees, whatever the energy function: turning
the rotor by 360/n maps it onto itself, so the interaction cannot tell the two
orientations apart. That makes the period a *prediction* -- something to check a
scan against rather than read off it -- which is what `tools/check_server.py`
uses to test this file against assemblies whose answer is known in advance.
Well depths carry no such guarantee: they are whatever the scorer says.

**What the default scorer is.** `GeometricScorer` needs nothing installed and
measures geometry: buried area, atomic overlap, interfacial gap. It is not a
force field and its score is in arbitrary units. It finds the orientations that
pack well, which is most of what sets the shape of a landscape for the all-alpha
interfaces these assemblies use -- but a number from it is not a binding energy
and must not be reported as one. `RosettaScorer` is where a real one goes;
PyRosetta is free for academic use but needs a licence from UW CoMotion, so it
stays behind the same interface and the app works without it.
"""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .structure import parse

# van der Waals radii, Angstroms (Bondi 1964, with the usual protein additions).
RADII = {
    "H": 1.10, "C": 1.70, "N": 1.55, "O": 1.52, "F": 1.47, "P": 1.80, "S": 1.80,
    "CL": 1.75, "SE": 1.90, "BR": 1.85, "I": 1.98,
    "NA": 2.27, "MG": 1.73, "K": 2.75, "CA": 2.31, "MN": 1.60, "FE": 1.60,
    "CO": 1.60, "NI": 1.63, "CU": 1.40, "ZN": 1.39,
}
DEFAULT_RADIUS = 1.70

PROBE = 1.4          # water, for buried surface area
CONTACT_CUTOFF = 4.5  # heavy-atom contact, Angstroms
CLASH_TOLERANCE = 0.4  # overlap allowed before a contact counts as a clash

# How far apart two atoms can be and still shade each other from the probe:
# two probe spheres touch at r_i + r_j + 2 * PROBE. Burial is summed over every
# pair inside this, not just the ones inside CONTACT_CUTOFF -- an atom at 6
# Angstroms is not in contact but is still losing area, and leaving it out
# understates the interface by a few per cent for no gain.
BURIAL_REACH = 2.0 * (1.9 + PROBE)

# How the geometric score is built from what was measured. One place, because a
# score with its weights spread over three functions cannot be reasoned about.
#
# Buried area drives it: a well in the landscape is an orientation where the two
# parts bury a lot of surface. Overlap is penalised steeply, because an
# orientation that puts two backbones through each other is not a shallow well,
# it is forbidden -- and a soft penalty would let the biggest interface win by
# interpenetrating, which is exactly the artefact this has to not produce.
BURIAL_WEIGHT = 0.01   # per square Angstrom buried, favourable
OVERLAP_WEIGHT = 5.0   # per square Angstrom of summed squared overlap


# --------------------------------------------------------------- linear algebra
#
# Small and explicit rather than numpy: the server is standard library only on
# purpose, and the only hard part -- a symmetric eigenproblem -- is thirty lines
# of Jacobi rotations.


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    length = _norm(a)
    if length < 1e-12:
        return (0.0, 0.0, 1.0)
    return (a[0] / length, a[1] / length, a[2] / length)


def _centroid(points):
    n = len(points)
    if not n:
        return (0.0, 0.0, 0.0)
    sx = sy = sz = 0.0
    for x, y, z in points:
        sx += x
        sy += y
        sz += z
    return (sx / n, sy / n, sz / n)


def jacobi_eigen(matrix: list[list[float]], sweeps: int = 100):
    """Eigenvalues and eigenvectors of a real symmetric matrix.

    Returns `(values, vectors)` sorted by descending eigenvalue, where
    `vectors[k]` is the unit eigenvector for `values[k]`. Cyclic Jacobi: it
    zeroes the largest off-diagonal element over and over, which converges
    quadratically and -- unlike anything built on a characteristic polynomial --
    stays accurate when two eigenvalues are close. Both callers here hit that
    case: a plane fit through points that nearly form a circle has two equal
    eigenvalues by construction.
    """
    n = len(matrix)
    a = [row[:] for row in matrix]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]

    for _ in range(sweeps):
        # Largest off-diagonal magnitude, and where it is.
        off, p, q = 0.0, 0, 1
        for i in range(n - 1):
            for j in range(i + 1, n):
                if abs(a[i][j]) > off:
                    off, p, q = abs(a[i][j]), i, j
        if off < 1e-14:
            break

        # The rotation that makes a[p][q] zero.
        theta = (a[q][q] - a[p][p]) / (2.0 * a[p][q])
        t = math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1.0))
        c = 1.0 / math.sqrt(t * t + 1.0)
        s = t * c

        for k in range(n):
            akp, akq = a[k][p], a[k][q]
            a[k][p] = c * akp - s * akq
            a[k][q] = s * akp + c * akq
        for k in range(n):
            apk, aqk = a[p][k], a[q][k]
            a[p][k] = c * apk - s * aqk
            a[q][k] = s * apk + c * aqk
        for k in range(n):
            vkp, vkq = v[k][p], v[k][q]
            v[k][p] = c * vkp - s * vkq
            v[k][q] = s * vkp + c * vkq

    pairs = sorted(((a[i][i], [v[k][i] for k in range(n)]) for i in range(n)),
                   key=lambda pair: -pair[0])
    return [value for value, _ in pairs], [_unit_n(vector) for _, vector in pairs]


def _unit_n(vector):
    length = math.sqrt(sum(component * component for component in vector))
    if length < 1e-12:
        return vector
    return [component / length for component in vector]


def kabsch_rotation(moving: list[tuple], fixed: list[tuple]):
    """The rotation that best takes `moving` onto `fixed`, both about their own
    centroids.

    Returns `(matrix, angle_degrees, axis)`. Horn's quaternion method rather
    than an SVD: the rotation falls out as the leading eigenvector of a 4x4
    symmetric matrix, which `jacobi_eigen` already does, and no 3x3 SVD has to
    be written. Unlike the SVD form it also cannot return a reflection.
    """
    pc, qc = _centroid(moving), _centroid(fixed)
    s = [[0.0] * 3 for _ in range(3)]
    for p, q in zip(moving, fixed):
        p0, p1, p2 = p[0] - pc[0], p[1] - pc[1], p[2] - pc[2]
        q0, q1, q2 = q[0] - qc[0], q[1] - qc[1], q[2] - qc[2]
        s[0][0] += p0 * q0; s[0][1] += p0 * q1; s[0][2] += p0 * q2
        s[1][0] += p1 * q0; s[1][1] += p1 * q1; s[1][2] += p1 * q2
        s[2][0] += p2 * q0; s[2][1] += p2 * q1; s[2][2] += p2 * q2

    k = [
        [s[0][0] + s[1][1] + s[2][2], s[1][2] - s[2][1], s[2][0] - s[0][2], s[0][1] - s[1][0]],
        [s[1][2] - s[2][1], s[0][0] - s[1][1] - s[2][2], s[0][1] + s[1][0], s[2][0] + s[0][2]],
        [s[2][0] - s[0][2], s[0][1] + s[1][0], -s[0][0] + s[1][1] - s[2][2], s[1][2] + s[2][1]],
        [s[0][1] - s[1][0], s[2][0] + s[0][2], s[1][2] + s[2][1], -s[0][0] - s[1][1] + s[2][2]],
    ]
    _, vectors = jacobi_eigen(k)
    w, x, y, z = vectors[0]

    matrix = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ]
    # The quaternion carries the angle and axis directly, and reading them from
    # it avoids the ill-conditioning of recovering them from the matrix trace
    # near 0 and 180 degrees.
    w = max(-1.0, min(1.0, w))
    angle = math.degrees(2.0 * math.acos(abs(w)))
    axis = _unit((x, y, z)) if _norm((x, y, z)) > 1e-9 else (0.0, 0.0, 1.0)
    if w < 0:  # -q is the same rotation; keep the axis on the positive-w branch
        axis = (-axis[0], -axis[1], -axis[2])
    return matrix, angle, axis


def rotation_about(axis, degrees: float):
    """Rotation matrix about a unit axis through the origin (right-handed)."""
    ux, uy, uz = _unit(axis)
    angle = math.radians(degrees)
    c, s = math.cos(angle), math.sin(angle)
    t = 1.0 - c
    return [
        [t * ux * ux + c, t * ux * uy - s * uz, t * ux * uz + s * uy],
        [t * ux * uy + s * uz, t * uy * uy + c, t * uy * uz - s * ux],
        [t * ux * uz - s * uy, t * uy * uz + s * ux, t * uz * uz + c],
    ]


def apply(matrix, point):
    x, y, z = point
    return (matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z,
            matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z,
            matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z)


def angle_between(a, b) -> float:
    """Degrees between two directions, treating antiparallel as parallel.

    An axis has no sign: two estimates of the same axis pointing opposite ways
    agree perfectly, and reporting 180 degrees of disagreement for that would
    make the one diagnostic that matters useless.
    """
    cosine = abs(_dot(_unit(a), _unit(b)))
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


# ------------------------------------------------------------------ components


@dataclass
class Component:
    """One rigid part of the assembly: the rotor, or the axle."""

    name: str
    chains: list[str]
    coords: list[tuple[float, float, float]] = field(default_factory=list)
    radii: list[float] = field(default_factory=list)
    # Atom keys per chain, for superposing a chain onto its symmetry mates.
    by_chain: dict = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.coords)

    @property
    def centre(self):
        return _centroid(self.coords)


def _radius(element: str) -> float:
    return RADII.get((element or "C").strip().upper(), DEFAULT_RADIUS)


def split(parsed, rotor_chains, axle_chains) -> tuple[Component, Component]:
    """Pull the two components out of a parsed structure, by chain id.

    Hydrogens are dropped: deposited structures mostly have none, predicted ones
    mostly do, and a landscape that changes depending on which you fed it is
    worse than one computed on heavy atoms throughout. Waters go too -- an
    interface measured through the crystallographer's water is not the interface.
    """
    rotor = Component("rotor", list(rotor_chains))
    axle = Component("axle", list(axle_chains))
    wanted = {}
    for chain in rotor.chains:
        wanted[chain] = rotor
    for chain in axle.chains:
        if chain in wanted:
            raise LandscapeError(f"chain {chain} is in both the rotor and the axle")
        wanted[chain] = axle

    for residue in parsed.residues:
        component = wanted.get(residue.chain)
        if component is None or residue.kind == "water":
            continue
        for index in range(residue.start, residue.end + 1):
            element = parsed.elements[index]
            if (element or "").strip().upper() in ("H", "D"):
                continue
            component.coords.append(parsed.coords[index])
            component.radii.append(_radius(element))
            key = (residue.seq, residue.name, parsed.atom_names[index])
            component.by_chain.setdefault(residue.chain, {})[key] = parsed.coords[index]

    for component in (rotor, axle):
        if not component.count:
            raise LandscapeError(
                f"the {component.name} has no heavy atoms in chains "
                f"{', '.join(component.chains) or '(none given)'}")
    return rotor, axle


class LandscapeError(ValueError):
    """Something about the request cannot be scanned, and saying why is more
    use than a traceback."""


# ---------------------------------------------------------------- the axis
#
# Everything downstream is measured about this, so getting it wrong produces a
# plausible-looking curve about a meaningless axis. It is recovered rather than
# assumed, and the two components' independent answers are compared.


def component_axis(component: Component):
    """The symmetry axis of one component, from its own chains.

    A chain and its symmetry mate are the same chain rotated, so superposing
    one onto the other recovers the rotation -- and its axis is the symmetry
    axis. Chains rarely have identical atom lists in a real entry (a disordered
    loop here, a missing terminus there), so the superposition runs over the
    atoms the pair has in common.

    The fold comes from the whole set of pairwise rotations at once -- see
    `_best_fold`. Returns `(direction, point, fold, spread)`, or None when the
    component has no internal symmetry to measure.
    """
    chains = [c for c in component.chains if len(component.by_chain.get(c, {})) >= 4]
    if len(chains) < 2:
        return None

    reference = max(chains, key=lambda c: len(component.by_chain[c]))
    reference_atoms = component.by_chain[reference]

    pairs = []
    for chain in chains:
        if chain == reference:
            continue
        other = component.by_chain[chain]
        shared = [key for key in reference_atoms if key in other]
        if len(shared) < 4:
            continue
        moving = [reference_atoms[key] for key in shared]
        fixed = [other[key] for key in shared]
        _, angle, axis = kabsch_rotation(moving, fixed)
        if angle < 1.0 or angle > 359.0:
            continue  # the same chain again, or a translation-only mate
        pairs.append((angle, axis))

    if not pairs:
        return None

    # Not every pair is about the axis we want. A Dn axle has n perpendicular
    # two-folds as well as its main rotation, and each of those relates the
    # reference chain to a mate just as truthfully -- so averaging all the axes
    # together points somewhere between them and is simply wrong. The main axis
    # is the one the *largest group* of pairs agrees on, so take the biggest
    # cluster and leave the rest out.
    angles, aligned = _axis_cluster(pairs)
    direction = _unit(_centroid(aligned))

    # Spread of the individual estimates about their mean: this is what says
    # whether "the component is symmetric" is even true.
    spread = max(angle_between(d, direction) for d in aligned)

    fold = _fold_from(angles, len(aligned))

    # The centroid of a symmetric component lies on its symmetry axis: the
    # symmetry permutes the atoms, so it cannot move their mean. That is a
    # cleaner way to a point on the axis than solving the screw equation, and
    # it does not degrade when the rotation is small.
    return direction, component.centre, fold, spread


FOLD_TOLERANCE = 6.0   # degrees a pairwise rotation may miss its multiple by
AXIS_CLUSTER = 12.0    # degrees two pair axes may differ and still be "the same"


def _axis_cluster(pairs: list[tuple]) -> tuple[list[float], list[tuple]]:
    """The largest group of pairs that agree on one axis.

    An axis has no sign and Kabsch picks one arbitrarily, so everything is
    compared and flipped into a single hemisphere first -- otherwise mates on
    opposite sides of a ring cancel and the mean of their axes is noise rather
    than the axis.
    """
    best: tuple[int, float, list] = (0, 1e9, [])
    for angle, candidate in pairs:
        members = []
        deviation = 0.0
        for other_angle, axis in pairs:
            separation = angle_between(candidate, axis)
            if separation <= AXIS_CLUSTER:
                flipped = axis if _dot(candidate, axis) >= 0 else (-axis[0], -axis[1], -axis[2])
                members.append((other_angle, flipped))
                deviation += separation
        score = (len(members), -deviation / max(1, len(members)))
        if score > (best[0], -best[1]):
            best = (len(members), deviation / max(1, len(members)), members)
    members = best[2] or [(pairs[0][0], pairs[0][1])]
    return [angle for angle, _ in members], [axis for _, axis in members]


def _fold_from(angles: list[float], count: int) -> int:
    """The n in Cn that explains a set of rotations about one axis, or 0.

    `count` pairs about the axis, plus the reference chain they were measured
    against, is a ring of `count + 1` members -- so that is the first candidate,
    and it is accepted only if every rotation really does land on a multiple of
    360/n. Checking rather than assuming is what makes a wrong answer come back
    as "unknown": a C8 ring with a chain too disordered to superpose would
    otherwise be confidently reported as a C7.

    The second candidate recovers exactly that case, by reading the fold off the
    smallest rotation instead of the chain count. It is only allowed when two or
    more pairs can corroborate it, because a single rotation of 51 degrees is
    within a fraction of a degree of 360/7 and means nothing of the kind -- which
    is what the asymmetric PomB dimer in this repository's cache does if asked.
    """
    if not angles:
        return 0
    candidates = [count + 1]
    if len(angles) >= 2:
        smallest = min(angles)
        if smallest > 1e-6:
            candidates.append(int(round(360.0 / smallest)))

    for n in candidates:
        if n < 2 or n > 60:
            continue
        step = 360.0 / n
        if max(abs(angle - round(angle / step) * step) for angle in angles) <= FOLD_TOLERANCE:
            return n
    return 0


def inertia_axis(component: Component):
    """The long axis of a component, as a last resort.

    An axle with one chain has no internal symmetry to measure, but it is a
    rod, and a rod's long axis is the one the rotor turns about.
    """
    centre = component.centre
    covariance = [[0.0] * 3 for _ in range(3)]
    for point in component.coords:
        d = _sub(point, centre)
        for i in range(3):
            for j in range(3):
                covariance[i][j] += d[i] * d[j]
    _, vectors = jacobi_eigen(covariance)
    return _unit(tuple(vectors[0])), centre


def detect_axis(rotor: Component, axle: Component, override=None) -> dict:
    """The axis the rotor turns about, and how much to believe it."""
    if override:
        direction = _unit(tuple(override.get("direction") or (0.0, 0.0, 1.0)))
        point = tuple(override.get("point") or axle.centre)
        return {"direction": list(direction), "point": list(point), "source": "given",
                "rotor_fold": 0, "axle_fold": 0, "agreement": None, "spread": None}

    from_rotor = component_axis(rotor)
    from_axle = component_axis(axle)

    if from_rotor and from_axle:
        agreement = angle_between(from_rotor[0], from_axle[0])
        # Average the two, after putting them in the same hemisphere. The axle
        # supplies the point: the rotor's centroid is on the axis too, but the
        # axle is the part that defines where along it the assembly sits.
        other = from_axle[0] if _dot(from_rotor[0], from_axle[0]) >= 0 else \
            (-from_axle[0][0], -from_axle[0][1], -from_axle[0][2])
        direction = _unit(_centroid([from_rotor[0], other]))
        source = "both components"
        spread = max(from_rotor[3], from_axle[3])
        point = from_axle[1]
    elif from_rotor or from_axle:
        only = from_rotor or from_axle
        direction, point, _, spread = only
        agreement = None
        source = "the rotor" if from_rotor else "the axle"
    else:
        direction, point = inertia_axis(axle)
        agreement, spread, source = None, None, "the axle's long axis (no symmetry found)"

    return {
        "direction": list(direction),
        "point": list(point),
        "source": source,
        "rotor_fold": _fold_of(rotor, from_rotor),
        "axle_fold": _fold_of(axle, from_axle),
        "agreement": None if agreement is None else round(agreement, 3),
        "spread": None if spread is None else round(spread, 3),
    }


def _fold_of(component: Component, measured) -> int:
    """The component's rotational order: measured, or 1 for a single chain.

    A one-chain component is C1, and saying so is worth more than saying
    nothing: a C1 rotor on a C8 axle still has a landscape of period 45
    degrees, because the axle's symmetry alone forces it.
    """
    if measured and measured[2]:
        return measured[2]
    return 1 if len(component.chains) == 1 else 0


def expected_period(rotor_fold: int, axle_fold: int) -> float:
    """The period symmetry forces on the landscape, in degrees, or 0 if unknown.

    A Cn rotor turned by 360/n is the same rotor, so the interaction energy
    cannot distinguish the two orientations: E has period 360/n. The axle's own
    symmetry does the same from the other side. Both hold at once, so the period
    is 360/lcm(n, m) -- and for a C4 rotor on a D8 axle that is 45 degrees,
    which is the spacing Courbet et al. report for the D8-C4 system.
    """
    if rotor_fold < 1 or axle_fold < 1:
        return 0.0
    return 360.0 / math.lcm(rotor_fold, axle_fold)


# --------------------------------------------------------------- neighbour grid


class Grid:
    """A uniform cell list over a fixed set of points.

    Built once over the atoms that do not move, then asked for the neighbours of
    each rotated atom. A scan asks this tens of millions of times, which is why
    it is a flat dict of integer keys and not anything cleverer.
    """

    def __init__(self, points, indices, spacing: float):
        self.spacing = spacing
        self.cells: dict = {}
        for index in indices:
            x, y, z = points[index]
            key = (int(x // spacing), int(y // spacing), int(z // spacing))
            self.cells.setdefault(key, []).append(index)

    def near(self, point):
        """Every stored index within one cell of `point`, as a flat list."""
        spacing = self.spacing
        cx, cy, cz = int(point[0] // spacing), int(point[1] // spacing), int(point[2] // spacing)
        found = []
        cells = self.cells
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    bucket = cells.get((cx + dx, cy + dy, cz + dz))
                    if bucket:
                        found.extend(bucket)
        return found


def sphere_points(count: int = 64):
    """Roughly even points on the unit sphere, for surface area.

    A Fibonacci spiral rather than Shrake and Rupley's subdivided icosahedron:
    the same accuracy per point, and it is four lines instead of a table.
    """
    points = []
    golden = math.pi * (3.0 - math.sqrt(5.0))
    for i in range(count):
        z = 1.0 - 2.0 * (i + 0.5) / count
        r = math.sqrt(max(0.0, 1.0 - z * z))
        theta = golden * i
        points.append((math.cos(theta) * r, math.sin(theta) * r, z))
    return points


SPHERE = sphere_points()


def atom_areas(index: int, coords, radii, grid: Grid, extra, sphere) -> tuple[float, float]:
    """One atom's accessible area with its own component alone, and in the
    complex. The difference between the two is what it buries.

    Both come out of a single pass over the sample points: a point shaded by the
    atom's own neighbours is not accessible in either state, so it can be
    dropped before the other component is consulted at all. Computing them
    together rather than caching the first is what keeps the scan exact -- see
    `sphere` below -- at no extra cost, because the own-component occluders had
    to be tested either way.

    `sphere` is the set of sample directions, and the caller rotates it with the
    rotor. Discretised area is not rotation invariant: a fixed set of sample
    points gives a slightly different answer for the same configuration turned
    by 45 degrees, and in a landscape that shows up as noise of a few per cent
    on every point -- enough to invent minima that are not there. Turning the
    sample points with the assembly makes two symmetry-equivalent orientations
    produce *identical* numbers instead of nearly-identical ones, which is what
    lets the period be checked against symmetry rather than eyeballed.
    """
    centre = coords[index]
    radius = radii[index] + PROBE
    area = 4.0 * math.pi * radius * radius

    own = []
    for other in grid.near(centre):
        if other == index:
            continue
        reach = radius + radii[other] + PROBE
        d = _sub(coords[other], centre)
        if _dot(d, d) < reach * reach:
            own.append((coords[other], radii[other] + PROBE))

    other_coords, other_radii, other_grid = extra
    across = []
    for other in other_grid.near(centre):
        reach = radius + other_radii[other] + PROBE
        d = _sub(other_coords[other], centre)
        if _dot(d, d) < reach * reach:
            across.append((other_coords[other], other_radii[other] + PROBE))

    free_hits = bound_hits = 0
    for direction in sphere:
        px = centre[0] + direction[0] * radius
        py = centre[1] + direction[1] * radius
        pz = centre[2] + direction[2] * radius
        for position, reach in own:
            dx, dy, dz = px - position[0], py - position[1], pz - position[2]
            if dx * dx + dy * dy + dz * dz < reach * reach:
                break
        else:
            free_hits += 1
            for position, reach in across:
                dx, dy, dz = px - position[0], py - position[1], pz - position[2]
                if dx * dx + dy * dy + dz * dz < reach * reach:
                    break
            else:
                bound_hits += 1

    scale = area / len(sphere)
    return free_hits * scale, bound_hits * scale


# ------------------------------------------------------------------- scorers


class Scorer:
    """What a scan asks for at each angle.

    One method, so a Rosetta backend and a geometric one are interchangeable and
    the front end can plot whichever ran without knowing which it was. `unit` and
    `label` travel with the numbers for exactly that reason: the y-axis says what
    it is showing rather than assuming.
    """

    name = "scorer"
    label = "score"
    unit = ""

    def available(self) -> tuple[bool, str]:
        return True, ""

    def prepare(self, rotor: Component, axle: Component, axis: dict) -> None:
        pass

    def score(self, rotor_coords, axle: Component, sphere) -> dict:
        raise NotImplementedError


class GeometricScorer(Scorer):
    """Buried area, overlap and interfacial gap. No dependencies, no force field.

    The score is `OVERLAP_WEIGHT * overlap - BURIAL_WEIGHT * buried`, in
    arbitrary units, so lower is better-packed. It is a shape measure. Calling
    it an energy would be wrong, and calling it a binding energy would be
    wrong twice.

    What makes a full scan cheap enough to run on a laptop is that an atom can
    only ever touch the other component if its distance from the axis and its
    height along it both land near one of the other component's atoms. Rotation
    changes neither, so that test can be applied once, before the scan starts --
    and for a ring threaded on a rod it throws away most of both parts.
    """

    name = "geometric"
    label = "packing score"
    unit = "a.u."

    def prepare(self, rotor: Component, axle: Component, axis: dict) -> None:
        self.axis = (_unit(tuple(axis["direction"])), tuple(axis["point"]))

        self.rotor_interface = self._candidates(rotor, axle, BURIAL_REACH)
        self.axle_interface = self._candidates(axle, rotor, BURIAL_REACH)

        # Occluders: every heavy atom of the component, since an atom at the
        # interface is still shaded by its own neighbours behind it. Cells are
        # BURIAL_REACH across, which is further than any pair can shade each
        # other, so the 27-cell lookup never misses an occluder.
        self.rotor_grid = Grid(rotor.coords, range(rotor.count), BURIAL_REACH)
        self.axle_grid = Grid(axle.coords, range(axle.count), BURIAL_REACH)
        # Only the interface atoms, for the pair walk: the rest cannot be reached.
        self.axle_near_grid = Grid(axle.coords, self.axle_interface, BURIAL_REACH)
        self.rotor = rotor

    def _candidates(self, component: Component, other: Component, reach: float):
        """Atoms of `component` that could reach `other` at *some* angle.

        Rotation preserves distance from the axis and height along it, so an
        atom's (radius, height) is fixed for the whole scan. Anything whose
        (radius, height) is more than `reach` from every atom of the other part
        can be dropped before the scan starts -- it never comes close at any
        angle, and for a ring on a rod that is the great majority of both.
        """
        direction, point = self.axis

        def cylindrical(p):
            d = _sub(p, point)
            height = _dot(d, direction)
            radial = math.sqrt(max(0.0, _dot(d, d) - height * height))
            return radial, height

        occupied = set()
        for p in other.coords:
            radial, height = cylindrical(p)
            occupied.add((int(radial // reach), int(height // reach)))

        kept = []
        for index, p in enumerate(component.coords):
            radial, height = cylindrical(p)
            cell = (int(radial // reach), int(height // reach))
            for dr in (-1, 0, 1):
                for dh in (-1, 0, 1):
                    if (cell[0] + dr, cell[1] + dh) in occupied:
                        kept.append(index)
                        break
                else:
                    continue
                break
        return kept

    def score(self, rotor_coords, axle: Component, sphere) -> dict:
        radii_r, radii_a = self.rotor.radii, axle.radii
        coords_a = axle.coords

        clashes = 0
        contacts = 0
        overlap = 0.0
        gaps = []
        # Atoms close enough to bury area, and the closer subset in contact.
        near_rotor = []
        near_axle = set()

        for i in self.rotor_interface:
            p = rotor_coords[i]
            closest = None
            reachable = False
            for j in self.axle_near_grid.near(p):
                q = coords_a[j]
                dx, dy, dz = p[0] - q[0], p[1] - q[1], p[2] - q[2]
                squared = dx * dx + dy * dy + dz * dz
                if squared > BURIAL_REACH * BURIAL_REACH:
                    continue
                reachable = True
                near_axle.add(j)
                if squared > CONTACT_CUTOFF * CONTACT_CUTOFF:
                    continue
                gap = math.sqrt(squared) - (radii_r[i] + radii_a[j])
                if closest is None or gap < closest:
                    closest = gap
                contacts += 1
                if gap < -CLASH_TOLERANCE:
                    clashes += 1
                    overlap += (-gap - CLASH_TOLERANCE) ** 2
            if reachable:
                near_rotor.append(i)
            if closest is not None:
                gaps.append(closest)

        buried = 0.0
        if near_rotor:
            rotor_grid = Grid(rotor_coords, range(len(rotor_coords)), BURIAL_REACH)
            axle_extra = (coords_a, radii_a, self.axle_grid)
            for i in near_rotor:
                free, bound = atom_areas(i, rotor_coords, radii_r, rotor_grid,
                                         axle_extra, sphere)
                buried += free - bound
            rotor_extra = (rotor_coords, radii_r, rotor_grid)
            for j in near_axle:
                free, bound = atom_areas(j, coords_a, radii_a, self.axle_grid,
                                         rotor_extra, sphere)
                buried += free - bound

        gaps.sort()
        median_gap = gaps[len(gaps) // 2] if gaps else None
        return {
            "score": round(OVERLAP_WEIGHT * overlap - BURIAL_WEIGHT * buried, 4),
            "bsa": round(buried, 1),
            "clashes": clashes,
            "contacts": contacts,
            "overlap": round(overlap, 3),
            "gap": None if median_gap is None else round(median_gap, 3),
        }


class RosettaScorer(Scorer):
    """Interface ddG from PyRosetta: bound score minus the two parts apart.

    **Not implemented.** This is the shape a real energy function plugs into and
    there is nothing behind it yet. It is kept listed, and listed as
    unavailable, because a scoring interface with exactly one implementation is
    an interface nobody has checked is general enough -- and because the panel
    showing "interface ddG -- unavailable" is a truer account of what this build
    can do than a menu that quietly offers only shape.

    What it would take is written out in `docs/rosetta-backend.md`: the pose
    setup, the unbound state, and the choice about repacking that decides
    whether a scan takes a minute or a week.
    """

    name = "rosetta"
    label = "interface ddG"
    unit = "REU"

    WHY = (
        "the Rosetta backend is not implemented -- this is the interface a real energy "
        "function plugs into, and nothing is behind it yet. Installing PyRosetta will not "
        "turn it on. The geometric backend is what this build can actually score with; see "
        "docs/rosetta-backend.md for what implementing this one involves."
    )

    def available(self) -> tuple[bool, str]:
        # Deliberately not conditioned on whether pyrosetta imports. It used to
        # be, which meant installing PyRosetta made this report itself ready and
        # then fail with an empty message -- the import was standing in for an
        # implementation that was never written.
        return False, self.WHY

    def prepare(self, rotor, axle, axis):
        raise LandscapeError(self.WHY)

    def score(self, rotor_coords, axle, sphere):
        raise LandscapeError(self.WHY)


BACKENDS = {"geometric": GeometricScorer, "rosetta": RosettaScorer}


def backend_catalogue() -> list[dict]:
    """What this build can score with, and why not, where it cannot."""
    catalogue = []
    for name, factory in BACKENDS.items():
        scorer = factory()
        ready, why = scorer.available()
        catalogue.append({"id": name, "label": scorer.label, "unit": scorer.unit,
                          "available": ready, "why": why})
    return catalogue


# ---------------------------------------------------------------- the scan


def scan(rotor: Component, axle: Component, axis: dict, angles, rises=(0.0,),
         backend: str = "geometric"):
    """Score every (angle, rise) in turn, yielding one row each.

    A generator because a fine scan is slow and the caller writes each row to
    disk as it arrives: that is what makes the job resumable and what lets the
    panel draw a partial curve rather than a spinner.
    """
    factory = BACKENDS.get(backend)
    if factory is None:
        raise LandscapeError(f"no scoring backend called {backend!r}; "
                             f"have {', '.join(sorted(BACKENDS))}")
    scorer = factory()
    ready, why = scorer.available()
    if not ready:
        raise LandscapeError(why)
    scorer.prepare(rotor, axle, axis)

    direction = _unit(tuple(axis["direction"]))
    point = tuple(axis["point"])
    base = [_sub(p, point) for p in rotor.coords]

    for angle in angles:
        matrix = rotation_about(direction, angle)
        turned = [apply(matrix, p) for p in base]
        # The sample sphere turns with the rotor, so two orientations that
        # symmetry says are the same come out numerically identical rather than
        # within a few per cent. `atom_areas` explains why that matters.
        sphere = [apply(matrix, d) for d in SPHERE]
        for rise in rises:
            shift = (point[0] + direction[0] * rise,
                     point[1] + direction[1] * rise,
                     point[2] + direction[2] * rise)
            moved = [(p[0] + shift[0], p[1] + shift[1], p[2] + shift[2]) for p in turned]
            row = {"angle": round(angle, 4), "rise": round(rise, 4)}
            row.update(scorer.score(moved, axle, sphere))
            yield row


def angle_list(step: float) -> list[float]:
    """The angles a scan visits: a whole turn, open at the top.

    360 is not included because it is 0 again, and a landscape with both ends
    present double-counts one point in every descriptor that averages.
    """
    if step <= 0 or step > 180:
        raise LandscapeError("the step has to be between 0 and 180 degrees")
    count = int(round(360.0 / step))
    if abs(count * step - 360.0) > 1e-6:
        raise LandscapeError(f"{step} degrees does not divide 360 a whole number of times")
    return [round(i * step, 6) for i in range(count)]


def rise_list(rise) -> list[float]:
    """The translations along the axis to scan at, usually just zero."""
    if not rise:
        return [0.0]
    low = float(rise.get("min", 0.0))
    high = float(rise.get("max", 0.0))
    step = float(rise.get("step", 1.0) or 1.0)
    if high < low:
        low, high = high, low
    if step <= 0:
        raise LandscapeError("the rise step has to be positive")
    count = int(math.floor((high - low) / step + 1e-9)) + 1
    if count > 41:
        raise LandscapeError(f"{count} rise steps is too many; widen the step")
    return [round(low + i * step, 4) for i in range(count)]


# ----------------------------------------------------------- what the curve says


def descriptors(points: list[dict], expected: float = 0.0) -> dict:
    """Minima, barriers, period and asymmetry, read off a finished scan.

    Everything here treats the curve as circular, because it is: the last angle
    is adjacent to the first, and a minimum sitting at 0 degrees is a real
    minimum rather than an edge effect.

    The asymmetry is the one worth explaining. For each well, compare the
    barrier going forward with the barrier going back. Equal barriers mean a
    rotor equally likely to turn either way -- Brownian. Different ones mean a
    direction is cheaper, which is the signature of a ratchet.

    It is reported as unavailable rather than as zero when the landscape has one
    minimum per period, because there it is *identically* zero and says nothing.
    The barrier leaving a well forwards and the barrier leaving its neighbour
    backwards are the same peak, so with a single well per period the two
    directions are the same number by construction -- and printing "asymmetry
    0.0, so it diffuses" off the back of that would be reading a theorem about
    periodic functions as a result about the assembly. A landscape has to have
    structure *within* a period for the question to have an answer: the three
    main plus nine lesser minima Courbet et al. report for their C3-C3 system is
    what that looks like.
    """
    # The one-dimensional landscape: the rise = 0 slice, or the best rise at
    # each angle when a two-dimensional scan was asked for.
    by_angle: dict = {}
    for row in points:
        angle = row["angle"]
        current = by_angle.get(angle)
        if current is None or row["score"] < current["score"]:
            by_angle[angle] = row
    ordered = [by_angle[a] for a in sorted(by_angle)]
    n = len(ordered)
    if n < 4:
        return {"minima": [], "complete": False}

    scores = [row["score"] for row in ordered]
    angles = [row["angle"] for row in ordered]
    low, high = min(scores), max(scores)
    span = high - low

    # --- minima, circularly -------------------------------------------------
    # `<=` on one side and `<` on the other, so a flat-bottomed well reports one
    # minimum rather than every sample along its floor.
    indices = [i for i in range(n)
               if scores[i] <= scores[i - 1] and scores[i] < scores[(i + 1) % n]]
    if not indices:  # a perfectly flat landscape has no wells, which is the answer
        indices = []

    minima = []
    for position, i in enumerate(indices):
        # Barriers run to the neighbouring minima either side, which is what
        # "the barrier between two wells" means and what the rotor has to climb.
        forward = _peak_between(scores, i, indices[(position + 1) % len(indices)], +1)
        reverse = _peak_between(scores, i, indices[position - 1], -1)
        minima.append({
            "angle": angles[i],
            "score": scores[i],
            "bsa": ordered[i].get("bsa"),
            "clashes": ordered[i].get("clashes"),
            "rise": ordered[i].get("rise", 0.0),
            "forward_barrier": round(forward - scores[i], 4),
            "reverse_barrier": round(reverse - scores[i], 4),
            "prominence": round(min(forward, reverse) - scores[i], 4),
        })

    # Deep wells against shallow ones. A tenth of the whole range is arbitrary
    # but has to be something, and it is reported so a reader can re-cut it.
    threshold = 0.1 * span
    deep = [m for m in minima if m["prominence"] >= threshold]
    for m in minima:
        m["deep"] = m["prominence"] >= threshold

    # --- periodicity --------------------------------------------------------
    # One discrete Fourier coefficient per order, on the mean-removed curve.
    #
    # The period is *not* 360 over the strongest order. A curve of period 45
    # degrees has power at orders 8, 16, 24 and so on -- every multiple of 8 --
    # and which of those is largest is a fact about the shape of one well, not
    # about how often the wells repeat. A double-dipped well puts more power at
    # 16 than at 8 and would be reported as a period of 22.5 degrees, which is
    # wrong and which symmetry flatly contradicts.
    #
    # What is true is that every order carrying power is a multiple of the
    # fundamental, so the fundamental is their greatest common divisor. That is
    # what is taken below, over the orders that carry enough power to mean
    # anything.
    mean = sum(scores) / n
    centred = [s - mean for s in scores]
    power = []
    for k in range(1, n // 2 + 1):
        real = imaginary = 0.0
        for j, value in enumerate(centred):
            phase = -2.0 * math.pi * k * j / n
            real += value * math.cos(phase)
            imaginary += value * math.sin(phase)
        power.append((real * real + imaginary * imaginary, k))
    total = sum(p for p, _ in power) or 1.0
    best_power, dominant = max(power)

    # The period is measured by shifting the curve onto itself, not read out of
    # the transform. Two things make the transform the wrong tool here. The
    # strongest order is the shape of one well rather than the spacing of them,
    # so a double-dipped well reports half the period. And a sharp curve has
    # harmonics above the Nyquist limit which *alias back down*: a 45-degree
    # period sampled every 10 degrees puts its 32nd order onto the 4th, which
    # looks exactly like a 90-degree period and is not one.
    #
    # Shifting has neither problem, and says what the word means: the period is
    # the smallest turn that leaves the landscape looking the same.
    shift = _period_shift(scores, span)
    order = max(1, n // shift) if shift and n % shift == 0 else 1
    measured_period = 360.0 * shift / n

    # Power at the order symmetry demands, whether or not it is the dominant
    # one. On a real entry refined without imposed symmetry these differ, and
    # "the forced period is present but not dominant" is a different and more
    # useful statement than "the period does not match".
    expected_order = round(360.0 / expected) if expected else 0
    expected_share = next((p / total for p, k in power if k == expected_order), None)

    # Whether this scan could see the expected period at all. Two ways it
    # cannot, and both have to be ruled out before a mismatch means anything.
    #
    # The period has to land on the sample grid: a 45-degree period scanned
    # every 10 degrees is never compared with itself, because no multiple of
    # the step is 45. And eight wells cannot be found in twelve samples.
    #
    # Saying "too coarse to check" is the difference between a test that failed
    # and a test that was never run.
    step_degrees = 360.0 / n
    on_grid = bool(expected) and abs(expected / step_degrees - round(expected / step_degrees)) < 1e-6
    resolvable = on_grid and bool(expected_order) and 2 * expected_order < n

    forwards = [m["forward_barrier"] for m in minima]
    reverses = [m["reverse_barrier"] for m in minima]
    per_period = len(minima) / order if order else 0.0
    asymmetry = None
    if minima and per_period > 1.0001:
        pairs = [(f, r) for f, r in zip(forwards, reverses) if (f + r) > 1e-9]
        if pairs:
            asymmetry = round(sum((f - r) / (f + r) for f, r in pairs) / len(pairs), 4)

    # Where the structure as it arrived sits in its own landscape. For anything
    # experimental this is the one validation that needs no reference curve: the
    # deposited orientation is the one nature or the refinement picked, so a
    # scorer worth trusting should put it at or near the bottom. A rank well
    # down the list is a result about the scorer, not about the assembly.
    at_zero = by_angle.get(0.0)
    deposited = None
    if at_zero is not None:
        better = sum(1 for s in scores if s < at_zero["score"])
        deposited = {"score": at_zero["score"], "rank": better + 1, "of": n,
                     "is_minimum": any(m["angle"] == 0.0 for m in minima)}

    # How much of the turn is simply blocked. A tightly interdigitated assembly
    # clashes almost everywhere, and its "landscape" is a steric wall rather
    # than a set of wells -- worth saying outright, because the curve looks the
    # same either way until you read the axis.
    clash_free = sum(1 for row in ordered if not row.get("clashes"))

    return {
        "complete": True,
        "samples": n,
        "step": round(360.0 / n, 4),
        "minima": minima,
        "deposited": deposited,
        "clash_free_fraction": round(clash_free / n, 4),
        "deep_count": len(deep),
        "lesser_count": len(minima) - len(deep),
        "prominence_threshold": round(threshold, 4),
        "range": round(span, 4),
        "best": min(minima, key=lambda m: m["score"])["angle"] if minima else None,
        "period_order": order,
        "period": round(measured_period, 4),
        # The strongest single order, which is the shape of a well rather than
        # the spacing of them. Reported because a big gap between this and
        # `period_order` is exactly what a structured well looks like.
        "dominant_order": dominant,
        "dominant_power": round(best_power / total, 4),
        "period_power": round(
            sum(p for p, k in power if order and k % order == 0) / total, 4),
        "expected_period": round(expected, 4) if expected else 0.0,
        "expected_period_power": None if expected_share is None else round(expected_share, 4),
        "period_resolvable": resolvable,
        "period_matches_symmetry": resolvable and abs(measured_period - expected) < 1e-6,
        "period_note": "" if not expected or resolvable else (
            f"a period of {expected:g}° is not a whole number of {step_degrees:g}° steps, so "
            f"this scan never compares the curve with itself a period apart — scan at "
            f"{_fine_enough(expected_order, expected)}° to check it"
            if not on_grid else
            f"{n} samples cannot resolve {expected_order} wells a turn — scan at "
            f"{_fine_enough(expected_order, expected)}° or finer to check the period"),
        "barrier_mean": round(sum(forwards) / len(forwards), 4) if forwards else None,
        "barrier_max": round(max(forwards), 4) if forwards else None,
        "minima_per_period": round(per_period, 3),
        "asymmetry": asymmetry,
        # Why there is no asymmetry to report, when there is not. A number the
        # panel cannot explain the absence of gets read as a zero.
        "asymmetry_note": "" if asymmetry is not None else (
            "one minimum per period: forward and reverse barriers are the same peak, "
            "so the measure is zero by construction rather than by measurement"
            if minima else "no wells to compare"),
    }


PERIOD_TOLERANCE = 0.02  # of the depth range, when matching a shifted curve


def _period_shift(scores: list[float], span: float) -> int:
    """The smallest whole-sample shift that maps the curve onto itself.

    Returns the shift in samples, or the sample count when nothing smaller
    works -- which is the honest answer for a landscape with no repeat in it.
    A flat curve matches at every shift and would otherwise report the finest
    period the sampling allows, which says nothing.
    """
    n = len(scores)
    if span <= 1e-12:
        return n
    tolerance = PERIOD_TOLERANCE * span
    for shift in range(1, n):
        if n % shift:
            continue  # a period has to divide the turn a whole number of times
        if all(abs(scores[j] - scores[(j + shift) % n]) <= tolerance for j in range(n)):
            return shift
    return n


def _fine_enough(order: int, period: float = 0.0) -> float:
    """The coarsest step that resolves `order` wells and divides the period."""
    for step in (45, 30, 20, 15, 10, 5, 2, 1):
        if order and 2 * order >= 360 / step:
            continue
        if period and abs(period / step - round(period / step)) > 1e-6:
            continue
        return step
    return 1


def _peak_between(scores, start: int, stop: int, direction: int) -> float:
    """The highest point on the way from one index to another, circularly."""
    n = len(scores)
    peak = scores[start]
    i = start
    for _ in range(n):
        i = (i + direction) % n
        peak = max(peak, scores[i])
        if i == stop:
            break
    return peak


# -------------------------------------------------------------- the subprocess
#
# A scan is minutes of CPU, so it does not run inside the server process: that
# would hold a request open, pin the memory of a whole assembly in the process
# serving the viewer, and lose everything computed so far if anything restarted.
# It runs as `python3 -m proteincad.landscape <directory>` instead, appending one
# line per angle to points.ndjson -- which is what makes it resumable, because
# the lines already there are the angles already done.


def request_digest(request: dict, pdb: str) -> str:
    """A stable name for one scan, so an identical request is never run twice.

    The hash covers the coordinates as well as the settings: the same chains
    scanned at the same step on a structure that has moved is a different
    question with a different answer.
    """
    import hashlib
    payload = json.dumps({
        "rotor": sorted(request.get("rotor") or []),
        "axle": sorted(request.get("axle") or []),
        "step": request.get("step"),
        "rise": request.get("rise") or None,
        "backend": request.get("backend") or "geometric",
        "axis": request.get("axis") or None,
    }, sort_keys=True)
    digest = hashlib.sha256()
    digest.update(payload.encode("utf-8"))
    digest.update(pdb.encode("utf-8"))
    return digest.hexdigest()[:16]


def done_angles(path: Path) -> list[dict]:
    """The rows already on disk, dropping a half-written last line.

    A scan killed mid-write leaves a truncated line, and treating it as data is
    how a resumed job reports a score that was never computed.
    """
    rows = []
    if not path.is_file():
        return rows
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and "angle" in row and "score" in row:
            rows.append(row)
    return rows


def run(directory: Path) -> int:
    """Run (or finish) the scan in `directory`. The subprocess entry point."""
    directory = Path(directory)
    request = json.loads((directory / "request.json").read_text())
    pdb = (directory / "assembly.pdb").read_text()
    points_path = directory / "points.ndjson"

    try:
        parsed = parse(pdb, name=request.get("name") or "assembly")
        rotor, axle = split(parsed, request.get("rotor") or [], request.get("axle") or [])
        axis = detect_axis(rotor, axle, request.get("axis"))
        angles = angle_list(float(request.get("step") or 5.0))
        rises = rise_list(request.get("rise"))

        axis_detail = dict(axis)
        axis_detail["expected_period"] = expected_period(axis["rotor_fold"], axis["axle_fold"])
        axis_detail["rotor_atoms"] = rotor.count
        axis_detail["axle_atoms"] = axle.count
        axis_detail["total"] = len(angles) * len(rises)
        (directory / "axis.json").write_text(json.dumps(axis_detail, indent=2))

        already = {(row["angle"], row.get("rise", 0.0)) for row in done_angles(points_path)}
        todo = [a for a in angles if any((a, r) not in already for r in rises)]

        with points_path.open("a", buffering=1) as sink:
            for row in scan(rotor, axle, axis, todo, rises, request.get("backend") or "geometric"):
                if (row["angle"], row["rise"]) in already:
                    continue
                sink.write(json.dumps(row) + "\n")
                sink.flush()
        (directory / "finished").write_text(str(time.time()))
        return 0
    except Exception as error:  # the parent has no other way to learn why
        message = str(error) if isinstance(error, (LandscapeError, ValueError)) \
            else f"{type(error).__name__}: {error}"
        (directory / "error.txt").write_text(message)
        print(f"landscape: {message}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: python3 -m proteincad.landscape <scan directory>", file=sys.stderr)
        return 2
    return run(Path(argv[0]))


if __name__ == "__main__":
    raise SystemExit(main())
