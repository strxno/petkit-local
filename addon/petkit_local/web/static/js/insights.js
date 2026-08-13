import { BASE, api, esc, toast } from './core.js';
import { onAction, onChange } from './delegate.js';
import { hourStrips, scatterChart, stackedBarChart } from './charts.js';

// ---------------- Insights ----------------
// Per-pet visit history, aggregated CLIENT-SIDE from one flat fetch
// (events/metrics.py::build_insights) — 180 days is a couple of thousand
// small objects, well inside what a browser buckets instantly, and it means
// toggling a pet or a chart never refetches. Unlike the Timeline, this tab is
// deliberately NOT wired into the websocket refresh (see main.js): a
// multi-month scan on every device event would be a self-inflicted load, and
// nothing here needs to be live to the second.

let IN_FROM = null,
  IN_TO = null,
  IN_DEVICE = '';
// Selected series keys: a pet's id as a STRING (matching how every other
// dataset-carried id in this panel is compared, e.g. timeline.js's TL_PET),
// plus the literal 'unattributed'. null means "not yet chosen" — every pet
// and 'unattributed' start selected, the first time data arrives.
let IN_PETS = null;
// The last successful response, so a pet-chip toggle or a preset button can
// re-render without a round trip when nothing server-side changed.
let IN_LAST = null;
// The device list, cached alongside it for the same reason — `renderInsights`
// rebuilds the WHOLE view (including the controls card) on every call, since
// a pet-chip toggle has no cheap way to patch just the chart cards without
// re-deriving what changed; caching this is what makes that full rebuild not
// cost a fetch of its own.
let IN_DEVICES = [];

// Same reasoning as timeline.js's todayLocal(): the server cuts a day at
// LOCAL midnight, so the date pickers have to agree about what "today" and
// "N days ago" mean, which toISOString() (UTC) does not. Duplicated rather
// than imported — timeline.js's copy is single-purpose module state, not a
// shared export, and this is five lines.
function todayLocal() {
  const d = new Date();
  return fmtLocalDate(d);
}
function fmtLocalDate(d) {
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function daysAgoLocal(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return fmtLocalDate(d);
}

//: A small categorical palette, defined once in styles.css (`--pet-1` …
//: `--pet-6`) so it is themed the same way every other colour on this page
//: is — no per-chart light/dark branching. Cycles past 6 pets rather than
//: erroring; a seventh cat reusing a colour is a cosmetic rarity, not a bug.
const PET_COLOR_VARS = ['--pet-1', '--pet-2', '--pet-3', '--pet-4', '--pet-5', '--pet-6'];
const UNATTRIBUTED_COLOR = 'var(--mut)';

function buildPetColors(pets) {
  const map = new Map();
  pets.forEach((p, i) =>
    map.set(String(p.id), `var(${PET_COLOR_VARS[i % PET_COLOR_VARS.length]})`),
  );
  return map;
}
function colorForKey(key, petColors) {
  return key === 'unattributed'
    ? UNATTRIBUTED_COLOR
    : petColors.get(String(key)) || UNATTRIBUTED_COLOR;
}

//: Which pet (or 'unattributed') a visit counts toward — face-confirmed wins,
//: matching `ai/weight.py::attribute_visit`'s own short-circuit; a visit with
//: neither is the "nothing claimed this" bucket every chart shows in grey.
function seriesKey(visit) {
  const id = visit.pet_id ?? visit.attributed_pet_id;
  return id == null ? 'unattributed' : String(id);
}

function petChip(p, colors, selected) {
  const key = String(p.id);
  const swatch = `<span class="chart-swatch" style="background:${esc(colors.get(key))}"></span>`;
  const photo = p.photo_url
    ? `<img class="chip-av hide-on-error" src="${BASE}${esc(p.photo_url)}" alt="">`
    : swatch;
  return `<button class="chip-btn${selected ? ' on' : ''}" data-action="in-pet-toggle" data-k="${esc(key)}">${photo}${esc(p.name)}</button>`;
}

function unattributedChip(selected) {
  return `<button class="chip-btn${selected ? ' on' : ''}" data-action="in-pet-toggle" data-k="unattributed"><span class="chart-swatch" style="background:${UNATTRIBUTED_COLOR}"></span>Unattributed</button>`;
}

function presetButtons() {
  const presets = [
    ['7', '7d'],
    ['30', '30d'],
    ['90', '90d'],
    ['all', 'All'],
  ];
  return presets
    .map(
      ([k, label]) =>
        `<button class="ghost act" data-action="in-preset" data-k="${k}">${label}</button>`,
    )
    .join('');
}

function controlsHtml(ds, pets, colors) {
  return `<div class="card">
    <div class="row" style="align-items:center;margin-bottom:10px">
      <input type="date" id="inFrom" value="${esc(IN_FROM)}" data-change="in-from" style="width:auto">
      <span class="mut">to</span>
      <input type="date" id="inTo" value="${esc(IN_TO)}" data-change="in-to" style="width:auto">
      ${presetButtons()}
      <span class="grow"></span>
      <select id="inDevice" data-change="in-device" style="width:auto">
        <option value="">All devices</option>
        ${ds.map(d => `<option value="${esc(d.id)}"${String(d.id) === IN_DEVICE ? ' selected' : ''}>${esc(d.name)} #${esc(d.id)}</option>`).join('')}
      </select>
    </div>
    ${
      pets.length
        ? `<div class="row" style="align-items:center">
      <div class="chips" role="group" aria-label="Filter by pet">
        ${pets.map(p => petChip(p, colors, IN_PETS.has(String(p.id)))).join('')}
        ${unattributedChip(IN_PETS.has('unattributed'))}
      </div>
    </div>`
        : '<p class="mut">No pets yet — add one in the AI / Pets tab, then set its weight to start attributing visits.</p>'
    }
  </div>`;
}

function retentionNoticeHtml(data) {
  if (data.retention_cutoff_ts == null || data.start >= data.retention_cutoff_ts) return '';
  const cutoffDate = new Date(data.retention_cutoff_ts * 1000).toLocaleDateString();
  return `<div class="card notice">
    <p style="margin:0">History only reaches back to <b>${esc(cutoffDate)}</b> — events older than that have
    been pruned by the events retention window. A quiet stretch before this date more likely means
    "no history left", not "the pet stopped visiting". Raise the window on the
    <a data-action="goto-tab" data-tab="setup">Setup</a> tab.</p>
  </div>`;
}

//: Fallback only — the server always sends `min_corroborating_samples`
//: (`ai/weight.py::MIN_CORROBORATING_SAMPLES`), and this legend uses THAT,
//: never a hardcoded copy of its own. A confirmed real case: a pet with 4
//: confirmed visits (one below the backend's own trust bar of 5) offered a
//: "use this" suggestion here before this fix, at a moment the backend
//: itself would grade every one of that pet's weight matches `unverified`
//: rather than `inferred` — the UI was more confident than the data it was
//: showing. If this constant is ever missing from a response, matching the
//: backend's own default is the safer fallback than an arbitrarily lower one.
const FALLBACK_MIN_SAMPLES = 5;

function legendHtml(pets, colors, visits, minSamples) {
  const counts = new Map();
  for (const v of visits) {
    const key = seriesKey(v);
    const c = counts.get(key) || { confirmed: 0, inferred: 0, unverified: 0 };
    if (v.pet_id != null) c.confirmed++;
    // A weight match's OWN grade decides the bucket — never just "has an
    // attributed_pet_id" — because that binary check is exactly what showed
    // 32 uncorroborated guesses as if they were 32 trusted ones.
    else if (v.grade === 'inferred') c.inferred++;
    else if (v.attributed_pet_id != null) c.unverified++;
    counts.set(key, c);
  }
  const rows = pets
    .map(p => {
      const key = String(p.id);
      const c = counts.get(key) || { confirmed: 0, inferred: 0, unverified: 0 };
      const weightKg = p.weight != null ? (p.weight / 1000).toFixed(2) : null;
      const enoughSamples = p.observed && p.observed.n_confirmed >= minSamples;
      const observed = enoughSamples
        ? `≈ ${(p.observed.median / 1000).toFixed(2)} kg from ${p.observed.n_confirmed} recognised visit${p.observed.n_confirmed === 1 ? '' : 's'} (±${Math.round(p.observed.spread)} g)`
        : p.observed
          ? `${p.observed.n_confirmed} recognised visit${p.observed.n_confirmed === 1 ? '' : 's'} so far — needs ${minSamples} before suggesting a weight`
          : null;
      const suggest = enoughSamples
        ? `<button class="ghost act" data-action="in-suggest-weight" data-id="${esc(p.id)}" data-weight="${esc(p.observed.median)}" data-name="${esc(p.name)}">use this</button>`
        : '';
      return `<div class="row" style="align-items:center;gap:8px">
        <span class="chart-swatch" style="background:${esc(colors.get(key))}"></span>
        <b>${esc(p.name)}</b>
        <span class="mut">${weightKg != null ? weightKg + ' kg entered' : 'no weight set'}</span>
        <span class="grow"></span>
        <span class="mut">${c.confirmed} confirmed · ${c.inferred} inferred${c.unverified ? ` · ${c.unverified} unverified` : ''}</span>
      </div>${observed ? `<p class="sub mut" style="margin:2px 0 8px">${esc(observed)} ${suggest}</p>` : ''}`;
    })
    .join('');
  const unattributedCount = visits.filter(v => seriesKey(v) === 'unattributed').length;
  return `<div class="card">
    <h3>Pets</h3>
    ${rows || '<p class="mut">No pets yet.</p>'}
    <p class="sub mut" style="margin-top:8px">
      <b>Inferred</b> — weight matched a pet whose entered weight is backed by
      ${minSamples}+ recognised visits. <b>Unverified</b> — weight matched, but
      that pet has too few recognised visits to trust the match yet; treat these
      as low-confidence.
    </p>
    ${
      unattributedCount
        ? `<p class="mut" style="margin-top:4px">${unattributedCount} visit${unattributedCount === 1 ? '' : 's'} in this range matched no pet — too far from any entered weight, or ambiguous between two.</p>`
        : ''
    }
  </div>`;
}

//: 50 g bins — fine enough to show two close cats as separate humps, coarse
//: enough that a household's visit count actually fills them in.
const WEIGHT_BUCKET_G = 50;

function weightHistogramHtml(pets, colors, visits) {
  const weighed = visits.filter(v => v.weight != null && IN_PETS.has(seriesKey(v)));
  if (!weighed.length)
    return '<p class="mut">No weighed visits for the selected pets in this range.</p>';
  const idx = w => Math.floor(w / WEIGHT_BUCKET_G);
  const lo = Math.min(...weighed.map(v => idx(v.weight)));
  const hi = Math.max(...weighed.map(v => idx(v.weight)));
  const bars = [];
  for (let i = lo; i <= hi; i++) {
    const segByKey = new Map();
    bars.push({
      label: `${i * WEIGHT_BUCKET_G}g`,
      midpoint: i * WEIGHT_BUCKET_G + WEIGHT_BUCKET_G / 2,
      segByKey,
    });
  }
  for (const v of weighed) {
    const i = idx(v.weight) - lo;
    const key = seriesKey(v);
    bars[i].segByKey.set(key, (bars[i].segByKey.get(key) || 0) + 1);
  }
  const shaped = bars.map(b => ({
    label: b.label,
    midpoint: b.midpoint,
    segments: [...b.segByKey.entries()].map(([key, value]) => ({ key, value })),
  }));
  return stackedBarChart({
    bars: shaped,
    colorFor: k => colorForKey(k, colors),
    title: 'Observed weights — clusters are cats; click a bar to set a pet’s weight from it',
  });
}

function weightOverTimeHtml(pets, colors, visits) {
  const points = visits
    .filter(v => v.weight != null && IN_PETS.has(seriesKey(v)) && seriesKey(v) !== 'unattributed')
    .map(v => ({
      x: v.display_ts ?? v.ts,
      y: v.weight / 1000,
      key: seriesKey(v),
      filled: v.pet_id != null,
      title: `${new Date((v.display_ts ?? v.ts) * 1000).toLocaleString()} · ${(v.weight / 1000).toFixed(2)} kg · ${v.grade || 'unattributed'}`,
    }));
  if (!points.length)
    return '<p class="mut">No selected pet has a weighed visit in this range.</p>';
  const xs = points.map(p => p.x);
  const ys = points.map(p => p.y);
  return scatterChart({
    points,
    xDomain: [Math.min(...xs), Math.max(...xs)],
    yDomain: [0, Math.max(...ys) * 1.15],
    colorFor: k => colorForKey(k, colors),
    xFmt: t => new Date(t * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
    yFmt: v => v.toFixed(1),
  });
}

function visitsPerDayHtml(pets, colors, visits) {
  const dayKey = ts => fmtLocalDate(new Date(ts * 1000));
  const byDay = new Map();
  for (const v of visits) {
    const key = seriesKey(v);
    if (!IN_PETS.has(key)) continue;
    const d = dayKey(v.display_ts ?? v.ts);
    if (!byDay.has(d)) byDay.set(d, new Map());
    const m = byDay.get(d);
    m.set(key, (m.get(key) || 0) + 1);
  }
  if (!byDay.size) return '<p class="mut">No visits from the selected pets in this range.</p>';
  const days = [...byDay.keys()].sort();
  const bars = days.map(d => ({
    label: d.slice(5), // MM-DD — the year rarely matters at chart width
    segments: [...byDay.get(d).entries()].map(([key, value]) => ({ key, value })),
  }));
  return stackedBarChart({ bars, colorFor: k => colorForKey(k, colors) });
}

function timeOfDayHtml(pets, colors, visits) {
  const rows = [];
  for (const p of pets) {
    const key = String(p.id);
    if (!IN_PETS.has(key)) continue;
    const counts = new Array(24).fill(0);
    for (const v of visits) {
      if (seriesKey(v) !== key) continue;
      counts[new Date((v.display_ts ?? v.ts) * 1000).getHours()]++;
    }
    rows.push({ label: p.name, color: colors.get(key), counts });
  }
  if (IN_PETS.has('unattributed')) {
    const counts = new Array(24).fill(0);
    for (const v of visits) {
      if (seriesKey(v) !== 'unattributed') continue;
      counts[new Date((v.display_ts ?? v.ts) * 1000).getHours()]++;
    }
    if (counts.some(c => c > 0))
      rows.push({ label: 'Unattributed', color: UNATTRIBUTED_COLOR, counts });
  }
  if (!rows.length) return '<p class="mut">Nothing to show for the current pet selection.</p>';
  return hourStrips({ rows });
}

function renderInsights() {
  const v = document.getElementById('insightsView');
  if (!v) return;
  if (!IN_LAST) {
    v.innerHTML = '<div class="card"><p class="mut">Loading…</p></div>';
    return;
  }
  const data = IN_LAST;
  const pets = data.pets || [];
  const colors = buildPetColors(pets);

  // Every chart function is handed the FULL visit list and filters by
  // `IN_PETS` itself (each needs a slightly different notion of "selected" —
  // the histogram excludes unweighed visits first, the scatter excludes
  // 'unattributed' entirely) rather than one list built here that would not
  // fit all four.
  v.innerHTML = `
    ${retentionNoticeHtml(data)}
    ${controlsHtml(IN_DEVICES, pets, colors)}
    ${legendHtml(pets, colors, data.visits, data.min_corroborating_samples ?? FALLBACK_MIN_SAMPLES)}
    <div class="card"><h3>Observed weights</h3>${weightHistogramHtml(pets, colors, data.visits)}</div>
    <div class="card"><h3>Weight over time</h3>${weightOverTimeHtml(pets, colors, data.visits)}</div>
    <div class="card"><h3>Visits per day</h3>${visitsPerDayHtml(pets, colors, data.visits)}</div>
    <div class="card"><h3>Time of day</h3>${timeOfDayHtml(pets, colors, data.visits)}</div>
  `;
}

async function loadInsights() {
  if (!IN_TO) {
    IN_TO = todayLocal();
    IN_FROM = daysAgoLocal(29);
  }
  const q = new URLSearchParams({ from: IN_FROM, to: IN_TO });
  if (IN_DEVICE) q.set('device', IN_DEVICE);
  const [ds, data] = await Promise.all([api('devices'), api('insights?' + q.toString())]);
  if (data.error) {
    const v = document.getElementById('insightsView');
    if (v) v.innerHTML = `<div class="card"><p class="mut">${esc(data.error)}</p></div>`;
    return;
  }
  const pets = data.pets || [];
  // A newly added pet (or the very first load) defaults to selected; a
  // deleted one is harmless to leave in the Set — it will never match a
  // `seriesKey()` again. Mirrors timeline.js's "a deleted pet must not keep
  // filtering the view to nothing" rule, just from the other direction.
  if (IN_PETS === null) IN_PETS = new Set(['unattributed']);
  for (const p of pets) IN_PETS.add(String(p.id));

  IN_DEVICES = ds || [];
  IN_LAST = data;
  renderInsights();
}

function setRange(fromStr, toStr) {
  IN_FROM = fromStr;
  IN_TO = toStr;
  loadInsights();
}
onAction('in-preset', el => {
  const k = el.dataset.k;
  const to = todayLocal();
  setRange(k === 'all' ? '1970-01-01' : daysAgoLocal(Number(k) - 1), to);
});
onChange('in-from', el => setRange(el.value, IN_TO));
onChange('in-to', el => setRange(IN_FROM, el.value));
onChange('in-device', el => {
  IN_DEVICE = el.value;
  loadInsights();
});
onAction('in-pet-toggle', el => {
  const k = el.dataset.k;
  if (IN_PETS.has(k)) IN_PETS.delete(k);
  else IN_PETS.add(k);
  renderInsights();
});
onAction('in-suggest-weight', async el => {
  const name = el.dataset.name;
  const weight = Math.round(Number(el.dataset.weight));
  if (!confirm(`Set ${name}'s weight to ${(weight / 1000).toFixed(2)} kg, from confirmed visits?`))
    return;
  const r = await api('pets/' + el.dataset.id, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ weight }),
  });
  if (r.error) {
    toast('Error: ' + r.error);
    return;
  }
  toast('Weight updated');
  loadInsights();
});
onAction('chart-bucket-click', el => {
  const key = el.dataset.key;
  const weightAttr = el.dataset.weight;
  if (!weightAttr || key === 'unattributed' || !IN_LAST) return;
  const pet = (IN_LAST.pets || []).find(p => String(p.id) === key);
  if (!pet) return;
  const weight = Math.round(Number(weightAttr));
  const count = Number(el.dataset.count) || 0;
  // A bucket built from one or two visits is exactly the shape a single bad
  // reading produces — the confirm dialog says so rather than presenting a
  // one-visit bar with the same confidence as a well-supported one.
  const caution =
    count <= 2
      ? `\n\nOnly ${count} visit${count === 1 ? '' : 's'} in this bar — a single bad reading could be behind it.`
      : '';
  if (
    !confirm(
      `Set ${pet.name}'s weight to ${(weight / 1000).toFixed(2)} kg (from ${count} visit${count === 1 ? '' : 's'} in this bar)?${caution}`,
    )
  )
    return;
  api('pets/' + pet.id, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ weight }),
  }).then(r => {
    if (r.error) {
      toast('Error: ' + r.error);
      return;
    }
    toast('Weight updated');
    loadInsights();
  });
});

export { loadInsights };
