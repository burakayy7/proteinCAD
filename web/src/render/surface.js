// Molecular surface: a Gaussian density field contoured with surface nets.
//
// Each atom contributes exp(-d^2 / 2s^2) with s tied to its van der Waals
// radius, so overlapping atoms merge into one smooth envelope -- the shape you
// want when looking at a whole nanostructure rather than individual residues.
//
// The contouring itself is surface nets, in isosurface.js -- shared with the
// cryo-EM map renderer, which contours an identical kind of field.

import { contourField } from './isosurface.js';
import { SpatialGrid } from '../core/grid.js';
import { VDW_RADII } from '../core/elements.js';

const MAX_VOXELS = 5_000_000;

/**
 * @param {Structure} structure
 * @param {Int32Array|number[]} atomIndices atoms to include
 * @param {{resolution?: number, smoothness?: number, radiusOffset?: number}} options
 * @returns {THREE.BufferGeometry|null}
 */
export function buildSurface(structure, atomIndices, options = {}) {
  const count = atomIndices.length;
  if (count < 4) return null;

  const smoothness = options.smoothness ?? 1.7;
  const radiusOffset = options.radiusOffset ?? 0.4;
  const iso = Math.exp(-(smoothness * smoothness) / 2);

  // --- grid -----------------------------------------------------------------
  let minX = Infinity, minY = Infinity, minZ = Infinity;
  let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
  let maxRadius = 0;
  for (let k = 0; k < count; k++) {
    const i = atomIndices[k];
    const r = VDW_RADII[structure.element[i]] + radiusOffset;
    if (r > maxRadius) maxRadius = r;
    if (structure.x[i] < minX) minX = structure.x[i];
    if (structure.x[i] > maxX) maxX = structure.x[i];
    if (structure.y[i] < minY) minY = structure.y[i];
    if (structure.y[i] > maxY) maxY = structure.y[i];
    if (structure.z[i] < minZ) minZ = structure.z[i];
    if (structure.z[i] > maxZ) maxZ = structure.z[i];
  }
  const pad = maxRadius * 1.8;
  minX -= pad; minY -= pad; minZ -= pad;
  maxX += pad; maxY += pad; maxZ += pad;

  let voxel = options.resolution ?? (count <= 20000 ? 0.85 : count <= 100000 ? 1.1 : 1.4);
  let nx, ny, nz;
  for (let guard = 0; guard < 40; guard++) {
    nx = Math.ceil((maxX - minX) / voxel) + 1;
    ny = Math.ceil((maxY - minY) / voxel) + 1;
    nz = Math.ceil((maxZ - minZ) / voxel) + 1;
    if (nx * ny * nz <= MAX_VOXELS) break;
    voxel *= 1.25;
  }
  if (nx < 3 || ny < 3 || nz < 3) return null;

  // --- density --------------------------------------------------------------
  const field = new Float32Array(nx * ny * nz);
  const strideY = nx;
  const strideZ = nx * ny;

  for (let k = 0; k < count; k++) {
    const i = atomIndices[k];
    const radius = VDW_RADII[structure.element[i]] + radiusOffset;
    const sigma = radius / smoothness;
    const inv2Sigma2 = 1 / (2 * sigma * sigma);
    const cutoff = sigma * 2.8;
    const ax = structure.x[i], ay = structure.y[i], az = structure.z[i];

    const i0 = Math.max(0, Math.floor((ax - cutoff - minX) / voxel));
    const i1 = Math.min(nx - 1, Math.ceil((ax + cutoff - minX) / voxel));
    const j0 = Math.max(0, Math.floor((ay - cutoff - minY) / voxel));
    const j1 = Math.min(ny - 1, Math.ceil((ay + cutoff - minY) / voxel));
    const k0 = Math.max(0, Math.floor((az - cutoff - minZ) / voxel));
    const k1 = Math.min(nz - 1, Math.ceil((az + cutoff - minZ) / voxel));
    const cutoff2 = cutoff * cutoff;

    for (let gz = k0; gz <= k1; gz++) {
      const dz = minZ + gz * voxel - az;
      const dz2 = dz * dz;
      for (let gy = j0; gy <= j1; gy++) {
        const dy = minY + gy * voxel - ay;
        const dyz2 = dz2 + dy * dy;
        if (dyz2 > cutoff2) continue;
        let offset = gz * strideZ + gy * strideY + i0;
        for (let gx = i0; gx <= i1; gx++, offset++) {
          const dx = minX + gx * voxel - ax;
          const d2 = dyz2 + dx * dx;
          if (d2 > cutoff2) continue;
          field[offset] += Math.exp(-d2 * inv2Sigma2);
        }
      }
    }
  }

  // --- contour --------------------------------------------------------------
  // The geometry half is in isosurface.js, because a cryo-EM map contours the
  // same way: the only thing that differs is what fills the field and what each
  // vertex is attributed to.
  const nearest = new SpatialGrid(
    structure.x, structure.y, structure.z,
    atomIndices instanceof Int32Array ? atomIndices : Int32Array.from(atomIndices),
    Math.max(3, maxRadius)
  );
  const reach = maxRadius * 3;
  const fallback = atomIndices[0];

  const geometry = contourField(
    field,
    { nx, ny, nz },
    iso,
    { ox: minX, oy: minY, oz: minZ, dx: voxel, dy: voxel, dz: voxel },
    {
      attribute: (px, py, pz) => {
        const atom = nearest.closest(px, py, pz, reach);
        return atom < 0 ? fallback : atom;
      },
    }
  );
  if (!geometry) return null;
  geometry.userData.voxelSize = voxel;
  return geometry;
}
