/* ----------------------------------------------------------------------
 * Shared runtime for LPT's deterministic HTML campaign/run reports
 * (src/binder_report.py, src/ppi_report.py). Dom utils, the rail-nav +
 * hero renderers (generic given REPORT.rail / REPORT.hero, built the same
 * way by every report), the histogram chart primitive, and the Mol*
 * structure-explorer harness (structure loading, hotspot highlighting,
 * water/ion hiding) — every piece here is track-agnostic. Track-specific
 * rendering (design cards, funnel/scatter charts, narrative sections)
 * stays in each report's own app.js.
 * ------------------------------------------------------------------- */

/* ---------------------------------------------------------------- utils */
function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}
function fmt(x, d) { return (typeof x === 'number') ? x.toFixed(d === undefined ? 2 : d) : x; }
function pct(x, d) { return (typeof x === 'number') ? (x * 100).toFixed(d === undefined ? 1 : d) + '%' : '—'; }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }

/* ---------------------------------------------------------------- hero + rail */
function renderRail() {
  const r = REPORT.rail;
  const nav = r.nav.map(n => `<a href="${n.href}">${esc(n.label)}</a>`).join('');
  const stats = r.stats.map(s => `<div class="railstat"><div class="k">${esc(s.k)}</div><div class="v"${s.color ? ` style="color:${s.color}"` : ''}>${esc(s.v)}${s.unit ? `<small>${esc(s.unit)}</small>` : ''}</div></div>`).join('');
  document.getElementById('railnav').innerHTML = `
    <div class="brand">${esc(r.brand)}</div>
    <div class="target">${esc(r.target)}</div>
    <div class="pdb mono">${esc(r.pdb_line)}</div>
    <nav>${nav}</nav>
    ${stats}
  `;
}

function renderHero() {
  const h = REPORT.hero;
  const pills = h.pills.map(p => `<span class="pill ${p.kind || ''} ${p.kind === 'good' ? 'dot' : ''}">${esc(p.label)}</span>`).join('');
  const stats = h.stats.map(s => `<div class="statcell"><div class="k">${esc(s.k)}</div><div class="v">${esc(s.v)}${s.unit ? `<span class="unit">${esc(s.unit)}</span>` : ''}</div>${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}</div>`).join('');
  document.getElementById('hero').innerHTML = `
    <div class="eyebrow">${esc(h.eyebrow)}</div>
    <h1>${esc(h.title)}</h1>
    <p class="lede">${esc(h.subtitle)}</p>
    <div class="pillrow">${pills}</div>
    <div class="statgrid">${stats}</div>
  `;
}

/* ---------------------------------------------------------------- appendix */
/* Every stage's own markdown report, rendered whole (REPORT.appendix, built
 * by report_common.stage_documents). Track-agnostic: a track decides WHICH
 * files go in the list, not how they are shown. */
function renderAppendix() {
  const wrap = document.getElementById('appendix-docs');
  if (!wrap) return;
  const docs = REPORT.appendix || [];
  const idx = document.getElementById('appendix-index');
  if (!docs.length) {
    wrap.innerHTML = '<div class="empty-note">No stage reports found for this run.</div>';
    return;
  }
  idx.innerHTML =
    docs.map(d => `<a class="docchip" href="#${esc(d.id)}">${esc(d.num)} · ${esc(d.label)}</a>`).join('') +
    '<button class="docchip toggle" id="appendix-toggle">Collapse all</button>';
  wrap.innerHTML = docs.map(d => `
    <details class="stagedoc" open id="${esc(d.id)}">
      <summary>
        <span class="num">${esc(d.num)}</span>
        <span class="label">${esc(d.label)}</span>
        <span class="file mono">${esc(d.path)}${(d.also || []).map(a => ' = ' + esc(a)).join('')}</span>
      </summary>
      <div class="docbody"><div class="prose">${d.html}</div></div>
    </details>`).join('');

  // An index link must not scroll to a collapsed section — open it first.
  idx.querySelectorAll('a.docchip').forEach(a => {
    a.addEventListener('click', () => {
      const target = document.getElementById(a.getAttribute('href').slice(1));
      if (target) target.open = true;
    });
  });
  const btn = document.getElementById('appendix-toggle');
  btn.dataset.next = 'collapse';
  btn.addEventListener('click', () => {
    const collapse = btn.dataset.next !== 'expand';
    wrap.querySelectorAll('details.stagedoc').forEach(d => { d.open = !collapse; });
    btn.dataset.next = collapse ? 'expand' : 'collapse';
    btn.textContent = collapse ? 'Expand all' : 'Collapse all';
  });
}

/* ---------------------------------------------------------------- footer */
function renderFooter() {
  document.getElementById('methods').innerHTML = REPORT.footer_html;
}

/* ---------------------------------------------------------------- charts: histogram */
function renderHistogram(svgId, hist, opts) {
  const svg = document.getElementById(svgId);
  const W = 320, H = 170, padL = 26, padR = 8, padT = 10, padB = 24;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const maxCount = Math.max(...hist.counts, 1);
  const n = hist.counts.length;
  const barW = plotW / n;
  const threshold = opts && opts.threshold;
  let s = '';
  for (let gy = 0; gy <= 4; gy++) {
    const y = padT + plotH - (gy / 4) * plotH;
    s += `<line class="gridline" x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}"/>`;
    s += `<text class="axislabel" x="${padL - 4}" y="${y + 3}" text-anchor="end">${Math.round(maxCount * gy / 4)}</text>`;
  }
  hist.counts.forEach((c, i) => {
    const h = (c / maxCount) * plotH;
    const x = padL + i * barW;
    const y = padT + plotH - h;
    const binMid = (hist.edges[i] + hist.edges[i + 1]) / 2;
    const hot = threshold !== undefined && binMid >= threshold;
    s += `<rect class="bar ${hot ? 'bar-hot' : ''}" x="${(x + 0.6).toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(barW - 1.2, 0.5).toFixed(1)}" height="${Math.max(h, 0).toFixed(1)}"/>`;
  });
  if (threshold !== undefined) {
    const tx = padL + ((threshold - hist.edges[0]) / (hist.edges[hist.edges.length - 1] - hist.edges[0])) * plotW;
    s += `<line class="thresholdline" x1="${tx}" x2="${tx}" y1="${padT}" y2="${padT + plotH}"/>`;
  }
  [0, 0.25, 0.5, 0.75, 1].forEach(f => {
    const x = padL + f * plotW;
    s += `<text class="axislabel" x="${x}" y="${H - 6}" text-anchor="middle">${f}</text>`;
  });
  svg.innerHTML = s;
}

/* ---------------------------------------------------------------- charts: horizontal bar rows
 * Generic version of binder's original renderFunnel: rows = [{label, pct, note}],
 * note (e.g. "-N first") is optional and drawn on-bar. */
function renderBarRows(svgId, rows, opts) {
  const svg = document.getElementById(svgId);
  if (!rows.length) { svg.innerHTML = ''; return; }
  const W = (opts && opts.width) || 640, padL = (opts && opts.padL) || 160, padR = (opts && opts.padR) || 60;
  const padT = 6, rowH = 24, gap = 3;
  const plotW = W - padL - padR;
  let s = '';
  rows.forEach((r, i) => {
    const y = padT + i * (rowH + gap);
    const w = (r.pct / 100) * plotW;
    s += `<text class="axislabel" x="${padL - 8}" y="${y + rowH / 2 + 3}" text-anchor="end" style="font-size:10.5px">${esc(r.label)}</text>`;
    s += `<rect class="bar-track" x="${padL}" y="${y}" width="${plotW}" height="${rowH}"/>`;
    s += `<rect class="bar" x="${padL}" y="${y}" width="${w.toFixed(1)}" height="${rowH}"/>`;
    s += `<text class="axislabel" x="${padL + plotW + 6}" y="${y + rowH / 2 + 3}" text-anchor="start" style="font-size:10.5px; font-weight:600;">${r.pct.toFixed(1)}%</text>`;
    if (r.note) {
      s += `<text class="axislabel on-bar" x="${padL + Math.min(w, plotW) - 6}" y="${y + rowH / 2 + 3}" text-anchor="end" style="font-size:9.5px">${esc(r.note)}</text>`;
    }
  });
  svg.setAttribute('viewBox', `0 0 ${W} ${padT + rows.length * (rowH + gap) + 4}`);
  svg.innerHTML = s;
}

/* ---------------------------------------------------------------- mol* structure explorer
 * Factory over a shared single viewer instance. `opts`:
 *   structs           - {key: {b64, target_chain, binder_chain, ...}}
 *   hotspotResidueIds - (key) => [auth_seq_id, ...] to select/focus, or [] —
 *                        called with the structure key being loaded, since
 *                        a design's own numbering can differ from the
 *                        native structure's (e.g. RFD3/BoltzGen renumber
 *                        the target chain in their output)
 *   onLoaded          - (key) => void, called after a structure loads (caption etc.)
 *   stageId           - container element id (default 'molstar-stage')
 */
function createStructureExplorer(opts) {
  const structs = opts.structs || {};
  const stageId = opts.stageId || 'molstar-stage';
  const captionId = opts.captionId || 'explorer-caption';
  let viewer = null, viewerReady = false, currentKey = null;

  function b64ToText(b64) { return atob(b64); }

  function hideWaterIon(v) {
    try {
      const structsCur = v.plugin.managers.structure.hierarchy.current.structures;
      if (!structsCur || !structsCur.length) return;
      const struct = structsCur[0];
      const hide = struct.components.filter(c => /water|ion/i.test((c.cell.obj && c.cell.obj.label) || ''));
      if (hide.length) v.plugin.managers.structure.component.toggleVisibility(hide);
    } catch (e) { console.warn('hideWaterIon failed', e); }
  }

  function commonViewerOptions() {
    return {
      layoutIsExpanded: false,
      layoutShowControls: true,
      layoutShowRemoteState: false,
      layoutShowSequence: true,
      layoutShowLog: false,
      layoutShowLeftPanel: false,
      collapseRightPanel: false,
      disabledExtensions: ['pdbe-structure-quality-report', 'dnatco-ntcs', 'assembly-symmetry', 'rcsb-validation-report', 'zenodo-import', 'mvs'],
      viewportShowAnimation: false,
    };
  }

  async function loadStructure(key) {
    currentKey = key;
    const capEl = document.getElementById(captionId);
    if (capEl) capEl.textContent = 'Loading…';
    const s = structs[key];
    if (!s) { if (capEl) capEl.textContent = 'Structure not available.'; return; }
    viewer.plugin.clear();
    await viewer.loadStructureFromData(b64ToText(s.b64), 'mmcif', { dataLabel: key });
    hideWaterIon(viewer);
    const ids = (opts.hotspotResidueIds && opts.hotspotResidueIds(key)) || [];
    if (ids.length && s.target_chain) {
      viewer.structureInteractivity({
        elements: { prefix: { auth_asym_id: s.target_chain }, items: { auth_seq_id: ids } },
        action: ['select', 'focus'],
      });
    } else {
      viewer.plugin.managers.camera.reset();
    }
    viewer.plugin.managers.camera.reset();
    if (opts.onLoaded) opts.onLoaded(key);
  }

  async function initViewer(startKey) {
    if (!startKey) {
      const capEl = document.getElementById(captionId);
      if (capEl) capEl.textContent = 'No structures available to display.';
      return;
    }
    viewer = await molstar.Viewer.create(stageId, commonViewerOptions());
    viewerReady = true;
    await loadStructure(startKey);
  }

  return {
    loadStructure,
    initViewer,
    get currentKey() { return currentKey; },
    get ready() { return viewerReady; },
  };
}
