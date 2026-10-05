// Checks for the pure-JS core (parsers, model, selection, bonds, export).
// These modules never import three.js, so they run straight in node:
//
//   node tools/check.mjs
//
// Files under data/cache are optional; download them with
//   curl -o data/cache/4hhb.pdb https://files.rcsb.org/download/4HHB.pdb

import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { parseStructure } from '../web/src/core/../io/load.js';
import { Kind, SS, KIND_NAMES } from '../web/src/core/structure.js';
import { assignSecondaryStructure } from '../web/src/core/secondary.js';
import { select, describeSelection, countMask } from '../web/src/core/selection.js';
import { getBonds } from '../web/src/core/bonds.js';
import { writePDB, chainIdsFor } from '../web/src/io/write.js';
import {
  buildContigs, buildEsm3Spec, buildJobSpec, chainMask, contigsFor, cropSpans,
  ESM3_MODE_NEEDS, motifContigs, partialContigs,
  residuesForVolume, summariseSpec,
} from '../web/src/design/spec.js';
import { Ccp4Reader, Ccp4Error, parseCCP4 } from '../web/src/io/ccp4.js';
import { DensityMap } from '../web/src/core/density.js';
import { emdbId, emdbNumber, mapUrl, readEbiEntry } from '../web/src/io/emdb.js';
import {
  angleList, detectAxis, expectedPeriod, scan, splitComponents,
} from '../web/src/core/landscape.js';
import { descriptors } from '../web/src/core/landscape-descriptors.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');

let passed = 0;
let failed = 0;
const skipped = [];

function check(label, condition, detail = '') {
  if (condition) { passed++; console.log(`  ok   ${label}${detail ? `  (${detail})` : ''}`); }
  else { failed++; console.log(`  FAIL ${label}${detail ? `  (${detail})` : ''}`); }
}

function load(relative) {
  const path = join(root, relative);
  if (!existsSync(path)) { skipped.push(relative); return null; }
  const text = readFileSync(path, 'utf8');
  const t0 = performance.now();
  const s = parseStructure(text, relative);
  s.parseMs = performance.now() - t0;
  return s;
}

function section(name) { console.log(`\n${name}`); }

// --- crambin: small, single chain, has HELIX/SHEET records ------------------
section('1crn.pdb  (crambin, PDB format)');
const crn = load('data/samples/1crn.pdb');
if (crn) {
  check('atom count', crn.atomCount === 327, `${crn.atomCount} atoms in ${crn.parseMs.toFixed(1)} ms`);
  check('residue count', crn.residueCount === 46, `${crn.residueCount} residues`);
  check('single chain A', crn.chainCount === 1 && crn.chains[0].id === 'A');
  check('chain is protein', crn.chains[0].kind === Kind.PROTEIN);
  check('sequence starts TTCCPS', crn.chainSequence(crn.chains[0]).startsWith('TTCCPS'),
    crn.chainSequence(crn.chains[0]).slice(0, 12));
  check('backbone atoms cached', crn.residues.every((r) => r.ca >= 0 && r.n >= 0 && r.c >= 0));
  check('chain is fully linked', crn.residues.slice(0, -1).every((r) => r.linkNext));

  const fromRecords = crn.residues.map((r) => r.ss);
  const helixFromFile = fromRecords.filter((v) => v === SS.HELIX).length;
  check('HELIX records applied', helixFromFile > 0, `${helixFromFile} helix residues from file`);

  // Recompute with the built-in DSSP and compare with the deposited records.
  assignSecondaryStructure(crn, { force: true });
  const computed = crn.residues.map((r) => r.ss);
  let agree = 0;
  for (let i = 0; i < computed.length; i++) if (computed[i] === fromRecords[i]) agree++;
  const pct = (100 * agree) / computed.length;
  check('computed SS agrees with records', pct > 70, `${pct.toFixed(0)}% of residues agree`);
  const helices = computed.filter((v) => v === SS.HELIX).length;
  const sheets = computed.filter((v) => v === SS.SHEET).length;
  check('computed SS finds helix and sheet', helices > 5 && sheets > 1, `${helices} helix, ${sheets} sheet`);

  const bonds = getBonds(crn);
  check('bonds perceived', bonds.count > 320 && bonds.count < 360, `${bonds.count} bonds`);
  const disulfide = [];
  for (let k = 0; k < bonds.count; k++) {
    const a = bonds.a[k], b = bonds.b[k];
    if (crn.atomName[a] === 'SG' && crn.atomName[b] === 'SG') disulfide.push([a, b]);
  }
  check('3 disulfide bridges found', disulfide.length === 3, `${disulfide.length}`);

  // Round trip through the writer.
  const text = writePDB([{ structure: crn }]);
  const again = parseStructure(text, 'roundtrip.pdb');
  check('round trip keeps atoms', again.atomCount === crn.atomCount, `${again.atomCount}`);
  let maxDelta = 0;
  for (let i = 0; i < crn.atomCount; i++) {
    maxDelta = Math.max(maxDelta, Math.abs(crn.x[i] - again.x[i]), Math.abs(crn.y[i] - again.y[i]));
  }
  check('round trip keeps coordinates', maxDelta < 0.0011, `max delta ${maxDelta.toFixed(4)} A`);

  // Round trip with a transform applied (translate +10 in x).
  const moved = writePDB([{ structure: crn, matrix: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 10, 0, 0, 1] }]);
  const movedStructure = parseStructure(moved, 'moved.pdb');
  check('transform applied on export', Math.abs(movedStructure.x[0] - crn.x[0] - 10) < 0.002,
    `${crn.x[0].toFixed(3)} -> ${movedStructure.x[0].toFixed(3)}`);
}

// --- haemoglobin: four chains plus haem groups and waters -------------------
section('4hhb.pdb  (haemoglobin, 4 chains + ligands)');
const hhb = load('data/cache/4hhb.pdb');
if (hhb) {
  check('atoms parsed', hhb.atomCount > 4500, `${hhb.atomCount} atoms in ${hhb.parseMs.toFixed(1)} ms`);
  const polymer = hhb.chains.filter((c) => c.kind === Kind.PROTEIN);
  check('four protein chains', polymer.length === 4, polymer.map((c) => c.id).join(','));
  const hem = hhb.residues.filter((r) => r.name === 'HEM');
  check('four haem ligands', hem.length === 4 && hem.every((r) => r.kind === Kind.LIGAND));
  const iron = hhb.residues.filter((r) => r.name === 'HEM').map((r) => hhb.atomInResidue(r, 'FE'));
  check('haem iron element resolved', iron.every((i) => i >= 0 && hhb.element[i] === 26));
  const water = hhb.residues.filter((r) => r.kind === Kind.WATER).length;
  check('waters classified', water > 100, `${water} waters`);

  check('selection: chain A', countMask(select(hhb, 'chain A')) > 1000);
  check('selection: protein and not water', countMask(select(hhb, 'protein and not water')) > 4000);
  const nearHem = select(hhb, 'byres (within 4.5 of resn HEM) and protein');
  const d = describeSelection(hhb, nearHem);
  check('selection: byres within 4.5 of HEM', d.residues > 30 && d.residues < 120,
    `${d.residues} residues across chains ${d.chains.join(',')}`);
  check('selection: name CA', countMask(select(hhb, 'name CA')) === hhb.residues.filter((r) => r.ca >= 0).length);
  check('selection: b > 40 is a subset', countMask(select(hhb, 'b > 40')) < hhb.atomCount);
  check('selection: parse error reported', (() => {
    try { select(hhb, 'chain A and nonsense'); return false; } catch { return true; }
  })());

  // Entities: distinct molecules, from COMPND records.
  const alpha = hhb.entities.find((e) => /ALPHA/i.test(e.name));
  const beta = hhb.entities.find((e) => /BETA/i.test(e.name));
  check('COMPND names the two globins', !!alpha && !!beta,
    [alpha && alpha.name, beta && beta.name].join(' / '));
  check('alpha is chains A and C', alpha && alpha.chains.join(',') === 'A,C', alpha && alpha.chains.join(','));
  check('beta is chains B and D', beta && beta.chains.join(',') === 'B,D', beta && beta.chains.join(','));
  const haem = hhb.entities.find((e) => e.name === 'HEM');
  check('haem is its own component', !!haem && haem.chains.length === 4,
    haem && `${haem.chains.length} copies`);
  check('water is one component', hhb.entities.some((e) => e.name === 'Water'));
  check('every residue has an entity', hhb.residues.every((r) => hhb.entities[r.entityIndex] !== undefined));

  // Tier three: with the COMPND records stripped, identical sequences must
  // still group -- this is the path for predicted models and design output.
  const stripped = parseStructure(
    readFileSync(join(root, 'data/cache/4hhb.pdb'), 'utf8')
      .split('\n').filter((l) => !l.startsWith('COMPND')).join('\n'),
    'nocompnd.pdb'
  );
  const grouped = stripped.entities.filter((e) => e.kind === Kind.PROTEIN);
  check('sequence fallback finds two proteins', grouped.length === 2,
    grouped.map((e) => `${e.name} [${e.chains.join(',')}]`).join(' / '));
  check('sequence fallback groups A with C', grouped.some((e) => e.chains.join(',') === 'A,C'));
}

// --- B-DNA dodecamer: nucleic acid classification ---------------------------
section('1bna.pdb  (B-DNA dodecamer)');
const bna = load('data/cache/1bna.pdb');
if (bna) {
  const nucleic = bna.chains.filter((c) => c.kind === Kind.NUCLEIC);
  check('two nucleic chains', nucleic.length === 2, nucleic.map((c) => c.id).join(','));
  const residues = bna.residues.filter((r) => r.kind === Kind.NUCLEIC);
  check('24 nucleotides', residues.length === 24, `${residues.length}`);
  check('phosphate backbone found', residues.filter((r) => r.p >= 0).length >= 22);
  check('base anchor found', residues.every((r) => r.base >= 0));
  check('strands linked', nucleic.every((c) => {
    let links = 0;
    for (let r = c.residueStart; r < c.residueEnd; r++) if (bna.residues[r].linkNext) links++;
    return links === c.residueEnd - c.residueStart;
  }));
}

// --- ubiquitin in mmCIF -----------------------------------------------------
section('1ubq.cif  (mmCIF format)');
const ubq = load('data/cache/1ubq.cif');
if (ubq) {
  check('format detected as cif', ubq.format === 'cif');
  check('atom count', ubq.atomCount === 660, `${ubq.atomCount} atoms in ${ubq.parseMs.toFixed(1)} ms`);
  check('76 protein residues', ubq.residues.filter((r) => r.kind === Kind.PROTEIN).length === 76);
  check('title parsed', ubq.title.length > 10, ubq.title.slice(0, 48));
  check('struct_conf applied', ubq.residues.some((r) => r.ss === SS.HELIX) && ubq.residues.some((r) => r.ss === SS.SHEET));
  check('sequence is ubiquitin', ubq.chainSequence(ubq.chains[0]).startsWith('MQIFVKTLTGK'),
    ubq.chainSequence(ubq.chains[0]).slice(0, 16));
}

// --- design job spec --------------------------------------------------------
section('design spec (built from what is on screen)');
if (crn) {
  const hotspots = crn.residues.filter((r) => r.seq >= 22 && r.seq <= 25).map((r) => r.index);
  const spans = cropSpans(crn, hotspots, 10);
  check('crop is one span on chain A', spans.length === 1 && spans[0].chain === 'A',
    spans.map((s) => `${s.chain}${s.from}-${s.to}`).join(','));
  check('crop is smaller than the whole chain',
    spans[0].to - spans[0].from + 1 < crn.residueCount,
    `${spans[0].to - spans[0].from + 1} of ${crn.residueCount} residues`);
  check('crop covers the picked residues', spans[0].from <= 22 && spans[0].to >= 25);

  const wide = cropSpans(crn, hotspots, 30);
  check('a larger radius takes more', (wide[0].to - wide[0].from) >= (spans[0].to - spans[0].from),
    `10 Å: ${spans[0].to - spans[0].from + 1}, 30 Å: ${wide[0].to - wide[0].from + 1}`);

  check('contig string is RFdiffusion shaped',
    buildContigs(spans, 60, 90) === `A${spans[0].from}-${spans[0].to}/0 60-90`,
    buildContigs(spans, 60, 90));
  check('two target chains get a chain break',
    buildContigs([{ chain: 'A', from: 1, to: 50 }, { chain: 'B', from: 3, to: 40 }], 70, 70)
      === 'A1-50/0 B3-40/0 70-70');

  const spec = buildJobSpec({
    structure: crn, label: '1CRN', hotspots, cropRadius: 10,
    lengthMin: 60, lengthMax: 90, numDesigns: 3, model: 'mock',
  });
  check('spec names the hotspots', spec.target.hotspots.join(',') === 'A22,A23,A24,A25',
    spec.target.hotspots.join(','));
  check('spec carries a target PDB', spec.target.pdb.includes('ATOM') && spec.target.atoms > 50,
    `${spec.target.atoms} atoms`);
  const cropped = parseStructure(spec.target.pdb, 'crop.pdb');
  check('the cropped target re-reads', cropped.atomCount === spec.target.atoms,
    `${cropped.atomCount} atoms, ${cropped.residueCount} residues`);
  check('the crop matches the contig range',
    cropped.residues[0].seq === spans[0].from &&
    cropped.residues[cropped.residueCount - 1].seq === spans[0].to);

  // A transform on the structure must follow through to the job.
  const shifted = buildJobSpec({
    structure: crn, label: '1CRN', hotspots, cropRadius: 10, lengthMin: 60, lengthMax: 90,
    chainMatrices: [[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 25, 0, 0, 1]],
  });
  const moved = parseStructure(shifted.target.pdb, 'crop.pdb');
  check('the crop is written in scene coordinates',
    Math.abs(moved.x[0] - cropped.x[0] - 25) < 0.002,
    `${cropped.x[0].toFixed(2)} -> ${moved.x[0].toFixed(2)}`);

  check('volume converts to a residue count', residuesForVolume(133 * 80) === 80,
    `${residuesForVolume(133 * 80)} residues for 10,640 Å³`);
  check('no hotspots is an error', (() => {
    try { buildJobSpec({ structure: crn, label: 'x', hotspots: [] }); return false; } catch { return true; }
  })());

  // --- the other protocols --------------------------------------------------
  section('contigs for each RFdiffusion protocol');

  const motif = motifContigs(spans, 80, 120);
  check('motif scaffolding wraps the motif in flexible stretches',
    /^\d+-\d+\/A\d+-\d+\/\d+-\d+$/.test(motif), motif);
  {
    // The gaps exist to make the total land in the range asked for. Getting that
    // wrong is not an error anyone sees -- it is a design of the wrong size.
    const kept = motif.split('/').filter((p) => /^[A-Za-z]/.test(p))
      .reduce((n, p) => n + (+p.split('-')[1] - +p.slice(1).split('-')[0] + 1), 0);
    const pads = motif.split('/').filter((p) => /^\d/.test(p));
    const low = kept + pads.reduce((n, p) => n + +p.split('-')[0], 0);
    const high = kept + pads.reduce((n, p) => n + +p.split('-')[1], 0);
    check('and sizes them so the whole thing can land in the range asked for',
      low <= 80 + 1 && high >= 120 - 1, `${low}-${high} for 80-120`);
  }
  check('a motif on two chains gets a chain break between them',
    motifContigs([{ chain: 'A', from: 1, to: 10, runs: [[1, 10]] },
      { chain: 'B', from: 5, to: 9, runs: [[5, 9]] }], 60, 60)
      .includes('/0 '),
    motifContigs([{ chain: 'A', from: 1, to: 10, runs: [[1, 10]] },
      { chain: 'B', from: 5, to: 9, runs: [[5, 9]] }], 60, 60));

  // RFdiffusion refuses a partial-diffusion contig that is not exactly the
  // length of the input -- it has nowhere to diffuse an extra residue from --
  // so this is an invariant, not a preference.
  {
    const two = [{ chain: 'A', from: 1, to: 100, runs: [[1, 40], [45, 100]] },
      { chain: 'B', from: 1, to: 150, runs: [[1, 150]] }];
    const partial = partialContigs(two, ['A']);
    check('partial diffusion fixes the picked chain at its own length',
      partial === '96-96/0 B1-150', partial);
    // A fragment starting with a letter names residues, so it counts as the
    // span; one starting with a digit is a number of residues to generate, and
    // counts as itself. Reading them the same way is exactly the mistake that
    // makes a contig look right and be the wrong length.
    const total = partial.split(/[/\s]+/).filter(Boolean).filter((p) => p !== '0')
      .reduce((n, p) => {
        const [a, b] = p.replace(/^[A-Za-z]/, '').split('-');
        return n + (/^[A-Za-z]/.test(p) ? +b - +a + 1 : +a);
      }, 0);
    const input = two.reduce((n, s) => n + s.runs.reduce((m, [f, t]) => m + (t - f + 1), 0), 0);
    check('and the contig totals exactly the residues that were sent',
      total === input, `${total} contig residues, ${input} sent`);
    check('nothing picked diversifies all of it',
      partialContigs(two, []) === '96-96/0 150-150', partialContigs(two, []));
  }

  check('an unconditional monomer is just a length',
    contigsFor('monomer', [], 90, 110) === '90-110', contigsFor('monomer', [], 90, 110));
  check('a symmetric oligomer is too, because the length is the whole assembly',
    contigsFor('symmetry', spans, 200, 200) === '200-200');
  check('fold conditioning sends no contig — the scaffold files are the shape',
    contigsFor('scaffold', spans, 60, 90) === '');

  section('specs for each protocol');
  const mono = buildJobSpec({
    structure: crn, label: '1CRN', hotspots, mode: 'monomer',
    lengthMin: 90, lengthMax: 90, options: { steps: 30, guideScale: 2 },
  });
  check('a protocol that designs from nothing sends no target',
    mono.target.pdb === '' && mono.target.atoms === 0);
  check('and no hotspots, which would change which model runs',
    mono.target.hotspots.length === 0);
  check('the settings travel in run, named as the catalogue names them',
    mono.run.steps === 30 && mono.run.guideScale === 2, JSON.stringify(mono.run));
  check('the spec says which protocol it is', mono.mode === 'monomer');
  check('a monomer still needs a length', (() => {
    try {
      buildJobSpec({ structure: crn, label: 'x', hotspots, mode: 'monomer', lengthMin: 2, lengthMax: 1 });
      return false;
    } catch { return true; }
  })());

  const typed = buildJobSpec({
    structure: crn, label: '1CRN', hotspots, cropRadius: 10, lengthMin: 60, lengthMax: 90,
    contigs: '10-40/A22-25/10-40',
  });
  check('a typed contig wins over the generated one',
    typed.binder.contigs === '10-40/A22-25/10-40', typed.binder.contigs);
  check('and the generated one is still carried, so it can be put back',
    typed.binder.autoContigs.endsWith('/0 60-90'), typed.binder.autoContigs);
  check('an empty box means the generated one',
    buildJobSpec({ structure: crn, label: '1CRN', hotspots, cropRadius: 10,
      lengthMin: 60, lengthMax: 90, contigs: '   ' }).binder.contigs.endsWith('/0 60-90'));

  check('the preview survives a spec with no target',
    summariseSpec(mono).target.pdb.includes('nothing'), summariseSpec(mono).target.pdb);

  // --- the other engine ----------------------------------------------------
  // ESM3 is prompted with tracks rather than a contig, so almost nothing about
  // the spec is shared. What is shared is the question asked of the scene --
  // which residues did you point at -- and what the protocol does with them is
  // the whole difference between these cases.
  section('specs for the ESM3 engine');
  const scaffolded = buildEsm3Spec({
    structure: crn, label: '1CRN', hotspots, mode: 'motif', cropRadius: 10,
    lengthMin: 70, lengthMax: 70, options: { temperature: 0.7, plan: ['sequence:8'] },
  });
  check('the spec says which engine is to run it', scaffolded.engine === 'esm3');
  check('and it is still a backbone job, so stage two is unchanged',
    scaffolded.kind === 'binder');
  check('there is no contig, because the engine has none',
    scaffolded.binder.contigs === undefined, JSON.stringify(scaffolded.binder));
  check('the length range travels, since nothing else states the length',
    scaffolded.binder.lengthMin === 70 && scaffolded.binder.lengthMax === 70);
  check('the picked residues travel as the residues to keep',
    scaffolded.target.hotspots.length === hotspots.length);
  check('the settings travel in run, named as the catalogue names them',
    scaffolded.run.temperature === 0.7
    && JSON.stringify(scaffolded.run.plan) === '["sequence:8"]', JSON.stringify(scaffolded.run));
  check('a crop is sent, because the design is built onto this',
    scaffolded.target.atoms > 0 && scaffolded.target.atoms < crn.atomCount,
    `${scaffolded.target.atoms} of ${crn.atomCount}`);
  check('motif scaffolding needs residues to keep', (() => {
    try { buildEsm3Spec({ structure: crn, label: 'x', hotspots: [], mode: 'motif' }); return false; }
    catch { return true; }
  })());

  // The one that is not about a counterpart at all: the chain being inverse
  // folded is the design. Cropping it would ask for a sequence for a fragment
  // and then compare it against the fold of the whole thing.
  const inverse = buildEsm3Spec({
    structure: crn, label: '1CRN', hotspots, mode: 'inverse', cropRadius: 4,
  });
  check('inverse folding sends whole chains, not a crop around the site',
    inverse.target.atoms > scaffolded.target.atoms, `${inverse.target.atoms} atoms`);
  check('and sends no crop spans, because it did not crop',
    inverse.target.spans.length === 0);
  check('a radius it would have cropped by is ignored',
    buildEsm3Spec({ structure: crn, label: 'x', hotspots, mode: 'inverse', cropRadius: 30 })
      .target.atoms === inverse.target.atoms);

  const fromNothing = buildEsm3Spec({
    structure: crn, label: '1CRN', hotspots: [], mode: 'generate',
    lengthMin: 90, lengthMax: 90,
  });
  check('a design from nothing sends no structure',
    fromNothing.target.pdb === '' && fromNothing.target.atoms === 0);
  check('and the preview says so rather than reading as a failure',
    summariseSpec(fromNothing).target.pdb.includes('nothing'));
  check('structure prediction needs a sequence from somewhere', (() => {
    try { buildEsm3Spec({ structure: crn, label: 'x', hotspots: [], mode: 'predict' }); return false; }
    catch { return true; }
  })());
  check('and takes a typed one',
    buildEsm3Spec({ structure: crn, label: 'x', hotspots: [], mode: 'predict',
      options: { sequencePrompt: 'MKTAYIAKQ' } }).run.sequencePrompt === 'MKTAYIAKQ');
  check('a prompt that states the length excuses the range', (() => {
    try {
      buildEsm3Spec({ structure: crn, label: 'x', hotspots: [], mode: 'generate',
        lengthMin: 2, lengthMax: 1, options: { sequencePrompt: '____MKTAYIAKQ____' } });
      return true;
    } catch { return false; }
  })());

  check('whole-chain selection takes every atom of a picked chain',
    countMask(chainMask(crn, hotspots)) > 0
    && countMask(chainMask(crn, hotspots)) <= crn.atomCount);
  check('and nothing picked means the whole structure',
    countMask(chainMask(crn, [])) === countMask(chainMask(crn, hotspots)),
    'crambin is one chain, so these are the same selection');
}

// --- multi-character chain ids ----------------------------------------------
// mmCIF is happy with chains called `BL`; 7CGO has 219 of them. PDB has a single
// column for the chain, so the writer renames them -- and everything else in the
// spec has to use the new name or it describes a file that was never sent.
// RFdiffusion compounds it by reading a chain as one leading character:
// `int(subcon.split("-")[0][1:])` turns `BL20-266` into int("L20") and dies.
section('design spec with two-character chain ids');
{
  const lines = [];
  let serial = 1;
  for (const [chain, offset] of [['A', 0], ['B', 7]]) {
    for (let r = 20; r <= 34; r++) {
      // No residue 29: an unmodelled loop, as every experimental structure has.
      // 7CGO's chains jump 145 -> 157 and 283 -> 316.
      if (r === 29) continue;
      for (const [name, dx, dy] of [['N', 0, 0], ['CA', 1.0, 0.3], ['C', 2.0, 0], ['O', 2.2, 1.1]]) {
        lines.push('ATOM  ' + String(serial++).padStart(5) + ' ' + name.padEnd(4) + ' ALA '
          + chain + String(r).padStart(4) + '    '
          + ((r - 20) * 3.8 + dx).toFixed(3).padStart(8) + (offset + dy).toFixed(3).padStart(8)
          + (0).toFixed(3).padStart(8) + '  1.00  0.00          ' + name[0].padStart(2));
      }
    }
  }
  const pair = parseStructure(lines.join('\n') + '\nEND\n', 'pair.pdb');
  // Two characters, sharing a first letter, exactly as in the flagellar motor.
  pair.chains[0].id = 'BL';
  pair.chains[1].id = 'BM';

  const hotspots = pair.residues
    .filter((r) => r.chainIndex === 0 && r.seq >= 26 && r.seq <= 28)
    .map((r) => r.index);
  const spec = buildJobSpec({
    structure: pair, label: 'pair', hotspots, cropRadius: 12,
    lengthMin: 60, lengthMax: 100, numDesigns: 1,
  });

  const chainsInPdb = new Set();
  const residuesInPdb = new Set();
  for (const line of spec.target.pdb.split('\n')) {
    if (!line.startsWith('ATOM') && !line.startsWith('HETATM')) continue;
    chainsInPdb.add(line[21]);
    residuesInPdb.add(line[21] + line.slice(22, 26).trim());
  }

  check('both chains survive as distinct ids', chainsInPdb.size === 2,
    [...chainsInPdb].join(', '));
  check('the spec records what each chain was renamed to',
    JSON.stringify(spec.target.chainMap) === '{"BL":"B","BM":"A"}',
    JSON.stringify(spec.target.chainMap));

  const contigChains = [...spec.binder.contigs.matchAll(/([A-Za-z0-9])\d+-\d+\/0/g)].map((m) => m[1]);
  check('contigs name chains that are in the file sent',
    contigChains.length === 2 && contigChains.every((c) => chainsInPdb.has(c)),
    spec.binder.contigs);
  check('hotspots name residues that are in the file sent',
    spec.target.hotspots.length === 3 && spec.target.hotspots.every((h) => residuesInPdb.has(h)),
    spec.target.hotspots.join(','));

  check('RFdiffusion can parse the hotspots',
    spec.target.hotspots.every((h) => /^[A-Za-z0-9]\d+$/.test(h) && !Number.isNaN(parseInt(h.slice(1), 10))),
    spec.target.hotspots.join(','));

  // A numbering range is not a promise that every residue in it exists.
  // RFdiffusion asserts that it is -- `assert val in parsed_pdb["pdb_idx"]` --
  // so a span written end-to-end across an unmodelled loop is rejected.
  check('a gap splits the span into fragments', /\d-\d+\/[A-Za-z]/.test(spec.binder.contigs),
    spec.binder.contigs);
  const named = [];
  let sameChain = true;
  let ascending = true;
  for (const block of spec.binder.contigs.split(' ')) {
    const subcons = block.split('/').filter((p) => /^[A-Za-z]/.test(p));
    if (!subcons.length) continue;
    if (new Set(subcons.map((c) => c[0])).size !== 1) sameChain = false;
    const starts = subcons.map((c) => parseInt(c.slice(1).split('-')[0], 10));
    if (!starts.every((v, i) => i === 0 || starts[i - 1] < v)) ascending = false;
    for (const sub of subcons) {
      const [from, to] = sub.slice(1).split('-').map(Number);
      for (let n = from; n <= to; n++) named.push(sub[0] + n);
    }
  }
  check('every residue the contig names is in the file sent',
    named.length > 0 && named.every((r) => residuesInPdb.has(r)),
    `${named.length} residues named, ${named.filter((r) => !residuesInPdb.has(r)).length} absent`);
  // The two assertions in RFdiffusion's expand_sampled_mask.
  check('fragments in a block come from one chain', sameChain);
  check('fragments are in ascending order', ascending);
  check('the residue count skips the gaps', spec.target.residues === named.length,
    `${spec.target.residues} counted, ${named.length} named`);
}

// --- chain ids that are digits ----------------------------------------------
// A digit is legal in the chain column and mmCIF hands them out freely, but a
// chain id is also the first character of every "chain + residue number" label
// downstream, and `6316` cannot be split back into the two. RFdiffusion decides
// whether a contig fragment names a chain or a length to generate with exactly
// one test -- `subcon[0].isalpha()` -- so chain `6` turns `6316-319` into
// "generate between 6316 and 319 residues" and dies inside random.randint.
section('chain ids handed to a model');
{
  const letterOnly = (c) => /^[A-Za-z]$/.test(c);
  const lines = [];
  let serial = 1;
  for (const [chain, offset] of [['6L', 0], ['B', 7]]) {
    for (let r = 316; r <= 330; r++) {
      for (const [name, dx, dy] of [['N', 0, 0], ['CA', 1.0, 0.3], ['C', 2.0, 0], ['O', 2.2, 1.1]]) {
        lines.push('ATOM  ' + String(serial++).padStart(5) + ' ' + name.padEnd(4) + ' ALA '
          + chain[0] + String(r).padStart(4) + '    '
          + ((r - 316) * 3.8 + dx).toFixed(3).padStart(8) + (offset + dy).toFixed(3).padStart(8)
          + (0).toFixed(3).padStart(8) + '  1.00  0.00          ' + name[0].padStart(2));
      }
    }
  }
  const numbered = parseStructure(lines.join('\n') + '\nEND\n', 'numbered.pdb');
  numbered.chains[0].id = '6L';        // as an mmCIF asym id, truncating to `6`
  numbered.chains[1].id = 'B';

  const ids = chainIdsFor([{ structure: numbered }])[0];
  check('a chain whose name starts with a digit is renamed to a letter',
    /^[A-Za-z]$/.test(ids.get(0)), `6L -> ${ids.get(0)}`);
  check('a chain already named with a letter keeps it', ids.get(1) === 'B', ids.get(1));

  const hotspots = numbered.residues
    .filter((r) => r.chainIndex === 0 && r.seq >= 318 && r.seq <= 320)
    .map((r) => r.index);
  const spec = buildJobSpec({
    structure: numbered, label: 'numbered', hotspots, cropRadius: 12,
    lengthMin: 60, lengthMax: 100, numDesigns: 1,
  });

  // Mirrors RFdiffusion's own parse: a fragment is a chain only when it starts
  // with a letter, and everything else is a length range that has to be
  // satisfiable.
  const fragments = spec.binder.contigs.split(' ').flatMap((b) => b.split('/'))
    .filter((f) => f && f !== '0');
  const emptyRanges = fragments.filter((f) => {
    if (/^[A-Za-z]/.test(f)) return false;
    const [from, to] = f.split('-').map(Number);
    return !(from <= to);
  });
  check('every contig fragment is a chain or a satisfiable length',
    emptyRanges.length === 0, emptyRanges.join(', ') || spec.binder.contigs);
  check('contigs name the renamed chain, not the digit',
    !/(^| |\/)\d+-\d+\/0/.test(spec.binder.contigs), spec.binder.contigs);
  check('hotspots split back into a chain and a number',
    spec.target.hotspots.every((h) => /^[A-Za-z]\d+$/.test(h)),
    spec.target.hotspots.join(','));

  // A renamed chain must not take a letter that another chain is already
  // called, or the chain everyone knows as A is the one that moved.
  const mixed = { structure: { chains: [
    { index: 0, id: '6L' }, { index: 1, id: 'A' }, { index: 2, id: 'BL' }, { index: 3, id: 'B' },
  ] } };
  const settled = chainIdsFor([mixed])[0];
  check('a chain already called A keeps A', settled.get(1) === 'A', settled.get(1));
  check('and a chain already called B keeps B', settled.get(3) === 'B', settled.get(3));
  check('the renamed ones take what is left',
    new Set(settled.values()).size === 4
    && letterOnly(settled.get(0)) && letterOnly(settled.get(2)),
    [...settled.values()].join(', '));

  // A crop of one structure is a handful of chains, so letters never run out;
  // digits stay in the pool only for exporting an assembly with more than 52.
  const many = { structure: { chains: Array.from({ length: 60 }, (_, i) => ({ index: i, id: 'X' })) } };
  const wide = chainIdsFor([many])[0];
  check('digits are still used once the letters are gone',
    new Set(wide.values()).size === 60 && /\d/.test(wide.get(59)),
    `${new Set(wide.values()).size} distinct, last ${wide.get(59)}`);
}

// --- flagellar motor-hook complex: the large-assembly stress test -----------
section('7cgo.cif  (Salmonella flagellar motor-hook complex, stress test)');
const big = load('data/cache/7cgo.cif');
if (big) {
  check('parsed under 12 s', big.parseMs < 12000, `${big.atomCount} atoms in ${(big.parseMs / 1000).toFixed(2)} s`);
  check('many chains', big.chainCount > 10, `${big.chainCount} chains, ${big.residueCount} residues`);
  const kinds = {};
  for (const c of big.chains) kinds[KIND_NAMES[c.kind]] = (kinds[KIND_NAMES[c.kind]] || 0) + 1;
  check('chains classified', Object.keys(kinds).length >= 1, JSON.stringify(kinds));
  const t0 = performance.now();
  assignSecondaryStructure(big, { force: true });
  const ssMs = performance.now() - t0;
  const helix = big.residues.filter((r) => r.ss === SS.HELIX).length;
  const sheet = big.residues.filter((r) => r.ss === SS.SHEET).length;
  check('secondary structure under 8 s', ssMs < 8000, `${(ssMs / 1000).toFixed(2)} s, ${helix} helix / ${sheet} sheet`);
  const t1 = performance.now();
  const bonds = getBonds(big);
  check('bond perception under 12 s', performance.now() - t1 < 12000,
    `${bonds.count} bonds in ${((performance.now() - t1) / 1000).toFixed(2)} s`);

  // 219 chains, but only a handful of distinct proteins.
  check('mmCIF entities are named', big.entities.length > 1 && big.entities.every((e) => e.name),
    `${big.entities.length} components`);
  const hook = big.entities.find((e) => /FlgE/.test(e.name));
  const rod = big.entities.find((e) => /FlgG/.test(e.name));
  check('hook protein FlgE found', !!hook && hook.chains.length === 33,
    hook && `${hook.name} × ${hook.chains.length}`);
  check('rod protein FlgG found', !!rod && rod.chains.length === 24,
    rod && `${rod.name} × ${rod.chains.length}`);
  const copies = big.entities.reduce((sum, e) => sum + e.chains.length, 0);
  check('components cover every chain', copies === big.chainCount, `${copies} of ${big.chainCount}`);
}

/* ------------------------------------------------------------- CCP4 maps */

section('Density maps');

/**
 * A CCP4 file built to order.
 *
 * `order` is MAPC/MAPR/MAPS: which crystal axis the fast, medium and slow
 * stored axis is. Writing the data out in that order and asking the reader to
 * put it back is the only way to be sure the permutation is handled, and it has
 * to be, because EMD-25575 -- the D8-C4 rotor this was built for -- is stored
 * (3, 2, 1) and a map read as though it were (1, 2, 3) comes out transposed
 * about its diagonal, which on a symmetric particle looks almost right.
 */
function makeCCP4({ order = [1, 2, 3], size = [4, 5, 6], mode = 2, little = true,
                    value = (x, y, z) => x * 100 + y * 10 + z, cell = null,
                    origin = [0, 0, 0], starts = [0, 0, 0], magic = true } = {}) {
  // 4 for a mode this writer does not know, so a file carrying an unsupported
  // mode still gets a well-formed header for the reader to reject it on.
  const bytesPer = { 0: 1, 1: 2, 2: 4, 6: 2 }[mode] ?? 4;
  const stored = [size[order[0] - 1], size[order[1] - 1], size[order[2] - 1]];
  const [nc, nr, ns] = stored;
  const cellA = cell || [size[0] * 1, size[1] * 2, size[2] * 3];
  const buffer = new ArrayBuffer(1024 + nc * nr * ns * bytesPer);
  const view = new DataView(buffer);
  view.setInt32(0, nc, little); view.setInt32(4, nr, little); view.setInt32(8, ns, little);
  view.setInt32(12, mode, little);
  view.setInt32(16, starts[0], little); view.setInt32(20, starts[1], little);
  view.setInt32(24, starts[2], little);
  view.setInt32(28, size[0], little); view.setInt32(32, size[1], little);
  view.setInt32(36, size[2], little);
  view.setFloat32(40, cellA[0], little); view.setFloat32(44, cellA[1], little);
  view.setFloat32(48, cellA[2], little);
  view.setInt32(64, order[0], little); view.setInt32(68, order[1], little);
  view.setInt32(72, order[2], little);
  view.setFloat32(196, origin[0], little); view.setFloat32(200, origin[1], little);
  view.setFloat32(204, origin[2], little);
  if (magic) {
    new Uint8Array(buffer, 208, 4).set([0x4d, 0x41, 0x50, 0x20]);  // 'MAP '
    view.setUint8(212, little ? 0x44 : 0x11);
  }
  let at = 1024;
  const index = [0, 0, 0];
  for (let s = 0; s < ns; s++) {
    for (let r = 0; r < nr; r++) {
      for (let c = 0; c < nc; c++) {
        index[order[0] - 1] = c; index[order[1] - 1] = r; index[order[2] - 1] = s;
        const v = value(index[0], index[1], index[2]);
        if (mode === 2) view.setFloat32(at, v, little);
        else if (mode === 1) view.setInt16(at, v, little);
        else if (mode === 6) view.setUint16(at, v, little);
        else view.setInt8(at, v);
        at += bytesPer;
      }
    }
  }
  return buffer;
}

function worstVoxel(map, want) {
  let worst = 0;
  let where = '';
  for (let z = 0; z < map.nz; z++) {
    for (let y = 0; y < map.ny; y++) {
      for (let x = 0; x < map.nx; x++) {
        const got = map.field[z * map.nx * map.ny + y * map.nx + x];
        const diff = Math.abs(got - want(x, y, z));
        if (diff > worst) { worst = diff; where = `(${x},${y},${z}) ${got} vs ${want(x, y, z)}`; }
      }
    }
  }
  return { worst, where };
}

{
  const unique = (x, y, z) => x * 100 + y * 10 + z;
  let wrong = 0;
  for (const order of [[1, 2, 3], [3, 2, 1], [2, 1, 3], [3, 1, 2], [1, 3, 2], [2, 3, 1]]) {
    const map = parseCCP4(makeCCP4({ order, value: unique }));
    const { worst } = worstVoxel(map, unique);
    const dims = `${map.nx}x${map.ny}x${map.nz}`;
    if (worst > 1e-6 || dims !== '4x5x6') wrong++;
  }
  check('all six MAPC/MAPR/MAPS orders put every voxel back where it belongs',
    wrong === 0, `${6 - wrong} of 6`);

  const transposed = parseCCP4(makeCCP4({ order: [3, 2, 1], value: unique }));
  check('a (3,2,1) map is not silently read as (1,2,3)',
    transposed.field[1] === unique(1, 0, 0), `got ${transposed.field[1]}, want 100`);
  check('and its voxel sizes follow the crystal axes, not the stored ones',
    Math.abs(transposed.voxel[0] - 1) < 1e-6 && Math.abs(transposed.voxel[1] - 2) < 1e-6
    && Math.abs(transposed.voxel[2] - 3) < 1e-6, transposed.voxel.join(', '));
}

{
  // Fed in awkward pieces: a header split in two, rows split across chunks.
  const buffer = makeCCP4({ order: [3, 2, 1], size: [7, 5, 6] });
  const bytes = new Uint8Array(buffer);
  const whole = parseCCP4(buffer);
  let worstStreamed = 0;
  for (const chunk of [1, 3, 17, 1000, 1024, 1023]) {
    const reader = new Ccp4Reader();
    for (let at = 0; at < bytes.length; at += chunk) {
      reader.push(bytes.subarray(at, Math.min(at + chunk, bytes.length)));
    }
    const streamed = reader.finish();
    for (let i = 0; i < whole.field.length; i++) {
      worstStreamed = Math.max(worstStreamed, Math.abs(streamed.field[i] - whole.field[i]));
    }
  }
  check('reading in chunks gives the same map as reading it whole',
    worstStreamed === 0, `worst difference ${worstStreamed}`);
}

{
  // Downsampling: a budget smaller than the map forces a stride, and a block
  // average has to preserve the mean exactly or every contour level shifts.
  const value = (x, y, z) => x + y + z;
  const big = parseCCP4(makeCCP4({ size: [8, 8, 8], value }));
  const small = parseCCP4(makeCCP4({ size: [8, 8, 8], value }), { budget: 100 });
  check('a map under the budget is kept at full resolution',
    big.stride === 1 && big.nx === 8, `stride ${big.stride}`);
  check('one over it is reduced by a whole-number stride',
    small.stride === 2 && small.nx === 4 && small.nz === 4,
    `stride ${small.stride}, ${small.nx}x${small.ny}x${small.nz}`);
  check('reducing preserves the mean, so contour levels do not move',
    Math.abs(small.mean - big.mean) < 1e-4, `${small.mean} vs ${big.mean}`);
  check('and the voxel grows with the stride, so the map keeps its size in space',
    Math.abs(small.voxel[0] - 2 * big.voxel[0]) < 1e-6,
    `${small.voxel[0]} vs ${big.voxel[0]}`);
  check('block averaging pulls the extremes in rather than pushing them out',
    small.min >= big.min - 1e-6 && small.max <= big.max + 1e-6,
    `[${small.min}, ${small.max}] inside [${big.min}, ${big.max}]`);
}

{
  const constant = () => 7;
  for (const [mode, label] of [[0, 'int8'], [1, 'int16'], [2, 'float32'], [6, 'uint16']]) {
    const map = parseCCP4(makeCCP4({ mode, value: constant }));
    check(`mode ${mode} (${label}) reads`, Math.abs(map.field[0] - 7) < 1e-6, `${map.field[0]}`);
  }
  const big = parseCCP4(makeCCP4({ little: false, value: (x, y, z) => x * 100 + y * 10 + z }));
  check('a big-endian map reads the same as a little-endian one',
    worstVoxel(big, (x, y, z) => x * 100 + y * 10 + z).worst < 1e-6);
  const headerless = parseCCP4(makeCCP4({ magic: false }));
  check('a file with no "MAP " marker is accepted if its dimensions read sensibly',
    headerless.nx === 4 && headerless.ny === 5 && headerless.nz === 6);
}

{
  // Where the map sits in space. Two places the origin can live, and a map put
  // at the wrong corner lines up with nothing.
  const fromField = parseCCP4(makeCCP4({ origin: [10, 20, 30] }));
  check('an ORIGIN record places the map', fromField.origin[0] === 10 && fromField.origin[2] === 30,
    fromField.origin.join(', '));
  const fromStart = parseCCP4(makeCCP4({ starts: [2, 3, 4] }));
  check('and NxSTART does when ORIGIN is zero, scaled by the voxel',
    Math.abs(fromStart.origin[0] - 2 * 1) < 1e-6 && Math.abs(fromStart.origin[1] - 3 * 2) < 1e-6
    && Math.abs(fromStart.origin[2] - 4 * 3) < 1e-6, fromStart.origin.join(', '));
}

{
  const truncated = new Uint8Array(makeCCP4()).subarray(0, 1024 + 40);
  const reader = new Ccp4Reader();
  reader.push(truncated);
  const partial = reader.finish();
  check('a map that was cut short says so rather than contouring a hole quietly',
    partial.complete === false && partial.rowsRead < partial.rowsExpected,
    `${partial.rowsRead} of ${partial.rowsExpected} rows`);

  let refused = null;
  try { parseCCP4(new ArrayBuffer(64)); } catch (error) { refused = error; }
  check('a file too short to hold a header is refused', refused instanceof Ccp4Error,
    refused && refused.message.slice(0, 48));

  let badMode = null;
  try { parseCCP4(makeCCP4({ mode: 4 })); } catch (error) { badMode = error; }
  check('an unsupported mode is named rather than read as rubbish',
    badMode instanceof Ccp4Error && /complex/.test(badMode.message), badMode && badMode.message.slice(0, 54));
}

{
  const map = new DensityMap(parseCCP4(makeCCP4({ size: [6, 6, 6], value: (x, y, z) => x + y + z })), 'EMD-1');
  check('sigma and level convert back and forth',
    Math.abs(map.sigmaForLevel(map.levelForSigma(2.5)) - 2.5) < 1e-6);
  check('with no recommended level it falls back to a few sigma',
    Math.abs(map.defaultLevel() - map.levelForSigma(3)) < 1e-9);
  map.recommended = 4.25;
  check("and uses EMDB's level when the entry carried one", map.defaultLevel() === 4.25);
  check('occupancy falls as the level rises',
    map.occupancy(map.min) === 1 && map.occupancy(map.max + 1) === 0);
  check('the box it covers is the grid times the voxel',
    Math.abs(map.bounds().max[0] - 5 * map.voxel[0]) < 1e-6, map.bounds().max.join(', '));
}

/* ------------------------------------------------------------------- EMDB */

{
  const yes = ['EMD-25575', 'emd-25575', 'emd_25575', 'EMD25575', '25575', 'emd 25575'];
  const no = ['4HHB', '9N49', '1BNA', '1CRN', '', 'EMD-', 'abcd', '2575'];
  check('EMDB ids are recognised however they are typed',
    yes.every((t) => emdbId(t) === 'EMD-25575'),
    yes.map((t) => `${t}->${emdbId(t)}`).find((s) => !s.endsWith('EMD-25575')) || 'all');
  // The one that matters: a four-character token is a PDB id, and quietly
  // fetching a density map for it would be worse than asking.
  check('and a PDB id is never mistaken for one', no.every((t) => emdbId(t) === null),
    no.find((t) => emdbId(t) !== null) || 'none');
  check('the number is pulled out for the URLs', emdbNumber('EMD-25575') === '25575'
    && mapUrl('emd_25575').endsWith('EMD-25575/map/emd_25575.map.gz'), mapUrl('25575'));

  const entry = readEbiEntry({
    admin: { title: 'D8-C4 computationally-designed Rotor' },
    map: { contour_list: { contour: [{ level: '2.0' }, { level: '1.35', primary: true }] } },
    crossreferences: { pdb_list: { pdb_reference: [{ pdb_id: '7t02' }] } },
    structure_determination_list: {
      structure_determination: [{
        image_processing: [{ final_reconstruction: { resolution: { valueOf_: '5.9' } } }],
      }],
    },
  });
  check('the primary contour level is the one taken', entry.contour === 1.35, `${entry.contour}`);
  check('the title and any fitted model come with it',
    entry.title.startsWith('D8-C4') && entry.fitted[0] === '7t02');
  check('and the resolution, from wherever EBI nested it this time',
    entry.resolution === 5.9, `${entry.resolution}`);
  const empty = readEbiEntry({});
  check('an entry document with nothing useful in it is not an error',
    empty.contour === null && empty.title === '' && empty.fitted.length === 0);
}

/* ------------------------------------------------- the rotational landscape */

section('Rotational landscape');

// The geometric scan exists in two languages: here, because it runs in the
// browser on a static deployment, and in proteincad/landscape.py, because
// PyRosetta cannot run in a browser and the server backend needs the same
// scaffolding. Two implementations of one piece of arithmetic drift, so both
// suites compute the landscape of one committed structure and assert one
// committed curve.
{
  const fixturePath = join(root, 'tools/fixtures/landscape-d8c4.json');
  const pdbPath = join(root, 'tools/fixtures/landscape-d8c4.pdb');
  if (!existsSync(fixturePath) || !existsSync(pdbPath)) {
    skipped.push('tools/fixtures/landscape-d8c4');
  } else {
    const fixture = JSON.parse(readFileSync(fixturePath, 'utf8'));
    const structure = parseStructure(readFileSync(pdbPath, 'utf8'), 'landscape-d8c4.pdb');
    const { rotor, axle } = splitComponents(structure, fixture.rotor, fixture.axle);
    const axis = detectAxis(rotor, axle);

    check('the rotor and axle come out of the structure whole',
      rotor.count === 40 && axle.count === 160, `${rotor.count} and ${axle.count} atoms`);
    check('a D8 axle does not drag the measured axis off z',
      Math.abs(Math.abs(axis.direction[2]) - 1) < 1e-4,
      axis.direction.map((v) => v.toFixed(5)).join(', '));
    check('the folds agree with the Python',
      axis.rotorFold === fixture.axis.rotor_fold && axis.axleFold === fixture.axis.axle_fold,
      `C${axis.rotorFold} on C${axis.axleFold}`);
    const expected = expectedPeriod(axis.rotorFold, axis.axleFold);
    check('and so does the period symmetry forces',
      expected === fixture.axis.expected_period, `${expected}`);

    const points = scan(rotor, axle, axis, angleList(fixture.step));
    const byAngle = new Map(fixture.points.map((p) => [p.angle, p]));

    // Not bit-identical, and it cannot be: this side reads coordinates out of
    // a Float32Array -- the viewer stores them that way on purpose -- while the
    // Python parses the same file to full precision. Buried area is a count of
    // sample points that fall outside an occluder, so a coordinate a ten
    // millionth of an Angstrom out can flip one of them. The bar is that the
    // difference stays far below anything the curve is read for.
    let worstScore = 0;
    let worstBsa = 0;
    let clashDiffs = 0;
    for (const point of points) {
      const want = byAngle.get(point.angle);
      if (!want) { worstScore = Infinity; break; }
      worstScore = Math.max(worstScore, Math.abs(point.score - want.score));
      worstBsa = Math.max(worstBsa, Math.abs(point.bsa - want.bsa));
      if (point.clashes !== want.clashes) clashDiffs++;
    }
    const range = fixture.descriptors.range;
    check('every angle scores what the Python scored',
      worstScore < 0.01 * range,
      `worst ${worstScore.toExponential(2)} on a range of ${range}`);
    check('and buries the same area',
      worstBsa < 2.0, `worst ${worstBsa.toFixed(2)} square Angstroms`);
    check('and counts the same clashes', clashDiffs === 0, `${clashDiffs} angles differ`);

    const said = descriptors(points, expected);
    const differing = Object.keys(fixture.descriptors).filter((key) => {
      const a = fixture.descriptors[key];
      const b = said[key];
      if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) > 1e-3;
      return JSON.stringify(a) !== JSON.stringify(b);
    });
    check('and the curve is described the same way',
      differing.length === 0,
      differing.map((k) => `${k}: ${JSON.stringify(said[k])} vs ${JSON.stringify(fixture.descriptors[k])}`).join('; ') || 'all match');
    check('with the period symmetry forces, and known to be checkable',
      said.period === 45 && said.period_resolvable && said.period_matches_symmetry,
      `${said.period} degrees`);
  }
}

{
  // The period is the smallest turn that leaves the landscape looking the same,
  // measured by shifting. Reading it off the strongest frequency is wrong twice
  // over: a double-dipped well puts more power on the second harmonic, and a
  // sharp curve's harmonics alias back down below the Nyquist limit.
  const curve = (step, fn) => {
    const out = [];
    for (let i = 0; i < 360 / step; i++) {
      out.push({ angle: i * step, rise: 0, clashes: 0, score: fn(i * step) });
    }
    return out;
  };
  const sharp = (a) => Math.cos((a * 8 * Math.PI) / 180) + 0.4 * Math.cos((a * 16 * Math.PI) / 180);

  const fine = descriptors(curve(5, sharp), 45);
  check('a 45 degree period is found at 5 degree steps',
    fine.period === 45 && fine.period_matches_symmetry, `${fine.period}`);
  // The second harmonic is stronger than the fundamental here, which is what
  // breaks reading the period off the transform.
  const doubled = descriptors(curve(5, (a) => Math.cos((a * 16 * Math.PI) / 180)
    + 0.3 * Math.cos((a * 8 * Math.PI) / 180)), 45);
  check('a well with two dips in it does not halve the reported period',
    doubled.period === 45, `${doubled.period}, dominant order ${doubled.dominant_order}`);

  // 45 is not a whole number of 10s, so no shift ever compares like with like.
  const offGrid = descriptors(curve(10, sharp), 45);
  check('a period the sampling cannot land on is reported as unchecked, not failed',
    offGrid.period_resolvable === false && offGrid.period_matches_symmetry === false
    && /not a whole number/.test(offGrid.period_note), offGrid.period_note.slice(0, 56));
  const coarse = descriptors(curve(30, sharp), 45);
  check('and so is one the sampling is too coarse to resolve',
    coarse.period_resolvable === false && /cannot resolve|not a whole number/.test(coarse.period_note),
    coarse.period_note.slice(0, 56));

  const flat = descriptors(curve(10, () => 1), 0);
  check('a flat landscape claims no period at all', flat.period === 360, `${flat.period}`);
}

console.log(`\n${passed} passed, ${failed} failed${skipped.length ? `, skipped: ${skipped.join(', ')}` : ''}`);
process.exit(failed ? 1 : 0);
