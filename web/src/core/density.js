// A density map in the document: what a CCP4 file becomes once it is read.
//
// Sits beside Structure rather than inside it. A map is not a molecule -- it has
// no atoms, no chains and no sequence, and nothing that selects or measures
// applies to it -- but it occupies the same space, moves the same way, and
// belongs in the same tree. Keeping them separate is what stops every loop over
// `structure.atoms` having to ask whether this one has any.
//
// No three.js here, same rule as the rest of core/: this is the part a Python
// backend would mirror, and it runs in plain node so the parser can be tested
// without a browser.

/** Contour level as a multiple of the map's own noise, which is how a map is
 * normally talked about: "three sigma" means the same thing across maps whose
 * absolute scales differ by three orders of magnitude. */
export const DEFAULT_SIGMA = 3;

export class DensityMap {
  /**
   * @param {object} grid  what io/ccp4.js returns
   * @param {string} name
   */
  constructor(grid, name = 'map') {
    this.name = name;
    this.title = '';
    this.field = grid.field;
    this.nx = grid.nx;
    this.ny = grid.ny;
    this.nz = grid.nz;
    this.voxel = grid.voxel;
    this.origin = grid.origin;
    this.min = grid.min;
    this.max = grid.max;
    this.mean = grid.mean;
    this.rms = grid.rms;
    this.reported = grid.reported || null;
    this.stride = grid.stride || 1;
    this.sourceSize = grid.sourceSize || [grid.nx, grid.ny, grid.nz];
    this.sourceVoxel = grid.sourceVoxel || grid.voxel;
    this.complete = grid.complete !== false;
    // What EMDB says this map should be looked at at, when it came from there.
    this.recommended = null;
    this.source = '';
  }

  get voxelCount() {
    return this.nx * this.ny * this.nz;
  }

  /** World position of grid point (i, j, k). */
  position(i, j, k) {
    return [
      this.origin[0] + i * this.voxel[0],
      this.origin[1] + j * this.voxel[1],
      this.origin[2] + k * this.voxel[2],
    ];
  }

  /** The corners of the box this map covers, in world space. */
  bounds() {
    return {
      min: this.position(0, 0, 0),
      max: this.position(this.nx - 1, this.ny - 1, this.nz - 1),
    };
  }

  centre() {
    const { min, max } = this.bounds();
    return [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
  }

  /** Level in map units for a multiple of the noise. */
  levelForSigma(sigma) {
    return this.mean + sigma * this.rms;
  }

  /** And back, so a slider in map units can be labelled in sigma. */
  sigmaForLevel(level) {
    return this.rms > 0 ? (level - this.mean) / this.rms : 0;
  }

  /**
   * Where to put the contour when nothing has said.
   *
   * EMDB's own recommended level when the entry carried one, because that is
   * the level the depositors looked at it at. Otherwise a few sigma, which is
   * the convention and is at least the right order of magnitude for any map.
   *
   * A level quoted against the deposited map has to be judged against the
   * deposited map's numbers: block averaging pulls the extremes in, so a level
   * expressed in sigma would move when the map is downsampled and the same
   * entry would contour differently on two machines.
   */
  defaultLevel() {
    if (this.recommended !== null && Number.isFinite(this.recommended)) {
      return this.recommended;
    }
    return this.levelForSigma(DEFAULT_SIGMA);
  }

  /** A sensible span for a slider: never wider than the data. */
  levelRange() {
    const low = Math.max(this.min, this.mean);
    const high = this.max;
    return high > low ? [low, high] : [this.min, this.max];
  }

  /** Fraction of the box enclosed at a level, for the readout. */
  occupancy(level) {
    let above = 0;
    for (let i = 0; i < this.field.length; i++) if (this.field[i] >= level) above++;
    return above / this.field.length;
  }

  summary() {
    return {
      name: this.name,
      title: this.title,
      grid: [this.nx, this.ny, this.nz],
      voxel: this.voxel,
      origin: this.origin,
      min: this.min,
      max: this.max,
      mean: this.mean,
      rms: this.rms,
      stride: this.stride,
      sourceSize: this.sourceSize,
      complete: this.complete,
    };
  }

  /** One line for the tree: what this map is, in the terms a reader wants. */
  describe() {
    const size = `${this.sourceSize.join('×')}`;
    const spacing = `${this.sourceVoxel[0].toFixed(2)} Å/voxel`;
    const reduced = this.stride > 1
      ? `, shown at ${this.nx}×${this.ny}×${this.nz}` : '';
    return `${size}, ${spacing}${reduced}`;
  }
}
