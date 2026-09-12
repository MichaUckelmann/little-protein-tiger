"""End-to-end PPI run on the LEGACY BoltzGen stages, sized to be affordable.

A driver rather than a CLI invocation, for two reasons that are properties of
the code rather than preferences:

* **`run_pipeline.py` cannot size these stages.** `--n-batches` feeds the
  binder track; the legacy PPI BoltzGen path reads `design.pilot.num_designs`
  and `design.production.num_designs` from `config.yaml` and nothing else, and
  there is no `--config` flag. The shipped 1,000 / 20,000 are sized for a small
  complex: on YAP1/TEAD1 (~285 tokens, six-step protein protocol, 57 s/design
  by `boltzgen_runner.seconds_per_design`) they are 15.8 and 316 GPU-h — about
  thirteen days for one validation run.
* **`--stop-after` would not have stopped it.** That flag is honoured only on
  the binder track; the legacy PPI path runs design -> execution -> analysis ->
  summary regardless.

So this run is deliberately small. It is a PATH test, not a campaign: the point
is that pathway -> literature -> structure -> design -> execution -> analysis ->
summary all still work together, LLM stages included, with the shipped
thresholds now actually gating (every historical e2e driver overrode all four,
which is how they went unexercised).

Note what this does NOT test: the new `boltzgen_spec` / `boltzgen_runner` /
per-modality gate. Those are reached only through the BINDER track
(`--workflow binder --design-engine boltzgen`), because the PPI dispatch still
sends BoltzGen to the legacy stages. Routing PPI through the bridge to the new
backend is a separate change.

Usage:
    python scripts/e2e_ppi_boltzgen.py [--designs 200] [--budget-usd 5]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

#: The documented reference query — the same one `projects/mesothelioma_showcase`
#: ran unattended for $0.79, so the upstream stages have a known-good outcome
#: (YAP1/TEAD1 on 3KYS) to compare against.
PROMPT = "Design cancer therapeutics to target key nodes in mesothelioma."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--designs", type=int, default=200,
                    help="production design count (default 200 — a path test)")
    ap.add_argument("--pilot-designs", type=int, default=24,
                    help="pilot design count (default 24)")
    ap.add_argument("--budget-usd", type=float, default=5.0)
    ap.add_argument("--project", default="e2e_ppi_boltzgen")
    ap.add_argument("--provider", default="gemini")
    args = ap.parse_args(argv)

    import yaml

    from src.env_config import load_env
    from src.pipeline_runner import (
        PipelineError, PipelinePausedError, PipelineRunner,
    )

    load_env()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

    # Size the two legacy stages. Everything else — including all four
    # `design.thresholds` — is left at the SHIPPED values on purpose: the
    # drivers that overrode them are why they had never gated a real run.
    cfg["design"]["pilot"]["num_designs"] = args.pilot_designs
    cfg["design"]["pilot"]["budget"] = max(4, args.pilot_designs // 3)
    cfg["design"]["production"]["num_designs"] = args.designs
    cfg["design"]["production"]["budget"] = max(10, args.designs // 10)

    from src.boltzgen_runner import seconds_per_design

    est = ((args.pilot_designs + args.designs)
           * seconds_per_design(285, "protein-anything") / 3600)
    logger.info(
        f"PPI e2e on the legacy BoltzGen stages: pilot {args.pilot_designs}, "
        f"production {args.designs} — roughly {est:.1f} GPU-h at YAP1/TEAD1 "
        f"size, against {(1000 + 20000) * seconds_per_design(285, 'protein-anything') / 3600:.0f} "
        f"GPU-h for the shipped counts.")

    runner = PipelineRunner(
        config=cfg, provider=args.provider, project=args.project,
        design_engine="boltzgen", budget_usd=args.budget_usd,
    )
    t0 = time.time()
    try:
        result = runner.run(query=PROMPT)
    except PipelinePausedError as exc:
        logger.warning(f"paused at {exc.pause_point}: {exc.payload}")
        return 3
    except PipelineError as exc:
        logger.error(f"failed after {(time.time() - t0) / 60:.0f} min: {exc}")
        return 1

    logger.info(f"finished in {(time.time() - t0) / 60:.0f} min — "
                f"{result.go_recommendation}")
    for stage, path in (result.stage_files or {}).items():
        logger.info(f"  {stage:14s} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
