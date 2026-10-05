// The in-memory structure model.
//
// Layout: atoms live in flat typed arrays (there can be millions of them),
// while residues and chains are plain objects (there are far fewer, and the
// code that walks them is much easier to read this way).
//
//   Structure
//     .atomCount, .x/.y/.z, .element, .atomName[], .bFactor, ...
//     .residues[] -> { name, seq, chainIndex, start, end, kind, ss, ca, ... }
//     .chains[]   -> { id, residueStart, residueEnd, kind }
//
// This module deliberately has no three.js import: it is the part of the app
// that a Python backend will mirror, and it must stay runnable in plain node.

import { elementIndex, guessElement, ELEMENT_H, VDW_RADII, COVALENT_RADII } from './elements.js';

/** Residue classification. */
export const Kind = {
  UNKNOWN: 0,
  PROTEIN: 1,
  NUCLEIC: 2,
  WATER: 3,
  ION: 4,
  LIGAND: 5,
};

export const KIND_NAMES = ['unknown', 'protein', 'nucleic', 'water', 'ion', 'ligand'];

/** Secondary structure codes. */
export const SS = { COIL: 0, HELIX: 1, SHEET: 2, TURN: 3 };

const AMINO = new Set(
  ('ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL ' +
   'MSE SEC PYL ASX GLX UNK HYP CSO PTR SEP TPO KCX LLP MLY CME CSD OCS SAC ACE NME')
    .split(' ')
);

const NUCLEIC = new Set(
  ('A C G U I DA DC DG DT DU DI +A +C +G +U RA RC RG RU 1MA 5MC OMC 5MU 7MG G7M PSU 2MG H2U')
    .split(' ')
);

const WATER = new Set(['HOH', 'DOD', 'WAT', 'H2O', 'TIP', 'TIP3', 'SOL']);

// Single-atom groups that are almost always counter-ions rather than ligands.
const IONS = new Set(
  ('NA K MG CA ZN FE FE2 FE3 MN CU CU1 CO NI CD HG CL BR IOD F IO3 SO4 PO4 CS RB SR BA AU AG PT LI AL')
    .split(' ')
);

const AA3_TO_1 = {
  ALA: 'A', ARG: 'R', ASN: 'N', ASP: 'D', CYS: 'C', GLN: 'Q', GLU: 'E', GLY: 'G',
  HIS: 'H', ILE: 'I', LEU: 'L', LYS: 'K', MET: 'M', PHE: 'F', PRO: 'P', SER: 'S',
  THR: 'T', TRP: 'W', TYR: 'Y', VAL: 'V', MSE: 'M', SEC: 'U', PYL: 'O', HYP: 'P',
};

export function residueLetter(name) {
  if (AA3_TO_1[name]) return AA3_TO_1[name];
  if (NUCLEIC.has(name)) return name.length > 1 ? name[name.length - 1] : name;
  return 'X';
}

let nextStructureId = 1;

export class Structure {
  constructor(name = 'structure') {
    this.id = nextStructureId++;
    this.name = name;
    this.title = '';
    this.format = '';
    this.modelCount = 1;

    this.atomCount = 0;
    this.x = new Float32Array(0);
    this.y = new Float32Array(0);
    this.z = new Float32Array(0);
    this.element = new Uint8Array(0);
    this.bFactor = new Float32Array(0);
    this.occupancy = new Float32Array(0);
    this.serial = new Int32Array(0);
    this.residueIndex = new Uint32Array(0);
    this.atomName = [];
    this.altLoc = [];

    this.residues = [];
    this.chains = [];
    this.entities = []; // the distinct molecules, as opposed to their copies
    this.bonds = null; // { a: Int32Array, b: Int32Array } -- built on demand

    this.center = [0, 0, 0];
    this.radius = 0;
    this.min = [0, 0, 0];
    this.max = [0, 0, 0];
  }

  get residueCount() { return this.residues.length; }
  get chainCount() { return this.chains.length; }

  residueOfAtom(i) { return this.residues[this.residueIndex[i]]; }
  chainOfAtom(i) { return this.chains[this.residueOfAtom(i).chainIndex]; }
  chainOfResidue(res) { return this.chains[res.chainIndex]; }

  /** Index of the first atom named `name` in a residue, or -1. */
  atomInResidue(res, name) {
    for (let i = res.start; i <= res.end; i++) {
      if (this.atomName[i] === name) return i;
    }
    return -1;
  }

  atomPosition(i, out = [0, 0, 0]) {
    out[0] = this.x[i]; out[1] = this.y[i]; out[2] = this.z[i];
    return out;
  }

  atomRadius(i) { return VDW_RADII[this.element[i]]; }

  /** Human-readable label, e.g. "A / LYS 42 / CA". */
  atomLabel(i) {
    const res = this.residueOfAtom(i);
    const chain = this.chains[res.chainIndex];
    return `${chain.id} / ${res.name} ${res.seq}${res.insCode.trim()} / ${this.atomName[i]}`;
  }

  residueLabel(res) {
    const chain = this.chains[res.chainIndex];
    return `${chain.id} / ${res.name} ${res.seq}${res.insCode.trim()}`;
  }

  /** Serialisable summary -- this is what gets handed to the Python backend. */
  summary() {
    return {
      id: this.id,
      name: this.name,
      title: this.title,
      format: this.format,
      atoms: this.atomCount,
      residues: this.residues.length,
      models: this.modelCount,
      entities: this.entities.map((e) => ({
        name: e.name,
        kind: KIND_NAMES[e.kind],
        copies: e.chains.length,
        residues: e.residues,
        chains: e.chains,
      })),
      chains: this.chains.map((c) => ({
        id: c.id,
        kind: KIND_NAMES[c.kind],
        residues: c.residueEnd - c.residueStart + 1,
        sequence: c.kind === Kind.PROTEIN || c.kind === Kind.NUCLEIC ? this.chainSequence(c) : undefined,
      })),
    };
  }

  chainSequence(chain) {
    let seq = '';
    for (let r = chain.residueStart; r <= chain.residueEnd; r++) {
      const res = this.residues[r];
      if (res.kind === Kind.PROTEIN || res.kind === Kind.NUCLEIC) seq += residueLetter(res.name);
    }
    return seq;
  }

  computeBounds() {
    if (this.atomCount === 0) {
      this.center = [0, 0, 0]; this.radius = 0;
      this.min = [0, 0, 0]; this.max = [0, 0, 0];
      return;
    }
    let minX = Infinity, minY = Infinity, minZ = Infinity;
    let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    for (let i = 0; i < this.atomCount; i++) {
      const px = this.x[i], py = this.y[i], pz = this.z[i];
      if (px < minX) minX = px; if (px > maxX) maxX = px;
      if (py < minY) minY = py; if (py > maxY) maxY = py;
      if (pz < minZ) minZ = pz; if (pz > maxZ) maxZ = pz;
    }
    this.min = [minX, minY, minZ];
    this.max = [maxX, maxY, maxZ];
    this.center = [(minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2];
    let r2 = 0;
    for (let i = 0; i < this.atomCount; i++) {
      const dx = this.x[i] - this.center[0];
      const dy = this.y[i] - this.center[1];
      const dz = this.z[i] - this.center[2];
      const d2 = dx * dx + dy * dy + dz * dz;
      if (d2 > r2) r2 = d2;
    }
    this.radius = Math.sqrt(r2);
  }

  /** Counts used by the UI: { protein, nucleic, water, ion, ligand, hydrogens }. */
  stats() {
    const out = { protein: 0, nucleic: 0, water: 0, ion: 0, ligand: 0, hydrogens: 0 };
    for (const res of this.residues) {
      const key = KIND_NAMES[res.kind];
      if (key in out) out[key]++;
    }
    for (let i = 0; i < this.atomCount; i++) if (this.element[i] === ELEMENT_H) out.hydrogens++;
    return out;
  }
}

/**
 * Incremental structure assembly. Parsers push atoms in file order; `finish()`
 * groups them into residues and chains and derives everything else.
 */
export class StructureBuilder {
  constructor(name = 'structure') {
    this.structure = new Structure(name);
    this.x = []; this.y = []; this.z = [];
    this.element = []; this.bFactor = []; this.occupancy = [];
    this.serial = []; this.atomName = []; this.altLoc = [];
    this.residueIndex = [];
    this.residues = [];
    this.chainIds = [];
    this.names = new Map(); // string interning
    this.ssRanges = []; // { chainId, startSeq, endSeq, type }
    this.entityNames = new Map(); // mmCIF entity id -> description
    this.compounds = []; // PDB COMPND: { molecule, chains: [id] }
    this.explicitBonds = [];
    this._prevKey = null;
    this._chainOfResidue = [];
  }

  intern(str) {
    let v = this.names.get(str);
    if (v === undefined) { v = str; this.names.set(str, str); }
    return v;
  }

  /**
   * Add one atom. `element` may be a symbol string or omitted (then guessed).
   * Atoms must arrive grouped by residue, which is true for every real file.
   */
  addAtom(a) {
    const chainId = a.chainId && a.chainId.trim() ? a.chainId.trim() : 'A';
    const resName = this.intern((a.resName || 'UNK').trim().toUpperCase());
    const insCode = a.insCode || ' ';
    const key = `${chainId}|${a.resSeq}|${insCode}|${resName}`;
    if (key !== this._prevKey) {
      this._prevKey = key;
      this.residues.push({
        name: resName,
        seq: a.resSeq | 0,
        insCode,
        chainIndex: 0,
        chainId,
        start: this.x.length,
        end: this.x.length,
        kind: Kind.UNKNOWN,
        ss: SS.COIL,
        hetero: !!a.hetero,
        linkNext: false,
        ca: -1, c: -1, n: -1, o: -1, cb: -1, p: -1, c3: -1, c1: -1, o3: -1, base: -1,
        entityId: a.entityId ? this.intern(String(a.entityId)) : '',
        entityIndex: 0,
        index: this.residues.length,
      });
    }
    const res = this.residues[this.residues.length - 1];
    const i = this.x.length;
    res.end = i;

    const name = this.intern((a.name || '').trim());
    const polymerish = AMINO.has(resName) || NUCLEIC.has(resName);
    let elem;
    if (a.element && a.element.trim()) elem = elementIndex(a.element);
    else elem = guessElement(name, polymerish);
    if (elem === 0) elem = guessElement(name, polymerish);

    this.x.push(a.x); this.y.push(a.y); this.z.push(a.z);
    this.element.push(elem);
    this.bFactor.push(a.bFactor === undefined ? 0 : a.bFactor);
    this.occupancy.push(a.occupancy === undefined ? 1 : a.occupancy);
    this.serial.push(a.serial === undefined ? i + 1 : a.serial);
    this.atomName.push(name);
    this.altLoc.push(a.altLoc && a.altLoc.trim() ? a.altLoc.trim() : '');
    this.residueIndex.push(this.residues.length - 1);
  }

  /** Register a secondary structure range from HELIX/SHEET or struct_conf records. */
  addSecondaryStructure(chainId, startSeq, endSeq, type) {
    this.ssRanges.push({ chainId: (chainId || '').trim(), startSeq, endSeq, type });
  }

  addBond(serialA, serialB) {
    this.explicitBonds.push([serialA, serialB]);
  }

  finish() {
    const s = this.structure;
    const n = this.x.length;
    s.atomCount = n;
    s.x = Float32Array.from(this.x);
    s.y = Float32Array.from(this.y);
    s.z = Float32Array.from(this.z);
    s.element = Uint8Array.from(this.element);
    s.bFactor = Float32Array.from(this.bFactor);
    s.occupancy = Float32Array.from(this.occupancy);
    s.serial = Int32Array.from(this.serial);
    s.residueIndex = Uint32Array.from(this.residueIndex);
    s.atomName = this.atomName;
    s.altLoc = this.altLoc;
    s.residues = this.residues;

    buildChains(s);
    classifyResidues(s);
    applySecondaryStructure(s, this.ssRanges);
    linkPolymer(s);
    classifyChains(s);
    assignEntities(s, this.entityNames, this.compounds);
    s.computeBounds();

    if (this.explicitBonds.length) {
      s.explicitBonds = resolveExplicitBonds(s, this.explicitBonds);
    }
    return s;
  }
}

function buildChains(s) {
  s.chains = [];
  let current = null;
  for (let r = 0; r < s.residues.length; r++) {
    const res = s.residues[r];
    if (!current || current.id !== res.chainId) {
      current = {
        id: res.chainId,
        index: s.chains.length,
        residueStart: r,
        residueEnd: r,
        kind: Kind.UNKNOWN,
      };
      s.chains.push(current);
    }
    current.residueEnd = r;
    res.chainIndex = current.index;
  }
  for (const c of s.chains) {
    c.atomStart = s.residues[c.residueStart].start;
    c.atomEnd = s.residues[c.residueEnd].end;
  }
}

function classifyResidues(s) {
  for (const res of s.residues) {
    // Cache the backbone atoms; the cartoon builder leans on these.
    for (let i = res.start; i <= res.end; i++) {
      switch (s.atomName[i]) {
        case 'CA': res.ca = i; break;
        case 'C': res.c = i; break;
        case 'N': res.n = i; break;
        case 'O': case 'OXT': if (res.o < 0) res.o = i; break;
        case 'CB': res.cb = i; break;
        case 'P': res.p = i; break;
        case "C3'": case 'C3*': res.c3 = i; break;
        case "C1'": case 'C1*': res.c1 = i; break;
        case "O3'": case 'O3*': res.o3 = i; break;
        case 'N1': if (res.base < 0) res.base = i; break;
        case 'N9': res.base = i; break;
        default: break;
      }
    }

    if (WATER.has(res.name)) {
      res.kind = Kind.WATER;
    } else if (res.ca >= 0 && res.c >= 0 && res.n >= 0) {
      res.kind = Kind.PROTEIN;
    } else if (res.c3 >= 0 || (res.p >= 0 && res.c1 >= 0)) {
      res.kind = Kind.NUCLEIC;
    } else if (AMINO.has(res.name)) {
      res.kind = Kind.PROTEIN;
    } else if (NUCLEIC.has(res.name)) {
      res.kind = Kind.NUCLEIC;
    } else if (res.end === res.start && IONS.has(res.name)) {
      res.kind = Kind.ION;
    } else {
      res.kind = Kind.LIGAND;
    }
  }
}

function classifyChains(s) {
  for (const c of s.chains) {
    const counts = new Array(6).fill(0);
    for (let r = c.residueStart; r <= c.residueEnd; r++) counts[s.residues[r].kind]++;
    let best = Kind.UNKNOWN;
    // Polymer wins over incidental waters/ligands sharing the same chain id.
    if (counts[Kind.PROTEIN] > 0 || counts[Kind.NUCLEIC] > 0) {
      best = counts[Kind.PROTEIN] >= counts[Kind.NUCLEIC] ? Kind.PROTEIN : Kind.NUCLEIC;
    } else {
      let bestCount = -1;
      for (let k = 1; k < counts.length; k++) {
        if (counts[k] > bestCount) { bestCount = counts[k]; best = k; }
      }
    }
    c.kind = best;
    c.counts = {
      protein: counts[Kind.PROTEIN], nucleic: counts[Kind.NUCLEIC],
      water: counts[Kind.WATER], ion: counts[Kind.ION], ligand: counts[Kind.LIGAND],
    };
  }
}

/**
 * Group residues into entities -- the distinct molecules in the file, as
 * opposed to their copies. A 219-chain motor is really eight proteins; this is
 * what makes "colour by protein" and the component list possible.
 *
 * Three sources, in order of trust:
 *   1. mmCIF `_entity.pdbx_description`, reached through each atom's entity id;
 *   2. PDB COMPND records, which name molecules and list their chains;
 *   3. sequence identity -- chains that read the same are the same molecule.
 *
 * Ligands, ions and waters fall back to their residue name, so a haem is a
 * component in its own right rather than part of whatever chain it sits in.
 */
function assignEntities(s, entityNames, compounds) {
  const entities = [];
  const byKey = new Map();

  const chainCompound = new Map();
  for (const compound of compounds) {
    for (const id of compound.chains) chainCompound.set(id, compound.molecule);
  }

  const sequenceKey = new Map();
  for (const chain of s.chains) {
    if (chain.kind === Kind.PROTEIN || chain.kind === Kind.NUCLEIC) {
      sequenceKey.set(chain.index, `seq:${s.chainSequence(chain)}`);
    }
  }

  let unnamed = 0;
  for (const res of s.residues) {
    const chain = s.chains[res.chainIndex];
    const polymer = res.kind === Kind.PROTEIN || res.kind === Kind.NUCLEIC;
    let key;
    let name = null;

    if (res.entityId && entityNames.has(res.entityId)) {
      key = `entity:${res.entityId}`;
      name = entityNames.get(res.entityId);
    } else if (polymer && chainCompound.has(chain.id)) {
      name = chainCompound.get(chain.id);
      key = `compound:${name}`;
    } else if (res.kind === Kind.WATER) {
      key = 'water';
      name = 'Water';
    } else if (!polymer) {
      key = `chem:${res.name}`;
      name = res.name;
    } else if (sequenceKey.has(chain.index)) {
      key = sequenceKey.get(chain.index);
    } else {
      key = `chain:${chain.id}`;
      name = `Chain ${chain.id}`;
    }

    let entity = byKey.get(key);
    if (!entity) {
      if (!name) {
        unnamed++;
        name = res.kind === Kind.NUCLEIC ? `Nucleic acid ${unnamed}` : `Protein ${unnamed}`;
      }
      entity = {
        index: entities.length,
        name,
        kind: res.kind,
        residues: 0,
        atoms: 0,
        chains: [],
        chainSet: new Set(),
      };
      entities.push(entity);
      byKey.set(key, entity);
    }
    entity.residues++;
    entity.atoms += res.end - res.start + 1;
    if (!entity.chainSet.has(chain.id)) {
      entity.chainSet.add(chain.id);
      entity.chains.push(chain.id);
    }
    res.entityIndex = entity.index;
  }

  for (const entity of entities) delete entity.chainSet;
  s.entities = entities;

  // Each chain reports the entity most of its residues belong to, for the tree.
  for (const chain of s.chains) {
    const counts = new Map();
    for (let r = chain.residueStart; r <= chain.residueEnd; r++) {
      const index = s.residues[r].entityIndex;
      counts.set(index, (counts.get(index) || 0) + 1);
    }
    let best = 0;
    let bestCount = -1;
    for (const [index, count] of counts) {
      if (count > bestCount) { bestCount = count; best = index; }
    }
    chain.entityIndex = best;
  }
}

function applySecondaryStructure(s, ranges) {
  if (!ranges.length) return;
  // One chain id can appear as several runs -- in a PDB file the polymer comes
  // first and its ligands and waters follow under the same id -- so ranges have
  // to be applied to every run, not just the first one found.
  const byChain = new Map();
  for (const c of s.chains) {
    if (!byChain.has(c.id)) byChain.set(c.id, []);
    byChain.get(c.id).push(c);
  }
  let applied = 0;
  for (const range of ranges) {
    for (const chain of byChain.get(range.chainId) || []) {
      for (let r = chain.residueStart; r <= chain.residueEnd; r++) {
        const res = s.residues[r];
        if (res.seq >= range.startSeq && res.seq <= range.endSeq && res.kind === Kind.PROTEIN) {
          res.ss = range.type;
          applied++;
        }
      }
    }
  }
  // Records that matched nothing (mismatched ids, renumbered residues) would
  // otherwise leave the whole structure as coil; fall back to computing it.
  if (applied > 0) s.hasSecondaryStructure = true;
}

function dist2(s, i, j) {
  const dx = s.x[i] - s.x[j], dy = s.y[i] - s.y[j], dz = s.z[i] - s.z[j];
  return dx * dx + dy * dy + dz * dz;
}

/** Mark residues that are covalently continuous with the next one. */
function linkPolymer(s) {
  for (let r = 0; r < s.residues.length - 1; r++) {
    const a = s.residues[r], b = s.residues[r + 1];
    if (a.chainIndex !== b.chainIndex || a.kind !== b.kind) continue;
    if (a.kind === Kind.PROTEIN && a.c >= 0 && b.n >= 0) {
      a.linkNext = dist2(s, a.c, b.n) < 2.6 * 2.6;
    } else if (a.kind === Kind.NUCLEIC && a.o3 >= 0 && b.p >= 0) {
      a.linkNext = dist2(s, a.o3, b.p) < 2.6 * 2.6;
    } else if (a.kind === Kind.NUCLEIC && a.c3 >= 0 && b.c3 >= 0) {
      a.linkNext = dist2(s, a.c3, b.c3) < 8.0 * 8.0;
    }
  }
}

function resolveExplicitBonds(s, pairs) {
  const bySerial = new Map();
  for (let i = 0; i < s.atomCount; i++) bySerial.set(s.serial[i], i);
  const out = [];
  for (const [sa, sb] of pairs) {
    const a = bySerial.get(sa), b = bySerial.get(sb);
    if (a === undefined || b === undefined || a === b) continue;
    const cut = (COVALENT_RADII[s.element[a]] + COVALENT_RADII[s.element[b]] + 0.6) ** 2;
    if (dist2(s, a, b) <= cut) out.push(a < b ? [a, b] : [b, a]);
  }
  return out;
}

export { AMINO, NUCLEIC, WATER, IONS };
