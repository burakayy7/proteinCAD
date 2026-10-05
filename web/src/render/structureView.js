// One loaded structure in the scene.
//
// The object graph exists to make "pick up a subunit and move it" work the way
// a CAD user expects -- the handle sits at the part's own centre, rotation
// happens about that centre rather than the file's arbitrary origin, and a
// subunit takes its cofactors with it:
//
//   group           structure node, at the structure centre       (movable)
//     pivot         cancels that offset
//       unit        everything sharing a chain id, at its centre  (movable)
//         unitPivot cancels that offset
//           chain   one run of residues, hideable
//             mesh  geometry in the file's own coordinates
//
// A file lists a polymer and then its ligands and waters under the same chain
// id, which the model keeps as separate runs; grouping the runs into a unit is
// what makes "move chain B" carry chain B's haem along with it.
//
// Hiding is a flag, moving writes a transform, exporting reads
// chain.matrixWorld. No geometry is rebuilt for any of it.

import * as THREE from 'three';
import { Kind } from '../core/structure.js';
import { buildCartoon, autoQuality } from './cartoon.js';
import { buildSpheres, buildSticks } from './atoms.js';
import { buildSurface } from './surface.js';
import { computeAtomColors, chainColor } from './colors.js';
import {
  addPickAttribute, applyInstanceColors, applyVertexColors, createMaterial, disposeObject,
} from './geometry.js';

export const REPRESENTATIONS = [
  { id: 'cartoon', label: 'Cartoon' },
  { id: 'trace', label: 'Backbone trace' },
  { id: 'ballstick', label: 'Ball & stick' },
  { id: 'sticks', label: 'Sticks' },
  { id: 'spacefill', label: 'Spacefill' },
  { id: 'surface', label: 'Surface' },
];

const HET_KINDS = new Set([Kind.LIGAND, Kind.ION]);

// Sugar and phosphate atoms; whatever is left in a nucleotide is the base.
// C1' stays out so the base stays visually attached to the backbone.
const NUCLEIC_BACKBONE = new Set([
  'P', 'OP1', 'OP2', 'O1P', 'O2P', "O5'", "C5'", "C4'", "O4'", "C3'", "O3'", "C2'", "O2'",
  'O5*', 'C5*', 'C4*', 'O4*', 'C3*', 'O3*', 'C2*', 'O2*',
]);

export class StructureView {
  /**
   * @param {Structure} structure
   * @param {number} slot 1..255, this structure's picking id
   * @param {number} index position in the document, used for palette offsets
   */
  constructor(structure, slot, index = 0) {
    this.structure = structure;
    this.slot = slot;
    this.index = index;

    // Display name: several views can share one parsed structure (duplicates),
    // so the label is per view rather than taken from the file every time.
    this.label = structure.name;

    this.group = new THREE.Group();
    this.group.name = structure.name;
    this.group.userData.view = this;
    this.origin = new THREE.Vector3(...structure.center);
    this.group.position.copy(this.origin);

    this.pivot = new THREE.Group();
    this.pivot.position.copy(this.origin).negate();
    this.group.add(this.pivot);

    // One movable unit per chain id, holding every run of residues with that id.
    this.units = new Map();
    this.chainGroups = [];
    for (const chain of structure.chains) {
      let unit = this.units.get(chain.id);
      if (!unit) {
        const centre = unitCentre(structure, chain.id);
        const node = new THREE.Group();
        node.name = `${structure.name}/${chain.id}`;
        node.position.copy(centre);
        node.userData.view = this;
        node.userData.chainId = chain.id;
        this.pivot.add(node);

        const inner = new THREE.Group();
        inner.position.copy(centre).negate();
        node.add(inner);

        unit = { id: chain.id, group: node, pivot: inner, origin: centre, chains: [] };
        this.units.set(chain.id, unit);
      }
      unit.chains.push(chain.index);

      const run = new THREE.Group();
      run.name = `${structure.name}/${chain.id}#${chain.index}`;
      run.userData.chainIndex = chain.index;
      run.userData.view = this;
      unit.pivot.add(run);
      this.chainGroups.push(run);
    }

    this.representation = 'cartoon';
    this.colorScheme = 'chain';
    this.uniformColor = 0x9fb4cc;
    this.chainColorOverrides = new Map();
    this.showWater = false;
    this.showHydrogens = false;
    this.quality = 'auto';
    this.visible = true;
    this.selection = null;

    this.material = createMaterial({ vertexColors: true });
    this.instanceMaterial = createMaterial({ vertexColors: false });
    this.surfaceMaterial = createMaterial({ vertexColors: true, roughness: 0.75 });

    this.atomColors = this.#computeColors();
  }

  get name() { return this.label; }

  #computeColors() {
    return computeAtomColors(this.structure, this.colorScheme, {
      structureIndex: this.index,
      uniformColor: this.uniformColor,
      chainOverrides: this.chainColorOverrides,
    });
  }

  setVisible(visible) {
    this.visible = visible;
    this.group.visible = visible;
  }

  setChainVisible(chainIndex, visible) {
    const node = this.chainGroups[chainIndex];
    if (node) node.visible = visible;
  }

  isChainVisible(chainIndex) {
    const node = this.chainGroups[chainIndex];
    return node ? node.visible : false;
  }

  setRepresentation(id) {
    this.representation = id;
    this.build();
  }

  setColorScheme(id, uniformColor) {
    this.colorScheme = id;
    if (uniformColor !== undefined) this.uniformColor = uniformColor;
    this.refreshColors();
  }

  setChainColor(chainIndex, hex) {
    if (hex === null) this.chainColorOverrides.delete(chainIndex);
    else this.chainColorOverrides.set(chainIndex, hex);
    this.refreshColors();
  }

  chainColorHex(chainIndex) {
    if (this.chainColorOverrides.has(chainIndex)) return this.chainColorOverrides.get(chainIndex);
    return chainColor(this.structure, chainIndex, this.index);
  }

  setSelection(mask) {
    this.selection = mask;
    this.applyColors();
  }

  setShowWater(show) { this.showWater = show; this.build(); }
  setShowHydrogens(show) { this.showHydrogens = show; this.build(); }

  refreshColors() {
    this.atomColors = this.#computeColors();
    this.applyColors();
  }

  /** Repaint every mesh from the atom colour table plus the selection tint. */
  applyColors() {
    for (const pivot of this.chainGroups) {
      for (const mesh of pivot.children) {
        if (mesh.isInstancedMesh) applyInstanceColors(mesh, this.atomColors, this.selection);
        else applyVertexColors(mesh.geometry, this.atomColors, this.selection);
      }
    }
  }

  /** Rebuild geometry for every chain. */
  build() {
    for (const pivot of this.chainGroups) {
      for (const child of [...pivot.children]) {
        disposeObject(child);
        pivot.remove(child);
      }
    }
    for (const chain of this.structure.chains) this.#buildChain(chain);
    this.applyColors();
  }

  /** Surface voxel size in Angstrom, coarsened for large structures. */
  surfaceResolution() {
    const atoms = this.structure.atomCount;
    if (atoms <= 20000) return 0.8;
    if (atoms <= 100000) return 1.2;
    if (atoms <= 400000) return 2.0;
    return 2.6;
  }

  #atomsOfChain(chain, filter) {
    const s = this.structure;
    const out = [];
    for (let i = chain.atomStart; i <= chain.atomEnd; i++) {
      if (!this.showHydrogens && s.element[i] === 1) continue;
      const kind = s.residueOfAtom(i).kind;
      if (kind === Kind.WATER && !this.showWater) continue;
      if (filter && !filter(kind, i)) continue;
      out.push(i);
    }
    return Int32Array.from(out);
  }

  #add(chain, mesh, kind) {
    if (!mesh) return;
    mesh.userData.kind = kind;
    mesh.userData.chainIndex = chain.index;
    mesh.userData.view = this;
    addPickAttribute(mesh.geometry, this.slot, !!mesh.isInstancedMesh);
    this.chainGroups[chain.index].add(mesh);
  }

  #buildChain(chain) {
    const s = this.structure;
    const rep = this.representation;
    const quality = this.quality === 'auto' ? autoQuality(s.residueCount) : this.quality;

    if (rep === 'cartoon' || rep === 'trace') {
      const geometry = buildCartoon(s, null, {
        chainIndex: chain.index,
        style: rep === 'trace' ? 'tube' : 'cartoon',
        quality,
        tubeRadius: 0.4,
      });
      if (geometry) this.#add(chain, new THREE.Mesh(geometry, this.material), 'cartoon');

      // Ligands, ions and (optionally) waters are always drawn as atoms: a
      // cartoon on its own would hide the chemistry people care about.
      const het = this.#atomsOfChain(chain, (kind) => HET_KINDS.has(kind) || kind === Kind.WATER);
      if (het.length) {
        this.#add(chain, buildSpheres(s, het, this.instanceMaterial, { fixedRadius: 0.30 }), 'spheres');
        this.#add(chain, buildSticks(s, this.#maskFor(het), this.instanceMaterial, {
          radius: 0.15, chainIndex: chain.index,
        }), 'sticks');
      }

      // Nucleic acids get their bases drawn: a bare backbone tube says nothing
      // about the sequence or about which strand pairs with which.
      if (chain.kind === Kind.NUCLEIC && rep === 'cartoon') {
        const bases = this.#atomsOfChain(chain, (kind, i) =>
          kind === Kind.NUCLEIC && !NUCLEIC_BACKBONE.has(s.atomName[i]));
        if (bases.length) {
          this.#add(chain, buildSticks(s, this.#maskFor(bases), this.instanceMaterial, {
            radius: 0.18, chainIndex: chain.index,
          }), 'sticks');
        }
      }
      return;
    }

    if (rep === 'surface') {
      const atoms = this.#atomsOfChain(chain, (kind) => kind !== Kind.WATER);
      // Surfaces are built per chain so each keeps its own colour and can be
      // moved; the grid has to be sized against the whole structure, or a
      // 200-chain assembly would build 200 fine grids and take ten seconds.
      const geometry = buildSurface(s, atoms, { resolution: this.surfaceResolution() });
      if (geometry) this.#add(chain, new THREE.Mesh(geometry, this.surfaceMaterial), 'surface');
      return;
    }

    const atoms = this.#atomsOfChain(chain, null);
    if (!atoms.length) return;

    if (rep === 'spacefill') {
      this.#add(chain, buildSpheres(s, atoms, this.instanceMaterial, { radiusScale: 1 }), 'spheres');
      return;
    }

    if (rep === 'ballstick') {
      this.#add(chain, buildSpheres(s, atoms, this.instanceMaterial, { fixedRadius: 0.32 }), 'spheres');
      this.#add(chain, buildSticks(s, this.#maskFor(atoms), this.instanceMaterial, {
        radius: 0.15, chainIndex: chain.index,
      }), 'sticks');
      return;
    }

    if (rep === 'sticks') {
      this.#add(chain, buildSticks(s, this.#maskFor(atoms), this.instanceMaterial, {
        radius: 0.20, chainIndex: chain.index,
      }), 'sticks');
      // Ions and other unbonded atoms would vanish in a pure stick view.
      const lone = [];
      for (const i of atoms) {
        const res = s.residueOfAtom(i);
        if (res.start === res.end) lone.push(i);
      }
      if (lone.length) {
        this.#add(chain, buildSpheres(s, Int32Array.from(lone), this.instanceMaterial, { fixedRadius: 0.32 }), 'spheres');
      }
    }
  }

  #maskFor(atoms) {
    const mask = new Uint8Array(this.structure.atomCount);
    for (let k = 0; k < atoms.length; k++) mask[atoms[k]] = 1;
    return mask;
  }

  /** World-space box of the whole structure or of one chain. */
  box(chainIndex = null) {
    const s = this.structure;
    const box = new THREE.Box3();
    const point = new THREE.Vector3();
    if (chainIndex === null) {
      for (const chain of s.chains) box.union(this.box(chain.index));
      return box;
    }
    const chain = s.chains[chainIndex];
    const matrix = this.chainGroups[chainIndex].matrixWorld;
    for (let i = chain.atomStart; i <= chain.atomEnd; i++) {
      point.set(s.x[i], s.y[i], s.z[i]).applyMatrix4(matrix);
      box.expandByPoint(point);
    }
    return box;
  }

  /** Box around an arbitrary atom mask, used to frame a selection. */
  boxOfMask(mask) {
    const s = this.structure;
    const box = new THREE.Box3();
    const point = new THREE.Vector3();
    for (let i = 0; i < s.atomCount; i++) {
      if (!mask[i]) continue;
      const chainIndex = s.residueOfAtom(i).chainIndex;
      point.set(s.x[i], s.y[i], s.z[i]).applyMatrix4(this.chainGroups[chainIndex].matrixWorld);
      box.expandByPoint(point);
    }
    return box;
  }

  /** Atom position in world space, following whatever transforms are applied. */
  atomWorldPosition(atomIndex, out = new THREE.Vector3()) {
    const s = this.structure;
    const chainIndex = s.residueOfAtom(atomIndex).chainIndex;
    out.set(s.x[atomIndex], s.y[atomIndex], s.z[atomIndex]);
    return out.applyMatrix4(this.chainGroups[chainIndex].matrixWorld);
  }

  /** The movable node for a chain: everything sharing that chain's id. */
  unitForChain(chainIndex) {
    const chain = this.structure.chains[chainIndex];
    const unit = chain && this.units.get(chain.id);
    return unit ? unit.group : null;
  }

  /** Per-chain world matrices, for export. */
  chainMatrices() {
    this.group.updateMatrixWorld(true);
    return this.chainGroups.map((p) => Array.from(p.matrixWorld.elements));
  }

  /** True when anything has been moved away from where the file put it. */
  hasTransform() {
    const moved = (node, origin) =>
      node.position.distanceToSquared(origin) > 1e-8 ||
      Math.abs(node.quaternion.w) < 1 - 1e-8;
    if (moved(this.group, this.origin)) return true;
    for (const unit of this.units.values()) if (moved(unit.group, unit.origin)) return true;
    return false;
  }

  resetTransforms() {
    this.group.position.copy(this.origin);
    this.group.quaternion.identity();
    this.group.scale.set(1, 1, 1);
    for (const unit of this.units.values()) {
      unit.group.position.copy(unit.origin);
      unit.group.quaternion.identity();
      unit.group.scale.set(1, 1, 1);
    }
    this.group.updateMatrixWorld(true);
  }

  dispose() {
    disposeObject(this.group);
    this.material.dispose();
    this.instanceMaterial.dispose();
    this.surfaceMaterial.dispose();
    this.group.removeFromParent();
  }
}

/** Centroid of every atom carrying a chain id, across all of its runs. */
function unitCentre(structure, chainId) {
  let x = 0, y = 0, z = 0, n = 0;
  for (const chain of structure.chains) {
    if (chain.id !== chainId) continue;
    for (let i = chain.atomStart; i <= chain.atomEnd; i++) {
      x += structure.x[i]; y += structure.y[i]; z += structure.z[i]; n++;
    }
  }
  return n > 0 ? new THREE.Vector3(x / n, y / n, z / n) : new THREE.Vector3();
}
