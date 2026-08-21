#!/usr/bin/env python3
"""
Mocked-LLM end-to-end for the binder track.

Stubs the three LLM stages and the GPU, and runs everything else for real:
target resolution, the interface hotspot parse, domain-aware trimming, RFD3 spec
emission + validation, scoring (including ipSAE), calibration, ranking and the
report/FASTA writers.

The fake RFD3/RF3 outputs are cloned from REAL structures, so biotite parses
them and the geometry columns mean something — a synthetic CIF would let a whole
class of parsing bug through.

    python scripts/test_e2e_binder.py [--keep]
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402
from loguru import logger  # noqa: E402

BCR = Path("/home/m.uckelmann_cbs-niob.local/data/BCR/outputs/production/CD79b")
STRUCT = _ROOT / "data" / "structures" / "7XQ8_ba1.cif"

TARGET_INTEL_MD = """# Target intel (stub)

### PIPELINE HANDOFF
- target_gene: CD79B
- target_uniprot: P40259
- pdb_id: 7XQ8
- assembly: ba1
- target_chain: B
- partner_chain: A
- partner_name: CD79A
- target_entity_description: B-cell antigen receptor complex-associated protein beta chain
- target_chain_length: 140
- interface_bsa_A2: 2735
- signalling_interface: yes
- downstream_effect: blocks CD79A/CD79B heterodimerisation and BCR assembly
- interface_rationale: the extracellular Ig interface is the assembly contact
- design_intent: disrupt
- modality: mini_protein
- binder_length_min: 68
- binder_length_max: 86
- keep_domain_hint: B42-145
- alternatives_json: []
- go_recommendation: GO
- go_rationale: solved complex, compact hydrophobic epitope
- structure_query: Analyse the CD79A/CD79B extracellular interface in 7XQ8.
"""

INTERFACE_MD = """# Interface (stub)

### MODEL-READY HOTSPOTS

| RES | auth_seq_id | label_seq_id | atoms |
|---|---|---|---|
| TRP | 76 | 35 | CG,CD1,NE1 |
| LEU | 77 | 36 | CG,CD1,CD2 |
| TRP | 78 | 37 | CD2,CE3,CZ3 |
| LEU | 89 | 48 | CG,CD1 |
| LEU | 91 | 50 | CD1,CD2,CG |
| VAL | 132 | 91 | CG1,CG2 |

### PIPELINE HANDOFF
- pdb_id: 7XQ8
- target_chain: B
- partner_chain: A
- design_intent: disrupt
- modality: mini_protein
"""

SUMMARY_MD = """# Design review (stub)

### PIPELINE HANDOFF
- go_recommendation: CONDITIONAL_GO
- go_rationale: stub review
"""

_STUBS = {
    "binder-target-intel": TARGET_INTEL_MD,
    "complex-structure-analysis": INTERFACE_MD,
    "design-analyst": SUMMARY_MD,
}


def _install_llm_stub(monkey: list) -> None:
    """Replace SkillRunner.run with canned markdown and a stamped usage."""
    from src.skill_runner import SkillRunner
    from src.token_budget import Usage

    original_run, original_usage = SkillRunner.run, SkillRunner.usage

    def fake_run(self, query, context_text=None, trace_path=None):
        if self.skill_name not in _STUBS:
            raise AssertionError(f"unexpected skill invoked: {self.skill_name}")
        logger.info(f"[stub] {self.skill_name} ({len(query):,} char query)")
        return _STUBS[self.skill_name]

    def fake_usage(self):
        return Usage(input_tokens=12_000, output_tokens=1_500,
                     cache_creation_tokens=8_000, cache_read_tokens=40_000)

    SkillRunner.run, SkillRunner.usage = fake_run, fake_usage
    monkey.append(lambda: setattr(SkillRunner, "run", original_run))
    monkey.append(lambda: setattr(SkillRunner, "usage", original_usage))


def _materialise_campaign(paths, n_designs: int, n_seq: int) -> None:
    """
    Fake an RFD3 + MPNN + RF3 campaign using real structures.

    Copies genuine CD79b designs and refolds so every geometry and confidence
    column is computed from real coordinates.
    """
    paths.mkdirs()
    designs = sorted(p for p in (BCR / "rfd3").iterdir()
                     if p.name.endswith(".cif.gz"))[:n_designs]
    for d in designs:
        shutil.copy(d, paths.rfd3_dir / d.name)
        side = d.with_name(d.name[: -len(".cif.gz")] + ".json")
        if side.exists():
            shutil.copy(side, paths.rfd3_dir / side.name)
        (paths.filtered_dir / d.name).symlink_to(paths.rfd3_dir / d.name)

    stems = {d.name[: -len(".cif.gz")] for d in designs}
    copied = 0
    for sub in sorted(p for p in (BCR / "rf3_out").iterdir() if p.is_dir()):
        base = sub.name
        stem = base.rsplit("_b", 1)[0]
        if stem not in stems:
            continue
        dest = paths.rf3_dir / base
        dest.mkdir(parents=True, exist_ok=True)
        for suffix in ("_model.cif", "_summary_confidences.json", "_confidences.json"):
            src = sub / f"{base}{suffix}"
            if src.exists():
                shutil.copy(src, dest / src.name)
        (paths.mpnn_dir / f"{base}.cif").write_bytes(b"")
        copied += 1
    logger.info(f"[stub] materialised {len(designs)} designs / {copied} refolds")


def _install_gpu_stub(monkey: list, n_designs: int) -> None:
    from src import pipeline_runner as pr

    original = pr.PipelineRunner._run_gpu_stage

    def fake_gpu(self, mode, spec_path, trim, dirs, result, *, attach,
                 n_batches=None):
        from src.foundry_runner import collect, plan_campaign, validate_spec

        paths = self._binder_paths(dirs, mode)
        # The REAL validator still runs: this is the check that would have
        # caught a bad spec before days of GPU.
        validate_spec(spec_path, kept_segments=trim.kept_segments)
        _materialise_campaign(paths, n_designs, 4)
        plan = plan_campaign(self._binder_cfg(), paths, mode=mode, n_batches=1)
        out = dirs["binder"] / self._BINDER_STAGE_FILES[mode]
        self._write_binder_report(out, f"{mode} (stub)", "stubbed GPU stage",
                                  collect(paths))
        self._record_stage(mode, "complete", out, stage=mode)
        result.stage_files[mode] = out
        result.stages_completed.append(mode)
        return {"paths": paths, "plan": plan, "summary": collect(paths)}

    pr.PipelineRunner._run_gpu_stage = fake_gpu
    monkey.append(lambda: setattr(pr.PipelineRunner, "_run_gpu_stage", original))


def run(tmp: Path, n_designs: int) -> int:
    from src.pipeline_runner import PipelineRunner
    from src.project import Project

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    project = Project.create("e2e-binder-test", query="stub", workflow="binder",
                             root=tmp)
    rnd = project.new_round(note="e2e")

    undo: list = []
    try:
        _install_llm_stub(undo)
        _install_gpu_stub(undo, n_designs)
        # Squeeze the residue budget so the trim actually has work to do; the
        # configured 220 would leave this 140-residue chain untouched.
        cfg["design"]["foundry"]["target_residue_budget"] = 110
        runner = PipelineRunner(cfg, project=project, round_id=rnd["run_id"],
                                workflow="binder", budget_usd=50.0)
        result = runner.run(query="design binders against CD79B",
                            target="CD79B")
    finally:
        for fn in undo:
            fn()

    run_dir = project.run_dir(rnd["run_id"])
    binder = run_dir / "binder"
    failures: list[str] = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    for stage in ("target_intel", "interface", "trim", "binder_spec",
                  "pilot", "calibration", "binder_scoring", "binder_summary"):
        f = binder / PipelineRunner._BINDER_STAGE_FILES[stage]
        check(f.exists(), f"missing stage report {f.name}")

    trim_map = json.loads((binder / "trim" / "trim_map.json").read_text())
    check(trim_map["identity_numbering"] is True,
          "trim renumbered — hotspot ids would no longer address the right residues")
    check(all(h["retained"] for h in trim_map["hotspots"]), "a hotspot was lost")
    check(trim_map["n_segments"] == 1, "trim is fragmented")

    specs = sorted((binder / "spec").glob("*.json"))
    check(bool(specs), "no RFD3 spec written")
    if specs:
        spec = json.loads(specs[0].read_text())
        entry = next(iter(spec.values()))
        check(entry["dialect"] == 2, "spec is not dialect 2")
        check(len(entry["select_hotspots"]) == 6,
              f"expected 6 hotspots, got {len(entry['select_hotspots'])}")

    scores = binder / "scoring" / "refold_scores.csv"
    check(scores.exists(), "no refold_scores.csv")
    if scores.exists():
        import csv

        rows = list(csv.DictReader(scores.open()))
        check(bool(rows), "scores file is empty")
        check(not [r for r in rows if r["error"]],
              f"{len([r for r in rows if r['error']])} scoring errors")
        check(any(r.get("ipsae_min") for r in rows), "ipSAE was never computed")

    calib = json.loads((binder / "calibration" / "calibration.json").read_text())
    check(calib["verdict"] in ("SCALE_UP", "SCALE_UP_PARTIAL", "ITERATE", "STOP"),
          f"bad calibration verdict {calib['verdict']!r}")

    ledger = project.root / "ledger.jsonl"
    check(ledger.exists(), "no budget ledger")
    if ledger.exists():
        spent = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
        check(len(spent) == 3, f"expected 3 LLM stages billed, got {len(spent)}")
        check(all(e["usd"] > 0 for e in spent), "a stage was billed at $0")

    manifest = json.loads((project.root / "manifest.json").read_text())
    stages = manifest["rounds"][-1]["stages"]
    for stage in ("target_intel", "interface", "trim", "binder_spec",
                  "binder_scoring"):
        check(stage in stages, f"{stage} not recorded in the manifest")
    check("budget" in manifest, "budget block missing from the manifest")

    print("\n" + "=" * 68)
    if failures:
        print(f"FAILED — {len(failures)} problem(s)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("PASSED — binder track end to end")
        print(f"  stages:      {', '.join(result.stages_completed)}")
        print(f"  trim:        {trim_map['n_residues_before']} -> "
              f"{trim_map['n_residues_after']} residues {trim_map['kept_segments']}")
        print(f"  contig:      {trim_map['contig']}")
        print(f"  calibration: {calib['verdict']}")
        print(f"  spend:       ${sum(e['usd'] for e in spent):.4f}")
    print("=" * 68)
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--designs", type=int, default=12)
    ap.add_argument("--keep", action="store_true", help="keep the temp project")
    args = ap.parse_args()

    for path, what in ((BCR, "CD79b reference campaign"), (STRUCT, "7XQ8 structure")):
        if not path.exists():
            print(f"SKIP: {what} not available at {path}", file=sys.stderr)
            return 0

    tmp = Path(tempfile.mkdtemp(prefix="lpt-e2e-binder-"))
    try:
        return run(tmp, args.designs)
    finally:
        if args.keep:
            print(f"project kept at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
