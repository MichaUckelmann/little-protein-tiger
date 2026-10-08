#!/usr/bin/env python3
"""Does the `blank` arm of the corpus ablation actually withhold the corpus?

    .venv/bin/python scripts/audit_blanking.py

Builds the same patched runner `ablate_corpus.py` uses for the blank arm, then
calls every tool the pathway skill can reach, with a Python audit hook watching
which files and directories are opened.

  blanked tools   must return exactly NOTHING and open no corpus file;
  live tools      (LIVE_BY_DESIGN) must open no corpus file and return no DOI
                  that is in the fingerprint set.

"Corpus file" = the fingerprint directory, the vector index, the clusters, the
edge index and the paper database. DepMap's CSV and RCSB's metadata cache are
allowed: neither holds paper content. Exit code 1 on any violation, so this can
gate a run.

Limits, stated because a clean result is only as good as the tripwire: the audit
hook sees Python-level opens and directory scans, not reads done natively inside
LanceDB or pyarrow. The vector index is reached only through `search_corpus`,
which is blanked and must not reach its implementation at all; that is asserted
separately by tests/test_ablate_corpus_blanking.py. A tool's call arguments here
are representative, not exhaustive.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ablate_corpus as ac  # noqa: E402

DATA = (ROOT / "data").resolve()
CORPUS_PATHS = [DATA / "fingerprints", DATA / "vectors", DATA / "clusters.json",
                DATA / "depmap_edges.parquet", DATA / "literature.db"]
ALLOWED = [DATA / "depmap", DATA / "pdb_metadata.json"]
EVENTS: list[tuple[str, str]] = []
ARMED = False


def _hook(event, args):
    if not ARMED or event not in ("open", "os.listdir", "os.scandir", "glob.glob"):
        return
    raw = args[0]
    if isinstance(raw, int) or raw is None:
        return
    try:
        p = Path(raw if not isinstance(raw, bytes) else raw.decode()).resolve()
    except Exception:
        return
    if any(p == a or a in p.parents for a in ALLOWED):
        return
    if any(p == c or c in p.parents for c in CORPUS_PATHS):
        EVENTS.append((event, str(p.relative_to(ROOT))))


# Representative arguments per tool. Every key a tool might read is supplied.
ARGS = {
    "search_corpus": {"query": "STING signalling autoinflammation", "top_k": 8},
    "get_fingerprint": {"identifier": "10.1038/s41467-017-00301-4"},
    "find_pdb_structures": {"proteins": ["STING1", "TBK1"]},
    "get_interactions_for": {"protein": "STING1"},
    "find_quantitative_evidence": {"protein_pair": ["STING1", "TBK1"]},
    "shortest_interaction_path": {"protein_a": "STING1", "protein_b": "TBK1"},
    "interaction_hubs": {"top_n": 5},
    "novelty_signal": {"protein": "STING1"},
    "export_subgraph": {"seeds": ["STING1"], "output_path": "/dev/null"},
    "cluster_for_protein": {"protein": "STING1"},
    "cluster_members": {"cluster_id": 1},
    "find_clusters_by_keyword": {"query": "STING"},
    "search_rcsb_pdb": {"proteins": ["STING1"]},
    "get_genetic_codependency": {"protein_a": "STING1", "protein_b": "TBK1"},
    "find_cocorrelated_genes": {"protein": "STING1", "top_k": 5},
}


def main() -> int:
    global ARMED
    import yaml
    from src.env_config import load_env
    load_env()
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    runner = ac.make_runner(config)
    ac.patch(runner, "blank", {})
    fp_dois = {f.stem[4:].replace("_", "/", 1).lower() for f in (DATA / "fingerprints").glob("doi_*.json")}
    sys.addaudithook(_hook)

    rows, bad = [], 0
    tools = sorted(ac.CORPUS_TOOLS) + sorted(t for t in ac.LIVE_BY_DESIGN if t in ARGS)
    for name in tools:
        EVENTS.clear()
        ARMED = True
        try:
            out = runner._execute_tool(name, ARGS[name])
        except Exception as exc:  # noqa: BLE001 - a crash is reported, not hidden
            out = f"EXCEPTION {type(exc).__name__}: {exc}"
        ARMED = False
        blanked = name in ac.CORPUS_TOOLS
        dois = {d.rstrip(".").lower() for d in re.findall(r"10\.\d{4,9}/[^\s\"',;)\]]+", out)} - {d for d in () }
        corpus_dois = sorted(d for d in dois if d in fp_dois)
        problems = []
        if blanked and out != ac.NOTHING:
            problems.append("blanked tool returned something other than NOTHING")
        if EVENTS:
            problems.append(f"opened corpus paths: {sorted({e[1] for e in EVENTS})[:3]}")
        if not blanked and corpus_dois:
            problems.append(f"returned corpus DOIs: {corpus_dois[:3]}")
        bad += bool(problems)
        rows.append({"tool": name, "class": "BLANKED" if blanked else "live", "chars": len(out),
                     "corpus_opens": len(EVENTS), "corpus_dois": len(corpus_dois), "problems": problems,
                     "head": out[:90].replace("\n", " ")})
        print(f"  {name:28}{'BLANKED' if blanked else 'live':8}{len(out):>8} chars  corpus opens {len(EVENTS):>3}  "
              f"{'OK' if not problems else 'LEAK: ' + '; '.join(problems)}\n      {rows[-1]['head']}")
    (ROOT / "outputs" / "phase2").mkdir(parents=True, exist_ok=True)
    (ROOT / "outputs" / "phase2" / "blanking_audit.json").write_text(json.dumps(rows, indent=1))
    print(f"\n{len(rows)} tools audited; {bad} with problems")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
