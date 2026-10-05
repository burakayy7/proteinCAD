// The scan, off the main thread.
//
// A 72-angle scan of a 9,000-atom assembly takes about 400 ms, which is short
// enough to be worth doing and long enough that doing it on the main thread
// would freeze the viewport for the whole of it -- and a large assembly is
// several times that. So it runs here, posts each angle back as it lands, and
// the curve draws itself while it is still being computed.
//
// Only typed arrays cross: the components are built and the axis measured on
// the main thread (a few milliseconds each), and what arrives here is the
// coordinates, the radii and the axis. Nothing is serialised to JSON and no
// structure is re-parsed.

import { Component, GeometricScorer, angleList, riseList } from './core/landscape.js';

self.onmessage = (event) => {
  const { rotor, axle, axis, step, rise } = event.data;
  try {
    const angles = angleList(step);
    const rises = riseList(rise);
    const total = angles.length * rises.length;
    const scorer = new GeometricScorer();
    scorer.prepare(revive(rotor), revive(axle), axis);

    // Batched, because posting 72 messages that each trigger a redraw costs
    // more than the scan does. Every ~60 ms is often enough to look live.
    let batch = [];
    let lastPost = 0;
    let computeMs = 0;
    const flush = (done) => {
      if (!batch.length && !done) return;
      // `computeMs` is the arithmetic alone, so a scan that feels slow can be
      // told apart from one that is slow: the two have different fixes.
      self.postMessage({ points: batch, done, total, computeMs });
      batch = [];
      lastPost = performance.now();
    };

    for (const angle of angles) {
      for (const rise of rises) {
        const started = performance.now();
        const point = scorer.score(angle, rise);
        computeMs += performance.now() - started;
        batch.push(point);
      }
      if (performance.now() - lastPost > 60) flush(false);
    }
    flush(true);
  } catch (error) {
    self.postMessage({ error: error.message || String(error) });
  }
};

/** Rebuild a Component from the arrays that came across. */
function revive(plain) {
  const component = new Component(plain.name, plain.chains);
  component.x = plain.x;
  component.y = plain.y;
  component.z = plain.z;
  component.radii = plain.radii;
  return component;
}
