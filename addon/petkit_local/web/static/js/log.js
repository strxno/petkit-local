import { BASE, api, esc, toast, fmtTs, fmtBytes, copyText } from './core.js';
import { onAction, onChange, onInput } from './delegate.js';

// ---------------- Live log ----------------
const LOG = { rows: [], device: '' };
const logDevices = new Map();

function updateLogDevices() {
  const pick = document.getElementById('logDevice');
  if (!pick) return;
  pick.innerHTML =
    '<option value="">All devices</option>' +
    [...logDevices]
      .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }))
      .map(([id, name]) => `<option value="${esc(id)}">${esc(name)} #${esc(id)}</option>`)
      .join('');
  pick.value = LOG.device;
}

function matchesLogDevice(e) {
  return !LOG.device || (e.device_id != null && String(e.device_id) === LOG.device);
}

onChange('log-device', el => {
  LOG.device = el.value;
  const view = document.getElementById('logView');
  [...view.children].forEach((node, i) => {
    node.style.display = matchesLogDevice(LOG.rows[i]) ? '' : 'none';
  });
  showLogCount();
});

api('devices')
  .then(devices => {
    for (const d of devices) logDevices.set(String(d.id), d.name || d.type || 'Device');
    updateLogDevices();
  })
  .catch(() => {});
function logDetailText(dt) {
  // Not every log entry is an HTTP exchange — a redaction detail has no method
  // or status, and rendering "undefined undefined → undefined" for it is worse
  // than showing the object.
  if (dt && dt.topic) return mqttDetailText(dt);
  if (!dt || !dt.method) return JSON.stringify(dt, null, 2);
  const L = [];
  L.push(dt.method + ' ' + dt.path + ' → ' + dt.status);
  if (dt.headers && Object.keys(dt.headers).length) {
    L.push('');
    L.push('── Request headers ──');
    for (const k in dt.headers) L.push(k + ': ' + dt.headers[k]);
  }
  if (dt.query && Object.keys(dt.query).length) {
    L.push('');
    L.push('── Query ──');
    for (const k in dt.query) L.push(k + ' = ' + dt.query[k]);
  }
  if (dt.req_body) {
    L.push('');
    L.push('── Request body ──');
    L.push(pretty(dt.req_body));
  }
  if (dt.proxy) {
    const p = dt.proxy;
    L.push('');
    L.push('── Proxied to the real cloud ──');
    L.push(p.upstream);
    L.push(
      'status ' +
        p.status +
        (p.error ? '  ·  error ' + p.error.code + ' ' + (p.error.msg || '') : ''),
    );
    L.push(
      'served to the device: ' +
        (p.served === 'upstream' ? 'the cloud’s reply' : 'OUR reply (upstream unusable)'),
    );
    if (p.redactions && p.redactions.length) L.push('redacted: ' + p.redactions.join(', '));
    if (p.upstream_body) {
      L.push('');
      L.push('── What the cloud sent ──');
      L.push(pretty(p.upstream_body));
    }
  }
  if (dt.resp_body) {
    L.push('');
    L.push('── Sent to the device ──');
    L.push(pretty(dt.resp_body));
  }
  return L.join('\n');
}
// An MQTT frame: who it was between, which way it went, and what was in it.
// The summary line only has room for the tail of the topic, so the full one is
// repeated here.
function mqttDetailText(dt) {
  const L = [dt.direction || ''];
  if (dt.client) L.push('client: ' + dt.client);
  if (dt.origin) L.push('origin: ' + dt.origin);
  L.push(dt.topic);
  if (dt.payload) {
    L.push('');
    L.push('── Payload ──');
    L.push(pretty(dt.payload));
  }
  return L.join('\n');
}
function pretty(s) {
  try {
    return JSON.stringify(JSON.parse(s), null, 2);
  } catch (e) {
    return s;
  }
}
// A heartbeat is not its own event kind — it is an `http` row whose summary
// names the endpoint. Both the row renderer and the download filter have to
// agree on that, so the test lives here and they share it.
const isHeartbeat = e => e.kind === 'http' && /heartbeat/.test(e.summary || '');

function makeLogNode(e) {
  const hb = isHeartbeat(e);
  const dt = e.detail ? logDetailText(e.detail) : '';
  return (
    `<div class="l ${dt ? 'exp' : ''}" style="${matchesLogDevice(e) ? '' : 'display:none'}" data-hb="${hb ? 1 : 0}"${dt ? ' data-action="toggle-log"' : ''}>` +
    `<div class="lh"><span class="mut">${fmtTs(e.ts)}</span><span class="k ${esc(e.kind)}">${esc(e.kind)}</span>` +
    `${e.device_id != null ? `<span class="mut">#${esc(e.device_id)}</span>` : ''}<span class="lsum">${esc(e.summary)}</span>` +
    // Inside the row, which also carries data-action="toggle-log". `delegate`
    // resolves the innermost [data-action], so this swallows the click instead
    // of expanding the row on the way past — no stopPropagation needed.
    `${dt ? '<button class="mini logcopy" data-action="copy-log" title="Copy this entry">Copy</button>' : ''}` +
    `${dt ? '<span class="mut chev">▸</span>' : ''}</div>` +
    `${dt ? `<pre class="ld">${esc(dt)}</pre>` : ''}</div>`
  );
}
onAction('toggle-log', el => el.classList.toggle('open'));
// The whole entry, header included: a pasted detail with no timestamp or
// endpoint on it is hard to place once it is out of the list.
onAction('copy-log', el => {
  const row = el.closest('.l');
  const head = row && row.querySelector('.lh');
  const body = row && row.querySelector('pre.ld');
  const headText = head
    ? Array.from(head.children)
        .filter(c => c !== el && !c.classList.contains('chev'))
        .map(c => c.textContent.trim())
        .filter(Boolean)
        .join(' ')
    : '';
  copyText([headText, body ? body.textContent : ''].filter(Boolean).join('\n'));
});
//: How many rows this browser keeps. The cap is the reason the count is worth
//: showing at all: "Device logs" reports its line count off the server's whole
//: file, while this list silently drops its oldest row past the limit — so the
//: one that needed saying was the one that never said it.
const LOG_CAP = 600;

function showLogCount() {
  const el = document.getElementById('logCount');
  if (!el) return;
  if (LOG.device) {
    const hideHb = document.getElementById('hideHb').checked;
    el.textContent = `${LOG.rows.filter(e => matchesLogDevice(e) && !(hideHb && isHeartbeat(e))).length} matching entries / ${LOG.rows.length} buffered`;
    return;
  }
  el.textContent = !LOG.rows.length
    ? ''
    : LOG.rows.length >= LOG_CAP
      ? `last ${LOG_CAP} requests`
      : `${LOG.rows.length} requests`;
}

function pushLog(e) {
  if (e.kind === 'ping') return;
  if (e.device_id != null && !logDevices.has(String(e.device_id))) {
    logDevices.set(String(e.device_id), 'Device');
    updateLogDevices();
  }
  LOG.rows.push(e);
  if (LOG.rows.length > LOG_CAP) LOG.rows.shift();
  const v = document.getElementById('logView');
  if (!v) return;
  const nearBottom = v.scrollHeight - v.scrollTop - v.clientHeight < 60;
  v.insertAdjacentHTML('beforeend', makeLogNode(e));
  while (v.children.length > LOG_CAP) v.removeChild(v.firstChild);
  if (document.getElementById('autoscroll').checked && nearBottom) v.scrollTop = v.scrollHeight;
  showLogCount();
}
function clearLog() {
  LOG.rows = [];
  document.getElementById('logView').innerHTML = '';
  showLogCount();
}
onAction('clear-log', () => clearLog());

// Save what is on screen, in the same shape the expanded rows show — the point
// of the download is attaching it to a bug report, and a second format nobody
// has read would defeat that. Honours "hide heartbeats" so the file matches
// what the user was actually looking at when they hit download; that used to
// test `e.kind === 'heartbeat'`, which no event ever is, so the checkbox was
// silently ignored.
function downloadLog() {
  const hideHb = document.getElementById('hideHb').checked;
  const rows = LOG.rows.filter(e => matchesLogDevice(e) && !(hideHb && isHeartbeat(e)));
  if (!rows.length) return toast('Nothing to download yet');
  const text = rows
    .map(e => {
      const head = `[${fmtTs(e.ts)}] ${e.kind}${e.device_id != null ? ' #' + e.device_id : ''} — ${e.summary || ''}`;
      const body = e.detail ? logDetailText(e.detail) : '';
      return body ? head + '\n' + body : head;
    })
    .join('\n\n' + '─'.repeat(70) + '\n\n');
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `petkit-local-traffic-${stamp}.log`;
  a.click();
  // Revoking immediately can cancel the download in some browsers; one tick is
  // enough for the navigation to have been started.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`Saved ${rows.length} rows`);
}
onAction('download-log', () => downloadLog());
document.getElementById('hideHb').addEventListener('change', e => {
  document.body.classList.toggle('hidehb', e.target.checked);
  showLogCount();
});
document.body.classList.toggle('hidehb', document.getElementById('hideHb').checked);

// ---------------- Device logs (uploaded by the device itself) ----------------
// Filtering is server-side: a devRun.log runs to a few thousand lines, and
// shipping the whole file on every keystroke costs more than shipping the
// matches. The input is debounced so a fast typist queues one request, not ten.
const DEVLOG = { files: [], sel: '', q: '', reason: '' };
let _devlogT = null;

const DEVLOG_REASONS = {
  no_log_root: 'no log directory is configured',
  no_bucket_endpoint: 'this app has no bucket address, so there is nowhere to send the device',
  authority_not_splittable:
    'the bucket address cannot be split into a host the device can build — it needs a dotted name or IP',
};

async function loadDeviceLogs() {
  const r = await api('devicelogs');
  DEVLOG.files = r.files || [];
  DEVLOG.reason = r.reason || '';
  const pick = document.getElementById('devLogPick');
  if (!pick) return;
  if (!DEVLOG.files.length) {
    pick.innerHTML = '<option value="">no logs yet</option>';
    const why = DEVLOG.reason
      ? `Cannot collect: ${DEVLOG_REASONS[DEVLOG.reason] || esc(DEVLOG.reason)}.`
      : (r.enabled_devices || []).length
        ? 'Collection is on. The device uploads on its own schedule — a reboot is the usual trigger.'
        : 'No device has debug log collection turned on.';
    document.getElementById('devLogView').innerHTML =
      `<p class="mut" style="padding:14px">${esc(why)}</p>`;
    document.getElementById('devLogDl').style.display = 'none';
    document.getElementById('devLogCount').textContent = '';
    return;
  }
  if (!DEVLOG.files.some(f => f.rel === DEVLOG.sel)) DEVLOG.sel = DEVLOG.files[0].rel;
  pick.innerHTML = DEVLOG.files
    .map(
      f =>
        `<option value="${esc(f.rel)}" ${f.rel === DEVLOG.sel ? 'selected' : ''}>${esc(fmtTs(f.mtime))} · #${esc(f.device ?? '?')} · ${esc(fmtBytes(f.size))}</option>`,
    )
    .join('');
  readDeviceLog();
}

function devlogUrl(rel, extra) {
  return 'devicelogs/' + rel.split('/').map(encodeURIComponent).join('/') + (extra || '');
}

async function readDeviceLog() {
  if (!DEVLOG.sel) return;
  const q = DEVLOG.q;
  const r = await api(devlogUrl(DEVLOG.sel, '?q=' + encodeURIComponent(q)));
  if (q !== DEVLOG.q) return; // a newer keystroke already won
  const view = document.getElementById('devLogView');
  const dl = document.getElementById('devLogDl');
  dl.style.display = '';
  dl.href = BASE + 'api/' + devlogUrl(DEVLOG.sel, '?download=1');
  document.getElementById('devLogCount').textContent = q
    ? `${r.matched} of ${r.total} lines`
    : `${r.total} lines`;
  if (!r.lines.length) {
    view.innerHTML = `<p class="mut" style="padding:14px">No line matches.</p>`;
    return;
  }
  view.innerHTML = r.lines
    .map(
      ([n, text]) =>
        `<div class="dl"><span class="ln">${n}</span><span>${hl(text, q)}</span></div>`,
    )
    .join('');
}

// Highlight without ever emitting unescaped input: the raw line is split around
// each match and EVERY fragment is escaped, so `<mark>` is the only markup that
// reaches the DOM. Single-term queries only — with several terms there is no
// one span to mark.
function hl(text, q) {
  const terms = q.split(/\s+/).filter(Boolean);
  if (terms.length !== 1) return esc(text);
  const needle = terms[0].toLowerCase();
  const low = text.toLowerCase();
  let out = '',
    i = 0,
    j;
  while ((j = low.indexOf(needle, i)) !== -1) {
    out += esc(text.slice(i, j)) + '<mark>' + esc(text.slice(j, j + needle.length)) + '</mark>';
    i = j + needle.length;
  }
  return out + esc(text.slice(i));
}

onChange('devlog-pick', el => {
  DEVLOG.sel = el.value;
  readDeviceLog();
});
onInput('devlog-grep', el => {
  DEVLOG.q = el.value;
  clearTimeout(_devlogT);
  _devlogT = setTimeout(readDeviceLog, 200);
});

export { DEVLOG_REASONS, pushLog, loadDeviceLogs };
