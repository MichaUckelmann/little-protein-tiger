#!/usr/bin/env python3
"""Benchmark the TRIM across real targets, real epitopes and a budget ladder.

Why this exists
---------------
The trim's quality guards — BSA retention, the hotspot-retention check, the
newly-exposed-hydrophobic gate — have essentially never judged a real cut in a
real campaign. 22 of the 25 trims in `projects/` are NO-OPS: the target already
fitted `design.foundry.target_residue_budget`, so every one of them measures
0.0% exposure and 100% retention. Nothing on disk can calibrate a threshold,
and `MAX_EXPOSED_HYDROPHOBIC_FRACTION = 0.25` is consequently a proposal.

Two phases, because the expensive half and the interesting half are different:

**interface** (costs API money, ~$0.16 and ~90 s per entry) runs the REAL
`complex-structure-analysis` stage through `--workflow structure`, which is
the entry point that skips discovery and target selection entirely and calls
exactly one LLM stage. What comes back is a genuine epitope with grounded
residue names and RFD3 sidechain atoms, having passed every chain-assignment
and grounding guard the pipeline applies.

That matters more than it sounds. The earlier sweep used
`benchmark_trim.derive_hotspots`, a deterministic ddG/BSA stand-in, and on
5VAI chain R it picked residue 205 — inside the transmembrane bundle — which
makes the hotspot-bearing "domain" the entire 387-residue chain and every
budget under it raise `TrimBudgetError`. A stand-in epitope benchmarks a trim
nobody would ever run.

**ladder** (free, CPU, repeatable) re-reads those epitopes off disk and trims
each target at a descending budget ladder with the exposure gates and the BSA
floor DISABLED, so it MEASURES instead of being refused early, then derives
what production would have decided from the numbers. The gate is a pure
function of them (`structure_trim.exposure_verdict`), so deriving the verdict
is exactly equivalent to a second gated trim and costs nothing.

Usage
-----
    # once per target, spends money
    python scripts/benchmark_trimming.py interface --pdb 7CZD 6VJJ 3KYS \
        --budget-per-entry 0.35

    # as often as you like, free
    python scripts/benchmark_trimming.py ladder --out trim_ladder.tsv
    python scripts/benchmark_trimming.py report --out trim_ladder.tsv

`interface` is idempotent: an entry whose `21_interface.md` already exists is
skipped, so a rerun after a network failure costs nothing for the entries that
landed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore")

from src import structure_trim as st                              # noqa: E402
from src.handoff import parse_handoff, parse_hotspot_residues     # noqa: E402

#: Where `interface` puts its projects. One per entry, so a failure is isolated
#: and the manifest/ledger of each is readable on its own.
PROJECT_PREFIX = "trimbench_"

#: Budget rungs as a fraction of the target chain's own residue count. 1.0 is
#: the no-op control and is always worth having: it is what distinguishes "this
#: cut opened core" from "this structure reads as exposed however you treat it".
#:
#: Deliberately FINE near the top, because the window where a cut is possible
#: at all can be narrow. The trim cuts on domain boundaries, so a budget below
#: the hotspot-bearing domain set raises `TrimBudgetError` and a budget at or
#: above the whole chain is a no-op — everything interesting is in between, and
#: for a single-domain target that interval is EMPTY. Measured on 7CZD (one
#: 116-residue Ig domain): 117 is a no-op and 105 and below all raise. That is
#: the correct answer for that target and it is why chain size alone is the
#: wrong selection criterion for this benchmark; see `docs/trim-benchmark.md`.
#: Rungs are free, so err on the side of more of them.
LADDER = (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.6, 0.5)


# ──────────────────────────────────────────────────────────────────────────
# phase 1: real epitopes
# ──────────────────────────────────────────────────────────────────────────

def interface_phase(pdbs: list[str], budget_per_entry: float,
                    provider: str, uniprots: dict[str, str]) -> None:
    """Run the one-LLM-stage structure workflow once per entry."""
    total = 0.0
    for i, pdb in enumerate(pdbs, 1):
        project = f"{PROJECT_PREFIX}{pdb.lower()}"
        if _interface_artifact(project) is not None:
            print(f"[{i}/{len(pdbs)}] {pdb}: already have an epitope — skipped",
                  flush=True)
            continue
        cmd = [sys.executable, "scripts/run_pipeline.py",
               "--workflow", "structure", "--pdb", pdb,
               "--project", project, "--stop-after", "spec",
               "--provider", provider,
               "--budget", f"{budget_per_entry:.2f}"]
        if pdb.upper() in uniprots:
            # Activates the chain-assignment, organism and membrane-topology
            # checks, all three of which fail OPEN without an accession. A
            # trim benchmark does not need them, but a target whose chain
            # assignment is backwards is not a target worth trimming.
            cmd += ["--uniprot", uniprots[pdb.upper()]]
        print(f"[{i}/{len(pdbs)}] {pdb}: {' '.join(cmd[2:])}", flush=True)
        # A non-zero exit is expected and useful: `--stop-after spec` runs the
        # trim at the production budget, and a trim that REFUSES ends the run.
        # `21_interface.md` is written before the trim, so the epitope survives
        # and the ladder can still use it.
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        spend = _project_spend(project)
        total += spend
        ok = _interface_artifact(project) is not None
        print(f"    exit {proc.returncode} | ${spend:.4f} | epitope "
              f"{'OK' if ok else 'MISSING'} | running total ${total:.2f}",
              flush=True)
        if not ok:
            tail = "\n      ".join(
                (proc.stderr or proc.stdout or "").strip().splitlines()[-4:])
            print(f"      {tail}", flush=True)
    print(f"\ninterface phase spend: ${total:.2f}")


def _interface_artifact(project: str) -> Path | None:
    """The `21_interface.md` for one project, wherever the track put it.

    A single-site structure run writes it under
    `runs/round-N/binder/sites/primary/binder/`, but a plain binder run writes
    it one level up — so glob rather than assume, and take the newest.
    """
    d = ROOT / "projects" / project
    if not d.is_dir():
        return None
    hits = sorted(d.rglob("21_interface.md"), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def _project_spend(project: str) -> float:
    """What this project actually spent, from the project's own accounting.

    `manifest.json`'s `budget.spent_usd` rollup first, since that is what
    `--budget` itself enforces. The ledger is the fallback and is summed over
    `kind == "actual"` ONLY: it also carries pre-flight `estimate` rows (the
    7CZD probe was estimated at $0.36 against an actual $0.16), so summing
    every row would roughly triple the reported spend and the wrong number
    here is the one that decides whether the next entry runs.
    """
    d = ROOT / "projects" / project
    man = d / "manifest.json"
    if man.exists():
        try:
            spent = ((json.loads(man.read_text(encoding="utf-8")).get("budget")
                      or {}).get("spent_usd"))
            if spent is not None:
                return float(spent)
        except Exception:
            pass
    total = 0.0
    led = d / "ledger.jsonl"
    if led.exists():
        for line in led.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("kind") == "actual":
                total += float(row.get("usd") or 0.0)
    return total


# ──────────────────────────────────────────────────────────────────────────
# phase 2: the ladder
# ──────────────────────────────────────────────────────────────────────────

FIELDS = [
    "pdb_id", "target_chain", "partner_chain", "n_hotspots",
    "budget", "budget_frac", "n_before", "n_after", "removed",
    "n_segments", "method", "contig",
    "bsa_target_side_A2", "bsa_retention",
    "over_budget", "n_away", "area_away_A2", "n_near", "area_near_A2",
    "exposed_fraction", "exposure_verdict", "near_refuses",
    "retention_refuses", "production_verdict", "error",
]


def ladder_phase(out_path: Path, only: list[str] | None) -> list[dict]:
    rows: list[dict] = []
    projects = sorted((ROOT / "projects").glob(f"{PROJECT_PREFIX}*"))
    for proj in projects:
        pdb = proj.name[len(PROJECT_PREFIX):].upper()
        if only and pdb not in only:
            continue
        art = _interface_artifact(proj.name)
        if art is None:
            print(f"{pdb}: no epitope on disk — run the interface phase",
                  flush=True)
            continue
        text = art.read_text(encoding="utf-8")
        handoff = parse_handoff(text)
        raw = parse_hotspot_residues(text, handoff)
        if not raw:
            print(f"{pdb}: {art} has no MODEL-READY HOTSPOTS table", flush=True)
            continue
        hs = json.loads(raw)
        tgt, ptn = hs.get("target_chain"), hs.get("partner_chain")
        hots = hs.get("residues") or []
        path = _structure_path(pdb)
        if path is None or not tgt or not hots:
            print(f"{pdb}: missing structure or chain/epitope", flush=True)
            continue
        try:
            n_res = len(st.chain_residues(path, tgt))
        except Exception as exc:
            print(f"{pdb}: {type(exc).__name__}: {exc}", flush=True)
            continue
        print(f"\n## {pdb} chain {tgt} ({n_res} res) vs {ptn} — "
              f"{len(hots)} hotspots from the real interface stage", flush=True)
        seen: set[int] = set()
        for frac in LADDER:
            budget = max(st.MIN_TARGET_RESIDUES, int(round(n_res * frac)))
            if budget in seen:
                continue
            seen.add(budget)
            rows.append(_one_rung(pdb, path, tgt, ptn, hots, budget, frac,
                                  n_res))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write("\t".join(FIELDS) + "\n")
        for r in rows:
            fh.write("\t".join("" if r.get(k) is None else str(r.get(k, ""))
                               for k in FIELDS) + "\n")
    print(f"\n{len(rows)} rungs -> {out_path}")
    return rows


def _one_rung(pdb, path, tgt, ptn, hots, budget, frac, n_res) -> dict:
    """Trim once, UNGATED, and derive what production would have decided."""
    row = {"pdb_id": pdb, "target_chain": tgt, "partner_chain": ptn,
           "n_hotspots": len(hots), "budget": budget,
           "budget_frac": round(frac, 2), "n_before": n_res}
    with tempfile.TemporaryDirectory() as td:
        try:
            # Gates OFF so this MEASURES. `max_exposed_hydrophobic=None`
            # disables both exposure measures; `min_bsa_retention=0.0`
            # disables the epitope-damage floor. Every other refusal — the
            # 80-residue floor, hotspot loss, insertion codes,
            # TrimBudgetError — still fires, and is recorded as `error`,
            # because those are facts about the target rather than
            # thresholds under test.
            res = st.trim_target(
                path, target_chain=tgt, partner_chain=ptn, hotspots=hots,
                budget=budget, out_dir=Path(td), pdb_id=pdb,
                min_bsa_retention=0.0, max_exposed_hydrophobic=None)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {' '.join(str(exc).split())[:110]}"
            row["production_verdict"] = "REFUSED_OTHER"
            print(f"  bud={budget:4d}  {row['error'][:96]}", flush=True)
            return row
        away, near = st._exposed_hydrophobic(
            path, res.trimmed_path, tgt, res.hotspots_retained)

    a_away = round(sum(r[2] for r in away), 1)
    a_near = round(sum(r[2] for r in near), 1)
    bsa = res.interface_bsa_target_side_A2
    verdict, fraction = st.exposure_verdict(a_away, len(away), bsa)
    near_refuses = bool(near)
    retention_refuses = bool(ptn and bsa > 0 and res.bsa_retention < 0.90
                             and res.n_residues_after != res.n_residues_before)
    row.update({
        "n_after": res.n_residues_after,
        "removed": res.n_residues_before - res.n_residues_after,
        # The budget is NOT a hard bound, and this is how much it is missed
        # by. `plan_trim` validates the hotspot-bearing domains against the
        # budget and only THEN runs `_bridge_gaps` / `_drop_islands` /
        # `_drop_dangles`, and bridging ADDS residues (up to `BRIDGE_GAP = 12`
        # per hole, on the deliberate grounds that a 3-residue hole is a worse
        # target than no hole). So a trim can return more residues than it was
        # given budget for. Not silent in production — `validate_spec`'s
        # `max_complex_tokens` is the real ceiling and catches it at
        # spec-build — but it means "budget" reads as a request, not a limit.
        "over_budget": max(0, res.n_residues_after - budget),
        "n_segments": res.n_segments, "method": res.method,
        "contig": res.contig,
        "bsa_target_side_A2": bsa, "bsa_retention": res.bsa_retention,
        "n_away": len(away), "area_away_A2": a_away,
        "n_near": len(near), "area_near_A2": a_near,
        "exposed_fraction": None if fraction is None else round(fraction, 4),
        "exposure_verdict": verdict,
        "near_refuses": int(near_refuses),
        "retention_refuses": int(retention_refuses),
        "error": "",
    })
    row["production_verdict"] = production_verdict(
        retention_refuses, near_refuses, verdict)
    print(f"  bud={budget:4d} {row['n_before']:3d}->{row['n_after']:3d} "
          f"rm={row['removed']:3d} seg={row['n_segments']} {res.method:9s} "
          f"away={len(away):2d}/{a_away:7.1f} near={len(near):2d}/{a_near:6.1f} "
          f"ret={res.bsa_retention:6.1%} frac="
          f"{'   n/a' if fraction is None else format(fraction, '6.1%')}  "
          f"{row['production_verdict']}", flush=True)
    return row


def production_verdict(retention_refuses: bool, near_refuses: bool,
                       exposure: str) -> str:
    """What a GATED `trim_target` would have done, from the ungated numbers.

    The gate is a pure function of the measurements, so deriving the verdict
    is exactly equivalent to a second gated trim and costs nothing. What must
    match is the PRECEDENCE, because `trim_target` raises on the first failure
    and only that one is ever reported.

    Measured against the implementation by character offset, because I got it
    backwards first time and a test caught it: the order is **near-epitope
    exposure (9491), then the away fraction (10003), then the residue-count
    fallback (10760), then the BSA-retention floor (14547)**. Retention is
    checked LAST, not first — it sits after `write_trimmed` further down the
    function, not with the interface arithmetic that computes it.

    The error mattered in one specific direction. A cut bad enough to open
    core beside the epitope has usually damaged the interface too, so both
    flags are set together often; ranking retention first would have
    relabelled a large share of REFUSED_NEAR rows as REFUSED_RETENTION and
    made the exposure guard look like it was catching much less than it does.
    """
    if near_refuses:
        return "REFUSED_NEAR"
    if exposure != "ok":
        return "REFUSED_EXPOSURE"
    return "REFUSED_RETENTION" if retention_refuses else "PASS"


def _structure_path(pdb: str) -> Path | None:
    d = ROOT / "data" / "structures"
    for cand in (f"{pdb.upper()}.cif", f"{pdb.upper()}_ba1.cif",
                 f"{pdb.lower()}.cif"):
        if (d / cand).exists():
            return d / cand
    return None


# ──────────────────────────────────────────────────────────────────────────
# phase 3: read it back
# ──────────────────────────────────────────────────────────────────────────

def report_phase(out_path: Path) -> None:
    import csv
    import statistics as stats

    rows = list(csv.DictReader(out_path.open(encoding="utf-8"), delimiter="\t"))
    if not rows:
        print("no rows")
        return

    def f(r, k):
        try:
            return float(r[k])
        except (ValueError, KeyError, TypeError):
            return None

    noop = [r for r in rows if r.get("removed") == "0"]
    cut = [r for r in rows if r.get("removed") not in ("0", "", None)]
    print(f"{len(rows)} rungs over {len({r['pdb_id'] for r in rows})} targets "
          f"| {len(noop)} no-ops, {len(cut)} real cuts, "
          f"{len([r for r in rows if r['production_verdict'] == 'REFUSED_OTHER'])}"
          f" refused before measurement")

    print("\nproduction verdict by whether anything was actually removed:")
    for label, group in (("no-op", noop), ("real cut", cut)):
        counts: dict[str, int] = {}
        for r in group:
            counts[r["production_verdict"]] = counts.get(
                r["production_verdict"], 0) + 1
        print(f"  {label:9s} " + "  ".join(
            f"{k}={v}" for k, v in sorted(counts.items())))

    exposed = [f(r, "exposed_fraction") for r in cut]
    exposed = [x for x in exposed if x is not None]
    if exposed:
        print(f"\nexposed fraction over {len(exposed)} real cuts: "
              f"min {min(exposed):.1%} | median {stats.median(exposed):.1%} "
              f"| max {max(exposed):.1%}")
    noop_ex = [f(r, "exposed_fraction") for r in noop]
    noop_ex = [x for x in noop_ex if x is not None]
    if noop_ex:
        print(f"exposed fraction over {len(noop_ex)} no-ops:    "
              f"min {min(noop_ex):.1%} | median {stats.median(noop_ex):.1%} "
              f"| max {max(noop_ex):.1%}   <- must be ~0, or the measure is "
              f"reporting something other than the cut")

    passing = [r for r in cut if r["production_verdict"] == "PASS"]
    print(f"\nreal cuts production would ACCEPT: {len(passing)}/{len(cut)}")
    for r in sorted(passing, key=lambda r: -(f(r, "exposed_fraction") or 0))[:12]:
        print(f"  {r['pdb_id']} {r['target_chain']} {r['n_before']:>3}->"
              f"{r['n_after']:>3} seg={r['n_segments']} "
              f"frac={float(r['exposed_fraction']):.1%} {r['contig']}")

    # The number the threshold would be fitted on: where do accepted and
    # refused cuts actually separate?
    # `.get`, not `[]`: a TSV written by an older revision of this script has
    # no such column, and a benchmark that cannot read its own earlier output
    # throws away the runs it cost money to produce.
    over = [(r, int(r.get("over_budget") or 0)) for r in rows
            if (r.get("over_budget") or "0").isdigit()
            and int(r.get("over_budget") or 0) > 0]
    if over:
        worst = max(over, key=lambda t: t[1])
        print(f"\nthe budget is not a hard bound: {len(over)}/{len(rows)} rungs "
              f"returned MORE residues than their budget, by up to "
              f"{worst[1]} ({worst[0]['pdb_id']} budget {worst[0]['budget']} "
              f"-> {worst[0]['n_after']} residues). `plan_trim` checks the "
              f"budget before `_bridge_gaps` adds residues back.")

    segs = [(r, int(r.get("n_segments") or 0)) for r in rows
            if (r.get("n_segments") or "").isdigit()]
    bad = [t for t in segs if t[1] >= 6]
    if bad:
        w = max(bad, key=lambda t: t[1])
        print(f"\nsegment explosion: {len(bad)}/{len(segs)} rungs produced >=6 "
              f"segments, worst {w[1]} ({w[0]['pdb_id']} budget "
              f"{w[0]['budget']}). `max_chainbreaks` is derived from the "
              f"segment count, so an N-segment target spends N-1 chain breaks "
              f"before the binder is looked at — nothing refuses this.")

    acc = [f(r, "exposed_fraction") for r in passing]
    ref = [f(r, "exposed_fraction") for r in cut
           if r["production_verdict"] == "REFUSED_EXPOSURE"]
    acc, ref = [x for x in acc if x is not None], [x for x in ref if x is not None]
    if acc and ref:
        print(f"\nseparation: accepted cuts reach {max(acc):.1%}, "
              f"exposure-refused cuts start at {min(ref):.1%}"
              + ("  <- a real gap, the threshold sits inside it"
                 if max(acc) < min(ref) else
                 "  <- OVERLAP: no single fraction separates these"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="phase", required=True)

    i = sub.add_parser("interface", help="run the real hotspot picker (costs API)")
    i.add_argument("--pdb", nargs="+", required=True)
    # Set above the PRE-FLIGHT ESTIMATE, not above the expected spend.
    # `--budget` refuses a stage whose PROJECTED cost would pass the cap, and
    # the projection for this stage runs ~2.3x the real figure ($0.36
    # projected against $0.12-0.16 measured). So a cap of $0.35 — more than
    # twice what the stage actually costs — is refused before it starts, and
    # every entry comes back MISSING for a reason that looks nothing like a
    # budget problem. This is a runaway guard, not a forecast.
    i.add_argument("--budget-per-entry", type=float, default=0.50)
    i.add_argument("--provider", default="gemini")
    i.add_argument("--uniprot", nargs="*", default=[],
                   help="PDB=ACCESSION pairs, e.g. 7CZD=Q9NZQ7")

    l = sub.add_parser("ladder", help="trim each epitope across budgets (free)")
    l.add_argument("--out", type=Path, default=ROOT / "docs" / "trim-ladder.tsv")
    l.add_argument("--only", nargs="*", default=None)

    r = sub.add_parser("report", help="read a ladder TSV back")
    r.add_argument("--out", type=Path, default=ROOT / "docs" / "trim-ladder.tsv")

    a = ap.parse_args(argv)
    if a.phase == "interface":
        pairs = dict(p.split("=", 1) for p in a.uniprot if "=" in p)
        interface_phase([p.upper() for p in a.pdb], a.budget_per_entry,
                        a.provider, {k.upper(): v for k, v in pairs.items()})
    elif a.phase == "ladder":
        ladder_phase(a.out, [p.upper() for p in a.only] if a.only else None)
    else:
        report_phase(a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
