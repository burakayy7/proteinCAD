// Turning what is on screen into a design job.
//
// The viewer's real contribution to a design run is the job spec: which
// residues to build against, how much target to send, how big the new chain
// should be. Getting that right by hand -- residue ranges, contig strings,
// trimming a 45,000-residue assembly down to something a GPU can hold -- is
// the tedious part, and it is exactly what a 3D editor can do well.
//
// No three.js here: this is pure geometry over the core model, so it runs in
// node and is covered by tools/check.mjs.

import { writePDB, chainIdsFor } from '../io/write.js';
import { Kind } from '../core/structure.js';
import { expandToResidues, withinDistance } from '../core/selection.js';

// Protein packs at about 1.35 g/cm3 and an average residue is ~110 Da, which
// works out at ~133 A^3 per residue. Useful for turning a drawn volume into a
// chain length, and for telling the user when a box is unrealistically small.
export const VOLUME_PER_RESIDUE = 133;

export function residuesForVolume(cubicAngstrom) {
  return Math.max(4, Math.round(cubicAngstrom / VOLUME_PER_RESIDUE));
}

/**
 * "A59" style labels, which is what RFdiffusion's hotspot argument wants.
 *
 * `chainIds` maps chain index to the name used in the PDB that accompanies these
 * labels. It matters because RFdiffusion reads a hotspot as one character of
 * chain followed by a number -- `[(i[0], int(i[1:])) for i in hotspot_res]` --
 * so a label like `BL203` is read as chain `B`, residue `L203`, and dies on the
 * int(). Passing the written names keeps the labels both parseable and true.
 */
export function hotspotLabels(structure, residueIndices, chainIds = null) {
  const labels = [];
  for (const index of residueIndices) {
    const res = structure.residues[index];
    if (!res) continue;
    const chain = (chainIds && chainIds.get(res.chainIndex))
      || structure.chains[res.chainIndex].id;
    labels.push(`${chain}${res.seq}`);
  }
  return labels;
}

/**
 * The stretch of target worth sending: every polymer residue with an atom
 * within `radius` of a hotspot residue, collapsed to one contiguous span per
 * chain. Spans rather than exact residue lists because that is what contig
 * strings express, and because a span keeps the fold intact instead of handing
 * the model a bag of disconnected fragments.
 */
export function cropSpans(structure, hotspotResidues, radius = 12, minResidues = 4) {
  const seed = new Uint8Array(structure.atomCount);
  for (const index of hotspotResidues) {
    const res = structure.residues[index];
    if (!res) continue;
    for (let i = res.start; i <= res.end; i++) seed[i] = 1;
  }
  const near = expandToResidues(structure, withinDistance(structure, seed, radius));

  const byChain = new Map();
  for (const res of structure.residues) {
    if (res.kind !== Kind.PROTEIN && res.kind !== Kind.NUCLEIC) continue;
    let hit = false;
    for (let i = res.start; i <= res.end; i++) if (near[i]) { hit = true; break; }
    if (!hit) continue;
    const chain = structure.chains[res.chainIndex];
    const span = byChain.get(chain.id);
    if (!span) {
      byChain.set(chain.id, {
        chain: chain.id, chainIndex: chain.index, from: res.seq, to: res.seq, residues: 1,
      });
    }
    else {
      span.from = Math.min(span.from, res.seq);
      span.to = Math.max(span.to, res.seq);
      span.residues++;
    }
  }
  // A neighbouring copy clipped by the radius contributes a residue or two,
  // which would become a dangling one-residue chain in the contig. Drop those.
  const kept = [...byChain.values()].filter((span) => span.residues >= minResidues);
  const spans = (kept.length ? kept : [...byChain.values()])
    .sort((a, b) => a.chain.localeCompare(b.chain));

  // The crop keeps everything between the ends, so work out what is actually
  // there. A span is a numbering range, and a numbering range is not a promise
  // that every residue in it exists: disordered loops are simply absent from an
  // experimental structure, and 7CGO's chains jump 145 -> 157 and 283 -> 316.
  const present = new Map(spans.map((s) => [s.chainIndex, []]));
  for (const res of structure.residues) {
    if (res.kind !== Kind.PROTEIN && res.kind !== Kind.NUCLEIC) continue;
    const seqs = present.get(res.chainIndex);
    if (!seqs) continue;
    const span = spans.find((s) => s.chainIndex === res.chainIndex);
    if (res.seq >= span.from && res.seq <= span.to) seqs.push(res.seq);
  }
  for (const span of spans) span.runs = consecutiveRuns(present.get(span.chainIndex));
  return spans;
}

/** [[20,145],[157,249]] from a list of residue numbers, gaps and all. */
export function consecutiveRuns(seqs) {
  const sorted = [...new Set(seqs)].sort((a, b) => a - b);
  const runs = [];
  for (const seq of sorted) {
    const last = runs[runs.length - 1];
    if (last && seq === last[1] + 1) last[1] = seq;
    else runs.push([seq, seq]);
  }
  return runs;
}

/** Atoms belonging to the cropped spans, for writing the trimmed target file. */
export function cropMask(structure, spans, options = {}) {
  const wanted = new Map(spans.map((s) => [s.chain, s]));
  const mask = new Uint8Array(structure.atomCount);
  for (const res of structure.residues) {
    const span = wanted.get(structure.chains[res.chainIndex].id);
    if (!span || res.seq < span.from || res.seq > span.to) continue;
    const polymer = res.kind === Kind.PROTEIN || res.kind === Kind.NUCLEIC;
    if (!polymer && !(options.includeLigands && (res.kind === Kind.LIGAND || res.kind === Kind.ION))) continue;
    for (let i = res.start; i <= res.end; i++) mask[i] = 1;
  }
  return mask;
}

/**
 * RFdiffusion contig: kept target spans, a chain break, then the length range
 * of the chain to generate. `A17-145/0 70-100` reads as "keep chain A residues
 * 17 to 145, start a new chain, build 70 to 100 residues".
 *
 * A span with gaps becomes several fragments in one block, `B20-145/B147-249/0`,
 * which is RFdiffusion's syntax for a fragmented receptor. It insists that every
 * residue number in a fragment exists in the PDB -- `assert val in
 * parsed_pdb["pdb_idx"]` -- so a range that spans an unmodelled loop is rejected
 * outright, naming only the first number it could not find.
 */
export function buildContigs(spans, lengthMin, lengthMax) {
  const kept = spans.map(keptBlock);
  const length = lengthMin === lengthMax ? `${lengthMin}-${lengthMin}` : `${lengthMin}-${lengthMax}`;
  return kept.length ? `${kept.join('/0 ')}/0 ${length}` : length;
}

/**
 * One chain's kept fragments, as a contig block.
 *
 * `pdbChain` where there is one: the contig has to name the chain as it appears
 * in the file being sent, not as the viewer knows it. RFdiffusion also reads the
 * chain as a single leading character -- `int(subcon[1:].split("-")[0])` -- so a
 * two-character name is not merely wrong, it is a crash.
 */
function keptBlock(span) {
  const chain = span.pdbChain || span.chain;
  return runsOf(span).map(([from, to]) => `${chain}${from}-${to}`).join('/');
}

function runsOf(span) {
  return span.runs && span.runs.length ? span.runs : [[span.from, span.to]];
}

function residuesIn(span) {
  return runsOf(span).reduce((n, [from, to]) => n + (to - from + 1), 0);
}

/**
 * Motif scaffolding: keep the picked residues exactly and build new protein
 * around them, `10-40/A163-181/10-40`.
 *
 * The flexible stretches are sized so the whole thing lands in the requested
 * length range: the motif is already spoken for, and what is left is shared out
 * between the gaps. One block per chain, because RFdiffusion asserts that the
 * fragments inside a block share a chain and ascend.
 */
export function motifContigs(spans, lengthMin, lengthMax) {
  if (!spans.length) return `${lengthMin}-${lengthMax}`;
  const motif = spans.reduce((n, s) => n + residuesIn(s), 0);
  const gaps = spans.reduce((n, s) => n + runsOf(s).length + 1, 0);
  const low = Math.max(0, Math.floor((lengthMin - motif) / gaps));
  const high = Math.max(low, Math.ceil((lengthMax - motif) / gaps));
  const pad = `${low}-${high}`;
  return spans.map((span) => {
    const chain = span.pdbChain || span.chain;
    const pieces = [pad];
    for (const [from, to] of runsOf(span)) pieces.push(`${chain}${from}-${to}`, pad);
    return pieces.join('/');
  }).join('/0 ');
}

/**
 * Partial diffusion: noise an existing structure partway and denoise it again.
 *
 * The contig has to come out exactly the length of the input -- RFdiffusion has
 * nowhere to diffuse an extra residue from, and says so -- so the chain being
 * diversified is written as a fixed length rather than a range, and everything
 * else is kept as it stands. `100-100/0 B1-150` is the README's own example of
 * diversifying a 100-residue binder against a 150-residue target.
 *
 * The chain that gets diversified is the one the picked residues are on. That is
 * the only thing the scene says about intent, and it is the right thing: you
 * load a design back in, click it, and it is the design that varies.
 */
export function partialContigs(spans, pickedChains) {
  const picked = new Set(pickedChains || []);
  const wanted = spans.some((s) => picked.has(s.pdbChain || s.chain))
    ? (s) => picked.has(s.pdbChain || s.chain)
    : () => true;           // nothing picked: diversify all of it
  return spans.map((span) => {
    const n = residuesIn(span);
    return wanted(span) ? `${n}-${n}` : keptBlock(span);
  }).join('/0 ');
}

/** The contig for a protocol, given what the scene has in it. */
export function contigsFor(mode, spans, lengthMin, lengthMax, pickedChains) {
  if (mode === 'monomer' || mode === 'symmetry') {
    return lengthMin === lengthMax ? `${lengthMin}-${lengthMin}` : `${lengthMin}-${lengthMax}`;
  }
  if (mode === 'motif') return motifContigs(spans, lengthMin, lengthMax);
  if (mode === 'partial') return partialContigs(spans, pickedChains);
  if (mode === 'scaffold') return '';   // the scaffold files describe the shape
  return buildContigs(spans, lengthMin, lengthMax);
}

/**
 * How each protocol treats the scene. Mirrors the mode table the server serves
 * from colab_worker.py; kept here as the little that has to be known before the
 * server answers, so building a spec never waits on a fetch.
 */
export const MODE_NEEDS = {
  binder: { target: 'required', hotspots: 'required' },
  motif: { target: 'required', hotspots: 'ignored' },
  monomer: { target: 'none', hotspots: 'ignored' },
  symmetry: { target: 'none', hotspots: 'ignored' },
  partial: { target: 'required', hotspots: 'ignored' },
  scaffold: { target: 'optional', hotspots: 'optional' },
};

/**
 * @param {{
 *   structure: Structure, label: string, chainMatrices?: number[][],
 *   hotspots: number[], cropRadius?: number, lengthMin: number, lengthMax: number,
 *   volume?: object|null, numDesigns?: number, seed?: number, model?: string,
 *   includeLigands?: boolean, mode?: string, contigs?: string, options?: object,
 * }} input
 */
export function buildJobSpec(input) {
  const {
    structure, label, chainMatrices = null, hotspots = [], cropRadius = 12,
    lengthMin = 60, lengthMax = 100, volume = null, numDesigns = 4, seed = 0,
    model = 'mock', includeLigands = false, mode = 'binder', contigs = '',
    options = {},
  } = input;

  const needs = MODE_NEEDS[mode] || MODE_NEEDS.binder;
  if (needs.hotspots === 'required' && !hotspots.length) {
    throw new Error('pick at least one target residue to build against');
  }
  if (needs.target === 'required' && !hotspots.length) {
    throw new Error(`${mode} needs some residues picked, to say which part of the structure to send`);
  }
  if (lengthMin < 4 || lengthMax < lengthMin) throw new Error('binder length range is not usable');

  // A protocol that designs from nothing sends nothing: an unconditional
  // monomer has no target, and hotspots would silently select RFdiffusion's
  // complex checkpoint -- a different model answering a different question.
  const wantsTarget = needs.target !== 'none' && hotspots.length > 0;

  let spans = [];
  let chainIds = new Map();
  let pdb = '';
  let atoms = 0;
  if (wantsTarget) {
    spans = cropSpans(structure, hotspots, cropRadius);
    if (!spans.length) throw new Error('no polymer found near the picked residues');

    const mask = cropMask(structure, spans, { includeLigands });
    // World coordinates, so whatever comes back is already positioned against
    // the target as the user has it arranged rather than in the file's own frame.
    const entry = { structure, mask, chainMatrices };
    // Ask for the names before writing, and use them everywhere the spec refers
    // to a chain. PDB has one column for the chain id, so a name like `BL` is
    // renamed on the way out; contigs and hotspots that still said `BL`
    // described a file that no longer existed.
    chainIds = chainIdsFor([entry])[0];
    for (const span of spans) span.pdbChain = chainIds.get(span.chainIndex) || span.chain;
    pdb = writePDB([entry], { title: `proteinCAD target crop: ${label}` });
    for (let i = 0; i < mask.length; i++) atoms += mask[i];
  }

  const labels = wantsTarget ? hotspotLabels(structure, hotspots, chainIds) : [];
  // Which chains were clicked on, which is a different question from whether
  // the hotspots get sent. Partial diffusion sends none and still has to know:
  // the chain you picked is the one that gets diversified.
  const picked = new Set(labels.map((text) => text[0]));
  // Typed by hand beats generated. The generated string is offered as the
  // starting point and is what most runs use, but every protocol past binder
  // design eventually needs a contig nothing can infer from a click.
  const auto = contigsFor(mode, spans, lengthMin, lengthMax, picked);

  return {
    version: 1,
    kind: 'binder',
    mode,
    model,
    target: {
      name: label,
      // Hotspots are the binder protocol's instrument; the others are not about
      // an interface, and sending them would change which model runs.
      hotspots: needs.hotspots === 'ignored' ? [] : labels,
      // What each chain was called in the viewer, against what it is called in
      // the PDB above -- so a design can be traced back to what it was built on.
      chainMap: Object.fromEntries(spans.map((s) => [s.chain, s.pdbChain])),
      spans,
      // Counted over the runs, not end-to-end: the gaps are not residues.
      residues: spans.reduce(
        (sum, s) => sum + (s.runs || [[s.from, s.to]])
          .reduce((n, [from, to]) => n + (to - from + 1), 0), 0),
      atoms,
      pdb,
    },
    binder: {
      lengthMin,
      lengthMax,
      contigs: String(contigs || '').trim() || auto,
      autoContigs: auto,
    },
    volume,
    // Everything RFdiffusion can be told, flat, exactly as the option catalogue
    // names it. The worker turns these into Hydra overrides from that same
    // table, so a setting the panel offers is a setting that gets passed.
    run: { numDesigns, seed, ...options },
  };
}

/**
 * How each ESM3 protocol treats the scene. Mirrors ESM3_MODES on the server, for
 * the same reason MODE_NEEDS does: building a spec must not wait on a fetch.
 *
 * `subject` is the one that is not about the scene at all. It says the structure
 * being sent *is* the molecule being redesigned, rather than something the
 * design sits against -- which decides whether the whole picked chain goes or
 * just a crop around the picked residues, and whether stage two joins the two.
 */
export const ESM3_MODE_NEEDS = {
  generate: { target: 'none', hotspots: 'ignored', subject: false },
  motif: { target: 'required', hotspots: 'required', subject: false },
  inverse: { target: 'required', hotspots: 'optional', subject: true },
  predict: { target: 'optional', hotspots: 'ignored', subject: true },
  resample: { target: 'required', hotspots: 'optional', subject: true },
};

/** Every atom of every chain that has a picked residue in it. */
export function chainMask(structure, hotspotResidues, options = {}) {
  const chains = new Set();
  for (const index of hotspotResidues) {
    const res = structure.residues[index];
    if (res) chains.add(res.chainIndex);
  }
  const all = chains.size === 0;
  const mask = new Uint8Array(structure.atomCount);
  for (const res of structure.residues) {
    if (!all && !chains.has(res.chainIndex)) continue;
    const polymer = res.kind === Kind.PROTEIN || res.kind === Kind.NUCLEIC;
    if (!polymer && !(options.includeLigands
      && (res.kind === Kind.LIGAND || res.kind === Kind.ION))) continue;
    for (let i = res.start; i <= res.end; i++) mask[i] = 1;
  }
  return mask;
}

/**
 * The same job, for the other engine.
 *
 * Separate from buildJobSpec rather than a branch inside it, because almost
 * nothing is shared: there is no contig, the length can come from a typed prompt
 * instead of the range, and what gets sent is a crop around the picked residues
 * for one protocol and whole chains for another. A single function trying to be
 * both would be a list of conditionals with no reader.
 *
 * @param {{
 *   structure: Structure, label: string, chainMatrices?: number[][],
 *   hotspots: number[], cropRadius?: number, lengthMin: number, lengthMax: number,
 *   numDesigns?: number, seed?: number, model?: string, includeLigands?: boolean,
 *   mode?: string, options?: object, volume?: object|null,
 * }} input
 */
export function buildEsm3Spec(input) {
  const {
    structure, label, chainMatrices = null, hotspots = [], cropRadius = 12,
    lengthMin = 60, lengthMax = 100, numDesigns = 4, seed = 0, model = 'mock',
    includeLigands = false, mode = 'generate', options = {}, volume = null,
  } = input;

  const needs = ESM3_MODE_NEEDS[mode] || ESM3_MODE_NEEDS.generate;
  const typed = String(options.sequencePrompt || '').trim();

  if (needs.hotspots === 'required' && !hotspots.length) {
    throw new Error('pick the residues to keep (Pick site, then click)');
  }
  if (needs.target === 'required' && !hotspots.length) {
    throw new Error(`${mode} needs a structure: pick some residues to say which one, `
      + 'and which part of it');
  }
  if (mode === 'predict' && !hotspots.length && !typed) {
    throw new Error('structure prediction needs a sequence: type one into the sequence '
      + 'prompt, or pick a structure to take it from');
  }
  if (!typed && !needs.subject && (lengthMin < 4 || lengthMax < lengthMin)) {
    throw new Error('design length range is not usable');
  }

  const wantsTarget = needs.target !== 'none' && hotspots.length > 0;
  let spans = [];
  let chainIds = new Map();
  let pdb = '';
  let atoms = 0;

  if (wantsTarget) {
    // A crop around the site for the protocol that builds *onto* a motif; whole
    // chains for the ones that redesign what they are given. Cropping a chain
    // you are inverse folding would ask for a sequence for a fragment and then
    // compare it against the fold of the whole thing.
    const mask = needs.subject
      ? chainMask(structure, hotspots, { includeLigands })
      : cropMask(structure, (spans = cropSpans(structure, hotspots, cropRadius)),
        { includeLigands });
    if (!needs.subject && !spans.length) {
      throw new Error('no polymer found near the picked residues');
    }
    const entry = { structure, mask, chainMatrices };
    chainIds = chainIdsFor([entry])[0];
    for (const span of spans) span.pdbChain = chainIds.get(span.chainIndex) || span.chain;
    pdb = writePDB([entry], { title: `proteinCAD ESM3 input: ${label}` });
    for (let i = 0; i < mask.length; i++) atoms += mask[i];
    if (!atoms) throw new Error('nothing was selected to send');
  }

  return {
    version: 1,
    kind: 'binder',
    engine: 'esm3',
    mode,
    model,
    target: {
      name: label,
      // Here these are the residues to *keep*, not a surface to bind. Same
      // field because it is the same question of the scene -- which residues
      // did you point at -- and the mode says what is done with them.
      hotspots: needs.hotspots === 'ignored' ? [] : hotspotLabels(structure, hotspots, chainIds),
      chainMap: Object.fromEntries(spans.map((s) => [s.chain, s.pdbChain])),
      spans,
      residues: spans.reduce(
        (sum, s) => sum + (s.runs || [[s.from, s.to]])
          .reduce((n, [from, to]) => n + (to - from + 1), 0), 0),
      atoms,
      pdb,
    },
    // Only load-bearing when nothing else says how long the design is: a typed
    // prompt states it, and the protocols that redesign a structure read it off
    // the structure.
    binder: { lengthMin, lengthMax },
    volume,
    run: { numDesigns, seed, ...options },
  };
}

/** A short, readable version of the spec for the preview box (no PDB blob). */
export function summariseSpec(spec) {
  const copy = JSON.parse(JSON.stringify(spec));
  // A protocol that designs from nothing has no target to summarise, and
  // `<0 atoms>` reads as a failure rather than as the point.
  copy.target.pdb = spec.target.pdb
    ? `<${spec.target.atoms} atoms, ${Math.round(spec.target.pdb.length / 1024)} kB>`
    : '<none: this protocol designs from nothing>';
  return copy;
}
