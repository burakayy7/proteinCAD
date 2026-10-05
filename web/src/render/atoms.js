// Atom- and bond-level representations: spacefill, ball-and-stick, sticks.
//
// Both are InstancedMesh: one instance per atom (spheres) or two per bond
// (sticks, split at the midpoint so each half takes its own atom's colour).
// Instance colours come from `mesh.instanceColor`, which three.js applies even
// when the material has vertexColors off.

import * as THREE from 'three';
import { instanceGeometry, unitSphere, unitCylinder } from './geometry.js';
import { getBonds } from '../core/bonds.js';

export function sphereDetail(count) {
  if (count <= 2000) return 2;
  if (count <= 60000) return 1;
  return 0;
}

/**
 * @param {Structure} structure
 * @param {Int32Array|number[]} atomIndices
 * @param {THREE.Material} material
 * @param {{radiusScale?: number, fixedRadius?: number|null, detail?: number}} options
 */
export function buildSpheres(structure, atomIndices, material, options = {}) {
  const count = atomIndices.length;
  if (!count) return null;
  const detail = options.detail ?? sphereDetail(count);
  const geometry = instanceGeometry(unitSphere(detail));
  const mesh = new THREE.InstancedMesh(geometry, material, count);
  const matrix = new THREE.Matrix4();
  const atomIndex = new Uint32Array(count);
  const scale = options.radiusScale ?? 1;
  const fixed = options.fixedRadius ?? null;

  for (let k = 0; k < count; k++) {
    const i = atomIndices[k];
    const r = fixed === null ? structure.atomRadius(i) * scale : fixed;
    matrix.makeScale(r, r, r);
    matrix.setPosition(structure.x[i], structure.y[i], structure.z[i]);
    mesh.setMatrixAt(k, matrix);
    atomIndex[k] = i;
  }
  mesh.instanceMatrix.needsUpdate = true;
  mesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(count * 3).fill(0.8), 3);
  mesh.userData.atomIndex = atomIndex;
  geometry.userData.atomIndex = atomIndex;
  mesh.computeBoundingSphere();
  return mesh;
}

const UP = new THREE.Vector3(0, 1, 0);

/**
 * Bond cylinders. `mask` limits which atoms participate; a bond is drawn when
 * both of its atoms survive the mask.
 */
export function buildSticks(structure, mask, material, options = {}) {
  const bonds = getBonds(structure);
  const radius = options.radius ?? 0.16;
  const segments = options.segments ?? 8;

  // A bond belongs to the chain of its first atom, so an inter-chain link (a
  // disulfide, say) is drawn once and follows that chain when it is moved.
  const chainIndex = options.chainIndex;
  const keep = [];
  for (let k = 0; k < bonds.count; k++) {
    const a = bonds.a[k], b = bonds.b[k];
    if (mask && (!mask[a] || !mask[b])) continue;
    if (chainIndex !== undefined && structure.residueOfAtom(a).chainIndex !== chainIndex) continue;
    keep.push(k);
  }
  if (!keep.length) return null;

  const count = keep.length * 2;
  const geometry = instanceGeometry(unitCylinder(segments));
  const mesh = new THREE.InstancedMesh(geometry, material, count);
  const atomIndex = new Uint32Array(count);

  const matrix = new THREE.Matrix4();
  const quaternion = new THREE.Quaternion();
  const position = new THREE.Vector3();
  const scale = new THREE.Vector3();
  const dir = new THREE.Vector3();

  let instance = 0;
  for (const k of keep) {
    const a = bonds.a[k], b = bonds.b[k];
    dir.set(structure.x[b] - structure.x[a], structure.y[b] - structure.y[a], structure.z[b] - structure.z[a]);
    const length = dir.length();
    if (length < 1e-4) continue;
    dir.divideScalar(length);
    quaternion.setFromUnitVectors(UP, dir);
    scale.set(radius, length / 2, radius);

    for (let half = 0; half < 2; half++) {
      const atom = half === 0 ? a : b;
      const t = half === 0 ? 0.25 : 0.75;
      position.set(
        structure.x[a] + dir.x * length * t,
        structure.y[a] + dir.y * length * t,
        structure.z[a] + dir.z * length * t
      );
      matrix.compose(position, quaternion, scale);
      mesh.setMatrixAt(instance, matrix);
      atomIndex[instance] = atom;
      instance++;
    }
  }

  mesh.count = instance;
  mesh.instanceMatrix.needsUpdate = true;
  mesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(count * 3).fill(0.8), 3);
  mesh.userData.atomIndex = atomIndex;
  geometry.userData.atomIndex = atomIndex;
  mesh.computeBoundingSphere();
  return mesh;
}

