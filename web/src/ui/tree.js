// The structure tree: one row per loaded structure, expanding to its chains,
// then one per density map. Chain rows carry the visibility toggle, the colour
// swatch and selection; a map row carries its contour level, because that is
// the control that decides whether it shows anything at all.

import { el, clear, throttle } from './dom.js';
import { KIND_NAMES, Kind } from '../core/structure.js';
import { REPRESENTATIONS } from '../render/structureView.js';
import { MAP_STYLES } from '../render/mapView.js';
import { hexString } from '../render/colors.js';

export function createTree(app, container) {
  const expanded = new Set();

  // A chain id shows up more than once when a file lists a polymer and then its
  // ligands and waters under the same id, so say what each run actually holds.
  function chainSummary(structure, chain) {
    const counts = chain.counts || {};
    const residues = chain.residueEnd - chain.residueStart + 1;
    if (chain.kind === Kind.PROTEIN || chain.kind === Kind.NUCLEIC) {
      const polymer = chain.kind === Kind.PROTEIN ? counts.protein : counts.nucleic;
      return `${polymer} ${chain.kind === Kind.PROTEIN ? 'res' : 'nt'}`;
    }
    if (chain.kind === Kind.LIGAND || chain.kind === Kind.ION) {
      const names = new Set();
      for (let r = chain.residueStart; r <= chain.residueEnd && names.size < 4; r++) {
        names.add(structure.residues[r].name);
      }
      const list = [...names].slice(0, 3).join(' ');
      return residues > names.size ? `${residues}× ${list}` : list;
    }
    return `${residues} ${KIND_NAMES[chain.kind]}`;
  }

  function entityName(structure, chain) {
    const entity = structure.entities[chain.entityIndex];
    return entity ? entity.name : '';
  }

  function selectedChains(view) {
    const mask = app.selectionMask(view);
    const set = new Set();
    if (!mask) return set;
    for (const chain of view.structure.chains) {
      for (let i = chain.atomStart; i <= chain.atomEnd; i++) {
        if (mask[i]) { set.add(chain.index); break; }
      }
    }
    return set;
  }

  function chainRow(view, chain, selected) {
    const swatch = el('span.swatch', {
      style: { background: hexString(view.chainColorHex(chain.index)) },
      title: 'Chain colour',
      onclick: (event) => {
        event.stopPropagation();
        const picker = el('input', {
          type: 'color',
          value: hexString(view.chainColorHex(chain.index)),
          style: { position: 'fixed', left: `${event.clientX}px`, top: `${event.clientY}px`, opacity: 0 },
          oninput: (e) => {
            view.setChainColor(chain.index, parseInt(e.target.value.slice(1), 16));
            swatch.style.background = e.target.value;
            app.viewer.requestRender();
          },
          onchange: () => picker.remove(),
        });
        document.body.appendChild(picker);
        picker.click();
      },
    });

    const visible = view.isChainVisible(chain.index);
    return el('div.chain-row', {
      class: selected.has(chain.index) ? 'selected' : '',
      onclick: (event) => app.selectChain(view, chain.index, event.shiftKey ? 'toggle' : 'replace'),
      ondblclick: () => app.viewer.frame(view.box(chain.index).expandByScalar(2), true),
      title: [`${view.structure.name} chain ${chain.id}`, entityName(view.structure, chain)]
        .filter(Boolean).join(' — '),
    },
      swatch,
      el('span.chain-id', null, chain.id),
      el('span.chain-meta', null, chainSummary(view.structure, chain)),
      el('button.eye', {
        title: 'Move this chain',
        onclick: (event) => {
          event.stopPropagation();
          app.moveChain(view, chain.index, 'translate');
        },
      }, '✥'),
      el('button.eye', {
        class: visible ? '' : 'off',
        title: visible ? 'Hide chain' : 'Show chain',
        onclick: (event) => {
          event.stopPropagation();
          view.setChainVisible(chain.index, !view.isChainVisible(chain.index));
          app.viewer.requestRender();
          render();
        },
      }, visible ? '◉' : '○')
    );
  }

  function structureBlock(view) {
    const structure = view.structure;
    const open = expanded.has(view);
    const selected = selectedChains(view);

    const isActive = app.activeView === view;
    const head = el('div.tree-structure', {
      class: `${isActive ? 'current' : ''} ${selected.size ? 'active' : ''}`.trim(),
      title: `${structure.title || view.label}${isActive ? '\n(active — tools act on this one)' : '\nClick to make active'}`,
      onclick: () => app.setActiveView(view),
    },
      el('button.twisty', {
        class: open ? 'open' : '',
        onclick: (event) => {
          event.stopPropagation();
          open ? expanded.delete(view) : expanded.add(view);
          render();
        },
      }, '▶'),
      el('span.tree-name', null, view.label),
      el('button.eye', {
        title: 'Move this structure',
        onclick: (event) => { event.stopPropagation(); app.moveStructure(view, 'translate'); },
      }, '✥'),
      el('button.eye', {
        title: 'Duplicate',
        onclick: (event) => { event.stopPropagation(); app.duplicate(view); },
      }, '⧉'),
      el('button.eye', {
        class: view.visible ? '' : 'off',
        title: view.visible ? 'Hide structure' : 'Show structure',
        onclick: (event) => {
          event.stopPropagation();
          view.setVisible(!view.visible);
          app.viewer.requestRender();
          render();
        },
      }, view.visible ? '◉' : '○'),
      el('button.close', {
        title: 'Remove structure',
        onclick: (event) => { event.stopPropagation(); app.remove(view); },
      }, '✕')
    );

    const children = [head];
    if (open) {
      if (structure.title) children.push(el('div.tree-sub.title', null, structure.title));
      const stats = structure.stats();
      const bits = [`${structure.atomCount.toLocaleString()} atoms`, `${structure.chainCount} chains`];
      if (stats.ligand) bits.push(`${stats.ligand} ligands`);
      if (stats.water) bits.push(`${stats.water} waters`);
      if (structure.modelCount > 1) bits.push(`${structure.modelCount} models (first shown)`);
      children.push(el('div.tree-sub', null, bits.join(' · ')));

      children.push(el('div.tree-sub', null,
        el('select.rep-select', {
          title: 'Representation for this structure',
          onchange: (event) => app.setRepresentation(event.target.value, view),
        }, REPRESENTATIONS.map((r) => el('option', { value: r.id, selected: r.id === view.representation }, r.label)))
      ));

      for (const chain of structure.chains) children.push(chainRow(view, chain, selected));
    }
    return el('div.tree-item', null, children);
  }

  /**
   * One density map.
   *
   * Shorter than a structure block because a map has nothing underneath it --
   * no chains, no sequence, nothing to select. What it does have is a contour
   * level, and that belongs here rather than in a panel: it is the one control
   * that decides whether the map shows anything at all, and it wants to be next
   * to the thing it controls.
   */
  function mapBlock(view) {
    const map = view.map;
    const open = expanded.has(view);
    const [low, high] = map.levelRange();

    const head = el('div.tree-structure', {
      class: app.activeMap === view ? 'current' : '',
      title: [map.title || view.label, map.describe()].filter(Boolean).join('\n'),
      onclick: () => { app.activeMap = view; render(); },
    },
      el('button.twisty', {
        class: open ? 'open' : '',
        onclick: (event) => {
          event.stopPropagation();
          open ? expanded.delete(view) : expanded.add(view);
          render();
        },
      }, '▶'),
      el('span.swatch', {
        style: { background: hexString(view.color) },
        title: 'Map colour',
        onclick: (event) => {
          event.stopPropagation();
          const picker = el('input', {
            type: 'color',
            value: hexString(view.color),
            style: { position: 'fixed', left: `${event.clientX}px`, top: `${event.clientY}px`, opacity: 0 },
            oninput: (e) => {
              view.setColor(parseInt(e.target.value.slice(1), 16));
              app.viewer.requestRender();
              render();
            },
            onchange: () => picker.remove(),
          });
          document.body.appendChild(picker);
          picker.click();
        },
      }),
      el('span.tree-name', null, view.label),
      el('button.eye', {
        title: 'Move this map',
        onclick: (event) => { event.stopPropagation(); app.moveMap(view, 'translate'); },
      }, '✥'),
      el('button.eye', {
        class: view.visible ? '' : 'off',
        title: view.visible ? 'Hide map' : 'Show map',
        onclick: (event) => {
          event.stopPropagation();
          view.setVisible(!view.visible);
          app.viewer.requestRender();
          render();
        },
      }, view.visible ? '◉' : '○'),
      el('button.close', {
        title: 'Remove map',
        onclick: (event) => { event.stopPropagation(); app.removeMap(view); },
      }, '✕')
    );

    const children = [head];
    if (open) {
      if (map.title) children.push(el('div.tree-sub.title', null, map.title));
      const bits = [map.describe()];
      if (map.resolution) bits.push(`${Number(map.resolution).toFixed(1)} Å`);
      bits.push(`${Math.round(view.triangleCount / 1000)}k triangles`);
      children.push(el('div.tree-sub', null, bits.join(' · ')));

      // Level in map units, labelled in sigma as well: a level means nothing on
      // its own across maps whose absolute scales differ by orders of magnitude,
      // and sigma is how everyone talks about one.
      const readout = el('span.mono.level-readout');
      // Re-contouring a 200^3 map takes about a quarter of a second, which is
      // fine once and awful sixty times a second. Throttled on the trailing
      // edge so the surface follows the drag without queueing a rebuild behind
      // every pixel of it; the number keeps up on every event, because that is
      // free and the lag would otherwise look like a dropped input.
      const recontour = throttle((level) => app.setMapLevel(view, level), 120);
      const slider = el('input.level-slider', {
        type: 'range',
        min: low, max: high, step: (high - low) / 400 || 0.001,
        value: view.level,
        title: 'Contour level',
        oninput: (event) => {
          const level = Number(event.target.value);
          showLevel(level);
          recontour(level);
        },
      });
      const showLevel = (level = view.level) => {
        readout.textContent = `${level.toPrecision(3)}  (${map.sigmaForLevel(level).toFixed(1)}σ)`;
      };
      showLevel();

      children.push(el('div.tree-sub', null,
        el('div.row.tight', null, el('span.muted', null, 'level'), readout),
        slider,
        el('div.row.tight', null,
          map.recommended !== null ? el('button.btn.small', {
            title: `The level EMDB records for this entry (${map.recommended})`,
            onclick: () => {
              app.setMapLevel(view, map.recommended);
              slider.value = String(map.recommended);
              showLevel();
            },
          }, 'EMDB level') : null,
          el('select.rep-select', {
            title: 'How to draw it',
            onchange: (event) => {
              view.setStyle(event.target.value);
              app.viewer.requestRender();
            },
          }, MAP_STYLES.map((s) => el('option', { value: s.id, selected: s.id === view.style }, s.label)))
        )
      ));

      if (!view.mesh) {
        children.push(el('div.tree-sub.sel-error', null,
          'Nothing is enclosed at this level — drag it down.'));
      }
      if (!map.complete) {
        children.push(el('div.tree-sub.sel-error', null,
          'The download ended early, so this map has a hole in it.'));
      }
      if (map.fitted && map.fitted.length) {
        children.push(el('div.tree-sub', null,
          el('div.row.tight', null,
            el('span.muted', null, 'fitted'),
            ...map.fitted.slice(0, 4).map((pdb) => el('button.btn.small', {
              title: `Load ${pdb.toUpperCase()}, the model deposited with this map`,
              onclick: () => app.fetchById(pdb).catch((error) => app.emit('error', error.message)),
            }, pdb.toUpperCase())))));
      }
    }
    return el('div.tree-item', null, children);
  }

  function render() {
    clear(container);
    if (!app.views.length && !app.maps.length) {
      container.appendChild(el('div.empty-note', null, 'Nothing loaded yet.'));
      return;
    }
    for (const view of app.views) container.appendChild(structureBlock(view));
    for (const view of app.maps) container.appendChild(mapBlock(view));
  }

  app.on('structures', render);
  app.on('selection', render);
  render();
  return { render };
}
