// PDB format reader.
//
// Only the records that matter for viewing are handled: coordinates, secondary
// structure, explicit connectivity and a few title records. Alternate locations
// other than the first are dropped, and only the first MODEL is loaded (NMR
// ensembles would otherwise stack dozens of copies on top of each other).

import { StructureBuilder, SS } from '../core/structure.js';

function num(line, from, to, fallback = 0) {
  const v = parseFloat(line.slice(from, to));
  return Number.isFinite(v) ? v : fallback;
}

function int(line, from, to, fallback = 0) {
  const v = parseInt(line.slice(from, to), 10);
  return Number.isFinite(v) ? v : fallback;
}

export function parsePDB(text, name = 'structure') {
  const builder = new StructureBuilder(name);
  const s = builder.structure;
  s.format = 'pdb';

  let model = 0;
  let modelCount = 0;
  let atomIndex = 0;
  let compound = '';
  const titleParts = [];

  const len = text.length;
  let pos = 0;
  while (pos < len) {
    let end = text.indexOf('\n', pos);
    if (end === -1) end = len;
    const line = text.charCodeAt(end - 1) === 13 ? text.slice(pos, end - 1) : text.slice(pos, end);
    pos = end + 1;
    if (line.length < 6) continue;

    const record = line.slice(0, 6);
    if (record === 'ATOM  ' || record === 'HETATM') {
      if (model > 1) continue; // keep the first model only
      if (line.length < 54) continue;
      const altLoc = line[16];
      if (altLoc !== ' ' && altLoc !== 'A' && altLoc !== '1') continue;
      builder.addAtom({
        serial: int(line, 6, 11, atomIndex + 1),
        name: line.slice(12, 16),
        altLoc,
        resName: line.slice(17, 20),
        chainId: line.slice(20, 22).trim() || 'A',
        resSeq: int(line, 22, 26),
        insCode: line[26] || ' ',
        x: num(line, 30, 38),
        y: num(line, 38, 46),
        z: num(line, 46, 54),
        occupancy: line.length >= 60 ? num(line, 54, 60, 1) : 1,
        bFactor: line.length >= 66 ? num(line, 60, 66, 0) : 0,
        element: line.length >= 78 ? line.slice(76, 78) : '',
        hetero: record === 'HETATM',
      });
      atomIndex++;
    } else if (record === 'HELIX ') {
      builder.addSecondaryStructure(line.slice(19, 20), int(line, 21, 25), int(line, 33, 37), SS.HELIX);
    } else if (record === 'SHEET ') {
      builder.addSecondaryStructure(line.slice(21, 22), int(line, 22, 26), int(line, 33, 37), SS.SHEET);
    } else if (record === 'MODEL ') {
      model = int(line, 10, 14, model + 1);
      modelCount++;
    } else if (record === 'CONECT') {
      const from = int(line, 6, 11, -1);
      if (from >= 0) {
        for (let c = 11; c + 5 <= line.length && c < 31; c += 5) {
          const to = int(line, c, c + 5, -1);
          if (to > 0) builder.addBond(from, to);
        }
      }
    } else if (record === 'TITLE ') {
      const part = line.slice(10).trim();
      if (part) titleParts.push(part);
    } else if (record === 'COMPND') {
      compound += `${line.slice(10).trim()} `;
    } else if (record === 'HEADER') {
      const code = line.slice(62, 66).trim();
      if (code) s.name = code;
    } else if (record === 'CRYST1') {
      s.cell = {
        a: num(line, 6, 15), b: num(line, 15, 24), c: num(line, 24, 33),
        alpha: num(line, 33, 40), beta: num(line, 40, 47), gamma: num(line, 47, 54),
        spaceGroup: line.slice(55, 66).trim(),
      };
    }
  }

  s.title = titleParts.join(' ').replace(/\s+/g, ' ').trim();
  s.modelCount = Math.max(1, modelCount);
  builder.compounds = parseCompound(compound);
  return builder.finish();
}

/**
 * COMPND names the molecules in the file and says which chains carry each one:
 *
 *   COMPND    MOL_ID: 1;
 *   COMPND   2 MOLECULE: HEMOGLOBIN (DEOXY) (ALPHA CHAIN);
 *   COMPND   3 CHAIN: A, C;
 *
 * The records are one long semicolon-separated list once the continuation
 * numbers are stripped, so this reads it as key/value pairs and starts a new
 * molecule at each MOL_ID.
 */
function parseCompound(text) {
  const compounds = [];
  let current = null;
  for (const part of text.split(';')) {
    const colon = part.indexOf(':');
    if (colon < 0) continue;
    const key = part.slice(0, colon).trim().toUpperCase();
    const value = part.slice(colon + 1).trim();
    if (key === 'MOL_ID') {
      current = { molecule: '', chains: [] };
      compounds.push(current);
    } else if (!current) {
      continue;
    } else if (key === 'MOLECULE') {
      current.molecule = value.replace(/\s+/g, ' ');
    } else if (key === 'CHAIN') {
      current.chains = value.split(',').map((id) => id.trim()).filter(Boolean);
    }
  }
  return compounds.filter((c) => c.molecule && c.chains.length);
}
