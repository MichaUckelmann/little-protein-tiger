#!/usr/bin/env python3
"""Phase 2, tier A: deterministic scoring of the generated reports. No LLM, $0.

    .venv/bin/python scripts/phase2_score.py            # score every finished cell
    .venv/bin/python scripts/phase2_score.py --offline  # extraction only, no Crossref/RCSB

Per report: how many cited DOIs exist (Crossref, with the `j.` repair found in
the earlier audit), how many are in the corpus, whether each cited PDB entry
exists and names the protein it is cited for (RCSB), whether the gene symbols
in the candidate table resolve, the chosen primary target (alias-normalised),
and whether it is the YAP1/TEAD family (a density-pull check, see the plan).
Writes outputs/phase2/scores.json (and a DOI cache next to it).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

import phase2_common as C
from audit_citations_external import resolve as crossref_resolve  # noqa: E402

DOI_CACHE = C.OUT / "doi_cache.json"
PDB_CACHE = C.OUT / "pdb_cache.json"
HIPPO = {"YAP1", "TEAD1", "TEAD2", "TEAD3", "TEAD4", "WWTR1", "TAZ"}
_PDB_ID = re.compile(r"\b([1-9][A-Za-z0-9]{3})\b")
_GQL = ("query($ids:[String!]!){entries(entry_ids:$ids){rcsb_id polymer_entities{"
        "rcsb_polymer_entity{pdbx_description} rcsb_entity_source_organism{"
        "rcsb_gene_name{value} ncbi_scientific_name}}}}")


def _load(p):
    return json.loads(p.read_text()) if p.exists() else {}


def check_dois(dois, cache, offline):
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = "lpt-phase2-audit/1.0"
    for d in dois:
        if d in cache and not cache[d]["status"].startswith("error"):
            continue
        if offline:
            cache[d] = {"status": "unchecked"}
            continue
        e = crossref_resolve(d, s)
        if e["status"] == "not_found" and d.startswith("10.1016/") and not d.startswith("10.1016/j."):
            fixed = crossref_resolve(d.replace("10.1016/", "10.1016/j.", 1), s)
            if fixed["status"] == "resolves":
                e = {**fixed, "status": "resolves_after_repair"}
        cache[d] = e
        time.sleep(0.12)


def node_pdb_pairs(text: str) -> list[tuple[str, str]]:
    """(PDB id, header text it sits under) from 'Suggested PDB' lines."""
    out, header = [], ""
    for line in text.splitlines():
        if line.startswith("#"):
            header = re.sub(r"[#*\[\]]", " ", line)
        if re.search(r"suggested pdb", line, re.I):
            tail = line.split(":", 1)[-1]
            for pid in _PDB_ID.findall(re.sub(r"\([^)]*\)", "", tail)):
                if re.search(r"\d", pid):
                    out.append((pid.upper(), header))
    return out


def check_pdb(ids, cache, offline):
    import requests
    todo = [i for i in ids if i not in cache]
    if offline or not todo:
        return
    for i in range(0, len(todo), 40):
        chunk = todo[i:i + 40]
        r = requests.post("https://data.rcsb.org/graphql", json={"query": _GQL, "variables": {"ids": chunk}}, timeout=40)
        found = {}
        for e in (r.json().get("data") or {}).get("entries") or []:
            names, genes = [], set()
            for pe in e.get("polymer_entities") or []:
                names.append(((pe.get("rcsb_polymer_entity") or {}).get("pdbx_description")) or "")
                for src in pe.get("rcsb_entity_source_organism") or []:
                    for g in src.get("rcsb_gene_name") or []:
                        genes.add(g.get("value", ""))
            found[e["rcsb_id"].upper()] = {"exists": True, "descriptions": names, "genes": sorted(genes)}
        for pid in chunk:
            cache[pid] = found.get(pid, {"exists": False})


def symbols_in(header: str, N) -> set[str]:
    out = set()
    for tok in re.split(r"[/(),;]|\s+and\s+|\s{2,}", header):
        tok = tok.strip()
        if 2 <= len(tok) <= 40:
            out.add(sym(tok, N))
    return {x for x in out if x}


def sym(name: str, N) -> str:
    r = N.resolve(name)
    s = getattr(r, "human_gene_symbol", None) or (r.get("human_gene_symbol") if isinstance(r, dict) else None)
    return s or re.sub(r"[^A-Z0-9]", "", name.upper())


def pdb_matches(header: str, entry: dict, N) -> bool:
    want = symbols_in(header, N)
    have = {sym(g, N) for g in entry.get("genes", [])}
    if want & have:
        return True
    desc = " ".join(entry.get("descriptions", [])).lower()
    return any(len(t) >= 4 and t.lower() in desc for t in re.split(r"[/(),;\s]+", header))


def primary_pair(text: str, N) -> list[str]:
    m = re.search(r"- target_complex:\s*(.+)", text)
    if not m:
        return []
    return sorted({sym(x.strip(), N) for x in re.split(r"\s*/\s*", m.group(1)) if x.strip()})


def landscape_symbols(text: str, N) -> list[str]:
    body = text.split("TARGET OPPORTUNITY LANDSCAPE")[-1].split("### PRIMARY")[0] if "TARGET OPPORTUNITY LANDSCAPE" in text else ""
    raw = []
    for block in re.split(r"\n#### ", body)[1:]:
        head = re.sub(r"^\[[A-Z_ ]+\]\s*", "", block.splitlines()[0].strip())
        raw += [g.strip() for g in re.split(r"\s*/\s*", head) if g.strip()]
    return raw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    from src.env_config import load_env
    load_env()
    from src.identifier_normalizer import get_normalizer
    N = get_normalizer()
    excluded = C.prompt_dois()
    idx = C._fp_index()
    doi_cache, pdb_cache = _load(DOI_CACHE), _load(PDB_CACHE)

    reports = []
    for prompt, rep, arm, meta in C.cells():
        text = C.report_text(prompt, rep, arm)
        dois = sorted({C.clean_doi(d) for d in C.DOI_RE.findall(text)} - excluded)
        reports.append((prompt, rep, arm, meta, text, dois, node_pdb_pairs(text)))

    check_dois(sorted({d for r in reports for d in r[5]}), doi_cache, args.offline)
    check_pdb(sorted({p for r in reports for p, _ in r[6]}), pdb_cache, args.offline)
    DOI_CACHE.write_text(json.dumps(doi_cache, indent=1))
    PDB_CACHE.write_text(json.dumps(pdb_cache, indent=1))

    rows = []
    for prompt, rep, arm, meta, text, dois, pdbs in reports:
        st = [doi_cache.get(d, {}).get("status", "unchecked") for d in dois]
        n = len(dois)
        pdb_exist = [p for p, _ in pdbs if pdb_cache.get(p, {}).get("exists")]
        pdb_ok = [p for p, h in pdbs if pdb_cache.get(p, {}).get("exists") and pdb_matches(h, pdb_cache[p], N)]
        raw_genes = landscape_symbols(text, N)
        resolved = [g for g in raw_genes if (getattr(N.resolve(g), "human_gene_symbol", None) or "")]
        pair = primary_pair(text, N)
        rows.append({
            "prompt": prompt, "rep": rep, "arm": arm,
            "n_dois": n,
            "doi_resolves": sum(s.startswith("resolves") for s in st),
            "doi_repaired": sum(s == "resolves_after_repair" for s in st),
            "doi_not_found": sum(s == "not_found" for s in st),
            "doi_unchecked": sum(s.startswith("error") or s == "unchecked" for s in st),
            "doi_in_corpus": sum(d in idx for d in dois),
            "n_pdb": len(pdbs), "pdb_exist": len(pdb_exist), "pdb_names_protein": len(pdb_ok),
            "n_landscape_genes": len(raw_genes), "landscape_genes_resolved": len(resolved),
            "primary_pair": pair, "primary_is_hippo": bool(set(pair) & HIPPO),
            "genes": sorted({sym(g, N) for g in raw_genes}),
            **C.stated_coverage(text), **C.trace_stats(prompt, rep, arm),
            "usd": meta.get("usd"), "n_tool_calls": meta.get("n_tool_calls"),
            "n_corpus_calls": meta.get("n_corpus_calls"), "report_chars": len(text),
        })
    (C.OUT / "scores.json").write_text(json.dumps(rows, indent=1))

    print(f"\n  {'prompt':6}{'rep':4}{'arm':6}{'DOIs':>5}{'exist':>6}{'repair':>7}{'nofnd':>6}{'corpus':>7}{'PDB':>5}{'ok':>4}  primary")
    for r in sorted(rows, key=lambda r: (r["prompt"], r["rep"], r["arm"])):
        print(f"  {r['prompt']:6}{r['rep']:4}{r['arm']:6}{r['n_dois']:5}{r['doi_resolves']:6}{r['doi_repaired']:7}"
              f"{r['doi_not_found']:6}{r['doi_in_corpus']:7}{r['n_pdb']:5}{r['pdb_names_protein']:4}  {'/'.join(r['primary_pair'])}")
    for arm in C.ARMS:
        a = [r for r in rows if r["arm"] == arm]
        if a:
            fab = [r for r in a if (r["stated_fingerprints"] or 0) > 0 and r["fingerprints_returned"] == 0]
            print(f"\n  {arm}: reports stating papers analysed that the trace shows were never returned: {len(fab)} of {len(a)}")
            tot = sum(r["n_dois"] for r in a)
            print(f"\n  {arm}: {len(a)} reports, {tot} DOI citations, "
                  f"{sum(r['doi_not_found'] for r in a)} not found, {sum(r['doi_in_corpus'] for r in a)} in corpus, "
                  f"{sum(r['n_pdb'] for r in a)} PDB cited / {sum(r['pdb_names_protein'] for r in a)} name the protein; "
                  f"Hippo-family primary on {sum(r['primary_is_hippo'] for r in a)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
