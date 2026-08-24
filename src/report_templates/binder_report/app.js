/* Binder-track-specific rendering. Shared dom utils, rail/hero/footer,
 * histogram chart, and the Mol* structure-explorer harness live in
 * src/report_templates/_shared/base.js (injected before this file). */

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

function renderFunnel() {
  const dropMap = {};
  REPORT.funnel.dropped_by.forEach(d => dropMap[d.criterion] = d.n);
  const rows = REPORT.funnel.passing_alone.map(r => ({
    label: r.criterion, pct: r.pct,
    note: dropMap[r.criterion] !== undefined ? `−${dropMap[r.criterion]} first` : undefined,
  }));
  renderBarRows('chart-funnel', rows);
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

function renderDesignCards(explorer) {
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
      explorer.loadStructure(key);
      setActiveStructure(key);
      const target = document.getElementById('explorer');
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    wrap.appendChild(card);
  });
  if (!REPORT.top_designs.length) {
    wrap.innerHTML = '<div class="empty-note">No designs cleared the ranking gate for this campaign yet.</div>';
  }
}

function renderStructurePicker(explorer) {
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
    btn.addEventListener('click', () => { explorer.loadStructure(key); setActiveStructure(key); });
    wrap.appendChild(btn);
  });

  const toggle = document.getElementById('site-view-toggle');
  if (REPORT.hotspots && REPORT.hotspots.length) {
    toggle.innerHTML = `<button class="chip active" data-hs="all">All hotspots</button>`;
    toggle.querySelectorAll('.chip').forEach(btn => {
      btn.addEventListener('click', () => {
        toggle.querySelectorAll('.chip').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        if (explorer.ready) explorer.loadStructure('native');
      });
    });
  }
}

function setActiveStructure(key) {
  document.querySelectorAll('#structure-picker .chip').forEach(c => c.classList.toggle('active', c.dataset.key === key));
  document.querySelectorAll('#design-cards .designcard').forEach(c => c.classList.toggle('active', c.dataset.key === key));
}

// Native-structure numbering is wrong for a refolded design — RFD3
// renumbers the target chain in its own output (see the Python-side
// _design_hotspot_auth_seq_ids docstring). Falls back to the native list
// if no design sidecar could be found (report_data's design list is null).
function hotspotResidueIds(key) {
  if (key && key !== 'native' && REPORT.design_hotspot_auth_seq_ids) {
    return REPORT.design_hotspot_auth_seq_ids;
  }
  return (REPORT.hotspots || []).map(h => h.auth_seq_id);
}

function updateCaption(key) {
  const cap = document.getElementById('explorer-caption');
  // Always the NATIVE count here — hotspot_engagement was computed as a
  // fraction of that same original hotspot list regardless of which
  // numbering the viewer highlights, so the ratio denominator must match.
  const nHotspots = hotspotResidueIds('native').length;
  if (key === 'native') {
    cap.innerHTML = `<b>Native ${esc(REPORT.site_decision.pdb_id)}</b> — ${nHotspots} hotspot residue(s) highlighted on the target chain (teal), with the native partner (copper).`;
  } else {
    const i = parseInt(key.split('_')[1], 10);
    const d = REPORT.top_designs[i];
    const engaged = Math.round((d.hotspot_engagement || 0) * nHotspots);
    cap.innerHTML = `<b>${esc(d.family || d.name)}</b> (${d.binder_len} aa de novo binder, chain A / copper) refolded against the trimmed target (chain B / teal). ` +
      `ipTM <b>${fmt(d.iptm, 3)}</b> · ipSAE<sub>min</sub> <b>${fmt(d.ipsae_min, 3)}</b> · dock RMSD <b>${fmt(d.rmsd_dock, 2)} Å</b> · ` +
      `${engaged}/${nHotspots} hotspots engaged · epitope recall <b>${pct(d.epitope_recall)}</b>.`;
  }
}

/* ---------------------------------------------------------------- boot */
renderRail();
renderHero();
renderSiteSection();
renderHotspotSection();
renderConfidenceSection();

const explorer = createStructureExplorer({
  structs: STRUCTS,
  hotspotResidueIds,
  onLoaded: (key) => { setActiveStructure(key); updateCaption(key); },
});
renderDesignCards(explorer);
renderStructurePicker(explorer);
renderFooter();

const startKey = STRUCTS.native ? 'native' : (designKeys().find(k => STRUCTS[k]) || null);
explorer.initViewer(startKey).catch(e => { console.error('viewer failed', e); document.getElementById('explorer-caption').textContent = 'Viewer failed to load: ' + e; });
