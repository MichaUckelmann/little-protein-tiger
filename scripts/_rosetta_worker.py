#!/usr/bin/env python
"""PyRosetta interface metrics for RF3-refolded binder complexes.

Runs in the dedicated `pyrosetta` conda env as a subprocess, the same pattern as
`scripts/_sasa_worker.py` — PyRosetta's build is ABI-incompatible with LPT's venv.
Invoked by `src/rosetta_metrics.py`; not meant to be run by hand.

**Only ever called on designs that have already passed the confidence and
geometry gates.** Rosetta happily reports a superb ddG for a confidently wrong
structure: a mis-docked or low-pLDDT model is still a physical pose, so relax and
InterfaceAnalyzer produce numbers that look real and mean nothing. Gating first
is what makes these metrics informative rather than noise.

Metrics (verbatim from the reference campaign, whose calibration they carry):

  ddg            InterfaceAnalyzer dG_separated (kcal/mol, lower = better).
                 Partners are repacked when separated, so this is a true ddG.
  dsasa          interface buried SASA (A^2)
  cms            contact molecular surface — shape-aware, less inflated than
                 dsasa by loose packing
  hbonds_int     H-bonds across the interface
  unsat_hbonds   InterfaceAnalyzer's buried-unsatisfied polar count
  vbuns / sbuns  buried unsatisfied donors/acceptors CREATED by binding (complex
                 minus separated partners); all heavy atoms / sidechain only.
                 Computed by hand because the filter's use_ddG_style option
                 returns a constant 0.0 in this PyRosetta build.
  packstat       interface packing statistic (0-1, higher = better)
  ddg_per_dsasa  100*ddg/dsasa — binding energy density
  ddg_per_res    ddg / binder length
  iface_score    ddg + <buns-weight> * vbuns — so a design cannot buy a good ddG
                 by burying unsatisfied polars
"""

from __future__ import annotations

import argparse
import csv
import glob
import multiprocessing as mp
import os
import sys
import time
import traceback

# Populated once per worker process by _init_worker.
_STATE: dict = {}


def _init_worker(relax: bool, chain1: str, chain2: str, buns_weight: float,
                 dalphaball: bool) -> None:
    import pyrosetta
    import pyrosetta.rosetta as ros

    pyrosetta.init(" ".join([
        "-ignore_unrecognized_res false",
        "-ignore_zero_occupancy false",
        "-mute all",
    ]))
    sfxn = pyrosetta.get_fa_scorefxn()

    relaxer = None
    if relax:
        from pyrosetta.rosetta.protocols.relax import FastRelax
        from pyrosetta.rosetta.protocols.constraint_generator import (
            AddConstraints, CoordinateConstraintGenerator,
        )
        # CA coordinate constraints hold the predicted fold in place while
        # sidechains (and a little backbone) relieve clashes; without them
        # FastRelax can drift far enough to invent or destroy an interface.
        cg = CoordinateConstraintGenerator()
        cg.set_ca_only(True)
        cg.set_sd(0.5)
        add_cst = AddConstraints()
        add_cst.add_generator(cg)
        sf = sfxn.clone()
        sf.set_weight(ros.core.scoring.ScoreType.coordinate_constraint, 1.0)
        fr = FastRelax(sf, 1)
        fr.constrain_relax_to_start_coords(False)
        relaxer = (add_cst, fr)
    else:
        from pyrosetta.rosetta.core.pack.task import TaskFactory
        from pyrosetta.rosetta.core.pack.task.operation import (
            RestrictToRepacking, InitializeFromCommandline,
        )
        from pyrosetta.rosetta.protocols.minimization_packing import PackRotamersMover
        tf = TaskFactory()
        tf.push_back(InitializeFromCommandline())
        tf.push_back(RestrictToRepacking())
        prm = PackRotamersMover(sfxn)
        prm.task_factory(tf)
        relaxer = prm

    _STATE.update(pyrosetta=pyrosetta, ros=ros, sfxn=sfxn, relax=relax,
                  mover=relaxer, chain1=chain1, chain2=chain2,
                  buns_weight=buns_weight, dalphaball=dalphaball)


def count_interface_hbonds(pose, sfxn, chain1: str, chain2: str) -> int:
    """H-bonds whose donor and acceptor residues lie on opposite partners."""
    from pyrosetta.rosetta.core.scoring.hbonds import HBondSet, fill_hbond_set
    sfxn(pose)
    pose.update_residue_neighbors()
    info = pose.pdb_info()
    side = {}
    for i in range(1, pose.total_residue() + 1):
        c = info.chain(i)
        side[i] = 1 if c in chain1 else (2 if c in chain2 else 0)
    hbset = HBondSet()
    fill_hbond_set(pose, False, hbset)
    return sum(1 for i in range(1, hbset.nhbonds() + 1)
               if {side.get(hbset.hbond(i).don_res(), 0),
                   side.get(hbset.hbond(i).acc_res(), 0)} == {1, 2})


def compute_cms(pose, chain1: str, chain2: str) -> float:
    from pyrosetta.rosetta.protocols.simple_filters import ContactMolecularSurfaceFilter
    from pyrosetta.rosetta.core.select.residue_selector import ChainSelector
    cms = ContactMolecularSurfaceFilter()
    cms.selector1(ChainSelector(chain1))
    cms.selector2(ChainSelector(chain2))
    return float(cms.compute(pose))


def _buns_raw(pose, sidechain_only: bool, dalphaball: bool) -> float:
    """Buried unsatisfied polar heavy atoms in a pose, as-is."""
    from pyrosetta.rosetta.protocols.simple_filters import BuriedUnsatHbondFilter
    f = BuriedUnsatHbondFilter()
    if sidechain_only:
        f.set_report_sc_heavy_atom_unsats(True)
    else:
        f.set_report_all_heavy_atom_unsats(True)
    f.set_probe_radius(1.1)
    # DAlphaBall gives better SASA but needs an external binary that is not
    # always present in a conda PyRosetta, so it is opt-in. The setter takes no
    # argument in this build -- calling it at all switches DAlphaBall on, so it
    # must stay inside the branch.
    if dalphaball:
        f.set_dalphaball_sasa()
    return float(f.compute(pose))


def compute_buns_delta(pose, sfxn, dalphaball: bool) -> tuple[float, float]:
    """Buried unsats *created by binding*: complex - binder - target.

    This is the quantity that costs affinity -- polars the interface buries
    without satisfying -- as opposed to unsats already present in either
    monomer's core, which binding neither creates nor can fix.

    Computed by hand rather than with the filter's ``use_ddG_style`` option:
    that path returns exactly 0.0 for every structure in this PyRosetta build
    (verified over 20 designs whose whole-pose counts ranged 4-17), so it would
    silently contribute a constant-zero column. The manual delta over the same
    designs ranges 0-7, median 2.

    The separated partners are not repacked, so this is the standard rigid
    approximation: sidechains at the interface keep their bound rotamers.
    Returns (all-heavy-atom delta, sidechain-only delta), each >= 0 after
    clamping -- binding can also *satisfy* a monomer unsat, which would make the
    raw delta negative, and that is not a penalty.
    """
    chains = pose.split_by_chain()
    if len(chains) < 2:
        raise ValueError(f"expected >=2 chains, split_by_chain gave {len(chains)}")
    parts = [chains[i] for i in range(1, len(chains) + 1)]
    for p in parts:
        sfxn(p)
    out = []
    for sc in (False, True):
        delta = (_buns_raw(pose, sc, dalphaball)
                 - sum(_buns_raw(p, sc, dalphaball) for p in parts))
        out.append(max(0.0, delta))
    return out[0], out[1]


def run_interface_analyzer(pose, sfxn, chain1: str, chain2: str) -> dict:
    from pyrosetta.rosetta.protocols.analysis import InterfaceAnalyzerMover
    iam = InterfaceAnalyzerMover(f"{chain1}_{chain2}")
    iam.set_scorefunction(sfxn)
    iam.set_compute_packstat(True)
    iam.set_pack_input(False)
    iam.set_pack_separated(True)   # repack the separated state -> a true ddG
    iam.set_calc_dSASA(True)
    iam.apply(pose)
    return {
        "ddg": iam.get_interface_dG(),
        "dsasa": iam.get_interface_delta_sasa(),
        "unsat_hbonds": iam.get_interface_delta_hbond_unsat(),
        "packstat": iam.get_interface_packstat(),
    }


def design_name(path: str) -> str:
    b = os.path.basename(path)
    for suffix in ("_model.cif", ".cif", ".pdb"):
        if b.endswith(suffix):
            return b[: -len(suffix)]
    return b


def process_one(path: str) -> dict:
    S = _STATE
    ts = time.time()
    row = {"design": design_name(path), "structure": path}
    try:
        pose = S["pyrosetta"].pose_from_file(path)
        c1, c2 = S["chain1"], S["chain2"]
        info = pose.pdb_info()
        chains = {info.chain(i) for i in range(1, pose.total_residue() + 1)}
        missing = {c for c in (c1 + c2)} - chains
        if missing:
            raise ValueError(f"missing chain(s) {sorted(missing)}; found {sorted(chains)}")

        if S["relax"]:
            add_cst, fr = S["mover"]
            add_cst.apply(pose)
            fr.apply(pose)
        else:
            S["mover"].apply(pose)

        hb = count_interface_hbonds(pose, S["sfxn"], c1, c2)
        cms = compute_cms(pose, c1, c2)
        vbuns, sbuns = compute_buns_delta(pose, S["sfxn"], S["dalphaball"])
        # InterfaceAnalyzer mutates the pose (it separates the partners), so
        # hand it a clone and keep `pose` valid for the final rescore.
        iam = run_interface_analyzer(pose.clone(), S["sfxn"], c1, c2)
        ddg, dsasa = iam["ddg"], iam["dsasa"]
        n_binder = sum(1 for i in range(1, pose.total_residue() + 1)
                       if info.chain(i) in c1)

        row.update({
            "ddg": round(ddg, 3),
            "dsasa": round(dsasa, 2),
            "cms": round(cms, 2),
            "hbonds_int": hb,
            "unsat_hbonds": round(iam["unsat_hbonds"], 2),
            "vbuns": round(vbuns, 2),
            "sbuns": round(sbuns, 2),
            "packstat": round(iam["packstat"], 3),
            "ddg_per_dsasa": round(100.0 * ddg / dsasa, 4) if dsasa else "",
            "ddg_per_res": round(ddg / n_binder, 4) if n_binder else "",
            "iface_score": round(ddg + S["buns_weight"] * vbuns, 3),
            "binder_len": n_binder,
            "total_score": round(S["sfxn"](pose), 2),
            "error": "",
        })
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {' '.join(str(e).split())}"[:250]
        traceback.print_exc(file=sys.stderr)
    row["seconds"] = round(time.time() - ts, 1)
    return row


FIELDS = ["design", "pass", "ddg", "dsasa", "cms", "hbonds_int", "unsat_hbonds",
          "vbuns", "sbuns", "packstat", "ddg_per_dsasa", "ddg_per_res",
          "iface_score", "binder_len", "total_score", "seconds", "structure", "error"]


def gather_inputs(args) -> list[str]:
    if args.list:
        return [l.strip() for l in open(args.list) if l.strip()]
    if args.from_csv:
        with open(args.from_csv) as fh:
            rows = list(csv.DictReader(fh))
        if args.only_pass:
            rows = [r for r in rows if r.get("pass") == "True"]
        paths = [r["refold_cif"] for r in rows if r.get("refold_cif")]
        return [p for p in paths if os.path.exists(p)]
    if args.indir:
        return sorted(glob.glob(os.path.join(args.indir, args.glob)))
    return []


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group("inputs (pick one)")
    src.add_argument("--from-csv", help="refold_scores.csv; uses its refold_cif column "
                                        "and preserves its row order.")
    src.add_argument("--indir", help="RF3 output dir.")
    src.add_argument("--list", help="File with one structure path per line.")
    ap.add_argument("--glob", default="*/*_model.cif", help="Glob within --indir.")
    ap.add_argument("--only-pass", action="store_true",
                    help="With --from-csv, score only rows that passed the refold filter.")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sample", type=int, default=0,
                    help="Evenly sample N designs across the set (for calibration).")
    ap.add_argument("--out", required=True)
    ap.add_argument("--binder-chain", default="A")
    ap.add_argument("--target-chain", default="B")
    ap.add_argument("--nproc", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--no-relax", action="store_true",
                    help="Repack only instead of constrained FastRelax (~10x faster, "
                         "noisier ddG).")
    ap.add_argument("--dalphaball", action="store_true",
                    help="Use DAlphaBall SASA in the buried-unsat filter (needs the "
                         "DAlphaBall binary; off by default).")
    ap.add_argument("--buns-weight", type=float, default=1.0,
                    help="kcal/mol charged per buried unsat in iface_score "
                         "(default: %(default)s).")
    # Gates, calibrated on the 400-refold test200 set (2026-08-05). These are a
    # ~80-residue helical binder against a 65-residue target, so the interfaces
    # are small: observed cms maxed out at 358, which is why a textbook
    # cms >= 350 would have passed 1/400. Recalibrate for a different target.
    ap.add_argument("--max-ddg", type=float, default=-18.0)
    ap.add_argument("--min-dsasa", type=float, default=800.0)
    ap.add_argument("--min-cms", type=float, default=180.0)
    ap.add_argument("--min-hbonds", type=int, default=3)
    ap.add_argument("--max-vbuns", type=float, default=4.0)
    args = ap.parse_args()

    paths = gather_inputs(args)
    if not paths:
        print("No inputs (need --from-csv, --indir or --list)", file=sys.stderr)
        return 1
    if args.sample and args.sample < len(paths):
        step = len(paths) / args.sample
        paths = [paths[int(i * step)] for i in range(args.sample)]
    if args.limit:
        paths = paths[: args.limit]

    nproc = max(1, min(args.nproc, len(paths)))
    mode = "repack" if args.no_relax else "relax"
    print(f"[rosetta] {len(paths)} designs, {nproc} workers, mode={mode}, "
          f"interface {args.binder_chain}_{args.target_chain}", flush=True)

    t0 = time.time()
    init_args = (not args.no_relax, args.binder_chain, args.target_chain,
                 args.buns_weight, args.dalphaball)
    rows = []
    # spawn: a forked child inherits a half-initialised PyRosetta and misbehaves.
    ctx = mp.get_context("spawn")
    with ctx.Pool(nproc, initializer=_init_worker, initargs=init_args) as pool:
        for i, row in enumerate(pool.imap_unordered(process_one, paths, chunksize=1), 1):
            rows.append(row)
            if i % 25 == 0 or i == len(paths):
                el = time.time() - t0
                print(f"  [{i}/{len(paths)}] {el:.0f}s elapsed, "
                      f"{el / i:.2f}s/design wall, eta {(len(paths) - i) * el / i:.0f}s",
                      flush=True)

    num = lambda r, k: r.get(k) if isinstance(r.get(k), (int, float)) else None
    for r in rows:
        v = {k: num(r, k) for k in ("ddg", "dsasa", "cms", "hbonds_int", "vbuns")}
        r["pass"] = bool(
            not r.get("error")
            and v["ddg"] is not None and v["ddg"] <= args.max_ddg
            and v["dsasa"] is not None and v["dsasa"] >= args.min_dsasa
            and v["cms"] is not None and v["cms"] >= args.min_cms
            and v["hbonds_int"] is not None and v["hbonds_int"] >= args.min_hbonds
            and v["vbuns"] is not None and v["vbuns"] <= args.max_vbuns
        )
    rows.sort(key=lambda r: (r.get("iface_score") is None,
                             r.get("iface_score") if isinstance(r.get("iface_score"),
                                                                (int, float)) else 1e9))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if not r.get("error")]
    n_err = len(rows) - len(ok)
    dt = time.time() - t0
    print(f"\n[ok] {len(rows)} scored ({n_err} errors) in {dt:.0f}s "
          f"({dt * nproc / max(len(rows), 1):.1f}s/design CPU) -> {args.out}")
    if ok:
        import statistics as st
        for k in ("ddg", "dsasa", "cms", "hbonds_int", "vbuns", "sbuns",
                  "packstat", "ddg_per_dsasa", "iface_score"):
            v = sorted(r[k] for r in ok if isinstance(r.get(k), (int, float)))
            if v:
                print(f"     {k:<14} min {v[0]:9.2f}  med {st.median(v):9.2f}  "
                      f"max {v[-1]:9.2f}")
        gates = [("ddg    <= %.0f" % args.max_ddg, "ddg", args.max_ddg, True),
                 ("dsasa  >= %.0f" % args.min_dsasa, "dsasa", args.min_dsasa, False),
                 ("cms    >= %.0f" % args.min_cms, "cms", args.min_cms, False),
                 ("hbonds >= %d" % args.min_hbonds, "hbonds_int", args.min_hbonds, False),
                 ("vbuns  <= %.0f" % args.max_vbuns, "vbuns", args.max_vbuns, True)]
        print("     --- passing each gate alone ---")
        for label, k, lim, lower_better in gates:
            n = sum(1 for r in ok if isinstance(r.get(k), (int, float))
                    and (r[k] <= lim if lower_better else r[k] >= lim))
            print(f"     {label:<16} {n:4d}/{len(ok)}")
    print(f"[pass] {sum(1 for r in rows if r['pass'])}/{len(rows)} pass all gates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
