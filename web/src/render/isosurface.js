// Surface nets: one vertex per sign-changing cell, quads across sign-changing
// edges.
//
// Used instead of marching cubes for the same two reasons everywhere it
// appears: no 256-entry lookup table, and the dual mesh stays smooth at the
// coarse grids that large assemblies and downsampled maps both force.
//
// This takes a scalar field and knows nothing about where it came from. Two
// things produce one: the Gaussian envelope over a set of atoms in surface.js,
// and a cryo-EM density map off the disk. They differ in how the field is
// filled and in what each vertex is attributed to, and in nothing else -- so
// the half that is actually geometry lives here and is written once.

import { MeshBuilder } from './geometry.js';

const EDGES = [
  [0, 1], [1, 3], [2, 3], [0, 2],
  [4, 5], [5, 7], [6, 7], [4, 6],
  [0, 4], [1, 5], [2, 6], [3, 7],
];
// Corner c = (cx, cy, cz) with cx = c&1, cy = (c>>1)&1, cz = (c>>2)&1

/**
 * @param {Float32Array} field  values on an nx by ny by nz grid, x fastest
 * @param {{nx: number, ny: number, nz: number}} dims
 * @param {number} iso  the level to contour at
 * @param {{ox: number, oy: number, oz: number, dx: number, dy: number, dz: number}} place
 *   world position of grid point (i, j, k) is (ox + i*dx, oy + j*dy, oz + k*dz)
 * @param {{attribute?: (x: number, y: number, z: number) => number, estimate?: number}} options
 *   `attribute` records what each vertex came from -- an atom index, for the
 *   representations that colour and pick by atom. A field with no such thing
 *   behind it leaves it out and every vertex records zero.
 * @returns {THREE.BufferGeometry|null}
 */
export function contourField(field, dims, iso, place, options = {}) {
  const { nx, ny, nz } = dims;
  if (nx < 3 || ny < 3 || nz < 3) return null;

  const { ox, oy, oz, dx, dy, dz } = place;
  const attribute = options.attribute || null;
  const strideY = nx;
  const strideZ = nx * ny;

  const cellStrideY = nx - 1;
  const cellStrideZ = (nx - 1) * (ny - 1);
  const cellVertex = new Int32Array(cellStrideZ * (nz - 1)).fill(-1);
  const builder = new MeshBuilder(options.estimate || 65536);

  const corner = new Float32Array(8);
  const grad = [0, 0, 0];

  for (let cz = 0; cz < nz - 1; cz++) {
    for (let cy = 0; cy < ny - 1; cy++) {
      for (let cx = 0; cx < nx - 1; cx++) {
        const base = cz * strideZ + cy * strideY + cx;
        let inside = 0;
        for (let c = 0; c < 8; c++) {
          const v = field[base + (c & 1) + ((c >> 1) & 1) * strideY + ((c >> 2) & 1) * strideZ] - iso;
          corner[c] = v;
          if (v > 0) inside |= 1 << c;
        }
        if (inside === 0 || inside === 255) continue;

        let sx = 0, sy = 0, sz = 0, crossings = 0;
        for (const [a, b] of EDGES) {
          const va = corner[a], vb = corner[b];
          if ((va > 0) === (vb > 0)) continue;
          const t = va / (va - vb);
          const ax = a & 1, ay = (a >> 1) & 1, az = (a >> 2) & 1;
          const bx = b & 1, by = (b >> 1) & 1, bz = (b >> 2) & 1;
          sx += ax + (bx - ax) * t;
          sy += ay + (by - ay) * t;
          sz += az + (bz - az) * t;
          crossings++;
        }
        if (!crossings) continue;

        const fx = cx + sx / crossings;
        const fy = cy + sy / crossings;
        const fz = cz + sz / crossings;
        const px = ox + fx * dx;
        const py = oy + fy * dy;
        const pz = oz + fz * dz;

        gradientAt(field, nx, ny, nz, strideY, strideZ, fx, fy, fz, grad);
        // The gradient is in grid steps; dividing by the voxel size turns it
        // into a world-space direction, which matters the moment the voxel is
        // not a cube.
        grad[0] /= dx; grad[1] /= dy; grad[2] /= dz;
        const length = Math.hypot(grad[0], grad[1], grad[2]) || 1;

        cellVertex[cz * cellStrideZ + cy * cellStrideY + cx] = builder.vertex(
          px, py, pz,
          -grad[0] / length, -grad[1] / length, -grad[2] / length,
          attribute ? attribute(px, py, pz) : 0
        );
      }
    }
  }

  if (builder.vertexCount === 0) return null;

  // Quads: every sign-changing grid edge is shared by exactly four cells, and
  // those four cell vertices form one face of the dual mesh. A cell (cx,cy,cz)
  // spans corners cx..cx+1, so the cells around the +x edge at (gx,gy,gz) are
  // the ones with cx = gx, cy in {gy-1,gy}, cz in {gz-1,gz}.
  for (let gz = 1; gz < nz - 1; gz++) {
    for (let gy = 1; gy < ny - 1; gy++) {
      for (let gx = 1; gx < nx - 1; gx++) {
        const at = gz * strideZ + gy * strideY + gx;
        const inside = field[at] - iso > 0;

        if ((field[at + 1] - iso > 0) !== inside) {
          const base = gx + (gy - 1) * cellStrideY + (gz - 1) * cellStrideZ;
          quad(builder, cellVertex,
            base, base + cellStrideY, base + cellStrideY + cellStrideZ, base + cellStrideZ, inside);
        }
        if ((field[at + strideY] - iso > 0) !== inside) {
          const base = (gx - 1) + gy * cellStrideY + (gz - 1) * cellStrideZ;
          quad(builder, cellVertex,
            base, base + cellStrideZ, base + cellStrideZ + 1, base + 1, inside);
        }
        if ((field[at + strideZ] - iso > 0) !== inside) {
          const base = (gx - 1) + (gy - 1) * cellStrideY + gz * cellStrideZ;
          quad(builder, cellVertex,
            base, base + 1, base + 1 + cellStrideY, base + cellStrideY, inside);
        }
      }
    }
  }

  if (builder.isEmpty) return null;
  return builder.build();
}

/**
 * The four cell vertices wind counter-clockwise seen from the positive axis, so
 * they are emitted in order when the low corner is the inside one and reversed
 * otherwise -- that keeps every face pointing away from the density.
 */
function quad(builder, cellVertex, c0, c1, c2, c3, insideAtLowCorner) {
  const v0 = cellVertex[c0], v1 = cellVertex[c1], v2 = cellVertex[c2], v3 = cellVertex[c3];
  if (v0 < 0 || v1 < 0 || v2 < 0 || v3 < 0) return;
  if (insideAtLowCorner) builder.quad(v0, v1, v2, v3);
  else builder.quad(v0, v3, v2, v1);
}

/** Central-difference gradient, trilinearly blended to the vertex position. */
function gradientAt(field, nx, ny, nz, strideY, strideZ, fx, fy, fz, out) {
  const x0 = Math.min(nx - 2, Math.max(1, Math.floor(fx)));
  const y0 = Math.min(ny - 2, Math.max(1, Math.floor(fy)));
  const z0 = Math.min(nz - 2, Math.max(1, Math.floor(fz)));
  const tx = Math.min(1, Math.max(0, fx - x0));
  const ty = Math.min(1, Math.max(0, fy - y0));
  const tz = Math.min(1, Math.max(0, fz - z0));
  out[0] = 0; out[1] = 0; out[2] = 0;

  for (let c = 0; c < 8; c++) {
    const dx = c & 1, dy = (c >> 1) & 1, dz = (c >> 2) & 1;
    const gx = Math.min(nx - 2, x0 + dx);
    const gy = Math.min(ny - 2, y0 + dy);
    const gz = Math.min(nz - 2, z0 + dz);
    const w = (dx ? tx : 1 - tx) * (dy ? ty : 1 - ty) * (dz ? tz : 1 - tz);
    if (w < 1e-6) continue;
    const at = gz * strideZ + gy * strideY + gx;
    out[0] += w * (field[at + 1] - field[at - 1]);
    out[1] += w * (field[at + strideY] - field[at - strideY]);
    out[2] += w * (field[at + strideZ] - field[at - strideZ]);
  }
}
