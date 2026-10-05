// One density map in the scene.
//
// The same object graph as a structure, one level shallower: a map has no
// chains to move independently, so it is a group with a mesh in it, centred on
// its own box so the rotate handle turns it about its middle rather than about
// the file's corner.
//
//   group    at the map centre    (movable, same as a structure's)
//     pivot  cancels that offset
//       mesh the contoured surface
//
// Changing the level rebuilds the mesh and nothing else: the field stays, the
// transform stays, and the group it hangs off never moves. That is what lets
// the level be a slider rather than a reload.

import * as THREE from 'three';
import { contourField } from './isosurface.js';
import { createMaterial, disposeObject } from './geometry.js';

export const MAP_STYLES = [
  { id: 'solid', label: 'Solid' },
  { id: 'wire', label: 'Wireframe' },
  { id: 'transparent', label: 'Transparent' },
];

/** Distinct from the structure palette: a map is scenery, not a molecule. */
export const MAP_COLORS = [0x9aa7b5, 0x7fb3d5, 0xc8a97e, 0x8fbf9f, 0xb79fc7];

export class MapView {
  /**
   * @param {DensityMap} map
   * @param {number} index  position in the document, for the default colour
   */
  constructor(map, index = 0) {
    this.map = map;
    this.index = index;
    this.label = map.name;
    this.isMap = true;

    this.level = map.defaultLevel();
    this.style = 'solid';
    this.color = MAP_COLORS[index % MAP_COLORS.length];
    this.opacity = 0.55;
    this.visible = true;
    this.mesh = null;

    const centre = map.centre();
    this.origin = new THREE.Vector3(centre[0], centre[1], centre[2]);
    this.group = new THREE.Group();
    this.group.name = map.name;
    this.group.userData.view = this;
    this.group.position.copy(this.origin);

    this.pivot = new THREE.Group();
    this.pivot.position.copy(this.origin).negate();
    this.group.add(this.pivot);

    this.material = createMaterial({ vertexColors: false });
    this.material.color = new THREE.Color(this.color);
    this.#applyStyle();
    this.build();
  }

  get name() { return this.label; }

  /** Contour at the current level. Returns false when nothing is enclosed. */
  build() {
    if (this.mesh) {
      this.pivot.remove(this.mesh);
      disposeObject(this.mesh);
      this.mesh = null;
    }
    const map = this.map;
    const geometry = contourField(
      map.field,
      { nx: map.nx, ny: map.ny, nz: map.nz },
      this.level,
      {
        ox: map.origin[0], oy: map.origin[1], oz: map.origin[2],
        dx: map.voxel[0], dy: map.voxel[1], dz: map.voxel[2],
      },
      { estimate: 1 << 17 }
    );
    if (!geometry) return false;

    // No pick attribute and no atom colours: a map has no atoms, so clicking it
    // selects nothing. Without an explicit zero the pick pass would read the
    // attribute's default and report a slot that belongs to some structure.
    geometry.setAttribute('pickColor', new THREE.BufferAttribute(
      new Uint8Array(geometry.getAttribute('position').count * 4), 4, true));
    delete geometry.userData.atomIndex;

    this.mesh = new THREE.Mesh(geometry, this.material);
    this.mesh.userData.view = this;
    this.mesh.visible = this.visible;
    this.pivot.add(this.mesh);
    return true;
  }

  get triangleCount() {
    const index = this.mesh && this.mesh.geometry.getIndex();
    return index ? index.count / 3 : 0;
  }

  setLevel(level) {
    this.level = level;
    return this.build();
  }

  setStyle(style) {
    this.style = style;
    this.#applyStyle();
  }

  setColor(hex) {
    this.color = hex;
    this.material.color = new THREE.Color(hex);
    this.material.needsUpdate = true;
  }

  setOpacity(opacity) {
    this.opacity = opacity;
    this.#applyStyle();
  }

  #applyStyle() {
    const material = this.material;
    material.wireframe = this.style === 'wire';
    // Solid stays slightly transparent on purpose: the usual reason to have a
    // map and a model in the same scene is to see the model inside the map, and
    // an opaque envelope hides exactly what you opened it for.
    const opaque = this.style === 'solid';
    material.transparent = !opaque || this.opacity < 1;
    material.opacity = this.style === 'transparent' ? Math.min(this.opacity, 0.35) : this.opacity;
    material.depthWrite = material.opacity > 0.95;
    material.side = THREE.DoubleSide;
    material.needsUpdate = true;
  }

  setVisible(visible) {
    this.visible = visible;
    this.group.visible = visible;
  }

  /** World-space box of the contoured surface, or of the whole grid if empty. */
  box() {
    this.group.updateMatrixWorld(true);
    const box = new THREE.Box3();
    if (this.mesh) {
      this.mesh.geometry.computeBoundingBox();
      box.copy(this.mesh.geometry.boundingBox).applyMatrix4(this.mesh.matrixWorld);
      if (!box.isEmpty()) return box;
    }
    const { min, max } = this.map.bounds();
    return box.setFromPoints([
      new THREE.Vector3(...min).applyMatrix4(this.pivot.matrixWorld),
      new THREE.Vector3(...max).applyMatrix4(this.pivot.matrixWorld),
    ]);
  }

  hasTransform() {
    return this.group.position.distanceToSquared(this.origin) > 1e-10
      || Math.abs(this.group.quaternion.w) < 1 - 1e-8;
  }

  resetTransforms() {
    this.group.position.copy(this.origin);
    this.group.quaternion.identity();
    this.group.updateMatrixWorld(true);
  }

  dispose() {
    disposeObject(this.group);
    this.material.dispose();
  }
}
