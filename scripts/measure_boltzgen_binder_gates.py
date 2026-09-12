"""Score a finished BoltzGen run with the BINDER track's metrics, and report
what the binder-track gates would have decided.

This is a MEASUREMENT, not a pipeline stage. It imports `src.binder_metrics`
and `src.binder_ranking` read-only, writes only into `--out` (never into the
run directory), and changes nothing about how either track executes. Its
purpose is to answer three questions that cannot be settled by reading code,
before any decision is made about unifying the two backends:

1. **Do the binder track's gates discriminate on BoltzGen output at all?**
   Eight of its nine hard gates are computable here; the ninth (`ipsae_min_min`)
   is off by default. BoltzGen's own gates passed 100/100 designs on both
   archived runs, so "does anything bite" is a real question.

2. **Is `binder_rmsd_dock` meaningful for Boltz-2?** The binder track's whole
   justification for that gate is that RF3 provably cannot convey a docked
   pose (`p_provide_inter_molecule_distances` is 0.0 at inference), so iPTM
   alone selects confidently mis-docked binders. BoltzGen's `folding` step sets
   `target_templates: true`, and whether that leaks the *pose* as well as the
   target *fold* is not answerable from the source. It is answerable from the
   distribution: a pinned pose gives a spike near zero, an independent one a
   broad spread. `--report` prints both that spread and the correlation against
   BoltzGen's own `bb_target_aligned_rmsd_design`.

3. **How much does losing `ipsae_min` cost?** No PAE matrix is persisted
   anywhere in a BoltzGen run, so all eight `ipsae_*` fields are absent and the
   heaviest composite weight (2.0) drops out with a warning
   (`binder_ranking.composite_score`). `--ipsae-from-boltzgen` substitutes
   BoltzGen's own `design_ipsae_min` so the two rankings can be compared —
   it is NOT a drop-in equivalent (different PAE cutoff, different d0 variant,
   and observed values run an order of magnitude below the RF3-calibrated bar
   of 0.5), which is exactly why it is opt-in and reported separately.

Backend differences handled here, all of them already parameters of
`binder_metrics` rather than new code:

* **Chain roles are inverted.** BoltzGen writes target = chain A, binder =
  chain B; RFD3 writes binder = A, target = B. `ScoreConfig(binder_chain="B",
  target_chain="A")`.
* **`plddt_scale` is 1.0, not 100.0.** BoltzGen's refold B-factor is already
  on RF3's 0-1 convention (per-residue, broadcast to every atom of the
  residue). Protenix is the 0-100 backend; copying its scale here would divide
  every pLDDT by 100.
* **Hotspots need no remap, but they do need the right source.** BoltzGen's
  output CIF carries the original mmCIF `label_seq` as its new `auth_seq_id`,
  and a `binding:` list is already in that numbering — so no remap (this is
  the fact `_stage_analysis` handles by rewriting `auth_seq_id` before calling
  the SASA worker). The ids are read from the **executed YAML's `binding:`
  list**, not from `02_structure.md`: `hotspot_engagement` is a fraction of
  what a design was conditioned on, and a multi-region structure report holds
  every region's hotspots flattened together. See `read_hotspots`.
* **The design pose is the INVERSE-FOLDED one.** `intermediate_designs/` holds
  the design-step output, whose chain-B sequence differs from the CSV's
  `designed_sequence` and whose B-factor is a design mask rather than pLDDT.
  The pose that matches the scored sequence is
  `intermediate_designs_inverse_folded/<id>.cif`, and its refold is that
  directory's `refold_cif/<id>.cif`.

Usage:

    python scripts/measure_boltzgen_binder_gates.py outputs/e2e_mesothelioma
    python scripts/measure_boltzgen_binder_gates.py outputs/e2e_cgas_sting \
        --ipsae-from-boltzgen
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from src.binder_metrics import FIELDS, ScoreConfig, score_one, write_scores  # noqa: E402
from src.binder_ranking import rank_designs, write_ranking_outputs  # noqa: E402
from src.handoff import parse_hotspot_residues  # noqa: E402

#: Where BoltzGen's own per-design metric table lives inside a run.
_METRICS_CSV = "final_ranked_designs/all_designs_metrics.csv"
#: The inverse-folded design pose + its refold. NOT `intermediate_designs/`.
_DESIGN_DIR = "intermediate_designs_inverse_folded"
_REFOLD_SUBDIR = "refold_cif"


def _f(row: dict, key: str) -> float | None:
    """One CSV cell as a float, or None when absent/unparseable."""
    v = row.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def boltzgen_summary(row: dict, *, ipsae_from_boltzgen: bool = False) -> dict:
    """One `all_designs_metrics.csv` row as an RF3-shaped summary dict.

    The shape is what `score_one` already reads out of an RF3
    `_summary_confidences.json`, so no scoring logic needs a second
    implementation — the same manoeuvre `protenix_confidence_adapter` makes.

    Two shape details that are not cosmetic:

    * `chain_ptm` is indexed `[binder, target]` by `score_one`, so the design's
      own pTM goes first.
    * every `chain_pair_*` matrix is read as `[0][1]` (they are
      upper-triangular with None on and below the diagonal), so the interface
      value must sit in that one cell.

    `interaction_pae` — not `pae_mean` or `complex_pde` — is the mean
    inter-chain PAE, i.e. the actual analogue of RF3's `chain_pair_pae[0][1]`.
    A whole-structure mean would dilute the interface signal with two
    mostly-unrelated intra-chain blocks, which is the same trap the Protenix
    adapter documents.
    """
    summary: dict = {
        "iptm": _f(row, "design_to_target_iptm"),
        "ptm": _f(row, "ptm"),
        "overall_plddt": _f(row, "complex_plddt"),
        "chain_ptm": [_f(row, "design_ptm"), _f(row, "target_ptm")],
        "chain_pair_pae": [[None, _f(row, "interaction_pae")], [None, None]],
        "chain_pair_pae_min": [[None, _f(row, "min_design_to_target_pae")],
                               [None, None]],
        # BoltzGen reports mean PDE only (`complex_pde` / `complex_ipde`);
        # there is no min-PDE column, and a mean in a min's slot would be a
        # quietly wrong number rather than a missing one.
        "chain_pair_pde_min": [[None, None], [None, None]],
        # No clash flag exists. Left absent deliberately: `require_no_clash`
        # then falls back to the geometry-derived `clash_severe == 0`, which
        # the binder track already considers the better signal (RF3's own
        # has_clash was False for all 400 reference designs, 159 of which had
        # a sub-2.2 A contact).
        "has_clash": None,
        "ranking_score": _f(row, "quality_score"),
    }
    if ipsae_from_boltzgen:
        summary["_boltzgen_ipsae"] = {
            "ipsae_min": _f(row, "design_ipsae_min"),
            "ipsae_binder": _f(row, "design_to_target_ipsae"),
            "ipsae_target": _f(row, "target_to_design_ipsae"),
        }
    return summary


_BINDING_RE = re.compile(r"^\s*binding:\s*([0-9][0-9,\s]*)\s*$", re.M)


def _executed_yaml(run_dir: Path) -> Path | None:
    """The YAML the execution stage actually ran.

    Mirrors `_find_design_yaml`: prefer `*_boltzgen.yaml`, else any `*.yaml`,
    and take the FIRST — the execution stage is single-YAML, so a multi-region
    design writes several and only region 1 ever reaches the GPU.
    """
    d = run_dir / "03_design_inputs"
    if not d.is_dir():
        return None
    cands = sorted(d.glob("*_boltzgen.yaml")) or sorted(
        p for p in d.glob("*.yaml") if p.is_file())
    return cands[0] if cands else None


def read_hotspots(run_dir: Path) -> tuple[list[int], str]:
    """Hotspot ids in BoltzGen's OUTPUT numbering.

    The ids come from the **executed design YAML's `binding:` list**, not from
    `02_structure.md`, and that distinction is the whole correctness of
    `hotspot_engagement` — which is a FRACTION of the hotspots a design was
    actually conditioned on.

    `parse_hotspot_residues` flattens *every* MODEL-READY HOTSPOTS section in a
    report into one list, and a multi-region structure report has several. On
    `outputs/e2e_mesothelioma` that is 15 residues across 3 sections, while the
    YAML that ran (region 1) declared 5 — so scoring against the report gave a
    flat 8/15 = 0.533 for nearly every design and charged them for residues in
    a region they were never asked to bind. The YAML is the honest denominator.

    No remap is needed either way: BoltzGen's `binding:` list is indexed on the
    deposited mmCIF `label_seq`, and its output CIF carries that same integer as
    the target chain's `auth_seq_id`.

    Falls back to the structure report when no YAML survives, and always
    reports the two sets' disagreement — on `outputs/e2e_cgas_sting` they are
    entirely disjoint (`209,304,412` vs `238,259,328,514,362,364`), which is a
    fact about that run's provenance worth seeing rather than averaging over.
    """
    report = run_dir / "02_structure.md"
    report_ids: list[int] = []
    fellback = 0
    if report.is_file():
        raw = parse_hotspot_residues(report.read_text(encoding="utf-8"), {})
        for r in (json.loads(raw).get("residues") or []) if raw else []:
            label, auth = r.get("label_seq_id"), r.get("auth_seq_id")
            if label is None or int(label) == int(auth):
                fellback += 1
            report_ids.append(int(label if label is not None else auth))

    yaml_path = _executed_yaml(run_dir)
    yaml_ids: list[int] = []
    if yaml_path is not None:
        for m in _BINDING_RE.finditer(yaml_path.read_text(encoding="utf-8")):
            yaml_ids += [int(x) for x in m.group(1).replace(" ", "").split(",")
                         if x]

    if yaml_ids:
        ids, src = yaml_ids, f"{yaml_path.name} (binding:)"
    elif report_ids:
        ids, src = report_ids, f"{report.name} (no YAML found)"
    else:
        raise SystemExit(
            f"no hotspots recoverable for {run_dir}: neither a `binding:` list "
            f"in 03_design_inputs/ nor a MODEL-READY HOTSPOTS table in "
            f"02_structure.md")

    notes = [f"{len(ids)} hotspots from {src}"]
    if yaml_ids and report_ids:
        extra = sorted(set(report_ids) - set(yaml_ids))
        missing = sorted(set(yaml_ids) - set(report_ids))
        if extra:
            notes.append(f"{len(extra)} hotspot(s) in the structure report are "
                         f"NOT in the executed YAML ({extra}) — other "
                         f"region(s); scoring against them would charge these "
                         f"designs for residues they were not asked to bind")
        if missing:
            notes.append(f"{len(missing)} hotspot(s) in the YAML are NOT in the "
                         f"structure report ({missing}) — the design was "
                         f"conditioned on residues the report does not list")
    if fellback:
        notes.append(f"{fellback} report row(s) have label_seq_id == "
                     f"auth_seq_id (label-aligned structure, or an "
                     f"uncorrected table)")
    return ids, "; ".join(notes)


def score_run(run_dir: Path, *, limit: int | None = None,
              ipsae_from_boltzgen: bool = False) -> tuple[list[dict], str]:
    """Score every design in `run_dir` with the binder track's own scorer."""
    exec_dir = run_dir / "04_execution_outputs"
    metrics_csv = exec_dir / _METRICS_CSV
    if not metrics_csv.is_file():
        raise SystemExit(f"no {_METRICS_CSV} under {exec_dir} — this is not a "
                         f"completed BoltzGen run")
    design_dir = exec_dir / _DESIGN_DIR
    refold_dir = design_dir / _REFOLD_SUBDIR
    for d in (design_dir, refold_dir):
        if not d.is_dir():
            raise SystemExit(f"missing {d} — the `folding` step did not run, so "
                             f"there is no independent refold to compare against")

    hotspots, hs_note = read_hotspots(run_dir)
    logger.info(hs_note)

    with metrics_csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if limit:
        rows = rows[:limit]

    # BoltzGen: target = A, binder = B. RFD3 is the other way round, and
    # ScoreConfig defaults to RFD3's convention.
    cfg = ScoreConfig(binder_chain="B", target_chain="A")

    out: list[dict] = []
    for i, row in enumerate(rows, 1):
        name = str(row.get("id") or "").strip()
        fname = str(row.get("file_name") or f"{name}.cif").strip()
        design_path, refold_path = design_dir / fname, refold_dir / fname
        if not design_path.is_file() or not refold_path.is_file():
            out.append({"name": name, "design_family": name,
                        "design_cif": str(design_path),
                        "refold_cif": str(refold_path),
                        "error": "design or refold CIF missing"})
            continue
        summary = boltzgen_summary(row, ipsae_from_boltzgen=ipsae_from_boltzgen)
        native_ipsae = summary.pop("_boltzgen_ipsae", None)
        try:
            rec = score_one(
                name, refold_path, design_path, summary, hotspots,
                cfg=cfg,
                # No PAE matrix is persisted anywhere in a BoltzGen run, so
                # the ipSAE block is skipped rather than fed a guess.
                conf=None,
                # No RFD3 design sidecar exists; the six rfd3_* columns stay
                # absent, which `binder_ranking` handles by dropping them.
                sidecar=None,
                plddt_scale=1.0,
            )
        except Exception as exc:   # one unreadable pair must not end the sweep
            rec = {"name": name, "design_family": name,
                   "design_cif": str(design_path),
                   "refold_cif": str(refold_path),
                   "error": f"{type(exc).__name__}: {exc}"}
        if native_ipsae:
            rec.update({k: v for k, v in native_ipsae.items() if v is not None})
            rec["ipsae_variant"] = "boltzgen_native"
        # Carried for the cross-checks in `report`; `write_scores` drops any
        # key outside FIELDS, so these never reach refold_scores.csv.
        rec["_bg_iptm"] = _f(row, "design_to_target_iptm")
        rec["_bg_dock"] = _f(row, "bb_target_aligned_rmsd_design")
        rec["_bg_pass"] = str(row.get("pass_filters") or "").strip().lower()
        out.append(rec)
        if i % 25 == 0:
            logger.info(f"  scored {i}/{len(rows)}")
    return out, hs_note


def _pct(vals: list[float], q: float) -> float:
    """Percentile by nearest rank — no interpolation, no numpy dependency."""
    if not vals:
        return float("nan")
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def report(rows: list[dict], run_dir: Path) -> str:
    """The three questions this script exists to answer."""
    ok = [r for r in rows if not r.get("error")]
    bad = [r for r in rows if r.get("error")]
    out = [f"# {run_dir.name} — binder-track metrics over BoltzGen output", "",
           f"scored {len(ok)}/{len(rows)} designs"
           + (f" ({len(bad)} errored)" if bad else "")]
    if bad:
        out.append("")
        for r in bad[:5]:
            out.append(f"  ! {r['name']}: {r['error']}")

    def col(k: str) -> list[float]:
        return [float(r[k]) for r in ok
                if r.get(k) is not None and r.get(k) != ""]

    out += ["", "## Metric distributions (binder-track columns)", "",
            "| column | n | p05 | median | p95 | gate |",
            "|:-------|--:|----:|-------:|----:|:-----|"]
    gates = {"binder_rmsd_dock": "<= 5.0", "binder_rmsd_fold": "<= 2.0",
             "epitope_recall": ">= 0.5", "hotspot_engagement": ">= 0.75",
             "binder_plddt": ">= 0.75", "iptm": ">= 0.5",
             "iface_pae": "<= 15.0", "clash_severe": "== 0",
             "ipsae_min": "(weight 2.0)"}
    for k, g in gates.items():
        v = col(k)
        if not v:
            out.append(f"| `{k}` | 0 | — | — | — | {g} — **column absent** |")
            continue
        out.append(f"| `{k}` | {len(v)} | {_pct(v, 0.05):.3f} | "
                   f"{statistics.median(v):.3f} | {_pct(v, 0.95):.3f} | {g} |")

    # Q2: is the dock gate measuring anything, or did target templating pin it?
    dock = col("binder_rmsd_dock")
    if dock:
        near = sum(1 for d in dock if d <= 1.0)
        out += ["", "## Is `binder_rmsd_dock` independent of the design pose?", "",
                f"- spread: {min(dock):.2f} - {max(dock):.2f} A "
                f"(median {statistics.median(dock):.2f})",
                f"- within 1 A of the designed pose: {near}/{len(dock)} "
                f"({100 * near / len(dock):.0f}%)",
                "",
                "A target-templated refold that also pinned the POSE would pile "
                "up near zero. A broad spread means the dock was re-chosen, and "
                "the gate is measuring something the design did not dictate."]
        pairs = [(float(r["binder_rmsd_dock"]), float(r["_bg_dock"])) for r in ok
                 if r.get("binder_rmsd_dock") is not None
                 and r.get("_bg_dock") is not None]
        if len(pairs) > 2:
            try:
                r_xy = statistics.correlation([p[0] for p in pairs],
                                              [p[1] for p in pairs])
                out.append(f"- vs BoltzGen's own `bb_target_aligned_rmsd_design`: "
                           f"r = {r_xy:.4f} over {len(pairs)} designs "
                           f"(a cross-check that we are measuring the same thing "
                           f"it is, by a different route)")
            except statistics.StatisticsError:
                pass
    return "\n".join(out)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Score a finished BoltzGen run with the binder track's "
                    "metrics and gates. Read-only: writes only into --out.")
    ap.add_argument("run_dir", type=Path,
                    help="a completed BoltzGen run directory (the one holding "
                         "02_structure.md and 04_execution_outputs/)")
    ap.add_argument("--out", type=Path, default=None,
                    help="where to write refold_scores.csv / ranked.csv / "
                         "top_k.csv / filter_stats.txt. Defaults to a sibling "
                         "temp dir; NEVER the run directory.")
    ap.add_argument("--limit", type=int, default=None,
                    help="score only the first N designs (smoke test)")
    ap.add_argument("--ipsae-from-boltzgen", action="store_true",
                    help="populate ipsae_* from BoltzGen's own design_ipsae_min "
                         "instead of leaving them absent. Differently "
                         "parameterised from RF3's ipSAE — for comparison only.")
    ap.add_argument("--config", type=Path, default=ROOT / "config.yaml",
                    help="config whose design.binder_ranking block supplies the "
                         "gates and weights")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    run_dir = args.run_dir.resolve()
    out_dir = (args.out or Path(os.environ.get("TMPDIR", "/tmp"))
               / f"boltzgen_binder_gates_{run_dir.name}").resolve()
    # A measurement must not be mistakable for a pipeline artifact.
    if out_dir == run_dir or run_dir in out_dir.parents:
        raise SystemExit(f"--out {out_dir} is inside the run directory; this "
                         f"script never writes into a run")

    import yaml
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    br = ((cfg.get("design") or {}).get("binder_ranking")) or {}

    rows, _ = score_run(run_dir, limit=args.limit,
                        ipsae_from_boltzgen=args.ipsae_from_boltzgen)

    out_dir.mkdir(parents=True, exist_ok=True)
    write_scores(rows, out_dir / "refold_scores.csv")

    scored = [{k: v for k, v in r.items() if k in FIELDS} for r in rows]
    ranking = rank_designs(
        scored,
        thresholds=br.get("thresholds"), weights=br.get("weights"),
        mmr=br.get("mmr"), top_k=int(br.get("top_k", 20)),
        max_per_backbone=int(br.get("max_per_backbone", 1)),
    )
    write_ranking_outputs(ranking, out_dir)

    text = report(rows, run_dir)
    stats = ranking.filter_stats
    text += "\n".join([
        "", "", "## What the binder-track gates decide", "",
        f"- input: {stats.n_input}",
        f"- survivors: {stats.n_survivors}",
        f"- top-K: {len(ranking.top_k)}",
        f"- composite columns actually used: {ranking.composite_columns}",
        "", "### Drop reasons", "",
    ] + [f"- {k}: {v}" for k, v in sorted(stats.dropped.items(),
                                          key=lambda kv: -kv[1])])

    own = run_dir / "05_ranking" / "filter_stats.txt"
    if own.is_file():
        text += ("\n\n## What this run's own (BoltzGen-track) gates decided\n\n```\n"
                 + own.read_text(encoding="utf-8").rstrip() + "\n```\n")

    (out_dir / "REPORT.md").write_text(text + "\n", encoding="utf-8")
    print(text)
    logger.info(f"artifacts -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
