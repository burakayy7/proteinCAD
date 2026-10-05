// Bond perception by interatomic distance.
//
// Two atoms are bonded when their separation is below the sum of their covalent
// radii plus a tolerance. Explicit CONECT records, when present, are merged in.
// The result is cached on the structure because only the stick-style
// representations need it.

import { SpatialGrid } from './grid.js';
import { COVALENT_RADII, ELEMENT_H } from './elements.js';
import { Kind } from './structure.js';

const TOLERANCE = 0.45;
const MAX_BOND = 3.0;
const MIN_BOND2 = 0.16;

export function getBonds(structure) {
  if (!structure.bonds) structure.bonds = computeBonds(structure);
  return structure.bonds;
}

export function computeBonds(structure) {
  const n = structure.atomCount;
  const { x, y, z, element } = structure;
  const a = [];
  const b = [];
  if (n === 0) return { a: new Int32Array(0), b: new Int32Array(0), count: 0 };

  // Waters are drawn as isolated spheres; excluding them keeps the grid small.
  const included = [];
  for (let i = 0; i < n; i++) {
    if (structure.residueOfAtom(i).kind !== Kind.WATER) included.push(i);
  }
  const grid = new SpatialGrid(x, y, z, Int32Array.from(included), MAX_BOND);
  const seen = new Set();

  for (let k = 0; k < included.length; k++) {
    const i = included[k];
    const ri = COVALENT_RADII[element[i]];
    const xi = x[i], yi = y[i], zi = z[i];
    grid.near(xi, yi, zi, MAX_BOND, (j) => {
      if (j <= i) return;
      const dx = x[j] - xi, dy = y[j] - yi, dz = z[j] - zi;
      const d2 = dx * dx + dy * dy + dz * dz;
      if (d2 < MIN_BOND2) return;
      // Hydrogens make exactly one bond; keeping the cut tight avoids clutter.
      const cut = ri + COVALENT_RADII[element[j]] + TOLERANCE;
      if (d2 > cut * cut) return;
      if (element[i] === ELEMENT_H && element[j] === ELEMENT_H) return;
      a.push(i); b.push(j);
      seen.add(i * n + j);
    });
  }

  if (structure.explicitBonds) {
    for (const [i, j] of structure.explicitBonds) {
      const lo = Math.min(i, j), hi = Math.max(i, j);
      if (seen.has(lo * n + hi)) continue;
      seen.add(lo * n + hi);
      a.push(lo); b.push(hi);
    }
  }

  return { a: Int32Array.from(a), b: Int32Array.from(b), count: a.length };
}
