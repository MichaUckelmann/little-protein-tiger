#!/usr/bin/env python3
"""Benchmark src.structure_trim across a size spectrum of real complexes.

No API calls: the trim is deterministic Python (plus free RCSB domain lookups).
The one LLM-derived input in production is the hotspot list, so this derives it
deterministically from the SAME tool outputs the interface skill reads —
per-residue ddG and BSA from analyze_interface — ranked, spatially clustered and
capped at MAX_HOTSPOTS, which is what the skill is instructed to do.

Three questions, per structure:
  1. does it cut at domain boundaries, or mid-domain?
  2. does the epitope survive?
  3. does cutting expose hydrophobic core that was buried before? A freshly
     exposed hydrophobic slab is what RFD3 preferentially binds, and it is the
     documented failure mode for membrane targets.
"""
from __future__ import annotations
import json, sys, traceback, types, warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path("/home/m.uckelmann_cbs-niob.local/code/little-protein-tiger")
sys.path.insert(0, str(ROOT))

import gemmi  # noqa: E402
from src.env_config import load_env  # noqa: E402
from src.structure_tools import analyze_interface, is_solvent_or_additive  # noqa: E402
from src.structure_trim import trim_target, write_trimmed  # noqa: E402
from src.foundry_spec import MAX_HOTSPOTS  # noqa: E402

# LPT_FOUNDRY_ROOT and the checkpoint dir live in `.env`, and the `designs`
# subcommand resolves the real foundry binaries through them. Every pipeline
# entry point calls this; a script that reaches `foundry_runner` and does not
# fails with "foundry root is not set" on a machine where it plainly is.
load_env(ROOT / ".env")

KD = {"A": 1.8, "R": -4.5, "N": -3.5, "D": -3.5, "C": 2.5, "Q": -3.5, "E": -3.5,
      "G": -0.4, "H": -3.2, "I": 4.5, "L": 3.8, "K": -3.9, "M": 1.9, "F": 2.8,
      "P": -1.6, "S": -0.8, "T": -0.7, "W": -0.9, "Y": -1.3, "V": 4.2}
HYDROPHOBIC = set("AVLIMFWCY")


def _model(path: Path):
    st = gemmi.read_structure(str(path))
    st.setup_entities()
    st.remove_alternative_conformations()
    return st


def protein_chains(path: Path, min_len: int = 25):
    out = []
    for ch in _model(path)[0]:
        n = sum(1 for r in ch
                if (gemmi.find_tabulated_residue(r.name) or gemmi.ResidueInfo()).is_amino_acid())
        if n >= min_len:
            out.append((ch.name, n))
    out.sort(key=lambda x: -x[1])
    return out


def ca_coords(path: Path, chain: str) -> dict[int, tuple]:
    out = {}
    for ch in _model(path)[0]:
        if ch.name != chain:
            continue
        for r in ch:
            a = r.find_atom("CA", "*")
            if a is not None:
                out[int(r.seqid.num)] = (a.pos.x, a.pos.y, a.pos.z)
    return out


def derive_hotspots(path: Path, target: str, partner: str, cap: int = MAX_HOTSPOTS):
    """Deterministic stand-in for the interface skill's selection."""
    res = analyze_interface(str(path), target, partner)
    iface = res.get("interface") or {}
    per = {}
    for e in iface.get("bsa_per_residue") or []:
        if e.get("chain") != target or is_solvent_or_additive(str(e.get("residue") or "")):
            continue
        per[int(e["resnum"])] = float(e["bsa_A2"])
    ddg = {}
    for e in res.get("chain_a_interface_residues") or []:
        if e.get("ddg_estimate_kcal_mol") is not None and e.get("resnum") is not None:
            ddg[int(e["resnum"])] = float(e["ddg_estimate_kcal_mol"])
    if not per:
        return [], iface
    # rank: strongest ddG first (more negative = better), BSA as the co-criterion
    ranked = sorted(per, key=lambda r: (ddg.get(r, 0.0), -per[r]))
    coords = ca_coords(path, target)
    chosen = [ranked[0]]
    for r in ranked[1:]:
        if len(chosen) >= cap:
            break
        if r not in coords or chosen[0] not in coords:
            continue
        cx = tuple(sum(coords[c][i] for c in chosen) / len(chosen) for i in range(3))
        d = sum((coords[r][i] - cx[i]) ** 2 for i in range(3)) ** 0.5
        if d <= 14.0:            # one compact patch, per the skill's guidance
            chosen.append(r)
    return ([{"auth_seq_id": r,
              "residue": _res_name(path, target, r),
              "rfd3_atoms": contact_atoms(path, target, r, partner)}
             for r in sorted(chosen)], iface)


def _res_name(path: Path, chain: str, auth: int) -> str:
    for ch in _model(path)[0]:
        if ch.name != chain:
            continue
        for res in ch:
            if int(res.seqid.num) == auth:
                return res.name
    return ""


def contact_atoms(path: Path, chain: str, auth: int, partner: str,
                  n: int = 2) -> str:
    """The heavy SIDECHAIN atoms of one residue that face the partner.

    RFD3 hotspot selection is atom-level, so a benchmark that builds a real
    spec has to name atoms, and which ones is not arbitrary — they are what
    the binder is steered to pack against. The interface skill picks them by
    reading the contact; the deterministic equivalent is "the heavy sidechain
    atoms nearest the partner chain", which is the same question answered from
    coordinates.

    Hydrogens are excluded deliberately: a 1.8 A structure carries riding H,
    and an atom-name sort would happily return HD23 — an atom that exists, so
    `validate_spec` accepts it, steering RFD3 at nothing useful. Backbone is
    excluded for the same reason, EXCEPT as the fallback for GLY (and any
    residue whose sidechain was not modelled), where `CA,C` is all there is.
    """
    model = _model(path)[0]
    target_res = None
    partner_atoms = []
    for ch in model:
        for res in ch:
            if ch.name == chain and int(res.seqid.num) == auth:
                target_res = res
            elif ch.name == partner:
                partner_atoms.extend(a.pos for a in res if not a.name.startswith("H"))
    if target_res is None or not partner_atoms:
        return "CB,CA"
    side = [a for a in target_res
            if a.name not in ("N", "CA", "C", "O", "OXT")
            and not a.name.startswith("H")]
    if len(side) < n:
        # No modelled sidechain to steer with. CB when it exists, else pure
        # backbone — and `_check_hotspot_atoms_are_buildable` is what tells an
        # operator a set dominated by these steers weakly.
        have = {a.name for a in target_res}
        return "CB,CA" if "CB" in have else "CA,C"
    ranked = sorted(side, key=lambda a: min(a.pos.dist(p) for p in partner_atoms))
    return ",".join(a.name for a in ranked[:n])


def sasa_per_residue(path: Path, chain: str) -> dict[int, float]:
    """Per-residue SASA of ONE chain in isolation."""
    import freesasa  # noqa
    raise RuntimeError("unused")


def sasa_biotite(path: Path, chain: str) -> dict[int, tuple[str, float]]:
    import biotite.structure as struc
    import biotite.structure.io.pdbx as pdbx
    f = pdbx.CIFFile.read(str(path))
    arr = pdbx.get_structure(f, model=1)
    arr = arr[struc.filter_amino_acids(arr) & (arr.chain_id == chain)]
    if arr.array_length() == 0:
        return {}
    s = struc.sasa(arr, vdw_radii="Single")
    out: dict[int, list] = {}
    for i in range(arr.array_length()):
        if s[i] != s[i]:
            continue
        rid = int(arr.res_id[i])
        out.setdefault(rid, [arr.res_name[i], 0.0])
        out[rid][1] += float(s[i])
    return {k: (v[0], v[1]) for k, v in out.items()}


def one_letter(res3: str) -> str:
    info = gemmi.find_tabulated_residue(res3)
    return info.one_letter_code.upper() if info else "X"


def newly_exposed_hydrophobic(orig: Path, trimmed: Path, chain: str, kept: set[int]):
    """Hydrophobic area exposed BY THE CUT, over residues the trim kept.

    Both SASAs are of the target chain alone, so the partner's contribution
    cancels and what remains is what cutting revealed.
    """
    a = sasa_biotite(orig, chain)
    b = sasa_biotite(trimmed, chain)
    tot = phob = 0.0
    worst = []
    for rid in kept & set(a) & set(b):
        d = b[rid][1] - a[rid][1]
        if d <= 0:
            continue
        tot += d
        if one_letter(a[rid][0]) in HYDROPHOBIC:
            phob += d
            worst.append((round(d, 1), rid, a[rid][0]))
    worst.sort(reverse=True)
    return round(tot, 1), round(phob, 1), worst[:5]


def boundary_alignment(res, chain_min: int, chain_max: int):
    """How far each internal cut sits from the nearest annotated domain edge."""
    edges = set()
    for d in res.domains or []:
        for a in (getattr(d, "start", None), getattr(d, "end", None)):
            if a is not None:
                edges.add(int(a))
    cuts = []
    for i, (s, e) in enumerate(res.kept_segments):
        if s != chain_min:
            cuts.append(s)
        if e != chain_max:
            cuts.append(e)
    if not cuts:
        return [], None
    if not edges:
        return cuts, None
    return cuts, [min(abs(c - x) for x in edges) for c in cuts]


def run(path: Path, budget: int = 220) -> dict:
    chains = protein_chains(path)
    if len(chains) < 2:
        return {"file": path.name, "skipped": "fewer than two protein chains"}
    (target, n_t), (partner, _) = chains[0], chains[1]
    row = {"file": path.name, "target": target, "partner": partner, "n_target": n_t}
    hs, iface = derive_hotspots(path, target, partner)
    row["n_hotspots"] = len(hs)
    if not hs:
        row["skipped"] = "no interface residues"
        return row
    out = Path("/tmp/bench_out") / path.stem
    out.mkdir(parents=True, exist_ok=True)
    res = trim_target(path, target_chain=target, partner_chain=partner,
                      hotspots=hs, budget=budget, out_dir=out,
                      pdb_id=path.stem.split("_")[0])
    ids = sorted(ca_coords(path, target))
    cuts, dists = boundary_alignment(res, min(ids), max(ids))
    kept = {r for s, e in res.kept_segments for r in range(s, e + 1)}
    tot, phob, worst = newly_exposed_hydrophobic(path, Path(res.trimmed_path), target, kept)
    row.update(
        method=res.method, n_before=res.n_residues_before, n_after=res.n_residues_after,
        removed=res.n_residues_before - res.n_residues_after, n_segments=res.n_segments,
        n_domains=len(res.domains or []),
        hotspots_kept=len(res.hotspots_retained), hotspots_lost=len(res.hotspots_lost),
        bsa_retention=round(res.bsa_retention, 3),
        bsa_dropped=round(res.bsa_dropped_A2, 1),
        cuts=cuts, cut_to_domain_edge=dists,
        exposed_total_A2=tot, exposed_hydrophobic_A2=phob, worst_exposed=worst,
        warnings=res.warnings)
    return row


def launch_designs(out_root: Path, n_designs: int, dry_run: bool = True,
                   only: int | None = None) -> None:
    """One detached RFD3-ONLY job per rung.

    **Phase A runs the design step and nothing else**, and that is the whole
    economy of the experiment: the hypothesis is about where RFD3 PLACES the
    binder, which is decided at design time, so no MPNN and no RF3 refold are
    needed to answer it. Costed with the pipeline's own constant:
    `SEC_PER_RFD3_DESIGN`, 300 designs per rung, 13 rungs -> ~5.9 GPU-h.

    This is worth stating because the scope pointed at
    `write_campaign_driver`, and that runs the FULL pipeline — design,
    prefilter, MPNN, RF3. Driving Phase A through it and reading
    `plan.est_gpu_hours` gives **34.8 GPU-h across the same 13 rungs**, six
    times the Phase A budget, because almost all of it is refolds the readout
    never looks at. Measured, not reasoned: 15.0 h for the 6 6VJJ rungs plus
    19.8 h for the 7 3KYS rungs.

    So the driver is not used — but `_rfd3_command` IS, which is the part that
    matters: the command, the sampler settings, the checkpoint alias and the
    binary resolution all come from the production config, so this measures
    the same generator a real campaign runs. A hand-written RFD3 invocation
    would be a second, divergent path.

    `--dry-run` is the default, and a real launch goes through
    `JobRegistry.launch` — per the standing workstation note,
    a Bash background task's teardown reaches descendants and has killed a
    campaign and its detached child before.

    `only` launches ONE rung. Without it every rung goes at once, and 13
    concurrent RFD3 processes on one card would not only thrash but destroy
    the secondary measurement: Phase A's rungs span 168-286 tokens, which is
    exactly the spread needed to fit the size law `SEC_PER_RFD3_DESIGN` does
    not have. Contended timings measure the contention.
    """
    import yaml

    from src.env_config import resolve_env_path
    from src.foundry_runner import (SEC_PER_RFD3_DESIGN, FoundryPaths,
                                    FoundryValidationError, _rfd3_command,
                                    _resolve_foundry_bin)

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    design = cfg.get("design") or {}

    # `_rfd3_command` emits `uv run .venv-blackwell/bin/rfd3 ...` — a path
    # RELATIVE to the foundry checkout, because `uv run` resolves the venv
    # from its cwd. The campaign driver honours that (`cd "$FOUNDRY"`); this
    # script wrote `cd <LPT root>` and every rung would have died instantly
    # with `Failed to spawn .venv-blackwell/bin/rfd3`, thirteen times, for
    # nothing. Resolve the binary here too, so a wrong/absent venv name fails
    # now rather than inside a detached job.
    f_cfg = design.get("foundry") or {}
    foundry_root = resolve_env_path("LPT_FOUNDRY_ROOT", f_cfg.get("root"))
    if not foundry_root:
        raise FoundryValidationError(
            "foundry root is not set — set LPT_FOUNDRY_ROOT in .env or "
            "design.foundry.root in config.yaml")
    foundry_root = Path(foundry_root)
    rfd3_bin = _resolve_foundry_bin(
        foundry_root, f_cfg.get("rfd3_bin", ".venv-blackwell/bin/rfd3"),
        "rfd3")
    payload = json.loads((out_root / "ladder.json").read_text(encoding="utf-8"))
    n_batches = max(1, n_designs // 4)
    total_h = 0.0
    for rung in payload["rungs"]:
        if "error" in rung or "spec_path" not in rung:
            continue
        if only is not None and int(rung["budget"]) != only:
            continue
        rung_dir = out_root / f"rung_{rung['budget']}"
        paths = FoundryPaths.under(rung_dir / "campaign")
        for d in (paths.campaign_dir, paths.rfd3_dir, paths.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

        # A CampaignPlan is what `_rfd3_command` reads its counts off, and only
        # these three fields are used. Built directly rather than through
        # `plan_campaign`, whose job is to size and disk-clamp a FULL campaign.
        plan = types.SimpleNamespace(n_batches=n_batches,
                                     diffusion_batch_size=4,
                                     expected_rfd3=n_batches * 4)
        cmd = _rfd3_command(design, Path(rung["spec_path"]), paths, plan,
                            bin_=rfd3_bin)
        script = paths.campaign_dir / "run_designs.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "# Phase A: RFD3 only. No MPNN, no RF3 — see launch_designs().\n"
            "set -euo pipefail\n"
            f"cd {foundry_root}\n"
            f"{cmd}\n", encoding="utf-8")
        script.chmod(0o755)

        est = n_batches * 4 * SEC_PER_RFD3_DESIGN / 3600
        total_h += est
        print(f"  rung {rung['budget']}: {script}  "
              f"({n_batches * 4} designs, ~{est:.2f} GPU-h at "
              f"{SEC_PER_RFD3_DESIGN:g} s/design)", flush=True)
        if not dry_run:
            # `JobRegistry.launch`, the same entry point the campaign driver
            # uses (start_new_session=True, so it survives this process and a
            # Bash-task teardown). An earlier revision called a module-level
            # `launch_detached` that does not exist in job_registry and never
            # has — the ImportError fired only under `--launch`, so the whole
            # of Phase A planned cleanly thirteen times and could not run.
            from src.job_registry import JobRegistry

            log = paths.logs_dir / "rfd3.log"
            rec = JobRegistry(paths.registry_path).launch(
                f"rung_{rung['budget']}", ["bash", str(script)],
                cwd=str(paths.campaign_dir), log_path=log,
                note=f"Phase A rung {rung['budget']}")
            print(f"    launched: pid {rec.pid} -> {log}", flush=True)
    print(f"\ntotal across rungs: ~{total_h:.2f} GPU-h"
          + ("  (dry run — nothing launched)" if dry_run else ""))
    print("NOTE: SEC_PER_RFD3_DESIGN has no size law — it is a flat constant "
          "for every complex size. Phase A measures one for free; compare the "
          "rung directories' mtimes against their token counts.")


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sweep", help="the original size-spectrum benchmark")
    s.add_argument("files", nargs="+")
    s.add_argument("--budget", type=int, default=220)

    s = sub.add_parser("ladder", help="trim one target to a series of budgets")
    s.add_argument("structure")
    s.add_argument("--target", required=True)
    s.add_argument("--partner", required=True)
    s.add_argument("--budgets", required=True,
                   help="comma-separated, e.g. 168,153,140,117,106,90")
    s.add_argument("--out", required=True)

    s = sub.add_parser("spec", help="build + validate one RFD3 spec per rung")
    s.add_argument("--out", required=True)
    s.add_argument("--binder-min", type=int, default=70)
    s.add_argument("--binder-max", type=int, default=86)

    s = sub.add_parser("designs", help="one detached RFD3 job per rung (GPU)")
    s.add_argument("--out", required=True)
    s.add_argument("--designs", type=int, default=300)
    s.add_argument("--launch", action="store_true",
                   help="actually launch; without it, plan and print only")
    s.add_argument("--rung", type=int, default=None,
                   help="launch ONE rung (by budget). Rungs must not share "
                        "the card: concurrent jobs would corrupt the "
                        "per-rung timing Phase A also measures.")

    s = sub.add_parser("score", help="the section 5.4 readout (CPU)")
    s.add_argument("--out", required=True)
    s.add_argument("--cutoff", type=float, default=8.0)

    a = ap.parse_args()
    if a.cmd == "sweep":
        rows = []
        for f in (Path(x) for x in a.files):
            try:
                rows.append(run(f, a.budget))
                print(f"  ok   {f.name}", flush=True)
            except Exception as exc:                              # noqa: BLE001
                rows.append({"file": f.name,
                             "error": f"{type(exc).__name__}: {exc}"})
                print(f"  FAIL {f.name}: {type(exc).__name__}: {exc}", flush=True)
                traceback.print_exc(limit=2)
        out = Path("/tmp/bench_out/results.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, indent=1, default=str))
        print(f"\nwrote {out}  ({len(rows)} rows)")
    elif a.cmd == "ladder":
        ladder(Path(a.structure), a.target, a.partner,
               [int(b) for b in a.budgets.split(",")], Path(a.out))
    elif a.cmd == "spec":
        build_specs(Path(a.out), a.binder_min, a.binder_max)
    elif a.cmd == "designs":
        launch_designs(Path(a.out), a.designs, dry_run=not a.launch,
                       only=a.rung)
    elif a.cmd == "score":
        score_designs(Path(a.out), a.cutoff)
    return 0



# =======================================================================
# GLUE_PIPELINE_SCOPE.md section 5 — the exposed-hydrophobic dose-response
# =======================================================================
#
# Four subcommands, in the order they are meant to be run. Every threshold in
# `structure_trim.py` that concerns exposed hydrophobic surface
# (MAX_EXPOSED_HYDROPHOBIC = 2, EXPOSED_HOTSPOT_CLEARANCE_A = 10, the 15 A^2
# per-residue delta) was set from the CPU-only sweep above, with no GPU
# validation at all — and measured over all 22 `trim_map.json` files on disk,
# no production campaign has ever cut more than 9 residues, so they have never
# once fired on a real run. This measures whether the rationale is true:
# does a freshly exposed hydrophobic patch actually pull RFD3's binder off the
# epitope, and at what dose?
#
# The economy of the design: the hypothesis is about where RFD3 PLACES the
# binder, which is decided at design time. So Phase A needs no MPNN, no RF3
# and no refold — the whole readout comes off the design CIFs.
#
#   ladder        trim one target to a series of budgets, recording each
#                 rung's exposed-patch residue set          (CPU)
#   spec          build + validate one RFD3 spec per rung   (CPU)
#   designs       launch one detached RFD3 job per rung      (GPU)
#   score         the readout over each rung's designs       (CPU)
#
# `score` is re-runnable and never touches the GPU, which matters: two of the
# metrics it reports are new, and a scorer that is wrong compresses exactly
# the difference it is meant to measure (see diary 2026-09-10).

BACKBONE = ("N", "CA", "C", "O", "OXT")


def ladder(path: Path, target: str, partner: str, budgets: list[int],
           out_root: Path) -> dict:
    """One trim per budget, with the exposure guard DISABLED.

    `max_exposed_hydrophobic=None` is the point: the guard is what is under
    test, so it must not refuse the rungs that would answer the question. The
    epitope is held fixed — `trim_target` raises if a hotspot is lost, so every
    rung that succeeds retained all of them by construction.
    """
    from src.structure_trim import _exposed_hydrophobic

    hs, _iface = derive_hotspots(path, target, partner)
    if not hs:
        raise SystemExit(f"{path.name}: no interface residues to derive hotspots from")
    pdb_id = path.stem.split("_")[0]
    rungs = []
    for budget in budgets:
        rung_dir = out_root / f"rung_{budget}"
        rung_dir.mkdir(parents=True, exist_ok=True)
        try:
            res = trim_target(path, target_chain=target, partner_chain=partner,
                              hotspots=hs, budget=budget, out_dir=rung_dir,
                              pdb_id=pdb_id, max_exposed_hydrophobic=None)
        except Exception as exc:                                  # noqa: BLE001
            rungs.append({"budget": budget,
                          "error": f"{type(exc).__name__}: {exc}"})
            print(f"  rung {budget}: {type(exc).__name__}: {exc}", flush=True)
            continue
        away, near = _exposed_hydrophobic(
            path, Path(res.trimmed_path), target, res.hotspots_retained)
        rung = {
            "budget": budget,
            "trimmed_path": str(res.trimmed_path),
            "contig": res.contig,
            "kept_segments": [list(s) for s in res.kept_segments],
            "n_segments": res.n_segments,
            "n_after": res.n_residues_after,
            "hotspots_auth": sorted(int(h["auth_seq_id"])
                                    for h in res.hotspots_retained),
            # The patch, as auth ids plus areas. Split exactly as the guard
            # splits it, because the two thresholds are separate numbers.
            "patch_away": [[n, int(a), float(d)] for n, a, d, *_ in away],
            "patch_near": [[n, int(a), float(d)] for n, a, d, *_ in near],
            "exposed_away_A2": round(sum(float(d) for _n, _a, d, *_ in away), 1),
            "exposed_near_A2": round(sum(float(d) for _n, _a, d, *_ in near), 1),
            "bsa_retention": round(res.bsa_retention, 3),
            "interface_bsa_target_side_A2": res.interface_bsa_target_side_A2,
        }
        rungs.append(rung)
        print(f"  rung {budget}: {res.n_residues_after} res, "
              f"{res.n_segments} seg, away {rung['exposed_away_A2']} A^2 "
              f"({len(away)} res), near {rung['exposed_near_A2']} A^2 "
              f"({len(near)} res)", flush=True)

    payload = {"file": str(path), "pdb_id": pdb_id, "target_chain": target,
               "partner_chain": partner,
               "hotspots": [{"auth_seq_id": int(h["auth_seq_id"]),
                             "residue": h.get("residue"),
                             "rfd3_atoms": h.get("rfd3_atoms")} for h in hs],
               "rungs": rungs}
    (out_root / "ladder.json").write_text(json.dumps(payload, indent=1),
                                          encoding="utf-8")
    return payload


def build_specs(out_root: Path, binder_min: int = 70,
                binder_max: int = 86) -> None:
    """One RFD3 spec per rung, through the production builder and validator."""
    from src.foundry_spec import build_rfd3_spec, validate_spec

    payload = json.loads((out_root / "ladder.json").read_text(encoding="utf-8"))
    for rung in payload["rungs"]:
        if "error" in rung:
            continue
        rung_dir = out_root / f"rung_{rung['budget']}"
        # The hotspots the trim RETAINED, with the atoms the ladder derived.
        atoms = {int(h["auth_seq_id"]): h["rfd3_atoms"]
                 for h in payload["hotspots"]}
        hotspots = [{"auth_seq_id": a, "rfd3_atoms": atoms[a]}
                    for a in rung["hotspots_auth"] if a in atoms]
        spec_path = rung_dir / "spec.json"
        # The PDB sibling `trim_target` writes, which is what
        # `_stage_trim` hands RFD3 (pipeline_runner's `pdb_input`) — NOT the
        # .cif. RFD3 reads an mmCIF by label_seq_id, so an author-numbered
        # contig against the .cif addresses the wrong residues or aborts on
        # the GPU; `validate_spec` now refuses that pairing, which is what
        # caught all 13 rungs here.
        build_rfd3_spec(
            name=f"{payload['pdb_id'].lower()}_rung{rung['budget']}",
            structure_path=Path(rung["trimmed_path"]).with_suffix(".pdb"),
            contig=rung["contig"], hotspots=hotspots,
            target_chain=payload["target_chain"], out_path=spec_path,
            binder_min=binder_min, binder_max=binder_max)
        summary = validate_spec(
            spec_path, kept_segments=[tuple(s) for s in rung["kept_segments"]])
        one = next(iter(summary["designs"].values()))
        rung["spec_path"] = str(spec_path)
        rung["n_tokens"] = rung["n_after"] + (binder_min + binder_max) // 2
        print(f"  rung {rung['budget']}: spec ok — "
              f"{one['n_target_residues']} target res, "
              f"{one['n_hotspots']} hotspots, {one['n_segments']} seg, "
              f"~{rung['n_tokens']} tokens", flush=True)
    (out_root / "ladder.json").write_text(json.dumps(payload, indent=1),
                                          encoding="utf-8")


def score_designs(out_root: Path, cutoff: float = 8.0) -> list[dict]:
    """The section 5.4 readout, per design, CPU-only.

    `patch_enrichment` is the statistic to read, not `patch_contact_fraction`:
    the patch GROWS down the ladder, so an uncorrected fraction rises with
    patch size whether or not the binder is being pulled anywhere. Enrichment
    divides by the patch's share of the accessible target, so 1.0 means
    "contacts the patch exactly as often as its size predicts" and the
    hypothesis predicts > 1.0 rising with dose.
    """
    from src.binder_metrics import epitope, hotspots_from_rfd3, read_structure

    payload = json.loads((out_root / "ladder.json").read_text(encoding="utf-8"))
    rows = []
    for rung in payload["rungs"]:
        if "error" in rung:
            continue
        rung_dir = out_root / f"rung_{rung['budget']}"
        # Iterate the SIDECARS, not the CIFs. The sidecar is what carries
        # `diffused_index_map`, and RFD3's own output naming varies: the
        # calibration directories on disk hold `<name>_<i>_model_<m>.cif.gz`
        # with no suffix, while the prefilter's inputs carry `_b<k>_d<k>`
        # (which is why `foundry_stages` globs on `"_b" in name`). Matching a
        # CIF by its sidecar's stem PREFIX covers both without guessing.
        cars = sorted(rung_dir.rglob("rfd3/**/*_model_*.json"))
        if not cars:
            print(f"  rung {rung['budget']}: no designs yet", flush=True)
            continue
        patch_auth = {int(a) for _n, a, _d in rung["patch_away"]}
        patch_auth |= {int(a) for _n, a, _d in rung["patch_near"]}
        scored = 0
        for sidecar in cars:
            cif = _cif_for(sidecar)
            if cif is None:
                continue
            imap = json.loads(sidecar.read_text(encoding="utf-8")).get(
                "diffused_index_map") or {}
            # auth -> output resid, for the TARGET chain only. The map is the
            # only place the input->output relabelling exists; the spec has no
            # map, and reading the spec instead silently scores the wrong
            # residues (CLAUDE.md, the RFD3 sidecar note).
            out_of = {}
            for k, v in imap.items():
                try:
                    out_of[int(k[1:])] = int(str(v)[1:])
                except (ValueError, IndexError):
                    continue
            patch_out = {out_of[a] for a in patch_auth if a in out_of}
            try:
                atoms = read_structure(cif)
                contacts = epitope(atoms, "A", "B", cutoff,
                                   binder_backbone_only=False)
                accessible = {int(r) for r in atoms[atoms.chain_id == "B"].res_id}
            except Exception as exc:                              # noqa: BLE001
                print(f"    {cif.name}: {type(exc).__name__}: {exc}")
                continue
            hots = set(hotspots_from_rfd3(sidecar, "B"))
            share = (len(patch_out) / len(accessible)) if accessible else 0.0
            frac = (len(contacts & patch_out) / len(contacts)) if contacts else 0.0
            rows.append({
                "rung": rung["budget"],
                "design": cif.stem,
                "exposed_away_A2": rung["exposed_away_A2"],
                "exposed_near_A2": rung["exposed_near_A2"],
                "n_patch": len(patch_out),
                "n_contacts": len(contacts),
                "patch_contact_fraction": round(frac, 4),
                "patch_share_of_target": round(share, 4),
                "patch_enrichment": round(frac / share, 3) if share else None,
                "hotspot_engagement_design":
                    round(len(hots & contacts) / len(hots), 4) if hots else None,
            })
            scored += 1
        print(f"  rung {rung['budget']}: scored {scored} designs "
              f"({len(patch_auth)} patch residues)", flush=True)
    (out_root / "design_scores.json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    _summarise(rows)
    return rows


def _cif_for(sidecar: Path) -> Path | None:
    """The design CIF belonging to one sidecar JSON.

    Handles `<stem>.cif`, `<stem>.cif.gz` and `<stem>_b0_d0.cif` — three
    namings that all occur in real output directories. `read_structure`
    already reads gzip, so no decompression is needed here.
    """
    stem = sidecar.stem
    for cand in (f"{stem}.cif", f"{stem}.cif.gz"):
        p = sidecar.with_name(cand)
        if p.exists():
            return p
    hits = sorted(sidecar.parent.glob(f"{stem}_b*.cif*"))
    return hits[0] if hits else None


def _summarise(rows: list[dict]) -> None:
    """Per-rung medians — the dose-response table, printed."""
    import statistics as st

    by_rung: dict[int, list[dict]] = {}
    for r in rows:
        by_rung.setdefault(r["rung"], []).append(r)
    if not by_rung:
        return
    print(f"\n{'rung':>6} {'n':>5} {'away A^2':>9} {'near A^2':>9} "
          f"{'patch%':>7} {'enrich':>7} {'engage':>7}")
    for rung in sorted(by_rung, reverse=True):
        rs = by_rung[rung]
        enr = [r["patch_enrichment"] for r in rs if r["patch_enrichment"]]
        eng = [r["hotspot_engagement_design"] for r in rs
               if r["hotspot_engagement_design"] is not None]
        print(f"{rung:>6} {len(rs):>5} {rs[0]['exposed_away_A2']:>9.0f} "
              f"{rs[0]['exposed_near_A2']:>9.0f} "
              f"{st.median(r['patch_contact_fraction'] for r in rs):>7.3f} "
              f"{(st.median(enr) if enr else float('nan')):>7.2f} "
              f"{(st.median(eng) if eng else float('nan')):>7.3f}")

if __name__ == "__main__":
    raise SystemExit(_main())
