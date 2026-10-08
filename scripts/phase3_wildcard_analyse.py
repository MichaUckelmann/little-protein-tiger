#!/usr/bin/env python3
"""Analyse the wildcard v1 / v2 evaluation: gates, grounding, judged quality, novelty against closed-book.

    .venv/bin/python scripts/phase3_wildcard_analyse.py

Reads what phase3_wildcard_v2.py (gates), phase2_judge.py (rubric, claim support, pair_* tasks)
and the closed-book arm already wrote. Paired by prompt, bootstrap intervals over prompts' reports,
no p-values: ten reports per arm detect only large effects.
"""
from __future__ import annotations

import collections
import json
import random
import re
import statistics as st
import sys

import phase2_common as C

ARMS = ["notools", "live", "wildcard1", "wildcard2"]
CRIT = ["target_rationale", "traceability", "calibration", "prior_art", "usefulness"]
PAIRS = [("wildcard2", "live"), ("wildcard2", "notools"), ("wildcard2", "wildcard1")]


def jl(task, judge):
    p = C.JUDGE_DIR / f"{task}__{judge}.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def diff(values, a, b, rng=None):
    ds = []
    for p in sorted({k[0] for k in values}):
        x, y = values.get((p, a)), values.get((p, b))
        if x and y:
            if rng:
                x, y = [rng.choice(x) for _ in x], [rng.choice(y) for _ in y]
            ds.append(st.mean(x) - st.mean(y))
    return (st.mean(ds), ds) if ds else (None, [])


def ci(values, a, b, B=3000):
    est, per = diff(values, a, b)
    if est is None:
        return None
    rng = random.Random(7)
    bs = sorted(diff(values, a, b, rng)[0] for _ in range(B))
    return round(est, 2), [round(bs[int(.025 * B)], 2), round(bs[int(.975 * B)], 2)], sum(d > 0 for d in per), len(per)


def main() -> int:
    cells = {(p, r, a): m for p, r, a, m in C.cells() if a in ARMS}
    arm_of = {C.blind_id(p, r, a): (p, a) for (p, r, a) in cells}
    out = {}
    print("reports per arm:", {a: sum(1 for k in cells if k[2] == a) for a in ARMS})

    # ---- gates (wildcard arms)
    print("\nGATES (deterministic; src/hypothesis_gates.py)")
    for arm in ("wildcard1", "wildcard2"):
        g = [json.loads(p.read_text()) for p in C.OUT.glob(f"*__{arm}.gates.json")]
        if not g:
            continue
        nb = [c for x in g for c in x["candidates"] if c["track"] == "corpus_derived"]
        fails = collections.Counter(f for c in nb for f in c["failed"])
        ci_, cg = sum(x["chain_items"] for x in g), sum(x["chain_grounded"] for x in g)
        print(f"  {arm}: {len(g)} runs | corpus-derived candidates {len(nb)}, eligible {sum(c['eligible'] for c in nb)} | "
              f"chain items grounded {cg}/{ci_} ({100 * cg / max(ci_, 1):.0f}%) | skill preference passes gates in {sum(x['pref_eligible'] for x in g)}/{len(g)} runs")
        print(f"     failures among corpus-derived candidates: {dict(fails)} | forwarded corpus-derived: "
              f"{sum(1 for x in g if x['selected']['track'] == 'corpus_derived')}/{len(g)}, ungated: {sum(1 for x in g if not x['selected']['gated'])}")
        out[f"gates_{arm}"] = {"runs": len(g), "chain": [cg, ci_], "pref_pass": sum(x["pref_eligible"] for x in g)}

    # ---- novelty against the closed-book arm: do the corpus-derived pairs appear in its candidate lists?
    print("\nNOVELTY against closed-book (candidate pairs the no-tools reports list for the same prompt)")
    sys.path.insert(0, str(C.ROOT))
    from src import hypothesis_gates as G
    from phase3_wildcard_v2 import resolver
    resolve = resolver()
    lists = collections.defaultdict(set)
    for (p, r, a) in cells:
        if a in ("notools", "live"):
            for c in G.parse_choices(C.report_text(p, r, a)):
                lists[(p, a)].add(G.pair_key(c.get("complex", ""), resolve))
    for arm in ("wildcard1", "wildcard2"):
        tot = novel_nb = elig = elig_novel = 0
        for x in (json.loads(p.read_text()) for p in C.OUT.glob(f"*__{arm}.gates.json")):
            prompt = x["name"].rsplit("_", 1)[0]
            for c in x["candidates"]:
                if c["track"] != "corpus_derived":
                    continue
                k = G.pair_key(c["complex"], resolve)
                tot += 1
                nov = k not in lists[(prompt, "notools")]
                novel_nb += nov
                if c["eligible"]:
                    elig += 1
                    elig_novel += nov
        print(f"  {arm}: corpus-derived {tot}, absent from the no-tools lists {novel_nb} | eligible {elig}, of which absent from no-tools lists {elig_novel}")

    # ---- rubric
    print("\nRUBRIC (1-5), mean over reports; difference paired by prompt")
    for judge in C.JUDGES:
        v = {c: {} for c in CRIT + ["overall", "overall_ex_trace"]}
        for r in jl("rubric", judge):
            if r.get("parsed") and r["blind"] in arm_of:
                s = r["parsed"]["scores"]
                key = arm_of[r["blind"]]
                for c in CRIT:
                    v[c].setdefault(key, []).append(s[c])
                v["overall"].setdefault(key, []).append(st.mean(s[c] for c in CRIT))
                v["overall_ex_trace"].setdefault(key, []).append(st.mean(s[c] for c in CRIT if c != "traceability"))
        means = {a: round(st.mean(x for (p, aa), xs in v["overall"].items() if aa == a for x in xs), 2) for a in ARMS if any(aa == a for (_, aa) in v["overall"])}
        print(f"  {judge}: overall mean by arm {means}")
        for a, b in PAIRS:
            o, ex = ci(v["overall"], a, b), ci(v["overall_ex_trace"], a, b)
            if o:
                print(f"     {a} - {b:10} overall {o[0]:+.2f} {o[1]} ({o[2]}/{o[3]} prompts) | excluding traceability {ex[0]:+.2f} {ex[1]}")
        out[f"rubric_{judge}"] = means

    # ---- pairwise
    print("\nPAIRWISE (both presentation orders must agree; wins : losses : order-inconsistent)")
    for judge in C.JUDGES:
        for a, b in PAIRS:
            pw = {}
            for r in jl(f"pair_{a}_vs_{b}", judge):
                if r.get("parsed"):
                    w = r["parsed"].get("winner")
                    win = r["A"] if w == "A" else r["B"] if w == "B" else "tie"
                    pw.setdefault((r["prompt"], r["rep"]), {})[r["order"]] = win
            if pw:
                W = sum(1 for o in pw.values() if o.get("a_first") == o.get("b_first") == a)
                L = sum(1 for o in pw.values() if o.get("a_first") == o.get("b_first") == b)
                print(f"  {judge:7} {a} vs {b:10} {W}:{L}:{len(pw) - W - L}  (n={len(pw)} pairs)")

    # ---- claim support
    print("\nCLAIM SUPPORT (judge labels on sampled DOI-bearing lines; supported / partly / unsupported)")
    for judge in C.JUDGES:
        c = collections.defaultdict(collections.Counter)
        for r in jl("support", judge):
            if r.get("parsed") and r["blind"] in arm_of:
                c[arm_of[r["blind"]][1]][r["parsed"].get("label")] += 1
        print(f"  {judge}: " + " | ".join(f"{a} {c[a]['supported']}/{c[a]['partly']}/{c[a]['unsupported']}" for a in ARMS if c[a]))
    (C.OUT / "wildcard_analysis.json").write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
