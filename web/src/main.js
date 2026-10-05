// Entry point: build the app, wire the chrome around the viewport.

import { App } from './app.js';
import { createTree } from './ui/tree.js';
import { createInspector } from './ui/inspector.js';
import { createDesign } from './ui/design.js';
import { createMotion } from './ui/motion.js';
import { REPRESENTATIONS } from './render/structureView.js';
import { COLOR_SCHEMES } from './render/colors.js';
import { $, options } from './ui/dom.js';

const app = new App({
  viewport: $('#viewport'),
  labels: $('#labels'),
  loading: $('#loading'),
  loadingText: $('#loading-text'),
});
window.proteinCAD = app; // handy from the console, and for future scripting

createTree(app, $('#tree'));
createInspector(app, $('#inspector'));
createDesign(app, $('#design'));
createMotion(app, $('#motion'), $('#landscape'));

for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => {
    for (const other of document.querySelectorAll('.tab')) {
      const on = other === tab;
      other.classList.toggle('on', on);
      $(`#${other.dataset.tab}`).hidden = !on;
    }
  });
}

/* ---------------------------------------------------------------- toolbar */

const representationSelect = options($('#representation'), REPRESENTATIONS, app.settings.representation);
representationSelect.addEventListener('change', (event) => {
  app.updateSettings({ representation: event.target.value });
});

const colorSelect = options($('#color-scheme'), COLOR_SCHEMES, app.settings.colorScheme);
colorSelect.addEventListener('change', (event) => {
  app.updateSettings({ colorScheme: event.target.value });
});

$('#btn-open').addEventListener('click', () => $('#file-input').click());
$('#file-input').addEventListener('change', async (event) => {
  await app.loadFiles([...event.target.files]);
  event.target.value = '';
});

$('#fetch-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('#fetch-id');
  const id = input.value.trim();
  if (!id) return;
  try {
    await app.fetchById(id);
    input.value = '';
  } catch (error) {
    showError(`Could not fetch ${id}: ${error.message}`);
  }
});

$('#btn-fit').addEventListener('click', () => app.frameAll());
$('#btn-snapshot').addEventListener('click', () => app.snapshot());
$('#btn-export').addEventListener('click', () => {
  if (!app.views.length) return showError('Nothing to export.');
  app.exportPDB();
});

const spinButton = $('#btn-spin');
spinButton.addEventListener('click', () => {
  app.updateSettings({ spin: !app.settings.spin });
  spinButton.classList.toggle('on', app.settings.spin);
});

$('#btn-help').addEventListener('click', () => { $('#help').hidden = false; });
$('#help-close').addEventListener('click', () => { $('#help').hidden = true; });
$('#help').addEventListener('click', (event) => {
  if (event.target === $('#help')) $('#help').hidden = true;
});

$('#collapse-left').addEventListener('click', () => togglePanel('left'));
$('#collapse-right').addEventListener('click', () => togglePanel('right'));

function togglePanel(side) {
  const workspace = $('.workspace');
  const collapsed = workspace.classList.toggle(`no-${side}`);
  const button = $(`#collapse-${side}`);
  button.title = collapsed
    ? `Show the ${side === 'left' ? 'structures' : 'inspector'} panel`
    : 'Collapse panel';
  button.setAttribute('aria-expanded', String(!collapsed));
  requestAnimationFrame(() => app.viewer.resize());
}

for (const button of document.querySelectorAll('[data-example]')) {
  button.addEventListener('click', async () => {
    try {
      await app.fetchById(button.dataset.example);
    } catch (error) {
      showError(`Could not load ${button.dataset.example}: ${error.message}`);
    }
  });
}

/* ------------------------------------------------------------- status bar */

const statusHover = $('#status-hover');
const statusCounts = $('#status-counts');

app.on('hover', () => { statusHover.textContent = app.hoverLabel() || ' '; });
app.on('structures', updateCounts);
app.on('selection', updateCounts);

function updateCounts() {
  const { structures, maps, atoms, chains } = app.counts();
  const selected = app.selectionCount;
  const bits = [];
  if (maps) bits.push(`${maps} map${maps === 1 ? '' : 's'}`);
  if (structures) {
    bits.push(`${structures} structure${structures === 1 ? '' : 's'}`);
    bits.push(`${atoms.toLocaleString()} atoms`);
    bits.push(`${chains} chains`);
  }
  if (selected) bits.push(`${selected.toLocaleString()} selected`);
  statusCounts.textContent = bits.join('  ·  ');
  $('#empty-state').hidden = app.views.length > 0 || app.maps.length > 0;
}
updateCounts();

let errorTimer = null;
function showError(message) {
  statusHover.textContent = message;
  statusHover.style.color = '#ff8a80';
  clearTimeout(errorTimer);
  errorTimer = setTimeout(() => {
    statusHover.style.color = '';
    statusHover.textContent = app.hoverLabel() || ' ';
  }, 6000);
}
app.on('error', (message) => showError(message));

/* ---------------------------------------------------------- drag and drop */

const dropHint = $('#drop-hint');
let dragDepth = 0;

window.addEventListener('dragenter', (event) => {
  if (![...event.dataTransfer.types].includes('Files')) return;
  dragDepth++;
  dropHint.hidden = false;
});
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) dropHint.hidden = true;
});
window.addEventListener('drop', async (event) => {
  event.preventDefault();
  dragDepth = 0;
  dropHint.hidden = true;
  const files = [...(event.dataTransfer.files || [])];
  if (files.length) await app.loadFiles(files);
});

/* --------------------------------------------------------------- keyboard */

const REP_KEYS = ['cartoon', 'trace', 'ballstick', 'sticks', 'spacefill', 'surface'];

window.addEventListener('keydown', (event) => {
  const tag = (event.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
  if (event.metaKey && event.key.toLowerCase() === 'a') {
    event.preventDefault();
    app.selectExpression('all');
    return;
  }
  if (event.ctrlKey || event.metaKey) return;

  const key = event.key.toLowerCase();
  const digit = parseInt(event.key, 10);
  if (digit >= 1 && digit <= REP_KEYS.length) {
    representationSelect.value = REP_KEYS[digit - 1];
    app.updateSettings({ representation: REP_KEYS[digit - 1] });
    return;
  }

  switch (key) {
    case 'f': app.frameSelection(); break;
    case 'r': app.frameAll(); break;
    // Shift forces the whole structure even when a chain is selected.
    case 'g': if (event.shiftKey) app.setTransformTarget('structure'); app.attachGizmo('translate'); break;
    case 't': if (event.shiftKey) app.setTransformTarget('structure'); app.attachGizmo('rotate'); break;
    case 'm': app.setMode(app.mode === 'measure' ? 'select' : 'measure'); break;
    case 'i': app.isolateSelection(); break;
    case 'h': if (event.shiftKey) app.showAll(); break;
    case ' ': {
      event.preventDefault();
      app.updateSettings({ spin: !app.settings.spin });
      spinButton.classList.toggle('on', app.settings.spin);
      break;
    }
    case '[': togglePanel('left'); break;
    case ']': togglePanel('right'); break;
    case 'escape': app.clearSelection(); app.detachGizmo(); $('#help').hidden = true; break;
    case 'backspace': case 'delete': app.hideSelection(); break;
    default: return;
  }
  event.preventDefault();
});

/* ----------------------------------------------------------- initial load */

const params = new URLSearchParams(location.search);
// `?load=` takes either kind of id: 4HHB is a structure, EMD-25575 is a map.
const initial = params.get('load') || params.get('pdb') || params.get('emdb');
if (initial) {
  app.fetchById(initial).catch((error) => showError(`Could not load ${initial}: ${error.message}`));
}
