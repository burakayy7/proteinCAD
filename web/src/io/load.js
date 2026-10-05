// Format detection and dispatch. Pure text in, Structure out.

import { parsePDB } from './pdb.js';
import { parseCIF } from './cif.js';
import { assignSecondaryStructure } from '../core/secondary.js';

export function detectFormat(text, filename = '') {
  const lower = filename.toLowerCase();
  if (lower.endsWith('.cif') || lower.endsWith('.mmcif') || lower.endsWith('.cif.gz')) return 'cif';
  if (lower.endsWith('.pdb') || lower.endsWith('.ent') || lower.endsWith('.pdb.gz')) return 'pdb';
  const head = text.slice(0, 4096);
  if (/^\s*data_/m.test(head) || head.includes('_atom_site.')) return 'cif';
  return 'pdb';
}

function baseName(filename) {
  const file = filename.split(/[\\/]/).pop() || 'structure';
  const stem = file.replace(/\.(gz|zip)$/i, '').replace(/\.(pdb|ent|cif|mmcif)$/i, '');
  // PDB ids read better upper case, and that is how the file names come back
  // from the cache; anything else keeps whatever the user called it.
  return /^[a-z0-9]{4}$/i.test(stem) ? stem.toUpperCase() : stem;
}

/**
 * Parse a structure file and finish the model: secondary structure is computed
 * when the file did not provide it.
 */
export function parseStructure(text, filename = 'structure') {
  const format = detectFormat(text, filename);
  const name = baseName(filename);
  const structure = format === 'cif' ? parseCIF(text, name) : parsePDB(text, name);
  if (structure.atomCount === 0) {
    throw new Error(`No atoms found in ${filename}. Is it a ${format.toUpperCase()} file?`);
  }
  assignSecondaryStructure(structure);
  structure.source = filename;
  return structure;
}
