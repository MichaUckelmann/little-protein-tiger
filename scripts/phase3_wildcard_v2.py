#!/usr/bin/env python3
"""Evaluate the two-track draft of wildcard-expert against the current skill.

    .venv/bin/python scripts/phase3_wildcard_v2.py run --arms wildcard1 wildcard2 --max-usd 4
    .venv/bin/python scripts/phase3_wildcard_v2.py gates          # apply the gates to every finished run, no API calls
    .venv/bin/python scripts/phase3_wildcard_v2.py report

Arms (same model, same tools, same corpus, same five prompts as Phase 2):
  wildcard1  the skill as it stands in skills/wildcard-expert/SKILL.md
  wildcard2  docs/skill_drafts/wildcard-expert-v2.md (scripts/build_wildcard_v2.py)

Cells land beside the Phase 2 reports in outputs/phase2/ as <prompt>_r<k>__<arm>, so the
Phase 2 judging scripts see them. The gates (src/hypothesis_gates.py) are applied OFFLINE to
each run's own candidates and tool responses, identically for both arms: for wildcard1, whose
`choices_json` has no evidence_chain, one is inferred from the numeric fields it does state,
so the grounding comparison is like for like.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import random
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import phase2_corpus_eval as P  # noqa: E402  (installs the notools arm; owns PROMPTS)
import phase2_common as C  # noqa: E402
from src import hypothesis_gates as G  # noqa: E402

ac = P.ac
OUT = _ROOT / "outputs" / "phase2"
ac.OUT = OUT
ac.SKILL = "wildcard-expert"
ARMS = ("wildcard1", "wildcard2")
DRAFT = _ROOT / "docs" / "skill_drafts" / "wildcard-expert-v2.md"
CANON_TIERS = {"VALIDATED", "BIOLOGICALLY_JUSTIFIED", "PATHWAY_INFERRED"}
_prev_patch = ac.patch
# The pipeline's defaults are 30 tool rounds and 100k input tokens (ablate_corpus raised the tokens to 200k).
# The current wildcard skill ran out of rounds on tau repeat 2 under that cap, and the two-track draft makes
# 54 to 59 tool calls per run, so both skills run here with 60 rounds and a 400k ceiling. This is an
# EXPERIMENT setting: the production defaults are unchanged, and whether a promoted skill needs a higher
# cap is a result of this evaluation, not an assumption. Runs of the five reused diagnostic cells (v1,
# repeat 1) and the first two v2 cells ran under the old cap and all finished well inside it.
MAX_ITER = 60
MAX_INPUT_TOKENS = 400_000


def _make_runner(config):
    from src.skill_runner import SkillRunner
    models = config.get("models", {}).get("gemini", {})
    return SkillRunner(skill_name=ac.SKILL, provider="gemini", model_id=models.get("default") or "gemini-3.7-flash",
                       config=config, max_iter=MAX_ITER, max_input_tokens=MAX_INPUT_TOKENS)


ac.make_runner = _make_runner


def _patch(runner, arm, cache):
    if arm not in ARMS:
        return _prev_patch(runner, arm, cache)
    import src.skill_runner as sr
    if arm == "wildcard2":
        runner.system_prompt = DRAFT.read_text(encoding="utf-8") + sr._OUTPUT_FORMAT_RULE
    # wildcard1: the system prompt the runner loaded from skills/wildcard-expert/SKILL.md, untouched


ac.patch = _patch


# ------------------------------------------------------------------ run
def run(arms, max_usd, reps=2, only=None):
    import yaml
    P.preflight()
    config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cells = [(slug, f"r{k}", arm, q) for slug, _s, q, _c in P.PROMPTS if not only or slug in only for k in range(1, reps + 1) for arm in arms]
    random.Random("wc2-2026-10-08").shuffle(cells)
    spent = 0.0
    for slug, rep, arm, q in cells:
        if spent >= max_usd:
            print(f"stopping: ${spent:.2f} >= {max_usd}")
            break
        m = ac.cell(f"{slug}_{rep}", arm, q, config, force=False)
        spent += m.get("usd") or 0
        if m.get("error"):
            # Running out of tool rounds or input tokens is an OUTCOME of the skill (the pipeline's own
            # limits: 30 rounds, 100k tokens), recorded and counted as a failed run. Anything else
            # (TLS, API errors) is a setup problem: discard the cell and stop.
            if re.match(r"RuntimeError: (Max iterations|Input token limit)", m["error"]):
                print(f"  {slug}_{rep}/{arm}: recorded as a FAILED RUN ({m['error'][:70]})")
                continue
            for ext in ("md", "json", "trace"):
                p = OUT / f"{slug}_{rep}__{arm}.{ext}"
                (p.unlink(missing_ok=True) if p.is_file() else __import__("shutil").rmtree(p, ignore_errors=True))
            print("error, cell discarded, stopping:", m["error"][:160])
            break
    print(f"spent ${spent:.2f}")


# ------------------------------------------------------------------ gates
def responses_by_tool(name: str, arm: str) -> dict[str, str]:
    tr = json.loads((OUT / f"{name}__{arm}.trace" / "trace_raw.json").read_text(encoding="utf-8"))
    by = collections.defaultdict(list)
    for m in tr:
        for part in m.get("parts", []):
            fr = part.get("functionResponse")
            if fr:
                by[fr["name"]].append((fr.get("response") or {}).get("result", ""))
    return {k: "\n".join(v) for k, v in by.items()}


def infer_v1(c: dict, report: str) -> dict:
    """wildcard1 states no track or chain: derive them from what it does state."""
    c = dict(c)
    c["track"] = "canonical" if c.get("tier") in CANON_TIERS else "corpus_derived"
    chain = []
    if c.get("novelty_score") is not None:
        chain.append({"kind": "novelty_score", "value": str(c["novelty_score"]), "tool": "novelty_signal"})
    mr = c.get("depmap_max_r_to_hubs")
    if isinstance(mr, dict) and mr.get("r") is not None:
        chain.append({"kind": "depmap_r", "value": str(mr["r"]), "tool": "get_genetic_codependency"})
    for n in c.get("depmap_neighborhood") or []:
        if isinstance(n, dict) and n.get("r") is not None:
            chain.append({"kind": "depmap_r", "value": str(n["r"]), "tool": "find_cocorrelated_genes"})
    for d in re.findall(r"10\.\d{4,9}/[^\s\"',;)\]]+", f"{c.get('evidence_basis', '')}"):
        chain.append({"kind": "doi", "value": d.rstrip("."), "tool": "get_fingerprint"})
    c["evidence_chain"] = chain
    return c


def entries_for(ids, cache_path=OUT / "pdb_cache.json"):
    import requests
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    todo = [i for i in dict.fromkeys(i.upper() for i in ids) if i not in cache]
    q = ("query($ids:[String!]!){entries(entry_ids:$ids){rcsb_id polymer_entities{rcsb_polymer_entity{pdbx_description}"
         " rcsb_entity_source_organism{rcsb_gene_name{value}}}}}")
    for i in range(0, len(todo), 40):
        chunk = todo[i:i + 40]
        r = requests.post("https://data.rcsb.org/graphql", json={"query": q, "variables": {"ids": chunk}}, timeout=40)
        found = {}
        for e in (r.json().get("data") or {}).get("entries") or []:
            desc, genes = [], set()
            for pe in e.get("polymer_entities") or []:
                desc.append(((pe.get("rcsb_polymer_entity") or {}).get("pdbx_description")) or "")
                for s in pe.get("rcsb_entity_source_organism") or []:
                    genes |= {g.get("value", "") for g in s.get("rcsb_gene_name") or []}
            found[e["rcsb_id"].upper()] = {"exists": True, "descriptions": desc, "genes": sorted(genes)}
        for pid in chunk:
            cache[pid] = found.get(pid, {"exists": False})
    cache_path.write_text(json.dumps(cache, indent=1))
    return cache


def resolver():
    from src.identifier_normalizer import get_normalizer
    N = get_normalizer()

    def resolve(name: str) -> str:
        r = N.resolve(name)
        s = getattr(r, "human_gene_symbol", None) or (r.get("human_gene_symbol") if isinstance(r, dict) else None)
        return s or re.sub(r"[^A-Z0-9]", "", name.upper())
    return resolve


def skill_pref(report: str) -> str | None:
    m = re.search(r"- target_complex:\s*(.+)", report)
    return m.group(1).strip() if m else None


def gates() -> list[dict]:
    from src.env_config import load_env
    load_env()
    resolve = resolver()
    runs = []
    for arm in ARMS:
        for p in sorted(OUT.glob(f"*__{arm}.json")):
            name = p.stem.split("__")[0]
            meta = json.loads(p.read_text())
            if meta.get("error"):
                continue
            report = (OUT / f"{name}__{arm}.md").read_text(encoding="utf-8")
            cands = G.parse_choices(report)
            if arm == "wildcard1":
                cands = [infer_v1(c, report) for c in cands]
            ids = [i for c in cands for i in (c.get("pdb_ids") or [])]
            entries = entries_for(ids) if ids else {}
            resp = responses_by_tool(name, arm)
            res = G.evaluate(cands, resp, entries, resolve)
            pref = skill_pref(report)
            sel = G.select_forward(cands, res, "novel_if_eligible", pref)
            pref_res = next((r for r, c in zip(res, cands) if c.get("complex") == pref), None)
            row = {"name": name, "arm": arm, "n_candidates": len(cands), "pref": pref,
                   "pref_eligible": bool(pref_res and pref_res.eligible), "pref_failed": pref_res.failed if pref_res else ["not in choices_json"],
                   "selected": sel, "candidates": [{"complex": r.complex, "track": r.track, "eligible": r.eligible, "failed": r.failed,
                                                   "checks": {k: v[1] for k, v in r.checks.items()}} for r in res],
                   "chain_items": sum(len(c.get("evidence_chain") or []) for c in cands),
                   "chain_grounded": sum(G.check_evidence_chain(c, resp)[2]["grounded"] for c in cands)}
            runs.append(row)
            (OUT / f"{name}__{arm}.gates.json").write_text(json.dumps(row, indent=1))
    return runs


def report() -> None:
    runs = gates()
    print(f"\n{'arm':10}{'runs':>5}{'cands':>7}{'Track B':>8}{'eligible':>9}{'chain items grounded':>22}{'skill pref passes':>19}{'forwarded track B':>19}")
    for arm in ARMS:
        r = [x for x in runs if x["arm"] == arm]
        if not r:
            continue
        nb = sum(1 for x in r for c in x["candidates"] if c["track"] == "corpus_derived")
        el = sum(1 for x in r for c in x["candidates"] if c["eligible"])
        ci, cg = sum(x["chain_items"] for x in r), sum(x["chain_grounded"] for x in r)
        pp = sum(x["pref_eligible"] for x in r)
        fb = sum(1 for x in r if x["selected"]["track"] == "corpus_derived")
        print(f"{arm:10}{len(r):>5}{sum(x['n_candidates'] for x in r):>7}{nb:>8}{el:>9}{f'{cg}/{ci}':>22}{f'{pp}/{len(r)}':>19}{f'{fb}/{len(r)}':>19}")
        fails = collections.Counter(g for x in r for c in x["candidates"] for g in c["failed"])
        print("           gate failures:", dict(fails), "| forwarded UNGATED:", sum(1 for x in r if not x["selected"]["gated"]))
    print("\nper run (arm, prompt, skill preference -> forwarded):")
    for x in sorted(runs, key=lambda x: (x["arm"], x["name"])):
        print(f"  {x['arm']:10}{x['name']:10} pref {str(x['pref'])[:22]:22} pref-ok {str(x['pref_eligible']):5} -> {str(x['selected']['chosen'])[:22]:22} ({x['selected']['track']}, {'gated' if x['selected']['gated'] else 'UNGATED'})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["run", "gates", "report"])
    ap.add_argument("--arms", nargs="+", choices=list(ARMS), default=list(ARMS))
    ap.add_argument("--max-usd", type=float, default=4.0)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--only", nargs="+", help="prompt slugs")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a.arms, a.max_usd, a.reps, a.only)
    else:
        report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
