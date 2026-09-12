"""Foundry-track regression baseline — the net under the BoltzGen backend work.

Run it BEFORE and AFTER any change that touches shared code
(`binder_ranking`, `campaign_calibration`, `pipeline_runner`) and diff the two
JSON outputs. It is the only thing that has caught -- or could catch -- a
BoltzGen change silently moving a foundry campaign's results.

    python scripts/foundry_regression_baseline.py > /tmp/before.json
    # ... make the change ...
    python scripts/foundry_regression_baseline.py > /tmp/after.json
    diff <(python -m json.tool /tmp/before.json) <(python -m json.tool /tmp/after.json)

Takes ~90 s over the 13 campaigns in `projects/`. Needs no GPU and no network.

Two invariants, and they are NOT the same:

* `hashes` must ALWAYS be identical. A difference means something wrote into a
  campaign directory, which nothing in this work is allowed to do.
* `rederive` may legitimately change when ranking behaviour changes on
  purpose -- `z_clip` moved all 13. What must NOT change in that case is
  `n_survivors`: the gate is separate from the ranking, so a survivor-count
  difference is a real regression even when a top-K reshuffle is intended.


Two independent invariants, both re-runnable:

1. ARTIFACT HASHES — SHA256 of every foundry scoring artifact on disk. Proves
   nothing wrote into a campaign directory.
2. RE-DERIVATION — re-run `binder_ranking.rank_designs` over each campaign's
   own frozen `refold_scores.csv` and record the funnel + top-K ids.

Invariant 2 is deliberately compared against ITSELF across runs, not against
the frozen ranked.csv: those were written under whatever config.yaml held at
the time, so a mismatch there means config drift, not a regression. Comparing
before-change to after-change isolates the change.
"""
from __future__ import annotations
import hashlib, json, sys, io, contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NAMES = ("refold_scores.csv", "ranked.csv", "top_k.csv", "filter_stats.txt",
         "calibration.json", "rosetta_metrics.csv")


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    import yaml
    from src.binder_ranking import rank_designs, read_scores

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    br = (cfg.get("design") or {}).get("binder_ranking") or {}

    out: dict = {"hashes": {}, "rederive": {}}

    for name in NAMES:
        for p in sorted(ROOT.glob(f"projects/*/runs/*/binder/*/{name}")):
            out["hashes"][str(p.relative_to(ROOT))] = sha(p)

    for scores in sorted(ROOT.glob("projects/*/runs/*/binder/*/refold_scores.csv")):
        key = str(scores.relative_to(ROOT))
        try:
            rows = read_scores(scores)
            # Silence loguru's per-call chatter; we only want the numbers.
            with contextlib.redirect_stderr(io.StringIO()):
                res = rank_designs(
                    rows,
                    thresholds=br.get("thresholds"),
                    weights=br.get("weights"),
                    mmr=br.get("mmr"),
                    top_k=int(br.get("top_k", 20)),
                    max_per_backbone=int(br.get("max_per_backbone", 1)),
                )
            out["rederive"][key] = {
                "n_input": res.filter_stats.n_input,
                "n_survivors": res.filter_stats.n_survivors,
                "dropped": dict(sorted(res.filter_stats.dropped.items())),
                "n_backbones": res.n_backbones,
                "composite_columns": res.composite_columns,
                "top_k_names": [r.get("name") for r in res.top_k],
                "top_k_composite": [round(float(r["composite_score"]), 6)
                                    for r in res.top_k],
            }
        except Exception as exc:
            out["rederive"][key] = {"error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
