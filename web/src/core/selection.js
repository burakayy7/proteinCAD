// A small selection language, in the spirit of PyMOL's.
//
//   chain A and resi 10-45
//   protein and not water
//   byres (within 4.5 of ligand)
//   ss h or ss e
//   name CA and b > 70
//
// Evaluating an expression against a Structure yields a Uint8Array mask over
// atoms. Masks are the currency of the whole app: representations, colouring,
// the inspector and the transform tools all take one.

import { SpatialGrid } from './grid.js';
import { Kind, SS } from './structure.js';
import { elementIndex, ELEMENT_H } from './elements.js';

const BACKBONE = new Set(['N', 'CA', 'C', 'O', 'OXT', 'P', "O5'", "C5'", "C4'", "C3'", "O3'", "O1P", "O2P", 'OP1', 'OP2']);

const KIND_KEYWORDS = {
  protein: Kind.PROTEIN,
  nucleic: Kind.NUCLEIC,
  water: Kind.WATER,
  ion: Kind.ION,
  ligand: Kind.LIGAND,
};

class Tokens {
  constructor(text) {
    this.list = String(text)
      .replace(/([()])/g, ' $1 ')
      .trim()
      .split(/\s+/)
      .filter(Boolean);
    this.pos = 0;
  }
  peek() { return this.list[this.pos]; }
  next() { return this.list[this.pos++]; }
  eof() { return this.pos >= this.list.length; }
}

export class SelectionError extends Error {}

/** Compile an expression into a function (structure) => Uint8Array. */
export function compileSelection(expr) {
  const tokens = new Tokens(expr || 'all');
  if (tokens.list.length === 0) return () => null;
  const node = parseOr(tokens);
  if (!tokens.eof()) throw new SelectionError(`unexpected "${tokens.peek()}"`);
  return (structure) => node(structure);
}

/** Convenience: compile and evaluate in one step. */
export function select(structure, expr) {
  return compileSelection(expr)(structure);
}

function parseOr(tokens) {
  let left = parseAnd(tokens);
  for (;;) {
    const t = tokens.peek();
    if (t !== 'or' && t !== '|') return left;
    tokens.next();
    const right = parseAnd(tokens);
    const prev = left;
    left = (s) => {
      const a = prev(s), b = right(s);
      for (let i = 0; i < a.length; i++) a[i] |= b[i];
      return a;
    };
  }
}

function parseAnd(tokens) {
  let left = parseNot(tokens);
  for (;;) {
    const t = tokens.peek();
    if (t !== 'and' && t !== '&') return left;
    tokens.next();
    const right = parseNot(tokens);
    const prev = left;
    left = (s) => {
      const a = prev(s), b = right(s);
      for (let i = 0; i < a.length; i++) a[i] &= b[i];
      return a;
    };
  }
}

function parseNot(tokens) {
  const t = tokens.peek();
  if (t === 'not' || t === '!') {
    tokens.next();
    const inner = parseNot(tokens);
    return (s) => {
      const a = inner(s);
      for (let i = 0; i < a.length; i++) a[i] = a[i] ? 0 : 1;
      return a;
    };
  }
  return parseTerm(tokens);
}

function splitList(arg) {
  return arg.split(',').map((v) => v.trim()).filter(Boolean);
}

function parseRanges(arg) {
  const ranges = [];
  for (const part of splitList(arg)) {
    const m = part.match(/^(-?\d+)\s*(?:-|:|\.\.)\s*(-?\d+)$/);
    if (m) ranges.push([parseInt(m[1], 10), parseInt(m[2], 10)]);
    else {
      const v = parseInt(part, 10);
      if (!Number.isFinite(v)) throw new SelectionError(`bad number range "${part}"`);
      ranges.push([v, v]);
    }
  }
  return ranges;
}

function inRanges(ranges, value) {
  for (const [lo, hi] of ranges) if (value >= lo && value <= hi) return true;
  return false;
}

function byAtom(fn) {
  return (s) => {
    const mask = new Uint8Array(s.atomCount);
    for (let i = 0; i < s.atomCount; i++) mask[i] = fn(s, i) ? 1 : 0;
    return mask;
  };
}

function byResidue(fn) {
  return (s) => {
    const mask = new Uint8Array(s.atomCount);
    for (const res of s.residues) {
      if (!fn(s, res)) continue;
      for (let i = res.start; i <= res.end; i++) mask[i] = 1;
    }
    return mask;
  };
}

function parseTerm(tokens) {
  if (tokens.eof()) throw new SelectionError('unexpected end of selection');
  const raw = tokens.next();
  const word = raw.toLowerCase();

  if (word === '(') {
    const inner = parseOr(tokens);
    if (tokens.next() !== ')') throw new SelectionError('missing ")"');
    return inner;
  }

  if (word === 'all' || word === '*') return (s) => new Uint8Array(s.atomCount).fill(1);
  if (word === 'none') return (s) => new Uint8Array(s.atomCount);

  if (word in KIND_KEYWORDS) {
    const kind = KIND_KEYWORDS[word];
    return byResidue((s, res) => res.kind === kind);
  }
  if (word === 'polymer') return byResidue((s, res) => res.kind === Kind.PROTEIN || res.kind === Kind.NUCLEIC);
  if (word === 'hetero' || word === 'het') return byResidue((s, res) => res.hetero);
  if (word === 'hydrogen' || word === 'h') return byAtom((s, i) => s.element[i] === ELEMENT_H);
  if (word === 'backbone' || word === 'mainchain') {
    return byAtom((s, i) => {
      const res = s.residueOfAtom(i);
      return (res.kind === Kind.PROTEIN || res.kind === Kind.NUCLEIC) && BACKBONE.has(s.atomName[i]);
    });
  }
  if (word === 'sidechain') {
    return byAtom((s, i) => {
      const res = s.residueOfAtom(i);
      return (res.kind === Kind.PROTEIN || res.kind === Kind.NUCLEIC) && !BACKBONE.has(s.atomName[i]);
    });
  }

  if (word === 'chain') {
    const values = new Set(splitList(requireArg(tokens, 'chain')));
    return byResidue((s, res) => values.has(s.chains[res.chainIndex].id));
  }
  if (word === 'resi' || word === 'resid' || word === 'residue' || word === 'i') {
    const ranges = parseRanges(requireArg(tokens, 'resi'));
    return byResidue((s, res) => inRanges(ranges, res.seq));
  }
  if (word === 'resn' || word === 'resname' || word === 'r') {
    const values = new Set(splitList(requireArg(tokens, 'resn')).map((v) => v.toUpperCase()));
    return byResidue((s, res) => values.has(res.name));
  }
  if (word === 'name' || word === 'atom') {
    const values = new Set(splitList(requireArg(tokens, 'name')).map((v) => v.toUpperCase()));
    return byAtom((s, i) => values.has(s.atomName[i].toUpperCase()));
  }
  if (word === 'elem' || word === 'element' || word === 'e') {
    const values = new Set(splitList(requireArg(tokens, 'elem')).map((v) => elementIndex(v)));
    return byAtom((s, i) => values.has(s.element[i]));
  }
  if (word === 'ss') {
    const codes = new Set(splitList(requireArg(tokens, 'ss')).map((v) => v.toLowerCase()));
    const wanted = new Set();
    if (codes.has('h') || codes.has('helix')) wanted.add(SS.HELIX);
    if (codes.has('e') || codes.has('s') || codes.has('sheet') || codes.has('strand')) wanted.add(SS.SHEET);
    if (codes.has('c') || codes.has('l') || codes.has('coil') || codes.has('loop')) wanted.add(SS.COIL);
    if (codes.has('t') || codes.has('turn')) wanted.add(SS.TURN);
    return byResidue((s, res) => wanted.has(res.ss));
  }
  if (word === 'index') {
    const ranges = parseRanges(requireArg(tokens, 'index'));
    return byAtom((s, i) => inRanges(ranges, i));
  }
  if (word === 'serial') {
    const ranges = parseRanges(requireArg(tokens, 'serial'));
    return byAtom((s, i) => inRanges(ranges, s.serial[i]));
  }
  if (word === 'b' || word === 'bfactor' || word === 'plddt' || word === 'occ' || word === 'occupancy') {
    const field = (word === 'occ' || word === 'occupancy') ? 'occupancy' : 'bFactor';
    const { op, value } = parseComparison(tokens, word);
    return byAtom((s, i) => compare(s[field][i], op, value));
  }

  if (word === 'byres') {
    const inner = parseTerm(tokens);
    return (s) => expandToResidues(s, inner(s));
  }

  if (word === 'within') {
    const distance = parseFloat(requireArg(tokens, 'within'));
    if (!Number.isFinite(distance)) throw new SelectionError('within needs a distance');
    if ((tokens.peek() || '').toLowerCase() === 'of') tokens.next();
    const inner = parseTerm(tokens);
    return (s) => {
      const target = inner(s);
      return withinDistance(s, target, distance);
    };
  }

  throw new SelectionError(`unknown selection keyword "${raw}"`);
}

function requireArg(tokens, keyword) {
  if (tokens.eof()) throw new SelectionError(`"${keyword}" needs a value`);
  return tokens.next();
}

function parseComparison(tokens, keyword) {
  let token = requireArg(tokens, keyword);
  let op = '>';
  const m = token.match(/^(<=|>=|<|>|=|==|!=)$/);
  if (m) {
    op = token;
    token = requireArg(tokens, keyword);
  } else {
    const inline = token.match(/^(<=|>=|<|>|=|==|!=)(.*)$/);
    if (inline) { op = inline[1]; token = inline[2] || requireArg(tokens, keyword); }
  }
  const value = parseFloat(token);
  if (!Number.isFinite(value)) throw new SelectionError(`"${keyword}" needs a number`);
  return { op, value };
}

function compare(a, op, b) {
  switch (op) {
    case '<': return a < b;
    case '<=': return a <= b;
    case '>': return a > b;
    case '>=': return a >= b;
    case '=': case '==': return a === b;
    case '!=': return a !== b;
    default: return false;
  }
}

/** Grow a mask so that every touched residue is fully selected. */
export function expandToResidues(structure, mask) {
  const out = new Uint8Array(structure.atomCount);
  for (const res of structure.residues) {
    let hit = false;
    for (let i = res.start; i <= res.end; i++) if (mask[i]) { hit = true; break; }
    if (!hit) continue;
    for (let i = res.start; i <= res.end; i++) out[i] = 1;
  }
  return out;
}

/** Atoms lying within `distance` of any atom in `target`. */
export function withinDistance(structure, target, distance) {
  const out = new Uint8Array(structure.atomCount);
  const seeds = [];
  for (let i = 0; i < target.length; i++) if (target[i]) seeds.push(i);
  if (!seeds.length) return out;
  const grid = new SpatialGrid(structure.x, structure.y, structure.z, Int32Array.from(seeds), Math.max(distance, 3));
  const d2 = distance * distance;
  for (let i = 0; i < structure.atomCount; i++) {
    const xi = structure.x[i], yi = structure.y[i], zi = structure.z[i];
    let found = 0;
    grid.near(xi, yi, zi, distance, (j) => {
      if (found) return;
      const dx = structure.x[j] - xi, dy = structure.y[j] - yi, dz = structure.z[j] - zi;
      if (dx * dx + dy * dy + dz * dz <= d2) found = 1;
    });
    out[i] = found;
  }
  return out;
}

export function countMask(mask) {
  let n = 0;
  for (let i = 0; i < mask.length; i++) n += mask[i] ? 1 : 0;
  return n;
}

/** Short human summary of a mask, for the inspector panel. */
export function describeSelection(structure, mask) {
  const chains = new Set();
  const residues = [];
  let atoms = 0;
  for (const res of structure.residues) {
    let hit = 0;
    for (let i = res.start; i <= res.end; i++) if (mask[i]) hit++;
    if (!hit) continue;
    atoms += hit;
    residues.push(res);
    chains.add(structure.chains[res.chainIndex].id);
  }
  return {
    atoms,
    residues: residues.length,
    chains: [...chains].sort(),
    firstResidue: residues[0] || null,
  };
}

export function maskForChain(structure, chainIndex) {
  const mask = new Uint8Array(structure.atomCount);
  const chain = structure.chains[chainIndex];
  if (!chain) return mask;
  for (let i = chain.atomStart; i <= chain.atomEnd; i++) mask[i] = 1;
  return mask;
}

export function maskForResidue(structure, res) {
  const mask = new Uint8Array(structure.atomCount);
  for (let i = res.start; i <= res.end; i++) mask[i] = 1;
  return mask;
}

export function maskAll(structure) {
  return new Uint8Array(structure.atomCount).fill(1);
}
