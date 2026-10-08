#!/usr/bin/env python3
"""Phase 2, tiers B and C: LLM judging of the generated reports. COSTS MONEY.

    .venv/bin/python scripts/phase2_judge.py support  --judge claude --dry-run
    .venv/bin/python scripts/phase2_judge.py support  --judge claude --max-usd 1.5
    .venv/bin/python scripts/phase2_judge.py rubric   --judge claude
    .venv/bin/python scripts/phase2_judge.py pairwise --judge claude
    .venv/bin/python scripts/phase2_judge.py human-packet

`support`  claim-by-claim: up to 8 DOI-bearing lines per report, judged against
           the cited paper's abstract (Europe PMC) and, for corpus papers, its
           fingerprint summary. Labels: supported / partly / unsupported /
           cannot_assess. Judged blind to arm.
`rubric`   five 1-5 scores per report, blind to arm, report order shuffled.
`pairwise` matched pairs (same prompt and repeat, live vs blank), BOTH
           presentation orders; counted only when the two orders agree.
`human-packet` six blinded pairs for hand scoring, with a separate key file.

Judges: `claude` (claude-sonnet-5) and `openai` (gpt-5.6-terra); neither is the
generator's family. A refusal is stored as `refused` and is NEVER retried on
another model, the rule the pipeline applies to its own stages. Everything is
resumable: finished items are skipped, so an interrupted run costs nothing to
continue. The judge sees no arm label, but live reports tend to cite with page
and section locators, so blinding is imperfect and the paper must say so.
"""
from __future__ import annotations

import argparse
import json
import random
import sys

import phase2_common as C

CRITERIA = {
    "target_rationale": "Is the recommended target well argued and specific (named interface, mechanism, why disrupting it matters), and is the reasoning correct as far as you can tell?",
    "traceability": "Are the factual claims attached to checkable sources (DOIs with locators) so a reader could verify them, rather than asserted without support?",
    "calibration": "Does the report say what is unknown, contested or weakly evidenced, instead of presenting everything as settled?",
    "prior_art": "Is the stated prior art and clinical status of the target, and of the inhibitors named, accurate as far as you know it? Penalise anything you believe is wrong or invented.",
    "usefulness": "Would this report let a protein-design team choose and start on a binder target (structure availability, which interface, what could go wrong)?",
}
ANCHORS = "1 = poor or wrong; 3 = adequate with clear gaps; 5 = excellent, specific and correct."

SUPPORT_SYS = (
    "You check whether cited papers support a specific claim in a report. You are given the claim line, and for each "
    "cited paper its title, abstract, and sometimes a short machine-made summary of the paper. Judge ONLY from that "
    "evidence, not from what you remember about the paper.\n"
    "Labels:\n"
    "- supported: the evidence states or directly implies the specific claim.\n"
    "- partly: the evidence is on the topic but does not show a specific part (a number, a mechanism detail, a causal step).\n"
    "- unsupported: the evidence contradicts the claim or is about something else.\n"
    "- cannot_assess: the evidence given is too thin to tell either way (for example the claim concerns a result that "
    "an abstract would not report).\n"
    'Return only JSON: {"label": "...", "reason": "<=40 words"}.'
)
RUBRIC_SYS = (
    "You are an expert reviewer of drug-target selection reports written for a protein-design team. The report is "
    "anonymous. Score it on five criteria, 1 to 5 each. " + ANCHORS + " Do not reward length. Judge from the text.\n"
    + "\n".join(f"- {k}: {v}" for k, v in CRITERIA.items())
    + '\nReturn only JSON: {"scores": {"target_rationale": n, "traceability": n, "calibration": n, "prior_art": n, '
      '"usefulness": n}, "notes": "<=60 words"}.'
)
PAIR_SYS = (
    "You are an expert reviewer of drug-target selection reports written for a protein-design team. Two anonymous "
    "reports answer the same request. Decide which is better overall on: " + "; ".join(CRITERIA) + ". Do not reward "
    "length. Judge from the text. Return only JSON: "
    '{"winner": "A" or "B" or "tie", "reason": "<=50 words"}.'
)


def run_items(task, judge, items, max_usd, dry, limit, think=True):
    """items: [(key, system, user, max_tokens, meta)] -> store rows."""
    store = C.Store(task, judge)
    todo = [it for it in items if not store.has(it[0])]
    if limit:
        todo = todo[:limit]
    chars = sum(len(it[1]) + len(it[2]) for it in todo)
    prov, model = C.JUDGES[judge]
    est = C._usd(model, int(chars / 3.6), 350 * len(todo))
    print(f"{task}/{judge}: {len(items)} items, {sum(store.has(it[0]) for it in items)} done, {len(todo)} to run; "
          f"~{chars / 3.6 / 1e3:.0f}k input tokens, estimate ${est:.2f}")
    if dry:
        return
    spent = store.spent()
    for key, system, user, max_tokens, meta in todo:
        if spent >= max_usd:
            print(f"  stopping: ${spent:.2f} >= --max-usd {max_usd}")
            break
        row = {"key": key, **meta, "judge": judge, "model": model}
        try:
            res = C.call_judge(judge, system, user, max_tokens, think)
            row.update(usd=res["usd"], input_tokens=res["input_tokens"], output_tokens=res["output_tokens"],
                       parsed=C.parse_json(res["text"]), raw=res["text"][:1500])
            if row["parsed"] is None:
                row["error"] = "unparseable"
        except C.Refused as exc:
            row.update(refused=True, refusal=str(exc), usd=0.0)
        except Exception as exc:  # noqa: BLE001 - recorded; the item stays retryable only by deleting the row
            row.update(error=f"{type(exc).__name__}: {str(exc)[:200]}", usd=0.0)
            print(f"  {key}: ERROR {row['error']}")
            if "SSL" in row["error"] or "401" in row["error"] or "403" in row["error"]:
                print("  setup problem, stopping before more calls")
                break
            continue
        store.add(row)
        spent += row["usd"]
    print(f"  spent on this task/judge so far: ${store.spent():.2f}")


def cmd_support(a):
    excluded = C.prompt_dois()
    items, all_dois = [], set()
    plan = []
    for prompt, rep, arm, _m in C.cells():
        bid = C.blind_id(prompt, rep, arm)
        for i, cl in enumerate(C.sample_claims(prompt, rep, arm)):
            plan.append((bid, i, cl))
            all_dois |= set(cl["dois"][:3])
    ev = C.fetch_evidence(sorted(all_dois))   # free: Europe PMC + local fingerprints
    n_na = 0
    for bid, i, cl in plan:
        parts = []
        for d in cl["dois"][:3]:
            e = ev.get(d) or {}
            if not (e.get("abstract") or e.get("fingerprint")):
                continue
            parts.append(f"PAPER {d}\nTitle: {e.get('title', '')}\nAbstract: {e.get('abstract') or '(none available)'}"
                         + (f"\nMachine-made summary: {e['fingerprint']}" if e.get("fingerprint") else ""))
        if not parts:
            n_na += 1
            continue
        user = f"CLAIM LINE:\n{cl['line']}\n\nEVIDENCE:\n\n" + "\n\n".join(parts)
        items.append((f"{bid}:{i}", SUPPORT_SYS, user, 300, {"blind": bid, "claim": i, "dois": cl["dois"][:3]}))
    print(f"{len(plan)} sampled claims; {n_na} have no retrievable evidence and are counted as cannot_assess without a call")
    run_items("support", a.judge, items, a.max_usd, a.dry_run, a.limit, think=False)


def cmd_rubric(a):
    cs = C.cells()
    random.Random(C.SALT).shuffle(cs)
    items = [(C.blind_id(p, r, arm), RUBRIC_SYS, f"REPORT:\n\n{C.report_text(p, r, arm)}", 500,
              {"blind": C.blind_id(p, r, arm)}) for p, r, arm, _m in cs]
    run_items("rubric", a.judge, items, a.max_usd, a.dry_run, a.limit)


def matched_pairs(baseline="blank"):
    by = {(p, r, arm) for p, r, arm, _m in C.cells()}
    return [(p, r) for p, r in sorted({(p, r) for p, r, _a in by}) if (p, r, "live") in by and (p, r, baseline) in by]


def cmd_pairwise(a):
    items = []
    for p, r in matched_pairs(a.baseline):
        for order in ("live_first", "base_first"):
            first, second = ("live", a.baseline) if order == "live_first" else (a.baseline, "live")
            user = (f"REPORT A:\n\n{C.report_text(p, r, first)}\n\n=====\n\nREPORT B:\n\n{C.report_text(p, r, second)}")
            items.append((f"{p}_{r}:{order}", PAIR_SYS, user, 300,
                          {"prompt": p, "rep": r, "order": order, "A": first, "B": second}))
    run_items(f"pairwise_{a.baseline}", a.judge, items, a.max_usd, a.dry_run, a.limit)


def cmd_pairwise2(a):
    """Pairwise between ANY two arms, both presentation orders: `--a wildcard2 --b live`."""
    by = {(p, r, arm) for p, r, arm, _m in C.cells()}
    pairs = [(p, r) for p, r in sorted({(p, r) for p, r, _x in by}) if (p, r, a.a) in by and (p, r, a.b) in by]
    items = []
    for p, r in pairs:
        for order in ("a_first", "b_first"):
            first, second = (a.a, a.b) if order == "a_first" else (a.b, a.a)
            user = f"REPORT A:\n\n{C.report_text(p, r, first)}\n\n=====\n\nREPORT B:\n\n{C.report_text(p, r, second)}"
            items.append((f"{p}_{r}:{order}", PAIR_SYS, user, 300, {"prompt": p, "rep": r, "order": order, "A": first, "B": second}))
    run_items(f"pair_{a.a}_vs_{a.b}", a.judge, items, a.max_usd, a.dry_run, a.limit)


def cmd_human(a):
    pairs = matched_pairs("blank")
    rng = random.Random(C.SALT + ":human")
    niche = [x for x in pairs if x[0] in ("sting", "alt", "tau")]
    obvious = [x for x in pairs if x[0] in ("kras", "ra")]
    pick = rng.sample(niche, min(3, len(niche))) + rng.sample(obvious, min(3, len(obvious)))
    key, md = [], ["# Phase 2 human check\n", "For each pair, score each report 1-5 on: " + "; ".join(CRITERIA) + f". {ANCHORS}",
                   "Then say which is better overall (A, B or tie). Do not look at `human_key.json` until you are done.\n"]
    for n, (p, r) in enumerate(pick, 1):
        flip = rng.random() < 0.5
        a_arm, b_arm = ("blank", "live") if flip else ("live", "blank")
        key.append({"pair": n, "prompt": p, "rep": r, "A": a_arm, "B": b_arm})
        md += [f"\n---\n\n## Pair {n}\n", f"### Report A\n\n{C.report_text(p, r, a_arm)}\n", f"### Report B\n\n{C.report_text(p, r, b_arm)}\n",
               "| | rationale | traceability | calibration | prior art | usefulness |\n|---|---|---|---|---|---|\n| A | | | | | |\n| B | | | | | |\n\nOverall: "]
    (C.OUT / "human_packet.md").write_text("\n".join(md), encoding="utf-8")
    (C.OUT / "human_key.json").write_text(json.dumps(key, indent=1))
    print(f"wrote {C.OUT / 'human_packet.md'} ({len(pick)} pairs) and human_key.json")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("task", choices=["support", "rubric", "pairwise", "pairwise2", "human-packet"])
    ap.add_argument("--judge", choices=list(C.JUDGES), default="claude")
    ap.add_argument("--baseline", choices=["blank", "notools"], default="blank", help="pairwise: live vs this arm")
    ap.add_argument("--a", default=None, help="pairwise2: first arm")
    ap.add_argument("--b", default=None, help="pairwise2: second arm")
    ap.add_argument("--max-usd", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=0, help="run at most N items (for a smoke test)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    {"support": cmd_support, "rubric": cmd_rubric, "pairwise": cmd_pairwise, "pairwise2": cmd_pairwise2, "human-packet": cmd_human}[a.task](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
