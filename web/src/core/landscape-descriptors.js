// What the curve says: minima, barriers, period and asymmetry.
//
// Mirrors `descriptors()` in proteincad/landscape.py exactly, including the
// three things it is careful about -- each of which is a way to read a theorem
// as a result:
//
//   * the period is not 360 over the strongest frequency. A curve of period 45
//     degrees has power at orders 8, 16, 24 -- every multiple of 8 -- and which
//     is largest is the shape of one well, not the spacing of them.
//   * a scan too coarse to carry the frequency has not failed the symmetry
//     check, it has not run it.
//   * asymmetry is unavailable, not zero, when there is one minimum per period:
//     the barrier leaving a well forwards and the barrier leaving its neighbour
//     backwards are then the same peak.
//
// Split from landscape.js so the arithmetic that reads a finished curve can be
// tested, and read, without the scan around it.

/**
 * @param {Array<{angle: number, score: number, clashes?: number}>} points
 * @param {number} expected  the period symmetry forces, or 0 if unknown
 */
export function descriptors(points, expected = 0) {
  // The one-dimensional landscape: the best rise at each angle, which with no
  // translation scan is simply each angle.
  const byAngle = new Map();
  for (const row of points) {
    const current = byAngle.get(row.angle);
    if (!current || row.score < current.score) byAngle.set(row.angle, row);
  }
  const ordered = [...byAngle.values()].sort((a, b) => a.angle - b.angle);
  const n = ordered.length;
  if (n < 4) return { minima: [], complete: false };

  const scores = ordered.map((row) => row.score);
  const angles = ordered.map((row) => row.angle);
  const low = Math.min(...scores);
  const high = Math.max(...scores);
  const span = high - low;

  // Minima, circularly. `<=` on one side and `<` on the other, so a
  // flat-bottomed well reports one minimum rather than every sample along it.
  const indices = [];
  for (let i = 0; i < n; i++) {
    const before = scores[(i - 1 + n) % n];
    const after = scores[(i + 1) % n];
    if (scores[i] <= before && scores[i] < after) indices.push(i);
  }

  const minima = indices.map((i, position) => {
    const forward = peakBetween(scores, i, indices[(position + 1) % indices.length], +1);
    const reverse = peakBetween(scores, i, indices[(position - 1 + indices.length) % indices.length], -1);
    return {
      angle: angles[i],
      score: scores[i],
      bsa: ordered[i].bsa,
      clashes: ordered[i].clashes,
      rise: ordered[i].rise ?? 0,
      forward_barrier: round(forward - scores[i], 4),
      reverse_barrier: round(reverse - scores[i], 4),
      prominence: round(Math.min(forward, reverse) - scores[i], 4),
    };
  });

  const threshold = 0.1 * span;
  for (const minimum of minima) minimum.deep = minimum.prominence >= threshold;
  const deep = minima.filter((m) => m.deep);

  // One discrete Fourier coefficient per order, on the mean-removed curve.
  const mean = scores.reduce((a, b) => a + b, 0) / n;
  const centred = scores.map((s) => s - mean);
  const power = [];
  for (let k = 1; k <= n >> 1; k++) {
    let real = 0;
    let imaginary = 0;
    for (let j = 0; j < n; j++) {
      const phase = (-2 * Math.PI * k * j) / n;
      real += centred[j] * Math.cos(phase);
      imaginary += centred[j] * Math.sin(phase);
    }
    power.push([real * real + imaginary * imaginary, k]);
  }
  const total = power.reduce((sum, [p]) => sum + p, 0) || 1;
  let dominant = power[0] ? power[0][1] : 1;
  let bestPower = power[0] ? power[0][0] : 0;
  for (const [p, k] of power) if (p > bestPower) { bestPower = p; dominant = k; }

  // The period is measured by shifting the curve onto itself, not read out of
  // the transform. The strongest order is the shape of one well rather than the
  // spacing of them, and a sharp curve's harmonics alias back down past the
  // Nyquist limit -- a 45-degree period sampled every 10 degrees puts its 32nd
  // order onto the 4th, which looks exactly like a 90-degree period.
  const shift = periodShift(scores, span);
  const order = shift && n % shift === 0 ? Math.max(1, n / shift) : 1;
  const measuredPeriod = (360 * shift) / n;

  const expectedOrder = expected ? Math.round(360 / expected) : 0;
  const expectedEntry = power.find(([, k]) => k === expectedOrder);
  // The period has to land on the sample grid to be compared with itself at
  // all, and has to be under the Nyquist limit to be seen.
  const stepDegrees = 360 / n;
  const onGrid = Boolean(expected)
    && Math.abs(expected / stepDegrees - Math.round(expected / stepDegrees)) < 1e-6;
  const resolvable = onGrid && Boolean(expectedOrder) && 2 * expectedOrder < n;

  const forwards = minima.map((m) => m.forward_barrier);
  const reverses = minima.map((m) => m.reverse_barrier);
  const perPeriod = order ? minima.length / order : 0;
  let asymmetry = null;
  if (minima.length && perPeriod > 1.0001) {
    const pairs = forwards.map((f, i) => [f, reverses[i]]).filter(([f, r]) => f + r > 1e-9);
    if (pairs.length) {
      asymmetry = round(pairs.reduce((sum, [f, r]) => sum + (f - r) / (f + r), 0) / pairs.length, 4);
    }
  }

  // Where the structure as it arrived sits in its own landscape. For anything
  // experimental this needs no reference curve: the deposited orientation is
  // the one nature or the refinement picked, so a scorer worth trusting should
  // put it at or near the bottom.
  const atZero = byAngle.get(0);
  const deposited = atZero ? {
    score: atZero.score,
    rank: scores.filter((s) => s < atZero.score).length + 1,
    of: n,
    is_minimum: minima.some((m) => m.angle === 0),
  } : null;

  const clashFree = ordered.filter((row) => !row.clashes).length;

  return {
    complete: true,
    samples: n,
    step: round(360 / n, 4),
    minima,
    deposited,
    clash_free_fraction: round(clashFree / n, 4),
    deep_count: deep.length,
    lesser_count: minima.length - deep.length,
    prominence_threshold: round(threshold, 4),
    range: round(span, 4),
    best: minima.length ? minima.reduce((a, b) => (a.score <= b.score ? a : b)).angle : null,
    period_order: order,
    period: round(measuredPeriod, 4),
    dominant_order: dominant,
    dominant_power: round(bestPower / total, 4),
    period_power: round(power.filter(([, k]) => order && k % order === 0)
      .reduce((sum, [p]) => sum + p, 0) / total, 4),
    expected_period: expected ? round(expected, 4) : 0,
    expected_period_power: expectedEntry ? round(expectedEntry[0] / total, 4) : null,
    period_resolvable: resolvable,
    period_matches_symmetry: resolvable && Math.abs(measuredPeriod - expected) < 1e-6,
    period_note: !expected || resolvable ? '' : (onGrid
      ? `${n} samples cannot resolve ${expectedOrder} wells a turn — scan at `
        + `${fineEnough(expectedOrder, expected)}° or finer to check the period`
      : `a period of ${trim(expected)}° is not a whole number of ${trim(stepDegrees)}° steps, `
        + 'so this scan never compares the curve with itself a period apart — scan at '
        + `${fineEnough(expectedOrder, expected)}° to check it`),
    barrier_mean: forwards.length ? round(forwards.reduce((a, b) => a + b, 0) / forwards.length, 4) : null,
    barrier_max: forwards.length ? round(Math.max(...forwards), 4) : null,
    minima_per_period: round(perPeriod, 3),
    asymmetry,
    asymmetry_note: asymmetry !== null ? '' : (minima.length
      ? 'one minimum per period: forward and reverse barriers are the same peak, '
        + 'so the measure is zero by construction rather than by measurement'
      : 'no wells to compare'),
  };
}

/** The highest point on the way from one index to another, circularly. */
function peakBetween(scores, start, stop, direction) {
  const n = scores.length;
  let peak = scores[start];
  let i = start;
  for (let step = 0; step < n; step++) {
    i = (i + direction + n) % n;
    if (scores[i] > peak) peak = scores[i];
    if (i === stop) break;
  }
  return peak;
}

const PERIOD_TOLERANCE = 0.02;  // of the depth range, when matching a shifted curve

/**
 * The smallest whole-sample shift that maps the curve onto itself.
 *
 * Returns the sample count when nothing smaller works, which is the honest
 * answer for a landscape with no repeat in it. A flat curve matches at every
 * shift and would otherwise report the finest period the sampling allows.
 */
function periodShift(scores, span) {
  const n = scores.length;
  if (span <= 1e-12) return n;
  const tolerance = PERIOD_TOLERANCE * span;
  for (let shift = 1; shift < n; shift++) {
    if (n % shift) continue;  // a period has to divide the turn a whole number of times
    let matches = true;
    for (let j = 0; j < n; j++) {
      if (Math.abs(scores[j] - scores[(j + shift) % n]) > tolerance) { matches = false; break; }
    }
    if (matches) return shift;
  }
  return n;
}

/** The coarsest step that resolves `order` wells and divides the period. */
function fineEnough(order, period = 0) {
  for (const step of [45, 30, 20, 15, 10, 5, 2, 1]) {
    if (order && 2 * order >= 360 / step) continue;
    if (period && Math.abs(period / step - Math.round(period / step)) > 1e-6) continue;
    return step;
  }
  return 1;
}

function trim(value) { return String(Math.round(value * 1000) / 1000); }

function round(value, digits) {
  const scale = 10 ** digits;
  return Math.round(value * scale) / scale;
}
