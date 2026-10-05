// Secondary structure assignment (a compact DSSP).
//
// Files that carry HELIX/SHEET (or struct_conf) records use those. Everything
// else -- predicted models, edited structures, coordinates coming back from a
// design run -- gets assigned here, using the Kabsch & Sander backbone
// hydrogen-bond energy and the standard turn/bridge patterns.

import { SpatialGrid } from './grid.js';
import { Kind, SS } from './structure.js';

const HB_ENERGY_CUTOFF = -0.5;
const Q = 0.084 * 332; // kcal/mol * charge product from the DSSP paper
const NEIGHBOUR_CUTOFF = 9.0;

function sub(s, i, j, out) {
  out[0] = s.x[i] - s.x[j]; out[1] = s.y[i] - s.y[j]; out[2] = s.z[i] - s.z[j];
  return out;
}

function dist(s, i, j) {
  const dx = s.x[i] - s.x[j], dy = s.y[i] - s.y[j], dz = s.z[i] - s.z[j];
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

function distPoint(s, i, p) {
  const dx = s.x[i] - p[0], dy = s.y[i] - p[1], dz = s.z[i] - p[2];
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

/**
 * @param {Structure} structure
 * @param {{force?: boolean}} options force re-assignment even if the file had records
 * @returns {boolean} whether an assignment was computed
 */
export function assignSecondaryStructure(structure, options = {}) {
  if (structure.hasSecondaryStructure && !options.force) return false;

  const s = structure;
  const list = [];
  for (const res of s.residues) {
    if (res.kind === Kind.PROTEIN && res.n >= 0 && res.ca >= 0 && res.c >= 0 && res.o >= 0) {
      list.push(res);
    }
    res.ss = SS.COIL;
  }
  const R = list.length;
  if (R < 4) return false;

  // Amide hydrogen: 1 A from N, pointing opposite the preceding carbonyl.
  const hx = new Float32Array(R), hy = new Float32Array(R), hz = new Float32Array(R);
  const donor = new Uint8Array(R);
  const tmp = [0, 0, 0];
  for (let i = 1; i < R; i++) {
    const res = list[i], prev = list[i - 1];
    if (prev.index !== res.index - 1 || !prev.linkNext || res.name === 'PRO') continue;
    sub(s, prev.c, prev.o, tmp);
    const len = Math.hypot(tmp[0], tmp[1], tmp[2]);
    if (len < 1e-4) continue;
    hx[i] = s.x[res.n] + tmp[0] / len;
    hy[i] = s.y[res.n] + tmp[1] / len;
    hz[i] = s.z[res.n] + tmp[2] / len;
    donor[i] = 1;
  }

  // hbond(a, b): the carbonyl of `a` donates to the amide of `b`.
  const bonds = new Set();
  const caIdx = Int32Array.from(list, (r) => r.ca);
  const gridPoints = new Int32Array(R);
  for (let i = 0; i < R; i++) gridPoints[i] = i;
  const cax = Float32Array.from(caIdx, (i) => s.x[i]);
  const cay = Float32Array.from(caIdx, (i) => s.y[i]);
  const caz = Float32Array.from(caIdx, (i) => s.z[i]);
  const grid = new SpatialGrid(cax, cay, caz, gridPoints, NEIGHBOUR_CUTOFF);

  const h = [0, 0, 0];
  for (let j = 0; j < R; j++) {
    if (!donor[j]) continue;
    const resJ = list[j];
    h[0] = hx[j]; h[1] = hy[j]; h[2] = hz[j];
    grid.near(cax[j], cay[j], caz[j], NEIGHBOUR_CUTOFF, (i) => {
      if (i === j || Math.abs(i - j) < 2) return;
      const resI = list[i];
      const rON = dist(s, resI.o, resJ.n);
      if (rON > 5.2) return;
      const rCH = distPoint(s, resI.c, h);
      const rOH = distPoint(s, resI.o, h);
      const rCN = dist(s, resI.c, resJ.n);
      if (rON < 0.5 || rCH < 0.5 || rOH < 0.5 || rCN < 0.5) return;
      const e = Q * (1 / rON + 1 / rCH - 1 / rOH - 1 / rCN);
      if (e < HB_ENERGY_CUTOFF) bonds.add(i * R + j);
    });
  }

  const hbond = (a, b) => a >= 0 && b >= 0 && a < R && b < R && bonds.has(a * R + b);
  const contiguous = (a, b) => {
    // Residues must be consecutive in the same chain for a turn to make sense.
    if (b <= a || b - a > 5) return false;
    for (let k = a; k < b; k++) if (!list[k].linkNext || list[k].index + 1 !== list[k + 1].index) return false;
    return true;
  };

  const ss = new Uint8Array(R); // 0 coil, 1 helix, 2 sheet

  // Helices: two consecutive n-turns. 4 first (alpha), then 3 and 5.
  for (const n of [4, 3, 5]) {
    for (let i = 0; i + n + 1 < R; i++) {
      if (!contiguous(i, i + n) || !contiguous(i + 1, i + 1 + n)) continue;
      if (hbond(i, i + n) && hbond(i + 1, i + 1 + n)) {
        for (let k = i + 1; k <= i + n; k++) if (!ss[k]) ss[k] = 1;
      }
    }
  }

  // Bridges: parallel and antiparallel ladders become strands.
  const bridge = new Uint8Array(R);
  for (let i = 1; i < R - 1; i++) {
    grid.near(cax[i], cay[i], caz[i], NEIGHBOUR_CUTOFF, (j) => {
      if (j <= i + 2 || j >= R - 1) return;
      const parallel = (hbond(i - 1, j) && hbond(j, i + 1)) || (hbond(j - 1, i) && hbond(i, j + 1));
      const anti = (hbond(i, j) && hbond(j, i)) || (hbond(i - 1, j + 1) && hbond(j - 1, i + 1));
      if (parallel || anti) { bridge[i] = 1; bridge[j] = 1; }
    });
  }
  for (let i = 0; i < R; i++) if (bridge[i]) ss[i] = 2;

  // Clean up: strands need a partner residue, helices need at least one turn.
  removeShortRuns(ss, 1, 3);
  removeShortRuns(ss, 2, 2);

  for (let i = 0; i < R; i++) {
    list[i].ss = ss[i] === 1 ? SS.HELIX : ss[i] === 2 ? SS.SHEET : SS.COIL;
  }
  s.hasSecondaryStructure = true;
  s.ssSource = 'computed';
  return true;
}

function removeShortRuns(ss, code, minLength) {
  let start = -1;
  for (let i = 0; i <= ss.length; i++) {
    const isCode = i < ss.length && ss[i] === code;
    if (isCode && start < 0) start = i;
    else if (!isCode && start >= 0) {
      if (i - start < minLength) for (let k = start; k < i; k++) ss[k] = 0;
      start = -1;
    }
  }
}
