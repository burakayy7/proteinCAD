// The landscape plot: a curve, its wells, and a cursor you can drag.
//
// Inline SVG rather than a chart library, for the same reason three.js is
// vendored and there is no build step: a dependency that has to be fetched is a
// dependency the app cannot be served from a folder without.
//
// The cursor is the point of the whole panel. Dragging it is dragging the rotor
// -- `onScrub` turns the assembly in the viewport -- so the plot is a control
// rather than a picture, and a well is something you can look at.
//
// Nothing here knows what the y-axis means. The scan says what it scored and in
// what units, and the axis is labelled from that, because a geometric packing
// score and a Rosetta ddG are different numbers and the plot must not imply
// they are the same one.

import { el, clear } from './dom.js';

// Left padding has to clear the rotated axis caption *and* the widest tick a
// clash-dominated curve produces, which runs to five figures.
const PAD = { left: 64, right: 12, top: 10, bottom: 22 };
const SVG = 'http://www.w3.org/2000/svg';

function node(tag, attrs) {
  const made = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value !== null && value !== undefined) made.setAttribute(key, String(value));
  }
  return made;
}

export class LandscapePlot {
  constructor(container, { onScrub } = {}) {
    this.container = container;
    this.onScrub = onScrub || (() => {});
    this.points = [];
    this.minima = [];
    this.label = 'score';
    this.unit = '';
    this.angle = 0;
    this.width = 600;
    this.height = 150;

    this.svg = node('svg', { class: 'plot', width: '100%', height: '100%' });
    this.readout = el('div.plot-readout');
    container.append(this.svg, this.readout);

    // Pointer capture, so a drag that leaves the plot keeps scrubbing instead
        // of stopping wherever the cursor crossed the edge.
    this.svg.addEventListener('pointerdown', (event) => {
      if (!this.points.length) return;
      this.svg.setPointerCapture(event.pointerId);
      this.dragging = true;
      this.#scrubTo(event);
    });
    this.svg.addEventListener('pointermove', (event) => {
      if (this.dragging) this.#scrubTo(event);
    });
    const stop = (event) => {
      if (!this.dragging) return;
      this.dragging = false;
      if (this.svg.hasPointerCapture(event.pointerId)) {
        this.svg.releasePointerCapture(event.pointerId);
      }
    };
    this.svg.addEventListener('pointerup', stop);
    this.svg.addEventListener('pointercancel', stop);

    this.observer = new ResizeObserver(() => this.draw());
    this.observer.observe(container);
  }

  /** @param {{points: Array, minima: Array, label: string, unit: string}} data */
  setData(data) {
    this.points = data.points || [];
    this.minima = data.minima || [];
    this.label = data.label || 'score';
    this.unit = data.unit || '';
    this.period = data.period || 0;
    this.total = data.total || this.points.length;
    this.draw();
  }

  setAngle(angle) {
    this.angle = angle;
    this.draw();
  }

  #scrubTo(event) {
    const box = this.svg.getBoundingClientRect();
    const inner = Math.max(1, box.width - PAD.left - PAD.right);
    const fraction = Math.min(1, Math.max(0, (event.clientX - box.left - PAD.left) / inner));
    // Snapped to a computed angle rather than free: every angle between two
    // samples is an orientation nothing was measured at, and a readout showing
    // a score for one would be showing an interpolation as a measurement.
    const nearest = this.#nearest(fraction * 360);
    if (nearest && nearest.angle !== this.angle) this.onScrub(nearest.angle);
  }

  #nearest(angle) {
    let best = null;
    let distance = Infinity;
    for (const point of this.points) {
      // Circular: 358 degrees is two degrees from zero, not 358.
      const gap = Math.abs(((point.angle - angle + 540) % 360) - 180);
      if (gap < distance) { distance = gap; best = point; }
    }
    return best;
  }

  draw() {
    const box = this.container.getBoundingClientRect();
    const width = Math.max(240, box.width || this.width);
    const height = Math.max(90, (box.height || this.height) - 18);
    this.svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    clear(this.svg);

    const plotWidth = width - PAD.left - PAD.right;
    const plotHeight = height - PAD.top - PAD.bottom;
    const x = (angle) => PAD.left + (angle / 360) * plotWidth;

    if (!this.points.length) {
      this.svg.append(node('text', {
        x: width / 2, y: height / 2, 'text-anchor': 'middle', class: 'plot-empty',
      }));
      this.svg.lastChild.textContent = 'No landscape yet — choose a rotor and an axle, then Scan.';
      this.readout.textContent = '';
      return;
    }

    const scores = this.points.map((p) => p.score);
    let low = Math.min(...scores);
    let high = Math.max(...scores);
    if (high - low < 1e-9) { high = low + 1; low -= 1; }
    const pad = (high - low) * 0.08;
    low -= pad;
    high += pad;
    const y = (score) => PAD.top + plotHeight * (1 - (score - low) / (high - low));

    // --- the frame, and a tick every 90 degrees -----------------------------
    for (const angle of [0, 90, 180, 270, 360]) {
      this.svg.append(node('line', {
        x1: x(angle), y1: PAD.top, x2: x(angle), y2: PAD.top + plotHeight, class: 'plot-grid',
      }));
      const label = node('text', {
        x: x(angle), y: height - 7, 'text-anchor': 'middle', class: 'plot-tick',
      });
      label.textContent = `${angle}°`;
      this.svg.append(label);
    }
    // A faint line at every period, which is what makes "eight wells, 45 apart"
    // something you can see rather than something you have to count.
    if (this.period > 0 && this.period < 180) {
      for (let angle = this.period; angle < 360; angle += this.period) {
        this.svg.append(node('line', {
          x1: x(angle), y1: PAD.top, x2: x(angle), y2: PAD.top + plotHeight,
          class: 'plot-period',
        }));
      }
    }
    for (const [score, where] of [[high - pad, 'top'], [low + pad, 'bottom']]) {
      const label = node('text', {
        x: PAD.left - 8, y: where === 'top' ? PAD.top + 9 : PAD.top + plotHeight,
        'text-anchor': 'end', class: 'plot-tick',
      });
      label.textContent = format(score);
      this.svg.append(label);
    }
    const axis = node('text', {
      x: 10, y: PAD.top + plotHeight / 2, class: 'plot-axis',
      transform: `rotate(-90 10 ${PAD.top + plotHeight / 2})`, 'text-anchor': 'middle',
    });
    axis.textContent = this.unit ? `${this.label} (${this.unit})` : this.label;
    this.svg.append(axis);

    // --- the curve, closed round the back -----------------------------------
    // The last sample is adjacent to the first, so the line is drawn through
    // the wrap as well: a well sitting on zero should look like a well and not
    // like the curve falling off both ends of the plot.
    const ordered = [...this.points].sort((a, b) => a.angle - b.angle);
    const path = ordered.map((p, i) => `${i ? 'L' : 'M'}${x(p.angle).toFixed(1)},${y(p.score).toFixed(1)}`);
    const partial = ordered.length < this.total;
    if (!partial && ordered.length > 1) {
      path.push(`L${x(360).toFixed(1)},${y(ordered[0].score).toFixed(1)}`);
    }
    this.svg.append(node('path', {
      d: path.join(' '), class: partial ? 'plot-line partial' : 'plot-line',
    }));

    // --- the wells ----------------------------------------------------------
    for (const minimum of this.minima) {
      this.svg.append(node('circle', {
        cx: x(minimum.angle), cy: y(minimum.score), r: minimum.deep ? 3.5 : 2.2,
        class: minimum.deep ? 'plot-min deep' : 'plot-min',
      }));
    }

    // --- the cursor ---------------------------------------------------------
    const current = this.#nearest(this.angle);
    if (current) {
      this.svg.append(node('line', {
        x1: x(current.angle), y1: PAD.top, x2: x(current.angle), y2: PAD.top + plotHeight,
        class: 'plot-cursor',
      }));
      this.svg.append(node('circle', {
        cx: x(current.angle), cy: y(current.score), r: 4, class: 'plot-dot',
      }));
      this.#showReadout(current);
    }
  }

  #showReadout(point) {
    clear(this.readout);
    const bits = [
      ['angle', `${point.angle.toFixed(1)}°`],
      [this.label, `${format(point.score)}${this.unit ? ` ${this.unit}` : ''}`],
    ];
    if (point.bsa !== undefined) bits.push(['buried', `${Math.round(point.bsa)} Å²`]);
    if (point.clashes !== undefined) bits.push(['clashes', String(point.clashes)]);
    if (point.gap !== null && point.gap !== undefined) bits.push(['gap', `${point.gap.toFixed(2)} Å`]);
    for (const [key, value] of bits) {
      this.readout.append(el('span.plot-stat', null,
        el('span.muted', null, key), el('b', null, value)));
    }
  }

  dispose() {
    this.observer.disconnect();
  }
}

function format(value) {
  if (!Number.isFinite(value)) return '-';
  const magnitude = Math.abs(value);
  if (magnitude >= 1000) return value.toFixed(0);
  if (magnitude >= 10) return value.toFixed(1);
  return value.toFixed(2);
}
