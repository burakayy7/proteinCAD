// Cartoon / ribbon representation.
//
// One extruded tube per polymer segment. The cross-section is a superellipse
// whose width, thickness and "squareness" vary along the chain, which is what
// turns a single sweep routine into helices (wide flat ribbons), strands (flat
// arrows) and loops (round tubes) without any per-type geometry code:
//
//   helix  w=1.45 t=0.28   |####|      strand   w=1.25 t=0.28  |####|
//   loop   w=0.32 t=0.32     ()        arrow    w=2.05 -> 0.08  >
//
// The frame that carries the cross-section comes from the peptide plane
// (carbonyl direction), so ribbons face the way a chemist expects; when the
// carbonyl is missing (CA-only models) it falls back to parallel transport.

import { MeshBuilder } from './geometry.js';
import { Kind, SS } from '../core/structure.js';

const DIMS = {
  [SS.HELIX]: [1.45, 0.28],
  [SS.SHEET]: [1.25, 0.28],
  [SS.COIL]: [0.32, 0.32],
  [SS.TURN]: [0.42, 0.42],
};
const NUCLEIC_DIMS = [0.80, 0.45];
const ARROW_BASE = 2.05;
const ARROW_TIP = 0.08;

export const QUALITY = {
  high: { segments: 8, profile: 12 },
  medium: { segments: 5, profile: 8 },
  low: { segments: 3, profile: 6 },
};

export function autoQuality(residueCount) {
  if (residueCount <= 2500) return 'high';
  if (residueCount <= 15000) return 'medium';
  return 'low';
}

/**
 * @param {Structure} structure
 * @param {Uint8Array|null} mask atom mask limiting what is drawn
 * @param {{style?: 'cartoon'|'tube', quality?: string, chainIndex?: number, tubeRadius?: number}} options
 * @returns {THREE.BufferGeometry|null}
 */
export function buildCartoon(structure, mask, options = {}) {
  const segments = collectSegments(structure, mask, options.chainIndex);
  if (!segments.length) return null;

  let residueTotal = 0;
  for (const seg of segments) residueTotal += seg.length;
  const q = QUALITY[options.quality || autoQuality(residueTotal)] || QUALITY.medium;
  const style = options.style || 'cartoon';
  const tubeRadius = options.tubeRadius ?? 0.35;

  const builder = new MeshBuilder(residueTotal * q.segments * q.profile + 64);
  for (const seg of segments) {
    extrudeSegment(structure, seg, builder, q, style, tubeRadius);
  }
  return builder.isEmpty ? null : builder.build();
}

/** Guide atom for a residue: CA for proteins, C3' (or P) for nucleic acids. */
function guideAtom(res) {
  if (res.kind === Kind.PROTEIN) return res.ca;
  if (res.kind === Kind.NUCLEIC) return res.c3 >= 0 ? res.c3 : res.p;
  return -1;
}

function residueVisible(structure, res, mask) {
  if (!mask) return true;
  for (let i = res.start; i <= res.end; i++) if (mask[i]) return true;
  return false;
}

/** Maximal runs of connected polymer residues that survive the mask. */
function collectSegments(structure, mask, chainIndex) {
  const segments = [];
  let current = null;
  const chains = chainIndex === undefined ? structure.chains : [structure.chains[chainIndex]];

  for (const chain of chains) {
    if (!chain) continue;
    current = null;
    for (let r = chain.residueStart; r <= chain.residueEnd; r++) {
      const res = structure.residues[r];
      const guide = guideAtom(res);
      const ok = guide >= 0 && residueVisible(structure, res, mask);
      if (!ok) { current = null; continue; }
      if (current && current.length) {
        const prev = current[current.length - 1];
        if (!prev.linkNext || prev.index + 1 !== res.index || prev.kind !== res.kind) current = null;
      }
      if (!current) { current = []; segments.push(current); }
      current.push(res);
    }
  }
  return segments.filter((s) => s.length >= 2);
}

const tmpA = [0, 0, 0];
const tmpB = [0, 0, 0];

function cross(ax, ay, az, bx, by, bz, out) {
  out[0] = ay * bz - az * by;
  out[1] = az * bx - ax * bz;
  out[2] = ax * by - ay * bx;
  return out;
}

function normalize(v) {
  const len = Math.hypot(v[0], v[1], v[2]);
  if (len < 1e-6) { v[0] = 0; v[1] = 0; v[2] = 1; return v; }
  v[0] /= len; v[1] /= len; v[2] /= len;
  return v;
}

function extrudeSegment(structure, residues, builder, quality, style, tubeRadius) {
  const n = residues.length;
  const guides = new Int32Array(n);
  for (let i = 0; i < n; i++) guides[i] = guideAtom(residues[i]);

  // --- control points -------------------------------------------------------
  const px = new Float32Array(n), py = new Float32Array(n), pz = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    px[i] = structure.x[guides[i]];
    py[i] = structure.y[guides[i]];
    pz[i] = structure.z[guides[i]];
  }
  // Helices are a corkscrew of CA positions; smoothing removes the wobble and
  // leaves the ribbon following the helical axis instead.
  if (style === 'cartoon') {
    const sx = px.slice(), sy = py.slice(), sz = pz.slice();
    for (let i = 1; i < n - 1; i++) {
      if (residues[i].ss !== SS.HELIX) continue;
      if (residues[i - 1].ss !== SS.HELIX || residues[i + 1].ss !== SS.HELIX) continue;
      px[i] = (sx[i - 1] + 2 * sx[i] + sx[i + 1]) / 4;
      py[i] = (sy[i - 1] + 2 * sy[i] + sy[i + 1]) / 4;
      pz[i] = (sz[i - 1] + 2 * sz[i] + sz[i + 1]) / 4;
    }
  }

  // --- ribbon normals (thickness direction) ---------------------------------
  const ux = new Float32Array(n), uy = new Float32Array(n), uz = new Float32Array(n);
  let haveLast = false;
  for (let i = 0; i < n; i++) {
    const res = residues[i];
    const next = i < n - 1 ? i + 1 : i - 1;
    const dirX = px[next] - px[i], dirY = py[next] - py[i], dirZ = pz[next] - pz[i];
    const sign = i < n - 1 ? 1 : -1;

    let refX = 0, refY = 0, refZ = 0, haveRef = false;
    if (res.kind === Kind.PROTEIN && res.o >= 0 && res.c >= 0) {
      refX = structure.x[res.o] - structure.x[res.c];
      refY = structure.y[res.o] - structure.y[res.c];
      refZ = structure.z[res.o] - structure.z[res.c];
      haveRef = true;
    } else if (res.kind === Kind.NUCLEIC && res.c1 >= 0 && res.c3 >= 0) {
      refX = structure.x[res.c1] - structure.x[res.c3];
      refY = structure.y[res.c1] - structure.y[res.c3];
      refZ = structure.z[res.c1] - structure.z[res.c3];
      haveRef = true;
    }

    tmpB[0] = dirX * sign; tmpB[1] = dirY * sign; tmpB[2] = dirZ * sign;
    normalize(tmpB);

    if (haveRef) {
      cross(tmpB[0], tmpB[1], tmpB[2], refX, refY, refZ, tmpA);
      normalize(tmpA);
    } else if (haveLast) {
      // Parallel transport: carry the previous normal forward, re-orthogonalised.
      tmpA[0] = ux[i - 1]; tmpA[1] = uy[i - 1]; tmpA[2] = uz[i - 1];
      orthogonalize(tmpA, tmpB[0], tmpB[1], tmpB[2]);
      normalize(tmpA);
    } else {
      arbitraryPerpendicular(tmpB[0], tmpB[1], tmpB[2], tmpA);
    }

    // Consecutive carbonyls alternate direction along a helix; flipping keeps
    // the ribbon from twisting 180 degrees every residue.
    if (haveLast) {
      const dot = tmpA[0] * ux[i - 1] + tmpA[1] * uy[i - 1] + tmpA[2] * uz[i - 1];
      if (dot < 0) { tmpA[0] = -tmpA[0]; tmpA[1] = -tmpA[1]; tmpA[2] = -tmpA[2]; }
    }
    ux[i] = tmpA[0]; uy[i] = tmpA[1]; uz[i] = tmpA[2];
    haveLast = true;
  }

  // --- per-residue cross-section dimensions ---------------------------------
  const widths = new Float32Array(n), thicks = new Float32Array(n);
  const arrowHead = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const res = residues[i];
    let dims;
    if (style === 'tube') dims = [tubeRadius, tubeRadius];
    else if (res.kind === Kind.NUCLEIC) dims = NUCLEIC_DIMS;
    else dims = DIMS[res.ss] || DIMS[SS.COIL];
    widths[i] = dims[0];
    thicks[i] = dims[1];
    if (style === 'cartoon' && res.kind === Kind.PROTEIN && res.ss === SS.SHEET) {
      const isLast = i === n - 1 || residues[i + 1].ss !== SS.SHEET;
      // The arrow occupies the interval leading into the last strand residue.
      const interval = i === n - 1 ? i - 1 : i;
      if (isLast && interval >= 0) arrowHead[interval] = 1;
    }
  }

  // --- sweep ----------------------------------------------------------------
  const segs = quality.segments;
  const profile = quality.profile;
  const pos = [0, 0, 0], tan = [0, 0, 0], up = [0, 0, 0], right = [0, 0, 0];
  let previousRing = -1;
  let ringCount = 0;

  const emitRing = (t, width, thick, atomIndex) => {
    sampleSpline(px, py, pz, n, t, pos, tan);
    normalize(tan);
    interpolateUp(ux, uy, uz, n, t, up);
    orthogonalize(up, tan[0], tan[1], tan[2]);
    normalize(up);
    cross(up[0], up[1], up[2], tan[0], tan[1], tan[2], right);
    normalize(right);
    const first = addRing(builder, pos, right, up, width, thick, profile, atomIndex);
    if (previousRing >= 0) stitch(builder, previousRing, first, profile);
    else capRing(builder, first, profile, pos, tan, -1, atomIndex);
    previousRing = first;
    ringCount++;
    return first;
  };

  for (let i = 0; i < n - 1; i++) {
    if (arrowHead[i]) {
      // Two rings in the same place, narrow then wide, make the flat shoulder;
      // the taper then runs all the way to t = i + 1, where the next interval
      // re-opens at its own width and closes the arrow with a flat face.
      emitRing(i, widths[i], thicks[i], guides[i]);
      emitRing(i, ARROW_BASE, thicks[i], guides[i]);
      for (let s = 1; s <= segs; s++) {
        const f = s / segs;
        emitRing(i + f, ARROW_BASE + (ARROW_TIP - ARROW_BASE) * f, thicks[i], guides[Math.round(i + f)]);
      }
      continue;
    }
    for (let s = 0; s < segs; s++) {
      const f = s / segs;
      const smooth = f * f * (3 - 2 * f);
      emitRing(
        i + f,
        widths[i] + (widths[i + 1] - widths[i]) * smooth,
        thicks[i] + (thicks[i + 1] - thicks[i]) * smooth,
        guides[Math.round(i + f)]
      );
    }
  }
  // The arrow already placed a ring on the final residue.
  if (!arrowHead[n - 2]) emitRing(n - 1, widths[n - 1], thicks[n - 1], guides[n - 1]);

  if (previousRing >= 0 && ringCount > 1) {
    sampleSpline(px, py, pz, n, n - 1, pos, tan);
    normalize(tan);
    capRing(builder, previousRing, profile, pos, tan, 1, guides[n - 1]);
  }
}

function orthogonalize(v, tx, ty, tz) {
  const dot = v[0] * tx + v[1] * ty + v[2] * tz;
  v[0] -= tx * dot; v[1] -= ty * dot; v[2] -= tz * dot;
  return v;
}

function arbitraryPerpendicular(x, y, z, out) {
  if (Math.abs(x) < Math.abs(y) && Math.abs(x) < Math.abs(z)) cross(x, y, z, 1, 0, 0, out);
  else if (Math.abs(y) < Math.abs(z)) cross(x, y, z, 0, 1, 0, out);
  else cross(x, y, z, 0, 0, 1, out);
  return normalize(out);
}

/** Uniform Catmull-Rom position and tangent at parameter `t` in [0, n-1]. */
function sampleSpline(px, py, pz, n, t, pos, tan) {
  const i = Math.min(n - 2, Math.max(0, Math.floor(t)));
  const f = Math.min(1, Math.max(0, t - i));
  const i0 = Math.max(0, i - 1), i1 = i, i2 = Math.min(n - 1, i + 1), i3 = Math.min(n - 1, i + 2);
  pos[0] = catmull(px[i0], px[i1], px[i2], px[i3], f);
  pos[1] = catmull(py[i0], py[i1], py[i2], py[i3], f);
  pos[2] = catmull(pz[i0], pz[i1], pz[i2], pz[i3], f);
  tan[0] = catmullTangent(px[i0], px[i1], px[i2], px[i3], f);
  tan[1] = catmullTangent(py[i0], py[i1], py[i2], py[i3], f);
  tan[2] = catmullTangent(pz[i0], pz[i1], pz[i2], pz[i3], f);
}

function catmull(p0, p1, p2, p3, t) {
  const t2 = t * t, t3 = t2 * t;
  return 0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2 + (-p0 + 3 * p1 - 3 * p2 + p3) * t3);
}

function catmullTangent(p0, p1, p2, p3, t) {
  const t2 = t * t;
  return 0.5 * ((-p0 + p2) + 2 * (2 * p0 - 5 * p1 + 4 * p2 - p3) * t + 3 * (-p0 + 3 * p1 - 3 * p2 + p3) * t2);
}

function interpolateUp(ux, uy, uz, n, t, out) {
  const i = Math.min(n - 2, Math.max(0, Math.floor(t)));
  const f = Math.min(1, Math.max(0, t - i));
  const j = Math.min(n - 1, i + 1);
  out[0] = ux[i] + (ux[j] - ux[i]) * f;
  out[1] = uy[i] + (uy[j] - uy[i]) * f;
  out[2] = uz[i] + (uz[j] - uz[i]) * f;
  return out;
}

/** Superellipse exponent: 2 is an ellipse, higher is more rectangular. */
function exponentFor(width, thick) {
  const lo = Math.min(width, thick), hi = Math.max(width, thick);
  if (hi < 1e-5) return 2;
  return 2 + 4 * (1 - lo / hi);
}

function addRing(builder, center, right, up, width, thick, profile, atomIndex) {
  const p = exponentFor(width, thick);
  const invP = 2 / p;
  const w = Math.max(width, 1e-4), h = Math.max(thick, 1e-4);
  let first = -1;
  for (let k = 0; k < profile; k++) {
    const angle = (k / profile) * Math.PI * 2;
    const c = Math.cos(angle), s = Math.sin(angle);
    const cx = Math.sign(c) * Math.pow(Math.abs(c), invP) * w;
    const cy = Math.sign(s) * Math.pow(Math.abs(s), invP) * h;
    // Outward normal of the superellipse, mapped into the ribbon frame.
    const gx = Math.sign(cx) * Math.pow(Math.abs(cx) / w, p - 1) / w;
    const gy = Math.sign(cy) * Math.pow(Math.abs(cy) / h, p - 1) / h;
    const nx = right[0] * gx + up[0] * gy;
    const ny = right[1] * gx + up[1] * gy;
    const nz = right[2] * gx + up[2] * gy;
    const len = Math.hypot(nx, ny, nz) || 1;
    const index = builder.vertex(
      center[0] + right[0] * cx + up[0] * cy,
      center[1] + right[1] * cx + up[1] * cy,
      center[2] + right[2] * cx + up[2] * cy,
      nx / len, ny / len, nz / len,
      atomIndex
    );
    if (k === 0) first = index;
  }
  return first;
}

function stitch(builder, ringA, ringB, profile) {
  for (let k = 0; k < profile; k++) {
    const k2 = (k + 1) % profile;
    builder.quad(ringA + k, ringA + k2, ringB + k2, ringB + k);
  }
}

const capScratch = new Float32Array(64 * 3);

function capRing(builder, ring, profile, center, tangent, direction, atomIndex) {
  const nx = tangent[0] * direction, ny = tangent[1] * direction, nz = tangent[2] * direction;
  // Copy the ring out first: adding vertices may reallocate the builder arrays.
  const scratch = profile * 3 <= capScratch.length ? capScratch : new Float32Array(profile * 3);
  for (let k = 0; k < profile; k++) {
    const src = (ring + k) * 3;
    scratch[k * 3] = builder.positions[src];
    scratch[k * 3 + 1] = builder.positions[src + 1];
    scratch[k * 3 + 2] = builder.positions[src + 2];
  }
  const hub = builder.vertex(center[0], center[1], center[2], nx, ny, nz, atomIndex);
  const firstCopy = builder.vertexCount;
  for (let k = 0; k < profile; k++) {
    builder.vertex(scratch[k * 3], scratch[k * 3 + 1], scratch[k * 3 + 2], nx, ny, nz, atomIndex);
  }
  for (let k = 0; k < profile; k++) {
    const k2 = (k + 1) % profile;
    if (direction < 0) builder.triangle(hub, firstCopy + k2, firstCopy + k);
    else builder.triangle(hub, firstCopy + k, firstCopy + k2);
  }
}
