"""charts.js — the Insights tab's hand-rolled SVG chart primitives.

Same shape as `test_panel_script_loads.py`'s `PROVISION_ASSERTIONS` harness:
import the real module against a minimal DOM/`esc` stub and run assertions
inside Node, since these are the only real tests a hand-rolled SVG renderer
gets without a browser. The two things worth pinning down: the output is
well-formed SVG, and a hostile pet/label string cannot break out of it — the
whole reason this panel escapes through `esc()` rather than trusting what a
device or a typed name puts on the page.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parent.parent / "petkit_local" / "web" / "static" / "js"
CHARTS_JS = JS_DIR / "charts.js"

#: `charts.js` imports `esc` from `core.js`; Node has no bundler here, so the
#: import is stubbed rather than resolved — the same escaping rule `core.js`
#: itself documents (`&<>"'/` -> entities), reimplemented in one line because
#: pulling in the real file would also pull in `BASE`/`fetch`/DOM globals
#: charts.js never needs.
ESC_STUB = """
globalThis.__ESC_MAP = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','/':'&#x2F;'};
"""

ASSERTIONS = """
const bars = [
  {label: '3200g', midpoint: 3200, segments: [{key: '1', value: 3}, {key: 'unattributed', value: 1}]},
  {label: '3250g', midpoint: 3250, segments: [{key: '2', value: 5}]},
];
const colorFor = k => (k === '1' ? '#2563eb' : k === '2' ? '#ea580c' : '#888');

const hist = stackedBarChart({bars, colorFor, title: 'Weights'});
if (!hist.includes('<svg') || !hist.includes('</svg>')) throw new Error('stackedBarChart: not well-formed SVG');
if (!hist.includes('data-weight="3200"')) throw new Error('stackedBarChart: midpoint did not reach data-weight');

const points = [
  {x: 100, y: 3.2, key: '1', filled: true, title: 'a'},
  {x: 200, y: 3.4, key: '1', filled: false, title: 'b'},
];
const scatter = scatterChart({points, xDomain: [0, 300], yDomain: [0, 5], colorFor});
if (!scatter.includes('<svg') || !scatter.includes('</svg>')) throw new Error('scatterChart: not well-formed SVG');

const rows = [
  {label: 'Milo', color: '#2563eb', counts: new Array(24).fill(0).map((_, i) => (i === 3 ? 5 : 1))},
];
const strips = hourStrips({rows});
if (!strips.includes('<svg') || !strips.includes('</svg>')) throw new Error('hourStrips: not well-formed SVG');

// Every axis-tick label and bar label passes through `esc()`, so a name like
// a pet's — user-typed, never validated for markup — cannot inject a tag.
const hostile = stackedBarChart({
  bars: [{label: '"><script>alert(1)</script>', segments: [{key: 'x"><b>evil</b>', value: 1}]}],
  colorFor: () => '#000',
});
if (hostile.includes('<script>')) throw new Error('stackedBarChart: hostile label was not escaped');
if (/[^&]<b>evil/.test(hostile)) throw new Error('stackedBarChart: hostile key was not escaped');

// niceTicks must not divide by zero or loop forever on a flat domain.
const flat = niceTicks(5, 5, 4);
if (!Array.isArray(flat) || flat.length < 1) throw new Error('niceTicks: broke on a zero-width domain');

console.log('CHARTS_OK');
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_charts_js_renders_well_formed_svg_and_escapes_hostile_input(tmp_path):
    harness = tmp_path / "charts.mjs"
    source = CHARTS_JS.read_text()
    # Same trick `test_panel_script_loads.py` avoids needing at all (it can
    # import the real files): here the one import (`esc` from `core.js`) is
    # swapped for a stub defined inline, then the module body is appended
    # verbatim so every export is a real top-level binding in the harness.
    body = source.replace(
        "import { esc } from './core.js';",
        "const esc = s => (s == null ? '' : String(s)).replace(/[&<>\"'\\/]/g, "
        "c => globalThis.__ESC_MAP[c]);",
    )
    harness.write_text(ESC_STUB + body + ASSERTIONS)
    r = subprocess.run(["node", str(harness)], capture_output=True, text=True, timeout=60)
    assert "CHARTS_OK" in r.stdout, r.stderr.strip()[:2000]
