#!/usr/bin/env python3
"""Phase 2: does corpus access change the quality or reliability of target-selection reports?

Five varied prompts x 3 repeats x 2 arms (corpus `live` vs corpus `blank`), the
`pathway-expert` stage, same model and prompt. Scoring and judging are
specified in docs/phase2_corpus_eval.md, which was written BEFORE any output
existed; this script only generates.

    .venv/bin/python scripts/phase2_corpus_eval.py --dry-run
    .venv/bin/python scripts/phase2_corpus_eval.py --max-usd 6
    .venv/bin/python scripts/phase2_corpus_eval.py --report     # cost / call counts only

Cells land in `outputs/phase2/<prompt>_r<k>__<arm>.{md,json,trace}` and are
cached. A cell that FAILED is deleted rather than cached, because
`ablate_corpus.cell` writes failures to disk as if they were results; an SSL
failure on Python 3.13 once produced ten such cells. Run with the project
interpreter (`.venv`, Python 3.12) for the same reason.

The blank arm returns "No results found" from every corpus-derived tool
(see ablate_corpus.CORPUS_TOOLS); DepMap and RCSB tools stay live in both arms.
Run order is shuffled with a fixed seed, so a drift in the model over the
session cannot line up with one arm.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import ablate_corpus as ac  # noqa: E402

OUT = _ROOT / "outputs" / "phase2"
ac.OUT = OUT

# --- the no-tools baseline arm -------------------------------------------------
import phase2_baseline  # noqa: E402
import src.skill_runner as _sr  # noqa: E402

_orig_patch = ac.patch
_orig_filter = _sr._filter_tools
_sr._filter_tools = lambda defs, skill: [] if str(skill).endswith("-notools") else _orig_filter(defs, skill)


def _patch(runner, arm, cache):
    if arm != "notools":
        return _orig_patch(runner, arm, cache)
    skill = (_ROOT / "skills" / "pathway-expert" / "SKILL.md").read_text(encoding="utf-8")
    runner.system_prompt = phase2_baseline.build(skill) + _sr._OUTPUT_FORMAT_RULE
    runner.skill_name = "pathway-expert-notools"      # _filter_tools above returns no tools for this name


ac.patch = _patch
ARMS = ("live", "blank")          # default matrix; `--arms notools` adds the no-tools baseline
ALL_ARMS = ("live", "blank", "notools")
REPEATS = 3

# stratum: niche = a specific system, obvious = a canonical target class any
# well-read model knows. Counts are curated papers whose resolved identifiers
# include the gene (14,514 fingerprints, measured 2026-10-07).
PROMPTS = [
    ("sting", "niche, dense",
     "Design inhibitors of cGAS-STING signalling for autoinflammatory disease.",
     "STING1 332, CGAS 292, TBK1 215"),
    ("alt", "niche, dense (corpus core theme)",
     "Design binders that disrupt histone chaperone complexes that sustain alternative lengthening of telomeres in cancer.",
     "ATRX 97, DAXX 87, HIRA 69, CHAF1A 63"),
    ("tau", "niche, moderate",
     "Design protein therapeutics that modulate Hsp70 and Hsp90 co-chaperone interactions in tau-driven neurodegeneration.",
     "HSP90AA1 90, HSPA1A 69, MAPT 72, STUB1 26"),
    ("kras", "obvious",
     "Design cancer therapeutics targeting oncogenic KRAS signalling in lung adenocarcinoma.",
     "KRAS 331, RAF1 111, PIK3CA 157"),
    ("ra", "obvious",
     "Design inhibitors of pro-inflammatory cytokine signalling in rheumatoid arthritis.",
     "TNF 189, IL6 182"),
    # Added 2026-10-08 for the wildcard-expert evaluation (user-specified wording). NOT part of the
    # Phase 2 analysis: phase2_common.PHASE2_PROMPTS keeps that to the five above.
    ("epi", "broad, dense",
     "Design protein therapeutics to target epigenetic modifiers for cancer therapy",
     "EZH2 310, SMARCA4 268, DNMT3A 265, BRD4 248, EP300 244"),
    ("mash", "broad, canonical genes sparse, adjacent biology dense",
     "Design protein therapeutics that target key nodes in metabolic liver disease (MASH/MASLD)",
     "PNPLA3 8, THRB 11, HSD17B13 5, FGF21 7; but SCAP 115, SREBF1 129, YAP1 542, TEAD1 536"),
]


def matrix(seed: int = 20261007, arms=ARMS, only=None, reps=REPEATS):
    cells = [(slug, f"r{k}", arm, q) for slug, _s, q, _c in PROMPTS if not only or slug in only
             for k in range(1, reps + 1) for arm in arms]
    random.Random(seed).shuffle(cells)
    return cells


def preflight() -> None:
    """Fail before any billing if TLS is broken or the blanking audit finds a leak."""
    import audit_blanking
    if audit_blanking.main() != 0:
        sys.exit("blanking audit failed; not spending anything. See outputs/phase2/blanking_audit.json")
    import requests
    from src.env_config import load_env
    load_env()
    try:
        requests.get("https://generativelanguage.googleapis.com/v1beta/models", timeout=15)
    except requests.exceptions.SSLError as exc:
        sys.exit(f"TLS preflight failed ({str(exc)[:120]}). Use .venv/bin/python (3.12); "
                 "Python 3.13 rejects the corporate proxy's certificate.")


def verify_cell(name: str, arm: str) -> dict:
    """What the tools actually sent back, read from the cell's own trace.

    Recorded in the cell's metadata for BOTH arms. For `blank`, every response
    from a blanked tool must be exactly ablate_corpus.NOTHING and no tool may
    have been called that is in neither CORPUS_TOOLS nor LIVE_BY_DESIGN; any
    violation is returned as `violations` and the caller discards the cell.
    A coverage statement in a report can then be checked against these counts.
    """
    tr = json.loads((OUT / f"{name}__{arm}.trace" / "trace_raw.json").read_text(encoding="utf-8"))
    chars, calls, nonblank_corpus, unknown = {}, {}, [], set()
    for msg in tr:
        for part in msg.get("parts", []):
            if "functionCall" in part:
                t = part["functionCall"]["name"]
                calls[t] = calls.get(t, 0) + 1
                if t not in ac.CORPUS_TOOLS and t not in ac.LIVE_BY_DESIGN:
                    unknown.add(t)
            fr = part.get("functionResponse")
            if fr:
                t = fr["name"]
                body = (fr.get("response") or {}).get("result", "")
                chars[t] = chars.get(t, 0) + len(body)
                if arm == "blank" and t in ac.CORPUS_TOOLS and body != ac.NOTHING:
                    nonblank_corpus.append(t)
    return {"tool_calls": calls, "response_chars_by_tool": chars,
            "violations": ([f"no-tools baseline made tool calls: {calls}"] if arm == "notools" and calls else []) + ([f"blanked tool returned content: {sorted(set(nonblank_corpus))}"] if nonblank_corpus else [])
                          + ([f"unclassified tools called: {sorted(unknown)}"] if unknown else [])}


def load(slug, rep, arm):
    p = OUT / f"{slug}_{rep}__{arm}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def report() -> None:
    print(f"\n  {'prompt':6} {'rep':4} {'arm':6} {'$':>6} {'calls':>6} {'corpus':>7} {'DOIs':>5}  primary")
    tot = 0.0
    for slug, rep, arm, _q in sorted(matrix(arms=ALL_ARMS), key=lambda c: (c[0], c[1], c[2])):
        m = load(slug, rep, arm)
        if not m:
            continue
        tot += m.get("usd") or 0
        print(f"  {slug:6} {rep:4} {arm:6} {m['usd']:6.2f} {m.get('n_tool_calls', 0):6} "
              f"{m.get('n_corpus_calls', 0):7} {m.get('n_dois', 0):5}  {m.get('primary', '')[:40]}")
    print(f"\n  spend: ${tot:.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-usd", type=float, default=6.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--arms", nargs="+", choices=ALL_ARMS, default=list(ARMS))
    ap.add_argument("--only", nargs="+", help="prompt slugs")
    ap.add_argument("--reps", type=int, default=REPEATS)
    args = ap.parse_args()
    cells = matrix(arms=tuple(args.arms), only=args.only, reps=args.reps)
    if args.dry_run:
        for slug, s, q, c in PROMPTS:
            print(f"  {slug:5} [{s}]  {c}\n        {q}")
        todo = [c for c in cells if not (OUT / f"{c[0]}_{c[1]}__{c[2]}.json").is_file()]
        print(f"\n{len(todo)} cells to run of {len(cells)}; about $0.13 each, ~${0.13 * len(todo):.1f}")
        return 0
    if args.report:
        report()
        return 0

    import yaml
    preflight()
    config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    spent, failures = 0.0, 0
    for slug, rep, arm, q in cells:
        if spent >= args.max_usd:
            print(f"stopping: spent ${spent:.2f} >= --max-usd {args.max_usd}")
            break
        name = f"{slug}_{rep}"
        meta = ac.cell(name, arm, q, config, force=False)
        spent += meta.get("usd") or 0
        if not meta.get("error") and "response_chars_by_tool" not in meta:
            v = verify_cell(name, arm)
            if v["violations"]:
                print(f"  {name}/{arm}: BLANKING VIOLATION {v['violations']} - cell discarded, stopping")
                for ext in ("md", "json", "trace"):
                    p = OUT / f"{name}__{arm}.{ext}"
                    (p.unlink(missing_ok=True) if p.is_file() else __import__("shutil").rmtree(p, ignore_errors=True))
                break
            meta.update(v)
            meta["blanked_tools"] = sorted(ac.CORPUS_TOOLS) if arm == "blank" else []
            (OUT / f"{name}__{arm}.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        if meta.get("error"):
            failures += 1
            for ext in ("md", "json", "trace"):
                (OUT / f"{name}__{arm}.{ext}").unlink(missing_ok=True)
            if failures >= 2:
                print("two failed cells; stopping so nothing more is spent on a broken setup")
                break
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
