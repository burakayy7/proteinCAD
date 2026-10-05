// Application state and behaviour.
//
// The App owns the document (a list of StructureViews), the selection, and the
// interaction modes. The UI modules under ui/ only read from it and call its
// methods; they never touch three.js directly. Everything the panels need to
// react to is announced through `on(event, handler)`.

import * as THREE from 'three';
import { Viewer } from './render/viewer.js';
import { StructureView } from './render/structureView.js';
import { MapView } from './render/mapView.js';
import { Markers } from './render/markers.js';
import { Gizmo } from './render/gizmo.js';
import { parseStructure } from './io/load.js';
import { readCCP4Stream } from './io/ccp4.js';
import { DensityMap } from './core/density.js';
import { emdbId, entryUrl, mapUrl, readEbiEntry } from './io/emdb.js';
import {
  GeometricScorer, angleList, detectAxis, descriptors, expectedPeriod, riseList,
  splitComponents,
} from './core/landscape.js';
import { writePDB, chainIdsFor, downloadText } from './io/write.js';
import {
  compileSelection, countMask, describeSelection, maskForChain, maskForResidue,
} from './core/selection.js';
import { Kind } from './core/structure.js';
import { DesignVolume } from './render/volume.js';
import {
  buildEsm3Spec, buildJobSpec, ESM3_MODE_NEEDS, MODE_NEEDS,
} from './design/spec.js';
import { throttle } from './ui/dom.js';
// Where the server is, and the token to reach it with. Everything under
// core/, io/ and design/ stays free of this: they are pure and node-testable,
// and the address of a deployment is not their business.
import * as api from './api.js';

const MAX_STRUCTURES = 200;

export class App {
  constructor(elements) {
    this.elements = elements;
    this.viewer = new Viewer(elements.viewport);
    this.markers = new Markers(this.viewer, elements.labels);
    this.gizmo = new Gizmo(this.viewer);

    this.views = [];
    // Density maps, kept beside the structures rather than among them. A map
    // has no atoms, so every loop that walks chains, selects residues or
    // exports coordinates would otherwise have to ask each entry whether it is
    // really a molecule.
    this.maps = [];
    this.slots = new Map();
    this.nextSlot = 1;
    this.selection = new Map();
    this.hovered = null;
    this.mode = 'select';
    this.listeners = new Map();
    this.busyCount = 0;

    // The structure the tools act on. Set by loading, by clicking in the
    // viewport, or by clicking a row in the tree -- never inferred from load
    // order, which is what used to pin the move handle to the first structure.
    this.activeView = null;
    // The map the move handle is on, when it is on one. Separate from
    // activeView because a map is never a selection target.
    this.activeMap = null;
    this.transformTarget = 'structure'; // 'structure' | 'chain' | 'volume'

    // Design state: the residues to build against, the shape to fill, and the
    // jobs that have been sent off.
    this.hotspots = new Map(); // view -> Set of residue indices
    this.volume = null;
    this.jobs = [];
    this.jobTimer = null;
    // What is left of the caller's allowance, on a deployment that has one.
    // Null means nobody is counting.
    this.quota = null;
    this.design = {
      // Which model draws the backbone. The two have nothing in common but the
      // job envelope -- different protocols, different settings, different idea
      // of what an input is -- so each keeps its own protocol and its own
      // settings bag below, and switching engines does not disturb the other's.
      engine: 'rfdiffusion',
      // Which protocol this is, within the engine above. Everything else in
      // this object is the same whichever one it is; the protocol decides what
      // the scene has to provide and which settings are worth showing.
      mode: 'binder',
      // The protocol each engine was last on, so switching away and back is not
      // a reset.
      lastMode: { rfdiffusion: 'binder', esm3: 'generate' },
      cropRadius: 12,
      lengthMin: 60,
      lengthMax: 100,
      numDesigns: 4,
      seed: 0,
      // Empty means "use the one built from the picked residues", which is what
      // almost every run wants. Typed in, it wins -- and it is the only way to
      // express a motif, an inpainted span, or a chain break nothing can infer
      // from a click.
      contigs: '',
      // Every RFdiffusion setting, flat, named as the option catalogue names
      // it. Deliberately one bag rather than a field each: the catalogue comes
      // from the server, so a capability added there appears here without this
      // file changing. Only what is in here is sent, and absence means "leave
      // RFdiffusion's own default alone".
      rf: {
        // Diffusion steps. The model was trained at 50; fewer is proportionally
        // faster and proportionally rougher.
        steps: 50,
        // 0 is the binder-design recommendation and gives the best hit rate, at
        // the cost of a batch that looks like one idea four times.
        noiseScale: 0,
      },
      // The same, for ESM3. A bag of its own rather than a shared one: the two
      // catalogues have keys with the same names and different meanings, and
      // merging them would let a setting chosen for one engine be sent to the
      // other. Empty means "the plan the protocol brings with it".
      esm3: {},
      // Stage two. A backbone has no sequence; ProteinMPNN picks one, cheaply
      // enough that asking for several costs nothing. Folding them is the
      // expensive part, so only the best few are checked.
      numSeqs: 8,
      foldTop: 2,
      // Sampling temperature for the sequence. Low keeps to what the backbone
      // most wants; raise it for a batch that differs from itself.
      samplingTemp: 0.1,
      model: 'mock',
      includeLigands: false,
    };

    // The protocols and settings this build can be asked for, fetched once from
    // the server. Nothing here is written into the browser: the table lives
    // beside the command builder that reads it, so the panel and the command
    // line cannot drift apart.
    this.designModes = [];
    this.designEngines = [];
    this.designCatalogue = null;

    // Rotational landscape state. The rotor and the axle are sets of chain ids
    // per structure, because that is what the question is about -- "these
    // chains turn, those ones do not" -- and it survives the structures being
    // moved, recoloured or re-represented in between.
    this.rotor = new Map();  // view -> Set of chain ids
    this.axle = new Map();
    this.landscape = null;   // the scan last asked for, as the server reports it
    this.scanTimer = null;
    this.scanWorker = null;
    this.scanBackends = null;
    // Set when this deployment has no landscape API at all, with the reason.
    this.scanUnsupported = '';
    this.motion = { step: 5, backend: 'geometric', rise: 0, riseStep: 1 };
    // Where the rotor has been dragged to, and the matrices it started from.
    // The angle is display only: a scan is always built from zero, so dragging
    // the scrubber cannot change what the next scan measures.
    this.rotorAngle = 0;
    this.rotorBase = null;

    this.settings = {
      representation: 'cartoon',
      colorScheme: 'chain',
      showWater: false,
      showHydrogens: false,
      background: 0x0e1116,
      depthCue: true,
      hoverPick: true,
      spin: false,
    };

    this.gizmo.onMove = () => { this.markers.refresh(); this.emit('gizmo-move'); };
    this.gizmo.onDrop = () => this.emit('transform');
    this.markers.onChange = () => this.emit('measure');

    this.#bindPointer();
  }

  // ---------------------------------------------------------------- events

  on(event, handler) {
    if (!this.listeners.has(event)) this.listeners.set(event, []);
    this.listeners.get(event).push(handler);
    return this;
  }

  emit(event, payload) {
    for (const handler of this.listeners.get(event) || []) handler(payload, this);
  }

  #busy(on, text = 'Working…') {
    this.busyCount = Math.max(0, this.busyCount + (on ? 1 : -1));
    this.elements.loading.hidden = this.busyCount === 0;
    if (on) this.#status(text);
  }

  /** Change the busy message without touching the counter. */
  #status(text) {
    this.elements.loadingText.textContent = text;
  }

  /**
   * Yield to the browser so the spinner actually paints before a long build.
   *
   * The timeout is not belt and braces: a hidden or backgrounded tab has its
   * animation frames throttled or stopped altogether, and without a fallback a
   * load started before switching tabs would never finish.
   */
  #yield() {
    return new Promise((resolve) => {
      let settled = false;
      const finish = () => { if (!settled) { settled = true; resolve(); } };
      if (typeof requestAnimationFrame === 'function' && !document.hidden) {
        requestAnimationFrame(() => setTimeout(finish, 0));
      }
      setTimeout(finish, 200);
    });
  }

  // --------------------------------------------------------------- loading

  async loadText(text, filename) {
    if (this.views.length >= MAX_STRUCTURES) throw new Error('Too many structures loaded');
    this.#busy(true, `Parsing ${filename}…`);
    try {
      await this.#yield();
      const structure = parseStructure(text, filename);
      this.#status(`Building ${structure.atomCount.toLocaleString()} atoms…`);
      await this.#yield();

      const view = new StructureView(structure, this.nextSlot, this.views.length);
      this.slots.set(this.nextSlot, view);
      this.nextSlot++;
      view.showWater = this.settings.showWater;
      view.showHydrogens = this.settings.showHydrogens;
      view.colorScheme = this.settings.colorScheme;
      view.representation = this.settings.representation;
      view.refreshColors();
      view.build();

      this.viewer.world.add(view.group);
      this.views.push(view);
      this.viewer.updateBounds();
      if (this.views.length === 1) this.viewer.frame(null, false);
      else this.viewer.requestRender();

      // Whatever you just loaded is what you want to work on.
      this.setActiveView(view);
      this.emit('structures');
      return view;
    } finally {
      this.#busy(false);
    }
  }

  async loadFiles(files) {
    for (const file of files) {
      try {
        const text = await readFile(file);
        await this.loadText(text, file.name);
      } catch (error) {
        this.emit('error', `${file.name}: ${error.message}`);
      }
    }
  }

  /** Fetch from the local server (which caches), falling back to the RCSB. */
  async fetchStructure(id) {
    const code = String(id).trim().toUpperCase();
    if (!/^[A-Z0-9]{4}[A-Z0-9]*$/.test(code)) throw new Error(`"${id}" is not a PDB id`);
    // Where this deployment's API is, before asking whether it has one. A
    // `?load=` in the URL runs this before config.json has been read, and
    // `hosted()` then answers "no" for a deployment that is -- which sends the
    // first fetch of every page load to an API that does not serve structures.
    await api.ready;
    this.#busy(true, `Fetching ${code}…`);
    try {
      let text = null;
      let name = `${code}.cif`;
      // A hosted deployment has no structure route: the RCSB sends
      // Access-Control-Allow-Origin, so the browser fetches it directly rather
      // than paying a Lambda to pass a thirty megabyte file through itself.
      if (!api.hosted()) {
        try {
          const response = await api.get(`structure/${code}`);
          if (response.ok) {
            text = await response.text();
            name = response.headers.get('X-Structure-Filename') || name;
          }
        } catch { /* server not running: fall through to the RCSB */ }
      }
      if (text === null) {
        const response = await fetch(`https://files.rcsb.org/download/${code}.cif`);
        if (!response.ok) throw new Error(`RCSB returned ${response.status}`);
        text = await response.text();
      }
      return await this.loadText(text, name);
    } finally {
      this.#busy(false);
    }
  }

  /**
   * Fetch whatever the id names: a structure from the PDB, a map from EMDB.
   *
   * One entry point, because from the box at the top of the window they are the
   * same action -- "show me this thing" -- and which databank it lives in is a
   * property of the id rather than a mode the user should have to pick.
   */
  async fetchById(id) {
    const emdb = emdbId(id);
    return emdb ? this.fetchMap(emdb) : this.fetchStructure(id);
  }

  /**
   * Fetch an EMDB map: this server first, then the EBI.
   *
   * Exactly the order `fetchStructure` uses, and for the same reasons. The
   * server caches, so the second visit costs nothing and works offline; the EBI
   * sends `Access-Control-Allow-Origin: *`, so a static copy of `web/` with no
   * server behind it can still read a map.
   *
   * The map is read as it arrives rather than buffered and then read. EMD-25576
   * is 244 MB decompressed from a 5 MB download, and the reader reduces it on
   * the way past, so the only array that ever exists is the one that renders.
   */
  async fetchMap(id) {
    const code = emdbId(id);
    if (!code) throw new Error(`"${id}" is not an EMDB id`);
    // See fetchStructure: `hosted()` is only meaningful once config.json is in.
    await api.ready;
    this.#busy(true, `Fetching ${code}…`);
    try {
      // Metadata in parallel: it carries the level the depositors looked at the
      // map at, and a map contoured at the wrong level is a solid block or
      // nothing at all. It is never allowed to hold up the map itself.
      const meta = this.#fetchMapMeta(code);

      let response = null;
      if (!api.hosted()) {
        try {
          const local = await api.get(`map/${code}`);
          if (local.ok) response = local;
        } catch { /* no server: go to the EBI */ }
      }
      if (!response) {
        response = await fetch(mapUrl(code));
        if (!response.ok) {
          throw new Error(response.status === 404
            ? `EMDB has no entry ${code}`
            : `EBI returned ${response.status} for ${code}`);
        }
      }

      const grid = await readCCP4Stream(response, {
        filename: `${code}.map.gz`,
        onProgress: (bytes) => {
          this.#status(`Reading ${code}… ${(bytes / 1048576).toFixed(0)} MB`);
        },
      });
      const map = new DensityMap(grid, code);
      const detail = await meta;
      map.title = detail.title || '';
      map.recommended = detail.contour;
      map.fitted = detail.fitted || [];
      map.resolution = detail.resolution || null;
      return this.addMap(map);
    } finally {
      this.#busy(false);
    }
  }

  async #fetchMapMeta(code) {
    const blank = { title: '', contour: null, fitted: [], resolution: null };
    if (!api.hosted()) {
      try {
        const local = await api.get(`map/${code}/meta`);
        if (local.ok) return { ...blank, ...(await local.json()) };
      } catch { /* fall through */ }
    }
    try {
      const response = await fetch(entryUrl(code), { cache: 'no-store' });
      if (response.ok) return { ...blank, ...readEbiEntry(await response.json()) };
    } catch { /* offline, or EBI is having a day */ }
    return blank;
  }

  /** Put a parsed map into the scene. */
  addMap(map) {
    const existing = this.maps.find((view) => view.map.name === map.name);
    if (existing) this.removeMap(existing);

    const view = new MapView(map, this.maps.length);
    if (!view.mesh) {
      // Nothing enclosed at the level we chose: drop to a few sigma, which
      // always encloses something, rather than adding an invisible object.
      view.setLevel(map.levelForSigma(3));
    }
    this.maps.push(view);
    this.viewer.world.add(view.group);
    this.viewer.updateBounds();
    this.viewer.requestRender();
    this.emit('structures');
    if (this.views.length === 0 && this.maps.length === 1) this.frameAll();
    return view;
  }

  removeMap(view) {
    const index = this.maps.indexOf(view);
    if (index < 0) return;
    this.maps.splice(index, 1);
    this.viewer.world.remove(view.group);
    view.dispose();
    if (this.activeMap === view) this.activeMap = null;
    this.viewer.updateBounds();
    this.viewer.requestRender();
    this.emit('structures');
  }

  /** Re-contour one map. The field and the transform stay as they are. */
  setMapLevel(view, level) {
    view.setLevel(level);
    this.viewer.requestRender();
    this.emit('structures');
  }

  /** Move a map with the gizmo, the same way a structure is moved. */
  moveMap(view, mode = 'translate') {
    this.activeMap = view;
    this.gizmo.attach(view.group, mode);
    this.emit('gizmo');
  }

  remove(view) {
    const index = this.views.indexOf(view);
    if (index < 0) return;
    if (this.gizmo.object && isDescendant(this.gizmo.object, view.group)) this.gizmo.detach();
    this.markers.removeForView(view);
    this.selection.delete(view);
    this.slots.delete(view.slot);
    this.views.splice(index, 1);
    view.dispose();
    if (this.activeView === view) {
      this.activeView = this.views[Math.min(index, this.views.length - 1)] || null;
    }
    this.viewer.updateBounds();
    this.viewer.requestRender();
    this.emit('structures');
    this.emit('selection');
    this.emit('transform');
  }

  removeAll() {
    for (const view of [...this.views]) this.remove(view);
    for (const view of [...this.maps]) this.removeMap(view);
  }

  // -------------------------------------------------------------- settings

  updateSettings(patch) {
    const before = { ...this.settings };
    Object.assign(this.settings, patch);

    if (patch.representation !== undefined) {
      this.setRepresentation(patch.representation);
    }
    if (patch.colorScheme !== undefined) {
      for (const view of this.views) view.setColorScheme(patch.colorScheme);
    }
    if (patch.showWater !== undefined && patch.showWater !== before.showWater) {
      for (const view of this.views) view.setShowWater(patch.showWater);
    }
    if (patch.showHydrogens !== undefined && patch.showHydrogens !== before.showHydrogens) {
      for (const view of this.views) view.setShowHydrogens(patch.showHydrogens);
    }
    if (patch.background !== undefined) this.viewer.setBackground(patch.background);
    if (patch.depthCue !== undefined) this.viewer.setDepthCue(patch.depthCue);
    if (patch.spin !== undefined) this.viewer.setAutoRotate(patch.spin);
    if (patch.slab !== undefined) this.viewer.setSlab(patch.slab);

    this.viewer.requestRender();
    this.emit('settings');
  }

  /**
   * Switch representation, for one structure or all of them. Rebuilding a
   * surface over a few hundred thousand atoms takes about a second, so the
   * spinner goes up and the browser gets a frame to paint it first.
   */
  async setRepresentation(id, target = null) {
    const views = target ? [target] : this.views;
    if (!views.length) return;
    const heavy = views.some((view) => view.structure.atomCount > 50000);
    if (heavy) {
      this.#busy(true, `Building ${id}…`);
      await this.#yield();
    }
    try {
      for (const view of views) view.setRepresentation(id);
      if (!target) this.settings.representation = id;
    } finally {
      if (heavy) this.#busy(false);
    }
    this.viewer.requestRender();
    this.emit('structures');
  }

  setMode(mode) {
    this.mode = mode;
    if (mode !== 'measure') this.markers.cancelPending();
    this.emit('settings');
  }

  // ------------------------------------------------------------- selection

  selectionMask(view) {
    return this.selection.get(view) || null;
  }

  get selectionCount() {
    let atoms = 0;
    for (const mask of this.selection.values()) atoms += countMask(mask);
    return atoms;
  }

  /**
   * @param {StructureView} view
   * @param {Uint8Array} mask
   * @param {'replace'|'add'|'toggle'|'subtract'} how
   */
  selectAtoms(view, mask, how = 'replace') {
    if (how === 'replace') {
      for (const other of this.views) if (other !== view) this.#applyMask(other, null);
      this.#applyMask(view, mask);
    } else {
      const current = this.selection.get(view);
      if (!current) {
        this.#applyMask(view, how === 'subtract' ? null : mask);
      } else {
        const next = current.slice();
        let overlap = true;
        if (how === 'toggle') {
          for (let i = 0; i < mask.length; i++) if (mask[i] && !current[i]) { overlap = false; break; }
        }
        for (let i = 0; i < mask.length; i++) {
          if (!mask[i]) continue;
          if (how === 'add') next[i] = 1;
          else if (how === 'subtract') next[i] = 0;
          else next[i] = overlap ? 0 : 1;
        }
        this.#applyMask(view, next);
      }
    }
    this.emit('selection');
  }

  #applyMask(view, mask) {
    if (!mask || countMask(mask) === 0) {
      this.selection.delete(view);
      view.setSelection(null);
    } else {
      this.selection.set(view, mask);
      view.setSelection(mask);
    }
    this.viewer.requestRender();
  }

  clearSelection() {
    for (const view of this.views) this.#applyMask(view, null);
    this.emit('selection');
  }

  invertSelection() {
    for (const view of this.views) {
      const current = this.selection.get(view);
      const next = new Uint8Array(view.structure.atomCount);
      for (let i = 0; i < next.length; i++) next[i] = current && current[i] ? 0 : 1;
      this.#applyMask(view, next);
    }
    this.emit('selection');
  }

  /** Run a selection expression against every loaded structure. */
  selectExpression(expression) {
    const compiled = compileSelection(expression);
    let total = 0;
    for (const view of this.views) {
      const mask = compiled(view.structure);
      total += countMask(mask);
      this.#applyMask(view, mask);
    }
    this.emit('selection');
    return total;
  }

  selectChain(view, chainIndex, how = 'replace') {
    this.selectAtoms(view, maskForChain(view.structure, chainIndex), how);
  }

  /** Select every copy of one molecule -- all 33 hook proteins, say. */
  selectEntity(view, entityIndex, how = 'replace') {
    const structure = view.structure;
    const mask = new Uint8Array(structure.atomCount);
    for (const res of structure.residues) {
      if (res.entityIndex !== entityIndex) continue;
      for (let i = res.start; i <= res.end; i++) mask[i] = 1;
    }
    this.selectAtoms(view, mask, how);
  }

  describeSelection() {
    const parts = [];
    for (const [view, mask] of this.selection) {
      parts.push({ view, ...describeSelection(view.structure, mask) });
    }
    return parts;
  }

  // ------------------------------------------------------------------ view

  frameAll() {
    this.viewer.updateBounds();
    this.viewer.frame(null, true);
  }

  frameSelection() {
    const box = new THREE.Box3();
    let found = false;
    for (const [view, mask] of this.selection) {
      box.union(view.boxOfMask(mask));
      found = true;
    }
    if (!found) return this.frameAll();
    box.expandByScalar(2.5);
    return this.viewer.frame(box, true);
  }

  /** Hide every chain that the selection does not touch. */
  isolateSelection() {
    if (!this.selection.size) return;
    for (const view of this.views) {
      const mask = this.selection.get(view);
      view.setVisible(!!mask);
      for (const chain of view.structure.chains) {
        let hit = false;
        if (mask) {
          for (let i = chain.atomStart; i <= chain.atomEnd; i++) if (mask[i]) { hit = true; break; }
        }
        view.setChainVisible(chain.index, hit);
      }
    }
    this.viewer.requestRender();
    this.emit('structures');
  }

  hideSelection() {
    for (const [view, mask] of this.selection) {
      for (const chain of view.structure.chains) {
        let hit = false;
        for (let i = chain.atomStart; i <= chain.atomEnd; i++) if (mask[i]) { hit = true; break; }
        if (hit) view.setChainVisible(chain.index, false);
      }
    }
    this.viewer.requestRender();
    this.emit('structures');
  }

  showAll() {
    for (const view of this.views) {
      view.setVisible(true);
      for (const chain of view.structure.chains) view.setChainVisible(chain.index, true);
    }
    this.viewer.requestRender();
    this.emit('structures');
  }

  // ------------------------------------------------------------ transforms

  /** Make a structure the one the tools act on. */
  setActiveView(view) {
    const next = view && this.views.includes(view) ? view : null;
    if (this.activeView === next) return;
    this.activeView = next;
    // Re-point the handle rather than leaving it on the previous structure.
    if (this.gizmo.object) this.attachGizmo(this.gizmo.mode);
    this.emit('structures');
    this.emit('transform');
  }

  setTransformTarget(target) {
    this.transformTarget = ['chain', 'volume'].includes(target) ? target : 'structure';
    if (this.gizmo.object) this.attachGizmo(this.gizmo.mode);
    this.emit('transform');
  }

  /** The single chain selected within one structure, or null. */
  selectedChainOf(view) {
    const mask = this.selection.get(view);
    if (!mask) return null;
    const summary = describeSelection(view.structure, mask);
    if (summary.chains.length !== 1) return null;
    return view.structure.chains.find((c) => c.id === summary.chains[0]) || null;
  }

  /** What the handle should be attached to right now, or null. */
  gizmoTarget() {
    if (this.transformTarget === 'volume') return this.volume ? this.volume.group : null;
    const view = this.activeView;
    if (!view) return null;
    if (this.transformTarget === 'chain') {
      const chain = this.selectedChainOf(view);
      return chain ? view.unitForChain(chain.index) : null;
    }
    return view.group;
  }

  /** Attach the move/rotate handle to the current target. */
  attachGizmo(mode = 'translate') {
    const object = this.gizmoTarget();
    if (!object) {
      this.gizmo.detach();
      this.emit('transform');
      return null;
    }
    this.gizmo.attach(object, mode);
    this.emit('transform');
    return this.gizmo;
  }

  /** Move a specific structure, whatever was active before. */
  moveStructure(view, mode = 'translate') {
    this.setActiveView(view);
    this.transformTarget = 'structure';
    this.attachGizmo(mode);
  }

  /** Move one chain (and everything under its id), selecting it so it is clear. */
  moveChain(view, chainIndex, mode = 'translate') {
    this.setActiveView(view);
    this.selectChain(view, chainIndex, 'replace');
    this.transformTarget = 'chain';
    this.attachGizmo(mode);
  }

  detachGizmo() {
    this.gizmo.detach();
    this.emit('transform');
  }

  /**
   * Another instance of the same structure. The parsed model is shared -- only
   * geometry is rebuilt -- so copies are cheap even for large assemblies.
   */
  duplicate(view, options = {}) {
    if (this.nextSlot > 255) throw new Error('too many structures for picking (255 max)');
    const copy = new StructureView(view.structure, this.nextSlot, this.views.length);
    this.slots.set(this.nextSlot, copy);
    this.nextSlot++;

    copy.label = nextCopyLabel(this.views, view);
    copy.representation = view.representation;
    copy.colorScheme = view.colorScheme;
    copy.uniformColor = view.uniformColor;
    copy.showWater = view.showWater;
    copy.showHydrogens = view.showHydrogens;
    copy.refreshColors();
    copy.build();

    view.group.updateMatrixWorld(true);
    copy.group.position.copy(view.group.position);
    copy.group.quaternion.copy(view.group.quaternion);
    if (options.offset !== false) {
      const step = Math.max(view.structure.radius * 1.4, 20);
      copy.group.position.x += step;
    }
    copy.group.updateMatrixWorld(true);

    this.viewer.world.add(copy.group);
    this.views.push(copy);
    this.viewer.updateBounds();
    this.viewer.requestRender();
    this.emit('structures');
    return copy;
  }

  /**
   * Ring of copies about an axis: place one subunit where it belongs relative to
   * the assembly, then repeat it around. Symmetric cryo-EM structures are
   * usually deposited with their symmetry axis on z, so that is the default.
   */
  arrayAbout(view, options = {}) {
    const count = Math.max(2, Math.min(120, Math.round(options.count || 2)));
    const axisName = options.axis || 'z';
    const axis = new THREE.Vector3(
      axisName === 'x' ? 1 : 0, axisName === 'y' ? 1 : 0, axisName === 'z' ? 1 : 0
    );
    const centre = options.centre
      ? new THREE.Vector3().fromArray(options.centre)
      : new THREE.Vector3().fromArray((options.about || view).structure.center);

    const made = [];
    const rotation = new THREE.Quaternion();
    const offset = new THREE.Vector3();
    for (let i = 1; i < count; i++) {
      const copy = this.duplicate(view, { offset: false });
      rotation.setFromAxisAngle(axis, (i * 2 * Math.PI) / count);
      offset.copy(copy.group.position).sub(centre).applyQuaternion(rotation).add(centre);
      copy.group.position.copy(offset);
      copy.group.quaternion.premultiply(rotation);
      copy.group.updateMatrixWorld(true);
      copy.label = `${view.label} ${i + 1}/${count}`;
      made.push(copy);
    }
    this.viewer.updateBounds();
    this.viewer.requestRender();
    this.emit('structures');
    return made;
  }

  resetTransforms() {
    for (const view of this.views) view.resetTransforms();
    this.markers.refresh();
    this.viewer.updateBounds();
    this.viewer.requestRender();
    this.emit('transform');
  }

  // ---------------------------------------------------------------- design

  hotspotsOf(view) {
    return this.hotspots.get(view) || null;
  }

  get hotspotCount() {
    let total = 0;
    for (const set of this.hotspots.values()) total += set.size;
    return total;
  }

  /** Add or remove one target residue from the design site. */
  toggleHotspot(view, residueIndex) {
    let set = this.hotspots.get(view);
    if (!set) { set = new Set(); this.hotspots.set(view, set); }
    if (set.has(residueIndex)) set.delete(residueIndex);
    else set.add(residueIndex);
    if (!set.size) this.hotspots.delete(view);
    this.#refreshHotspotMarkers();
    this.emit('design');
  }

  clearHotspots() {
    this.hotspots.clear();
    this.#refreshHotspotMarkers();
    this.emit('design');
  }

  /** Turn the current selection into hotspots, one entry per residue. */
  hotspotsFromSelection() {
    for (const [view, mask] of this.selection) {
      const set = this.hotspots.get(view) || new Set();
      for (const res of view.structure.residues) {
        for (let i = res.start; i <= res.end; i++) {
          if (mask[i]) { set.add(res.index); break; }
        }
      }
      if (set.size) this.hotspots.set(view, set);
    }
    this.#refreshHotspotMarkers();
    this.emit('design');
  }

  #refreshHotspotMarkers() {
    const points = [];
    for (const [view, set] of this.hotspots) {
      for (const index of set) {
        const res = view.structure.residues[index];
        const atom = res.ca >= 0 ? res.ca : res.start;
        points.push(view.atomWorldPosition(atom));
      }
    }
    this.markers.setHotspots(points);
  }

  /** The structure the design is being built against: wherever hotspots are. */
  designTarget() {
    for (const [view, set] of this.hotspots) if (set.size) return view;
    return this.activeView;
  }

  /** Centre of the picked patch, in world space. */
  hotspotCentre(view) {
    const set = this.hotspots.get(view);
    const centre = new THREE.Vector3();
    if (!set || !set.size) return centre;
    const point = new THREE.Vector3();
    for (const index of set) {
      const res = view.structure.residues[index];
      centre.add(view.atomWorldPosition(res.ca >= 0 ? res.ca : res.start, point));
    }
    return centre.divideScalar(set.size);
  }

  addVolume(shape = 'cylinder') {
    if (this.volume) this.volume.setShape(shape);
    else this.volume = new DesignVolume(this.viewer, shape);

    const view = this.designTarget();
    if (view) {
      const centre = this.hotspots.get(view) && this.hotspots.get(view).size
        ? this.hotspotCentre(view)
        : view.box().getCenter(new THREE.Vector3());
      const outward = centre.clone().sub(view.box().getCenter(new THREE.Vector3()));
      this.volume.placeAt(centre, outward);
    }
    this.setTransformTarget('volume');
    this.attachGizmo('translate');
    this.emit('design');
    return this.volume;
  }

  removeVolume() {
    if (!this.volume) return;
    if (this.transformTarget === 'volume') this.detachGizmo();
    this.volume.dispose();
    this.volume = null;
    this.transformTarget = 'structure';
    this.emit('design');
    this.emit('transform');
  }

  updateDesign(patch) {
    Object.assign(this.design, patch);
    this.emit('design');
  }

  /** The settings bag belonging to the engine currently selected. */
  optionBag(engine = null) {
    return (engine || this.design.engine) === 'esm3' ? this.design.esm3 : this.design.rf;
  }

  /**
   * Set or clear model settings, in the current engine's bag. A value of
   * undefined removes the key, which is not the same as setting it to zero:
   * absent means "whatever the model's own default is", and that is the only way
   * a panel built from a table can stay honest about a config it does not own.
   */
  updateOptions(patch) {
    const bag = this.optionBag();
    for (const [key, value] of Object.entries(patch)) {
      if (value === undefined) delete bag[key];
      else bag[key] = value;
    }
    this.emit('design');
  }

  /**
   * Switch engine, and with it the protocol table, the settings bag and the
   * panel below.
   *
   * The protocol is restored rather than reset: somebody comparing the two
   * engines on the same site switches back and forth, and losing the protocol
   * each time would make that a retyping exercise.
   */
  setDesignEngine(id) {
    if (id === this.design.engine) return;
    this.design.lastMode[this.design.engine] = this.design.mode;
    this.design.engine = id;
    this.designModes = this.enginesById(id).modes || [];
    const wanted = this.design.lastMode[id]
      || (this.designModes[0] && this.designModes[0].id)
      || 'binder';
    // Through setDesignMode so the new protocol's recommended settings arrive
    // with it, exactly as they do when the protocol is changed by hand.
    this.design.mode = '';
    this.setDesignMode(wanted, this.designModes);
    this.emit('catalogue', this.engineCatalogue());
  }

  /** One engine's entry in the catalogue, or an empty stand-in. */
  enginesById(id) {
    const engines = (this.designCatalogue && this.designCatalogue.engines) || [];
    return engines.find((e) => e.id === id) || {};
  }

  /**
   * The catalogue for the engine in use. A server too old to report engines
   * serves RFdiffusion's tables at the top level, which is what this falls back
   * to -- the viewer is a folder of files and somebody may be serving a cached
   * copy of it against a newer or older API than it expects.
   */
  engineCatalogue() {
    const catalogue = this.designCatalogue || {};
    const engine = this.enginesById(this.design.engine);
    return engine.options ? engine : catalogue;
  }

  /**
   * Switch protocol, taking that protocol's recommended settings with it.
   *
   * The defaults come from the server's mode table. Switching to symmetric
   * oligomer should hand you the contact potentials that protocol needs, not
   * quietly undo the step count you chose -- so a setting still holding the old
   * protocol's default is treated as untouched and dropped, and anything you
   * changed is kept. Without the equality test the monomer's radius-of-gyration
   * potential survives into a symmetric run, where it is the wrong force
   * entirely and nothing says so.
   */
  setDesignMode(name, modes = null) {
    const table = modes || this.designModes || [];
    const mode = table.find((m) => m.id === name);
    const old = table.find((m) => m.id === this.design.mode);
    const bag = this.optionBag();
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    for (const [key, value] of Object.entries((old && old.defaults) || {})) {
      if (same(bag[key], value)) delete bag[key];
    }
    for (const [key, value] of Object.entries((mode && mode.defaults) || {})) {
      if (!(key in bag)) bag[key] = value;
    }
    // The generated contig is protocol-specific, so one typed for the old
    // protocol is almost certainly wrong for the new one.
    this.design.mode = name;
    this.design.lastMode[this.design.engine] = name;
    this.design.contigs = '';
    this.emit('design');
  }

  /** Build the job spec from the current scene, without sending it. */
  buildSpec() {
    const view = this.designTarget();
    if (!view) throw new Error('load a structure and pick some target residues first');
    const set = this.hotspots.get(view);

    if (this.design.engine === 'esm3') {
      const needs = ESM3_MODE_NEEDS[this.design.mode] || ESM3_MODE_NEEDS.generate;
      if (needs.target !== 'none' && needs.target !== 'optional' && (!set || !set.size)) {
        throw new Error('pick some residues to say which structure, and which part of it');
      }
      return buildEsm3Spec({
        structure: view.structure,
        label: view.label,
        chainMatrices: view.chainMatrices(),
        hotspots: set ? [...set] : [],
        mode: this.design.mode,
        options: this.design.esm3,
        cropRadius: this.design.cropRadius,
        lengthMin: this.design.lengthMin,
        lengthMax: this.design.lengthMax,
        numDesigns: this.design.numDesigns,
        seed: this.design.seed,
        model: this.design.model,
        includeLigands: this.design.includeLigands,
        volume: this.volume
          ? { ...this.volume.toJSON(), points: this.volume.pointCloud(5) }
          : null,
      });
    }

    const needs = MODE_NEEDS[this.design.mode] || MODE_NEEDS.binder;
    if (needs.target !== 'none' && (!set || !set.size)) {
      throw new Error('pick at least one target residue (Pick site, then click)');
    }

    return buildJobSpec({
      structure: view.structure,
      label: view.label,
      chainMatrices: view.chainMatrices(),
      hotspots: set ? [...set] : [],
      mode: this.design.mode,
      contigs: this.design.contigs,
      options: this.design.rf,
      cropRadius: this.design.cropRadius,
      lengthMin: this.design.lengthMin,
      lengthMax: this.design.lengthMax,
      numDesigns: this.design.numDesigns,
      seed: this.design.seed,
      model: this.design.model,
      includeLigands: this.design.includeLigands,
      volume: this.volume
        ? { ...this.volume.toJSON(), points: this.volume.pointCloud(5) }
        : null,
    });
  }

  /** Send the spec to the server, which queues it for whatever runner is set. */
  async submitDesign() {
    const spec = this.buildSpec();
    const response = await api.post('design', spec);
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      if (response.status === 404 && /no route/i.test(detail.error || '')) {
        throw new Error('This server does not have the design API. It was probably started '
          + 'before the Design tab existed — restart it: python3 -m proteincad');
      }
      throw new Error(await this.#explain(detail.error || `server returned ${response.status}`));
    }
    const job = await response.json();
    this.jobs.unshift(job);
    this.emit('design');
    this.#pollJobs();
    return job;
  }

  /**
   * Take one finished backbone on to the second stage.
   *
   * Only the choice of design and the settings go over the wire. The server
   * already holds the target as it was sent to the model and the backbone as
   * the model returned it, and building the complex from those is the only way
   * to be sure the sequence is designed against what was actually there.
   */
  async foldDesign(jobId, index = 0) {
    const response = await api.post(`jobs/${jobId}/designs/${index}/fold`, {
      model: this.design.model,
      numDesigns: this.design.numSeqs,
      foldTop: this.design.foldTop,
      samplingTemp: this.design.samplingTemp,
      seed: this.design.seed,
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      if (response.status === 404 && /no route/i.test(detail.error || '')) {
        throw new Error('This server cannot design sequences yet — restart it: python3 -m proteincad');
      }
      throw new Error(await this.#explain(detail.error || `server returned ${response.status}`));
    }
    const job = await response.json();
    this.jobs.unshift(job);
    this.emit('design');
    this.#pollJobs();
    return job;
  }

  /**
   * Turn a server error into the real one where the real one is "restart me".
   *
   * A server running older code than the files on disk does not fail oddly --
   * it fails in the way it used to, complaining about something that is no
   * longer true. The message is then about the job, and the fix is about the
   * process, and nothing connects the two.
   */
  async #explain(message) {
    try {
      const response = await api.get('health');
      if (response.ok && (await response.json()).stale) {
        return `${message}\n\nThis server is running code older than the files on disk, `
          + 'so that message may be from the version it started with. Restart it: '
          + 'python3 -m proteincad';
      }
    } catch { /* the server is the thing that just failed; say nothing extra */ }
    return message;
  }

  /**
   * Point the server at a different GPU endpoint.
   *
   * A Colab tunnel gets a new address every session, and following it used to
   * mean stopping the server and starting it again with a new command line.
   * This is the same configuration, set from where you notice it is wrong.
   */
  async setCompute(url, token) {
    const response = await api.post('compute', { url: url.trim(), token: token.trim() });
    const detail = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(detail.error || `server returned ${response.status}`);
    // The runner list changed: `remote` appears or disappears with the endpoint.
    this.emit('runners', detail.runners || []);
    this.emit('design');
    return detail;
  }

  /**
   * Where the on-demand GPU is, if this deployment has one.
   *
   * Resolves to `{configured: false}` rather than throwing when the server is
   * too old to have the route, so the panel can treat "no GPU of its own" and
   * "no such feature" as the same thing: nothing to show.
   */
  async gpuStatus() {
    try {
      const response = await api.get('gpu');
      // A server too old to have the route, or one that has no instance of its
      // own, both mean the same thing: nothing to show. Anything else means we
      // failed to ask, which is not a fact about the GPU -- and treating it as
      // one is what used to stop the panel polling for the rest of the session.
      if (response.status === 404) return { configured: false };
      if (!response.ok) return { configured: false, unreachable: `the server answered ${response.status}` };
      return await response.json();
    } catch {
      return { configured: false, unreachable: 'cannot reach the server' };
    }
  }

  /** Start it, or stop it, without waiting for a job to want it. */
  async gpuAction(what) {
    const response = await api.post(`gpu/${what}`);
    const detail = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(detail.error || `server returned ${response.status}`);
    return detail;
  }

  /* ------------------------------------------------- the shared machine */

  /**
   * The GPU machine, on a deployment where one is shared.
   *
   * Different from `gpuStatus` above in the way that matters: that one asks
   * about an instance this app starts and stops for itself, and this one asks
   * about a machine that does not exist most of the time and belongs to
   * everybody. A copy running on a laptop has the first and not the second.
   */
  async refreshMachine() {
    // Three answers, not two, and the third is the one that used to be
    // mistaken for the first:
    //
    //   absent       this deployment has no shared machine. A laptop, where
    //                there is no such route, or a stack deployed without a
    //                launch template. Nothing to show and nothing to wait
    //                for -- the panel hides and stops asking.
    //   signed out   there is a machine; we are not allowed to see it. A
    //                Cognito id token lasts an hour, so this happens to
    //                anybody who leaves the tab open.
    //   unreachable  a blip, a 5xx, a laptop that slept. We do not know
    //                anything new, which is different from knowing there is
    //                nothing there.
    //
    // Collapsing the last two into `null` is what made one dropped request
    // hide the GPU panel for the rest of the session: the poll stopped, the
    // section stayed hidden, and a download in progress vanished off the
    // screen with no way back but a reload.
    let reach;
    try {
      const response = await api.get('machine');
      if (response.status === 401 || response.status === 403) {
        reach = { ok: false, signedOut: true, message: 'Sign in to use the GPU machine.' };
      } else if (response.status === 404) {
        reach = { ok: true, absent: true };
      } else if (!response.ok) {
        reach = { ok: false, message: `the server answered ${response.status}` };
      } else {
        const machine = await response.json();
        reach = machine.configured === false
          ? { ok: true, absent: true }
          : { ok: true, machine };
      }
    } catch {
      reach = { ok: false, message: 'cannot reach the server' };
    }

    if (reach.absent) this.machine = null;
    else if (reach.machine) this.machine = reach.machine;
    // Unreachable: keep the last thing we knew. Stale and labelled beats gone.
    this.machineReach = reach;
    this.emit('machine', this.machine);
    return reach;
  }

  async startMachine() {
    const response = await api.post('machine/start');
    const detail = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(detail.error || `server returned ${response.status}`);
    this.machine = detail;
    this.emit('machine', this.machine);
    return detail;
  }

  /**
   * Ask the shared machine to finish up and shut itself down.
   *
   * Not a stop button, which this deployment deliberately does not have: the
   * machine is shared, and ending one under somebody else's running design is
   * not a thing any user should be able to do. This asks; the machine decides
   * when, and it waits for work already queued.
   */
  async retireMachine() {
    const response = await api.post('machine/retire');
    const detail = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(detail.error || `server returned ${response.status}`);
    this.machine = detail;
    this.emit('machine', this.machine);
    return detail;
  }

  async downloadModel(name) {
    const response = await api.post(`machine/models/${name}`);
    const detail = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(detail.error || `server returned ${response.status}`);
    this.machine = detail;
    this.emit('machine', this.machine);
    return detail;
  }

  /**
   * What is standing between here and a run, in order.
   *
   * Empty means go. On a copy you are running yourself it is always empty:
   * there is no machine to wait for and no model to fetch, because whatever
   * the endpoint has is what it has.
   *
   * Which protocol needs which weights is the server's answer, not this
   * file's -- it comes from the same table the worker fetches from, so a
   * protocol that grows a dependency does not need this edited.
   */
  runBlockers(kind = 'binder') {
    // Whether we can currently see the machine comes first, and applies even
    // when there is a stale copy of it to hand: a list of weights read five
    // minutes ago is not a reason to say the run is ready to go. Returning []
    // here is what left Run looking pressable after a single failed poll, on a
    // deployment where it certainly was not.
    const reach = this.machineReach;
    if (reach && !reach.ok) {
      return [{ kind: 'unreachable', signedOut: !!reach.signedOut, message: reach.message }];
    }
    const machine = this.machine;
    if (!machine) return [];
    const blockers = [];
    if (machine.state !== 'ready') blockers.push({ kind: 'machine', state: machine.state });

    // Stage two needs the same two models whichever engine drew the backbone.
    // For stage one the protocol names are per engine and some of them collide,
    // so the engine's own map is asked first and the flat one -- all there was
    // when there was one engine -- is the fallback for an older API.
    const perEngine = (machine.engine_needs || {})[this.design.engine];
    const wanted = kind === 'fold'
      ? (machine.fold_needs || [])
      : ((perEngine || machine.needs || {})[this.design.mode] || []);
    const found = new Map((machine.models || []).map((model) => [model.id, model]));
    for (const id of wanted) {
      const model = found.get(id);
      if (!model || model.state !== 'ready') {
        blockers.push({
          kind: 'model', id, label: (model && model.label) || id,
          state: (model && model.state) || 'absent',
        });
      }
    }
    return blockers;
  }

  /**
   * Fetch the protocol and settings catalogue, once.
   *
   * A server too old to have the route is not an error worth shouting about --
   * the panel falls back to the settings it can draw without it and says so --
   * so this resolves to null rather than throwing.
   */
  async loadDesignCatalogue() {
    if (this.designCatalogue) return this.designCatalogue;
    try {
      const response = await api.get('design/options');
      if (!response.ok) return null;
      const catalogue = await response.json();
      if (!Array.isArray(catalogue.options)) return null;
      this.designCatalogue = catalogue;
      this.designEngines = catalogue.engines || [];
      // The engine's own table when the server reports engines, and the flat
      // one otherwise: that is where every build before there was a second
      // engine put RFdiffusion's protocols, and this file still has to work
      // against one of those.
      const engine = this.enginesById(this.design.engine);
      this.designModes = engine.modes || catalogue.modes || [];
      // The starting protocol's own recommendations, applied the same way
      // switching to it would apply them.
      this.setDesignMode(this.design.mode, this.designModes);
      this.emit('catalogue', this.engineCatalogue());
      return catalogue;
    } catch {
      return null;
    }
  }

  async refreshJobs() {
    try {
      const response = await api.get('jobs');
      if (!response.ok) return;
      const payload = await response.json();
      this.jobs = payload.jobs || [];
      // Only a hosted deployment sends this. On a laptop nobody is counting,
      // and the panel shows nothing rather than a limit of infinity.
      if (payload.quota) this.quota = payload.quota;
      this.emit('design');
    } catch { /* server gone; leave what we have */ }
  }

  #pollJobs() {
    if (this.jobTimer) return;
    this.jobTimer = setInterval(async () => {
      await this.refreshJobs();
      const busy = this.jobs.some((j) => j.status === 'queued' || j.status === 'running');
      if (!busy) { clearInterval(this.jobTimer); this.jobTimer = null; }
    }, 1500);
  }

  async cancelJob(id) {
    await api.post(`jobs/${id}/cancel`).catch(() => {});
    await this.refreshJobs();
  }

  /** Pull one finished design into the scene, already positioned on target. */
  async loadDesign(jobId, index = 0) {
    const response = await api.get(`jobs/${jobId}/designs/${index}`);
    if (!response.ok) throw new Error(`design ${index} of job ${jobId} is not available`);

    // Two shapes, one call. A local server sends the coordinates straight
    // back. A hosted one sends a short-lived link to them instead, because the
    // file belongs in a private bucket rather than in a Lambda's response --
    // and because a design can be larger than a Lambda may answer with.
    let name = response.headers.get('X-Design-Name') || `design-${jobId}-${index}`;
    let pdb;
    if ((response.headers.get('Content-Type') || '').includes('json')) {
      const link = await response.json();
      name = link.name || name;
      const file = await fetch(link.url);
      if (!file.ok) {
        throw new Error('that download link has expired — refresh the job list and try again');
      }
      pdb = await file.text();
    } else {
      pdb = await response.text();
    }

    const view = await this.loadText(pdb, `${name}.pdb`);
    view.setColorScheme('uniform', 0x5fd68a);
    return view;
  }

  // ----------------------------------------------- the rotational landscape
  //
  // Two components that share a symmetry axis, one of them turned through a
  // whole revolution. The viewer's job is the same as it is for design: say
  // which parts of what is on screen the question is about, send that and not
  // the whole assembly, and put the answer back where it came from. Here the
  // answer is a curve, and putting it back means being able to drag along it
  // and watch the rotor turn.

  /** Chain ids the current selection touches, per structure. */
  #selectedChains() {
    const found = new Map();
    for (const [view, mask] of this.selection) {
      const ids = new Set();
      for (const chain of view.structure.chains) {
        for (let i = chain.atomStart; i <= chain.atomEnd; i++) {
          if (mask[i]) { ids.add(chain.id); break; }
        }
      }
      if (ids.size) found.set(view, ids);
    }
    return found;
  }

  component(which) {
    return which === 'axle' ? this.axle : this.rotor;
  }

  /** Chain ids assigned to one component, flattened for display. */
  componentChains(which) {
    const names = [];
    for (const [view, ids] of this.component(which)) {
      for (const id of ids) names.push(this.views.length > 1 ? `${view.label}:${id}` : id);
    }
    return names;
  }

  componentCount(which) {
    let total = 0;
    for (const ids of this.component(which).values()) total += ids.size;
    return total;
  }

  /** Make the selected chains the rotor, or the axle. */
  setComponentFromSelection(which) {
    const chosen = this.#selectedChains();
    if (!chosen.size) {
      this.emit('error', `Select the ${which} chains first — alt-click a chain, or click a `
        + 'molecule in the Inspect tab to get every copy of it.');
      return 0;
    }
    return this.#assign(which, chosen);
  }

  /** Make every chain of one molecule the rotor, or the axle. */
  setComponentFromEntity(view, entityIndex, which) {
    const entity = view.structure.entities[entityIndex];
    if (!entity) return 0;
    return this.#assign(which, new Map([[view, new Set(entity.chains)]]));
  }

  #assign(which, chosen) {
    // Back to zero first: the scrubber may have left the rotor turned, and a
    // component assigned while it is would be defined against a pose the next
    // scan is not going to use.
    this.setRotorAngle(0);
    const target = this.component(which);
    const other = this.component(which === 'axle' ? 'rotor' : 'axle');
    target.clear();
    let count = 0;
    for (const [view, ids] of chosen) {
      target.set(view, new Set(ids));
      count += ids.size;
      // Nothing can be both. The assignment just made wins, because it is the
      // one the user is looking at.
      const taken = other.get(view);
      if (taken) {
        for (const id of ids) taken.delete(id);
        if (!taken.size) other.delete(view);
      }
    }
    this.#resetMotion();
    return count;
  }

  clearComponents() {
    this.setRotorAngle(0);
    this.rotor.clear();
    this.axle.clear();
    this.#resetMotion();
  }

  #resetMotion() {
    this.rotorBase = null;
    this.rotorAngle = 0;
    this.landscape = null;
    if (this.scanTimer) { clearInterval(this.scanTimer); this.scanTimer = null; }
    if (this.scanWorker) { this.scanWorker.terminate(); this.scanWorker = null; }
    this.emit('motion');
  }

  /**
   * Guess the two components from the molecules in the scene.
   *
   * A deposited two-component assembly is two distinct entities in many copies,
   * which the structure model already groups -- so the guess is just "the two
   * biggest molecules", and the one with more copies is the rotor, because a
   * ring threaded on an axle is the part with more of itself. It is a guess and
   * says so; the point is that the common case is one button rather than
   * forty alt-clicks.
   */
  guessComponents() {
    const view = this.activeView || this.views[0];
    if (!view) return false;
    const candidates = (view.structure.entities || [])
      .filter((entity) => entity.kind === Kind.PROTEIN || entity.kind === Kind.NUCLEIC)
      .filter((entity) => entity.chains.length)
      .sort((a, b) => b.atoms - a.atoms)
      .slice(0, 2);
    if (candidates.length < 2) return false;

    const [first, second] = candidates[0].chains.length >= candidates[1].chains.length
      ? candidates : [candidates[1], candidates[0]];
    this.setRotorAngle(0);
    this.rotor.clear();
    this.axle.clear();
    this.rotor.set(view, new Set(first.chains));
    this.axle.set(view, new Set(second.chains));
    this.#resetMotion();
    return { rotor: first.name, axle: second.name };
  }

  updateMotion(patch) {
    Object.assign(this.motion, patch);
    this.emit('motion');
  }

  /** Is there enough on screen to ask for a scan? */
  motionBlockers() {
    const reasons = [];
    const chosen = this.scanBackends
      && this.scanBackends.find((b) => b.id === this.motion.backend);
    if (chosen && chosen.where === 'server' && this.scanUnsupported) {
      reasons.push('that backend needs a server this page cannot reach');
    }
    if (!this.componentCount('rotor')) reasons.push('no rotor chains chosen');
    if (!this.componentCount('axle')) reasons.push('no axle chains chosen');
    if (this.componentCount('rotor') + this.componentCount('axle') > 0
        && !this.#motionViews().length) reasons.push('the chains are no longer loaded');
    return reasons;
  }

  #motionViews() {
    const views = [];
    for (const map of [this.rotor, this.axle]) {
      for (const view of map.keys()) {
        if (this.views.includes(view) && !views.includes(view)) views.push(view);
      }
    }
    return views;
  }

  /**
   * Which atoms of one structure belong to the chosen chains.
   *
   * Water is left out. The server drops it anyway -- an interface measured
   * through the crystallographer's water is not the interface -- but a file
   * splits a chain's waters into a run of their own, and the writer then has to
   * give that run a chain id of its own. Sending them turns "rotor: C D E F G"
   * into "rotor: C D E F G I J", where I and J are one water molecule each.
   */
  #maskForChains(view, ids) {
    const structure = view.structure;
    const mask = new Uint8Array(structure.atomCount);
    for (const residue of structure.residues) {
      if (residue.kind === Kind.WATER) continue;
      const chain = structure.chains[residue.chainIndex];
      if (!chain || !ids.has(chain.id)) continue;
      for (let i = residue.start; i <= residue.end; i++) mask[i] = 1;
    }
    return mask;
  }

  /**
   * The scan request: the two components as a PDB, and which exported chain is
   * which.
   *
   * The chain ids have to be the ones the file will actually carry, not the
   * ones in the scene: PDB gives a chain a single column, so `BL` is renamed on
   * the way out, and the rotor would otherwise be named after chains the server
   * cannot find. `chainIdsFor` is exported from the writer for exactly this.
   *
   * Only the rotor and the axle go. A scan of the 219-chain motor is a question
   * about two of its proteins, and sending the other 217 would be 27 MB of
   * coordinates for the server to parse and then ignore.
   */
  buildScanRequest() {
    const views = this.#motionViews();
    const entries = views.map((view) => {
      const ids = new Set([...(this.rotor.get(view) || []), ...(this.axle.get(view) || [])]);
      return {
        structure: view.structure,
        chainMatrices: view.chainMatrices(),
        mask: this.#maskForChains(view, ids),
      };
    });

    const maps = chainIdsFor(entries);
    const rotor = new Set();
    const axle = new Set();
    views.forEach((view, index) => {
      for (const chain of view.structure.chains) {
        const exported = maps[index].get(chain.index);
        if (!exported) continue;
        if ((this.rotor.get(view) || new Set()).has(chain.id)) rotor.add(exported);
        else if ((this.axle.get(view) || new Set()).has(chain.id)) axle.add(exported);
      }
    });

    const name = views.map((view) => view.label).join(' + ').slice(0, 60);
    const request = {
      name,
      pdb: writePDB(entries, { title: `${name} rotor/axle scan` }),
      rotor: [...rotor],
      axle: [...axle],
      step: this.motion.step,
      backend: this.motion.backend,
    };
    const rise = this.#riseRequest();
    if (rise) request.rise = rise;
    return request;
  }

  /** The rise control as the scanners want it, or null for rotation only. */
  #riseRequest() {
    const reach = Number(this.motion.rise) || 0;
    if (reach <= 0) return null;
    return { min: -reach, max: reach, step: Number(this.motion.riseStep) || 1 };
  }

  /**
   * Ask for the scan, and start polling.
   *
   * A scan is named after its own contents, so this is also how a finished one
   * is recovered: an identical request comes back `done` with the curve on it
   * rather than running again. Pressing Scan twice is free, and so is pressing
   * it after a reload.
   */
  async submitScan() {
    this.setRotorAngle(0);
    if (this.motion.backend === 'geometric') return this.#scanHere();
    return this.#scanOnServer();
  }

  /**
   * Run the scan in this browser, in a worker.
   *
   * The components are built and the axis measured here -- a few milliseconds
   * each -- and only typed arrays cross to the worker, so nothing is serialised
   * and no structure is re-parsed. Points come back in batches and the curve
   * draws itself while the rest is still being computed.
   *
   * The result is shaped exactly like the server's, so the plot, the scrubber
   * and the readout cannot tell which one ran. That is deliberate: where it was
   * computed is not a fact about the landscape.
   */
  async #scanHere() {
    const views = this.#motionViews();
    if (!views.length) throw new Error('the chains chosen are no longer loaded');
    if (views.length > 1) {
      throw new Error('a landscape is measured within one structure. Both components have to '
        + 'be chains of the same one — duplicate it into a single file first.');
    }

    const view = views[0];
    const matrices = view.chainMatrices();
    const { rotor, axle } = splitComponents(
      view.structure,
      [...(this.rotor.get(view) || [])],
      [...(this.axle.get(view) || [])],
      matrices
    );
    const axis = detectAxis(rotor, axle);
    const expected = expectedPeriod(axis.rotorFold, axis.axleFold);
    const rise = this.#riseRequest();
    const total = angleList(this.motion.step).length * riseList(rise).length;
    const started = performance.now();

    // Same shape the server reports, including the axis under the names the
    // panel reads. One readout, two possible producers.
    this.landscape = {
      id: 'local',
      status: 'running',
      progress: 0,
      total,
      error: '',
      elapsed: 0,
      where: 'browser',
      request: { name: view.label, rotor: [...(this.rotor.get(view) || [])],
                 axle: [...(this.axle.get(view) || [])], step: this.motion.step,
                 rise, backend: 'geometric' },
      backend: { id: 'geometric', label: GeometricScorer.label, unit: GeometricScorer.unit },
      axis: {
        direction: axis.direction,
        point: axis.point,
        source: axis.source,
        rotor_fold: axis.rotorFold,
        axle_fold: axis.axleFold,
        agreement: axis.agreement,
        spread: axis.spread,
        expected_period: expected,
        rotor_atoms: rotor.count,
        axle_atoms: axle.count,
        total,
      },
      points: [],
      descriptors: { minima: [], complete: false },
    };
    this.rotorBase = null;
    this.emit('motion');

    if (this.scanWorker) this.scanWorker.terminate();
    const worker = new Worker(new URL('./landscape-worker.js', import.meta.url), { type: 'module' });
    this.scanWorker = worker;

    // Redrawing is throttled apart from the scan, not driven by it. The curve
    // arrives faster than a panel can be rebuilt, and announcing every batch
    // puts the whole scan behind a queue of redraws: the arithmetic finishes in
    // half a second and the progress bar takes three to admit it. Ten redraws
    // over a scan look continuous and cost nothing.
    const announce = throttle(() => this.emit('motion'), 100);

    return new Promise((resolve, reject) => {
      worker.onmessage = (event) => {
        const { points, done, error } = event.data;
        if (error) {
          worker.terminate();
          this.scanWorker = null;
          this.landscape.status = 'failed';
          this.landscape.error = error;
          this.emit('motion');
          reject(new Error(error));
          return;
        }
        if (points && points.length) {
          this.landscape.points.push(...points);
          this.landscape.progress = this.landscape.points.length;
        }
        this.landscape.elapsed = Math.round((performance.now() - started) / 100) / 10;
        if (done) {
          worker.terminate();
          this.scanWorker = null;
          this.landscape.status = 'done';
          this.landscape.descriptors = descriptors(this.landscape.points, expected);
          this.emit('motion');
          resolve(this.landscape);
          return;
        }
        announce();
      };
      worker.onerror = (event) => {
        worker.terminate();
        this.scanWorker = null;
        this.landscape.status = 'failed';
        this.landscape.error = event.message || 'the scan worker failed to start';
        this.emit('motion');
        reject(new Error(this.landscape.error));
      };
      worker.postMessage({
        rotor: flatten(rotor),
        axle: flatten(axle),
        axis: { direction: axis.direction, point: axis.point },
        step: this.motion.step,
        rise,
      });
    });
  }

  async #scanOnServer() {
    const request = this.buildScanRequest();
    const response = await api.post('landscape', request);
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      if (response.status === 404) {
        throw new Error('This server does not have the landscape API. It was probably started '
          + 'before this tab existed — restart it: python3 -m proteincad');
      }
      throw new Error(detail.error || `server returned ${response.status}`);
    }
    this.landscape = await response.json();
    this.rotorBase = null;
    this.emit('motion');
    if (this.landscape.status === 'running' || this.landscape.status === 'queued') {
      this.#pollScan();
    } else {
      await this.refreshScan();
    }
    return this.landscape;
  }

  /** Re-read the scan, curve included, and redraw. */
  async refreshScan() {
    const id = this.landscape && this.landscape.id;
    if (!id || id === 'local') return this.landscape;
    try {
      // `points=1` while it runs, so a partial curve is drawn rather than a
      // progress bar: the first wells show up long before the last angle does.
      const response = await api.get(`landscape/${id}?points=1`);
      if (!response.ok) return this.landscape;
      this.landscape = await response.json();
      this.emit('motion');
    } catch { /* server gone; keep the curve we have */ }
    return this.landscape;
  }

  #pollScan() {
    if (this.scanTimer) return;
    this.scanTimer = setInterval(async () => {
      const state = await this.refreshScan();
      if (!state || !['queued', 'running'].includes(state.status)) {
        clearInterval(this.scanTimer);
        this.scanTimer = null;
      }
    }, 1200);
  }

  async cancelScan() {
    if (this.scanWorker) {
      this.scanWorker.terminate();
      this.scanWorker = null;
      if (this.landscape) {
        this.landscape.status = 'cancelled';
        // Whatever it got to is still a curve, and still worth looking at.
        if (this.landscape.points.length >= 4) {
          this.landscape.descriptors = descriptors(
            this.landscape.points, (this.landscape.axis || {}).expected_period || 0);
        }
      }
      this.emit('motion');
      return;
    }
    const id = this.landscape && this.landscape.id;
    if (!id || id === 'local') return;
    await api.post(`landscape/${id}/cancel`, {}).catch(() => {});
    await this.refreshScan();
  }

  /**
   * What this deployment can score a landscape with.
   *
   * A 404 is an answer, not a failure: the hosted front end is a different,
   * smaller API, and a static host has no API at all. Either way the scan
   * cannot run here, and the panel has to say so rather than offer an empty
   * menu and a button that fails when pressed.
   */
  /**
   * What this build can score a landscape with.
   *
   * The geometric backend is always there, because it runs here: it needs
   * nothing but arithmetic, and the coordinates are already in memory. Sending
   * them to a server to be re-parsed and scored would cost an upload, a
   * sign-in and a poll loop to get back a curve this page computes in under a
   * second. PyRosetta is the opposite case -- it cannot run in a browser at all
   * -- so it is offered only when a server answers, which is the line the two
   * halves are split along.
   */
  async loadScanBackends() {
    const local = [{
      id: 'geometric',
      label: GeometricScorer.label,
      unit: GeometricScorer.unit,
      available: true,
      where: 'browser',
      why: '',
    }];
    this.scanBackends = local;
    this.scanUnsupported = '';
    this.emit('motion');

    const server = await this.#serverBackends();
    // Anything the server has that this page cannot do itself. Its geometric
    // backend is the same arithmetic and slower to reach, so it is left out
    // rather than offered as a second, identical choice.
    const extra = (server || []).filter((backend) => backend.id !== 'geometric')
      .map((backend) => ({ ...backend, where: 'server' }));
    this.scanBackends = [...local, ...extra];
    this.emit('motion');
    return this.scanBackends;
  }

  async #serverBackends() {
    await api.ready;
    let payload = null;
    try {
      const response = await api.get('landscape/options');
      if (response.ok) payload = await response.json();
    } catch {
      // A deployment without the route does not answer with a tidy 404. API
      // Gateway rejects an unknown path before any CORS header is attached, so
      // the browser reports it as a failed fetch and never sees the status --
      // which is why anything other than a usable answer is treated the same
      // way below. An empty menu with no explanation is the one outcome that
      // leaves somebody staring at a Scan button wondering what is wrong.
    }
    return payload && payload.backends ? payload.backends : null;
  }

  /**
   * Why this deployment cannot scan, in terms of what to do about it.
   *
   * Three different situations produce the same silence from the API, and the
   * advice for each is different. The one worth separating out is the middle
   * one: a page served from your own machine that is nonetheless pointed at a
   * deployed API, because config.json is sitting in the checkout. Telling
   * somebody in that position to "run the Python server" sends them to start a
   * server they already have running.
   */
  #noScansBecause() {
    if (api.servedLocally() && api.hosted()) {
      return 'This page is served from your own machine, but web/config.json points it at '
        + 'a deployed API that has no landscape routes — so the server you are running is '
        + 'never asked. Add ?local=1 to the address to ignore that file, or move '
        + 'web/config.json aside.';
    }
    if (api.hosted()) {
      return 'The hosted deployment does not run rotational scans — they need the Python '
        + 'server. Run proteinCAD on your own machine (python3 -m proteincad) and open '
        + 'it with ?local=1 to scan a landscape.';
    }
    return 'This server does not have the landscape API. It was probably started before '
      + 'this tab existed — stop it and run python3 -m proteincad again.';
  }

  /** The axis the rotor turns about, as the scan measured it, or null. */
  motionAxis() {
    const axis = this.landscape && this.landscape.axis;
    return axis && axis.direction ? axis : null;
  }

  #rotorUnits() {
    const units = [];
    for (const [view, ids] of this.rotor) {
      if (!this.views.includes(view)) continue;
      for (const id of ids) {
        const unit = view.units.get(id);
        if (unit) units.push(unit.group);
      }
    }
    return units;
  }

  /**
   * Turn the rotor to one angle on the curve.
   *
   * This is the whole reason the landscape lives in a 3D editor rather than in
   * a notebook: the plot cursor and the assembly are the same control, so a
   * well at 140 degrees is a shape you can look at rather than a number.
   *
   * The rotation is about the measured axis in world space, which is not any
   * node's local frame -- so each movable chain gets its base pose carried
   * through its parent and back. The bases are snapshotted at zero and never
   * written over, so dragging the scrubber is non-destructive: it is always the
   * same rotation applied to the pose the scan was computed from, never one
   * rotation composed onto the last.
   */
  setRotorAngle(degrees) {
    const units = this.#rotorUnits();
    if (!units.length) { this.rotorAngle = 0; return; }

    if (!this.rotorBase) {
      this.rotorBase = units.map((node) => {
        node.updateMatrix();
        return { node, local: node.matrix.clone() };
      });
      // Nothing is applied yet, so the pose on screen already *is* zero.
      if (!degrees) { this.rotorAngle = 0; return; }
    }

    const axis = this.motionAxis();
    if (!axis) { this.rotorAngle = 0; return; }

    const direction = new THREE.Vector3().fromArray(axis.direction).normalize();
    const point = new THREE.Vector3().fromArray(axis.point);
    const turn = new THREE.Matrix4()
      .makeTranslation(point.x, point.y, point.z)
      .multiply(new THREE.Matrix4().makeRotationAxis(direction, (degrees * Math.PI) / 180))
      .multiply(new THREE.Matrix4().makeTranslation(-point.x, -point.y, -point.z));

    const parentWorld = new THREE.Matrix4();
    const inverse = new THREE.Matrix4();
    const next = new THREE.Matrix4();
    for (const { node, local } of this.rotorBase) {
      if (!node.parent) continue;
      node.parent.updateMatrixWorld(true);
      parentWorld.copy(node.parent.matrixWorld);
      inverse.copy(parentWorld).invert();
      next.copy(inverse).multiply(turn).multiply(parentWorld).multiply(local);
      next.decompose(node.position, node.quaternion, node.scale);
      node.updateMatrixWorld(true);
    }

    this.rotorAngle = degrees;
    this.markers.refresh();
    this.viewer.requestRender();
    this.emit('motion-angle', degrees);
  }

  // ---------------------------------------------------------------- output

  exportPDB() {
    if (!this.views.length) return;
    const entries = this.views.map((view) => ({
      structure: view.structure,
      chainMatrices: view.chainMatrices(),
    }));
    const title = this.views.map((v) => v.structure.name).join(' + ').slice(0, 60);
    downloadText('proteincad-scene.pdb', writePDB(entries, { title }));
  }

  snapshot() {
    const data = this.viewer.screenshot(2);
    const a = document.createElement('a');
    a.href = data;
    a.download = 'proteincad.png';
    a.click();
  }

  /** Everything a backend would need to know about the current scene. */
  session() {
    return {
      structures: this.views.map((view) => ({
        ...view.structure.summary(),
        representation: view.representation,
        colorScheme: view.colorScheme,
        visible: view.visible,
        transform: Array.from(view.group.matrixWorld.elements),
        chainTransforms: view.chainMatrices(),
      })),
      selection: [...this.selection].map(([view, mask]) => ({
        structure: view.structure.name,
        ...describeSelection(view.structure, mask),
        firstResidue: undefined,
      })),
      camera: {
        position: this.viewer.camera.position.toArray(),
        target: this.viewer.controls.target.toArray(),
      },
    };
  }

  // ------------------------------------------------------------ interaction

  #bindPointer() {
    const canvas = this.viewer.renderer.domElement;
    let down = null;

    canvas.addEventListener('pointerdown', (event) => {
      down = { x: event.clientX, y: event.clientY, time: performance.now(), button: event.button };
    });

    canvas.addEventListener('pointerup', (event) => {
      if (!down || event.button !== 0 || this.gizmo.dragging) { down = null; return; }
      const moved = Math.hypot(event.clientX - down.x, event.clientY - down.y);
      const elapsed = performance.now() - down.time;
      down = null;
      if (moved > 5 || elapsed > 600) return;
      this.#handleClick(event);
    });

    let hoverPending = false;
    const hover = throttle(async (event) => {
      if (!this.settings.hoverPick || this.gizmo.dragging || down || hoverPending) return;
      // Never pick while the camera is moving: the pick pass fences on the GPU
      // and has to wait for every queued frame, which is what made zooming and
      // orbiting stutter on large structures.
      if (this.viewer.cameraBusy()) { this.markers.setHover(null); return; }
      hoverPending = true;
      try {
        this.#setHover(await this.viewer.pickAsync(event.clientX, event.clientY));
      } finally {
        hoverPending = false;
      }
    }, 70);
    canvas.addEventListener('pointermove', hover);
    canvas.addEventListener('pointerleave', () => this.#setHover(null));

    canvas.addEventListener('dblclick', (event) => {
      const hit = this.viewer.pick(event.clientX, event.clientY);
      if (!hit) return this.frameAll();
      const view = this.slots.get(hit.slot);
      if (!view) return;
      const chainIndex = view.structure.residueOfAtom(hit.atomIndex).chainIndex;
      const box = view.box(chainIndex).expandByScalar(2);
      return this.viewer.frame(box, true);
    });
  }

  #setHover(hit) {
    const view = hit ? this.slots.get(hit.slot) : null;
    if (!view) {
      if (this.hovered) { this.hovered = null; this.markers.setHover(null); this.emit('hover', null); }
      return;
    }
    const same = this.hovered && this.hovered.view === view && this.hovered.atomIndex === hit.atomIndex;
    this.hovered = { view, atomIndex: hit.atomIndex };
    if (!same) {
      const position = view.atomWorldPosition(hit.atomIndex);
      this.markers.setHover(position, Math.max(0.5, view.structure.atomRadius(hit.atomIndex) * 0.75));
      this.emit('hover', this.hovered);
    }
  }

  #handleClick(event) {
    const hit = this.viewer.pick(event.clientX, event.clientY);
    if (!hit) {
      if (!event.shiftKey) this.clearSelection();
      if (this.mode === 'measure') this.markers.cancelPending();
      return;
    }
    const view = this.slots.get(hit.slot);
    if (!view) return;
    this.setActiveView(view);

    if (this.mode === 'measure') {
      this.markers.addPoint(view, hit.atomIndex);
      return;
    }

    if (this.mode === 'hotspot') {
      this.toggleHotspot(view, view.structure.residueOfAtom(hit.atomIndex).index);
      return;
    }

    const structure = view.structure;
    const residue = structure.residueOfAtom(hit.atomIndex);
    let mask;
    if (event.altKey) {
      mask = maskForChain(structure, residue.chainIndex);
    } else if (event.metaKey || event.ctrlKey) {
      mask = new Uint8Array(structure.atomCount);
      mask[hit.atomIndex] = 1;
    } else {
      mask = maskForResidue(structure, residue);
    }
    this.selectAtoms(view, mask, event.shiftKey ? 'toggle' : 'replace');
  }

  /** Text for the status bar. */
  hoverLabel() {
    if (!this.hovered) return '';
    const { view, atomIndex } = this.hovered;
    const s = view.structure;
    const residue = s.residueOfAtom(atomIndex);
    const bits = [
      s.name,
      s.atomLabel(atomIndex),
      `${s.x[atomIndex].toFixed(1)} ${s.y[atomIndex].toFixed(1)} ${s.z[atomIndex].toFixed(1)}`,
    ];
    if (residue.kind === Kind.PROTEIN || residue.kind === Kind.NUCLEIC) {
      bits.push(`B=${s.bFactor[atomIndex].toFixed(1)}`);
    }
    return bits.join('   ·   ');
  }

  counts() {
    let atoms = 0, residues = 0, chains = 0;
    for (const view of this.views) {
      atoms += view.structure.atomCount;
      residues += view.structure.residueCount;
      chains += view.structure.chainCount;
    }
    return { structures: this.views.length, maps: this.maps.length, atoms, residues, chains };
  }
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error('could not be read'));
    reader.readAsText(file);
  });
}

/** "9IJM" -> "9IJM (2)", avoiding labels already in use. */
function nextCopyLabel(views, view) {
  const base = view.label.replace(/\s*\(\d+\)$/, '').replace(/\s+\d+\/\d+$/, '');
  const used = new Set(views.map((v) => v.label));
  for (let n = 2; n < 1000; n++) {
    const candidate = `${base} (${n})`;
    if (!used.has(candidate)) return candidate;
  }
  return base;
}

function isDescendant(node, ancestor) {
  let current = node;
  while (current) {
    if (current === ancestor) return true;
    current = current.parent;
  }
  return false;
}

/** A Component as the plain typed arrays a worker can be handed. */
function flatten(component) {
  return {
    name: component.name,
    chains: component.chains,
    x: component.x,
    y: component.y,
    z: component.z,
    radii: component.radii,
  };
}
