// The Motion tab: the rotational landscape of a two-component assembly.
//
// Same shape as the Design tab, and for the same reason. The panel's job is to
// say which chains the question is about and hand that to the server; the
// server owns the scan. What comes back is a curve, and the curve is drawn in
// the strip under the viewport rather than in here, because a landscape wants
// the width and because dragging along it has to move the thing next to it.
//
// This panel holds the parts that are *settings* -- which chains, how fine, how
// scored -- plus the numbers read off the finished curve.

import { el, clear, fixed } from './dom.js';
import { LandscapePlot } from './plot.js';
import * as api from '../api.js';

const STATUS_MARK = { queued: '○', running: '◐', done: '●', failed: '✕', cancelled: '–' };
const STEPS = [45, 30, 20, 15, 10, 5, 2];

export function createMotion(app, container, strip) {
  container.append(componentSection(app), scanSection(app), readoutSection(app));
  createStrip(app, strip);
  // One fetch for the scoring backends, so Rosetta can be offered greyed out
  // with the reason on it rather than failing when pressed.
  app.loadScanBackends();
}

function section(title, ...children) {
  return el('div.section', null, el('h3', null, title), ...children);
}

/* --------------------------------------------------------- rotor and axle */

function componentSection(app) {
  const status = el('div.hint');
  const rows = el('div');

  const node = section('Rotor and axle',
    el('div.row.tight', null,
      el('button.btn.small.go', {
        title: 'Take the two largest molecules in the active structure as the rotor and the '
          + 'axle — the one with more copies turns. A guess, and usually the right one for a '
          + 'deposited assembly.',
        onclick: () => {
          const guess = app.guessComponents();
          if (!guess) {
            app.emit('error', 'That structure does not have two distinct molecules to split. '
              + 'Select the chains instead.');
          }
        },
      }, 'Guess from molecules'),
      el('button.btn.small', {
        title: 'Forget both',
        onclick: () => app.clearComponents(),
      }, 'Clear')
    ),
    rows, status
  );

  const draw = () => {
    clear(rows);
    for (const [which, label, hint] of [
      ['rotor', 'Rotor', 'The part that turns'],
      ['axle', 'Axle', 'The part it turns against'],
    ]) {
      const chains = app.componentChains(which);
      rows.append(el('div.row.tight', null,
        el('span.muted.opt', { title: hint }, label),
        el('button.btn.small', {
          title: `Use the selected chains as the ${which}`,
          onclick: () => app.setComponentFromSelection(which),
        }, 'From selection'),
        el('span.mono.muted', {
          title: chains.join(' ') || 'nothing chosen',
        }, chains.length
          ? `${chains.length} chain${chains.length === 1 ? '' : 's'}: ${summarise(chains)}`
          : '—')
      ));
    }

    const blockers = app.motionBlockers();
    status.textContent = blockers.length
      ? `Alt-click a chain, or click a molecule in Inspect to get every copy — ${blockers.join('; ')}.`
      : 'Both components chosen. The symmetry axis is measured from them, not assumed.';
  };

  app.on('motion', draw);
  app.on('structures', draw);
  app.on('selection', draw);
  draw();
  return node;
}

function summarise(chains) {
  return chains.length <= 6 ? chains.join(' ') : `${chains.slice(0, 5).join(' ')} +${chains.length - 5}`;
}

/* ------------------------------------------------------------------ the scan */

function scanSection(app) {
  const status = el('div.hint');
  const error = el('div.sel-error');
  const progress = el('div.scan-bar', null, el('i'));

  const stepSelect = el('select.rep-select', {
    title: 'How finely to sample the turn. Courbet et al. sampled every 45 degrees; 5 is '
      + 'fine enough to resolve lesser minima and still well under a minute for two '
      + 'components of a few thousand atoms.',
    onchange: (event) => app.updateMotion({ step: Number(event.target.value) }),
  }, STEPS.map((step) => el('option', { value: step, selected: step === app.motion.step },
    `${step}°  (${360 / step} points)`)));

  const backendSelect = el('select.rep-select.wide', {
    title: 'What scores the interface at each angle',
    onchange: (event) => app.updateMotion({ backend: event.target.value }),
  });

  const riseInput = el('input.sel-input', {
    type: 'number', min: 0, max: 20, step: 0.5, value: app.motion.rise, style: { width: '56px' },
    title: 'Also slide the rotor along the axis by up to this many Angstroms either way, '
      + 'making a two-dimensional landscape. Zero means rotation only. Every step multiplies '
      + 'the work, so start at zero.',
    onchange: (event) => app.updateMotion({ rise: Math.max(0, Number(event.target.value) || 0) }),
  });

  const scanButton = el('button.btn.small.go', {
    onclick: async () => {
      error.textContent = '';
      try {
        await app.submitScan();
      } catch (problem) {
        error.textContent = problem.message;
      }
    },
  }, 'Scan');

  const cancelButton = el('button.btn.small', {
    title: 'Stop it. What has been computed is kept, and scanning again resumes from there.',
    onclick: () => app.cancelScan(),
  }, 'Stop');

  const node = section('Scan',
    el('div.row.tight', null, el('span.muted.opt', null, 'step'), stepSelect),
    el('div.row.tight', null, el('span.muted.opt', null, 'score by'), backendSelect),
    el('div.row.tight', null, el('span.muted.opt', null, 'rise ±'), riseInput,
      el('span.muted', null, 'Å')),
    el('div.row.tight', null, scanButton, cancelButton),
    progress, status, error
  );

  const draw = () => {
    const scan = app.landscape;
    const blockers = app.motionBlockers();
    const running = scan && ['queued', 'running'].includes(scan.status);
    scanButton.disabled = blockers.length > 0 || running;
    scanButton.title = blockers.length ? `Cannot scan: ${blockers.join('; ')}` : 'Run the scan';
    cancelButton.hidden = !running;

    if (app.scanBackends) {
      clear(backendSelect);
      for (const backend of app.scanBackends) {
        // Where it runs is on the label, because it is the difference between
        // a scan that happens now and one that needs a server and an account.
        const where = backend.where === 'browser' ? ' — here' : ' — server';
        backendSelect.append(el('option', {
          value: backend.id,
          selected: backend.id === app.motion.backend,
          disabled: !backend.available,
          title: backend.why || (backend.where === 'browser'
            ? `${backend.label}, in ${backend.unit}. Computed in this browser — nothing is `
              + 'uploaded and no account is needed.'
            : `${backend.label}, in ${backend.unit}. Runs on the Python server.`),
        }, backend.available ? backend.label + where : `${backend.label} — unavailable`));
      }
      backendSelect.value = app.motion.backend;
      const chosen = app.scanBackends.find((b) => b.id === app.motion.backend);
      if (chosen && !chosen.available) error.textContent = chosen.why;
      else if (chosen && chosen.where === 'server' && app.scanUnsupported) {
        error.textContent = app.scanUnsupported;
      }
    }
    // Every step of rise multiplies the work by one more whole turn, so say
    // what has actually been asked for rather than leaving it to be discovered.
    const rises = (Math.floor(Number(app.motion.rise) / (Number(app.motion.riseStep) || 1)) * 2) + 1;
    riseInput.title = Number(app.motion.rise) > 0
      ? `A two-dimensional scan: ${rises} heights at every angle, so ${rises}x the work. `
        + 'The curve shows the best height at each angle.'
      : 'Also slide the rotor along the axis by up to this many Angstroms either way, '
        + 'making a two-dimensional landscape. Zero means rotation only.';

    progress.hidden = !scan || !scan.total;
    if (scan && scan.total) {
      const done = Math.min(1, (scan.progress || 0) / scan.total);
      progress.firstChild.style.width = `${(done * 100).toFixed(1)}%`;
      progress.classList.toggle('running', !!running);
    }

    if (!scan) {
      status.textContent = blockers.length ? '' : (app.motion.backend === 'geometric'
        ? 'Ready. The geometric scan runs in this browser — nothing is uploaded, and a '
          + 'whole turn takes about a second.'
        : 'Ready. An identical scan is never run twice — the result is named after what '
          + 'went into it.');
      return;
    }
    const mark = STATUS_MARK[scan.status] || '·';
    const counted = scan.total ? ` ${scan.progress}/${scan.total} angles` : '';
    const ran = scan.where === 'browser' ? ' · in this browser' : '';
    status.textContent = `${mark} ${scan.status}${counted} · ${fixed(scan.elapsed, 1)}s${ran}`;
    error.textContent = scan.error || error.textContent;
  };

  app.on('motion', draw);
  api.ready.then(draw);
  draw();
  return node;
}

/* -------------------------------------------------------------- the numbers */

function readoutSection(app) {
  const list = el('dl.kv');
  const notes = el('div.hint');
  const node = section('What the curve says', list, notes);

  const draw = () => {
    clear(list);
    clear(notes);
    const scan = app.landscape;
    const descriptors = scan && scan.descriptors;
    const axis = scan && scan.axis;
    node.hidden = !descriptors || !descriptors.complete;
    if (node.hidden) return;

    const add = (key, value, title) => {
      if (value === null || value === undefined || value === '') return;
      list.append(el('dt', { title: title || '' }, key), el('dd', { title: title || '' }, value));
    };

    // The axis first, because every other number is measured about it and a bad
    // one invalidates all of them.
    if (axis) {
      const folds = [];
      if (axis.rotor_fold) folds.push(`rotor C${axis.rotor_fold}`);
      if (axis.axle_fold > 1) folds.push(`axle C${axis.axle_fold}`);
      if (folds.length) add('symmetry', folds.join(', '), 'Measured from each component by '
        + 'superposing a chain onto its symmetry mates');
      if (axis.agreement !== null && axis.agreement !== undefined) {
        add('axis agree', `${fixed(axis.agreement, 2)}°`,
          'Angle between the axis the rotor implies and the axis the axle implies. A large '
          + 'number means the two parts do not share an axis and the scan is not measuring '
          + 'what it claims to.');
      }
    }

    add('period', `${fixed(descriptors.period, 1)}°`,
      'The spacing the curve actually repeats at, taken as the common divisor of every '
      + 'frequency carrying power — not the strongest frequency, which is the shape of one '
      + 'well rather than the spacing of them.');
    if (descriptors.expected_period) {
      // Three outcomes, not two. A scan too coarse to carry the frequency has
      // not failed the check, it has not run it — and a ✕ there would read as
      // the assembly disagreeing with its own symmetry.
      const verdict = !descriptors.period_resolvable ? ' — too coarse to check'
        : (descriptors.period_matches_symmetry ? ' ✓' : ' ✕');
      add('symmetry says', `${fixed(descriptors.expected_period, 1)}°${verdict}`,
        'What symmetry forces the period to be: 360 over the lowest common multiple of the '
        + 'two folds. This is a prediction the scan has to meet, not something read off it.');
    }
    add('wells', `${descriptors.deep_count} deep`
      + (descriptors.lesser_count ? `, ${descriptors.lesser_count} lesser` : ''),
      `Split at a prominence of ${fixed(descriptors.prominence_threshold, 2)}, a tenth of the `
      + 'range');
    add('barrier', fixed(descriptors.barrier_mean, 2), 'Mean height to climb out of a well, '
      + 'going forwards');
    add('depth range', fixed(descriptors.range, 2), 'Highest point minus lowest');
    add('asymmetry', descriptors.asymmetry === null ? '—' : fixed(descriptors.asymmetry, 3),
      'Forward barrier against reverse, averaged over the wells, from -1 to 1. Zero is a '
      + 'rotor equally happy to turn either way; away from zero is ratchet-like.');
    if (descriptors.deposited) {
      const { rank, of, is_minimum: isMinimum } = descriptors.deposited;
      add('as loaded', `rank ${rank} of ${of}${isMinimum ? ', in a well' : ''}`,
        'Where the orientation you loaded sits in its own landscape. For an experimental '
        + 'structure this is the one check that needs no reference curve: the deposited pose '
        + 'should be at or near the bottom, and a poor rank is a result about the scorer.');
    }

    if (descriptors.period_note) {
      notes.append(el('div', null, `Period: ${descriptors.period_note}.`));
    }
    if (descriptors.asymmetry === null && descriptors.asymmetry_note) {
      notes.append(el('div', null, `Asymmetry: ${descriptors.asymmetry_note}.`));
    }
    if (descriptors.clash_free_fraction < 0.5) {
      notes.append(el('div', null,
        `Only ${Math.round(descriptors.clash_free_fraction * 100)}% of the turn is clash-free. `
        + 'This assembly is interdigitated rather than free to rotate, so most of the curve is '
        + 'a steric wall and the well depths below are not comparable to a design that turns.'));
    }
    if (scan.backend && scan.backend.id === 'geometric') {
      notes.append(el('div', null, 'Scored on geometry — buried area, overlap and gap, in '
        + 'arbitrary units. It finds the orientations that pack well. It is not a force field '
        + 'and these are not binding energies.'));
    }
  };

  app.on('motion', draw);
  draw();
  return node;
}

/* --------------------------------------------------------------- the strip */

/**
 * The plot, docked under the viewport.
 *
 * It lives inside the viewport rather than in the panel grid so that adding it
 * cannot disturb the three-column layout -- the same reason the labels and the
 * loading spinner are positioned in there. It is not present until there is a
 * curve to draw, because an empty chart taking a fifth of the viewport is worse
 * than no chart.
 */
function createStrip(app, strip) {
  const canvas = el('div.plot-area');
  const title = el('span.plot-title');
  const angleLabel = el('span.mono.plot-angle');

  const plot = new LandscapePlot(canvas, {
    onScrub: (angle) => app.setRotorAngle(angle),
  });

  const slider = el('input.plot-slider', {
    type: 'range', min: 0, max: 359, step: 1, value: 0,
    title: 'Turn the rotor. The plot cursor and the assembly are the same control.',
    oninput: (event) => app.setRotorAngle(Number(event.target.value)),
  });

  const stepBy = (delta) => {
    const points = (app.landscape && app.landscape.points) || [];
    if (!points.length) return;
    const angles = [...new Set(points.map((p) => p.angle))].sort((a, b) => a - b);
    const here = angles.indexOf(app.rotorAngle);
    const next = angles[((here < 0 ? 0 : here) + delta + angles.length) % angles.length];
    app.setRotorAngle(next);
  };

  const toWell = (delta) => {
    const minima = ((app.landscape || {}).descriptors || {}).minima || [];
    const deep = minima.filter((m) => m.deep).map((m) => m.angle).sort((a, b) => a - b);
    if (!deep.length) return;
    const ahead = delta > 0
      ? deep.find((a) => a > app.rotorAngle + 1e-6)
      : [...deep].reverse().find((a) => a < app.rotorAngle - 1e-6);
    app.setRotorAngle(ahead === undefined ? deep[delta > 0 ? 0 : deep.length - 1] : ahead);
  };

  strip.append(
    el('div.plot-head', null,
      title,
      el('span.group.right', null,
        el('button.btn.small.ghost', { title: 'Previous deep well', onclick: () => toWell(-1) }, '⟨⟨'),
        el('button.btn.small.ghost', { title: 'Previous angle', onclick: () => stepBy(-1) }, '⟨'),
        angleLabel,
        el('button.btn.small.ghost', { title: 'Next angle', onclick: () => stepBy(1) }, '⟩'),
        el('button.btn.small.ghost', { title: 'Next deep well', onclick: () => toWell(1) }, '⟩⟩'),
        el('button.btn.small.ghost', {
          title: 'Back to the orientation as loaded',
          onclick: () => app.setRotorAngle(0),
        }, 'Reset'),
        el('button.icon', {
          title: 'Hide the landscape', onclick: () => { strip.hidden = true; app.viewer.resize(); },
        }, '×')
      )),
    canvas,
    el('div.plot-foot', null, slider)
  );
  strip.hidden = true;

  const draw = () => {
    const scan = app.landscape;
    const points = (scan && scan.points) || [];
    const descriptors = (scan && scan.descriptors) || {};
    const had = !strip.hidden;
    strip.hidden = !points.length;
    if (had !== !strip.hidden) app.viewer.resize();
    if (strip.hidden) return;

    const backend = (scan && scan.backend) || {};
    title.textContent = `${(scan.request && scan.request.name) || 'Landscape'} — `
      + `${scan.progress}/${scan.total} angles`
      + (descriptors.period ? `, period ${fixed(descriptors.period, 1)}°` : '');
    plot.setData({
      points,
      minima: descriptors.minima || [],
      label: backend.label || 'score',
      unit: backend.unit || '',
      period: descriptors.period || 0,
      total: scan.total || points.length,
    });
    drawAngle();
  };

  const drawAngle = () => {
    plot.setAngle(app.rotorAngle);
    slider.value = String(Math.round(app.rotorAngle));
    angleLabel.textContent = `${fixed(app.rotorAngle, 1)}°`;
  };

  app.on('motion', draw);
  app.on('motion-angle', drawAngle);
  draw();
  return strip;
}
