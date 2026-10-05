// CCP4 / MRC density maps: the format every cryo-EM map is deposited in.
//
// Read as a stream rather than a buffer. EMD-25576 is 400^3 float32 -- 244 MB
// once decompressed, from a 5 MB download -- and holding that whole thing in
// memory to then throw 98% of it away is how a viewer runs a laptop out of
// memory on a file that arrived in two seconds. So bytes are fed in as they
// decompress and reduced on the way past: the only array that ever exists is
// the one that gets rendered.
//
// Reducing by block average rather than by taking every k-th voxel is what
// makes that possible with no buffering at all. Each source voxel is added into
// its target cell as it goes by and the cells are divided through at the end,
// so a running sum over the output grid is the entire working set. Subsampling
// would need no sum, but a cryo-EM map is noisy and dropping voxels aliases
// that noise straight into the surface.
//
// Three things in this format exist to catch you out:
//
//   * the data is not necessarily in x, y, z order. MAPC/MAPR/MAPS say which
//     crystal axis the fastest, middle and slowest stored axis actually is, and
//     EMD-25575 -- the D8-C4 rotor -- is stored (3, 2, 1). Ignoring that gives a
//     map transposed about its diagonal, which on a symmetric particle looks
//     almost right.
//   * byte order is whatever wrote the file, declared in a stamp two thirds of
//     the way down the header.
//   * there are two different places the origin can be, and which one is
//     authoritative depends on which program wrote it.

/** Dimension of the reduced grid this aims for: ~8M voxels contours in ~1s. */
const DEFAULT_BUDGET = 8_000_000;
const HEADER_BYTES = 1024;

const MODE_BYTES = { 0: 1, 1: 2, 2: 4, 6: 2 };
const MODE_NAMES = {
  0: 'int8', 1: 'int16', 2: 'float32', 6: 'uint16',
  3: 'complex int16', 4: 'complex float32', 12: 'float16',
};

export class Ccp4Error extends Error {}

/**
 * Feed bytes in, get a reduced density grid out.
 *
 * ```js
 * const reader = new Ccp4Reader();
 * for await (const chunk of stream) reader.push(chunk);
 * const map = reader.finish();
 * ```
 */
export class Ccp4Reader {
  constructor(options = {}) {
    this.budget = options.budget ?? DEFAULT_BUDGET;
    this.header = new Uint8Array(HEADER_BYTES);
    this.headerFilled = 0;
    this.meta = null;
    this.skipBytes = 0;      // symmetry records, between header and data
    this.pending = null;     // bytes of a row that a chunk ended in the middle of
    this.pendingLength = 0;
    this.row = 0;            // how many whole source rows have been consumed
    this.done = false;
  }

  push(bytes) {
    if (this.done) return;
    let offset = 0;

    if (this.headerFilled < HEADER_BYTES) {
      const want = Math.min(HEADER_BYTES - this.headerFilled, bytes.length);
      this.header.set(bytes.subarray(0, want), this.headerFilled);
      this.headerFilled += want;
      offset = want;
      if (this.headerFilled < HEADER_BYTES) return;
      this.#readHeader();
    }

    if (this.skipBytes > 0) {
      const skip = Math.min(this.skipBytes, bytes.length - offset);
      this.skipBytes -= skip;
      offset += skip;
      if (this.skipBytes > 0) return;
    }

    this.#consume(bytes, offset);
  }

  /** Reduce every whole row available, keeping the tail for the next chunk. */
  #consume(bytes, offset) {
    const rowBytes = this.meta.rowBytes;
    let available = bytes.length - offset;
    if (available <= 0) return;

    // A row split across two chunks: finish it from the carry-over first.
    if (this.pendingLength > 0) {
      const need = rowBytes - this.pendingLength;
      const take = Math.min(need, available);
      this.pending.set(bytes.subarray(offset, offset + take), this.pendingLength);
      this.pendingLength += take;
      offset += take;
      available -= take;
      if (this.pendingLength < rowBytes) return;
      this.#row(this.pending, 0);
      this.pendingLength = 0;
    }

    const whole = Math.floor(available / rowBytes);
    for (let i = 0; i < whole; i++) {
      this.#row(bytes, offset);
      offset += rowBytes;
    }

    const tail = available - whole * rowBytes;
    if (tail > 0) {
      if (!this.pending) this.pending = new Uint8Array(rowBytes);
      this.pending.set(bytes.subarray(offset, offset + tail), 0);
      this.pendingLength = tail;
    }
  }

  /**
   * One stored row: `columns` voxels at a fixed row and section index.
   *
   * The row's position in the file gives its (row, section); which *crystal*
   * axis each of those is comes from MAPC/MAPR/MAPS, and the three strides
   * precomputed in the header reader turn all of it into one offset.
   */
  #row(bytes, offset) {
    const meta = this.meta;
    if (this.row >= meta.rows * meta.sections) return;

    const section = Math.floor(this.row / meta.rows);
    const rowIndex = this.row - section * meta.rows;
    this.row++;

    const stride = meta.stride;
    const base = Math.floor(rowIndex / stride) * meta.rowStride
      + Math.floor(section / stride) * meta.sectionStride;

    const sums = this.sums;
    const counts = this.counts;
    const view = new DataView(bytes.buffer, bytes.byteOffset + offset, meta.rowBytes);
    const little = meta.littleEndian;
    const columns = meta.columns;
    const columnStride = meta.columnStride;

    for (let c = 0; c < columns; c++) {
      let value;
      switch (meta.mode) {
        case 0: value = view.getInt8(c); break;
        case 1: value = view.getInt16(c * 2, little); break;
        case 6: value = view.getUint16(c * 2, little); break;
        default: value = view.getFloat32(c * 4, little);
      }
      const at = base + Math.floor(c / stride) * columnStride;
      sums[at] += value;
      counts[at]++;
    }
  }

  #readHeader() {
    const view = new DataView(this.header.buffer, 0, HEADER_BYTES);

    // "MAP " at word 53 says this is the format at all; the stamp after it says
    // which end the bytes came out of. Some writers leave the stamp blank, so
    // fall back to whichever byte order makes the dimensions plausible.
    const magic = String.fromCharCode(
      view.getUint8(208), view.getUint8(209), view.getUint8(210), view.getUint8(211));
    const stamp = view.getUint8(212);
    let little = stamp === 0x44 || stamp === 0x11 ? stamp === 0x44 : true;
    if (magic !== 'MAP ') {
      // Older files and some converters omit it. Accept the file if the sizes
      // read sensibly one way round, and say so plainly if neither works.
      const plausible = (le) => {
        const n = [view.getInt32(0, le), view.getInt32(4, le), view.getInt32(8, le)];
        return n.every((v) => v > 0 && v < 20000);
      };
      if (plausible(true)) little = true;
      else if (plausible(false)) little = false;
      else throw new Ccp4Error('this is not a CCP4 or MRC map (no "MAP " marker, '
        + 'and the dimensions do not read either way round)');
    } else if (stamp !== 0x44 && stamp !== 0x11) {
      little = true;
    }

    const columns = view.getInt32(0, little);
    const rows = view.getInt32(4, little);
    const sections = view.getInt32(8, little);
    const mode = view.getInt32(12, little);
    if (!(columns > 0 && rows > 0 && sections > 0)) {
      throw new Ccp4Error(`the map says it is ${columns}x${rows}x${sections}, which it is not`);
    }
    if (!(mode in MODE_BYTES)) {
      throw new Ccp4Error(
        `mode ${mode} (${MODE_NAMES[mode] || 'unknown'}) is not supported; `
        + 'proteinCAD reads int8, int16, uint16 and float32 maps');
    }

    const starts = [view.getInt32(16, little), view.getInt32(20, little), view.getInt32(24, little)];
    const intervals = [view.getInt32(28, little), view.getInt32(32, little), view.getInt32(36, little)];
    const cell = [view.getFloat32(40, little), view.getFloat32(44, little), view.getFloat32(48, little)];
    const mapc = view.getInt32(64, little);
    const mapr = view.getInt32(68, little);
    const maps = view.getInt32(72, little);
    const dmin = view.getFloat32(76, little);
    const dmax = view.getFloat32(80, little);
    const dmean = view.getFloat32(84, little);
    const nsymbt = view.getInt32(92, little);
    const originField = [
      view.getFloat32(196, little), view.getFloat32(200, little), view.getFloat32(204, little)];
    const rms = view.getFloat32(216, little);

    // Which crystal axis is the fast, medium and slow stored axis. Anything
    // outside 1..3 means the file did not say, and x, y, z is the only guess.
    const order = [mapc, mapr, maps].every((a) => a >= 1 && a <= 3)
      && new Set([mapc, mapr, maps]).size === 3 ? [mapc - 1, mapr - 1, maps - 1] : [0, 1, 2];

    // Counts and starts are given per *stored* axis; everything below wants
    // them per crystal axis.
    const stored = [columns, rows, sections];
    const size = [0, 0, 0];
    const start = [0, 0, 0];
    for (let i = 0; i < 3; i++) {
      size[order[i]] = stored[i];
      start[order[i]] = starts[i];
    }

    const voxel = [
      cell[0] / (intervals[0] || size[0] || 1),
      cell[1] / (intervals[1] || size[1] || 1),
      cell[2] / (intervals[2] || size[2] || 1),
    ];

    // Two places the origin can live. The ORIGIN words are what a cryo-EM map
    // normally carries; a crystallographic map leaves them zero and puts the
    // offset in NxSTART instead. Prefer whichever is not zero, because a map
    // placed at the wrong corner lines up with nothing.
    const fromField = originField.some((v) => v !== 0 && Number.isFinite(v));
    const origin = fromField
      ? originField
      : [start[0] * voxel[0], start[1] * voxel[1], start[2] * voxel[2]];

    // One integer stride, so a voxel stays a cube and the surface does not
    // stretch along whichever axis happened to be longest.
    const total = size[0] * size[1] * size[2];
    let stride = 1;
    while (Math.ceil(size[0] / stride) * Math.ceil(size[1] / stride)
           * Math.ceil(size[2] / stride) > this.budget) {
      stride++;
    }

    const nx = Math.ceil(size[0] / stride);
    const ny = Math.ceil(size[1] / stride);
    const nz = Math.ceil(size[2] / stride);

    // Stride through the *output* grid for a step along each stored axis.
    const outStride = [1, nx, nx * ny];

    this.meta = {
      columns, rows, sections, mode, littleEndian: little,
      rowBytes: columns * MODE_BYTES[mode],
      order, stride,
      columnStride: outStride[order[0]],
      rowStride: outStride[order[1]],
      sectionStride: outStride[order[2]],
      nx, ny, nz,
      voxel: [voxel[0] * stride, voxel[1] * stride, voxel[2] * stride],
      origin,
      sourceSize: size,
      sourceVoxel: voxel,
      reported: { min: dmin, max: dmax, mean: dmean, rms },
      total,
    };

    this.skipBytes = Math.max(0, nsymbt);
    this.sums = new Float32Array(nx * ny * nz);
    this.counts = new Uint16Array(nx * ny * nz);
  }

  /** Divide the running sums through and hand back the finished grid. */
  finish() {
    if (!this.meta) {
      throw new Ccp4Error('the map ended before its header was complete '
        + `(${this.headerFilled} of ${HEADER_BYTES} bytes)`);
    }
    this.done = true;
    const { nx, ny, nz } = this.meta;
    const field = this.sums;
    const counts = this.counts;

    let min = Infinity;
    let max = -Infinity;
    let sum = 0;
    let sumSquares = 0;
    let filled = 0;
    for (let i = 0; i < field.length; i++) {
      const n = counts[i];
      if (!n) { field[i] = 0; continue; }
      const value = field[i] / n;
      field[i] = value;
      if (value < min) min = value;
      if (value > max) max = value;
      sum += value;
      sumSquares += value * value;
      filled++;
    }
    if (!filled) throw new Ccp4Error('the map contained no density');

    const mean = sum / filled;
    const rms = Math.sqrt(Math.max(0, sumSquares / filled - mean * mean));
    const expected = this.meta.rows * this.meta.sections;

    return {
      field,
      nx, ny, nz,
      voxel: this.meta.voxel,
      origin: this.meta.origin,
      min, max, mean, rms,
      // What the file claimed, kept separate: block averaging narrows the
      // range, so a contour level quoted against the original has to be judged
      // against the original's numbers rather than these.
      reported: this.meta.reported,
      stride: this.meta.stride,
      sourceSize: this.meta.sourceSize,
      sourceVoxel: this.meta.sourceVoxel,
      // A download cut short still produces a grid; it is just a grid with a
      // hole in it, and silently contouring that is worse than saying so.
      complete: this.row >= expected,
      rowsRead: this.row,
      rowsExpected: expected,
    };
  }
}

/** Read a whole buffer in one go. */
export function parseCCP4(buffer, options = {}) {
  const reader = new Ccp4Reader(options);
  reader.push(buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer));
  return reader.finish();
}

/**
 * Read from a fetch Response, decompressing if it arrived gzipped.
 *
 * `onProgress` is called with the bytes decompressed so far, because a 244 MB
 * map takes long enough that a viewer with no progress looks hung.
 */
export async function readCCP4Stream(response, options = {}) {
  const reader = new Ccp4Reader(options);
  let stream = response.body;
  if (!stream) {
    const buffer = await response.arrayBuffer();
    reader.push(new Uint8Array(buffer));
    return reader.finish();
  }

  const name = (options.filename || '').toLowerCase();
  const encoded = (response.headers.get('Content-Type') || '').includes('gzip')
    || name.endsWith('.gz');
  // The browser already unwraps Content-Encoding: gzip. This is for a body that
  // *is* a .gz file, which is how EMDB serves maps.
  if (encoded) {
    if (typeof DecompressionStream === 'undefined') {
      throw new Ccp4Error('this browser cannot decompress gzip streams, so a .map.gz '
        + 'cannot be read here. Gunzip it and drop the .map file in instead.');
    }
    stream = stream.pipeThrough(new DecompressionStream('gzip'));
  }

  const chunks = stream.getReader();
  let seen = 0;
  for (;;) {
    const { done, value } = await chunks.read();
    if (done) break;
    reader.push(value);
    seen += value.length;
    if (options.onProgress) options.onProgress(seen);
  }
  return reader.finish();
}
