#!/usr/bin/env python3
"""Phase 2: apply the decision rules in docs/phase2_corpus_eval.md to the scored and judged reports.

    .venv/bin/python scripts/phase2_analyse.py

Reads outputs/phase2/scores.json (phase2_score.py) and whatever judgements exist
under outputs/phase2/judgements/ (phase2_judge.py); a missing tier is reported as
"not run", never filled in. Writes outputs/phase2/analysis.json and analysis.md.

Method: every comparison is PAIRED BY PROMPT. For a metric, each prompt gets an
arm mean over its 3 repeats, the effect is the mean over the 5 prompts of
(live - blank), and the interval is a bootstrap that resamples the REPORTS
inside each prompt x arm cell 5,000 times. Five prompts and 15 reports per arm
detect only large effects, and no p-value is claimed. The thresholds are the
ones written down before any output existed; nothing here is tuned.
"""
from __future__ import annotations

import json
import random
import statistics as st
import sys

import phase2_common as C

NICHE = {"sting", "alt", "tau"}
CRIT = ["target_rationale", "traceability", "calibration", "prior_art", "usefulness"]
B = 5000
BASE = sys.argv[1] if len(sys.argv) > 1 else "blank"     # the arm compared with `live`: blank | notools
assert BASE in ("blank", "notools"), "usage: phase2_analyse.py [blank|notools]"


def jl(task, judge):
    p = C.JUDGE_DIR / f"{task}__{judge}.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def paired(values, rng=None):
    """values: {(prompt, arm): [x,...]} -> mean over prompts of (live mean - blank mean)."""
    prompts = sorted({p for p, _a in values})
    d = []
    for p in prompts:
        l, b = values.get((p, "live")), values.get((p, BASE))
        if l and b:
            if rng:
                l = [rng.choice(l) for _ in l]
                b = [rng.choice(b) for _ in b]
            d.append(st.mean(l) - st.mean(b))
    return (st.mean(d), d) if d else (None, [])


def effect(values, label):
    est, per = paired(values)
    if est is None:
        return {"metric": label, "note": "not enough data"}
    rng = random.Random(7)
    boots = sorted(paired(values, rng)[0] for _ in range(B))
    return {"metric": label, "live_minus_blank": round(est, 3), "ci95": [round(boots[int(.025 * B)], 3), round(boots[int(.975 * B)], 3)],
            "per_prompt": {p: round(x, 3) for p, x in zip(sorted({p for p, _ in values}), per)},
            "live_mean": round(st.mean(x for (p, a), v in values.items() if a == "live" for x in v), 3),
            "baseline_mean": round(st.mean(x for (p, a), v in values.items() if a == BASE for x in v), 3)}


def kappa(a, b):
    cats = sorted(set(a) | set(b))
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in cats)
    return round((po - pe) / (1 - pe), 2) if pe < 1 else 1.0


def main() -> int:
    scores = [r for r in json.loads((C.OUT / "scores.json").read_text()) if r["arm"] in ("live", BASE) and r["prompt"] in C.PHASE2_PROMPTS]
    arm_of = {C.blind_id(r["prompt"], r["rep"], r["arm"]): (r["prompt"], r["arm"]) for r in scores}
    out = {"n_reports": len(scores)}

    # ---- A. deterministic
    def vals(f):
        d = {}
        for r in scores:
            x = f(r)
            if x is not None:
                d.setdefault((r["prompt"], r["arm"]), []).append(x)
        return d
    A = [effect(vals(lambda r: r["doi_not_found"]), "DOIs not found per report"),
         effect(vals(lambda r: r["doi_not_found"] / r["n_dois"] if r["n_dois"] else None), "fraction of cited DOIs not found"),
         effect(vals(lambda r: r["n_dois"]), "DOIs cited per report"),
         effect(vals(lambda r: r["doi_in_corpus"]), "corpus DOIs cited per report"),
         effect(vals(lambda r: 1 - r["pdb_names_protein"] / r["n_pdb"] if r["n_pdb"] else None), "fraction of cited PDB entries NOT naming the protein"),
         effect(vals(lambda r: r["usd"]), "USD per report")]
    out["A_deterministic"] = A
    tot = {a: sum(r["n_dois"] for r in scores if r["arm"] == a) for a in ("live", BASE)}
    out["doi_totals"] = tot
    live_top = {}
    for r in scores:
        if r["arm"] == "live":
            for g in r["primary_pair"]:
                live_top[g] = live_top.get(g, 0) + 1
    out["live_primary_gene_counts"] = dict(sorted(live_top.items(), key=lambda x: -x[1])[:8])
    out["live_hippo_primary"] = sum(r["primary_is_hippo"] for r in scores if r["arm"] == "live")
    out["baseline_hippo_primary"] = sum(r["primary_is_hippo"] for r in scores if r["arm"] == BASE)

    # ---- B. claim support
    B_res = {}
    for judge in C.JUDGES:
        rows = jl("support", judge)
        if not rows:
            B_res[judge] = "not run"
            continue
        per = {}
        for r in rows:
            if r.get("refused") or r.get("error") or not r.get("parsed"):
                continue
            if r["blind"] not in arm_of:
                continue
            per.setdefault(r["blind"], []).append(r["parsed"].get("label"))
        uns, frac = {}, {}
        for bid, labs in per.items():
            p, arm = arm_of[bid]
            assessed = [l for l in labs if l in ("supported", "partly", "unsupported")]
            uns.setdefault((p, arm), []).append(labs.count("unsupported"))
            if assessed:
                frac.setdefault((p, arm), []).append(labs.count("unsupported") / len(assessed))
        B_res[judge] = {"refused": sum(1 for r in rows if r.get("refused")), "items": len(rows),
                        "label_counts": {l: sum(1 for r in rows if (r.get("parsed") or {}).get("label") == l)
                                         for l in ("supported", "partly", "unsupported", "cannot_assess")},
                        "unsupported_per_report": effect(uns, "unsupported claims per report"),
                        "unsupported_fraction_of_assessed": effect(frac, "unsupported fraction of assessed claims")}
    out["B_claim_support"] = B_res

    # ---- C. rubric and pairwise
    C_res = {}
    rub_by_judge = {}
    for judge in C.JUDGES:
        rows = [r for r in jl("rubric", judge) if r.get("parsed") and not r.get("refused")]
        if not rows:
            C_res[judge] = "not run"
            continue
        rows = [r for r in rows if r["blind"] in arm_of]
        rub_by_judge[judge] = {r["blind"]: r["parsed"]["scores"] for r in rows if "scores" in r["parsed"]}
        overall, crit = {}, {c: {} for c in CRIT}
        for bid, sc in rub_by_judge[judge].items():
            p, arm = arm_of[bid]
            ok = [sc[c] for c in CRIT if isinstance(sc.get(c), (int, float))]
            if ok:
                overall.setdefault((p, arm), []).append(st.mean(ok))
            for c in CRIT:
                if isinstance(sc.get(c), (int, float)):
                    crit[c].setdefault((p, arm), []).append(sc[c])
        C_res[judge] = {"overall": effect(overall, "rubric mean (1-5)"), "by_criterion": [effect(crit[c], c) for c in CRIT]}
        pw = {}
        for r in jl(f"pairwise_{BASE}", judge):
            if r.get("parsed") and not r.get("refused"):
                w = r["parsed"].get("winner")
                win = r["A"] if w == "A" else r["B"] if w == "B" else "tie"
                pw.setdefault((r["prompt"], r["rep"]), {})[r["order"]] = win
        res = {"live": 0, BASE: 0, "tie": 0, "order_inconsistent": 0}
        for orders in pw.values():
            if len(orders) == 2:
                a, b = orders["live_first"], orders["base_first"]
                res[a if a == b else "order_inconsistent"] += 1
        C_res[judge]["pairwise_consistent"] = res
    out["C_quality"] = C_res
    if len(rub_by_judge) == 2:
        j1, j2 = list(rub_by_judge.values())
        common = sorted(set(j1) & set(j2))
        binned = lambda x: "low" if x <= 2 else "mid" if x == 3 else "high"
        pa, pb = [], []
        for bid in common:
            for c in CRIT:
                if isinstance(j1[bid].get(c), (int, float)) and isinstance(j2[bid].get(c), (int, float)):
                    pa.append(binned(round(j1[bid][c])))
                    pb.append(binned(round(j2[bid][c])))
        out["judge_agreement_kappa_binned"] = kappa(pa, pb)

    # ---- decision rules (fixed in advance)
    d = {}
    nf = next((x for x in A if x["metric"] == "fraction of cited DOIs not found"), {})
    d[f"{BASE}_cites_too_little_to_compare_dois"] = tot[BASE] < 15
    d["hallucination_dois"] = ("not computed" if "ci95" not in nf else
                               "live lower, CI excludes 0" if nf["ci95"][1] < 0 else
                               "no measurable difference (CI includes 0 or live higher)")
    sup = [v.get("unsupported_per_report") for v in B_res.values() if isinstance(v, dict)]
    d["hallucination_claims"] = ("not run" if not sup else
                                 ["live lower, CI excludes 0" if s.get("ci95", [0, 1])[1] < 0 else "no measurable difference" for s in sup])
    q = []
    for judge, v in C_res.items():
        if isinstance(v, dict):
            o = v["overall"]
            pp = [x for x in o.get("per_prompt", {}).values()]
            q.append({"judge": judge, "diff": o.get("live_minus_blank"), "prompts_live_better": sum(x > 0 for x in pp), "of": len(pp)})
    d["quality"] = q if q else "not run"
    d["quality_claim_holds"] = (bool(q) and len(q) == 2 and all((x["diff"] or 0) >= 0.5 and x["prompts_live_better"] >= 4 for x in q))
    d["harm_hippo_pull"] = (f"live chose a Hippo-family primary on {out['live_hippo_primary']} of "
                            f"{sum(1 for r in scores if r['arm'] == 'live')} live reports (rule: 3+ is reported as a finding)")
    out["decision"] = d

    (C.OUT / f"analysis_{BASE}.json").write_text(json.dumps(out, indent=1))
    lines = ["# Phase 2 analysis (rules fixed in docs/phase2_corpus_eval.md)\n", f"reports scored: {out['n_reports']}; DOIs cited: {tot}\n"]
    for e in A:
        if "ci95" in e:
            lines.append(f"- {e['metric']}: live {e['live_mean']} vs {BASE} {e['baseline_mean']}; live-minus-baseline {e['live_minus_blank']} (95% CI {e['ci95']})")
    lines.append("\n## claim support"); lines.append(json.dumps(B_res, indent=1)[:2500])
    lines.append("\n## quality"); lines.append(json.dumps(C_res, indent=1)[:3500])
    lines.append("\n## decision"); lines.append(json.dumps(d, indent=1))
    (C.OUT / f"analysis_{BASE}.md").write_text("\n".join(lines))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
