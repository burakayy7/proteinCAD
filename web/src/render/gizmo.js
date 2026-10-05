// The move/rotate handle. Wraps three's TransformControls and keeps the orbit
// controls out of the way while a drag is in progress.

import { TransformControls } from '../../vendor/TransformControls.js';

export class Gizmo {
  constructor(viewer) {
    this.viewer = viewer;
    this.dragging = false;
    this.onMove = null;
    this.onDrop = null;

    this.controls = new TransformControls(viewer.camera, viewer.renderer.domElement);
    this.controls.setSpace('world');
    this.controls.size = 0.85;
    this.controls.enabled = false;

    this.helper = this.controls.getHelper();
    this.helper.visible = false;
    viewer.helpers.add(this.helper);

    this.controls.addEventListener('dragging-changed', (event) => {
      this.dragging = event.value;
      viewer.controls.enabled = !event.value;
      if (!event.value && this.onDrop) this.onDrop();
    });
    this.controls.addEventListener('objectChange', () => {
      viewer.requestRender();
      if (this.onMove) this.onMove();
    });
    this.controls.addEventListener('change', () => viewer.requestRender());
  }

  get object() { return this.controls.object || null; }

  attach(object3D, mode = 'translate') {
    if (!object3D) return this.detach();
    this.controls.attach(object3D);
    this.controls.setMode(mode);
    this.controls.enabled = true;
    this.helper.visible = true;
    this.viewer.requestRender();
    return this;
  }

  setMode(mode) {
    this.controls.setMode(mode);
    this.viewer.requestRender();
  }

  get mode() { return this.controls.mode; }

  detach() {
    this.controls.detach();
    this.controls.enabled = false;
    this.helper.visible = false;
    this.viewer.requestRender();
    return this;
  }

  dispose() {
    this.detach();
    this.helper.removeFromParent();
    this.controls.dispose();
  }
}
