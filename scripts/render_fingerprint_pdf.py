#!/usr/bin/env python3
"""Render curated fingerprints as typeset PDFs, e.g. for a paper appendix.

    python scripts/render_fingerprint_pdf.py 10.7554/eLife.27049 10.1038/ncomms5511
    python scripts/render_fingerprint_pdf.py data/fingerprints/doi_10.7554_eLife.27049.json \
        --out docs/appendix_fingerprints --label "Example B"

Each argument is a DOI or a fingerprint JSON path. Fields are rendered verbatim
from the stored JSON; only layout is added. Journal and licence come from
`data/literature.db`.

**A fingerprint is a derivative of its paper, so the licence gate applies here
too.** A paper whose licence does not permit derivatives (no-derivatives,
unknown, or none recorded) is refused unless `--allow-restricted-licence` is
passed, same posture as `curate_papers.py`. Do not put a restricted fingerprint
in a published appendix.

Needs Google Chrome (or Chromium) on PATH; it does the HTML-to-PDF step. The
per-fingerprint `.html` is kept next to the PDF, plus a `.frag.html` body
fragment for embedding in a page.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.fingerprint_store import load_fingerprint  # noqa: E402
from src.paper_licence import describe, permits_derivatives  # noqa: E402

E = html.escape

CSS = """
@page{size:A4;margin:15mm 14mm}
*{box-sizing:border-box}
body{font-family:"Liberation Sans","DejaVu Sans",Arial,sans-serif;font-size:9.2pt;line-height:1.42;color:#17252b;margin:0}
.fp{max-width:182mm}
.top{border-bottom:2px solid #0b6e75;padding-bottom:6px;margin-bottom:8px}
.eyebrow{font-family:"Liberation Mono","DejaVu Sans Mono",monospace;font-size:7.5pt;color:#5a6b73;letter-spacing:.04em;text-transform:uppercase}
h1{font-family:"Liberation Serif",Georgia,serif;font-size:14.5pt;line-height:1.2;margin:3px 0 4px}
.chips span{display:inline-block;font-family:"Liberation Mono",monospace;font-size:7.3pt;background:#dff0f1;color:#0b5258;border-radius:3px;padding:1px 6px;margin:0 4px 3px 0}
h2{font-size:7.8pt;letter-spacing:.09em;text-transform:uppercase;color:#0b6e75;margin:12px 0 4px;padding-bottom:2px;border-bottom:1px solid #d5dee2;break-after:avoid}
p{margin:0 0 5px}
.hook{font-family:"Liberation Serif",Georgia,serif;font-size:9.6pt}
.kf{border:1px solid #d5dee2;border-radius:4px;padding:6px 8px;margin-bottom:5px;break-inside:avoid}
.kf .claim{font-weight:600;margin-bottom:3px}
.kv{display:grid;grid-template-columns:auto 1fr;column-gap:10px;row-gap:1px;font-size:8.2pt}
.kv dt{color:#5a6b73;font-family:"Liberation Mono",monospace;font-size:7.3pt;padding-top:1px}
.kv dd{margin:0}
.mono{font-family:"Liberation Mono","DejaVu Sans Mono",monospace;font-size:8pt}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:4px 14px}
ul{margin:0;padding-left:15px} li{margin-bottom:1px}
table{border-collapse:collapse;width:100%;font-size:8pt;break-inside:avoid}
th,td{text-align:left;padding:2px 6px;border-bottom:1px solid #d5dee2;vertical-align:top}
th{font-size:7pt;text-transform:uppercase;letter-spacing:.05em;color:#5a6b73}
.foot{margin-top:12px;padding-top:5px;border-top:1px solid #d5dee2;font-size:7.4pt;color:#5a6b73}
.note{background:#f1f5f6;border-left:3px solid #0b6e75;padding:4px 8px;font-size:8pt;margin:6px 0}
"""


def molar(x):
    if x is None:
        return None
    return f"{x:.2e} M"


def li(items):
    return "<ul>" + "".join(f"<li>{E(str(i))}</li>" for i in items) + "</ul>" if items else "<p>none</p>"


def render(d, licence, journal_line, label):
    pm = d["paper_metadata"]; cm = d.get("curation_metadata", {}); meth = d.get("methodology", {})
    o = []
    o.append('<div class="fp"><div class="top">')
    o.append(f'<div class="eyebrow">{E(label)} · LPT fingerprint · schema {E(d.get("schema_version",""))}</div>')
    o.append(f'<h1>{E(pm.get("title",""))}</h1>')
    chips = [f'DOI {pm.get("doi")}', d.get("study_category"), pm.get("study_type")]
    if pm.get("pmcid"): chips.append(pm["pmcid"])
    o.append('<div class="chips">' + "".join(f"<span>{E(str(c))}</span>" for c in chips if c) + "</div></div>")

    o.append("<h2>Situational context hook</h2>")
    o.append(f'<p class="hook">{E(pm.get("situational_context_hook",""))}</p>')

    kfs = d.get("key_findings", [])
    o.append(f"<h2>Key findings ({len(kfs)})</h2>")
    for k in kfs:
        rows = []
        pp = k.get("protein_pair")
        if pp: rows.append(("protein_pair", " ↔ ".join(map(str, pp))))
        rows.append(("evidence", f'{k.get("evidence_value","")} ({k.get("quantitative_or_qualitative","")})'))
        rows.append(("context", k.get("experimental_context", "")))
        if k.get("affinities_kd_Molar") is not None: rows.append(("Kd", molar(k["affinities_kd_Molar"])))
        if k.get("inhibitory_constant_Ki") is not None: rows.append(("Ki", molar(k["inhibitory_constant_Ki"])))
        if k.get("key_amino_acid_residues"): rows.append(("residues", ", ".join(k["key_amino_acid_residues"])))
        rows.append(("confidence", f'{k.get("confidence_score")}  ·  significant: {k.get("is_statistically_significant")}'))
        rows.append(("source_span", k.get("source_span", "")))
        o.append(f'<div class="kf"><div class="claim">{E(k.get("claim",""))}</div><dl class="kv">'
                 + "".join(f"<dt>{E(a)}</dt><dd>{E(str(b))}</dd>" for a, b in rows) + "</dl></div>")

    pc = d.get("pathway_context")
    if pc:
        o.append("<h2>Pathway context</h2>")
        o.append(f'<p><b>Pathways:</b> {E("; ".join(pc.get("pathways") or []))}</p>')
        if pc.get("pathway_logic"): o.append(f'<p><b>Pathway logic:</b> {E(pc["pathway_logic"])}</p>')
        if pc.get("disease_associations"):
            o.append("<table><tr><th>Disease</th><th>Mechanism</th><th>Evidence</th><th>Span</th></tr>" + "".join(
                f'<tr><td>{E(str(x.get("disease","")))}</td><td>{E(str(x.get("mechanism","")))}</td><td>{E(str(x.get("genetic_evidence_type","")))}</td><td>{E(str(x.get("source_span","")))}</td></tr>'
                for x in pc["disease_associations"]) + "</table>")
        tn = pc.get("target_nodes") or []
        if tn:
            o.append("<table><tr><th>Target node</th><th>Position</th><th>Dysregulation</th><th>Dependency evidence</th><th>Span</th></tr>" + "".join(
                f'<tr><td class="mono">{E(str(x.get("protein","")))}</td><td>{E(str(x.get("pathway_position","")))}</td><td>{E(str(x.get("dysregulation") or ""))}</td><td>{E(str(x.get("genetic_dependency_evidence") or ""))}</td><td>{E(str(x.get("source_span","")))}</td></tr>'
                for x in tn) + "</table>")
        o.append('<div class="cols"><div><b>Upstream regulators</b>' + li(pc.get("upstream_regulators"))
                 + '</div><div><b>Downstream effectors</b>' + li(pc.get("downstream_effectors")) + "</div></div>")
        if pc.get("redundancy_risks"): o.append("<p><b>Redundancy risks</b></p>" + li(pc["redundancy_risks"]))

    c = d.get("contradictions_and_negative_results") or []
    if c:
        o.append(f"<h2>Contradictions and negative results ({len(c)})</h2>")
        o.append("<table><tr><th>Finding</th><th>Conflicts with</th><th>Reasoning</th></tr>" + "".join(
            f'<tr><td>{E(str(x.get("finding","")))}</td><td>{E(str(x.get("conflicts_with_prior_work","")))}</td><td>{E(str(x.get("reasoning","")))}</td></tr>' for x in c) + "</table>")

    o.append("<h2>Methodology</h2><div class='cols'>")
    o.append("<div><b>Methods</b>" + li(meth.get("experimental_methods_used")) + "</div>")
    o.append("<div><b>Controls</b>" + li(meth.get("controls")) + "</div>")
    o.append("<div><b>Instruments</b>" + li(meth.get("instruments_used")) + "</div>")
    o.append(f"<div><b>Source organisms (NCBI taxon)</b><p class='mono'>{E(', '.join(map(str, meth.get('protein_origin_organism') or [])) or 'none')}</p>"
             f"<b>PDB accessions ({len(pm.get('pdb_accessions') or [])})</b><p class='mono'>{E(' '.join((pm.get('pdb_accessions') or [])[:24]))}{' …' if len(pm.get('pdb_accessions') or [])>24 else ''}</p></div></div>")

    ent = d.get("entities") or {}
    o.append("<h2>Entities</h2><div class='cols'>")
    o.append(f"<div><b>Proteins</b><p>{E(', '.join(ent.get('proteins') or []) or 'none')}</p></div>")
    o.append(f"<div><b>Chemicals</b><p>{E(', '.join(ent.get('chemicals') or []) or 'none')}</p></div></div>")
    if ent.get("equations"): o.append(f"<p><b>Equations</b> {E('; '.join(ent['equations']))}</p>")

    pi = d.get("protein_identifiers")
    if pi and pi.get("entries"):
        es = pi["entries"][:10]
        o.append(f"<h2>Normalised identifiers (sidecar; {len(pi['entries'])} resolved, {len(pi.get('unresolved') or [])} unresolved, {len(pi.get('filtered_out') or [])} filtered)</h2>")
        o.append("<table><tr><th>Raw name</th><th>Gene</th><th>UniProt</th><th>Tier</th></tr>" + "".join(
            f'<tr><td>{E(str(e.get("raw_name","")))}</td><td class="mono">{E(str(e.get("human_gene_symbol") or e.get("normalized") or ""))}</td><td class="mono">{E(str(e.get("human_uniprot") or ""))}</td><td>{E(str(e.get("match_confidence","")))}</td></tr>' for e in es) + "</table>")
        if len(pi["entries"]) > 10: o.append("<p style='font-size:7.5pt;color:#5a6b73'>First 10 entries shown.</p>")

    o.append(f'<div class="foot">Curated by <span class="mono">{E(str(cm.get("model","")))}</span> on {E(str(cm.get("curated_at",""))[:10])} '
             f'({cm.get("input_tokens")} input / {cm.get("output_tokens")} output tokens). Source: {E(journal_line)}, licence {E(licence)}. '
             f'Fields are rendered verbatim from the stored JSON; layout is added for display. <span class="mono">source_span</span> values are model-reported locators and are not machine-verified.</div></div>')
    return "".join(o)



def _find_chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    sys.exit("Chrome/Chromium not found on PATH; it is needed for the PDF step.")


def _load(arg: str, fp_dir: Path) -> tuple[dict, str | None]:
    path = Path(arg)
    if path.suffix == ".json" and path.exists():
        d = json.loads(path.read_text(encoding="utf-8"))
    else:
        doi = re.sub(r"^(doi:|https?://(dx\.)?doi\.org/)", "", arg)
        d = load_fingerprint(f"doi:{doi}", fp_dir)
        if d is None:
            sys.exit(f"No fingerprint for {arg!r} in {fp_dir}")
    return d, d.get("paper_metadata", {}).get("doi")


def _db_meta(db_path: Path, doi: str | None) -> tuple[str | None, str]:
    """(licence, "Journal Year") from the paper database; (None, '') if absent."""
    if not doi or not db_path.exists():
        return None, ""
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT licence, journal, year FROM papers WHERE doi=?", (doi,)).fetchone()
    finally:
        con.close()
    if not row:
        return None, ""
    return row[0], f"{row[1] or ''} {row[2] or ''}".strip()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("papers", nargs="+", help="DOI or fingerprint JSON path")
    ap.add_argument("--out", default="docs/appendix_fingerprints", help="output directory")
    ap.add_argument("--label", default="", help="eyebrow label; default 'Example <category>'")
    ap.add_argument("--fingerprint-dir", default=str(_ROOT / "data" / "fingerprints"))
    ap.add_argument("--db", default=str(_ROOT / "data" / "literature.db"))
    ap.add_argument("--allow-restricted-licence", action="store_true",
                    help="render papers whose licence does not permit derivatives")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    chrome = _find_chrome()

    for arg in args.papers:
        d, doi = _load(arg, Path(args.fingerprint_dir))
        licence, journal = _db_meta(Path(args.db), doi)
        if not permits_derivatives(licence) and not args.allow_restricted_licence:
            print(f"SKIP {doi}: {describe(licence)} (use --allow-restricted-licence to override)", file=sys.stderr)
            continue
        label = args.label or f"Example · {d.get('study_category') or 'uncategorised'}"
        slug = re.sub(r"[^A-Za-z0-9]+", "_", doi or "fingerprint").strip("_")
        frag = render(d, (licence or "unrecorded").upper(), journal or "source journal not in database", label)
        (out / f"{slug}.frag.html").write_text(frag, encoding="utf-8")
        page = f'<!doctype html><meta charset="utf-8"><title>{E(label)}</title><style>{CSS}</style>{frag}'
        h = out / f"{slug}.html"
        h.write_text(page, encoding="utf-8")
        pdf = out / f"{slug}.pdf"
        # Absolute paths: a relative one makes Chrome exit 0 and write nothing.
        r = subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                            f"--print-to-pdf={pdf}", h.as_uri()], capture_output=True, text=True)
        if not pdf.exists():
            sys.exit(f"Chrome wrote no PDF for {doi}: {r.stderr.strip()[-300:]}")
        print(f"wrote {pdf}")


if __name__ == "__main__":
    main()
