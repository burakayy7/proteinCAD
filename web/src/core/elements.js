// Chemical element table: symbols, radii and CPK colours.
//
// Elements are referenced by a small integer index everywhere else in the app so
// that per-atom data fits in typed arrays. Index 0 is the "unknown" element.
//
// vdw      - van der Waals radius in Angstrom (space-filling representation)
// covalent - covalent radius in Angstrom (used for bond perception)
// color    - packed 0xRRGGBB, the usual CPK / Jmol convention

const DATA = [
  // symbol, vdw, covalent, color
  ['X', 1.50, 0.80, 0xff1493],
  ['H', 1.10, 0.31, 0xffffff],
  ['He', 1.40, 0.28, 0xd9ffff],
  ['Li', 1.82, 1.28, 0xcc80ff],
  ['Be', 1.53, 0.96, 0xc2ff00],
  ['B', 1.92, 0.84, 0xffb5b5],
  ['C', 1.70, 0.76, 0x909090],
  ['N', 1.55, 0.71, 0x3050f8],
  ['O', 1.52, 0.66, 0xff0d0d],
  ['F', 1.47, 0.57, 0x90e050],
  ['Ne', 1.54, 0.58, 0xb3e3f5],
  ['Na', 2.27, 1.66, 0xab5cf2],
  ['Mg', 1.73, 1.41, 0x8aff00],
  ['Al', 1.84, 1.21, 0xbfa6a6],
  ['Si', 2.10, 1.11, 0xf0c8a0],
  ['P', 1.80, 1.07, 0xff8000],
  ['S', 1.80, 1.05, 0xffff30],
  ['Cl', 1.75, 1.02, 0x1ff01f],
  ['Ar', 1.88, 1.06, 0x80d1e3],
  ['K', 2.75, 2.03, 0x8f40d4],
  ['Ca', 2.31, 1.76, 0x3dff00],
  ['Sc', 2.11, 1.70, 0xe6e6e6],
  ['Ti', 1.87, 1.60, 0xbfc2c7],
  ['V', 1.79, 1.53, 0xa6a6ab],
  ['Cr', 1.89, 1.39, 0x8a99c7],
  ['Mn', 1.97, 1.39, 0x9c7ac7],
  ['Fe', 1.94, 1.32, 0xe06633],
  ['Co', 1.92, 1.26, 0xf090a0],
  ['Ni', 1.63, 1.24, 0x50d050],
  ['Cu', 1.40, 1.32, 0xc88033],
  ['Zn', 1.39, 1.22, 0x7d80b0],
  ['Ga', 1.87, 1.22, 0xc28f8f],
  ['Ge', 2.11, 1.20, 0x668f8f],
  ['As', 1.85, 1.19, 0xbd80e3],
  ['Se', 1.90, 1.20, 0xffa100],
  ['Br', 1.85, 1.20, 0xa62929],
  ['Kr', 2.02, 1.16, 0x5cb8d1],
  ['Rb', 3.03, 2.20, 0x702eb0],
  ['Sr', 2.49, 1.95, 0x00ff00],
  ['Y', 2.32, 1.90, 0x94ffff],
  ['Zr', 2.23, 1.75, 0x94e0e0],
  ['Nb', 2.18, 1.64, 0x73c2c9],
  ['Mo', 2.17, 1.54, 0x54b5b5],
  ['Tc', 2.16, 1.47, 0x3b9e9e],
  ['Ru', 2.13, 1.46, 0x248f8f],
  ['Rh', 2.10, 1.42, 0x0a7d8c],
  ['Pd', 2.10, 1.39, 0x006985],
  ['Ag', 2.11, 1.45, 0xc0c0c0],
  ['Cd', 2.18, 1.44, 0xffd98f],
  ['In', 1.93, 1.42, 0xa67573],
  ['Sn', 2.17, 1.39, 0x668080],
  ['Sb', 2.06, 1.39, 0x9e63b5],
  ['Te', 2.06, 1.38, 0xd47a00],
  ['I', 1.98, 1.39, 0x940094],
  ['Xe', 2.16, 1.40, 0x429eb0],
  ['Cs', 3.43, 2.44, 0x57178f],
  ['Ba', 2.68, 2.15, 0x00c900],
  ['La', 2.43, 2.07, 0x70d4ff],
  ['Ce', 2.42, 2.04, 0xffffc7],
  ['W', 2.18, 1.62, 0x2194d6],
  ['Pt', 2.13, 1.36, 0xd0d0e0],
  ['Au', 2.14, 1.36, 0xffd123],
  ['Hg', 2.23, 1.32, 0xb8b8d0],
  ['Pb', 2.02, 1.46, 0x575961],
  ['U', 1.86, 1.96, 0x008fff],
];

export const SYMBOLS = DATA.map((d) => d[0]);
export const VDW_RADII = Float32Array.from(DATA, (d) => d[1]);
export const COVALENT_RADII = Float32Array.from(DATA, (d) => d[2]);
export const ELEMENT_COLORS = Uint32Array.from(DATA, (d) => d[3]);
export const ELEMENT_UNKNOWN = 0;
export const ELEMENT_H = 1;
export const ELEMENT_C = 6;

const BY_SYMBOL = new Map();
for (let i = 0; i < SYMBOLS.length; i++) BY_SYMBOL.set(SYMBOLS[i].toUpperCase(), i);
// Deuterium and tritium are treated as hydrogen.
BY_SYMBOL.set('D', ELEMENT_H);
BY_SYMBOL.set('T', ELEMENT_H);

/** Element index for a symbol such as "C", "Fe", "FE". Unknown symbols map to 0. */
export function elementIndex(symbol) {
  if (!symbol) return ELEMENT_UNKNOWN;
  const key = symbol.trim().toUpperCase();
  const found = BY_SYMBOL.get(key);
  return found === undefined ? ELEMENT_UNKNOWN : found;
}

const NON_ELEMENT_CHARS = /[^A-Za-z]/g;

/**
 * Guess an element from a PDB atom name when the file omits the element column.
 * Atom names are ambiguous ("CA" is a C-alpha carbon in a protein but calcium
 * in an ion), so the residue kind is used as a tie-breaker.
 */
export function guessElement(atomName, isPolymerResidue) {
  const raw = (atomName || '').trim();
  if (!raw) return ELEMENT_UNKNOWN;
  const letters = raw.replace(NON_ELEMENT_CHARS, '');
  if (!letters) return ELEMENT_UNKNOWN;

  // In polymers, atom names follow the "element + remoteness" convention, so the
  // first letter is the element -- except for hydrogens named like "1HB"/"HB2".
  if (isPolymerResidue) {
    if (/^[0-9]*H/.test(raw)) return ELEMENT_H;
    return elementIndex(letters[0]);
  }

  // Free-standing groups: prefer a two-letter match (FE, ZN, CL...) then fall back.
  if (letters.length >= 2) {
    const two = elementIndex(letters.slice(0, 2));
    if (two !== ELEMENT_UNKNOWN) return two;
  }
  const one = elementIndex(letters[0]);
  if (one !== ELEMENT_UNKNOWN) return one;
  return ELEMENT_UNKNOWN;
}

export function elementSymbol(index) {
  return SYMBOLS[index] || 'X';
}
