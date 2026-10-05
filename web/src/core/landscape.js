// The rotational energy landscape, computed where the atoms already are.
//
// This mirrors `proteincad/landscape.py` -- the same axis detection, the same
// geometric scorer, the same descriptors -- and exists because the geometric
// backend needs nothing but arithmetic. The browser is already holding the
// coordinates, so sending them to a server to be re-parsed and scored costs an
// upload, a sign-in and a poll loop to get back something it could have worked
// out itself. PyRosetta is the opposite case and stays server-side: that is the
// line the two implementations are split along, and it is not an arbitrary one.
//
// Two implementations of the same science can drift, so `tools/check.mjs` and
// `tools/check_server.py` both compute the landscape of the same fixed assembly
// and assert the same curve. If either side moves, a test fails.
//
// No three.js here, same rule as the rest of core/: it runs in plain node,
// which is what lets it be tested against the Python without a browser.

import { VDW_RADII } from './elements.js';
import { Kind } from './structure.js';

const DEFAULT_RADIUS = 1.70;   // unknown elements; matches landscape.py
export const PROBE = 1.4;
export const CONTACT_CUTOFF = 4.5;
export const CLASH_TOLERANCE = 0.4;
// Two probe spheres touch at r_i + r_j + 2*PROBE; burial is summed over every
// pair inside that, not just the ones in contact.
export const BURIAL_REACH = 2 * (1.9 + PROBE);

export const BURIAL_WEIGHT = 0.01;
export const OVERLAP_WEIGHT = 5.0;

const FOLD_TOLERANCE = 6.0;
const AXIS_CLUSTER = 12.0;

/* ------------------------------------------------------------ linear algebra */

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

function unit(a) {
  const length = Math.hypot(a[0], a[1], a[2]);
  return length < 1e-12 ? [0, 0, 1] : [a[0] / length, a[1] / length, a[2] / length];
}

function centroid(points) {
  if (!points.length) return [0, 0, 0];
  let x = 0, y = 0, z = 0;
  for (const p of points) { x += p[0]; y += p[1]; z += p[2]; }
  return [x / points.length, y / points.length, z / points.length];
}

/**
 * Eigenvalues and eigenvectors of a real symmetric matrix, by cyclic Jacobi.
 *
 * Converges quadratically and, unlike anything built on a characteristic
 * polynomial, stays accurate when two eigenvalues are close -- which both
 * callers here hit.
 */
export function jacobiEigen(matrix, sweeps = 100) {
  const n = matrix.length;
  const a = matrix.map((row) => row.slice());
  const v = Array.from({ length: n }, (_, i) => Array.from({ length: n }, (_, j) => (i === j ? 1 : 0)));

  for (let sweep = 0; sweep < sweeps; sweep++) {
    let off = 0, p = 0, q = 1;
    for (let i = 0; i < n - 1; i++) {
      for (let j = i + 1; j < n; j++) {
        if (Math.abs(a[i][j]) > off) { off = Math.abs(a[i][j]); p = i; q = j; }
      }
    }
    if (off < 1e-14) break;

    const theta = (a[q][q] - a[p][p]) / (2 * a[p][q]);
    const t = Math.sign(theta || 1) / (Math.abs(theta) + Math.sqrt(theta * theta + 1));
    const c = 1 / Math.sqrt(t * t + 1);
    const s = t * c;

    for (let k = 0; k < n; k++) {
      const akp = a[k][p], akq = a[k][q];
      a[k][p] = c * akp - s * akq;
      a[k][q] = s * akp + c * akq;
    }
    for (let k = 0; k < n; k++) {
      const apk = a[p][k], aqk = a[q][k];
      a[p][k] = c * apk - s * aqk;
      a[q][k] = s * apk + c * aqk;
    }
    for (let k = 0; k < n; k++) {
      const vkp = v[k][p], vkq = v[k][q];
      v[k][p] = c * vkp - s * vkq;
      v[k][q] = s * vkp + c * vkq;
    }
  }

  const pairs = Array.from({ length: n }, (_, i) => [a[i][i], v.map((row) => row[i])]);
  pairs.sort((x, y) => y[0] - x[0]);
  return {
    values: pairs.map(([value]) => value),
    vectors: pairs.map(([, vector]) => {
      const length = Math.hypot(...vector);
      return length < 1e-12 ? vector : vector.map((c) => c / length);
    }),
  };
}

/**
 * The rotation that best takes `moving` onto `fixed`, about their centroids.
 *
 * Horn's quaternion method rather than an SVD: the rotation falls out as the
 * leading eigenvector of a 4x4 symmetric matrix, and unlike the SVD form it
 * cannot return a reflection.
 */
export function kabschRotation(moving, fixed) {
  const pc = centroid(moving);
  const qc = centroid(fixed);
  const s = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
  for (let i = 0; i < moving.length; i++) {
    const p0 = moving[i][0] - pc[0], p1 = moving[i][1] - pc[1], p2 = moving[i][2] - pc[2];
    const q0 = fixed[i][0] - qc[0], q1 = fixed[i][1] - qc[1], q2 = fixed[i][2] - qc[2];
    s[0][0] += p0 * q0; s[0][1] += p0 * q1; s[0][2] += p0 * q2;
    s[1][0] += p1 * q0; s[1][1] += p1 * q1; s[1][2] += p1 * q2;
    s[2][0] += p2 * q0; s[2][1] += p2 * q1; s[2][2] += p2 * q2;
  }

  const k = [
    [s[0][0] + s[1][1] + s[2][2], s[1][2] - s[2][1], s[2][0] - s[0][2], s[0][1] - s[1][0]],
    [s[1][2] - s[2][1], s[0][0] - s[1][1] - s[2][2], s[0][1] + s[1][0], s[2][0] + s[0][2]],
    [s[2][0] - s[0][2], s[0][1] + s[1][0], -s[0][0] + s[1][1] - s[2][2], s[1][2] + s[2][1]],
    [s[0][1] - s[1][0], s[2][0] + s[0][2], s[1][2] + s[2][1], -s[0][0] - s[1][1] + s[2][2]],
  ];
  let [w, x, y, z] = jacobiEigen(k).vectors[0];

  const matrix = [
    [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
    [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
    [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
  ];
  const clamped = Math.max(-1, Math.min(1, w));
  const angle = (2 * Math.acos(Math.abs(clamped)) * 180) / Math.PI;
  let axis = Math.hypot(x, y, z) > 1e-9 ? unit([x, y, z]) : [0, 0, 1];
  if (clamped < 0) axis = [-axis[0], -axis[1], -axis[2]];
  return { matrix, angle, axis };
}

/** Rotation matrix about a unit axis through the origin (right-handed). */
export function rotationAbout(axis, degrees) {
  const [ux, uy, uz] = unit(axis);
  const angle = (degrees * Math.PI) / 180;
  const c = Math.cos(angle);
  const s = Math.sin(angle);
  const t = 1 - c;
  return [
    [t * ux * ux + c, t * ux * uy - s * uz, t * ux * uz + s * uy],
    [t * ux * uy + s * uz, t * uy * uy + c, t * uy * uz - s * ux],
    [t * ux * uz - s * uy, t * uy * uz + s * ux, t * uz * uz + c],
  ];
}

export function applyMatrix(m, p) {
  return [
    m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2],
    m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2],
    m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2],
  ];
}

/** Degrees between two directions, treating antiparallel as parallel. */
export function angleBetween(a, b) {
  const cosine = Math.abs(dot(unit(a), unit(b)));
  return (Math.acos(Math.max(-1, Math.min(1, cosine))) * 180) / Math.PI;
}

/* ----------------------------------------------------------------- components */

export class LandscapeError extends Error {}

/**
 * One rigid part of the assembly.
 *
 * Coordinates are flat Float32Arrays rather than an array of triples: the scan
 * walks them tens of millions of times, and the difference between a packed
 * buffer and a list of three-element arrays is the difference between a scan
 * that feels instant and one that does not.
 */
export class Component {
  constructor(name, chains) {
    this.name = name;
    this.chains = chains;
    this.x = null; this.y = null; this.z = null;
    this.radii = null;
    this.byChain = new Map();
  }

  get count() { return this.radii ? this.radii.length : 0; }

  centre() {
    let sx = 0, sy = 0, sz = 0;
    for (let i = 0; i < this.count; i++) { sx += this.x[i]; sy += this.y[i]; sz += this.z[i]; }
    return this.count ? [sx / this.count, sy / this.count, sz / this.count] : [0, 0, 0];
  }
}

/**
 * Pull the two components out of a loaded structure, by chain id.
 *
 * `matrices` is the per-chain world transform, so a structure the user has
 * moved is scanned where it sits rather than where the file put it -- the same
 * thing `chainMatrices()` feeds the PDB writer.
 *
 * Hydrogens and waters are dropped: deposited structures mostly have no
 * hydrogens and predicted ones mostly do, and a landscape that changes
 * depending on which you fed it is worse than one computed on heavy atoms
 * throughout.
 */
export function splitComponents(structure, rotorChains, axleChains, matrices = null) {
  const wanted = new Map();
  for (const id of rotorChains) wanted.set(id, 'rotor');
  for (const id of axleChains) {
    if (wanted.get(id) === 'rotor') throw new LandscapeError(`chain ${id} is in both the rotor and the axle`);
    wanted.set(id, 'axle');
  }

  const parts = {
    rotor: new Component('rotor', [...rotorChains]),
    axle: new Component('axle', [...axleChains]),
  };
  const gathered = { rotor: [], axle: [] };

  const point = [0, 0, 0];
  for (const residue of structure.residues) {
    const chain = structure.chains[residue.chainIndex];
    const which = chain ? wanted.get(chain.id) : undefined;
    if (!which || residue.kind === Kind.WATER) continue;
    const matrix = matrices ? matrices[residue.chainIndex] : null;

    for (let i = residue.start; i <= residue.end; i++) {
      const code = structure.element[i];
      if (code === 1) continue;  // hydrogen
      transform(matrix, structure.x[i], structure.y[i], structure.z[i], point);
      const radius = VDW_RADII[code] || DEFAULT_RADIUS;
      gathered[which].push(point[0], point[1], point[2], code === 0 ? DEFAULT_RADIUS : radius);

      // For superposing a chain onto its symmetry mates. Keyed the same way the
      // Python is, so both sides pair the same atoms.
      let byChain = parts[which].byChain.get(chain.id);
      if (!byChain) { byChain = new Map(); parts[which].byChain.set(chain.id, byChain); }
      byChain.set(`${residue.seq}|${residue.name}|${structure.atomName[i]}`,
        [point[0], point[1], point[2]]);
    }
  }

  for (const which of ['rotor', 'axle']) {
    const flat = gathered[which];
    const count = flat.length / 4;
    const part = parts[which];
    if (!count) {
      throw new LandscapeError(`the ${which} has no heavy atoms in chains `
        + `${part.chains.join(', ') || '(none given)'}`);
    }
    part.x = new Float64Array(count);
    part.y = new Float64Array(count);
    part.z = new Float64Array(count);
    part.radii = new Float64Array(count);
    for (let i = 0; i < count; i++) {
      part.x[i] = flat[i * 4];
      part.y[i] = flat[i * 4 + 1];
      part.z[i] = flat[i * 4 + 2];
      part.radii[i] = flat[i * 4 + 3];
    }
  }
  return parts;
}

function transform(m, x, y, z, out) {
  if (!m) { out[0] = x; out[1] = y; out[2] = z; return; }
  out[0] = m[0] * x + m[4] * y + m[8] * z + m[12];
  out[1] = m[1] * x + m[5] * y + m[9] * z + m[13];
  out[2] = m[2] * x + m[6] * y + m[10] * z + m[14];
}

/* ------------------------------------------------------------------- the axis */

/** The largest group of pairs that agree on one axis. */
function axisCluster(pairs) {
  let best = { size: 0, deviation: Infinity, members: [] };
  for (const [, candidate] of pairs) {
    const members = [];
    let deviation = 0;
    for (const [angle, axis] of pairs) {
      const separation = angleBetween(candidate, axis);
      if (separation <= AXIS_CLUSTER) {
        const flipped = dot(candidate, axis) >= 0 ? axis : [-axis[0], -axis[1], -axis[2]];
        members.push([angle, flipped]);
        deviation += separation;
      }
    }
    const mean = deviation / Math.max(1, members.length);
    if (members.length > best.size || (members.length === best.size && mean < best.deviation)) {
      best = { size: members.length, deviation: mean, members };
    }
  }
  const members = best.members.length ? best.members : [pairs[0]];
  return { angles: members.map((m) => m[0]), axes: members.map((m) => m[1]) };
}

/**
 * The n in Cn that explains a set of rotations about one axis, or 0.
 *
 * `count` pairs about the axis plus the reference they were measured against is
 * a ring of count+1, so that is the first candidate, accepted only if every
 * rotation really lands on a multiple of 360/n. The second recovers a ring with
 * a chain too disordered to superpose, and needs two pairs to corroborate it: a
 * single rotation of 51 degrees is within a fraction of a degree of 360/7 and
 * means nothing of the kind.
 */
export function foldFrom(angles, count) {
  if (!angles.length) return 0;
  const candidates = [count + 1];
  if (angles.length >= 2) {
    const smallest = Math.min(...angles);
    if (smallest > 1e-6) candidates.push(Math.round(360 / smallest));
  }
  for (const n of candidates) {
    if (n < 2 || n > 60) continue;
    const step = 360 / n;
    const worst = Math.max(...angles.map((a) => Math.abs(a - Math.round(a / step) * step)));
    if (worst <= FOLD_TOLERANCE) return n;
  }
  return 0;
}

/** The symmetry axis of one component, from its own chains. */
export function componentAxis(component) {
  const chains = component.chains.filter((c) => (component.byChain.get(c) || new Map()).size >= 4);
  if (chains.length < 2) return null;

  let reference = chains[0];
  for (const chain of chains) {
    if (component.byChain.get(chain).size > component.byChain.get(reference).size) reference = chain;
  }
  const referenceAtoms = component.byChain.get(reference);

  const pairs = [];
  for (const chain of chains) {
    if (chain === reference) continue;
    const other = component.byChain.get(chain);
    const moving = [];
    const fixed = [];
    for (const [key, position] of referenceAtoms) {
      const match = other.get(key);
      if (match) { moving.push(position); fixed.push(match); }
    }
    if (moving.length < 4) continue;
    const { angle, axis } = kabschRotation(moving, fixed);
    if (angle < 1 || angle > 359) continue;
    pairs.push([angle, axis]);
  }
  if (!pairs.length) return null;

  const { angles, axes } = axisCluster(pairs);
  const direction = unit(centroid(axes));
  const spread = Math.max(...axes.map((a) => angleBetween(a, direction)));
  // The centroid of a symmetric component lies on its symmetry axis: the
  // symmetry permutes the atoms, so it cannot move their mean.
  return { direction, point: component.centre(), fold: foldFrom(angles, axes.length), spread };
}

/** The long axis of a component, as a last resort: a rod turns about its length. */
export function inertiaAxis(component) {
  const centre = component.centre();
  const covariance = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
  for (let i = 0; i < component.count; i++) {
    const d = [component.x[i] - centre[0], component.y[i] - centre[1], component.z[i] - centre[2]];
    for (let a = 0; a < 3; a++) for (let b = 0; b < 3; b++) covariance[a][b] += d[a] * d[b];
  }
  return { direction: unit(jacobiEigen(covariance).vectors[0]), point: centre };
}

/** The axis the rotor turns about, and how much to believe it. */
export function detectAxis(rotor, axle, override = null) {
  if (override && override.direction) {
    return {
      direction: unit(override.direction), point: override.point || axle.centre(),
      source: 'given', rotorFold: 0, axleFold: 0, agreement: null, spread: null,
    };
  }

  const fromRotor = componentAxis(rotor);
  const fromAxle = componentAxis(axle);
  let direction, point, agreement = null, spread = null, source;

  if (fromRotor && fromAxle) {
    agreement = angleBetween(fromRotor.direction, fromAxle.direction);
    const other = dot(fromRotor.direction, fromAxle.direction) >= 0
      ? fromAxle.direction
      : [-fromAxle.direction[0], -fromAxle.direction[1], -fromAxle.direction[2]];
    direction = unit(centroid([fromRotor.direction, other]));
    point = fromAxle.point;
    spread = Math.max(fromRotor.spread, fromAxle.spread);
    source = 'both components';
  } else if (fromRotor || fromAxle) {
    const only = fromRotor || fromAxle;
    direction = only.direction;
    point = only.point;
    spread = only.spread;
    source = fromRotor ? 'the rotor' : 'the axle';
  } else {
    ({ direction, point } = inertiaAxis(axle));
    source = "the axle's long axis (no symmetry found)";
  }

  return {
    direction, point, source,
    rotorFold: foldOf(rotor, fromRotor),
    axleFold: foldOf(axle, fromAxle),
    agreement: agreement === null ? null : round(agreement, 3),
    spread: spread === null ? null : round(spread, 3),
  };
}

function foldOf(component, measured) {
  if (measured && measured.fold) return measured.fold;
  return component.chains.length === 1 ? 1 : 0;
}

/**
 * The period symmetry forces on the landscape, in degrees, or 0 if unknown.
 *
 * A Cn rotor turned by 360/n is the same rotor, so the interaction cannot tell
 * the two orientations apart; the axle says the same from the other side. Both
 * hold at once, so the period is 360/lcm(n, m).
 */
export function expectedPeriod(rotorFold, axleFold) {
  if (rotorFold < 1 || axleFold < 1) return 0;
  return 360 / lcm(rotorFold, axleFold);
}

function gcd(a, b) { while (b) { [a, b] = [b, a % b]; } return a; }
function lcm(a, b) { return (a / gcd(a, b)) * b; }

/* -------------------------------------------------------------- neighbours */

/**
 * A uniform cell list over a fixed set of points.
 *
 * Flat typed arrays rather than a Map of buckets: a scan asks this tens of
 * millions of times, and a hash lookup per cell is most of the cost if you let
 * it be. Cells are bucketed by counting sort into one contiguous index array.
 */
class Grid {
  constructor(x, y, z, indices, spacing) {
    this.spacing = spacing;
    let minX = Infinity, minY = Infinity, minZ = Infinity;
    let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    for (const i of indices) {
      if (x[i] < minX) minX = x[i];
      if (x[i] > maxX) maxX = x[i];
      if (y[i] < minY) minY = y[i];
      if (y[i] > maxY) maxY = y[i];
      if (z[i] < minZ) minZ = z[i];
      if (z[i] > maxZ) maxZ = z[i];
    }
    this.minX = minX; this.minY = minY; this.minZ = minZ;
    this.nx = Math.max(1, Math.floor((maxX - minX) / spacing) + 1);
    this.ny = Math.max(1, Math.floor((maxY - minY) / spacing) + 1);
    this.nz = Math.max(1, Math.floor((maxZ - minZ) / spacing) + 1);

    const cells = this.nx * this.ny * this.nz;
    const counts = new Int32Array(cells + 1);
    const cellOf = new Int32Array(indices.length);
    for (let k = 0; k < indices.length; k++) {
      const i = indices[k];
      const cell = this.#cell(x[i], y[i], z[i]);
      cellOf[k] = cell;
      counts[cell + 1]++;
    }
    for (let c = 0; c < cells; c++) counts[c + 1] += counts[c];
    this.start = counts;
    this.items = new Int32Array(indices.length);
    const cursor = counts.slice(0, cells);
    for (let k = 0; k < indices.length; k++) {
      this.items[cursor[cellOf[k]]++] = indices[k];
    }
  }

  #cell(px, py, pz) {
    const cx = Math.min(this.nx - 1, Math.max(0, Math.floor((px - this.minX) / this.spacing)));
    const cy = Math.min(this.ny - 1, Math.max(0, Math.floor((py - this.minY) / this.spacing)));
    const cz = Math.min(this.nz - 1, Math.max(0, Math.floor((pz - this.minZ) / this.spacing)));
    return (cz * this.ny + cy) * this.nx + cx;
  }

  /** Append every stored index within one cell of the point to `out`. */
  near(px, py, pz, out) {
    out.length = 0;
    const cx = Math.floor((px - this.minX) / this.spacing);
    const cy = Math.floor((py - this.minY) / this.spacing);
    const cz = Math.floor((pz - this.minZ) / this.spacing);
    for (let dz = -1; dz <= 1; dz++) {
      const gz = cz + dz;
      if (gz < 0 || gz >= this.nz) continue;
      for (let dy = -1; dy <= 1; dy++) {
        const gy = cy + dy;
        if (gy < 0 || gy >= this.ny) continue;
        const row = (gz * this.ny + gy) * this.nx;
        for (let dx = -1; dx <= 1; dx++) {
          const gx = cx + dx;
          if (gx < 0 || gx >= this.nx) continue;
          const cell = row + gx;
          for (let at = this.start[cell]; at < this.start[cell + 1]; at++) out.push(this.items[at]);
        }
      }
    }
    return out;
  }
}

/** Roughly even points on the unit sphere, by Fibonacci spiral. */
export function spherePoints(count = 64) {
  const points = new Float64Array(count * 3);
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < count; i++) {
    const z = 1 - (2 * (i + 0.5)) / count;
    const r = Math.sqrt(Math.max(0, 1 - z * z));
    const theta = golden * i;
    points[i * 3] = Math.cos(theta) * r;
    points[i * 3 + 1] = Math.sin(theta) * r;
    points[i * 3 + 2] = z;
  }
  return points;
}

const SPHERE = spherePoints();
const SPHERE_COUNT = SPHERE.length / 3;

/* --------------------------------------------------------------- the scorer */

/**
 * Buried area, overlap and interfacial gap. No dependencies, no force field.
 *
 * The score is `OVERLAP_WEIGHT * overlap - BURIAL_WEIGHT * buried`, in
 * arbitrary units, so lower is better packed. It is a shape measure. Calling it
 * an energy would be wrong, and calling it a binding energy would be wrong
 * twice.
 */
export class GeometricScorer {
  static id = 'geometric';
  static label = 'packing score';
  static unit = 'a.u.';

  prepare(rotor, axle, axis) {
    this.rotor = rotor;
    this.axle = axle;
    this.axis = axis;

    this.rotorInterface = candidates(rotor, axle, axis, BURIAL_REACH);
    this.axleInterface = candidates(axle, rotor, axis, BURIAL_REACH);

    // Occluders: every heavy atom of the component, since an atom at the
    // interface is still shaded by its own neighbours behind it.
    this.rotorAll = range(rotor.count);
    this.axleGrid = new Grid(axle.x, axle.y, axle.z, range(axle.count), BURIAL_REACH);
    this.axleNearGrid = new Grid(axle.x, axle.y, axle.z, this.axleInterface, BURIAL_REACH);

    // Scratch, reused across angles so the scan allocates nothing per step.
    this.rx = new Float64Array(rotor.count);
    this.ry = new Float64Array(rotor.count);
    this.rz = new Float64Array(rotor.count);
    this.sphere = new Float64Array(SPHERE.length);
    this.neighbours = [];
    this.nearRotor = new Int32Array(this.rotorInterface.length);
    this.touchedAxle = new Uint8Array(axle.count);
    this.touchedList = new Int32Array(this.axleInterface.length);
    this.occluderX = new Float64Array(256);
    this.occluderY = new Float64Array(256);
    this.occluderZ = new Float64Array(256);
    this.occluderR = new Float64Array(256);
  }

  /**
   * Turn the rotor to `degrees`, optionally slide it `rise` Angstroms along the
   * axis, and score the interface there.
   *
   * The rise is what makes a two-dimensional landscape: an assembly can be
   * free to turn only if it is also at the right height, and a scan that holds
   * the height fixed can miss that entirely.
   */
  score(degrees, rise = 0) {
    const rotor = this.rotor;
    const axle = this.axle;
    const matrix = rotationAbout(this.axis.direction, degrees);
    const [px, py, pz] = this.axis.point;
    const [ax, ay, az] = this.axis.direction;
    const sx = px + ax * rise;
    const sy = py + ay * rise;
    const sz = pz + az * rise;

    const m00 = matrix[0][0], m01 = matrix[0][1], m02 = matrix[0][2];
    const m10 = matrix[1][0], m11 = matrix[1][1], m12 = matrix[1][2];
    const m20 = matrix[2][0], m21 = matrix[2][1], m22 = matrix[2][2];

    const rx = this.rx, ry = this.ry, rz = this.rz;
    for (let i = 0; i < rotor.count; i++) {
      const dx = rotor.x[i] - px, dy = rotor.y[i] - py, dz = rotor.z[i] - pz;
      rx[i] = m00 * dx + m01 * dy + m02 * dz + sx;
      ry[i] = m10 * dx + m11 * dy + m12 * dz + sy;
      rz[i] = m20 * dx + m21 * dy + m22 * dz + sz;
    }
    // The sample sphere turns with the rotor, so two orientations symmetry
    // calls identical come out as the same number rather than within a few per
    // cent. Without it the noise invents minima.
    const sphere = this.sphere;
    for (let s = 0; s < SPHERE_COUNT; s++) {
      const dx = SPHERE[s * 3], dy = SPHERE[s * 3 + 1], dz = SPHERE[s * 3 + 2];
      sphere[s * 3] = m00 * dx + m01 * dy + m02 * dz;
      sphere[s * 3 + 1] = m10 * dx + m11 * dy + m12 * dz;
      sphere[s * 3 + 2] = m20 * dx + m21 * dy + m22 * dz;
    }

    let clashes = 0;
    let contacts = 0;
    let overlap = 0;
    const gaps = [];
    let nearRotorCount = 0;
    let touchedCount = 0;
    const touched = this.touchedAxle;
    touched.fill(0);
    const near = this.neighbours;

    for (const i of this.rotorInterface) {
      const ax = rx[i], ay = ry[i], az = rz[i];
      this.axleNearGrid.near(ax, ay, az, near);
      let closest = Infinity;
      let reachable = false;
      for (let n = 0; n < near.length; n++) {
        const j = near[n];
        const dx = ax - axle.x[j], dy = ay - axle.y[j], dz = az - axle.z[j];
        const squared = dx * dx + dy * dy + dz * dz;
        if (squared > BURIAL_REACH * BURIAL_REACH) continue;
        reachable = true;
        if (!touched[j]) { touched[j] = 1; this.touchedList[touchedCount++] = j; }
        if (squared > CONTACT_CUTOFF * CONTACT_CUTOFF) continue;
        const gap = Math.sqrt(squared) - (rotor.radii[i] + axle.radii[j]);
        if (gap < closest) closest = gap;
        contacts++;
        if (gap < -CLASH_TOLERANCE) {
          clashes++;
          const excess = -gap - CLASH_TOLERANCE;
          overlap += excess * excess;
        }
      }
      if (reachable) this.nearRotor[nearRotorCount++] = i;
      if (closest < Infinity) gaps.push(closest);
    }

    let buried = 0;
    if (nearRotorCount) {
      const rotorGrid = new Grid(rx, ry, rz, this.rotorAll, BURIAL_REACH);
      for (let k = 0; k < nearRotorCount; k++) {
        const i = this.nearRotor[k];
        buried += this.#buriedArea(i, rx, ry, rz, rotor.radii, rotorGrid,
          axle.x, axle.y, axle.z, axle.radii, this.axleGrid, sphere);
      }
      for (let k = 0; k < touchedCount; k++) {
        const j = this.touchedList[k];
        buried += this.#buriedArea(j, axle.x, axle.y, axle.z, axle.radii, this.axleGrid,
          rx, ry, rz, rotor.radii, rotorGrid, sphere);
      }
    }

    gaps.sort((a, b) => a - b);
    return {
      angle: round(degrees, 4),
      rise: round(rise, 4),
      score: round(OVERLAP_WEIGHT * overlap - BURIAL_WEIGHT * buried, 4),
      bsa: round(buried, 1),
      clashes,
      contacts,
      overlap: round(overlap, 3),
      gap: gaps.length ? round(gaps[gaps.length >> 1], 3) : null,
    };
  }

  /**
   * How much area one atom loses to the other component.
   *
   * Both states come out of a single pass over the sample points: a point
   * shaded by the atom's own neighbours is not accessible in either, so it is
   * dropped before the other component is consulted at all.
   */
  #buriedArea(index, x, y, z, radii, ownGrid, ox, oy, oz, oradii, otherGrid, sphere) {
    const cx = x[index], cy = y[index], cz = z[index];
    const radius = radii[index] + PROBE;
    const near = this.neighbours;

    let ownCount = 0;
    ownGrid.near(cx, cy, cz, near);
    for (let n = 0; n < near.length; n++) {
      const other = near[n];
      if (other === index) continue;
      const reach = radius + radii[other] + PROBE;
      const dx = x[other] - cx, dy = y[other] - cy, dz = z[other] - cz;
      if (dx * dx + dy * dy + dz * dz >= reach * reach) continue;
      ownCount = this.#addOccluder(ownCount, x[other], y[other], z[other], radii[other] + PROBE);
    }
    const ownEnd = ownCount;

    otherGrid.near(cx, cy, cz, near);
    for (let n = 0; n < near.length; n++) {
      const other = near[n];
      const reach = radius + oradii[other] + PROBE;
      const dx = ox[other] - cx, dy = oy[other] - cy, dz = oz[other] - cz;
      if (dx * dx + dy * dy + dz * dz >= reach * reach) continue;
      ownCount = this.#addOccluder(ownCount, ox[other], oy[other], oz[other], oradii[other] + PROBE);
    }
    if (ownCount === ownEnd) return 0;  // nothing from the other side reaches it

    const qx = this.occluderX, qy = this.occluderY, qz = this.occluderZ, qr = this.occluderR;
    let lost = 0;
    for (let s = 0; s < SPHERE_COUNT; s++) {
      const sx = cx + sphere[s * 3] * radius;
      const sy = cy + sphere[s * 3 + 1] * radius;
      const sz = cz + sphere[s * 3 + 2] * radius;
      let blocked = false;
      for (let o = 0; o < ownEnd; o++) {
        const dx = sx - qx[o], dy = sy - qy[o], dz = sz - qz[o];
        if (dx * dx + dy * dy + dz * dz < qr[o] * qr[o]) { blocked = true; break; }
      }
      if (blocked) continue;
      for (let o = ownEnd; o < ownCount; o++) {
        const dx = sx - qx[o], dy = sy - qy[o], dz = sz - qz[o];
        if (dx * dx + dy * dy + dz * dz < qr[o] * qr[o]) { lost++; break; }
      }
    }
    return (4 * Math.PI * radius * radius * lost) / SPHERE_COUNT;
  }

  #addOccluder(count, x, y, z, radius) {
    if (count === this.occluderX.length) {
      const grow = (old) => { const next = new Float64Array(old.length * 2); next.set(old); return next; };
      this.occluderX = grow(this.occluderX);
      this.occluderY = grow(this.occluderY);
      this.occluderZ = grow(this.occluderZ);
      this.occluderR = grow(this.occluderR);
    }
    this.occluderX[count] = x;
    this.occluderY[count] = y;
    this.occluderZ[count] = z;
    this.occluderR[count] = radius;
    return count + 1;
  }
}

/**
 * Atoms of one component that could reach the other at *some* angle.
 *
 * Rotation preserves distance from the axis and height along it, so an atom's
 * (radius, height) is fixed for the whole scan: anything whose (radius, height)
 * is more than `reach` from every atom of the other part never comes close at
 * any angle. For a ring on a rod that is the great majority of both.
 */
function candidates(component, other, axis, reach) {
  const [dx, dy, dz] = axis.direction;
  const [px, py, pz] = axis.point;
  const cylindrical = (x, y, z) => {
    const ux = x - px, uy = y - py, uz = z - pz;
    const height = ux * dx + uy * dy + uz * dz;
    const radial = Math.sqrt(Math.max(0, ux * ux + uy * uy + uz * uz - height * height));
    return [radial, height];
  };

  const occupied = new Set();
  for (let i = 0; i < other.count; i++) {
    const [radial, height] = cylindrical(other.x[i], other.y[i], other.z[i]);
    occupied.add(`${Math.floor(radial / reach)}|${Math.floor(height / reach)}`);
  }

  const kept = [];
  for (let i = 0; i < component.count; i++) {
    const [radial, height] = cylindrical(component.x[i], component.y[i], component.z[i]);
    const cr = Math.floor(radial / reach);
    const ch = Math.floor(height / reach);
    let near = false;
    for (let dr = -1; dr <= 1 && !near; dr++) {
      for (let dh = -1; dh <= 1; dh++) {
        if (occupied.has(`${cr + dr}|${ch + dh}`)) { near = true; break; }
      }
    }
    if (near) kept.push(i);
  }
  return Int32Array.from(kept);
}

function range(n) {
  const out = new Int32Array(n);
  for (let i = 0; i < n; i++) out[i] = i;
  return out;
}

function round(value, digits) {
  const scale = 10 ** digits;
  return Math.round(value * scale) / scale;
}

/* ------------------------------------------------------------------ the scan */

/**
 * The translations along the axis to scan at, usually just zero.
 *
 * Mirrors `rise_list` in landscape.py, cap included: every extra step
 * multiplies the work by one more whole turn.
 */
export function riseList(rise) {
  if (!rise) return [0];
  let low = Number(rise.min ?? 0);
  let high = Number(rise.max ?? 0);
  const step = Number(rise.step ?? 1) || 1;
  if (high < low) [low, high] = [high, low];
  if (!(step > 0)) throw new LandscapeError('the rise step has to be positive');
  const count = Math.floor((high - low) / step + 1e-9) + 1;
  if (count > 41) throw new LandscapeError(`${count} rise steps is too many; widen the step`);
  return Array.from({ length: count }, (_, i) => round(low + i * step, 4));
}

/** The angles a scan visits: a whole turn, open at the top. */
export function angleList(step) {
  if (!(step > 0) || step > 180) throw new LandscapeError('the step has to be between 0 and 180 degrees');
  const count = Math.round(360 / step);
  if (Math.abs(count * step - 360) > 1e-6) {
    throw new LandscapeError(`${step} degrees does not divide 360 a whole number of times`);
  }
  return Array.from({ length: count }, (_, i) => round(i * step, 6));
}

/**
 * Score every angle in turn, calling `onPoint` as each lands.
 *
 * Reporting per angle rather than at the end is what lets the curve draw itself
 * while the scan runs, and what lets a worker post progress back.
 */
export function scan(rotor, axle, axis, angles, rises = [0], onPoint = null) {
  const scorer = new GeometricScorer();
  scorer.prepare(rotor, axle, axis);
  const points = [];
  const total = angles.length * rises.length;
  for (const angle of angles) {
    for (const rise of rises) {
      const point = scorer.score(angle, rise);
      points.push(point);
      if (onPoint) onPoint(point, points.length, total);
    }
  }
  return points;
}

export { descriptors } from './landscape-descriptors.js';
