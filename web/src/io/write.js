// PDB writer. Used by "Export" -- coordinates are written after applying the
// scene transform of each structure, so anything moved with the gizmo comes out
// where the user put it.

import { elementSymbol } from '../core/elements.js';
import { Kind } from '../core/structure.js';

const CHAIN_POOL = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';

function pad(value, width) {
  return String(value).padStart(width);
}

function padRight(value, width) {
  return String(value).padEnd(width);
}

function fixed(value, width, decimals) {
  let s = value.toFixed(decimals);
  if (s.length > width) s = value.toPrecision(width - 2);
  return pad(s, width);
}

/** Format the atom name into PDB's slightly odd four-column field. */
function atomNameField(name, element) {
  const n = name.trim();
  if (n.length >= 4) return n.slice(0, 4);
  if (element.length === 1) return ' ' + padRight(n, 3);
  return padRight(n, 4);
}

function applyMatrix(m, x, y, z, out) {
  if (!m) { out[0] = x; out[1] = y; out[2] = z; return out; }
  out[0] = m[0] * x + m[4] * y + m[8] * z + m[12];
  out[1] = m[1] * x + m[5] * y + m[9] * z + m[13];
  out[2] = m[2] * x + m[6] * y + m[10] * z + m[14];
  return out;
}

function writesAtoms(entry, chain) {
  if (!entry.mask) return true;
  for (let i = chain.atomStart; i <= chain.atomEnd; i++) if (entry.mask[i]) return true;
  return false;
}

/**
 * The single-letter chain ids `writePDB` will use, per entry, keyed by chain index.
 *
 * PDB gives the chain one column, so anything longer has to be renamed on the
 * way out -- mmCIF is happy with `BL`, and 7CGO has 219 chains named like that.
 * This is exported because a caller that names chains *elsewhere* -- a design
 * spec's contigs and hotspots, say -- has to use the same names as the file it
 * sends alongside them. Asking is the only way to be sure they agree.
 *
 * Only chains that actually write an atom get an id. Assigning to all 219 chains
 * of an assembly when three are being written would exhaust the 62-character
 * pool and start handing out duplicates.
 *
 * A name is kept only when it is a *letter*. A digit is legal in the column and
 * mmCIF hands them out freely -- 7CGO has chains called `6L`, which truncates to
 * `6` -- but downstream a chain id is not just a column, it is the first
 * character of every "chain + residue number" label, and `6316` cannot be split
 * back into the two. RFdiffusion decides whether a contig fragment names a chain
 * or a length to generate with exactly one test, `subcon[0].isalpha()`, so a
 * chain called `6` turns `6316-319` into "generate between 6316 and 319
 * residues" and dies inside random.randint. Digits stay in the pool as a last
 * resort for exporting an assembly with more than 52 chains, where a readable
 * name matters less than a distinct one.
 */
export function chainIdsFor(entries) {
  const letter = (c) => /^[A-Za-z]$/.test(c || '');
  const leading = (chain) => (chain.id.length === 1 ? chain.id : chain.id[0]) || '';
  const owns = (chain) => chain.id.length === 1 && letter(chain.id);

  const writing = entries.map((entry) =>
    entry.structure.chains.filter((chain) => writesAtoms(entry, chain)));

  // Chains that are *already* called by a single letter reserve it first, so a
  // renamed one never takes a name its owner is about to want: without this,
  // `6L` followed by `A` comes out as `A` followed by `B`, and the chain
  // everyone calls A is the one that moved.
  const reserved = new Set();
  for (const chains of writing) {
    for (const chain of chains) if (owns(chain)) reserved.add(chain.id);
  }

  const used = new Set();
  const free = () => CHAIN_POOL.split('').find((c) => !used.has(c) && !reserved.has(c))
    || CHAIN_POOL.split('').find((c) => !used.has(c));

  return writing.map((chains) => {
    const map = new Map();
    for (const chain of chains) {
      const first = leading(chain);
      const keep = letter(first) && !used.has(first) && (owns(chain) || !reserved.has(first));
      const id = keep ? first : (free() || first || 'A');
      used.add(id);
      reserved.delete(id);
      map.set(chain.index, id);
    }
    return map;
  });
}

/**
 * @param {Array<{structure: Structure, matrix?: ArrayLike<number>,
 *                chainMatrices?: ArrayLike<number>[], mask?: Uint8Array}>} entries
 *   `chainMatrices` (indexed by chain) wins over `matrix`, so subunits that were
 *   moved independently are written where the user left them.
 * @param {{title?: string}} options
 * @returns {string} PDB text
 */
export function writePDB(entries, options = {}) {
  const lines = [];
  if (options.title) lines.push(padRight(`TITLE     ${options.title}`, 80));
  lines.push(padRight('REMARK   1 Written by proteinCAD', 80));

  let serial = 1;
  const p = [0, 0, 0];
  const chainMaps = chainIdsFor(entries);

  for (const [entryIndex, entry] of entries.entries()) {
    const s = entry.structure;
    const mask = entry.mask;
    const matrixFor = (chainIndex) =>
      (entry.chainMatrices && entry.chainMatrices[chainIndex]) || entry.matrix || null;
    const chainMap = chainMaps[entryIndex];

    let lastChainIndex = -1;
    for (let i = 0; i < s.atomCount; i++) {
      if (mask && !mask[i]) continue;
      const res = s.residueOfAtom(i);
      if (lastChainIndex >= 0 && res.chainIndex !== lastChainIndex) {
        lines.push(padRight(`TER   ${pad(serial++, 5)}`, 80));
      }
      lastChainIndex = res.chainIndex;

      applyMatrix(matrixFor(res.chainIndex), s.x[i], s.y[i], s.z[i], p);
      const element = elementSymbol(s.element[i]);
      const record = res.hetero || res.kind === Kind.WATER || res.kind === Kind.LIGAND || res.kind === Kind.ION
        ? 'HETATM' : 'ATOM  ';
      const line =
        record +
        pad(serial % 100000, 5) + ' ' +
        atomNameField(s.atomName[i], element) +
        padRight(s.altLoc[i] || ' ', 1) +
        pad(res.name.slice(0, 3), 3) + ' ' +
        padRight(chainMap.get(res.chainIndex) || 'A', 1) +
        pad(res.seq, 4) +
        padRight(res.insCode || ' ', 1) + '   ' +
        fixed(p[0], 8, 3) + fixed(p[1], 8, 3) + fixed(p[2], 8, 3) +
        fixed(s.occupancy[i], 6, 2) + fixed(s.bFactor[i], 6, 2) +
        '          ' +
        pad(element.toUpperCase(), 2) + '  ';
      lines.push(line);
      serial++;
    }
    if (lastChainIndex >= 0) lines.push(padRight(`TER   ${pad(serial++, 5)}`, 80));
  }

  lines.push('END');
  return lines.join('\n') + '\n';
}

/** Trigger a browser download of a text file. */
export function downloadText(filename, text, mime = 'chemical/x-pdb') {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
