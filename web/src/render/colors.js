// Colour schemes.
//
// Every scheme produces one Float32Array of RGB triples per structure, in
// linear space (that is what three.js vertex colours expect). The geometry
// builders just index into it, so adding a scheme never touches the builders.

import { ELEMENT_COLORS } from '../core/elements.js';
import { Kind, SS } from '../core/structure.js';

export const CHAIN_PALETTE = [
  0x4f9dff, 0xff7a5c, 0x5fd68a, 0xffd166, 0xc792ea, 0x4dd0e1,
  0xf78fb3, 0xa3d977, 0xffa94d, 0x8fa3ff, 0x7fdbca, 0xe0b0ff,
  0xd4a373, 0x9be9a8, 0xff9ff3, 0xb0bec5,
];

export const STRUCTURE_PALETTE = [
  0x6fb3ff, 0xff8f6b, 0x74e0a3, 0xffd97d, 0xcf9cf5, 0x5fd9e8,
];

const SS_COLORS = {
  [SS.HELIX]: 0xf26d6d,
  [SS.SHEET]: 0xf2c14e,
  [SS.COIL]: 0x8b98a9,
  [SS.TURN]: 0x7fd1b9,
};

const KIND_COLORS = {
  [Kind.PROTEIN]: 0x8fa8c8,
  [Kind.NUCLEIC]: 0xd9a066,
  [Kind.WATER]: 0x5b7ea8,
  [Kind.ION]: 0xc792ea,
  [Kind.LIGAND]: 0x7fd1b9,
  [Kind.UNKNOWN]: 0x9aa5b1,
};

// Kyte-Doolittle hydropathy, mapped to a blue (polar) -> gold (greasy) ramp.
const HYDROPATHY = {
  ILE: 4.5, VAL: 4.2, LEU: 3.8, PHE: 2.8, CYS: 2.5, MET: 1.9, ALA: 1.8, GLY: -0.4,
  THR: -0.7, SER: -0.8, TRP: -0.9, TYR: -1.3, PRO: -1.6, HIS: -3.2, GLU: -3.5,
  GLN: -3.5, ASP: -3.5, ASN: -3.5, LYS: -3.9, ARG: -4.5,
};

const RESIDUE_PALETTE = {
  ALA: 0x8cd17d, ARG: 0x4f9dff, ASN: 0x5fd68a, ASP: 0xff6b6b, CYS: 0xffd166,
  GLN: 0x5fd68a, GLU: 0xff6b6b, GLY: 0xd8d8d8, HIS: 0x7fb3ff, ILE: 0x8cd17d,
  LEU: 0x8cd17d, LYS: 0x4f9dff, MET: 0xffd166, PHE: 0xc792ea, PRO: 0xffa94d,
  SER: 0x7fdbca, THR: 0x7fdbca, TRP: 0xc792ea, TYR: 0xc792ea, VAL: 0x8cd17d,
};

export const COLOR_SCHEMES = [
  { id: 'chain', label: 'Chain' },
  { id: 'entity', label: 'Protein / component' },
  { id: 'structure', label: 'Structure' },
  { id: 'ss', label: 'Secondary structure' },
  { id: 'element', label: 'Element' },
  { id: 'residue', label: 'Residue type' },
  { id: 'hydrophobicity', label: 'Hydrophobicity' },
  { id: 'bfactor', label: 'B-factor / pLDDT' },
  { id: 'kind', label: 'Molecule type' },
  { id: 'uniform', label: 'Single colour' },
];

/** sRGB hex -> linear RGB triple. */
export function hexToLinear(hex, out = [0, 0, 0]) {
  out[0] = srgbToLinear(((hex >> 16) & 255) / 255);
  out[1] = srgbToLinear(((hex >> 8) & 255) / 255);
  out[2] = srgbToLinear((hex & 255) / 255);
  return out;
}

export function srgbToLinear(c) {
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

export function linearToSrgb(c) {
  return c <= 0.0031308 ? c * 12.92 : 1.055 * Math.pow(c, 1 / 2.4) - 0.055;
}

export function hexString(hex) {
  return `#${(hex & 0xffffff).toString(16).padStart(6, '0')}`;
}

export function chainColor(structure, chainIndex, structureIndex = 0) {
  return CHAIN_PALETTE[(chainIndex + structureIndex * 5) % CHAIN_PALETTE.length];
}

/**
 * Colour per distinct molecule rather than per copy of it: in an assembly the
 * 33 copies of one hook protein all read as the same colour.
 */
export function entityColor(entityIndex) {
  return CHAIN_PALETTE[entityIndex % CHAIN_PALETTE.length];
}

export function structureColor(structureIndex) {
  return STRUCTURE_PALETTE[structureIndex % STRUCTURE_PALETTE.length];
}

function lerpHex(a, b, t) {
  const ar = (a >> 16) & 255, ag = (a >> 8) & 255, ab = a & 255;
  const br = (b >> 16) & 255, bg = (b >> 8) & 255, bb = b & 255;
  const r = Math.round(ar + (br - ar) * t);
  const g = Math.round(ag + (bg - ag) * t);
  const bl = Math.round(ab + (bb - ab) * t);
  return (r << 16) | (g << 8) | bl;
}

/** Blue -> pale -> red ramp used for B-factors. */
function bFactorColor(t) {
  return t < 0.5 ? lerpHex(0x3b6ff2, 0xf0f0f0, t * 2) : lerpHex(0xf0f0f0, 0xe04b4b, (t - 0.5) * 2);
}

/** The AlphaFold pLDDT palette. */
function plddtColor(v) {
  if (v >= 90) return 0x0053d6;
  if (v >= 70) return 0x65cbf3;
  if (v >= 50) return 0xffdb13;
  return 0xff7d45;
}

/**
 * @param {Structure} structure
 * @param {string} scheme id from COLOR_SCHEMES
 * @param {{structureIndex?: number, uniformColor?: number, chainOverrides?: Map<number, number>}} ctx
 * @returns {Float32Array} 3 linear floats per atom
 */
export function computeAtomColors(structure, scheme, ctx = {}) {
  const n = structure.atomCount;
  const out = new Float32Array(n * 3);
  const rgb = [0, 0, 0];
  const structureIndex = ctx.structureIndex || 0;
  const overrides = ctx.chainOverrides;

  const write = (i, hex) => {
    hexToLinear(hex, rgb);
    out[i * 3] = rgb[0]; out[i * 3 + 1] = rgb[1]; out[i * 3 + 2] = rgb[2];
  };

  // Per-chain and per-residue schemes are resolved once per residue.
  switch (scheme) {
    case 'element': {
      for (let i = 0; i < n; i++) write(i, ELEMENT_COLORS[structure.element[i]]);
      break;
    }
    case 'bfactor': {
      const looksLikePlddt = isPlddt(structure);
      let lo = Infinity, hi = -Infinity;
      for (let i = 0; i < n; i++) {
        const v = structure.bFactor[i];
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
      if (!Number.isFinite(lo) || hi <= lo) { lo = 0; hi = 1; }
      for (let i = 0; i < n; i++) {
        const v = structure.bFactor[i];
        write(i, looksLikePlddt ? plddtColor(v) : bFactorColor((v - lo) / (hi - lo)));
      }
      break;
    }
    case 'uniform': {
      const hex = ctx.uniformColor === undefined ? 0x9fb4cc : ctx.uniformColor;
      for (let i = 0; i < n; i++) write(i, hex);
      break;
    }
    default: {
      for (const res of structure.residues) {
        let hex;
        switch (scheme) {
          case 'chain': {
            hex = (overrides && overrides.get(res.chainIndex)) ??
              chainColor(structure, res.chainIndex, structureIndex);
            break;
          }
          case 'entity': hex = entityColor(res.entityIndex || 0); break;
          case 'structure': hex = structureColor(structureIndex); break;
          case 'ss': hex = res.kind === Kind.PROTEIN ? (SS_COLORS[res.ss] ?? SS_COLORS[SS.COIL]) : KIND_COLORS[res.kind]; break;
          case 'residue': hex = RESIDUE_PALETTE[res.name] ?? KIND_COLORS[res.kind]; break;
          case 'hydrophobicity': {
            const h = HYDROPATHY[res.name];
            hex = h === undefined ? 0x9aa5b1 : lerpHex(0x4f9dff, 0xffb347, (h + 4.5) / 9);
            break;
          }
          case 'kind': hex = KIND_COLORS[res.kind] ?? KIND_COLORS[Kind.UNKNOWN]; break;
          default: hex = 0x9fb4cc;
        }
        for (let i = res.start; i <= res.end; i++) write(i, hex);
      }
    }
  }

  // Ligands and ions keep element colours in the polymer-oriented schemes: a
  // haem or a zinc ion is much easier to read that way.
  if (scheme === 'chain' || scheme === 'structure' || scheme === 'ss') {
    for (const res of structure.residues) {
      if (res.kind === Kind.LIGAND || res.kind === Kind.ION) {
        for (let i = res.start; i <= res.end; i++) write(i, ELEMENT_COLORS[structure.element[i]]);
      }
    }
  }
  return out;
}

/** pLDDT files store 0-100 confidence in the B-factor column. */
function isPlddt(structure) {
  if (structure.atomCount === 0) return false;
  let min = Infinity, max = -Infinity;
  const step = Math.max(1, Math.floor(structure.atomCount / 500));
  for (let i = 0; i < structure.atomCount; i += step) {
    const v = structure.bFactor[i];
    if (v < min) min = v;
    if (v > max) max = v;
  }
  return min >= 0 && max <= 100 && max > 40 && /alphafold|_af|plddt|model_/i.test(structure.name + ' ' + (structure.source || ''));
}

/** Colour used to tint the current selection. */
export const SELECTION_COLOR = 0xffd54a;
export const HOVER_COLOR = 0xffffff;
