// mmCIF (PDBx) reader.
//
// mmCIF is the format that matters for large assemblies -- the legacy PDB format
// cannot express more than 99999 atoms or two-character chain ids. This is a
// full CIF tokenizer (quoted values, semicolon text blocks, loops) but it only
// keeps the handful of categories the viewer needs, and streams `_atom_site`
// rows straight into the structure builder so that 500k-atom files stay cheap.

import { StructureBuilder, SS } from '../core/structure.js';

const WANTED = new Set(['struct', 'entry', 'struct_conf', 'struct_sheet_range', 'cell', 'entity']);

class Tokenizer {
  constructor(text) {
    this.text = text;
    this.pos = 0;
    this.value = '';
    this.quoted = false;
    this.pushedBack = false;
  }

  back() { this.pushedBack = true; }

  next() {
    if (this.pushedBack) { this.pushedBack = false; return true; }
    const text = this.text;
    const len = text.length;
    let p = this.pos;

    for (;;) {
      while (p < len) {
        const c = text.charCodeAt(p);
        if (c === 32 || c === 9 || c === 10 || c === 13) p++;
        else break;
      }
      if (p >= len) { this.pos = p; return false; }
      if (text.charCodeAt(p) === 35) { // '#' comment
        while (p < len && text.charCodeAt(p) !== 10) p++;
        continue;
      }
      break;
    }

    const c = text.charCodeAt(p);
    const atLineStart = p === 0 || text.charCodeAt(p - 1) === 10;

    if (c === 59 && atLineStart) { // ';' text block
      let start = p + 1;
      let scan = start;
      let out = '';
      for (;;) {
        let nl = text.indexOf('\n', scan);
        if (nl === -1) { out = text.slice(start); p = len; break; }
        if (text.charCodeAt(nl + 1) === 59) {
          out = text.slice(start, nl);
          p = nl + 2;
          break;
        }
        scan = nl + 1;
      }
      this.value = out.trim();
      this.quoted = true;
      this.pos = p;
      return true;
    }

    if (c === 39 || c === 34) { // quoted value
      const quote = c;
      let q = p + 1;
      for (;;) {
        q = text.indexOf(String.fromCharCode(quote), q);
        if (q === -1) { q = len; break; }
        const after = q + 1 >= len ? 32 : text.charCodeAt(q + 1);
        if (after === 32 || after === 9 || after === 10 || after === 13) break;
        q++;
      }
      this.value = text.slice(p + 1, q);
      this.quoted = true;
      this.pos = Math.min(q + 1, len);
      return true;
    }

    let q = p;
    while (q < len) {
      const cc = text.charCodeAt(q);
      if (cc === 32 || cc === 9 || cc === 10 || cc === 13) break;
      q++;
    }
    this.value = text.slice(p, q);
    this.quoted = false;
    this.pos = q;
    return true;
  }

  /** True when the current token opens a new tag/loop/block rather than a value. */
  isStructural() {
    if (this.quoted) return false;
    const v = this.value;
    return v.charCodeAt(0) === 95 /* _ */ ||
      v === 'loop_' || v === 'stop_' ||
      v.startsWith('data_') || v.startsWith('save_');
  }
}

function nullable(value, quoted) {
  if (!quoted && (value === '.' || value === '?')) return '';
  return value;
}

class Category {
  constructor() { this.tags = []; this.rows = []; }
  column(name) { return this.tags.indexOf(name); }
  first(name) {
    const i = this.tags.indexOf(name);
    return i < 0 || !this.rows.length ? '' : this.rows[0][i];
  }
}

export function parseCIF(text, name = 'structure') {
  const builder = new StructureBuilder(name);
  const s = builder.structure;
  s.format = 'cif';

  const categories = new Map();
  const tk = new Tokenizer(text);
  const atoms = { firstModel: null, modelCount: 0, count: 0 };

  const getCategory = (cat) => {
    let c = categories.get(cat);
    if (!c) { c = new Category(); categories.set(cat, c); }
    return c;
  };

  while (tk.next()) {
    const token = tk.value;

    if (!tk.quoted && token === 'loop_') {
      const tags = [];
      let category = '';
      while (tk.next()) {
        if (tk.value.charCodeAt(0) !== 95 || tk.quoted) { tk.back(); break; }
        const dot = tk.value.indexOf('.');
        category = dot < 0 ? tk.value.slice(1) : tk.value.slice(1, dot);
        tags.push(dot < 0 ? '' : tk.value.slice(dot + 1));
      }
      if (category === 'atom_site') {
        readAtomSiteLoop(tk, tags, builder, atoms);
      } else if (WANTED.has(category)) {
        const cat = getCategory(category);
        cat.tags = tags;
        readLoopRows(tk, tags.length, (row) => cat.rows.push(row));
      } else {
        readLoopRows(tk, tags.length, null);
      }
      continue;
    }

    if (!tk.quoted && token.charCodeAt(0) === 95) {
      const dot = token.indexOf('.');
      const category = dot < 0 ? token.slice(1) : token.slice(1, dot);
      const tag = dot < 0 ? '' : token.slice(dot + 1);
      if (!tk.next()) break;
      const value = nullable(tk.value, tk.quoted);
      if (WANTED.has(category)) {
        const cat = getCategory(category);
        if (!cat.rows.length) cat.rows.push([]);
        cat.tags.push(tag);
        cat.rows[0].push(value);
      }
      continue;
    }

    if (!tk.quoted && token.startsWith('data_') && !s.name.startsWith('data_')) {
      const id = token.slice(5).trim();
      if (id && (name === 'structure' || !name)) s.name = id;
    }
  }

  // Entity descriptions name the distinct molecules ("Flagellar hook protein
  // FlgE"); atoms point at them through label_entity_id.
  const entity = categories.get('entity');
  if (entity) {
    const idColumn = entity.column('id');
    const descriptionColumn = entity.column('pdbx_description');
    if (idColumn >= 0 && descriptionColumn >= 0) {
      for (const row of entity.rows) {
        if (row[idColumn] && row[descriptionColumn]) {
          builder.entityNames.set(row[idColumn], row[descriptionColumn]);
        }
      }
    }
  }

  const struct = categories.get('struct');
  if (struct) s.title = struct.first('title') || '';
  const entry = categories.get('entry');
  if (entry && entry.first('id')) s.name = name === 'structure' ? entry.first('id') : name;
  const cell = categories.get('cell');
  if (cell) {
    s.cell = {
      a: parseFloat(cell.first('length_a')) || 0,
      b: parseFloat(cell.first('length_b')) || 0,
      c: parseFloat(cell.first('length_c')) || 0,
      alpha: parseFloat(cell.first('angle_alpha')) || 0,
      beta: parseFloat(cell.first('angle_beta')) || 0,
      gamma: parseFloat(cell.first('angle_gamma')) || 0,
      spaceGroup: '',
    };
  }

  readConformation(categories.get('struct_conf'), builder);
  readSheets(categories.get('struct_sheet_range'), builder);

  s.modelCount = Math.max(1, atoms.modelCount);
  return builder.finish();
}

function readLoopRows(tk, width, emit) {
  if (width <= 0) return;
  let row = emit ? new Array(width) : null;
  let col = 0;
  while (tk.next()) {
    if (tk.isStructural()) { tk.back(); break; }
    if (row) row[col] = nullable(tk.value, tk.quoted);
    col++;
    if (col === width) {
      if (row) { emit(row); row = new Array(width); }
      col = 0;
    }
  }
}

function readAtomSiteLoop(tk, tags, builder, atoms) {
  const idx = (t) => tags.indexOf(t);
  const cGroup = idx('group_PDB');
  const cId = idx('id');
  const cSymbol = idx('type_symbol');
  const cAtom = idx('auth_atom_id') >= 0 ? idx('auth_atom_id') : idx('label_atom_id');
  const cAlt = idx('label_alt_id');
  const cComp = idx('auth_comp_id') >= 0 ? idx('auth_comp_id') : idx('label_comp_id');
  const cAsym = idx('auth_asym_id') >= 0 ? idx('auth_asym_id') : idx('label_asym_id');
  const cSeq = idx('auth_seq_id') >= 0 ? idx('auth_seq_id') : idx('label_seq_id');
  const cIns = idx('pdbx_PDB_ins_code');
  const cX = idx('Cartn_x'), cY = idx('Cartn_y'), cZ = idx('Cartn_z');
  const cOcc = idx('occupancy');
  const cB = idx('B_iso_or_equiv');
  const cModel = idx('pdbx_PDB_model_num');
  const cEntity = idx('label_entity_id');

  const width = tags.length;
  const row = new Array(width);
  let col = 0;

  while (tk.next()) {
    if (tk.isStructural()) { tk.back(); break; }
    row[col] = nullable(tk.value, tk.quoted);
    col++;
    if (col < width) continue;
    col = 0;

    const model = cModel >= 0 ? row[cModel] : '1';
    if (atoms.firstModel === null) atoms.firstModel = model;
    if (model !== atoms.firstModel) {
      if (atoms.modelCount < 2) atoms.modelCount = 2;
      continue;
    }
    const alt = cAlt >= 0 ? row[cAlt] : '';
    if (alt && alt !== 'A' && alt !== '1') continue;

    const seq = cSeq >= 0 ? parseInt(row[cSeq], 10) : 0;
    builder.addAtom({
      serial: cId >= 0 ? parseInt(row[cId], 10) || atoms.count + 1 : atoms.count + 1,
      name: cAtom >= 0 ? row[cAtom] : '',
      altLoc: alt,
      resName: cComp >= 0 ? row[cComp] : 'UNK',
      chainId: (cAsym >= 0 ? row[cAsym] : 'A') || 'A',
      resSeq: Number.isFinite(seq) ? seq : 0,
      insCode: (cIns >= 0 && row[cIns]) ? row[cIns] : ' ',
      x: +row[cX], y: +row[cY], z: +row[cZ],
      occupancy: cOcc >= 0 ? (parseFloat(row[cOcc]) || 0) : 1,
      bFactor: cB >= 0 ? (parseFloat(row[cB]) || 0) : 0,
      element: cSymbol >= 0 ? row[cSymbol] : '',
      hetero: cGroup >= 0 ? row[cGroup] === 'HETATM' : false,
      entityId: cEntity >= 0 ? row[cEntity] : '',
    });
    atoms.count++;
    if (atoms.modelCount === 0) atoms.modelCount = 1;
  }
}

function readConformation(cat, builder) {
  if (!cat || !cat.rows.length) return;
  const type = cat.column('conf_type_id');
  const chain = cat.column('beg_auth_asym_id') >= 0 ? cat.column('beg_auth_asym_id') : cat.column('beg_label_asym_id');
  const beg = cat.column('beg_auth_seq_id') >= 0 ? cat.column('beg_auth_seq_id') : cat.column('beg_label_seq_id');
  const end = cat.column('end_auth_seq_id') >= 0 ? cat.column('end_auth_seq_id') : cat.column('end_label_seq_id');
  if (chain < 0 || beg < 0 || end < 0) return;
  for (const row of cat.rows) {
    const t = (row[type] || '').toUpperCase();
    let ss = SS.COIL;
    if (t.startsWith('HELX')) ss = SS.HELIX;
    else if (t.startsWith('STRN')) ss = SS.SHEET;
    else if (t.startsWith('TURN')) ss = SS.TURN;
    else continue;
    builder.addSecondaryStructure(row[chain], parseInt(row[beg], 10), parseInt(row[end], 10), ss);
  }
}

function readSheets(cat, builder) {
  if (!cat || !cat.rows.length) return;
  const chain = cat.column('beg_auth_asym_id') >= 0 ? cat.column('beg_auth_asym_id') : cat.column('beg_label_asym_id');
  const beg = cat.column('beg_auth_seq_id') >= 0 ? cat.column('beg_auth_seq_id') : cat.column('beg_label_seq_id');
  const end = cat.column('end_auth_seq_id') >= 0 ? cat.column('end_auth_seq_id') : cat.column('end_label_seq_id');
  if (chain < 0 || beg < 0 || end < 0) return;
  for (const row of cat.rows) {
    builder.addSecondaryStructure(row[chain], parseInt(row[beg], 10), parseInt(row[end], 10), SS.SHEET);
  }
}
