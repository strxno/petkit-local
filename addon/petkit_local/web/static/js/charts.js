import { esc } from './core.js';

// ---------------- Charts ----------------
// Hand-rolled inline SVG, not a library: the panel ships no build step and no
// third-party JS at all (see main.js's module map), and these four shapes —
// a histogram, a stacked bar, a scatter and a small-multiple strip — are
// simple enough that owning them outright costs less than wiring a canvas
// library's theming through `getComputedStyle` for four colours. Every colour
// below is a CSS custom property (`var(--fg)`, `var(--pet-1)`, …), so light
// and dark both come free and a redraw on theme change is never needed.
//
// Every chart takes a fixed virtual `viewBox` and is styled `width:100%;
// height:auto` in CSS — responsive and crisp at any zoom with no resize
// listener. Text is real `<text>`, not baked into a raster, so it is
// selectable and legible to a screen reader; a `<title>` on the root and on
// each mark gives the native browser tooltip for free, matching the pattern
// `role="img"`/`<title>` already establishes for accessible SVG here.

const CHART_W = 640;

/** "Nice" round numbers spanning `[min, max]` with roughly `count` ticks —
 * 0, 25, 50 rather than 0, 23.7, 47.4. Always includes 0 when the domain
 * does (every chart here is a non-negative count or a weight), since an axis
 * that does not anchor at zero misreads a bar chart's height as its value. */
function niceTicks(min, max, count = 5) {
  if (!(max > min)) return [min];
  const raw = (max - min) / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
  const start = Math.floor(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 0.001; v += step) ticks.push(Math.round(v * 1000) / 1000);
  return ticks;
}

/** A linear scale: value -> pixel, clamped to `[lo, hi]` in domain space. */
function scaleLinear([dLo, dHi], [rLo, rHi]) {
  const span = dHi - dLo || 1;
  return v => rLo + ((Math.min(dHi, Math.max(dLo, v)) - dLo) / span) * (rHi - rLo);
}

function axisY(ticks, y, x0, x1, fmt) {
  return ticks
    .map(
      t => `<g class="chart-grid">
      <line x1="${x0}" x2="${x1}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"></line>
      <text x="${x0 - 6}" y="${y(t).toFixed(1)}" text-anchor="end" dominant-baseline="middle">${esc(fmt ? fmt(t) : t)}</text>
    </g>`,
    )
    .join('');
}

/** Stacked bars over discrete buckets — the weight histogram AND "visits per
 * day" are the same shape (N buckets, each a stack of coloured segments), so
 * one renderer backs both rather than two near-duplicates.
 *
 * `bars`: `[{label, midpoint, segments: [{key, value}]}]`. `colorFor(key)`
 * resolves a segment's fill. `midpoint`, when a bar has one (the weight
 * histogram's bucket centre in grams), rides along as `data-weight` on every
 * segment so a click handler can act on it without re-parsing the label.
 */
function stackedBarChart({ bars, colorFor, height = 220, yFmt, title }) {
  const padL = 46,
    padR = 12,
    padT = 12,
    padB = 34;
  const innerW = CHART_W - padL - padR;
  const innerH = height - padT - padB;
  const n = Math.max(1, bars.length);
  const bw = innerW / n;
  const totals = bars.map(b => (b.segments || []).reduce((s, seg) => s + (seg.value || 0), 0));
  const maxY = Math.max(1, ...totals);
  const ticks = niceTicks(0, maxY, 4);
  const yTop = ticks[ticks.length - 1] || maxY;
  const y = scaleLinear([0, yTop], [padT + innerH, padT]);

  const barsSvg = bars
    .map((bar, i) => {
      const x = padL + i * bw + bw * 0.12;
      const w = bw * 0.76;
      let yCursor = padT + innerH;
      const segs = (bar.segments || [])
        .filter(s => s.value > 0)
        .map(seg => {
          const h = (innerH * seg.value) / yTop;
          yCursor -= h;
          const weightAttr = bar.midpoint != null ? ` data-weight="${esc(bar.midpoint)}"` : '';
          return `<rect class="chart-mark" x="${x.toFixed(1)}" y="${yCursor.toFixed(1)}" width="${w.toFixed(1)}" height="${Math.max(0, h).toFixed(1)}" fill="${esc(colorFor(seg.key))}" data-action="chart-bucket-click" data-bucket="${esc(bar.label)}" data-key="${esc(seg.key)}" data-count="${esc(seg.value)}"${weightAttr}><title>${esc(bar.label)}: ${seg.value}</title></rect>`;
        })
        .join('');
      const showLabel = n <= 14 || i % Math.ceil(n / 14) === 0;
      return `${segs}${showLabel ? `<text class="chart-xlabel" x="${(x + w / 2).toFixed(1)}" y="${(padT + innerH + 16).toFixed(1)}" text-anchor="middle">${esc(bar.label)}</text>` : ''}`;
    })
    .join('');

  return `<svg viewBox="0 0 ${CHART_W} ${height}" role="img" class="insight-chart">
    ${title ? `<title>${esc(title)}</title>` : ''}
    ${axisY(ticks, y, padL, CHART_W - padR, yFmt)}
    ${barsSvg}
    <line class="chart-axis" x1="${padL}" x2="${padL}" y1="${padT}" y2="${padT + innerH}"></line>
    <line class="chart-axis" x1="${padL}" x2="${CHART_W - padR}" y1="${padT + innerH}" y2="${padT + innerH}"></line>
  </svg>`;
}

/** Weight over time — a scatter, not a line: every point is one visit, and
 * the SPREAD is the story a connected line would paper over. A filled circle
 * is face-confirmed; a hollow ring is weight-inferred, at whatever grade the
 * caller already decided — see `insights.js`'s legend for what each means. */
function scatterChart({ points, xDomain, yDomain, colorFor, height = 220, yFmt, xFmt }) {
  const padL = 46,
    padR = 12,
    padT = 12,
    padB = 30;
  const innerW = CHART_W - padL - padR;
  const innerH = height - padT - padB;
  const x = scaleLinear(xDomain, [padL, padL + innerW]);
  const y = scaleLinear(yDomain, [padT + innerH, padT]);
  const yTicks = niceTicks(yDomain[0], yDomain[1], 4);
  const xTicks = niceTicks(xDomain[0], xDomain[1], 5);

  const dots = points
    .map(p => {
      const cx = x(p.x).toFixed(1);
      const cy = y(p.y).toFixed(1);
      const stroke = colorFor(p.key);
      return p.filled
        ? `<circle class="chart-mark" cx="${cx}" cy="${cy}" r="3.4" fill="${esc(stroke)}"><title>${esc(p.title || '')}</title></circle>`
        : `<circle class="chart-mark" cx="${cx}" cy="${cy}" r="3.4" fill="none" stroke="${esc(stroke)}" stroke-width="1.4"><title>${esc(p.title || '')}</title></circle>`;
    })
    .join('');

  const xLabels = xTicks
    .map(
      t =>
        `<text class="chart-xlabel" x="${x(t).toFixed(1)}" y="${(padT + innerH + 16).toFixed(1)}" text-anchor="middle">${esc(xFmt ? xFmt(t) : t)}</text>`,
    )
    .join('');

  return `<svg viewBox="0 0 ${CHART_W} ${height}" role="img" class="insight-chart">
    ${axisY(yTicks, y, padL, CHART_W - padR, yFmt)}
    ${dots}
    ${xLabels}
    <line class="chart-axis" x1="${padL}" x2="${padL}" y1="${padT}" y2="${padT + innerH}"></line>
    <line class="chart-axis" x1="${padL}" x2="${CHART_W - padR}" y1="${padT + innerH}" y2="${padT + innerH}"></line>
  </svg>`;
}

/** Small multiples: one 24-bucket hour-of-day strip per row (per pet plus
 * "unattributed"). A single polar clock reads worse for comparing several
 * pets at once than a stack of identical, aligned strips does. */
function hourStrips({ rows, rowH = 34 }) {
  const padL = 90,
    padR = 12,
    padT = 4,
    gap = 6;
  const innerW = CHART_W - padL - padR;
  const bw = innerW / 24;
  const maxCount = Math.max(1, ...rows.flatMap(r => r.counts));
  const height = padT + rows.length * (rowH + gap);

  const body = rows
    .map((row, ri) => {
      const rowY = padT + ri * (rowH + gap);
      const bars = row.counts
        .map((c, h) => {
          const bh = (rowH * c) / maxCount;
          const x = padL + h * bw;
          return `<rect class="chart-mark" x="${(x + bw * 0.08).toFixed(1)}" y="${(rowY + rowH - bh).toFixed(1)}" width="${(bw * 0.84).toFixed(1)}" height="${Math.max(0, bh).toFixed(1)}" fill="${esc(row.color)}"><title>${esc(row.label)} · ${h}:00 · ${c}</title></rect>`;
        })
        .join('');
      return `<text class="chart-rowlabel" x="${padL - 8}" y="${(rowY + rowH / 2).toFixed(1)}" text-anchor="end" dominant-baseline="middle">${esc(row.label)}</text>
      <line class="chart-axis" x1="${padL}" x2="${CHART_W - padR}" y1="${(rowY + rowH).toFixed(1)}" y2="${(rowY + rowH).toFixed(1)}"></line>
      ${bars}`;
    })
    .join('');

  // A handful of hour labels along the bottom, not all 24 — matches the
  // "skip labels once there are too many buckets" rule `stackedBarChart` uses.
  const hourLabels = [0, 6, 12, 18, 23]
    .map(
      h =>
        `<text class="chart-xlabel" x="${(padL + h * bw + bw / 2).toFixed(1)}" y="${(height + 12).toFixed(1)}" text-anchor="middle">${h}:00</text>`,
    )
    .join('');

  return `<svg viewBox="0 0 ${CHART_W} ${height + 16}" role="img" class="insight-chart">
    ${body}${hourLabels}
  </svg>`;
}

export { CHART_W, niceTicks, scaleLinear, stackedBarChart, scatterChart, hourStrips };
