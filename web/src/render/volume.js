// The design volume: a box, cylinder or sphere you place and size with the
// same handle as everything else.
//
// This is how "a specific shape and size" gets expressed. Its volume converts
// to a residue count for models that take a length (RFdiffusion), and its
// geometry can be sampled to a point cloud for models that take a shape
// directly (Chroma).

import * as THREE from 'three';
import { residuesForVolume } from '../design/spec.js';

const SHAPES = {
  box: () => new THREE.BoxGeometry(1, 1, 1),
  cylinder: () => new THREE.CylinderGeometry(0.5, 0.5, 1, 32),
  sphere: () => new THREE.SphereGeometry(0.5, 24, 16),
};

export const VOLUME_SHAPES = [
  { id: 'cylinder', label: 'Cylinder' },
  { id: 'box', label: 'Box' },
  { id: 'sphere', label: 'Sphere' },
];

export class DesignVolume {
  constructor(viewer, shape = 'cylinder') {
    this.viewer = viewer;
    this.group = new THREE.Group();
    this.group.name = 'design-volume';
    this.group.userData.designVolume = this;
    this.shape = shape;

    this.material = new THREE.MeshStandardMaterial({
      color: 0x4f9dff,
      transparent: true,
      opacity: 0.16,
      roughness: 0.4,
      side: THREE.DoubleSide,
      depthWrite: false,
    });
    this.edgeMaterial = new THREE.LineBasicMaterial({ color: 0x7fbaff, transparent: true, opacity: 0.85 });

    this.mesh = new THREE.Mesh(SHAPES[shape](), this.material);
    this.edges = new THREE.LineSegments(new THREE.EdgesGeometry(this.mesh.geometry, 25), this.edgeMaterial);
    this.group.add(this.mesh, this.edges);
    // Helpers are hidden during picking, so the volume never blocks a click.
    viewer.helpers.add(this.group);

    this.setSize(20, 35, 20);
  }

  setShape(shape) {
    if (!SHAPES[shape] || shape === this.shape) return;
    this.shape = shape;
    this.mesh.geometry.dispose();
    this.edges.geometry.dispose();
    this.mesh.geometry = SHAPES[shape]();
    this.edges.geometry = new THREE.EdgesGeometry(this.mesh.geometry, 25);
    this.viewer.requestRender();
  }

  /** Size in Angstrom along the object's own x, y, z. */
  setSize(x, y, z) {
    this.group.scale.set(Math.max(1, x), Math.max(1, y), Math.max(1, z));
    this.viewer.requestRender();
  }

  get size() {
    return [this.group.scale.x, this.group.scale.y, this.group.scale.z];
  }

  /** Enclosed volume in cubic Angstrom. */
  get volume() {
    const [x, y, z] = this.size;
    if (this.shape === 'box') return x * y * z;
    if (this.shape === 'sphere') return (Math.PI * x * y * z) / 6;
    return (Math.PI * x * z * y) / 4; // cylinder, axis along y
  }

  get residueEstimate() {
    return residuesForVolume(this.volume);
  }

  /**
   * Park the volume just outside the target, centred on the picked patch and
   * pushed along the outward direction so it sits where a binder would.
   */
  placeAt(centre, outward) {
    const direction = outward.clone();
    if (direction.lengthSq() < 1e-6) direction.set(0, 0, 1);
    direction.normalize();
    const halfDepth = (this.shape === 'cylinder' ? this.group.scale.y : this.group.scale.z) / 2;
    this.group.position.copy(centre).addScaledVector(direction, halfDepth * 0.9);
    // Point the long axis (y for a cylinder) away from the target.
    this.group.quaternion.setFromUnitVectors(
      new THREE.Vector3(0, this.shape === 'cylinder' ? 1 : 0, this.shape === 'cylinder' ? 0 : 1),
      direction
    );
    this.group.updateMatrixWorld(true);
    this.viewer.requestRender();
  }

  setVisible(visible) {
    this.group.visible = visible;
    this.viewer.requestRender();
  }

  /**
   * Points filling the volume, for models that take a shape directly. Spacing
   * is in Angstrom; the default gives roughly one point per residue.
   */
  pointCloud(spacing = 5) {
    const [sx, sy, sz] = this.size;
    const points = [];
    const local = new THREE.Vector3();
    const steps = (extent) => Math.max(1, Math.round(extent / spacing));
    const nx = steps(sx), ny = steps(sy), nz = steps(sz);
    this.group.updateMatrixWorld(true);
    for (let i = 0; i < nx; i++) {
      for (let j = 0; j < ny; j++) {
        for (let k = 0; k < nz; k++) {
          const u = (i + 0.5) / nx - 0.5;
          const v = (j + 0.5) / ny - 0.5;
          const w = (k + 0.5) / nz - 0.5;
          if (!this.#inside(u, v, w)) continue;
          local.set(u, v, w).applyMatrix4(this.group.matrixWorld);
          points.push([+local.x.toFixed(2), +local.y.toFixed(2), +local.z.toFixed(2)]);
        }
      }
    }
    return points;
  }

  /** Unit-cube coordinates, so the test is the same for any size. */
  #inside(u, v, w) {
    if (this.shape === 'box') return true;
    if (this.shape === 'sphere') return u * u + v * v + w * w <= 0.25;
    return u * u + w * w <= 0.25; // cylinder about y
  }

  toJSON() {
    this.group.updateMatrixWorld(true);
    const position = this.group.position;
    const axis = new THREE.Vector3(0, 1, 0).applyQuaternion(this.group.quaternion);
    return {
      shape: this.shape,
      size: this.size.map((v) => +v.toFixed(2)),
      centre: [+position.x.toFixed(2), +position.y.toFixed(2), +position.z.toFixed(2)],
      axis: [+axis.x.toFixed(3), +axis.y.toFixed(3), +axis.z.toFixed(3)],
      volume: Math.round(this.volume),
      residues: this.residueEstimate,
    };
  }

  dispose() {
    this.mesh.geometry.dispose();
    this.edges.geometry.dispose();
    this.material.dispose();
    this.edgeMaterial.dispose();
    this.group.removeFromParent();
    this.viewer.requestRender();
  }
}
