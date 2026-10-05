// The 3D viewport: renderer, camera, lights, navigation and GPU picking.
//
// Rendering is on demand -- a frame is drawn when something changed or while
// the camera is still settling -- so an idle viewport costs nothing even with a
// few million triangles resident.

import * as THREE from 'three';
import { OrbitControls } from '../../vendor/OrbitControls.js';
import { decodePick } from './geometry.js';

const PICK_VERTEX = /* glsl */`
attribute vec4 pickColor;
varying vec4 vPick;
void main() {
  vPick = pickColor;
  #include <begin_vertex>
  #include <project_vertex>
}`;

const PICK_FRAGMENT = /* glsl */`
varying vec4 vPick;
void main() {
  gl_FragColor = vPick;
}`;

export class Viewer {
  constructor(container, options = {}) {
    this.container = container;

    this.renderer = new THREE.WebGLRenderer({
      antialias: true,
      powerPreference: 'high-performance',
      stencil: false,
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setSize(container.clientWidth || 1, container.clientHeight || 1);
    this.renderer.setClearColor(0x0e1116, 1);
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.background = options.background ?? 0x0e1116;
    this.scene.background = new THREE.Color(this.background);

    this.camera = new THREE.PerspectiveCamera(35, this.aspect, 1, 5000);
    this.camera.position.set(0, 0, 120);
    this.scene.add(this.camera);

    this.world = new THREE.Group();
    this.world.name = 'world';
    this.scene.add(this.world);

    // Gizmos, markers and labels: excluded from picking and from framing.
    this.helpers = new THREE.Group();
    this.helpers.name = 'helpers';
    this.scene.add(this.helpers);

    this.#setupLights();

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.12;
    this.controls.rotateSpeed = 0.9;
    this.controls.zoomSpeed = 1.1;
    this.controls.panSpeed = 0.9;
    this.controls.screenSpacePanning = true;
    // Zoom towards the cursor: on a large assembly, zooming towards the orbit
    // centre means you cannot approach anything that is not already centred.
    this.controls.zoomToCursor = true;
    this.controls.minDistance = 1;
    this.controls.maxDistance = 20000;

    // Picking during camera movement is ruinous: each pick renders the scene
    // again and then fences on the GPU, which has to drain every frame already
    // queued. Anything watching the cursor should stand aside while moving.
    this.moving = false;
    this.lastMoved = 0;
    // Redrawing is the point of this, not just the bookkeeping. The loop only
    // renders when controls.update() reports movement, and that is not enough
    // on its own: OrbitControls applies a wheel dolly *synchronously inside its
    // own handler*, so by the next frame there is nothing left to apply and
    // update() returns false. Rotating and panning are damped and so keep
    // reporting movement for several frames, which is why only zoom was
    // affected -- it moved the camera and left the picture as it was.
    const moved = () => { this.lastMoved = performance.now(); this.requestRender(); };
    this.controls.addEventListener('change', moved);
    this.controls.addEventListener('start', () => { this.moving = true; moved(); });
    this.controls.addEventListener('end', () => { this.moving = false; moved(); });
    // Straight from the input as well: a wheel tick is movement whether or not
    // the control loop has got round to reporting it yet.
    this.renderer.domElement.addEventListener('wheel', moved, { passive: true });
    this.renderer.domElement.addEventListener('pointerdown', moved);

    this.sceneRadius = 50;
    this.sceneCentre = new THREE.Vector3();
    this.afterRender = []; // hooks that need post-render screen positions
    this.dirty = true;
    this.running = true;
    this.depthCue = true;
    this.slab = null;

    this.pickTarget = new THREE.WebGLRenderTarget(1, 1, {
      minFilter: THREE.NearestFilter,
      magFilter: THREE.NearestFilter,
      depthBuffer: true,
    });
    this.pickMaterial = new THREE.ShaderMaterial({
      vertexShader: PICK_VERTEX,
      fragmentShader: PICK_FRAGMENT,
      side: THREE.DoubleSide,
    });
    this.pickBuffer = new Uint8Array(4);

    this.clipNear = new THREE.Plane(new THREE.Vector3(0, 0, -1), 0);
    this.clipFar = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);

    this.#loop();
  }

  get aspect() {
    const w = this.container.clientWidth || 1;
    const h = this.container.clientHeight || 1;
    return w / h;
  }

  #setupLights() {
    // Lights ride on the camera so the molecule is always lit from the viewer's
    // side, which is what people expect from a molecular viewer.
    const key = new THREE.DirectionalLight(0xffffff, 2.4);
    key.position.set(0.6, 0.8, 1);
    const fill = new THREE.DirectionalLight(0xbcd4ff, 0.9);
    fill.position.set(-0.8, -0.2, 0.4);
    const rim = new THREE.DirectionalLight(0xffe6c4, 0.5);
    rim.position.set(0.2, -0.6, -1);
    this.camera.add(key, fill, rim);
    this.scene.add(new THREE.HemisphereLight(0x9db8e0, 0x30343c, 0.9));
  }

  setBackground(hex) {
    this.background = hex;
    this.scene.background = new THREE.Color(hex);
    this.renderer.setClearColor(hex, 1);
    if (this.scene.fog) this.scene.fog.color.setHex(hex);
    this.requestRender();
  }

  setDepthCue(enabled) {
    this.depthCue = enabled;
    if (!enabled) this.scene.fog = null;
    this.requestRender();
  }

  /** Depth slab in fractions of the scene radius, or null to disable. */
  setSlab(slab) {
    this.slab = slab;
    this.renderer.clippingPlanes = slab ? [this.clipNear, this.clipFar] : [];
    this.requestRender();
  }

  requestRender() { this.dirty = true; }

  /** True while the camera is being moved, or has just settled. */
  cameraBusy(quietMs = 180) {
    return this.moving || (performance.now() - this.lastMoved) < quietMs;
  }

  resize() {
    const w = this.container.clientWidth || 1;
    const h = this.container.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.requestRender();
  }

  #loop = () => {
    if (!this.running) return;
    requestAnimationFrame(this.#loop);
    const moved = this.controls.update();
    if (moved || this.dirty) {
      this.dirty = false;
      this.render();
    }
  };

  #updateCameraPlanes() {
    const distance = this.camera.position.distanceTo(this.controls.target);
    const radius = Math.max(this.sceneRadius, 1);
    // Bound the planes to the scene sphere rather than to the orbit target:
    // panning moves the target away from the geometry, and a depth range wider
    // than it needs to be is what produces z-fighting once you are up close.
    const toCentre = this.camera.position.distanceTo(this.sceneCentre);
    this.camera.near = Math.max(0.05, toCentre - radius * 1.1);
    this.camera.far = toCentre + radius * 1.1;
    this.camera.updateProjectionMatrix();

    if (this.depthCue) {
      const near = Math.max(0.1, distance - radius * 0.9);
      const far = distance + radius * 1.5;
      if (!this.scene.fog) this.scene.fog = new THREE.Fog(this.background, near, far);
      this.scene.fog.color.setHex(this.background);
      this.scene.fog.near = near;
      this.scene.fog.far = far;
    }

    if (this.slab) {
      const forward = new THREE.Vector3();
      this.camera.getWorldDirection(forward);
      const origin = this.camera.position.dot(forward);
      const near = distance - radius * this.slab.near;
      const far = distance + radius * this.slab.far;
      this.clipNear.normal.copy(forward);
      this.clipNear.constant = -(origin + near);
      this.clipFar.normal.copy(forward).negate();
      this.clipFar.constant = origin + far;
    }
  }

  render() {
    this.#updateCameraPlanes();
    this.renderer.render(this.scene, this.camera);
    for (const hook of this.afterRender) hook();
  }

  /**
   * Render one pixel with every mesh's pick colour. The camera view offset
   * shrinks the frustum to that pixel, so frustum culling throws away
   * everything the cursor is not over and the pass costs almost nothing.
   */
  #renderPickPixel(clientX, clientY) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;
    if (x < 0 || y < 0 || x > rect.width || y > rect.height) return false;

    const helpersWereVisible = this.helpers.visible;
    this.helpers.visible = false;
    this.camera.setViewOffset(rect.width, rect.height, x, y, 1, 1);
    this.#updateCameraPlanes();
    this.scene.overrideMaterial = this.pickMaterial;

    const previousBackground = this.scene.background;
    this.scene.background = null;
    this.renderer.setRenderTarget(this.pickTarget);
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.clear();
    this.renderer.render(this.scene, this.camera);
    this.renderer.setClearColor(this.background, 1);

    this.scene.overrideMaterial = null;
    this.scene.background = previousBackground;
    this.camera.clearViewOffset();
    this.helpers.visible = helpersWereVisible;
    return true;
  }

  #finishPick() {
    this.renderer.setRenderTarget(null);
    this.requestRender();
    return decodePick(this.pickBuffer);
  }

  /** Identify what is under a client-space point. Blocks on the GPU. */
  pick(clientX, clientY) {
    if (!this.#renderPickPixel(clientX, clientY)) return null;
    this.renderer.readRenderTargetPixels(this.pickTarget, 0, 0, 1, 1, this.pickBuffer);
    return this.#finishPick();
  }

  /**
   * Same, without the readback stall -- reading pixels synchronously waits for
   * the whole GPU queue to drain, which costs over 100 ms on a large assembly
   * and would make hovering feel broken.
   */
  async pickAsync(clientX, clientY) {
    if (!this.renderer.readRenderTargetPixelsAsync) return this.pick(clientX, clientY);
    if (!this.#renderPickPixel(clientX, clientY)) return null;
    await this.renderer.readRenderTargetPixelsAsync(this.pickTarget, 0, 0, 1, 1, this.pickBuffer);
    return this.#finishPick();
  }

  /** World-space bounding box of everything currently visible. */
  worldBox() {
    const box = new THREE.Box3();
    let found = false;
    this.world.traverseVisible((node) => {
      if (!node.isMesh || !node.geometry) return;
      if (!node.geometry.boundingBox) node.geometry.computeBoundingBox();
      const b = node.geometry.boundingBox.clone();
      if (node.isInstancedMesh && node.boundingBox) b.copy(node.boundingBox);
      b.applyMatrix4(node.matrixWorld);
      box.union(b);
      found = true;
    });
    return found ? box : null;
  }

  /**
   * Recompute the radius used for near/far planes and depth cueing. Call after
   * structures are added, removed or moved.
   */
  updateBounds() {
    const box = this.worldBox();
    if (box && !box.isEmpty()) {
      const sphere = box.getBoundingSphere(new THREE.Sphere());
      this.sceneRadius = Math.max(sphere.radius, 1);
      this.sceneCentre.copy(sphere.center);
    } else {
      this.sceneRadius = 50;
      this.sceneCentre.set(0, 0, 0);
    }
    // Zoom limits scaled to what is loaded: fixed ones are either far too
    // coarse for a small peptide or far too tight for a whole assembly.
    this.controls.minDistance = Math.max(0.5, this.sceneRadius * 0.02);
    this.controls.maxDistance = this.sceneRadius * 40;
    this.requestRender();
  }

  /** Frame a box (or the whole scene) without changing the view direction. */
  frame(box = null, animate = true) {
    const target = box || this.worldBox();
    if (!target || target.isEmpty()) return;
    const sphere = target.getBoundingSphere(new THREE.Sphere());
    const radius = Math.max(sphere.radius, 1);

    const vFov = (this.camera.fov * Math.PI) / 180;
    const hFov = 2 * Math.atan(Math.tan(vFov / 2) * this.camera.aspect);
    const fov = Math.min(vFov, hFov);
    const distance = (radius / Math.sin(fov / 2)) * 1.1;

    const direction = new THREE.Vector3().subVectors(this.camera.position, this.controls.target);
    if (direction.lengthSq() < 1e-6) direction.set(0, 0, 1);
    direction.normalize();

    const to = { target: sphere.center.clone(), position: sphere.center.clone().addScaledVector(direction, distance) };
    if (animate) this.#animateCamera(to);
    else {
      this.controls.target.copy(to.target);
      this.camera.position.copy(to.position);
      this.controls.update();
      this.requestRender();
    }
  }

  #animateCamera(to, duration = 320) {
    const fromTarget = this.controls.target.clone();
    const fromPosition = this.camera.position.clone();
    const start = performance.now();
    const step = () => {
      const t = Math.min(1, (performance.now() - start) / duration);
      const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      this.controls.target.lerpVectors(fromTarget, to.target, e);
      this.camera.position.lerpVectors(fromPosition, to.position, e);
      this.requestRender();
      if (t < 1) requestAnimationFrame(step);
    };
    step();
  }

  setAutoRotate(enabled, speed = 1.2) {
    this.controls.autoRotate = enabled;
    this.controls.autoRotateSpeed = speed;
    this.requestRender();
  }

  /** PNG data URL, rendered at `scale` times the on-screen resolution. */
  screenshot(scale = 2) {
    const pixelRatio = this.renderer.getPixelRatio();
    this.renderer.setPixelRatio(Math.min(pixelRatio * scale, 4));
    this.resize();
    this.render();
    const data = this.renderer.domElement.toDataURL('image/png');
    this.renderer.setPixelRatio(pixelRatio);
    this.resize();
    return data;
  }

  dispose() {
    this.running = false;
    this.resizeObserver.disconnect();
    this.controls.dispose();
    this.pickTarget.dispose();
    this.pickMaterial.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
