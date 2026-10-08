#!/usr/bin/env python3
"""Score the DOIs in finished stage reports against an EXTERNAL ground truth.

`PipelineRunner._verify_citations` asks "is this DOI in our corpus?". That is
the wrong question for a hallucination claim, in both directions: with corpus
tools on, a model cites corpus DOIs almost by construction, so a pass is partly
circular; with them off, a perfectly real paper we do not hold fails. This asks
the question that matters — does the DOI EXIST (Crossref), and does what it
resolves to look like the paper the report means — and splits the answer by
whether the corpus held it.

    python scripts/audit_citations_external.py                 # score everything found
    python scripts/audit_citations_external.py --no-network    # extract + corpus split only
    python scripts/audit_citations_external.py --report        # re-print from the cache

Reads (read-only): `outputs/ablation/*.md` (arm from the file name:
live/blank/decoy, see `ablate_corpus.py`) and `projects/*/runs/*/0[01]_*.md`
(arm "pipeline"). Writes `outputs/citation_audit.json`, which doubles as the
Crossref cache, so a rerun costs nothing. No LLM, no GPU, $0.

DOIs that appear in any `skills/*/SKILL.md` are EXCLUDED: a skill prompt carries
worked examples (e.g. an eLife DOI), and a model repeating one has copied its
instructions, not recalled or invented a paper. Counting them would score
prompt-copying as recall. The CITATION VERIFICATION block the pipeline appends
is stripped for the same reason — it restates DOIs the stage already cited.

A 404 from Crossref is not proof of fabrication on its own: a few registries
(DataCite, mEDRA, some preprint servers) are not in Crossref. 404s are therefore
re-tried against doi.org before being counted as non-resolving.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.fingerprint_store import load_fingerprint  # noqa: E402

CACHE = _ROOT / "outputs" / "citation_audit.json"
FP_DIR = _ROOT / "data" / "fingerprints"
_DOI = re.compile(r"\b(10\.\d{4,9}/(?:[^\s,;:()\[\]{}\"'<>]|\([^\s()]*\))+)")
_CV = re.compile(r"\n#{1,3}\s*(CITATION VERIFICATION|MODEL PROVENANCE).*", re.S | re.I)


def _fp_index() -> dict[str, Path]:
    """lowercased DOI -> fingerprint path (files keep the DOI's original case)."""
    idx = {}
    for f in FP_DIR.glob("doi_*.json"):
        idx[f.stem[4:].replace("_", "/", 1).lower()] = f
    return idx


def _clean(doi: str) -> str:
    return doi.rstrip(".*_`").lower()


def prompt_dois() -> set[str]:
    out: set[str] = set()
    for f in (_ROOT / "skills").glob("*/SKILL.md"):
        out |= {_clean(m) for m in _DOI.findall(f.read_text(encoding="utf-8", errors="ignore"))}
    return out


def collect() -> list[tuple[str, str, str]]:
    """(arm, source file label, report text) for every report with citations."""
    items = []
    for f in sorted((_ROOT / "outputs" / "ablation").glob("*__*.md")):
        arm = f.stem.split("__")[1]
        items.append((arm, f"ablation/{f.stem}", f.read_text(encoding="utf-8", errors="ignore")))
    for f in sorted((_ROOT / "projects").glob("*/runs/*/0[01]_*.md")):
        items.append(("pipeline", f"{f.parts[-4]}/{f.name}", f.read_text(encoding="utf-8", errors="ignore")))
    return items


def _norm(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return " ".join(re.sub(r"[^a-z0-9 ]", "", s.lower()).split())


def resolve(doi: str, session) -> dict:
    """Crossref lookup, falling back to doi.org on a 404."""
    try:
        r = session.get(f"https://api.crossref.org/works/{doi}", timeout=20)
        if r.status_code == 200:
            m = r.json()["message"]
            return {"status": "resolves", "title": " ".join(re.sub(r"<[^>]+>", "", (m.get("title") or [""])[0]).split()),
                    "year": ((m.get("issued") or {}).get("date-parts") or [[None]])[0][0],
                    "container": (m.get("container-title") or [""])[0], "via": "crossref"}
        if r.status_code == 404:
            # Not in Crossref. Ask the DOI system which agency holds it (DataCite,
            # mEDRA, ...); a 404 here too means the DOI is registered nowhere.
            a = session.get(f"https://api.crossref.org/works/{doi}/agency", timeout=20)
            if a.status_code == 200:
                ag = (a.json().get("message") or {}).get("agency", {}).get("label", "other")
                return {"status": "resolves_other_registry", "title": "", "via": ag}
            if a.status_code == 404:
                return {"status": "not_found", "title": "", "via": "crossref+agency"}
            return {"status": f"error_agency_{a.status_code}", "title": ""}
        return {"status": f"error_{r.status_code}", "title": ""}
    except Exception as exc:  # network failure must not read as fabrication
        return {"status": "error_network", "title": "", "detail": str(exc)[:120]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--delay", type=float, default=0.15)
    args = ap.parse_args()

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {"doi": {}, "cells": []}
    if not args.report:
        excluded = prompt_dois()
        cells, all_dois = [], set()
        for arm, label, text in collect():
            text = _CV.sub("", text)
            dois = sorted({_clean(d) for d in _DOI.findall(text)} - excluded)
            cells.append({"arm": arm, "file": label, "dois": dois})
            all_dois |= set(dois)
        cache["cells"] = cells
        cache["excluded_prompt_dois"] = sorted(excluded)
        todo = [d for d in sorted(all_dois) if d not in cache["doi"]
                or cache["doi"][d]["status"].startswith("error")
                or cache["doi"][d]["status"] == "unchecked"]
        if todo and not args.no_network:
            import requests
            s = requests.Session()
            s.headers["User-Agent"] = "lpt-citation-audit/1.0"
            for i, d in enumerate(todo, 1):
                cache["doi"].setdefault(d, {}).update(resolve(d, s))
                if i % 25 == 0:
                    print(f"  resolved {i}/{len(todo)}", file=sys.stderr)
                time.sleep(args.delay)
        idx = _fp_index()
        for d in all_dois:
            path = idx.get(d)
            fp = json.loads(path.read_text(encoding="utf-8")) if path else None
            e = cache["doi"].setdefault(d, {"status": "unchecked", "title": ""})
            e["in_corpus"] = fp is not None
            if fp is not None and e.get("title"):
                ft = fp.get("paper_metadata", {}).get("title", "")
                e["title_similarity_to_fingerprint"] = round(SequenceMatcher(None, _norm(e["title"]), _norm(ft)).ratio(), 2)
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps(cache, indent=1))

    # ---- report ----
    by_arm = defaultdict(lambda: defaultdict(int))
    nreports = defaultdict(int)
    for c in cache["cells"]:
        nreports[c["arm"]] += 1
        for d in c["dois"]:
            e = cache["doi"].get(d, {})
            ok = e.get("status", "unchecked")
            a = by_arm[c["arm"]]
            a["cited"] += 1
            a["in_corpus"] += bool(e.get("in_corpus"))
            a["resolves"] += ok.startswith("resolves")
            a["not_found"] += ok == "not_found"
            a["error/unchecked"] += ok.startswith("error") or ok == "unchecked"
            a["real_but_outside_corpus"] += ok.startswith("resolves") and not e.get("in_corpus")
    print(f"{'arm':10}{'reports':>8}{'cited':>7}{'in corpus':>10}{'resolve':>8}{'NOT FOUND':>10}{'real, not in corpus':>21}{'err':>5}")
    for arm in ("pipeline", "live", "decoy", "blank"):
        if arm in by_arm:
            a = by_arm[arm]
            print(f"{arm:10}{nreports[arm]:>8}{a['cited']:>7}{a['in_corpus']:>10}{a['resolves']:>8}{a['not_found']:>10}{a['real_but_outside_corpus']:>21}{a['error/unchecked']:>5}")
    nf = [(d, e) for d, e in cache["doi"].items() if e.get("status") == "not_found"]
    if nf:
        print("\nNOT FOUND (candidate fabrications):")
        for d, _ in nf:
            arms = sorted({c["arm"] + ":" + c["file"] for c in cache["cells"] if d in c["dois"]})
            print(f"  {d}   <- {', '.join(arms[:3])}")
    low = [(d, e) for d, e in cache["doi"].items() if e.get("in_corpus") and e.get("title_similarity_to_fingerprint", 1) < 0.6]
    if low:
        print(f"\nCorpus DOIs whose Crossref title differs from the fingerprint title (<0.6): {len(low)}")
        for d, e in low[:10]:
            print(f"  {d}  sim={e['title_similarity_to_fingerprint']}  {e['title'][:70]}")


if __name__ == "__main__":
    main()
