// The right-hand inspector: selection, sequence, transform, measure, display.
//
// Each section owns a small update() and subscribes to the events it cares
// about, so typing in the selection box is never interrupted by a repaint
// somewhere else in the panel.

import { el, clear, fixed } from './dom.js';
import { Kind, SS, residueLetter } from '../core/structure.js';
import { entityColor, hexString } from '../render/colors.js';
import { SelectionError } from '../core/selection.js';

const SS_CLASS = { [SS.HELIX]: 'h', [SS.SHEET]: 'e' };

export function createInspector(app, container) {
  container.append(
    selectionSection(app),
    componentsSection(app),
    sequenceSection(app),
    transformSection(app),
    measureSection(app),
    displaySection(app)
  );
}

function section(title, ...children) {
  return el('div.section', null, el('h3', null, title), ...children);
}

/* ------------------------------------------------------------- selection */

function selectionSection(app) {
  const input = el('input.sel-input', {
    type: 'text',
    placeholder: 'chain A and resi 10-40',
    spellcheck: 'false',
    title: 'Selection expression. Try: protein, byres (within 4.5 of ligand), ss h, b > 70',
  });
  const error = el('div.sel-error');
  const summary = el('div.kv');
  const detail = el('div.kv');

  const run = () => {
    error.textContent = '';
    const text = input.value.trim();
    if (!text) return app.clearSelection();
    try {
      const count = app.selectExpression(text);
      if (count === 0) error.textContent = 'Nothing matched.';
    } catch (e) {
      error.textContent = e instanceof SelectionError ? e.message : String(e.message || e);
    }
  };

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); run(); }
    event.stopPropagation();
  });

  const button = (label, title, onclick) => el('button.btn.small', { title, onclick }, label);

  const node = section('Selection',
    el('div.row', null, input),
    error,
    el('div.row.tight', null,
      button('Apply', 'Run the expression', run),
      button('Focus', 'Zoom to the selection (F)', () => app.frameSelection()),
      button('Isolate', 'Hide everything else', () => app.isolateSelection()),
      button('Hide', 'Hide the selected chains', () => app.hideSelection()),
      button('Invert', 'Invert the selection', () => app.invertSelection()),
      button('Show all', 'Unhide everything', () => app.showAll()),
      button('Clear', 'Clear the selection (Esc)', () => { input.value = ''; app.clearSelection(); })
    ),
    summary,
    detail
  );

  const update = () => {
    clear(summary);
    clear(detail);
    const parts = app.describeSelection();
    if (!parts.length) {
      summary.appendChild(el('dt', null, 'Selected'));
      summary.appendChild(el('dd', { class: 'muted' }, 'nothing'));
      return;
    }
    let atoms = 0, residues = 0;
    const chains = new Set();
    for (const part of parts) {
      atoms += part.atoms;
      residues += part.residues;
      for (const c of part.chains) chains.add(`${part.view.structure.name}/${c}`);
    }
    summary.append(
      el('dt', null, 'Atoms'), el('dd', null, atoms.toLocaleString()),
      el('dt', null, 'Residues'), el('dd', null, residues.toLocaleString()),
      el('dt', null, 'Chains'), el('dd', null, [...chains].join(' ') || '-')
    );

    // A single residue gets the full read-out.
    if (residues === 1) {
      const part = parts.find((p) => p.residues === 1);
      const residue = part.firstResidue;
      const structure = part.view.structure;
      let bSum = 0, n = 0;
      for (let i = residue.start; i <= residue.end; i++) { bSum += structure.bFactor[i]; n++; }
      detail.append(
        el('dt', null, 'Residue'), el('dd', null, structure.residueLabel(residue)),
        el('dt', null, 'Type'), el('dd', null, kindLabel(residue.kind)),
        el('dt', null, 'Structure'), el('dd', null, ssLabel(residue.ss)),
        el('dt', null, 'Mean B'), el('dd', null, fixed(bSum / Math.max(1, n), 1))
      );
    }
  };

  app.on('selection', update);
  app.on('structures', update);
  update();
  return node;
}

function kindLabel(kind) {
  return ({
    [Kind.PROTEIN]: 'amino acid', [Kind.NUCLEIC]: 'nucleotide', [Kind.WATER]: 'water',
    [Kind.ION]: 'ion', [Kind.LIGAND]: 'ligand',
  })[kind] || 'unknown';
}

function ssLabel(ss) {
  return ({ [SS.HELIX]: 'helix', [SS.SHEET]: 'strand', [SS.TURN]: 'turn' })[ss] || 'coil';
}

/* ------------------------------------------------------------ components */

/**
 * The distinct molecules in the scene, with how many copies of each. Doubles as
 * the legend for "Colour: protein / component", and clicking a row selects
 * every copy -- the quickest way to pull one protein out of an assembly.
 */
function componentsSection(app) {
  const body = el('div');
  const node = section('Components', body);

  const update = () => {
    clear(body);
    if (!app.views.length) {
      body.appendChild(el('div.hint', null, 'Nothing loaded.'));
      return;
    }
    const isLegend = app.settings.colorScheme === 'entity';
    // Duplicates share one parsed structure, so list each distinct one once.
    const seen = new Map();
    for (const view of app.views) {
      if (!seen.has(view.structure)) seen.set(view.structure, { view, instances: 0 });
      seen.get(view.structure).instances++;
    }

    for (const { view, instances } of seen.values()) {
      const entities = [...(view.structure.entities || [])]
        .sort((a, b) => b.atoms - a.atoms);
      if (!entities.length) continue;
      if (seen.size > 1 || instances > 1) {
        body.appendChild(el('div.hint', null,
          `${view.structure.name}${instances > 1 ? ` · ${instances} instances` : ''}`));
      }

      const selectedEntities = selectedEntitySet(view.structure, app.selectionMask(view));
      for (const entity of entities) {
        if (entity.kind === Kind.WATER && !view.showWater) continue;
        const selected = selectedEntities.has(entity.index);
        body.appendChild(el('div.chain-row', {
          class: selected ? 'selected' : '',
          title: `${entity.name}\n${entity.chains.length} chain${entity.chains.length === 1 ? '' : 's'} · ` +
            `${entity.residues.toLocaleString()} residues · chains ${entity.chains.slice(0, 12).join(' ')}` +
            `${entity.chains.length > 12 ? ' …' : ''}`,
          onclick: (event) => app.selectEntity(view, entity.index, event.shiftKey ? 'add' : 'replace'),
          ondblclick: () => { app.selectEntity(view, entity.index); app.frameSelection(); },
        },
          isLegend
            ? el('span.swatch', { style: { background: hexString(entityColor(entity.index)) } })
            : el('span.swatch', { style: { background: 'transparent', borderColor: 'transparent' } }),
          el('span.tree-name', null, entity.name),
          el('span.chain-meta', null, `${entity.chains.length}×`)
        ));
      }
    }
    body.appendChild(el('div.hint', null,
      isLegend ? 'Colours match the view. Click to select every copy.'
        : 'Click to select every copy. Switch Colour to “Protein / component” to colour by these.'));
  };

  app.on('structures', update);
  app.on('selection', update);
  app.on('settings', update);
  update();
  return node;
}

/** Which entities the selection touches, in one pass over the residues. */
function selectedEntitySet(structure, mask) {
  const found = new Set();
  if (!mask) return found;
  for (const res of structure.residues) {
    if (found.has(res.entityIndex)) continue;
    for (let i = res.start; i <= res.end; i++) {
      if (mask[i]) { found.add(res.entityIndex); break; }
    }
  }
  return found;
}

/* -------------------------------------------------------------- sequence */

const MAX_SEQUENCE = 3000;

function sequenceSection(app) {
  const body = el('div');
  const node = section('Sequence', body);

  const update = () => {
    clear(body);
    const focus = focusChain(app);
    if (!focus) {
      body.appendChild(el('div.hint', null, 'Select a chain to see its sequence.'));
      return;
    }
    const { view, chain } = focus;
    const structure = view.structure;
    const mask = app.selectionMask(view);
    const total = chain.residueEnd - chain.residueStart + 1;
    if (total > MAX_SEQUENCE) {
      body.appendChild(el('div.hint', null, `Chain ${chain.id}: ${total.toLocaleString()} residues (too long to list).`));
      return;
    }

    const letters = [];
    for (let r = chain.residueStart; r <= chain.residueEnd; r++) {
      const residue = structure.residues[r];
      if (residue.kind !== Kind.PROTEIN && residue.kind !== Kind.NUCLEIC) continue;
      let selected = false;
      if (mask) {
        for (let i = residue.start; i <= residue.end; i++) if (mask[i]) { selected = true; break; }
      }
      letters.push(el('span', {
        class: `${SS_CLASS[residue.ss] || ''} ${selected ? 'sel' : ''}`.trim(),
        title: `${structure.residueLabel(residue)} · ${ssLabel(residue.ss)}`,
        onclick: (event) => {
          const next = new Uint8Array(structure.atomCount);
          for (let i = residue.start; i <= residue.end; i++) next[i] = 1;
          app.selectAtoms(view, next, event.shiftKey ? 'add' : 'replace');
        },
      }, residueLetter(residue.name)));
    }
    body.append(
      el('div.hint', null, `${structure.name} · chain ${chain.id} · ${letters.length} residues`),
      el('div.sequence', null, letters)
    );
  };

  app.on('selection', update);
  app.on('structures', update);
  update();
  return node;
}

function focusChain(app) {
  for (const [view, mask] of app.selection) {
    for (const chain of view.structure.chains) {
      for (let i = chain.atomStart; i <= chain.atomEnd; i++) {
        if (mask[i]) return { view, chain };
      }
    }
  }
  return null;
}

/* ------------------------------------------------------------- transform */

function transformSection(app) {
  const status = el('div.hint');
  const subject = el('div.kv');
  const tools = {};
  const targets = {};
  const fields = {};

  const setTool = (tool) => {
    if (tool === 'none') app.detachGizmo();
    else app.attachGizmo(tool);
    update();
  };

  const numberField = (key, onCommit) => el('input.num', {
    type: 'number', step: key.startsWith('r') ? 5 : 1, value: 0,
    onchange: () => onCommit(),
    onkeydown: (event) => { if (event.key === 'Enter') onCommit(); event.stopPropagation(); },
  });

  const commit = () => {
    const object = app.gizmo.object;
    if (!object) return;
    object.position.set(+fields.x.value || 0, +fields.y.value || 0, +fields.z.value || 0);
    object.rotation.set(
      THREE_DEG * (+fields.rx.value || 0),
      THREE_DEG * (+fields.ry.value || 0),
      THREE_DEG * (+fields.rz.value || 0)
    );
    object.updateMatrixWorld(true);
    app.markers.refresh();
    app.viewer.requestRender();
    app.emit('transform');
  };

  for (const key of ['x', 'y', 'z', 'rx', 'ry', 'rz']) fields[key] = numberField(key, commit);

  const arrayCount = el('input.num', { type: 'number', min: 2, max: 120, step: 1, value: 11 });
  const arrayAxis = el('select.rep-select', null,
    ...['z', 'x', 'y'].map((a) => el('option', { value: a }, `${a.toUpperCase()} axis`)));
  const arrayAbout = el('select.rep-select');

  const node = section('Move / rotate',
    el('div.row.tight', null,
      el('span.muted', null, 'Target'),
      targets.structure = el('button.btn.small', {
        title: 'Move the whole active structure',
        onclick: () => { app.setTransformTarget('structure'); update(); },
      }, 'Structure'),
      targets.chain = el('button.btn.small', {
        title: 'Move the selected chain and everything under its id',
        onclick: () => { app.setTransformTarget('chain'); update(); },
      }, 'Chain'),
      targets.volume = el('button.btn.small', {
        title: 'Move the design volume',
        onclick: () => { app.setTransformTarget('volume'); update(); },
      }, 'Volume')
    ),
    subject,
    el('div.row.tight', null,
      tools.none = el('button.btn.small', { onclick: () => setTool('none') }, 'Off'),
      tools.translate = el('button.btn.small', { title: 'Move (G)', onclick: () => setTool('translate') }, 'Move'),
      tools.rotate = el('button.btn.small', { title: 'Rotate (T)', onclick: () => setTool('rotate') }, 'Rotate'),
      el('button.btn.small', {
        title: 'Put everything back where the file had it',
        onclick: () => app.resetTransforms(),
      }, 'Reset')
    ),
    el('div.row.tight', null, el('span.muted.axis', null, 'pos'), fields.x, fields.y, fields.z),
    el('div.row.tight', null, el('span.muted.axis', null, 'rot°'), fields.rx, fields.ry, fields.rz),
    status,
    el('div.row.tight', null,
      el('span.muted', { title: 'Repeat the active structure around an axis' }, 'Ring of'),
      arrayCount, arrayAxis,
      el('span.muted', null, 'about'), arrayAbout,
      el('button.btn.small', {
        title: 'Place copies evenly around the axis, keeping this one where it is',
        onclick: () => {
          if (!app.activeView) return;
          const about = app.views[+arrayAbout.value] || app.activeView;
          app.arrayAbout(app.activeView, {
            count: +arrayCount.value, axis: arrayAxis.value, about,
          });
        },
      }, 'Array'),
      el('button.btn.small', {
        title: 'Add one more copy of the active structure',
        onclick: () => { if (app.activeView) app.duplicate(app.activeView); },
      }, 'Duplicate')
    )
  );

  const update = () => {
    const object = app.gizmo.object;
    const active = object ? app.gizmo.mode : 'none';
    for (const [key, button] of Object.entries(tools)) button.classList.toggle('on', key === active);
    for (const [key, button] of Object.entries(targets)) {
      button.classList.toggle('on', key === app.transformTarget);
    }

    targets.volume.disabled = !app.volume;

    clear(subject);
    const view = app.activeView;
    const chain = view ? app.selectedChainOf(view) : null;
    const moving = app.transformTarget === 'volume'
      ? (app.volume ? `design volume (${app.volume.shape})` : 'no volume placed')
      : !view ? '—'
        : app.transformTarget === 'chain'
          ? (chain ? `chain ${chain.id} + its ligands` : 'no single chain selected')
          : 'whole structure';
    subject.append(
      el('dt', null, 'Active'), el('dd', null, view ? view.label : '—'),
      el('dt', null, 'Moving'), el('dd', null, moving)
    );

    if (!app.views.length) status.textContent = 'Load a structure first.';
    else if (app.transformTarget === 'volume' && !app.volume) {
      status.textContent = 'Place a design volume first (Design tab).';
    } else if (app.transformTarget === 'chain' && !chain) {
      status.textContent = 'Select exactly one chain, or switch the target to Structure.';
    } else if (active === 'none') {
      status.textContent = 'Pick Move or Rotate, or use the ✥ handle on any row.';
    } else {
      status.textContent = 'Exported coordinates follow the handle.';
    }

    syncFields();

    const selectedAbout = arrayAbout.value;
    clear(arrayAbout);
    app.views.forEach((v, i) => arrayAbout.appendChild(
      el('option', { value: i, selected: String(i) === selectedAbout }, v.label)
    ));
  };

  // Keep the read-out live while dragging, without repainting the whole panel.
  const syncFields = () => {
    const object = app.gizmo.object;
    for (const key of Object.keys(fields)) fields[key].disabled = !object;
    if (!object) return;
    const editing = Object.values(fields).includes(document.activeElement);
    if (editing) return;
    fields.x.value = round(object.position.x);
    fields.y.value = round(object.position.y);
    fields.z.value = round(object.position.z);
    fields.rx.value = round(object.rotation.x / THREE_DEG);
    fields.ry.value = round(object.rotation.y / THREE_DEG);
    fields.rz.value = round(object.rotation.z / THREE_DEG);
  };

  app.on('selection', update);
  app.on('structures', update);
  app.on('transform', update);
  app.on('design', update);
  app.on('gizmo-move', syncFields);
  update();
  return node;
}

const THREE_DEG = Math.PI / 180;

function round(value) {
  return Math.round(value * 100) / 100;
}

/* --------------------------------------------------------------- measure */

function measureSection(app) {
  const list = el('div.measure-list');
  const toggle = el('button.btn.small', {
    title: 'Click two atoms to measure the distance between them',
    onclick: () => app.setMode(app.mode === 'measure' ? 'select' : 'measure'),
  }, 'Measure');

  const node = section('Measure',
    el('div.row.tight', null,
      toggle,
      el('button.btn.small', { onclick: () => app.markers.clear() }, 'Clear')
    ),
    list
  );

  const update = () => {
    toggle.classList.toggle('on', app.mode === 'measure');
    clear(list);
    const items = app.markers.describe();
    if (!items.length) {
      list.appendChild(el('div.hint', null,
        app.mode === 'measure' ? 'Click two atoms.' : 'No measurements.'));
      return;
    }
    for (const item of items) {
      list.appendChild(el('div.measure-row', { title: `${item.from}  →  ${item.to}` },
        el('span', null, `${fixed(item.distance, 2)} Å`),
        el('span.muted', null, shorten(item.from)),
        el('button.close', { onclick: () => app.markers.remove(item.index) }, '✕')
      ));
    }
  };

  app.on('measure', update);
  app.on('settings', update);
  app.on('transform', update);
  update();
  return node;
}

function shorten(label) {
  const parts = label.split(' / ');
  return parts.slice(1).join(' ');
}

/* --------------------------------------------------------------- display */

function displaySection(app) {
  const check = (label, key, title) => el('label.check', { title },
    el('input', {
      type: 'checkbox',
      checked: !!app.settings[key],
      onchange: (event) => app.updateSettings({ [key]: event.target.checked }),
    }),
    label
  );

  const slabToggle = el('input', {
    type: 'checkbox',
    onchange: (event) => app.updateSettings({
      slab: event.target.checked ? { near: 1 - slabRange.value / 100, far: 3 } : null,
    }),
  });
  const slabRange = el('input', {
    type: 'range', min: 0, max: 95, value: 0,
    oninput: () => {
      if (!slabToggle.checked) { slabToggle.checked = true; }
      app.updateSettings({ slab: { near: 1 - slabRange.value / 100, far: 3 } });
    },
  });

  return section('Display',
    el('div.row', null, check('Waters', 'showWater', 'Show water molecules'), check('Hydrogens', 'showHydrogens')),
    el('div.row', null, check('Depth cue', 'depthCue', 'Fade distant parts into the background'),
      check('Hover info', 'hoverPick', 'Identify atoms under the cursor')),
    el('div.row', null,
      el('span.muted', null, 'Background'),
      el('input', {
        type: 'color',
        value: hexString(app.settings.background),
        oninput: (event) => app.updateSettings({ background: parseInt(event.target.value.slice(1), 16) }),
      })
    ),
    el('div.row', null,
      el('span.muted', { title: 'Cut away the front of the scene to look inside' }, 'Clip'),
      slabToggle, slabRange
    )
  );
}
