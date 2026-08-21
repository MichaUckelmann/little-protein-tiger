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

/* ---------------------------------------------------------------- site narrative + candidates */
function renderSiteSection() {
  const narrEl = document.getElementById('site-narrative');
  narrEl.innerHTML = REPORT.site_narrative_html || '<p class="prose-empty">No narrative captured for this stage.</p>';

  const table = document.getElementById('candidates-table');
  const cands = REPORT.candidates;
  if (!cands || !cands.length) {
    table.parentElement.innerHTML = '<div class="empty-note">No candidate-structure table available for this run.</div>';
  } else {
    const maxBsa = Math.max(...cands.map(c => c.bsa_A2 || 0), 1);
    const chosenPdb = REPORT.site_decision.pdb_id;
    let html = '<thead><tr><th>PDB</th><th>Partner</th><th>Res. (Å)</th><th>BSA (Å²)</th><th>H-bonds</th><th>φ frac</th></tr></thead><tbody>';
    cands.forEach(c => {
      const chosen = c.pdb_id === chosenPdb;
      html += `<tr class="${chosen ? 'chosen' : ''}">
        <td class="mono">${esc(c.pdb_id)}${chosen ? ' ★' : ''}</td>
        <td>${esc(c.partner_entity || '')}</td>
        <td class="mono">${fmt(c.resolution_A, 2)}</td>
        <td><div class="barcell"><div class="track"><div class="fill" style="width:${((c.bsa_A2 || 0) / maxBsa * 100).toFixed(0)}%"></div></div><div class="num mono">${(c.bsa_A2 || 0).toFixed(0)}</div></div></td>
        <td class="mono">${c.n_hbonds != null ? c.n_hbonds : '—'}</td>
        <td class="mono">${fmt(c.hydrophobic_fraction, 2)}</td>
      </tr>`;
    });
    html += '</tbody>';
    table.innerHTML = html;
  }

  const d = REPORT.site_decision;
  const goKind = d.go_recommendation === 'GO' ? 'good' : d.go_recommendation === 'NO_GO' ? 'bad' : 'warn';
  let alt = '';
  if (d.alternatives && d.alternatives.length) {
    alt = '<p><b>Alternatives considered:</b></p><ul>' +
      d.alternatives.map(a => `<li><span class="mono">${esc(a.pdb_id)}</span> — ${esc(a.why_not)}</li>`).join('') +
      '</ul>';
  }
  document.getElementById('site-decision-callout').innerHTML = `
    <div class="tag">Decision: ${esc(d.pdb_id)}, ${esc(d.target_chain)}–${esc(d.partner_chain)} (${esc(d.partner_name)})</div>
    <p><strong>${esc(d.interface_rationale || '')}</strong></p>
    <p>Design intent: <b>${esc(d.design_intent || '—')}</b> · Modality: <b>${esc(d.modality || '—')}</b> ·
       <span class="tag-chip ${goKind === 'good' ? 'phys' : ''}">${esc(d.go_recommendation || '')}</span> — ${esc(d.go_rationale || '')}</p>
    ${alt}
  `;
}

/* ---------------------------------------------------------------- hotspots */
function renderHotspotSection() {
  document.getElementById('hotspot-narrative').innerHTML =
    REPORT.hotspot_narrative_html || '<p class="prose-empty">No hotspot rationale captured for this stage.</p>';

  const table = document.getElementById('hotspot-table');
  const hs = REPORT.hotspots || [];
  if (!hs.length) {
    table.parentElement.innerHTML = '<div class="empty-note">No structured hotspot table available for this run.</div>';
  } else {
    let html = '<thead><tr><th>Residue</th><th>auth_seq_id</th><th>RFD3 atoms</th><th>Kept after trim</th></tr></thead><tbody>';
    hs.forEach(h => {
      html += `<tr>
        <td class="mono">${esc(h.residue)}</td>
        <td class="mono">${h.auth_seq_id}</td>
        <td class="mono">${esc(h.rfd3_atoms || '')}</td>
        <td>${h.retained === false ? '<span class="tag-chip" style="background:var(--bad-soft);color:var(--bad)">dropped</span>' : '<span class="tag-chip phys">yes</span>'}</td>
      </tr>`;
    });
    html += '</tbody>';
    table.innerHTML = html;
  }

  const cit = document.getElementById('citation-callout');
  if (REPORT.citation_html) {
    cit.innerHTML = `<div class="callout warn"><div class="tag">Citation verification</div>${REPORT.citation_html}</div>`;
  }
}

/* ---------------------------------------------------------------- confidence section */
function renderConfidenceSection() {
  const c = REPORT.calibration;
  document.getElementById('confidence-dek').textContent =
    `${REPORT.metrics.n_total.toLocaleString()} refolds scored from the ${REPORT.source_label}.`;

  const banner = document.getElementById('verdict-banner');
  if (c && c.verdict) {
    const kind = (c.verdict === 'SCALE_UP') ? 'v-good' : (c.verdict === 'ITERATE' || c.verdict === 'SCALE_UP_PARTIAL') ? 'v-warn' : 'v-bad';
    banner.className = 'verdictbanner ' + kind;
    banner.innerHTML = `<span class="badge">${esc(c.verdict)}</span><p>${esc(c.verdict_reason || '')}</p>`;
  } else {
    banner.className = 'verdictbanner';
    banner.innerHTML = '<p>No calibration verdict available — this campaign has not run a sized calibration trial.</p>';
  }

  const ciWrap = document.getElementById('ci-wrap');
  if (c && c.backbone_rate) {
    const br = c.backbone_rate;
    const SCALE = Math.max(0.6, Math.ceil(br.p_high * 10) / 10 + 0.1);
    const lowPct = Math.min(100, br.p_low / SCALE * 100);
    const highPct = Math.min(100, br.p_high / SCALE * 100);
    const hatPct = Math.min(100, br.p_hat / SCALE * 100);
    ciWrap.innerHTML = `
      <div style="display:flex; justify-content:space-between; font-size:12px; color:var(--ink-muted); font-family:var(--font-mono);">
        <span>backbone hit-rate, 95% Wilson CI (${br.k}/${br.n})</span><span>0% · ${(SCALE * 100 / 3).toFixed(0)}% · ${(SCALE * 100 * 2 / 3).toFixed(0)}%</span>
      </div>
      <div class="cibar">
        <div class="ci-tick" style="left:${100 / 3}%"></div>
        <div class="ci-tick" style="left:${200 / 3}%"></div>
        <div class="ci-range" style="left:${lowPct}%; width:${Math.max(highPct - lowPct, 0.5)}%"></div>
        <div class="ci-point" style="left:${hatPct}%"></div>
      </div>`;
  }

  document.getElementById('chart-iptm-cap').textContent =
    `all ${REPORT.metrics.n_total.toLocaleString()} refolds · excellence bar ${c ? c.success_metric + ' ≥ ' + c.excellence_bar : ''}`;
  document.getElementById('chart-scatter-cap').textContent =
    `${REPORT.metrics.scatter.length}-point sample · green = clears every hard gate and the excellence bar`;

  renderHistogram('chart-iptm', REPORT.metrics.iptm_hist, { threshold: c ? c.excellence_bar : undefined });
  renderHistogram('chart-ipsae', REPORT.metrics.ipsae_hist, { threshold: 0.5 });
  renderFunnel();
  renderScatter();
}

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

function renderFunnel() {
  const svg = document.getElementById('chart-funnel');
  const rows = REPORT.funnel.passing_alone;
  if (!rows.length) { svg.innerHTML = ''; return; }
  const W = 640, padL = 160, padR = 60, padT = 6, rowH = 24, gap = 3;
  const dropMap = {};
  REPORT.funnel.dropped_by.forEach(d => dropMap[d.criterion] = d.n);
  const plotW = W - padL - padR;
  let s = '';
  rows.forEach((r, i) => {
    const y = padT + i * (rowH + gap);
    const w = (r.pct / 100) * plotW;
    const dropped = dropMap[r.criterion];
    s += `<text class="axislabel" x="${padL - 8}" y="${y + rowH / 2 + 3}" text-anchor="end" style="font-size:10.5px">${esc(r.criterion)}</text>`;
    s += `<rect class="bar-track" x="${padL}" y="${y}" width="${plotW}" height="${rowH}"/>`;
    s += `<rect class="bar" x="${padL}" y="${y}" width="${w.toFixed(1)}" height="${rowH}"/>`;
    s += `<text class="axislabel" x="${padL + plotW + 6}" y="${y + rowH / 2 + 3}" text-anchor="start" style="font-size:10.5px; font-weight:600;">${r.pct.toFixed(1)}%</text>`;
    if (dropped !== undefined) {
      s += `<text class="axislabel on-bar" x="${padL + Math.min(w, plotW) - 6}" y="${y + rowH / 2 + 3}" text-anchor="end" style="font-size:9.5px">−${dropped} first</text>`;
    }
  });
  svg.setAttribute('viewBox', `0 0 ${W} ${padT + rows.length * (rowH + gap) + 4}`);
  svg.innerHTML = s;
}

function renderScatter() {
  const svg = document.getElementById('chart-scatter');
  const W = 640, H = 320, padL = 44, padR = 14, padT = 14, padB = 40;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  let s = '';
  for (let g = 0; g <= 4; g++) {
    const gx = padL + (g / 4) * plotW;
    const gy = padT + plotH - (g / 4) * plotH;
    s += `<line class="gridline" x1="${gx}" x2="${gx}" y1="${padT}" y2="${padT + plotH}"/>`;
    s += `<line class="gridline" x1="${padL}" x2="${padL + plotW}" y1="${gy}" y2="${gy}"/>`;
    s += `<text class="axislabel" x="${gx}" y="${padT + plotH + 16}" text-anchor="middle">${g / 4}</text>`;
    s += `<text class="axislabel" x="${padL - 6}" y="${gy + 3}" text-anchor="end">${g / 4}</text>`;
  }
  const c = REPORT.calibration;
  if (c && typeof c.excellence_bar === 'number' && c.success_metric === 'iptm') {
    const ex = padL + c.excellence_bar * plotW;
    s += `<line class="thresholdline" x1="${ex}" x2="${ex}" y1="${padT}" y2="${padT + plotH}"/>`;
  }
  const ey = padT + plotH - 0.5 * plotH;
  s += `<line class="thresholdline" x1="${padL}" x2="${padL + plotW}" y1="${ey}" y2="${ey}"/>`;
  REPORT.metrics.scatter.forEach(p => {
    const x = padL + p[0] * plotW;
    const y = padT + plotH - p[1] * plotH;
    s += `<circle class="pt ${p[2] ? 'pt-hit' : ''}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.4"/>`;
  });
  s += `<text class="axislabel" x="${padL + plotW / 2}" y="${H - 6}" text-anchor="middle" style="font-size:11px">ipTM</text>`;
  s += `<text class="axislabel" x="12" y="${padT + plotH / 2}" text-anchor="middle" transform="rotate(-90 12 ${padT + plotH / 2})" style="font-size:11px">ipSAE min</text>`;
  svg.innerHTML = s;
}

/* ---------------------------------------------------------------- design cards + explorer */
function designKeys() { return REPORT.top_designs.map((_, i) => 'design_' + i); }

function renderDesignCards() {
  const wrap = document.getElementById('design-cards');
  wrap.innerHTML = '';
  const nHotspots = (REPORT.hotspots || []).length || 1;
  designKeys().forEach(key => {
    const i = parseInt(key.split('_')[1], 10);
    const d = REPORT.top_designs[i];
    const engaged = Math.round((d.hotspot_engagement || 0) * nHotspots);
    const ok = (d.hotspot_engagement || 0) >= 1;
    const card = el('div', 'designcard', `
      <div class="id">${esc(d.family || d.name)} · ${d.binder_len} aa</div>
      <div class="metrics">
        <div class="metric"><span>ipTM</span><b>${fmt(d.iptm, 3)}</b></div>
        <div class="metric"><span>ipSAE min</span><b>${fmt(d.ipsae_min, 3)}</b></div>
        <div class="metric"><span>RMSD dock</span><b>${fmt(d.rmsd_dock, 2)} Å</b></div>
        <div class="metric"><span>Epitope recall</span><b>${pct(d.epitope_recall)}</b></div>
      </div>
      <div class="check ${ok ? 'ok' : 'partial'}">${engaged}/${nHotspots} hotspots engaged</div>
      <div class="seqline">${esc(d.seq || '')}</div>
    `);
    card.dataset.key = key;
    card.addEventListener('click', () => {
      loadStructure(key);
      const target = document.getElementById('explorer');
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    wrap.appendChild(card);
  });
  if (!REPORT.top_designs.length) {
    wrap.innerHTML = '<div class="empty-note">No designs cleared the ranking gate for this campaign yet.</div>';
  }
}

function renderStructurePicker() {
  const wrap = document.getElementById('structure-picker');
  wrap.innerHTML = '';
  const keys = [];
  if (STRUCTS.native) keys.push('native');
  keys.push(...designKeys().filter(k => STRUCTS[k]));
  keys.forEach(key => {
    const label = key === 'native' ? 'Native · ' + REPORT.site_decision.pdb_id
      : 'Design · ' + (REPORT.top_designs[parseInt(key.split('_')[1], 10)].family || key);
    const btn = el('button', 'chip', label);
    btn.dataset.key = key;
    btn.addEventListener('click', () => loadStructure(key));
    wrap.appendChild(btn);
  });

  const toggle = document.getElementById('site-view-toggle');
  if (REPORT.hotspots && REPORT.hotspots.length) {
    toggle.innerHTML = `<button class="chip active" data-hs="all">All hotspots</button>`;
    toggle.querySelectorAll('.chip').forEach(btn => {
      btn.addEventListener('click', () => {
        toggle.querySelectorAll('.chip').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        if (viewerReady) loadStructure('native');
      });
    });
  }
}

function setActiveStructure(key) {
  document.querySelectorAll('#structure-picker .chip').forEach(c => c.classList.toggle('active', c.dataset.key === key));
  document.querySelectorAll('#design-cards .designcard').forEach(c => c.classList.toggle('active', c.dataset.key === key));
}

/* ---------------------------------------------------------------- mol* (single shared viewer) */
let viewer, viewerReady = false, currentKey = 'native';

function b64ToText(b64) { return atob(b64); }

function hideWaterIon(v) {
  try {
    const structs = v.plugin.managers.structure.hierarchy.current.structures;
    if (!structs || !structs.length) return;
    const struct = structs[0];
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

function hotspotResidueIds() { return (REPORT.hotspots || []).map(h => h.auth_seq_id); }

function updateCaption() {
  const cap = document.getElementById('explorer-caption');
  const nHotspots = hotspotResidueIds().length;
  if (currentKey === 'native') {
    cap.innerHTML = `<b>Native ${esc(REPORT.site_decision.pdb_id)}</b> — ${nHotspots} hotspot residue(s) highlighted on the target chain (teal), with the native partner (copper).`;
  } else {
    const i = parseInt(currentKey.split('_')[1], 10);
    const d = REPORT.top_designs[i];
    const engaged = Math.round((d.hotspot_engagement || 0) * nHotspots);
    cap.innerHTML = `<b>${esc(d.family || d.name)}</b> (${d.binder_len} aa de novo binder, chain A / copper) refolded against the trimmed target (chain B / teal). ` +
      `ipTM <b>${fmt(d.iptm, 3)}</b> · ipSAE<sub>min</sub> <b>${fmt(d.ipsae_min, 3)}</b> · dock RMSD <b>${fmt(d.rmsd_dock, 2)} Å</b> · ` +
      `${engaged}/${nHotspots} hotspots engaged · epitope recall <b>${pct(d.epitope_recall)}</b>.`;
  }
}

async function loadStructure(key) {
  currentKey = key;
  setActiveStructure(key);
  document.getElementById('explorer-caption').textContent = 'Loading…';
  const s = STRUCTS[key];
  if (!s) { document.getElementById('explorer-caption').textContent = 'Structure not available.'; return; }
  viewer.plugin.clear();
  await viewer.loadStructureFromData(b64ToText(s.b64), 'mmcif', { dataLabel: key });
  hideWaterIon(viewer);
  const ids = hotspotResidueIds();
  if (ids.length) {
    viewer.structureInteractivity({
      elements: { prefix: { auth_asym_id: s.target_chain }, items: { auth_seq_id: ids } },
      action: ['select', 'focus'],
    });
  } else {
    viewer.plugin.managers.camera.reset();
  }
  viewer.plugin.managers.camera.reset();
  updateCaption();
}

async function initViewer() {
  const startKey = STRUCTS.native ? 'native' : (designKeys().find(k => STRUCTS[k]) || null);
  if (!startKey) {
    document.getElementById('explorer-caption').textContent = 'No structures available to display.';
    return;
  }
  viewer = await molstar.Viewer.create('molstar-stage', commonViewerOptions());
  viewerReady = true;
  await loadStructure(startKey);
}

/* ---------------------------------------------------------------- footer */
function renderFooter() {
  document.getElementById('methods').innerHTML = REPORT.footer_html;
}

/* ---------------------------------------------------------------- boot */
renderRail();
renderHero();
renderSiteSection();
renderHotspotSection();
renderConfidenceSection();
renderDesignCards();
renderStructurePicker();
renderFooter();

initViewer().catch(e => { console.error('viewer failed', e); document.getElementById('explorer-caption').textContent = 'Viewer failed to load: ' + e; });
