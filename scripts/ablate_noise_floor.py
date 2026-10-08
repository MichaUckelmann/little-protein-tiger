#!/usr/bin/env python3
"""Run-to-run noise floor for the pathway stage: how much does an UNCHANGED run move?

`ablate_corpus.py` compares live / blank / decoy once each. Whether a difference
between arms is the corpus or just a second draw from a stochastic model cannot
be told from one run per arm. This repeats the `live` arm — same prompt, same
tools, same corpus, same model — so that "blank changed the top pick" can be
read against "live changed the top pick by itself".

    .venv/bin/python scripts/ablate_noise_floor.py --dry-run
    python scripts/ablate_noise_floor.py --repeats 2 --max-usd 5
    python scripts/ablate_noise_floor.py --report

Arms are named `live2`, `live3`, ... and written beside the originals in
`outputs/ablation/`; cells that already exist are skipped. The original `live`
cells are NOT a clean baseline: five of six ran 2026-09-09, before the
2026-09-13 prompt changes (see TARGET_SELECTION_AUDIT.md), so the headline
number here is between the repeats, which share a prompt and a corpus. The
original `live` is reported separately as drift across those changes.

Run it with the project interpreter (`.venv`, Python 3.12). Python 3.13 rejects the
corporate proxy's re-signed certificate ("Missing Authority Key Identifier") and
every Gemini call fails before billing; a failed cell is written to disk as a
result, so delete `outputs/ablation/*__live[0-9].*` if that happens.

Reuses `ablate_corpus.cell()` and its parsers; deliberately does NOT call its
`summarise()`, which rewrites `docs/showcase/facts/ablation.json`, a tracked file
the launch deck reads.
"""
from __future__ import annotations

import argparse
import itertools
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import ablate_corpus as ac  # noqa: E402

_orig_patch = ac.patch
# `patch` treats any arm that is not "live" as a decoy; a repeat must be left alone.
ac.patch = lambda runner, arm, cache: None if arm.startswith("live") else _orig_patch(runner, arm, cache)


def _load(slug: str, arm: str) -> dict | None:
    p = ac.OUT / f"{slug}__{arm}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _jac(a, b):
    sa, sb = set(a or []), set(b or [])
    return round(len(sa & sb) / len(sa | sb), 2) if sa | sb else None


def report(arms: list[str]) -> None:
    print(f"\n  {'query':10s} " + "".join(f"{a:>16s}" for a in ["live(old)"] + arms) + "   top-pick agreement")
    pair_same = pair_n = 0
    jacs, doi_ratio = [], []
    out = []
    for slug, _label, _q in ac.QUERIES:
        cells = {a: _load(slug, a) for a in ["live"] + arms}
        cols = []
        for a in ["live"] + arms:
            c = cells[a]
            cols.append("-" if not c else ("ERR" if c.get("error") else "/".join(c.get("primary_pair") or ["?"])[:15]))
        print(f"  {slug:10s} " + "".join(f"{x:>16s}" for x in cols), end="")
        fresh = [cells[a] for a in arms if cells[a] and not cells[a].get("error")]
        row = {"slug": slug, "primary": {a: (cells[a] or {}).get("primary") for a in cells}}
        verdicts = []
        for x, y in itertools.combinations(fresh, 2):
            same = set(x["primary_pair"]) == set(y["primary_pair"])
            pair_same += same; pair_n += 1
            jacs.append(_jac(x["genes"], y["genes"]))
            if x["n_dois"] and y["n_dois"]:
                doi_ratio.append(min(x["n_dois"], y["n_dois"]) / max(x["n_dois"], y["n_dois"]))
            verdicts.append("same" if same else "DIFF")
        print("   " + (",".join(verdicts) or "-"))
        out.append(row)
    jacs = [j for j in jacs if j is not None]
    print(f"\n  between fresh repeats: top pick identical in {pair_same}/{pair_n} pairs")
    if jacs:
        print(f"  gene-set Jaccard between repeats: mean {sum(jacs)/len(jacs):.2f}  (min {min(jacs):.2f}, max {max(jacs):.2f})")
    if doi_ratio:
        print(f"  DOI-count ratio (smaller/larger): mean {sum(doi_ratio)/len(doi_ratio):.2f}")
    drift_same = drift_n = 0
    for slug, _l, _q in ac.QUERIES:
        old = _load(slug, "live")
        for a in arms:
            c = _load(slug, a)
            if old and c and not c.get("error") and not old.get("error"):
                drift_n += 1
                drift_same += set(old["primary_pair"]) == set(c["primary_pair"])
    print(f"  fresh vs ORIGINAL live (different prompt/corpus): identical in {drift_same}/{drift_n}")
    total = sum((_load(s, a) or {}).get("usd", 0) for s, _l, _q in ac.QUERIES for a in arms)
    print(f"  spend on these repeats: ${total:.2f}")
    (ac.OUT / "noise_floor.json").write_text(json.dumps({
        "arms": arms, "pairs": pair_n, "top_pick_identical": pair_same,
        "gene_jaccard": jacs, "doi_ratio": doi_ratio,
        "vs_original_live": [drift_same, drift_n], "usd": round(total, 4), "rows": out}, indent=2) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--only", action="append", metavar="SLUG")
    ap.add_argument("--max-usd", type=float, default=5.0, help="stop starting new cells past this")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    arms = [f"live{i}" for i in range(2, 2 + args.repeats)]
    queries = [q for q in ac.QUERIES if not args.only or q[0] in args.only]
    if args.dry_run:
        todo = [(s, a) for s, _l, _q in queries for a in arms if not (ac.OUT / f"{s}__{a}.json").is_file()]
        print(f"{len(todo)} cells to run ({len(arms)} repeats x {len(queries)} queries); "
              f"about $0.28 each, ~${0.28 * len(todo):.1f} total")
        return 0
    if not args.report:
        import yaml
        from src.env_config import load_env   # CA bundle for the corporate TLS proxy
        load_env()
        config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
        ac.OUT.mkdir(parents=True, exist_ok=True)
        spent = sum((_load(s, a) or {}).get("usd", 0) for s, _l, _q in ac.QUERIES for a in arms)
        for slug, _label, query in queries:
            for arm in arms:
                if spent >= args.max_usd:
                    print(f"  stopping: spent ${spent:.2f} >= --max-usd {args.max_usd}")
                    break
                spent += ac.cell(slug, arm, query, config, force=False).get("usd", 0) or 0
    report(arms)
    return 0


if __name__ == "__main__":
    sys.exit(main())
