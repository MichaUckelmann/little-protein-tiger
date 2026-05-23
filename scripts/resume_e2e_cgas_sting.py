#!/usr/bin/env python
"""Resume the cGAS-STING e2e from stage 3 (design).

Stages 0-2 produced sensible output in the first run (the bug was a Boltz vs
BoltzGen YAML schema mix-up in stage 3, plus a missing biopython in the
structure-tools env). This script reconstructs PipelineResult from the
existing 00/01/02 reports and drives stages 3-6 directly. Captures traces
under the same run dir.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.pipeline_runner import (
    PipelineRunner,
    PipelineResult,
    PipelineError,
    PipelinePausedError,
)


def main() -> int:
    run_dir = _ROOT / "outputs" / "e2e_cgas_sting"
    assert run_dir.exists(), f"run dir missing: {run_dir}"

    # Wipe stage-3+ artifacts so the resume regenerates from scratch.
    for p in [
        run_dir / "03_design_inputs",
        run_dir / "03_design_report.md",
        run_dir / "04_execution.md",
        run_dir / "04_execution_outputs",
        run_dir / "05_analysis.md",
        run_dir / "05_metrics_enriched.csv",
        run_dir / "05_ranking",
        run_dir / "06_summary.md",
        run_dir / "06_top_k.fasta",
    ]:
        if p.is_dir():
            import shutil
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text())
    cfg["design"]["pilot"]["num_designs"] = 50
    cfg["design"]["pilot"]["budget"] = 10
    cfg["design"]["production"]["num_designs"] = 100
    cfg["design"]["production"]["budget"] = 20
    cfg["design"]["thresholds"]["iptm_min"] = 0.10
    cfg["design"]["thresholds"]["ipae_max"] = 25.0
    cfg["design"]["thresholds"]["hotspot_sasa_delta_min"] = 0.0
    cfg["design"]["thresholds"]["require_boltzgen_pass"] = False

    runner = PipelineRunner(
        config=cfg,
        provider="claude",
        output_dir=run_dir,
        max_iter=30,
        max_tokens=120_000,
        capture_traces=True,
    )

    # Reconstruct result state. Pathway and literature handoffs come from
    # parsing their respective reports' PIPELINE HANDOFF blocks. structure
    # output is on disk so context_files works.
    pathway_handoff = runner._parse_handoff(
        (run_dir / "00_pathway.md").read_text(encoding="utf-8")
    )
    literature_handoff = runner._parse_handoff(
        (run_dir / "01_literature.md").read_text(encoding="utf-8")
    )
    structure_text = (run_dir / "02_structure.md").read_text(encoding="utf-8")
    structure_handoff = runner._parse_handoff(structure_text)

    result = PipelineResult(
        run_dir=run_dir,
        target_complex=literature_handoff.get("target_complex") or pathway_handoff.get("target_complex"),
        pdb_id=pathway_handoff.get("pdb_id"),
        pathway_handoff=pathway_handoff,
        literature_handoff=literature_handoff,
        stage_files={
            "pathway":    run_dir / "00_pathway.md",
            "literature": run_dir / "01_literature.md",
            "structure":  run_dir / "02_structure.md",
        },
        stages_completed=["pathway", "literature", "structure"],
    )
    # hotspot_residues_json is parsed from the structure report.
    hotspots_json = runner._parse_hotspot_residues(structure_text, structure_handoff)
    if hotspots_json:
        result.hotspot_residues_json = hotspots_json
        print(f"[setup] parsed {len(__import__('json').loads(hotspots_json).get('residues', []))} hotspot residues")
    else:
        print("[setup] WARNING: no hotspot_residues_json parsed from 02_structure.md")

    print(f"[setup] target_complex={result.target_complex}")
    print(f"[setup] pdb_id={result.pdb_id}")
    print(f"[setup] modality={literature_handoff.get('modality')}")
    print(f"[setup] design_intent={literature_handoff.get('design_intent')}")
    print()

    # Drive stages 3 → 6 directly (don't go through run() — it would re-init
    # PipelineResult and lose our reconstructed state).
    t0 = time.time()
    try:
        # Stage 3: design — produces YAML. This is where the first run failed.
        ctx = [run_dir / "02_structure.md", run_dir / "01_literature.md"]
        ctx = [c for c in ctx if c.exists()]
        runner._stage_design(literature_handoff, run_dir, result, ctx)
        print(f"[stage 3] design YAML written → {[f.name for f in result.design_files]}")

        # Stage 4: execution — boltzgen check happens here first. If the YAML
        # is still bad, we'll see it now and abort fast.
        runner._stage_execution(literature_handoff, run_dir, result, force_production=False)
        print(f"[stage 4] execution complete")

        # Stage 5: analysis — pyrosetta SASA enrichment + ranking.
        runner._stage_analysis(run_dir, result)
        print(f"[stage 5] analysis complete")

        # Stage 6: summary — Haiku candidate review.
        runner._stage_summary(literature_handoff, run_dir, result)
        print(f"[stage 6] summary complete")
    except PipelinePausedError as exc:
        print(f"\n[pause] {exc.pause_point}: {exc.payload}")
        return 1
    except PipelineError as exc:
        print(f"\n[error] {exc}")
        return 1
    except Exception as exc:
        print(f"\n[unexpected] {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    elapsed = time.time() - t0
    print()
    print("=" * 72)
    print(f"STAGES 3-6 PASS  ({elapsed:.0f}s)")
    print(f"  go_recommendation: {result.go_recommendation}")
    print(f"  go_rationale: {(result.go_rationale or '')[:200]}")
    print()
    print("Artifacts (excluding boltzgen intermediates):")
    skip = {"intermediate_designs", "intermediate_designs_inverse_folded", "config", "previous-config-1", "previous-config-2"}
    for f in sorted(run_dir.rglob("*")):
        if not f.is_file() or any(p in skip for p in f.parts):
            continue
        rel = f.relative_to(run_dir)
        print(f"  {rel}  ({f.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
