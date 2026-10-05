// The design panel: pick a site, draw a shape, send a job, load what comes
// back. The panel never talks to a model -- it produces a job spec and hands it
// to the server, which owns the queue and the runner.

import { el, clear, fixed } from './dom.js';
import { VOLUME_SHAPES } from '../render/volume.js';
import { summariseSpec } from '../design/spec.js';
import { downloadText } from '../io/write.js';
import * as api from '../api.js';
import * as auth from '../auth.js';

const STATUS_MARK = { queued: '○', running: '◐', done: '●', failed: '✕', cancelled: '–' };

export function createDesign(app, container) {
  container.append(accountSection(app), machineSection(app), siteSection(app),
                   volumeSection(app), runSection(app), advancedSection(app),
                   foldSection(app), gpuSection(app), computeSection(app),
                   jobsSection(app));
  // One fetch, after the sections exist to be filled in. Everything that
  // depends on it redraws off the `catalogue` event.
  app.loadDesignCatalogue();
}

function section(title, ...children) {
  return el('div.section', null, el('h3', null, title), ...children);
}

/* ---------------------------------------------------------------- account */

/**
 * Who you are signed in as, and what is left of your allowance.
 *
 * Present only on a deployment that asks people to sign in. Running proteinCAD
 * yourself, there is nobody to be and nothing to count, so this section is not
 * there at all rather than there and empty.
 */
function accountSection(app) {
  const status = el('div.hint');
  const error = el('div.sel-error');
  // Two ways in, because they are two different situations and only one of
  // them is obvious. Somebody who has an account knows to press Sign in;
  // somebody who does not would otherwise be looking at a button that, as far
  // as they can tell, is not for them.
  const signIn = el('button.btn.small', {
    title: 'Already have an account? Sign in to send designs to the GPU',
    onclick: () => auth.signIn(),
  }, 'Sign in');
  const signUp = el('button.btn.small.go', {
    title: 'Create an account — an email address and a password, and a code to '
      + 'confirm the address',
    onclick: () => auth.signUp(),
  }, 'Create account');
  const signOut = el('button.btn.small.ghost', {
    title: 'Sign out on this device',
    onclick: () => auth.signOut(),
  }, 'Sign out');

  const node = section('Account',
    el('div.row.tight', null, signUp, signIn, signOut), status, error);
  node.hidden = true;

  const draw = () => {
    node.hidden = !api.guarded();
    if (node.hidden) return;

    const who = auth.who();
    const inside = auth.signedIn();
    signIn.hidden = inside;
    signUp.hidden = inside;
    signOut.hidden = !inside;
    error.textContent = auth.problem || '';

    if (!inside) {
      status.textContent = 'You can look at structures without an account. Sending a '
        + 'design needs one — anyone can make one, it takes a minute.';
      return;
    }

    const name = (who && (who.email || who.username)) || 'signed in';
    const quota = app.quota;
    status.textContent = quota
      ? `${name} — ${quota.used} of ${quota.daily} jobs today, `
        + `${quota.concurrent} at a time. Resets in ${quota.resets_in}.`
      : name;
  };

  // Three things move this: finishing a sign-in, a 401 on any call, and the
  // job list coming back with a new count on it.
  auth.onChange(draw);
  api.onSignIn(draw);
  app.on('design', draw);
  api.ready.then(draw);
  draw();
  return node;
}

/* ------------------------------------------------------------------- site */

function siteSection(app) {
  const status = el('div.hint');
  const pickButton = el('button.btn.small', {
    title: 'Click residues on the target to mark where the new structure should bind',
    onclick: () => app.setMode(app.mode === 'hotspot' ? 'select' : 'hotspot'),
  }, 'Pick site');

  const node = section('Design site',
    el('div.row.tight', null,
      pickButton,
      el('button.btn.small', {
        title: 'Use the current selection as the site',
        onclick: () => app.hotspotsFromSelection(),
      }, 'From selection'),
      el('button.btn.small', { onclick: () => app.clearHotspots() }, 'Clear')
    ),
    status
  );

  const update = () => {
    pickButton.classList.toggle('on', app.mode === 'hotspot');
    const view = app.designTarget();
    const count = app.hotspotCount;
    if (!app.views.length) status.textContent = 'Load a structure first.';
    else if (!count) {
      status.textContent = app.mode === 'hotspot'
        ? 'Click residues on the target. Click again to unpick.'
        : 'No site picked yet.';
    } else {
      status.textContent = `${count} residue${count === 1 ? '' : 's'} on ${view ? view.label : '—'}`;
    }
  };

  app.on('design', update);
  app.on('settings', update);
  app.on('structures', update);
  update();
  return node;
}

/* ----------------------------------------------------------------- volume */

function volumeSection(app) {
  const info = el('div.hint');
  const shapeButtons = {};
  const fields = {};

  const commitSize = () => {
    if (!app.volume) return;
    app.volume.setSize(+fields.x.value || 1, +fields.y.value || 1, +fields.z.value || 1);
    app.emit('design');
  };

  for (const key of ['x', 'y', 'z']) {
    fields[key] = el('input.num', {
      type: 'number', min: 2, step: 1, value: 20,
      onchange: commitSize,
      onkeydown: (event) => { if (event.key === 'Enter') commitSize(); event.stopPropagation(); },
    });
  }

  const node = section('Shape and size',
    el('div.row.tight', null,
      ...VOLUME_SHAPES.map((shape) => (shapeButtons[shape.id] = el('button.btn.small', {
        title: `Place a ${shape.label.toLowerCase()} where the new structure should go`,
        onclick: () => app.addVolume(shape.id),
      }, shape.label))),
      el('button.btn.small', { onclick: () => app.removeVolume() }, 'Remove')
    ),
    el('div.row.tight', null, el('span.muted.axis', null, 'size'), fields.x, fields.y, fields.z),
    el('div.row.tight', null,
      el('button.btn.small', {
        title: 'Drag the handle to resize the volume',
        onclick: () => { app.setTransformTarget('volume'); app.attachGizmo('scale'); },
      }, 'Resize'),
      el('button.btn.small', {
        title: 'Drag the handle to move the volume',
        onclick: () => { app.setTransformTarget('volume'); app.attachGizmo('translate'); },
      }, 'Move'),
      el('button.btn.small', {
        title: 'Use the volume estimate as the binder length',
        onclick: () => {
          if (!app.volume) return;
          const n = app.volume.residueEstimate;
          app.updateDesign({ lengthMin: Math.max(20, Math.round(n * 0.85)), lengthMax: Math.round(n * 1.15) });
        },
      }, 'Use as length')
    ),
    info
  );

  const update = () => {
    for (const [id, button] of Object.entries(shapeButtons)) {
      button.classList.toggle('on', !!app.volume && app.volume.shape === id);
    }
    for (const key of Object.keys(fields)) fields[key].disabled = !app.volume;
    if (!app.volume) {
      info.textContent = 'Optional. A volume sets how big the new structure should be.';
      return;
    }
    const [x, y, z] = app.volume.size;
    if (document.activeElement !== fields.x) {
      fields.x.value = Math.round(x); fields.y.value = Math.round(y); fields.z.value = Math.round(z);
    }
    info.textContent = `${Math.round(app.volume.volume).toLocaleString()} Å³ ≈ ${app.volume.residueEstimate} residues`;
  };

  app.on('design', update);
  app.on('transform', update);
  app.on('gizmo-move', update);
  update();
  return node;
}

/* -------------------------------------------------------------------- run */

/**
 * Number inputs bound to app.design.
 *
 * `sync` exists because these settings change from outside the panel too --
 * "Use as length" writes two of them, and so does the console -- and a field
 * showing a stale value is worse than no field at all. The one being typed into
 * is left alone.
 */
function boundNumbers(app) {
  const fields = {};
  const number = (key, attrs) => (fields[key] = el('input.num', {
    type: 'number', value: app.design[key], ...attrs,
    onchange: (event) => app.updateDesign({ [key]: +event.target.value }),
    onkeydown: (event) => event.stopPropagation(),
  }));
  const sync = () => {
    for (const [key, field] of Object.entries(fields)) {
      if (document.activeElement !== field) field.value = app.design[key];
    }
  };
  return { number, sync };
}

function runSection(app) {
  const status = el('div.hint');
  const error = el('div.sel-error');
  const preview = el('pre.spec', { hidden: true });
  const modelSelect = el('select.rep-select', {
    onchange: (event) => app.updateDesign({ model: event.target.value }),
  }, el('option', { value: 'mock' }, 'mock'));

  const { number, sync } = boundNumbers(app);

  // Which model draws the backbone, and which of its protocols this is. Both
  // filled in from the server's own tables, so an engine or a protocol added
  // there appears here without this file changing.
  const engineSelect = el('select.rep-select.wide', {
    onchange: (event) => app.setDesignEngine(event.target.value),
  }, el('option', { value: 'rfdiffusion' }, 'RFdiffusion'));
  const engineRow = el('div.row.tight', { hidden: true },
    el('span.muted', { title: 'Which model draws the backbone' }, 'with'), engineSelect);
  const engineHelp = el('div.hint');
  const modeSelect = el('select.rep-select.wide', {
    onchange: (event) => app.setDesignMode(event.target.value),
  }, el('option', { value: 'binder' }, 'Binder'));
  const modeHelp = el('div.hint');

  // The generated contig is right for almost every run and cannot express a
  // motif, an inpainted span or a chain break. Shown so it can be read, editable
  // so it can be overridden, and resettable so an edit is never a one-way door.
  const contigs = el('input.wide.mono', {
    type: 'text', placeholder: 'auto',
    onchange: (event) => app.updateDesign({ contigs: event.target.value }),
    onkeydown: (event) => event.stopPropagation(),
  });
  const contigHint = el('div.hint');
  // The two settings that belong in plain sight rather than under Advanced,
  // marked `common` in the catalogue so the choice is made once, next to the
  // settings themselves, rather than by a list of names here.
  const commonRow = el('div.row.tight');
  const controls = [];

  // What is missing before this could run at all, on a deployment with a
  // shared machine. Empty and hidden on a copy you are running yourself.
  const blocked = el('div.hint.blocked', { hidden: true });
  const runButton = el('button.btn.small.go', {
    title: 'Queue the job',
    onclick: async () => {
      error.textContent = '';
      error.classList.remove('shout');
      // Same reasoning as Start: say something on the press, not when the
      // answer comes back. A design spec is built here before anything is
      // sent, and when that throws -- no site picked, say -- the whole
      // interaction was a button that did not visibly do anything.
      const was = runButton.textContent;
      runButton.disabled = true;
      runButton.textContent = 'Sending…';
      try {
        await app.submitDesign();
        runButton.textContent = 'Sent';
        setTimeout(() => { runButton.textContent = was; }, 1200);
      } catch (e) {
        error.textContent = e.message;
        // The Jobs list is at the bottom of the panel and is where somebody
        // looks after pressing Run. If the reason nothing arrived is up here,
        // it has to come and find them.
        error.classList.add('shout');
        error.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        runButton.textContent = was;
      } finally {
        update();
      }
    },
  }, 'Run design');

  const contigRow = el('div.row.tight', null,
    el('span.muted', {
      title: 'The contig string sent to RFdiffusion. Leave it empty to use the one '
        + 'built from the picked residues.',
    }, 'contigs'), contigs);
  const contigButtons = el('div.row.tight', null,
    el('button.btn.small', {
      title: 'Put the generated contig back',
      onclick: () => { app.updateDesign({ contigs: '' }); },
    }, 'Auto'),
    el('button.btn.small', {
      title: 'Copy the generated contig into the box so it can be edited',
      onclick: () => {
        try { app.updateDesign({ contigs: app.buildSpec().binder.autoContigs }); }
        catch (e) { error.textContent = e.message; }
      },
    }, 'Edit auto'));

  const node = section('Run',
    engineRow,
    engineHelp,
    el('div.row.tight', null, el('span.muted', null, 'make'), modeSelect),
    modeHelp,
    el('div.row.tight', null,
      el('span.muted', { title: 'How much target to send around the picked site' }, 'Crop Å'),
      number('cropRadius', { min: 4, max: 30, step: 1 }),
      el('span.muted', { title: 'Length range of the chain to generate' }, 'len'),
      number('lengthMin', { min: 10, max: 400, step: 5 }),
      number('lengthMax', { min: 10, max: 400, step: 5 })
    ),
    el('div.row.tight', null,
      el('span.muted', null, 'designs'), number('numDesigns', { min: 1, max: 64, step: 1 }),
      el('span.muted', null, 'seed'), number('seed', { min: 0, step: 1 }),
      el('span.muted', null, 'on'), modelSelect
    ),
    commonRow,
    contigRow,
    contigButtons,
    contigHint,
    el('div.row.tight', null,
      el('button.btn.small', {
        title: 'Show exactly what would be sent',
        onclick: () => {
          error.textContent = '';
          try {
            preview.hidden = !preview.hidden;
            if (!preview.hidden) preview.textContent = JSON.stringify(summariseSpec(app.buildSpec()), null, 1);
          } catch (e) { preview.hidden = true; error.textContent = e.message; }
        },
      }, 'Preview spec'),
      runButton
    ),
    error, status, blocked, preview
  );

  const mode = () => (app.designModes.find((m) => m.id === app.design.mode) || null);

  const update = () => {
    const view = app.designTarget();
    const current = mode();
    const needs = current ? current.target : 'required';
    const engine = app.enginesById(app.design.engine);
    if (engineSelect.value !== app.design.engine) engineSelect.value = app.design.engine;
    engineHelp.textContent = engine.help || '';
    if (modeSelect.value !== app.design.mode) modeSelect.value = app.design.mode;
    modeHelp.textContent = current ? current.help : '';

    // There are no contigs outside RFdiffusion: ESM3 is prompted with tracks,
    // and an empty box labelled `contigs` would be a control that does nothing.
    const hasContigs = app.design.engine === 'rfdiffusion';
    contigRow.hidden = !hasContigs;
    contigButtons.hidden = !hasContigs;
    contigHint.hidden = !hasContigs;

    status.textContent = needs === 'none'
      ? 'Designed from nothing: no structure is sent, and the length below is the whole of it.'
      : view && app.hotspotCount
        ? `${current && current.subject ? 'Redesigning' : 'Target'} ${view.label}, `
          + `${app.hotspotCount} picked residues`
          + `${current && current.subject ? '' : `, crop ${app.design.cropRadius} Å`}.`
        : needs === 'optional'
          ? 'Pick a structure, or prompt with a sequence below.'
          : 'Pick a design site to enable the run.';

    // What will actually be sent, which for an empty box is the generated one.
    // Showing it is the difference between a contig you can reason about and a
    // string that appears in an error message a minute into a run.
    if (hasContigs) {
      if (document.activeElement !== contigs) contigs.value = app.design.contigs;
      let shown = app.design.contigs.trim();
      if (!shown) {
        try { shown = app.buildSpec().binder.contigs; } catch { shown = ''; }
      }
      contigHint.textContent = shown
        ? `${app.design.contigs.trim() ? 'Typed' : 'Generated'}: ${shown}`
        : 'Pick some residues and the contig appears here.';
    }

    if (modelSelect.value !== app.design.model) modelSelect.value = app.design.model;

    // The machine and its weights, when there is a shared one. A Run button
    // that queues a job nothing can pick up for four minutes is worse than one
    // that says what is missing.
    const why = blockedBecause(app, 'binder');
    blocked.hidden = !why;
    blocked.textContent = why;
    runButton.disabled = Boolean(why);
    runButton.title = why || 'Queue the job';

    for (const control of controls) control.sync();
    sync();
  };

  app.on('machine', update);

  // The common settings are drawn from the same table as everything under
  // Advanced, so there is one definition of what `steps` means and one place its
  // range is written down.
  app.on('catalogue', (catalogue) => {
    // Offered only when the server reports more than one. A menu with a single
    // item is a claim that there is a choice to make.
    const engines = app.designEngines || [];
    engineRow.hidden = engines.length < 2;
    if (engines.length > 1) {
      clear(engineSelect);
      for (const entry of engines) {
        engineSelect.appendChild(el('option', {
          value: entry.id, title: entry.help, selected: entry.id === app.design.engine,
        }, entry.label));
      }
    }
    clear(modeSelect);
    for (const entry of catalogue.modes || []) {
      modeSelect.appendChild(el('option', {
        value: entry.id, title: entry.help, selected: entry.id === app.design.mode,
      }, entry.label));
    }
    clear(commonRow);
    controls.length = 0;
    for (const option of (catalogue.options || []).filter((o) => o.common)) {
      const control = optionControl(app, option);
      controls.push(control);
      commonRow.append(el('span.muted', { title: option.help }, option.label), control.node);
    }
    update();
  });

  // Runner names come from the server so nothing is hardcoded here. A server
  // that does not report any is one started before the design API existed --
  // the web files reload on every request but the Python process does not, so
  // this is easy to hit after an update.
  // Rebuilt rather than filled once: connecting to a new endpoint adds a runner
  // that was not there at load, and a menu that cannot offer it makes the
  // connection look like it did not work.
  const offerRunners = (runners) => {
    if (!Array.isArray(runners) || !runners.length) return;
    clear(modelSelect);
    for (const runner of runners) {
      modelSelect.appendChild(el('option', { value: runner, selected: runner === app.design.model }, runner));
    }
    if (!runners.includes(app.design.model)) app.updateDesign({ model: runners[0] });
  };
  app.on('runners', offerRunners);

  api.get('health').then((r) => r.json()).then((health) => {
    // The web files are re-read on every request, so a reload picks up changes
    // here at once. The Python process is imported once and never reloads, so a
    // change on that side leaves the old behaviour running behind the new
    // panel — which reads as the fix not working rather than as a stale server.
    if (health.stale) {
      error.textContent = 'This server is running code older than the files on disk. '
        + 'Restart it to pick the changes up: python3 -m proteincad';
    } else if (!Array.isArray(health.runners) || !health.runners.length) {
      error.textContent = 'This server has no design runners. If it was started before '
        + 'the Design tab existed, restart it: python3 -m proteincad';
      return;
    }
    offerRunners(health.runners);
  }).catch(() => {
    error.textContent = api.hosted()
      ? 'The proteinCAD API is not answering. If this has just started, try again in a minute.'
      : 'No server answering on api/health — designs need the Python server running.';
  });

  app.on('design', update);
  app.on('structures', update);
  update();
  return node;
}

/* --------------------------------------------------- everything RFdiffusion */

/**
 * One control for one catalogue entry.
 *
 * Empty means *unset*, and unset is not the same as zero or as false: it means
 * "leave RFdiffusion's own default alone". That distinction is the whole reason
 * a panel generated from a table can be honest about a config file it does not
 * own, so booleans get three states rather than a checkbox's two — a tickbox
 * could not tell "I want recentring off" from "I never touched it", and those
 * send different commands.
 */
function optionControl(app, option) {
  const held = () => app.optionBag()[option.key];
  const set = (value) => app.updateOptions({ [option.key]: value });
  let node;

  if (option.type === 'bool') {
    node = el('select.rep-select.tri', {
      onchange: (event) => set(event.target.value === '' ? undefined : event.target.value === 'on'),
    },
    el('option', { value: '', title: "RFdiffusion's own default" }, '—'),
    el('option', { value: 'on' }, 'on'),
    el('option', { value: 'off' }, 'off'));
  } else if (option.type === 'choice') {
    node = el('select.rep-select', {
      onchange: (event) => set(event.target.value || undefined),
    }, el('option', { value: '' }, '—'),
    ...(option.choices || []).map((choice) => el('option', { value: choice }, choice)));
  } else if (option.multiline) {
    node = el('textarea.wide.mono', {
      rows: 3, placeholder: option.placeholder || 'one per line',
      onchange: (event) => set(splitLines(event.target.value)),
      onkeydown: (event) => event.stopPropagation(),
    });
  } else if (option.type === 'int' || option.type === 'float') {
    node = el('input.num', {
      type: 'number', placeholder: '—',
      min: option.min, max: option.max, step: option.step || (option.type === 'int' ? 1 : 0.1),
      onchange: (event) => set(event.target.value === '' ? undefined : +event.target.value),
      onkeydown: (event) => event.stopPropagation(),
    });
  } else {
    node = el('input.wide.mono', {
      type: 'text', placeholder: option.placeholder || '—',
      onchange: (event) => set(event.target.value.trim() || undefined),
      onkeydown: (event) => event.stopPropagation(),
    });
  }

  const sync = () => {
    if (document.activeElement === node) return;   // never fight the typist
    const value = held();
    if (option.type === 'bool') node.value = value === undefined ? '' : (value ? 'on' : 'off');
    else if (Array.isArray(value)) node.value = value.join('\n');
    else node.value = value === undefined || value === null ? '' : String(value);
  };
  sync();
  return { node, sync, option };
}

function splitLines(text) {
  const lines = String(text).split('\n').map((line) => line.trim()).filter(Boolean);
  return lines.length ? lines : undefined;
}

/**
 * Every RFdiffusion setting there is, drawn from the table the server serves.
 *
 * Generated rather than written out: there are forty-odd of them across six
 * groups, and a control written by hand for each is forty chances for the panel
 * and the command line to disagree about a name. What is shown is filtered by
 * protocol — symmetry settings mean nothing to a binder run — with a way to see
 * the rest, because "not relevant here" is a judgement and it should be
 * possible to overrule it.
 */
function advancedSection(app) {
  const body = el('div');
  const status = el('div.hint');
  let showAll = false;
  let controls = [];

  const extra = el('textarea.wide.mono', {
    rows: 2, placeholder: 'inference.cautious=False',
    onchange: (event) => app.updateOptions({ extra: splitLines(event.target.value) }),
    onkeydown: (event) => event.stopPropagation(),
  });

  const allButton = el('button.btn.small', {
    title: 'Show settings the current protocol does not normally use',
    onclick: () => { showAll = !showAll; redraw(); },
  }, 'Show all');

  const node = section('Advanced',
    el('div.row.tight', null,
      allButton,
      el('button.btn.small', {
        title: 'Clear every setting back to RFdiffusion’s own defaults',
        onclick: () => {
          const bag = app.optionBag();
          const options = app.engineCatalogue().options || [];
          for (const key of Object.keys(bag)) {
            if (!options.some((o) => o.key === key && o.common)) delete bag[key];
          }
          app.emit('design');
        },
      }, 'Reset')
    ),
    body,
    el('div.row.tight', null, el('span.muted', {
      title: 'Passed to run_inference.py exactly as typed, one per line. This is the '
        + 'escape hatch for anything above that this build has not heard of.',
    }, 'overrides')),
    extra,
    status
  );

  const redraw = () => {
    const catalogue = app.engineCatalogue();
    clear(body);
    controls = [];
    if (!catalogue || !Array.isArray(catalogue.options)) {
      status.textContent = 'This server does not serve the settings catalogue. Restart it '
        + 'to get the full set of model options: python3 -m proteincad';
      return;
    }
    status.textContent = '';
    allButton.classList.toggle('on', showAll);

    const mode = (catalogue.modes || []).find((m) => m.id === app.design.mode);
    const groups = new Set((mode && mode.groups) || []);
    for (const group of catalogue.groups || []) {
      if (!showAll && groups.size && !groups.has(group.id)) continue;
      const rows = [];
      for (const option of catalogue.options || []) {
        if (option.group !== group.id || option.common) continue;
        // `modes` on an option is the narrower claim: symmetry settings exist
        // only for symmetric runs even inside the symmetry group.
        if (!showAll && option.modes && !option.modes.includes(app.design.mode)) continue;
        const control = optionControl(app, option);
        controls.push(control);
        rows.push(option.multiline
          ? el('div', null, el('div.row.tight', null,
            el('span.muted', { title: option.help }, option.label)), control.node)
          : el('div.row.tight', null,
            el('span.muted.opt', { title: option.help }, option.label), control.node));
      }
      if (!rows.length) continue;
      body.append(el('div.opt-group', null,
        el('div.muted.opt-head', { title: group.help }, group.label), ...rows));
    }

    if (catalogue.potentials && catalogue.potentials.length) {
      body.append(el('div.row.tight', null,
        el('span.muted', null, 'add'),
        el('select.rep-select.wide', {
          title: 'Append one of the potentials RFdiffusion implements',
          onchange: (event) => {
            if (!event.target.value) return;
            const now = app.optionBag().guidingPotentials || [];
            app.updateOptions({ guidingPotentials: [...now, event.target.value] });
            event.target.value = '';
          },
        }, el('option', { value: '' }, 'guiding potential…'),
        ...catalogue.potentials.map((p) => el('option', { value: p }, p)))));
    }
    sync();
  };

  const sync = () => {
    for (const control of controls) control.sync();
    if (document.activeElement !== extra) {
      extra.value = (app.optionBag().extra || []).join('\n');
    }
  };

  app.on('catalogue', redraw);
  app.on('design', () => {
    // The protocol decides which controls exist, and the engine decides which
    // protocols there are, so either changing is a redraw rather than a refresh
    // of values.
    const now = `${app.design.engine}/${app.design.mode}`;
    if (redraw.at !== now) { redraw.at = now; redraw(); }
    else sync();
  });
  redraw.at = `${app.design.engine}/${app.design.mode}`;
  redraw();
  return node;
}

/* ------------------------------------------------------------ the gpu box */

// How long to wait before asking again. A machine part-way through starting or
// stopping is worth watching closely; one that is settled is not, and every
// poll is an AWS call.
const GPU_POLL = { pending: 3000, stopping: 5000, running: 15000, stopped: 20000 };

/**
 * The GPU this server starts and stops for itself.
 *
 * A card bills by the second whether or not it is computing, so it is kept off
 * and the first job that needs one turns it on. That would be invisible except
 * for the two minutes it takes — which is exactly why this is here. Something
 * has to say whether the thing you are about to pay for is running, how long
 * until it stops on its own, and how to stop it sooner.
 *
 * Hidden entirely when the deployment has no instance of its own: Colab and a
 * local GPU have nothing to show here, and an empty section reads as a broken
 * one.
 */
function gpuSection(app) {
  const state = el('span.gpu-state', null, '—');
  const detail = el('div.hint');
  const error = el('div.sel-error');

  let latest = { configured: false };
  let timer = 0;
  let adopted = false;

  const act = (what, button) => async () => {
    error.textContent = '';
    button.disabled = true;
    try {
      draw(await app.gpuAction(what));
    } catch (e) {
      error.textContent = e.message;
    } finally {
      button.disabled = false;
      schedule(2000);
    }
  };

  const startButton = el('button.btn.small', {
    title: 'Bring the GPU up now, so it is ready by the time a design is. It stops itself '
      + 'again if nothing uses it.',
  }, 'Start');
  const stopButton = el('button.btn.small', {
    title: 'Stop paying for it now rather than waiting out the idle timer',
  }, 'Stop now');
  startButton.onclick = act('start', startButton);
  stopButton.onclick = act('stop', stopButton);

  const node = section('GPU',
    el('div.row.tight', null, state, startButton, stopButton),
    detail, error);
  node.hidden = true;

  const minutes = (seconds) => (seconds >= 90
    ? `${Math.round(seconds / 60)} min`
    : `${Math.max(0, Math.round(seconds))} s`);

  const describe = (gpu) => {
    // Before the state itself moves: EC2 reports `stopped` for a second or two
    // after being asked to start, and a label reading "starting" over a hint
    // reading "Off" looks like something went wrong.
    if (gpu.waking && gpu.state !== 'running') return 'Starting it now — a minute or two.';
    if (gpu.state === 'running' && gpu.jobs) {
      return `${gpu.jobs} job${gpu.jobs === 1 ? '' : 's'} on it. It will not stop while they run.`;
    }
    if (gpu.state === 'running' && gpu.stops_in !== undefined) {
      return `Costing money. Stops itself in ${minutes(gpu.stops_in)} unless something uses it.`;
    }
    if (gpu.state === 'running') return 'Costing money. No idle timer set on this server.';
    if (gpu.state === 'stopped') {
      return gpu.controls === false
        ? 'Off. Sending a design starts it, which takes a minute or two; it stops itself again '
          + `after ${gpu.idle_minutes || 15} idle minutes.`
        : 'Off — it costs only its disk like this. Run starts it, which takes a minute or '
          + 'two the first time each session.';
    }
    if (gpu.state === 'pending') return 'Booting. Jobs sent now will wait for it.';
    if (gpu.state === 'stopping') return 'Shutting down. Starting again means another boot.';
    return '';
  };

  const draw = (gpu) => {
    latest = gpu;
    node.hidden = !gpu.configured;
    if (!gpu.configured) return;

    // The one sensible default: a server with a GPU of its own should use it,
    // rather than quietly running mock designs. Done once, and only while the
    // menu is still on the value it loaded with, so it never overrides a
    // choice that was actually made.
    if (!adopted) {
      adopted = true;
      if (app.design.model === 'mock') app.updateDesign({ model: 'ec2' });
    }

    state.textContent = gpu.waking && gpu.state === 'stopped' ? 'starting' : (gpu.state || '—');
    state.dataset.state = gpu.state || 'unknown';
    // A shared deployment answers `controls: false`, and there are no routes
    // behind these buttons at all. They are not merely hidden for tidiness: a
    // Stop button on a box other people are using is a way to end somebody
    // else's run, so the hosted API does not offer one to hide.
    const mine = gpu.controls !== false;
    startButton.hidden = !mine || gpu.state === 'running' || gpu.state === 'pending';
    stopButton.hidden = !mine || gpu.state !== 'running';
    stopButton.disabled = !!gpu.jobs;
    detail.textContent = describe(gpu);
    detail.title = [gpu.instance, gpu.type, gpu.region].filter(Boolean).join(' · ');
    // A `terminate` shutdown behaviour and an unreachable instance are both
    // things you want to know before the first design, not after.
    error.textContent = gpu.warning || gpu.error || '';
  };

  const schedule = (delay) => {
    clearTimeout(timer);
    timer = setTimeout(poll, delay);
  };

  let missed = 0;

  const poll = async () => {
    const gpu = await app.gpuStatus();
    if (gpu.unreachable) {
      // Do not draw: the last thing the GPU said is still the best thing we
      // know, and painting `{configured: false}` over it would hide the panel
      // -- including the countdown on an instance that is still billing.
      if (latest.configured) {
        error.textContent = `Cannot reach the server (${gpu.unreachable}); still trying.`;
      }
      missed = Math.min(missed + 1, 5);
      schedule(Math.min(2000 * (2 ** (missed - 1)), 30000));
      return;
    }
    missed = 0;
    if (latest.configured) error.textContent = '';
    draw(gpu);
    if (!gpu.configured) return;  // nothing here changes; stop asking
    schedule(gpu.waking ? GPU_POLL.pending : (GPU_POLL[gpu.state] || 15000));
  };

  // A job that wakes the box is the most interesting thing that happens here,
  // so follow it rather than waiting out the poll.
  app.on('design', () => {
    if (latest.configured && latest.state !== 'running') schedule(1500);
  });

  poll();
  return node;
}

/* --------------------------------------------------- the shared machine */

// How often to look, by what is happening. A machine that is coming up or a
// model that is arriving changes every few seconds and is being watched; one
// that is sitting there ready does not, and is not.
const MACHINE_POLL = {
  launching: 3000, booting: 3000, downloading: 2000, ready: 20000, gone: 30000,
};

const BYTES = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.round(n / 1e6)} MB`);

/**
 * The GPU machine, on a deployment where one is shared.
 *
 * Deliberately not the same section as `gpuSection` above, which is for an
 * instance your own copy of proteinCAD starts and stops. This one is about a
 * machine that does not exist most of the time, belongs to everybody, and that
 * nobody may kill -- it can be asked to retire, and it goes when the work is
 * done -- and about the weights it has to fetch before it can do anything,
 * which is a wait worth showing rather than hiding.
 *
 * Only one of the two ever appears: this asks /machine, that asks /gpu, and a
 * deployment answers one of them.
 */
function machineSection(app) {
  const state = el('span.gpu-state', null, '—');
  const detail = el('div.hint');
  const warn = el('div.hint.blocked', { hidden: true });
  const error = el('div.sel-error');
  const models = el('div.models');
  const modelsHead = el('div.models-head');
  const footer = el('div.hint.muted');
  const bar = el('div.bar-fill');
  const sinceLabel = el('div.hint.muted.bar-timer');
  const progress = el('div.bar-wrap', { hidden: true },
    el('div.bar', null, bar), sinceLabel);

  let timer = 0;
  // How many presses are waiting on an answer. A poll that started before a
  // press describes the world before it, so it must not be allowed to paint.
  let busy = 0;
  // Why the last look failed, '' when it did not. Kept rather than thrown
  // away: a panel that cannot see the machine has to say so, not go blank.
  let trouble = '';
  let signedOut = false;
  let retries = 0;

  // Models somebody pressed Download for before there was a machine to put
  // them on, and what we told them we were doing about it.
  //
  // The machine does not exist most of the time -- that is the whole design --
  // so "no machine yet" is the state the panel is usually in when somebody
  // first wants a model. Refusing the press in that state left a button that
  // looked live, did nothing, and explained itself only in a tooltip. The
  // press is an instruction; the machine arriving is merely when it can be
  // carried out.
  const queued = new Set();
  const sending = new Set();
  const pending = new Map();

  const startButton = el('button.btn.small.go', {
    title: 'Create the GPU machine. About five minutes, and it turns itself '
      + 'off again when nothing has needed it.',
    onclick: () => start(),
  }, 'Start GPU machine');

  // Two presses, not a dialog. The machine is shared, so this is worth being
  // sure about -- but a confirm() blocks the page and reads as a browser
  // warning rather than as a decision about a GPU.
  let armed = false;
  const retireButton = el('button.btn.small', {
    title: 'Ask this machine to finish what it is doing and shut itself down. '
      + 'Everyone shares it, so anything already running or queued finishes first.',
    onclick: () => retire(),
  }, 'Retire');

  const node = section('GPU machine',
    el('div.row.tight', null, startButton, retireButton, state),
    detail, progress, warn, error, modelsHead, models, footer);
  node.hidden = true;

  // "4m12s", or "38s". Tabular so it does not jitter as it counts.
  const elapsed = (since) => {
    if (!since) return '';
    const s = Math.max(0, Math.round(Date.now() / 1000 - since));
    return s >= 60 ? `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s` : `${s}s`;
  };

  const describe = (machine) => {
    switch (machine.state) {
      case 'launching':
        return 'Asking AWS for a machine.';
      case 'booting':
        // The boot script reports which of its four steps it is on. Until the
        // first one lands there is nothing to say but the truth: it is early.
        if (machine.stage) {
          const step = machine.step && machine.steps
            ? `Step ${machine.step} of ${machine.steps}: ` : '';
          return `${step}${machine.stage}.`;
        }
        return 'It exists and is starting up. About five minutes in total.';
      case 'ready':
        if (machine.retiring) {
          return 'Retiring. It finishes anything running or queued first, then shuts '
            + 'itself down; the next design starts a fresh one.';
        }
        return 'Running. Download what you need, or just press Run and it will '
          + 'fetch what the job needs.';
      case 'failed':
        return 'The last attempt did not work.';
      default:
        return 'There is no machine right now, which is why this costs nothing when '
          + 'nobody is using it. Starting one takes four or five minutes.';
    }
  };

  /* ------------------------------------------------------------ pressing */

  /** Everything a press should change on screen, changed on the press. */
  const showAtOnce = (text, { starting = false } = {}) => {
    if (starting) {
      startButton.disabled = true;
      startButton.textContent = 'Starting…';
      state.textContent = 'starting';
      state.dataset.state = 'pending';
      detail.textContent = 'Asking AWS for a machine…';
      // Indeterminate, because at this point nothing is known except that a
      // request is out. It becomes a real measure once the boot reports in.
      progress.hidden = false;
      bar.classList.add('waiting');
      bar.style.width = '';
      sinceLabel.textContent = '';
    }
    if (text) drawModels(app.machine);
  };

  const disarm = () => {
    armed = false;
    retireButton.textContent = 'Retire';
    retireButton.classList.remove('on');
  };

  /** Ask the machine to stand down. First press arms, second press sends. */
  const retire = async () => {
    error.textContent = '';
    if (!armed) {
      armed = true;
      retireButton.textContent = 'Really retire?';
      retireButton.classList.add('on');
      setTimeout(() => { if (armed) { disarm(); drawControls(app.machine); } }, 5000);
      return;
    }
    disarm();
    busy++;
    retireButton.disabled = true;
    try {
      const machine = await app.retireMachine();
      busy--;
      draw(machine);
    } catch (e) {
      busy--;
      error.textContent = e.message;
    } finally {
      retireButton.disabled = false;
      schedule(1200);
    }
  };

  /** Start the machine. Also the second half of a Download pressed too early. */
  const start = async () => {
    error.textContent = '';
    busy++;
    clearTimeout(timer);          // no in-flight poll to repaint over this
    showAtOnce('', { starting: true });
    try {
      const machine = await app.startMachine();
      busy--;
      draw(machine);
    } catch (e) {
      busy--;
      error.textContent = e.message;
      // The press failed, so nothing is coming: put the queue back rather than
      // leaving rows saying they are waiting for a machine nobody is making.
      for (const id of queued) pending.delete(id);
      queued.clear();
      draw(app.machine);
    } finally {
      startButton.disabled = false;
      startButton.textContent = 'Start GPU machine';
      bar.classList.remove('waiting');
      schedule(1200);
    }
  };

  /**
   * Download a model -- from whatever state the machine happens to be in.
   *
   * Ready: ask for it now. No machine: make one, and remember what it was for.
   * Coming up: remember it; `carryOutQueue` sends it the moment it can. The
   * button is never a no-op, which is the point.
   */
  const press = async (model) => {
    error.textContent = '';
    const machine = app.machine || {};
    const live = machine.state === 'ready';
    pending.set(model.id, live
      ? 'asking the machine for it…'
      : 'queued — starts when the machine is ready');
    queued.add(model.id);
    busy++;
    showAtOnce('pending');
    try {
      if (live) {
        queued.delete(model.id);
        const answer = await app.downloadModel(model.id);
        busy--;
        draw(answer);
      } else if (machine.state === 'booting' || machine.state === 'launching') {
        busy--;
        draw(app.machine);        // already on its way; the queue does the rest
      } else {
        busy--;
        await start();            // makes the machine, then the queue fires
        return;
      }
    } catch (e) {
      busy = Math.max(0, busy - 1);
      pending.delete(model.id);
      queued.delete(model.id);
      error.textContent = e.message;
      draw(app.machine);
    } finally {
      schedule(900);
    }
  };

  /** Anything pressed before the machine could take it, sent once it can. */
  const carryOutQueue = async () => {
    const machine = app.machine;
    if (!machine || machine.state !== 'ready' || !queued.size) return;
    const found = new Map((machine.models || []).map((m) => [m.id, m]));
    for (const id of [...queued]) {
      const model = found.get(id);
      // The machine already has it, or already knows about it.
      if (model && model.state !== 'absent' && model.state !== 'failed') {
        queued.delete(id);
        continue;
      }
      if (sending.has(id)) continue;
      sending.add(id);
      busy++;
      try {
        const answer = await app.downloadModel(id);
        queued.delete(id);
        busy--;
        draw(answer);
      } catch (e) {
        busy = Math.max(0, busy - 1);
        queued.delete(id);
        pending.delete(id);
        error.textContent = e.message;
      } finally {
        sending.delete(id);
      }
    }
  };

  /* ------------------------------------------------------------ drawing */

  const modelRow = (model) => {
    // Its own bar, for the same reason the boot has one: a number that only
    // changes every couple of seconds reads as stuck.
    const fill = el('div.bar-fill');
    const track = el('div.bar.model-bar', { hidden: true }, fill);
    const mark = el('span.model-mark');
    const label = el('span.model-name', { title: model.help }, model.label);
    const status = el('span.model-status');
    const button = el('button.btn.small.model-go', {
      onclick: () => press(model),
    }, 'Download');

    const note = pending.get(model.id);
    const share = model.total ? Math.round((model.bytes / model.total) * 100) : 0;

    if (model.state === 'ready') {
      mark.textContent = '✓';
      status.textContent = `on the machine · ${BYTES(model.total)}`;
      button.hidden = true;
    } else if (model.state === 'downloading') {
      mark.textContent = '●';
      status.textContent = `${BYTES(model.bytes)} of ${BYTES(model.total)} · ${share}%`;
      button.hidden = true;
      fill.style.width = `${Math.max(2, share)}%`;
      track.hidden = false;
    } else if (model.state === 'wanted') {
      mark.textContent = '●';
      status.textContent = 'queued on the machine — starting';
      button.hidden = true;
      fill.classList.add('waiting');
      track.hidden = false;
    } else if (note) {
      // Pressed, and the server has not caught up yet. Shown from the press
      // rather than from the next poll, which may be twenty seconds away.
      mark.textContent = '●';
      mark.dataset.state = 'wanted';
      status.textContent = note;
      button.hidden = true;
      fill.classList.add('waiting');
      track.hidden = false;
      return el('div.model-row', null, mark, label, button, status, track);
    } else if (model.state === 'failed') {
      mark.textContent = '✕';
      status.textContent = 'failed';
      button.textContent = 'Retry';
      button.title = model.error || `Try fetching ${model.label} again`;
    } else {
      mark.textContent = '○';
      status.textContent = `not downloaded · ${BYTES(model.total)}`;
      // Pressable whatever the machine is doing. If there is not one, the
      // press makes one and the model follows it up.
      const ready = app.machine && app.machine.state === 'ready';
      button.title = ready
        ? `Fetch ${model.label} onto the machine now`
        : `Start the GPU machine and fetch ${model.label} onto it`;
    }
    // Nothing can be asked of a machine we cannot see.
    if (trouble) {
      button.disabled = true;
      button.title = signedOut
        ? 'Sign in first'
        : 'Not while the machine cannot be reached';
    }
    mark.dataset.state = model.state;
    if (model.state === 'failed' && model.error) {
      return el('div.model-row', null, mark, label, button, status, track,
        el('div.sel-error.model-why', null, model.error));
    }
    return el('div.model-row', null, mark, label, button, status, track);
  };

  const drawControls = (machine) => {
    // Only when there is something to retire, and never while one is already
    // on its way out: a second press would say nothing new.
    const can = machine && (machine.controls || {}).retire
      && !machine.retiring && (machine.state === 'ready' || machine.state === 'booting');
    retireButton.hidden = !can;
    retireButton.disabled = !!trouble || busy > 0;
    if (!can && armed) disarm();
  };

  const drawModels = (machine) => {
    clear(models);
    const list = (machine && machine.models) || [];
    for (const model of list) models.appendChild(modelRow(model));

    const here = list.filter((m) => m.state === 'ready').length;
    const coming = list.filter(
      (m) => m.state === 'downloading' || m.state === 'wanted' || pending.has(m.id)).length;
    const parts = [`${here} of ${list.length} on the machine`];
    if (coming) parts.push(`${coming} on the way`);
    modelsHead.textContent = list.length ? `Model weights · ${parts.join(' · ')}` : '';
    modelsHead.hidden = !list.length;
  };

  const draw = (machine, fromPoll = false) => {
    // A poll answered while a press is still in flight is describing the world
    // before the press. Letting it paint puts "There is no machine right now"
    // back over "Asking AWS for a machine…", which is the flicker that made
    // the button look dead.
    if (fromPoll && busy) return;
    node.hidden = !machine;
    if (!machine) return;

    state.textContent = machine.state;
    state.dataset.state = { ready: 'running', gone: 'stopped', failed: 'unknown' }[machine.state]
      || 'pending';
    startButton.hidden = machine.state !== 'gone' && machine.state !== 'failed';
    startButton.disabled = busy > 0 || !!trouble;
    drawControls(machine);
    detail.textContent = describe(machine);
    detail.title = [machine.instance, machine.zone].filter(Boolean).join(' · ');

    // Anything the machine now knows about is no longer something we are only
    // claiming on its behalf.
    for (const model of machine.models || []) {
      if (model.state !== 'absent') pending.delete(model.id);
    }

    // A bar while it is coming up. Steps where we have them, and a share of
    // five minutes before the first step reports -- which is honest about
    // being an estimate rather than pretending to measure something.
    const coming = machine.state === 'launching' || machine.state === 'booting';
    progress.hidden = !coming;
    if (coming) {
      const share = machine.step && machine.steps
        ? machine.step / machine.steps
        : Math.min(0.9, (Date.now() / 1000 - (machine.since || 0)) / 600);
      bar.style.width = `${Math.round(Math.max(0.03, share) * 100)}%`;
      const gone = elapsed(machine.since);
      // Pulling the image is most of it, and the image grew from nine
      // gigabytes to fifteen when ESM3 was added. An estimate that is always
      // half the real wait reads as the machine being stuck.
      sinceLabel.textContent = gone ? `${gone} elapsed · usually five to ten minutes` : '';
    }

    // Stale and labelled beats gone. What is on screen is the last thing the
    // machine said, and the line above it says so rather than letting it be
    // read as current.
    //
    // After that, two things the machine itself cannot complain about, in the
    // order that matters: a retire nothing has acted on, and a machine running
    // code older than the deployment answering the page. Both look exactly
    // like a broken feature until they are named.
    let note = '';
    if (trouble) {
      note = signedOut
        ? 'Signed out — sign in above to see and use the GPU machine.'
        : `Cannot reach the machine (${trouble}). This is the last it said; still trying.`;
    } else if (machine.retiring && machine.state !== 'ready') {
      // A machine that is still coming up has no worker yet, so of course it
      // has not acted. Saying it is overdue here blamed an old build for a
      // boot that was simply not finished.
      note = 'Retiring once it has finished starting — the part of it that reads '
        + 'the request is not running yet.';
    } else if (machine.retiring && machine.retire_waiting > 150) {
      note = 'Asked to retire ' + Math.round(machine.retire_waiting / 60) + ' min ago and '
        + 'still running. A machine started before this feature was deployed cannot act '
        + 'on it — terminate the instance in the AWS console to clear it.';
    } else if (machine.stale_build) {
      note = 'This machine is running an older build than the server '
        + `(${machine.build} against this deployment). It started before the last deploy `
        + 'and keeps the code it booted with — retire it to pick the current one up.';
    }
    warn.hidden = !note;
    warn.textContent = note;
    if (machine.error && !trouble) error.textContent = machine.error;

    drawModels(machine);

    footer.textContent = `Everyone shares this machine. Retire asks it to stand down `
      + `once anything running or queued has finished — it cannot cut a design short. `
      + `It also goes on its own after ${machine.idle_minutes} idle minutes, and `
      + `starting it again re-downloads whatever a job needs.`;
  };

  /* ------------------------------------------------------------ polling */

  const schedule = (delay) => {
    clearTimeout(timer);
    timer = setTimeout(poll, delay);
  };

  const poll = async () => {
    const reach = await app.refreshMachine();

    // No shared machine on this deployment -- a laptop, or a stack without a
    // launch template. Nothing here changes; stop asking. This is the only
    // case that stops the loop, and it is the only one that should: a failure
    // to look is not a discovery that there is nothing to look at.
    if (reach.absent) {
      node.hidden = true;
      return;
    }

    if (reach.ok) {
      trouble = '';
      signedOut = false;
      retries = 0;
      draw(app.machine, true);
      await carryOutQueue();
      const machine = app.machine || {};
      const arriving = (machine.models || []).some(
        (model) => model.state === 'downloading' || model.state === 'wanted');
      schedule(arriving || queued.size || pending.size
        ? MACHINE_POLL.downloading
        : (MACHINE_POLL[machine.state] || 15000));
      return;
    }

    // Could not look. Keep the section, keep the last rows, say so -- and come
    // back, backing off but never stopping.
    trouble = reach.message || 'no answer';
    signedOut = !!reach.signedOut;
    draw(app.machine, true);
    retries = Math.min(retries + 1, 5);
    schedule(signedOut ? 5000 : Math.min(2000 * (2 ** (retries - 1)), 30000));
  };

  // A job is the other thing that starts a machine, so follow one rather than
  // waiting out the poll.
  app.on('design', () => {
    if (app.machine && app.machine.state !== 'ready') schedule(1500);
  });

  poll();
  return node;
}

/**
 * What is left to do before a run, as a sentence.
 *
 * Returns '' when the answer is nothing. Used by both Run and the sequence
 * button, because they need different models and it would be a lie to tell
 * somebody to download ESMFold before drawing a backbone.
 */
function blockedBecause(app, kind = 'binder') {
  const blockers = app.runBlockers(kind);
  if (!blockers.length) return '';

  // Not knowing comes first and on its own: every other sentence here claims
  // to know what the machine has on it, and right now we do not.
  const lost = blockers.find((b) => b.kind === 'unreachable');
  if (lost) {
    return lost.signedOut
      ? 'Sign in to run designs.'
      : `Cannot check the GPU machine — ${lost.message}.`;
  }

  const machine = blockers.find((b) => b.kind === 'machine');
  const missing = blockers.filter((b) => b.kind === 'model');
  const steps = [];
  if (machine) {
    steps.push(machine.state === 'gone' || machine.state === 'failed'
      ? 'start the GPU machine'
      : 'wait for the machine to finish starting');
  }
  if (missing.length) {
    const names = missing.map((b) => b.label);
    const last = names.pop();
    steps.push(`download ${names.length ? `${names.join(', ')} and ${last}` : last}`);
  }
  const sentence = steps.join(', then ');
  return sentence.charAt(0).toUpperCase() + sentence.slice(1) + '.';
}

/* ----------------------------------------------------------- the endpoint */

/**
 * Where the GPU is, changeable without restarting anything.
 *
 * A Colab quick tunnel gets a new address and a new token every session, and a
 * session has to be restarted whenever something has to be cleared off the
 * card. Before this, following it meant stopping the server and retyping a
 * command line — so freeing a GPU cost the job list too. Shown only when the
 * server will accept it, which by default means it is bound to loopback.
 */
function computeSection(app) {
  const status = el('div.hint');
  const error = el('div.sel-error');
  const url = el('input.wide', { type: 'text', placeholder: 'https://….trycloudflare.com',
    onkeydown: (event) => event.stopPropagation() });
  const token = el('input.num.token', { type: 'text', placeholder: 'token',
    onkeydown: (event) => event.stopPropagation() });

  const node = section('Compute endpoint',
    el('div.row.tight', null, url),
    el('div.row.tight', null,
      token,
      el('button.btn.small', {
        title: 'Point this server at that endpoint now — no restart',
        onclick: async () => {
          error.textContent = '';
          try {
            const result = await app.setCompute(url.value, token.value);
            status.textContent = result.compute_configured
              ? `Connected. Runners: ${(result.runners || []).join(', ')}.`
              : 'Cleared — mock only.';
            if ((result.runners || []).includes('remote')) app.updateDesign({ model: 'remote' });
          } catch (e) { error.textContent = e.message; }
        },
      }, 'Connect')
    ),
    error, status
  );
  node.hidden = true;

  api.get('health').then((r) => r.json()).then((health) => {
    node.hidden = !health.allow_remote_config;
    if (health.compute_url && !url.value) url.value = health.compute_url;
    status.textContent = health.compute_configured
      ? `Pointed at ${health.compute_url}`
      : 'Not set — designs run on the mock runner until it is.';
  }).catch(() => { /* the panel already says the server is not answering */ });

  return node;
}

/* ------------------------------------------------------------- stage two */

/**
 * The step that turns a backbone into a protein.
 *
 * Deliberately its own section rather than more fields in Run: it is a separate
 * decision, made after looking at what the first stage produced, and it is
 * where most of the time goes. The settings live here; the button that starts
 * it lives on each finished design in Jobs, because that is what it acts on.
 */
function foldSection(app) {
  const { number, sync } = boundNumbers(app);
  const status = el('div.hint');

  const node = section('Sequence and fold',
    el('div.hint', null,
      'A backbone has no sequence — every residue comes back as glycine. ProteinMPNN '
      + 'chooses the amino acids against the target, then the sequence is folded on its '
      + 'own to see whether it returns to the shape it was designed for.'),
    el('div.row.tight', null,
      el('span.muted', {
        title: 'How many sequences to design for the backbone. Seconds each, so several '
          + 'costs almost nothing.',
      }, 'seqs'),
      number('numSeqs', { min: 1, max: 64, step: 1 }),
      el('span.muted', {
        title: 'How many of the best are folded. This is the slow half — about a minute '
          + 'each, plus a first load of the predictor.',
      }, 'fold'),
      number('foldTop', { min: 1, max: 16, step: 1 }),
      el('span.muted', {
        title: 'Sampling temperature. Low keeps to what the backbone most wants; raise it '
          + 'for a set of sequences that differ from each other.',
      }, 'temp'),
      number('samplingTemp', { min: 0, max: 1, step: 0.05 })
    ),
    status
  );

  const update = () => {
    sync();
    const ready = app.jobs.filter(
      (job) => job.status === 'done' && job.kind !== 'fold' && (job.designs || []).length);
    status.textContent = ready.length
      ? `Press "sequence" on a finished design below. ${ready.length} job${ready.length === 1 ? '' : 's'} ready.`
      : 'Run a design first — every finished backbone then gets a "sequence" button.';
  };

  app.on('design', update);
  update();
  return node;
}

/** The designed sequences as FASTA, with the numbers that judge them. */
function toFasta(job, designs) {
  const lines = [];
  for (const design of designs) {
    const metrics = design.metrics || {};
    if (!metrics.sequence) continue;
    const notes = ['mpnn_score', 'plddt', 'rmsd_to_backbone', 'contacts', 'hotspot_contacts']
      .filter((key) => metrics[key] !== undefined && metrics[key] !== null)
      .map((key) => `${key}=${metrics[key]}`).join(' ');
    lines.push(`>proteincad_job${job.id}_${design.name}${notes ? ` ${notes}` : ''}`);
    lines.push(metrics.sequence);
  }
  return lines.join('\n') + '\n';
}

/**
 * Did it work? The prediction was made from the sequence alone, knowing nothing
 * about the backbone, so how far it lands from that backbone is the measurement
 * that matters. Under 2 Å with a confidence above 80 is the usual bar.
 */
function foldVerdict(designs) {
  const scored = designs.map((d) => d.metrics || {})
    .filter((m) => Number.isFinite(m.rmsd_to_backbone));
  if (!scored.length) {
    const skipped = designs.map((d) => d.metrics || {}).find((m) => m.why);
    return skipped
      ? el('div.hint', null, `Sequences only — not folded: ${skipped.why}`)
      : null;
  }
  const best = scored.reduce((a, b) => (b.rmsd_to_backbone < a.rmsd_to_backbone ? b : a));
  const good = best.rmsd_to_backbone <= 2 && (best.plddt || 0) >= 80;
  return el('div.hint', {
    title: 'Every result carries a designed sequence. The highlighted ones were also '
      + 'folded: the sequence was predicted on its own, knowing nothing about the '
      + 'backbone, and what loads is that prediction. Coming back within 2 Å of the '
      + 'backbone, with a confidence above 80, is the usual bar for a design worth '
      + 'making. The rest are the backbone wearing their sequence.',
  }, `${good ? '✓' : '·'} ${scored.length} of ${designs.length} folded · best `
     + `${fixed(best.rmsd_to_backbone, 2)} Å from the backbone`
     + (Number.isFinite(best.plddt) ? `, pLDDT ${fixed(best.plddt, 0)}` : ''));
}

/* ------------------------------------------------------------------- jobs */

function jobsSection(app) {
  /**
   * Pressed before stage two could run. Say what is missing -- where the
   * cursor is, not only under the row -- and start fetching it.
   *
   * It does not then run the fold. Eight gigabytes takes a minute or two and a
   * job that started itself after an unattended wait is not something anybody
   * asked for; the button is still there when the weights are.
   */
  const fetchWhatFoldNeeds = async (blockers) => {
    const missing = blockers.filter((b) => b.kind === 'model');
    const ready = app.machine && app.machine.state === 'ready';
    if (!missing.length || !ready) {
      app.emit('error', blockedBecause(app, 'fold'));
      return;
    }
    const names = missing.map((b) => b.label).join(' and ');
    app.emit('error', `Stage two needs ${names}. Fetching now — the GPU machine `
      + 'section shows it arriving, then press this again.');
    for (const model of missing) {
      try { await app.downloadModel(model.id); } catch (e) { app.emit('error', e.message); }
    }
  };

  const list = el('div.measure-list');
  const node = section('Jobs', list);

  const update = () => {
    clear(list);
    // Asked once per redraw rather than once per design: it is the same answer
    // for every button on the page.
    const foldBlocked = blockedBecause(app, 'fold');
    if (!app.jobs.length) {
      list.appendChild(el('div.hint', null, 'No jobs yet.'));
      return;
    }
    for (const job of app.jobs) {
      const done = job.status === 'done';
      const running = job.status === 'running' || job.status === 'queued';
      const designs = job.designs || [];
      const sequences = el('pre.spec.wrap', { hidden: true });
      const command = el('pre.spec.wrap', { hidden: true });
      list.appendChild(el('div.job', null,
        el('div.row.tight', null,
          el('span.job-mark', { class: job.status }, STATUS_MARK[job.status] || '·'),
          el('span.mono', null, `#${job.id}`),
          el('span.muted', null, job.model || ''),
          job.mode && job.mode !== 'binder' && job.kind !== 'fold'
            ? el('span.muted', null, job.mode) : null,
          // Where a sequence job came from, so a pair of jobs reads as one
          // design taken through two stages rather than as two unrelated runs.
          job.kind === 'fold'
            ? el('span.muted', null, `sequence of #${job.source ? job.source.job : '?'}`)
            : null,
          el('span.chain-meta', null,
            running ? `${job.progress || 0}/${job.total || '?'}`
              : done ? `${designs.length} ${job.kind === 'fold' ? 'sequences' : 'designs'}` : job.status)
        ),
        // What the model is doing right now. A run spends its first minute
        // loading weights and several more on each design, so "0/4" on its own
        // is indistinguishable from a hang.
        running && job.stage ? el('div.hint', null, job.stage) : null,
        // Runner failures carry the model's own output; keep the line breaks.
        job.error ? el('pre.job-error', { title: job.error }, job.error) : null,
        done && designs.length
          ? el('div.row.tight', null,
            el('span.muted', { title: 'Load into the scene, already on the target' }, 'show'),
            ...designs.slice(0, 12).map((design, index) => el('button.btn.small', {
              // A folded result is a prediction of what the sequence does; an
              // unfolded one is the same backbone wearing a different sequence.
              // They are the same button and not the same thing, so say which.
              class: (design.metrics || {}).folded ? 'go' : null,
              title: `${design.name}${design.metrics ? ` · ${describeMetrics(design.metrics)}` : ''}`,
              onclick: async () => {
                try { await app.loadDesign(job.id, index); } catch (e) { app.emit('error', e.message); }
              },
            }, `${index + 1}`)),
            el('button.btn.small', {
              title: 'Load every design from this job',
              onclick: async () => {
                for (let i = 0; i < designs.length; i++) {
                  try { await app.loadDesign(job.id, i); } catch { /* keep going */ }
                }
              },
            }, 'all')
          )
          : null,
        // Stage two, on whichever backbone is worth taking further. Offered per
        // design rather than per job because they are not equally good, and the
        // folding is slow enough that choosing matters.
        done && designs.length && job.kind !== 'fold'
          ? el('div.row.tight', null,
            el('span.muted', {
              title: 'Design a sequence for this backbone and fold it to see whether it '
                + 'holds the shape. Settings are in "Sequence and fold" above.',
            }, 'sequence'),
            // Stage two needs different weights from stage one -- ProteinMPNN
            // and ESMFold rather than RFdiffusion -- so it asks the question
            // separately. Telling somebody to download ESMFold before they
            // have drawn a backbone would be asking for eight gigabytes they
            // might never use.
            // Never disabled. Stage two needs weights stage one did not, and a
            // backbone is usually finished before anybody has fetched eight and
            // a half gigabytes of folding model -- so the first press of this
            // button lands, by far most often, on a missing download. Greying
            // it out answers that with nothing at all: the reason is in a hint
            // below the row and in a tooltip, and neither is where the cursor
            // is. Pressing it now either runs stage two or says what is missing
            // and goes and gets it.
            ...designs.slice(0, 12).map((design, index) => el('button.btn.small', {
              title: foldBlocked || (`Design ${app.design.numSeqs} sequences for `
                + `${design.name}, folding the best ${app.design.foldTop}`),
              onclick: async () => {
                const blockers = app.runBlockers('fold');
                if (blockers.length) return fetchWhatFoldNeeds(blockers);
                try { await app.foldDesign(job.id, index); } catch (e) { app.emit('error', e.message); }
              },
            }, `${index + 1}`))
          )
          : null,
        done && designs.length && job.kind !== 'fold' && foldBlocked
          ? el('div.hint.blocked', null, foldBlocked)
          : null,
        done && job.kind === 'fold' ? foldVerdict(designs) : null,
        done && job.kind === 'fold' && designs.some((d) => (d.metrics || {}).sequence)
          ? el('div.row.tight', null,
            el('button.btn.small', {
              title: 'The designed sequences, ready to paste',
              onclick: () => {
                sequences.hidden = !sequences.hidden;
                if (!sequences.hidden) sequences.textContent = toFasta(job, designs);
              },
            }, 'Sequences'),
            el('button.btn.small', {
              title: 'Save them as a FASTA file',
              onclick: () => downloadText(`proteincad-job-${job.id}.fasta`, toFasta(job, designs)),
            }, 'FASTA')
          )
          : null,
        sequences,
        // The command the model was run with. With the whole of RFdiffusion's
        // configuration settable, this is the only way to tell a setting that
        // was applied from one an endpoint running older code never heard of —
        // and it is the thing to paste when asking anyone else what went wrong.
        job.command
          ? el('div.row.tight', null, el('button.btn.small', {
            title: 'The exact run_inference.py command this job ran',
            onclick: () => {
              command.hidden = !command.hidden;
              if (!command.hidden) command.textContent = job.command;
            },
          }, 'Command'))
          : null,
        command,
        running
          ? el('div.row.tight', null,
            el('button.btn.small', { onclick: () => app.cancelJob(job.id) }, 'Cancel'))
          : null
      ));
    }
  };

  app.on('design', update);
  app.on('machine', update);
  app.refreshJobs();
  update();
  return node;
}

function describeMetrics(metrics) {
  return Object.entries(metrics)
    // The sequence is a hundred characters and has its own view; in a tooltip
    // it would push everything else off the end.
    .filter(([key]) => key !== 'sequence')
    .map(([key, value]) => `${key} ${typeof value === 'number' ? fixed(value, 2) : value}`)
    .join(' · ');
}
