/* PPI-track-specific rendering. Shared dom utils, rail/hero/footer,
 * histogram chart, and the Mol* structure-explorer harness live in
 * src/report_templates/_shared/base.js (injected before this file). */

/* ---------------------------------------------------------------- pathway */
function renderPathwaySection() {
  document.getElementById('pathway-narrative').innerHTML =
    REPORT.pathway_narrative_html || '<p class="prose-empty">No pathway narrative captured for this run.</p>';

  const table = document.getElementById('choices-table');
  const choices = REPORT.choices || [];
  if (!choices.length) {
    table.parentElement.innerHTML = '<div class="empty-note">No candidate-target table available for this run.</div>';
    return;
  }
  const chosenComplex = REPORT.site_decision.target_complex;
  const tierKind = t => /^VALIDATED$/i.test(t) ? 'phys' : /^BIOLOGICALLY_JUSTIFIED$/i.test(t) ? 'reagent' : 'region2';
  let html = '<thead><tr><th>Tier</th><th>Complex</th><th>PDB candidates</th><th>Evidence</th></tr></thead><tbody>';
  choices.forEach(c => {
    const chosen = c.complex === chosenComplex;
    html += `<tr class="${chosen ? 'chosen' : ''}">
      <td><span class="tag-chip ${tierKind(c.tier)}">${esc(c.tier || '')}</span></td>
      <td>${esc(c.complex || '')}${chosen ? ' ★' : ''}</td>
      <td class="mono">${esc((c.pdb_ids || []).join(', '))}</td>
      <td style="max-width:42ch;">${esc(c.evidence_basis || '')}</td>
    </tr>`;
  });
  html += '</tbody>';
  table.innerHTML = html;
}

/* ---------------------------------------------------------------- literature */
function renderLiteratureSection() {
  document.getElementById('literature-narrative').innerHTML =
    REPORT.literature_narrative_html || '<p class="prose-empty">No literature narrative captured for this run.</p>';

  const d = REPORT.site_decision;
  const goKind = d.go_recommendation === 'GO' ? 'good' : d.go_recommendation === 'NO_GO' ? 'bad' : 'warn';
  document.getElementById('literature-decision-callout').innerHTML = `
    <div class="tag">Tractability: ${esc(d.tractability || '—')}</div>
    <p><span class="tag-chip ${goKind === 'good' ? 'phys' : ''}">${esc(d.go_recommendation || '')}</span> — ${esc(d.go_rationale || '')}</p>
  `;

  if (REPORT.literature_citation_html) {
    document.getElementById('literature-citation-callout').innerHTML =
      `<div class="callout warn"><div class="tag">Citation verification</div>${REPORT.literature_citation_html}</div>`;
  }
}

/* ---------------------------------------------------------------- structure + hotspots */
function renderStructureSection() {
  document.getElementById('structure-narrative').innerHTML =
    REPORT.structure_narrative_html || '<p class="prose-empty">No structure narrative captured for this run.</p>';

  const table = document.getElementById('hotspot-table');
  const hs = REPORT.hotspots || [];
  if (!hs.length) {
    table.parentElement.innerHTML = '<div class="empty-note">No structured hotspot table available for this run.</div>';
  } else {
    let html = '<thead><tr><th>Residue</th><th>auth_seq_id</th><th>RFD3 atoms</th></tr></thead><tbody>';
    hs.forEach(h => {
      html += `<tr>
        <td class="mono">${esc(h.residue)}</td>
        <td class="mono">${h.auth_seq_id}</td>
        <td class="mono">${esc(h.rfd3_atoms || '')}</td>
      </tr>`;
    });
    html += '</tbody>';
    table.innerHTML = html;
  }

  if (REPORT.structure_citation_html) {
    document.getElementById('structure-citation-callout').innerHTML =
      `<div class="callout warn"><div class="tag">Citation verification</div>${REPORT.structure_citation_html}</div>`;
  }
}

/* ---------------------------------------------------------------- design generation */
function renderGenerationSection() {
  document.getElementById('design-narrative').innerHTML =
    REPORT.design_narrative_html || '<p class="prose-empty">No design-spec narrative captured for this run.</p>';
  document.getElementById('execution-narrative').innerHTML =
    REPORT.execution_html || '<p class="prose-empty">No execution report captured for this run.</p>';
}

/* ---------------------------------------------------------------- confidence / ranking */
function renderConfidenceSection() {
  const m = REPORT.metrics;
  document.getElementById('confidence-dek').textContent =
    `${m.n_total.toLocaleString()} designs scored; ${m.n_survivors.toLocaleString()} (${pct(m.n_survivors / Math.max(m.n_total, 1))}) survive every hard gate.`;
  document.getElementById('chart-iptm-cap').textContent = `all ${m.n_total.toLocaleString()} designs · gate at ${m.iptm_min}`;
  document.getElementById('chart-sasa-cap').textContent = `all ${m.n_total.toLocaleString()} designs · gate at ${m.hotspot_sasa_delta_min} Å²`;

  renderHistogram('chart-iptm-hist', m.iptm_hist, { threshold: m.iptm_min });
  renderHistogram('chart-sasa-hist', m.sasa_hist, { threshold: m.hotspot_sasa_delta_min });

  const rows = REPORT.funnel.map(r => ({ label: r.criterion, pct: r.pct, note: `${r.n} dropped` }));
  renderBarRows('chart-funnel', rows);
}

/* ---------------------------------------------------------------- design cards + explorer */
function designKeys() { return REPORT.top_designs.map((_, i) => 'design_' + i); }

function renderDesignCards(explorer) {
  const wrap = document.getElementById('design-cards');
  wrap.innerHTML = '';
  designKeys().forEach(key => {
    const i = parseInt(key.split('_')[1], 10);
    const d = REPORT.top_designs[i];
    const card = el('div', 'designcard', `
      <div class="id">${esc(d.name)} · rank ${esc(d.composite_rank)}</div>
      <div class="metrics">
        <div class="metric"><span>Composite</span><b>${fmt(d.composite_score, 2)}</b></div>
        <div class="metric"><span>ipTM</span><b>${fmt(d.iptm, 3)}</b></div>
        <div class="metric"><span>Interface PAE</span><b>${fmt(d.ipae, 2)} Å</b></div>
        <div class="metric"><span>Hotspot SASA Δ</span><b>${fmt(d.hotspot_sasa_delta, 1)} Å²</b></div>
      </div>
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
    wrap.innerHTML = '<div class="empty-note">No designs cleared the ranking gate for this run.</div>';
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
      : 'Design · ' + (REPORT.top_designs[parseInt(key.split('_')[1], 10)].name || key);
    const btn = el('button', 'chip', label);
    btn.dataset.key = key;
    btn.addEventListener('click', () => { explorer.loadStructure(key); setActiveStructure(key); });
    wrap.appendChild(btn);
  });
}

function setActiveStructure(key) {
  document.querySelectorAll('#structure-picker .chip').forEach(c => c.classList.toggle('active', c.dataset.key === key));
  document.querySelectorAll('#design-cards .designcard').forEach(c => c.classList.toggle('active', c.dataset.key === key));
}

// Native-structure numbering is wrong for a BoltzGen design refold —
// BoltzGen renumbers the target chain in its own output (the original
// mmCIF label_seq becomes the new auth_seq_id; see the Python-side
// report_data comment). Falls back to native numbering if the design list
// wasn't computed (no hotspots at all).
function hotspotResidueIds(key) {
  if (key && key !== 'native' && REPORT.design_hotspot_auth_seq_ids) {
    return REPORT.design_hotspot_auth_seq_ids;
  }
  return (REPORT.hotspots || []).map(h => h.auth_seq_id);
}

function updateCaption(key) {
  const cap = document.getElementById('explorer-caption');
  // Always the NATIVE count — same reasoning as binder_report's app.js.
  const nHotspots = hotspotResidueIds('native').length;
  if (key === 'native') {
    cap.innerHTML = `<b>Native ${esc(REPORT.site_decision.pdb_id)}</b> — ${nHotspots} hotspot residue(s) highlighted on the target chain (teal), with the native partner (copper).`;
  } else {
    const i = parseInt(key.split('_')[1], 10);
    const d = REPORT.top_designs[i];
    cap.innerHTML = `<b>${esc(d.name)}</b> BoltzGen design refolded against the target. ` +
      `Composite <b>${fmt(d.composite_score, 2)}</b> · ipTM <b>${fmt(d.iptm, 3)}</b> · interface PAE <b>${fmt(d.ipae, 2)} Å</b> · ` +
      `hotspot SASA Δ <b>${fmt(d.hotspot_sasa_delta, 1)} Å²</b>.`;
  }
}

/* ---------------------------------------------------------------- verdict */
function renderVerdictSection() {
  const v = REPORT.verdict;
  const banner = document.getElementById('verdict-banner');
  if (v) {
    const kind = v === 'GO' ? 'v-good' : v === 'NO_GO' ? 'v-bad' : 'v-warn';
    banner.className = 'verdictbanner ' + kind;
    banner.innerHTML = `<span class="badge">${esc(v)}</span><p>${esc(REPORT.verdict_reason || '')}</p>`;
  } else {
    banner.className = 'verdictbanner';
    banner.innerHTML = '<p>No design-analyst verdict available yet for this run.</p>';
  }
  document.getElementById('verdict-body').innerHTML =
    REPORT.summary_html || '<p class="prose-empty">No summary stage output captured for this run.</p>';
}

/* ---------------------------------------------------------------- boot */
renderRail();
renderHero();
renderPathwaySection();
renderLiteratureSection();
renderStructureSection();
renderGenerationSection();
renderConfidenceSection();

const explorer = createStructureExplorer({
  structs: STRUCTS,
  hotspotResidueIds,
  onLoaded: (key) => { setActiveStructure(key); updateCaption(key); },
});
renderDesignCards(explorer);
renderStructurePicker(explorer);
renderVerdictSection();
renderFooter();

const startKey = STRUCTS.native ? 'native' : (designKeys().find(k => STRUCTS[k]) || null);
explorer.initViewer(startKey).catch(e => { console.error('viewer failed', e); document.getElementById('explorer-caption').textContent = 'Viewer failed to load: ' + e; });
