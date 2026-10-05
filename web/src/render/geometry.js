// Shared geometry plumbing for every representation.
//
// Two conventions hold everywhere:
//   * each vertex (or instance) records the atom it came from, in
//     `geometry.userData.atomIndex`. Colouring and selection highlighting are
//     then a single pass over that array -- no geometry rebuild.
//   * each vertex (or instance) carries a `pickColor` attribute encoding
//     (structure slot, atom index) so one offscreen pass identifies whatever is
//     under the cursor, regardless of representation.

import * as THREE from 'three';
import { SELECTION_COLOR, hexToLinear } from './colors.js';

export class MeshBuilder {
  constructor(estimatedVertices = 4096) {
    const v = Math.max(64, estimatedVertices);
    this.positions = new Float32Array(v * 3);
    this.normals = new Float32Array(v * 3);
    this.atoms = new Uint32Array(v);
    this.indices = new Uint32Array(v * 3);
    this.vertexCount = 0;
    this.indexCount = 0;
  }

  _growVertices(needed) {
    if (needed <= this.atoms.length) return;
    let size = this.atoms.length * 2;
    while (size < needed) size *= 2;
    const p = new Float32Array(size * 3); p.set(this.positions); this.positions = p;
    const n = new Float32Array(size * 3); n.set(this.normals); this.normals = n;
    const a = new Uint32Array(size); a.set(this.atoms); this.atoms = a;
  }

  _growIndices(needed) {
    if (needed <= this.indices.length) return;
    let size = this.indices.length * 2;
    while (size < needed) size *= 2;
    const i = new Uint32Array(size); i.set(this.indices); this.indices = i;
  }

  vertex(px, py, pz, nx, ny, nz, atomIndex) {
    this._growVertices(this.vertexCount + 1);
    const i = this.vertexCount++;
    this.positions[i * 3] = px; this.positions[i * 3 + 1] = py; this.positions[i * 3 + 2] = pz;
    this.normals[i * 3] = nx; this.normals[i * 3 + 1] = ny; this.normals[i * 3 + 2] = nz;
    this.atoms[i] = atomIndex;
    return i;
  }

  triangle(a, b, c) {
    this._growIndices(this.indexCount + 3);
    this.indices[this.indexCount++] = a;
    this.indices[this.indexCount++] = b;
    this.indices[this.indexCount++] = c;
  }

  quad(a, b, c, d) {
    this.triangle(a, b, c);
    this.triangle(a, c, d);
  }

  get isEmpty() { return this.indexCount === 0; }

  build() {
    const geometry = new THREE.BufferGeometry();
    const count = this.vertexCount;
    // slice() rather than subarray() so the over-allocated buffers can be freed.
    geometry.setAttribute('position', new THREE.BufferAttribute(this.positions.slice(0, count * 3), 3));
    geometry.setAttribute('normal', new THREE.BufferAttribute(this.normals.slice(0, count * 3), 3));
    geometry.setAttribute('color', new THREE.BufferAttribute(new Float32Array(count * 3).fill(0.8), 3));
    geometry.setIndex(new THREE.BufferAttribute(this.indices.slice(0, this.indexCount), 1));
    geometry.userData.atomIndex = this.atoms.slice(0, count);
    this.positions = this.normals = this.indices = this.atoms = null;
    return geometry;
  }
}

const selectionRGB = hexToLinear(SELECTION_COLOR);

/**
 * Write per-vertex colours from the structure's atom colour table, tinting
 * whatever the selection mask covers. Cheap enough to call on every selection
 * change, even for a million vertices.
 */
export function applyVertexColors(geometry, atomColors, selection) {
  const attr = geometry.getAttribute('color');
  const atoms = geometry.userData.atomIndex;
  if (!attr || !atoms) return;
  const out = attr.array;
  const blend = 0.85; // in linear space, so it needs to be high to read as gold
  for (let v = 0; v < atoms.length; v++) {
    const a = atoms[v];
    const r = atomColors[a * 3], g = atomColors[a * 3 + 1], b = atomColors[a * 3 + 2];
    if (selection && selection[a]) {
      out[v * 3] = r + (selectionRGB[0] - r) * blend;
      out[v * 3 + 1] = g + (selectionRGB[1] - g) * blend;
      out[v * 3 + 2] = b + (selectionRGB[2] - b) * blend;
    } else {
      out[v * 3] = r; out[v * 3 + 1] = g; out[v * 3 + 2] = b;
    }
  }
  attr.needsUpdate = true;
}

/** Same, for the per-instance colours of InstancedMesh representations. */
export function applyInstanceColors(mesh, atomColors, selection) {
  const atoms = mesh.userData.atomIndex;
  if (!mesh.instanceColor || !atoms) return;
  const out = mesh.instanceColor.array;
  const blend = 0.85; // in linear space, so it needs to be high to read as gold
  for (let v = 0; v < atoms.length; v++) {
    const a = atoms[v];
    const r = atomColors[a * 3], g = atomColors[a * 3 + 1], b = atomColors[a * 3 + 2];
    if (selection && selection[a]) {
      out[v * 3] = r + (selectionRGB[0] - r) * blend;
      out[v * 3 + 1] = g + (selectionRGB[1] - g) * blend;
      out[v * 3 + 2] = b + (selectionRGB[2] - b) * blend;
    } else {
      out[v * 3] = r; out[v * 3 + 1] = g; out[v * 3 + 2] = b;
    }
  }
  mesh.instanceColor.needsUpdate = true;
}

function encodePick(target, offset, atomIndex, slot) {
  target[offset] = atomIndex & 255;
  target[offset + 1] = (atomIndex >> 8) & 255;
  target[offset + 2] = (atomIndex >> 16) & 255;
  target[offset + 3] = slot;
}

/** RGBA = (atom index, structure slot). Slot 0 is reserved for "nothing". */
export function addPickAttribute(geometry, slot, instanced = false) {
  const atoms = geometry.userData.atomIndex;
  if (!atoms) return;
  const data = new Uint8Array(atoms.length * 4);
  for (let i = 0; i < atoms.length; i++) encodePick(data, i * 4, atoms[i], slot);
  const attr = instanced
    ? new THREE.InstancedBufferAttribute(data, 4, true)
    : new THREE.BufferAttribute(data, 4, true);
  geometry.setAttribute('pickColor', attr);
}

export function decodePick(rgba) {
  const slot = rgba[3];
  if (!slot) return null;
  return { slot, atomIndex: rgba[0] | (rgba[1] << 8) | (rgba[2] << 16) };
}

const sphereCache = new Map();
export function unitSphere(detail) {
  if (!sphereCache.has(detail)) sphereCache.set(detail, new THREE.IcosahedronGeometry(1, detail));
  return sphereCache.get(detail);
}

const cylinderCache = new Map();
export function unitCylinder(segments) {
  if (!cylinderCache.has(segments)) {
    // Unit height along +Y, centred, open ended: two of these make a bond.
    const g = new THREE.CylinderGeometry(1, 1, 1, segments, 1, true);
    cylinderCache.set(segments, g);
  }
  return cylinderCache.get(segments);
}

/**
 * A per-mesh geometry that shares the template's vertex buffers. Instanced
 * representations need their own `pickColor` attribute but should not each
 * upload their own copy of the unit sphere.
 */
export function instanceGeometry(template) {
  const geometry = new THREE.BufferGeometry();
  geometry.setIndex(template.getIndex());
  for (const name of ['position', 'normal']) {
    const attr = template.getAttribute(name);
    if (attr) geometry.setAttribute(name, attr);
  }
  return geometry;
}

/**
 * `vertexColors` must be false for instanced representations: three defines
 * USE_COLOR from it in the vertex shader, and with no `color` attribute on the
 * geometry that shader reads zeroes and everything renders black. Instance
 * colours reach the fragment shader through `instanceColor` regardless.
 */
export function createMaterial(options = {}) {
  return new THREE.MeshStandardMaterial({
    vertexColors: options.vertexColors ?? true,
    roughness: options.roughness ?? 0.55,
    metalness: options.metalness ?? 0.0,
    flatShading: options.flatShading ?? false,
    side: options.side ?? THREE.DoubleSide,
    transparent: options.transparent ?? false,
    opacity: options.opacity ?? 1,
    depthWrite: options.depthWrite ?? true,
  });
}

export function disposeObject(object) {
  object.traverse((node) => {
    if (node.geometry) node.geometry.dispose();
    if (node.material) {
      const materials = Array.isArray(node.material) ? node.material : [node.material];
      for (const m of materials) m.dispose();
    }
  });
}
