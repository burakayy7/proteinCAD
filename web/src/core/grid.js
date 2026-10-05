// Uniform spatial hash used for bond perception, hydrogen-bond search and
// distance selections. Built with a counting sort so it allocates three typed
// arrays and nothing else.

const MAX_CELLS = 4_000_000;

export class SpatialGrid {
  /**
   * @param {Float32Array} xs
   * @param {Float32Array} ys
   * @param {Float32Array} zs
   * @param {Int32Array|number[]|null} indices subset to index, or null for all
   * @param {number} cellSize preferred cell edge in Angstrom
   */
  constructor(xs, ys, zs, indices, cellSize = 3.2) {
    this.xs = xs; this.ys = ys; this.zs = zs;
    this.indices = indices;
    const count = indices ? indices.length : xs.length;
    this.count = count;

    let minX = Infinity, minY = Infinity, minZ = Infinity;
    let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    for (let k = 0; k < count; k++) {
      const i = indices ? indices[k] : k;
      const x = xs[i], y = ys[i], z = zs[i];
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
      if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
    }
    if (count === 0) { minX = minY = minZ = 0; maxX = maxY = maxZ = 0; }

    // Keep the cell count sane for very large or very sparse structures.
    const span = [Math.max(maxX - minX, 1), Math.max(maxY - minY, 1), Math.max(maxZ - minZ, 1)];
    let size = cellSize;
    for (let guard = 0; guard < 32; guard++) {
      const nx = Math.ceil(span[0] / size) + 1;
      const ny = Math.ceil(span[1] / size) + 1;
      const nz = Math.ceil(span[2] / size) + 1;
      if (nx * ny * nz <= MAX_CELLS) break;
      size *= 1.5;
    }

    this.size = size;
    this.min = [minX, minY, minZ];
    this.nx = Math.ceil(span[0] / size) + 1;
    this.ny = Math.ceil(span[1] / size) + 1;
    this.nz = Math.ceil(span[2] / size) + 1;

    const cellCount = this.nx * this.ny * this.nz;
    const counts = new Int32Array(cellCount + 1);
    const cellOf = new Int32Array(count);
    for (let k = 0; k < count; k++) {
      const i = indices ? indices[k] : k;
      const cx = Math.min(this.nx - 1, Math.max(0, ((xs[i] - minX) / size) | 0));
      const cy = Math.min(this.ny - 1, Math.max(0, ((ys[i] - minY) / size) | 0));
      const cz = Math.min(this.nz - 1, Math.max(0, ((zs[i] - minZ) / size) | 0));
      const cell = (cz * this.ny + cy) * this.nx + cx;
      cellOf[k] = cell;
      counts[cell + 1]++;
    }
    for (let c = 0; c < cellCount; c++) counts[c + 1] += counts[c];
    this.start = counts;
    this.items = new Int32Array(count);
    const cursor = counts.slice(0, cellCount);
    for (let k = 0; k < count; k++) {
      const cell = cellOf[k];
      this.items[cursor[cell]++] = indices ? indices[k] : k;
    }
  }

  /** Invoke `cb(atomIndex)` for every atom in cells overlapping the query sphere. */
  near(px, py, pz, radius, cb) {
    const size = this.size;
    const [minX, minY, minZ] = this.min;
    const r = Math.max(0, radius);
    const x0 = Math.max(0, Math.floor((px - r - minX) / size));
    const x1 = Math.min(this.nx - 1, Math.floor((px + r - minX) / size));
    const y0 = Math.max(0, Math.floor((py - r - minY) / size));
    const y1 = Math.min(this.ny - 1, Math.floor((py + r - minY) / size));
    const z0 = Math.max(0, Math.floor((pz - r - minZ) / size));
    const z1 = Math.min(this.nz - 1, Math.floor((pz + r - minZ) / size));
    if (x1 < x0 || y1 < y0 || z1 < z0) return;
    for (let cz = z0; cz <= z1; cz++) {
      for (let cy = y0; cy <= y1; cy++) {
        const rowBase = (cz * this.ny + cy) * this.nx;
        for (let cx = x0; cx <= x1; cx++) {
          const cell = rowBase + cx;
          const end = this.start[cell + 1];
          for (let p = this.start[cell]; p < end; p++) cb(this.items[p]);
        }
      }
    }
  }

  /** Closest indexed atom to a point within `radius`, or -1. */
  closest(px, py, pz, radius) {
    let best = -1;
    let bestD2 = radius * radius;
    this.near(px, py, pz, radius, (i) => {
      const dx = this.xs[i] - px, dy = this.ys[i] - py, dz = this.zs[i] - pz;
      const d2 = dx * dx + dy * dy + dz * dz;
      if (d2 < bestD2) { bestD2 = d2; best = i; }
    });
    return best;
  }
}
