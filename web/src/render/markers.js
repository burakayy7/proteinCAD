// Overlay geometry: the hover highlight and distance/angle measurements.
//
// Labels are plain HTML positioned from the projected world point after each
// render, which keeps text crisp and avoids pulling in a text-sprite renderer.

import * as THREE from 'three';
import { el, clear, fixed } from '../ui/dom.js';

export class Markers {
  constructor(viewer, labelLayer) {
    this.viewer = viewer;
    this.labelLayer = labelLayer;

    this.hover = new THREE.Mesh(
      new THREE.IcosahedronGeometry(1, 2),
      new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.28, depthTest: false })
    );
    this.hover.visible = false;
    this.hover.renderOrder = 10;
    viewer.helpers.add(this.hover);

    this.lineMaterial = new THREE.LineBasicMaterial({ color: 0xffd54a, transparent: true, opacity: 0.9, depthTest: false });
    this.lines = new THREE.Group();
    viewer.helpers.add(this.lines);

    // Target residues picked for a design run.
    this.hotspotGeometry = new THREE.IcosahedronGeometry(1.6, 2);
    this.hotspotMaterial = new THREE.MeshStandardMaterial({
      color: 0xff8f3a, emissive: 0x572200, roughness: 0.35,
    });
    this.hotspots = null;

    this.measurements = [];
    this.pending = [];
    this.onChange = null;

    viewer.afterRender.push(() => this.updateLabels());
  }

  setHover(position, radius = 1.1) {
    if (!position) {
      if (this.hover.visible) { this.hover.visible = false; this.viewer.requestRender(); }
      return;
    }
    this.hover.position.copy(position);
    this.hover.scale.setScalar(radius);
    this.hover.visible = true;
    this.viewer.requestRender();
  }

  /** Show the picked design hotspots as markers on the target surface. */
  setHotspots(points) {
    if (this.hotspots) {
      this.viewer.helpers.remove(this.hotspots);
      this.hotspots = null;
    }
    if (points && points.length) {
      const mesh = new THREE.InstancedMesh(this.hotspotGeometry, this.hotspotMaterial, points.length);
      const matrix = new THREE.Matrix4();
      points.forEach((point, i) => {
        matrix.makeTranslation(point.x, point.y, point.z);
        mesh.setMatrixAt(i, matrix);
      });
      mesh.instanceMatrix.needsUpdate = true;
      mesh.frustumCulled = false;
      this.viewer.helpers.add(mesh);
      this.hotspots = mesh;
    }
    this.viewer.requestRender();
  }

  /** Add an atom to the pending measurement; two atoms make a distance. */
  addPoint(view, atomIndex) {
    this.pending.push({ view, atomIndex });
    if (this.pending.length === 2) {
      const [a, b] = this.pending;
      this.pending = [];
      if (a.view === b.view && a.atomIndex === b.atomIndex) return;
      this.measurements.push({ a, b, line: this.#makeLine(), label: el('div.label') });
      this.labelLayer.appendChild(this.measurements[this.measurements.length - 1].label);
      this.refresh();
      if (this.onChange) this.onChange();
    }
  }

  cancelPending() { this.pending = []; }

  remove(index) {
    const m = this.measurements[index];
    if (!m) return;
    m.line.geometry.dispose();
    this.lines.remove(m.line);
    m.label.remove();
    this.measurements.splice(index, 1);
    this.viewer.requestRender();
    if (this.onChange) this.onChange();
  }

  clear() {
    while (this.measurements.length) this.remove(this.measurements.length - 1);
    this.pending = [];
  }

  /** Drop measurements that refer to a structure being removed. */
  removeForView(view) {
    for (let i = this.measurements.length - 1; i >= 0; i--) {
      const m = this.measurements[i];
      if (m.a.view === view || m.b.view === view) this.remove(i);
    }
    this.pending = this.pending.filter((p) => p.view !== view);
  }

  #makeLine() {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(6), 3));
    const line = new THREE.Line(geometry, this.lineMaterial);
    line.renderOrder = 11;
    line.frustumCulled = false;
    this.lines.add(line);
    return line;
  }

  /** Recompute endpoints; call after anything moves. */
  refresh() {
    const a = new THREE.Vector3(), b = new THREE.Vector3();
    for (const m of this.measurements) {
      m.a.view.atomWorldPosition(m.a.atomIndex, a);
      m.b.view.atomWorldPosition(m.b.atomIndex, b);
      const array = m.line.geometry.getAttribute('position').array;
      array[0] = a.x; array[1] = a.y; array[2] = a.z;
      array[3] = b.x; array[4] = b.y; array[5] = b.z;
      m.line.geometry.getAttribute('position').needsUpdate = true;
      m.line.geometry.computeBoundingSphere();
      m.distance = a.distanceTo(b);
      m.midpoint = a.clone().lerp(b, 0.5);
      m.label.textContent = `${fixed(m.distance, 2)} Å`;
    }
    this.viewer.requestRender();
  }

  updateLabels() {
    if (!this.measurements.length) return;
    const rect = this.viewer.renderer.domElement.getBoundingClientRect();
    const point = new THREE.Vector3();
    for (const m of this.measurements) {
      if (!m.midpoint) continue;
      point.copy(m.midpoint).project(this.viewer.camera);
      const visible = point.z > -1 && point.z < 1;
      m.label.style.display = visible ? '' : 'none';
      if (!visible) continue;
      m.label.style.left = `${((point.x + 1) / 2) * rect.width}px`;
      m.label.style.top = `${((1 - point.y) / 2) * rect.height}px`;
    }
  }

  describe() {
    return this.measurements.map((m, index) => ({
      index,
      distance: m.distance,
      from: m.a.view.structure.atomLabel(m.a.atomIndex),
      to: m.b.view.structure.atomLabel(m.b.atomIndex),
    }));
  }

  dispose() {
    this.clear();
    clear(this.labelLayer);
    this.hover.geometry.dispose();
    this.hover.material.dispose();
    this.lineMaterial.dispose();
  }
}
