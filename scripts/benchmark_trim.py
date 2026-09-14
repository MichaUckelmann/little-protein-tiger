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
# Derived, not hardcoded: this was one machine's absolute path, which is
# invisible until something reads a repo file through it. The Phase B
# analysis reads `config.yaml` for the dock gate, and its tests were the
# first thing here to do so, so they passed locally and failed CI with
# `FileNotFoundError: /home/.../little-protein-tiger/config.yaml`.
ROOT = Path(__file__).resolve().parents[1]
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


# --------------------------------------------------------------- Phase B ---
# Phase B refolds a CHOSEN SUBSET of Phase A's designs. The contrast is
# WITHIN-RUNG — patch-heavy against matched patch-light designs from the same
# rung — because a between-rung comparison varies exposure, target size, token
# count and segment count together, so "the smaller target was an easier
# design problem" explains any difference just as well (GLUE_PIPELINE_SCOPE.md,
# "Phase B — REVISED").
#
# Nothing here re-implements the pipeline: MPNN and RF3 run through
# `src/foundry_stages.py` exactly as the campaign driver invokes them, the
# per-refold metrics through `src.binder_metrics.score_campaign`, and the gates
# through `src.binder_ranking`'s own criteria list.

# Matching covariates, in the scope's priority order, as weights on z-scores.
#
# `engagement` is deliberately NOT here, though the scope lists it. It is a
# design-stage MEDIATOR, not a nuisance covariate: on 3KYS's bottom rung,
# median engagement falls from 11/12 to 9/12 and 37.7 % of designs land below
# the production gate, which IS the effect Phase B exists to measure.
# Matching on it would pair each patch-heavy design with the patch-light
# design whose engagement already matched, conditioning away part of the
# causal path and biasing every result toward null. Measured, not argued: with
# engagement in the weights the matcher still could only pair 0.958 against
# 0.833 at 3KYS rung 120, so it bought imbalance AND bias. Engagement is
# reported per arm instead, as a pre-refold outcome. `--match-engagement`
# restores the scope's original set for anyone who wants the comparison.
# `n_contacts` IS here, and it is the covariate that makes the contrast mean
# what it claims. Patch contacts are a subset of total target contacts, so
# without it the heavy arm is simply the stickier designs: matched only on
# length and clashes, 3KYS's heavy arms came out with HIGHER epitope
# engagement than their light partners (1.000 vs 0.875 at rung 173), which is
# the opposite of the hypothesis and is explained entirely by their making
# more contacts of every kind. Matched on total contacts, the question becomes
# the one worth asking: given the same number of target contacts, does having
# more of them on the fresh patch cost anything?
PHASEB_MATCH_WEIGHTS = {"binder_len": 4.0, "n_contacts": 3.0,
                        "sc_clashes": 2.0, "n_chainbreaks": 1.0}
PHASEB_MATCH_WEIGHTS_WITH_ENGAGEMENT = {"binder_len": 4.0, "n_contacts": 3.0,
                                        "engagement": 3.0, "sc_clashes": 2.0,
                                        "n_chainbreaks": 1.0}
PHASEB_MIN_NON_LOOP = 0.6
PHASEB_N_SEQ = 4
# Below this heavy-minus-light gap in patch contacts a rung cannot
# answer the question, and refolding it produces a null that reads
# like evidence of no effect.
PHASEB_MIN_SEPARATION = 2
# And below this many matched pairs a rung contributes nothing on its
# own — it can still be pooled, but it cannot be read alone.
PHASEB_MIN_PAIRS = 15


def _design_covariates(sidecar: Path) -> dict | None:
    """The matching covariates for one design, from its RFD3 sidecar.

    There is no binder-length key. `metrics.num_residues` counts the WHOLE
    complex and `diffused_index_map` carries one entry per FIXED (target)
    residue, so the binder is the difference — verified against the contig on
    a real sidecar (159 - 90 = 69 against contig `68-86`).

    The clash keys are FLAT dotted strings, the trap `prefilter_designs`
    documents: read as nested they return 0 for every design, which would
    turn the clash covariate into a constant and quietly drop it out of the
    matching.
    """
    try:
        d = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    m = d.get("metrics") or {}
    imap = d.get("diffused_index_map") or {}
    n_tot = m.get("num_residues")
    if n_tot is None or not imap:
        return None
    return {
        "binder_len": int(n_tot) - len(imap),
        "n_chainbreaks": int(m.get("n_chainbreaks", 0) or 0),
        "sc_clashes": int(
            m.get("n_clashing.interresidue_clashes_w_sidechain", 0) or 0),
        "bb_clashes": int(
            m.get("n_clashing.interresidue_clashes_w_backbone", 0) or 0),
        "non_loop_fraction": float(m.get("non_loop_fraction", 1.0) or 0.0),
    }


def _prefilter_ok(cov: dict, max_chainbreaks: int) -> bool:
    """The production prefilter's four criteria, on one design's sidecar.

    Selection restricts BOTH arms to prefilter survivors, for two reasons.
    It removes a confound — clashes and chainbreaks are exactly what the
    matching is trying to balance, and letting the prefilter drop them
    unevenly afterwards would undo it — and it makes the measured gate-pass
    rate the production-relevant one, since a real campaign never refolds a
    design the prefilter rejected. Thresholds mirror
    `foundry_runner.prefilter_designs`; `max_chainbreaks` is the rung's own
    segment count, the same derivation `write_campaign_driver` applies.
    """
    return (cov["n_chainbreaks"] <= max_chainbreaks
            and cov["sc_clashes"] <= 0
            and cov["bb_clashes"] <= 0
            and cov["non_loop_fraction"] >= PHASEB_MIN_NON_LOOP)


def _sidecar_for_design(rung_dir: Path, design: str) -> Path | None:
    """`design` in `design_scores.json` is a CIF stem, and for a `.cif.gz`
    that stem still ends in `.cif` (`Path("x.cif.gz").stem == "x.cif"`)."""
    stem = design[:-4] if design.endswith(".cif") else design
    hits = sorted(rung_dir.rglob(f"rfd3/**/{stem}.json"))
    return hits[0] if hits else None


def _zscale(rows: list[dict], keys) -> dict:
    """Per-covariate mean/SD over the rung, so the weights mean what they say.
    A zero-variance covariate gets SD 1.0 — it then contributes nothing rather
    than dividing by zero."""
    import statistics as st

    out = {}
    for k in keys:
        vals = [float(r[k]) for r in rows]
        sd = st.pstdev(vals) if len(vals) > 1 else 0.0
        out[k] = (st.mean(vals), sd if sd > 1e-9 else 1.0)
    return out


def _light_pool(cands: list[dict], n_arm: int,
                floor: int) -> tuple[list[dict], int]:
    """The patch-LIGHT candidate pool, defined by an absolute patch cap.

    Matching on covariates alone is not enough to define this arm, and the
    first version of this selector proved it: given the whole sub-floor
    remainder to choose from, `_match_arms` picked whichever design best
    matched binder length and engagement, which on 6VJJ meant partnering a
    3-contact "heavy" design with a 2-contact "light" one. A one-contact gap
    measures nothing.

    So the cap is chosen first — the SMALLEST patch-contact count that still
    leaves `n_arm` candidates strictly below the heavy arm's floor — and the
    covariate matching then happens inside that pool. Zero-contact designs
    are used whenever there are enough of them, which is the cleanest control
    the ladder can offer: same trim, same epitope, no patch contact at all.
    """
    below = [c for c in cands if c["patch_contacts"] < floor]
    for cap in range(0, max((c["patch_contacts"] for c in below), default=0) + 1):
        pool = [c for c in below if c["patch_contacts"] <= cap]
        if len(pool) >= n_arm:
            return pool, cap
    return below, floor - 1


def _match_arms(heavy: list[dict], pool: list[dict], scale: dict,
                weights: dict | None = None, *, caliper_contacts: int = 3,
                caliper_len: int = 3) -> tuple[list[dict], list[dict]]:
    """Greedy nearest-neighbour matching, without replacement, with calipers.

    Heavy designs are matched in descending patch-contact order, so the most
    informative ones get their pick of the pool. Distance is a weighted
    Euclidean over z-scored covariates — a lexicographic ordering would let a
    tie on binder length decide everything and ignore the rest.

    **The calipers are what make the arms comparable, and without them this
    function produced an actively misleading pairing.** Nearest-neighbour on
    its own always returns a partner: on 3KYS rung 120 the closest available
    patch-light design carried 36 target contacts against the heavy arm's 52,
    so the "effect" of patch contact would have been measured against designs
    making a third fewer contacts of every kind. A heavy design with no
    partner inside the caliper is DROPPED and reported, which turns a hidden
    bias into a visible loss of n — and a rung that loses most of its heavy
    arm is a rung whose contrast does not exist in the sampled population,
    which is worth knowing before spending GPU hours rather than after.
    """
    weights = weights or PHASEB_MATCH_WEIGHTS
    taken: set[str] = set()
    matched, unmatched = [], []
    for h in sorted(heavy, key=lambda r: -r["patch_contacts"]):
        best, best_d = None, None
        for c in pool:
            if c["design"] in taken:
                continue
            if abs(int(h["n_contacts"]) - int(c["n_contacts"])) > caliper_contacts:
                continue
            if abs(int(h["binder_len"]) - int(c["binder_len"])) > caliper_len:
                continue
            d = 0.0
            for name, w in weights.items():
                _mu, sd = scale[name]
                d += w * ((float(h[name]) - float(c[name])) / sd) ** 2
            if best_d is None or d < best_d:
                best, best_d = c, d
        if best is None:
            unmatched.append(h)
            continue
        taken.add(best["design"])
        matched.append({**best, "matched_to": h["design"],
                        "match_distance": round(best_d ** 0.5, 4)})
    return matched, unmatched


def phaseb_select(out_root: Path, *, rungs: list[int], control_rung: int | None,
                  single_arm: list[int], n_arm: int = 40,
                  n_control: int = 100,
                  match_engagement: bool = False,
                  caliper_contacts: int = 3,
                  caliper_len: int = 3) -> dict:
    """Choose the designs Phase B will refold, and write `phaseb_arms.json`.

    Reads Phase A's own `design_scores.json` for the patch-contact counts, so
    the selection variable is the measured one and not a second derivation.
    Patch contacts per design are `round(patch_contact_fraction *
    n_contacts)` — `design_scores.json` stores the fraction, and `n_patch`
    there is the rung's patch SIZE, not a per-design count.
    """
    ladder = json.loads((out_root / "ladder.json").read_text(encoding="utf-8"))
    scores = json.loads(
        (out_root / "design_scores.json").read_text(encoding="utf-8"))
    seg_of = {int(r["budget"]): int(r.get("n_segments", 1))
              for r in ladder["rungs"] if "spec_path" in r}
    tok_of = {int(r["budget"]): int(r["n_tokens"])
              for r in ladder["rungs"] if "spec_path" in r}
    by_rung: dict[int, list[dict]] = {}
    for r in scores:
        by_rung.setdefault(int(r["rung"]), []).append(r)

    out = {"ladder": out_root.name, "rungs": []}
    wanted = [(b, "paired") for b in rungs]
    wanted += [(b, "single") for b in single_arm]
    if control_rung is not None:
        wanted.append((control_rung, "control"))
    for budget, kind in wanted:
        rows = by_rung.get(budget)
        if not rows:
            print(f"  rung {budget}: no design scores — skipped", flush=True)
            continue
        rung_dir = out_root / f"rung_{budget}"
        cands = []
        for r in rows:
            sidecar = _sidecar_for_design(rung_dir, r["design"])
            if sidecar is None:
                continue
            cov = _design_covariates(sidecar)
            if cov is None or not _prefilter_ok(cov, seg_of.get(budget, 1)):
                continue
            cif = _cif_for(sidecar)
            if cif is None:
                continue
            cands.append({
                "design": r["design"], "sidecar": str(sidecar), "cif": str(cif),
                "patch_contacts": round(r["patch_contact_fraction"]
                                        * r["n_contacts"]),
                "patch_fraction": r["patch_contact_fraction"],
                "n_contacts": r["n_contacts"],
                "engagement": r["hotspot_engagement_design"] or 0.0,
                "binder_len": cov["binder_len"],
                "sc_clashes": cov["sc_clashes"],
                "n_chainbreaks": cov["n_chainbreaks"],
            })
        if not cands:
            print(f"  rung {budget}: no prefilter survivors — skipped",
                  flush=True)
            continue
        scale = _zscale(cands, ("binder_len", "n_contacts", "engagement",
                                "sc_clashes", "n_chainbreaks"))
        entry = {"budget": budget, "kind": kind, "n_tokens": tok_of.get(budget),
                 "n_segments": seg_of.get(budget, 1),
                 "n_prefilter_survivors": len(cands), "arms": {}}
        ranked = sorted(cands, key=lambda r: (-r["patch_contacts"],
                                              -r["patch_fraction"]))
        if kind == "control":
            entry["arms"]["control"] = [dict(c) for c in ranked[:n_control]]
        elif kind == "single":
            entry["arms"]["heavy"] = [dict(c) for c in ranked[:n_arm]]
        else:
            heavy = [dict(c) for c in ranked[:n_arm]]
            floor = min(h["patch_contacts"] for h in heavy)
            pool, cap = _light_pool(cands, n_arm, floor)
            entry["light_patch_cap"] = cap
            light, unmatched = _match_arms(
                heavy, pool, scale,
                PHASEB_MATCH_WEIGHTS_WITH_ENGAGEMENT if match_engagement
                else PHASEB_MATCH_WEIGHTS,
                caliper_contacts=caliper_contacts, caliper_len=caliper_len)
            dropped = {u["design"] for u in unmatched}
            # Only PAIRS go to the GPU: an unmatched heavy design would cost
            # a refold and contribute to no comparison.
            entry["arms"]["heavy"] = [h for h in heavy
                                      if h["design"] not in dropped]
            entry["arms"]["light"] = light
            entry["n_unmatched_heavy"] = len(unmatched)
            if unmatched:
                print(f"  rung {budget}: {len(unmatched)} of {len(heavy)} "
                      f"heavy designs had no partner within the caliper "
                      f"(+/-{caliper_contacts} contacts, +/-{caliper_len} "
                      f"residues) and were dropped", flush=True)
        import statistics as st
        entry["separation"] = {
            arm: {"n": len(v),
                  "median_patch_contacts": st.median(
                      x["patch_contacts"] for x in v) if v else None,
                  "median_binder_len": st.median(
                      x["binder_len"] for x in v) if v else None,
                  "median_engagement": round(st.median(
                      x["engagement"] for x in v), 4) if v else None,
                  "median_n_contacts": st.median(
                      x["n_contacts"] for x in v) if v else None}
            for arm, v in entry["arms"].items()}
        n_pairs = len(entry["arms"].get("light") or [])
        if entry["kind"] == "paired" and n_pairs < PHASEB_MIN_PAIRS:
            entry["weak"] = True
            print(f"  rung {budget}: WEAK — only {n_pairs} matched pairs, "
                  f"below the {PHASEB_MIN_PAIRS} this rung needs to "
                  f"contribute. Its contrast does not exist in the sampled "
                  f"population; refold it only to pool with other rungs.",
                  flush=True)
        h = (entry["separation"].get("heavy") or {}).get(
            "median_patch_contacts")
        l = (entry["separation"].get("light") or {}).get(
            "median_patch_contacts")
        if h is not None and l is not None:
            entry["separation_gap"] = h - l
            if h - l < PHASEB_MIN_SEPARATION:
                entry["weak"] = True
                print(f"  rung {budget}: WEAK — heavy median {h} vs light "
                      f"{l} patch contacts is a gap of {h - l}, below the "
                      f"{PHASEB_MIN_SEPARATION} this rung would need to "
                      f"measure anything. Refolding it buys a null result "
                      f"that means nothing.", flush=True)
        out["rungs"].append(entry)
        sep = "  ".join(
            f"{a}: n={s['n']} patch={s['median_patch_contacts']}"
            for a, s in entry["separation"].items())
        print(f"  rung {budget} ({kind}, {len(cands)} prefilter survivors of "
              f"{len(rows)}): {sep}", flush=True)

    (out_root / "phaseb_arms.json").write_text(
        json.dumps(out, indent=1), encoding="utf-8")
    n_designs = sum(len(v) for e in out["rungs"] for v in e["arms"].values())
    print(f"\nwrote {out_root / 'phaseb_arms.json'}: {n_designs} designs, "
          f"{n_designs * PHASEB_N_SEQ} refolds expected", flush=True)
    return out


def phaseb_launch(out_root: Path, *, dry_run: bool = True,
                  only: int | None = None) -> None:
    """One detached MPNN+RF3 job per rung, over the selected designs only.

    The subset mechanism is a directory of symlinks, which is exactly what
    the production prefilter hands MPNN (`foundry_stages.cmd_mpnn` scandirs a
    directory for `*.cif.gz`). The SIDECAR is symlinked beside each design
    too: `binder_metrics.score_campaign` resolves both the design CIF and its
    RFD3 sidecar out of the design dir, and without the sidecar every
    hotspot/chainbreak column comes back empty.

    Each rung gets its own parent directory because `cmd_rf3 --skip-existing`
    stages symlinks into `<out_dir>.parent/".rf3_staging"` and wipes what it
    finds there — two rungs sharing a parent would delete each other's queue.
    """
    import yaml

    from src.env_config import resolve_env_path
    from src.foundry_runner import (FoundryValidationError, _resolve_foundry_bin,
                                    rf3_seconds_per_refold)

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    design = cfg.get("design") or {}
    f_cfg = design.get("foundry") or {}
    foundry_root = resolve_env_path("LPT_FOUNDRY_ROOT", f_cfg.get("root"))
    if not foundry_root:
        raise FoundryValidationError(
            "foundry root is not set — set LPT_FOUNDRY_ROOT in .env or "
            "design.foundry.root in config.yaml")
    foundry_root = Path(foundry_root)
    # Resolve BOTH binaries up front: a wrong venv name must fail here, not
    # inside a detached job hours later (the Phase A lesson).
    mpnn_bin = _resolve_foundry_bin(
        foundry_root, f_cfg.get("mpnn_bin", ".venv-blackwell/bin/mpnn"), "mpnn")
    rf3_bin = _resolve_foundry_bin(
        foundry_root, f_cfg.get("rf3_bin", ".venv-blackwell/bin/rf3"), "rf3")
    ckpt_dir = resolve_env_path("LPT_FOUNDRY_CKPT_DIR", f_cfg.get("ckpt_dir"))

    arms = json.loads(
        (out_root / "phaseb_arms.json").read_text(encoding="utf-8"))
    total_h = 0.0
    for entry in arms["rungs"]:
        budget = int(entry["budget"])
        if only is not None and budget != only:
            continue
        work = out_root / "phaseb" / f"rung_{budget}"
        sel, mpnn_out, rf3_out = (work / "designs_selected",
                                  work / "mpnn_out", work / "rf3_out")
        logs = work / "logs"
        for d in (sel, mpnn_out, rf3_out, logs):
            d.mkdir(parents=True, exist_ok=True)
        members = [m for v in entry["arms"].values() for m in v]
        for m in members:
            for src in (Path(m["cif"]), Path(m["sidecar"])):
                link = sel / src.name
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(src)
        n_refolds = len(members) * PHASEB_N_SEQ
        est = n_refolds * rf3_seconds_per_refold(
            n_tokens=entry.get("n_tokens")) / 3600
        total_h += est
        py = sys.executable
        script = work / "run_phaseb.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "# Phase B: MPNN + RF3 over ONE rung's selected designs.\n"
            "# Same two stage commands the campaign driver writes; the only\n"
            "# difference is that the design dir holds a chosen subset.\n"
            "set -euo pipefail\n"
            f'cd "{ROOT}"\n'
            f'export CUDA_VISIBLE_DEVICES="0"\n'
            f'"{py}" "{ROOT}/src/foundry_stages.py" mpnn "{sel}" "{mpnn_out}"'
            f' --checkpoint "solublempnn" --n-seq {PHASEB_N_SEQ}'
            f' --chunk-size 250 --foundry "{foundry_root}"'
            f' --mpnn-bin "{mpnn_bin}"'
            + (f' --ckpt-dir "{ckpt_dir}"' if ckpt_dir else "")
            + f' --skip-existing >> "{logs}/mpnn.log" 2>&1\n'
            f'"{py}" "{ROOT}/src/foundry_stages.py" rf3 "{mpnn_out}"'
            f' "{rf3_out}" --checkpoint "rf3" --template "target"'
            f' --diffusion-batch-size 1 --seed 0 --foundry "{foundry_root}"'
            f' --rf3-bin "{rf3_bin}" --skip-existing'
            f' >> "{logs}/rf3.log" 2>&1\n', encoding="utf-8")
        script.chmod(0o755)
        print(f"  rung {budget}: {len(members)} designs -> {n_refolds} refolds,"
              f" ~{est:.2f} GPU-h  {script}", flush=True)
        if not dry_run:
            from src.job_registry import JobRegistry

            rec = JobRegistry(work / "jobs.json").launch(
                f"phaseb_rung_{budget}", ["bash", str(script)],
                cwd=str(work), log_path=logs / "stage.log",
                note=f"Phase B rung {budget}")
            print(f"    launched: pid {rec.pid} -> {logs}/stage.log",
                  flush=True)
    print(f"\ntotal across rungs: ~{total_h:.2f} GPU-h"
          + ("  (dry run — nothing launched)" if dry_run else ""), flush=True)


def phaseb_score(out_root: Path, cutoff: float = 8.0,
                 workers: int = 0) -> list[dict]:
    """Score every completed Phase B refold, and label it with its arm.

    Metrics come from `binder_metrics.score_campaign` — the same call
    `_score_campaign` makes for a real campaign — and the gate verdict from
    `binder_ranking`'s own criteria list, so a threshold change in
    `config.yaml` moves this readout with it.

    The one thing computed here is PATCH SURVIVAL: the fraction of a design's
    own patch contacts still present in its refold. Both sides use
    `binder_backbone_only=True`, unlike Phase A's design-stage count, because
    RFD3's binder sidechains belong to RFD3's sequence and not to the MPNN
    sequence being refolded — comparing sidechain contacts across the two
    would measure the sequence change, not the pose.
    """
    import os

    import yaml

    from src.binder_metrics import (ScoreConfig, epitope, hotspots_from_rfd3,
                                    read_structure, score_campaign)
    from src.binder_ranking import DEFAULT_THRESHOLDS, _criteria

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    rcfg = (((cfg.get("design") or {}).get("binder_ranking")) or {})
    crit = _criteria({**DEFAULT_THRESHOLDS, **(rcfg.get("thresholds") or {})})
    ladder = json.loads((out_root / "ladder.json").read_text(encoding="utf-8"))
    patch_of = {int(r["budget"]): ({int(a) for _n, a, _d in r["patch_away"]}
                                   | {int(a) for _n, a, _d in r["patch_near"]})
                for r in ladder["rungs"] if "spec_path" in r}
    arms = json.loads(
        (out_root / "phaseb_arms.json").read_text(encoding="utf-8"))
    workers = workers or max(1, (os.cpu_count() or 4) - 2)
    rows: list[dict] = []
    for entry in arms["rungs"]:
        budget = int(entry["budget"])
        work = out_root / "phaseb" / f"rung_{budget}"
        rf3_out, sel = work / "rf3_out", work / "designs_selected"
        if not rf3_out.is_dir():
            print(f"  rung {budget}: no refolds yet", flush=True)
            continue
        arm_of = {m["design"]: arm for arm, v in entry["arms"].items()
                  for m in v}
        # design_family strips MPNN's `_b<k>_d<k>`; the arm keys are CIF stems
        # ending in `.cif` (a `.cif.gz` stem), so index on both spellings.
        arm_of.update({k[:-4]: v for k, v in arm_of.items()
                       if k.endswith(".cif")})
        sidecars = sorted(sel.glob("*_model_*.json"))
        if not sidecars:
            print(f"  rung {budget}: no sidecars in {sel}", flush=True)
            continue
        hotspots = hotspots_from_rfd3(sidecars[0], "B")
        scored = score_campaign(
            rf3_out, sel, hotspots=hotspots,
            cfg=ScoreConfig(contact_cutoff=cutoff), workers=workers,
            with_ipsae=True)
        patch_cache: dict[str, set] = {}
        for r in scored:
            fam = r.get("design_family") or ""
            r["rung"] = budget
            r["arm"] = arm_of.get(fam) or arm_of.get(f"{fam}.cif")
            r["gate_pass"] = (not r.get("error")
                              and all(c(r) for _l, c in crit))
            r["gate_first_fail"] = next(
                (l for l, c in crit if not c(r)), None)
            r["patch_survival"] = None
            try:
                dcif, pcif = r.get("design_cif"), r.get("refold_cif")
                if not (dcif and pcif) or r.get("error"):
                    continue
                if fam not in patch_cache:
                    car = sel / f"{fam}.json"
                    imap = (json.loads(car.read_text(encoding="utf-8"))
                            .get("diffused_index_map") or {}) if car.exists() \
                        else {}
                    out_of = {}
                    for k, v in imap.items():
                        try:
                            out_of[int(k[1:])] = int(str(v)[1:])
                        except (ValueError, IndexError):
                            continue
                    patch_out = {out_of[a] for a in patch_of.get(budget, set())
                                 if a in out_of}
                    des = epitope(read_structure(Path(dcif)), "A", "B", cutoff,
                                  binder_backbone_only=True)
                    patch_cache[fam] = (patch_out, des & patch_out)
                patch_out, des_patch = patch_cache[fam]
                r["n_patch_design"] = len(des_patch)
                if des_patch:
                    prd = epitope(read_structure(Path(pcif)), "A", "B", cutoff,
                                  binder_backbone_only=True)
                    r["patch_survival"] = round(
                        len(des_patch & prd) / len(des_patch), 4)
            except Exception as exc:                              # noqa: BLE001
                r["patch_survival_error"] = f"{type(exc).__name__}: {exc}"
            finally:
                rows.append(r)
        n_lab = sum(1 for r in scored if r.get("arm"))
        print(f"  rung {budget}: scored {len(scored)} refolds "
              f"({n_lab} arm-labelled)", flush=True)
    (out_root / "phaseb_scores.json").write_text(
        json.dumps(rows, indent=1, default=str), encoding="utf-8")
    print(f"\nwrote {out_root / 'phaseb_scores.json'} ({len(rows)} rows)",
          flush=True)
    return rows


def _design_level(rows: list[dict]) -> list[dict]:
    """One record per RFD3 design, chosen the way production chooses.

    `binder_ranking.composite_score` + one per `design_family` is exactly
    `max_per_backbone=1`, so the unit matches what a campaign would report.
    Picking the best refold per metric instead would cherry-pick a different
    design for each column.
    """
    from src.binder_ranking import DEFAULT_Z_CLIP, composite_score

    usable = [r for r in rows if not r.get("error")]
    if not usable:
        return []
    composite_score(usable, z_clip=DEFAULT_Z_CLIP)
    best: dict[str, dict] = {}
    for r in usable:
        fam = r.get("design_family") or r.get("name")
        cur = best.get(fam)
        if cur is None or (r.get("composite_score") or float("-inf")) > (
                cur.get("composite_score") or float("-inf")):
            best[fam] = r
    return list(best.values())


def _dock_fraction(rows: list[dict], thresh: float) -> dict:
    """Fraction of records docked within `thresh` A, with a Wilson interval.

    **This, not the median, is the dock summary to read**, because the
    distribution is strongly BIMODAL: on 3KYS rung 173, refolds sit either
    under 5 A or beyond 30 A, with 2 of 104 in between. The median then
    reports which side of the gap the middle record happens to fall on — the
    design-level medians came out 20.94 A (heavy) against 2.26 A (light),
    a 9x "difference" that a rank test put at p=0.21, while the two refold
    distributions were 32.8 vs 30.5 A and the real contrast was 28 % vs 37 %
    of refolds docking at all. A median of a bimodal distribution is a coin
    flip dressed as an effect size.
    """
    from src.campaign_calibration import wilson_interval

    vals = [r["binder_rmsd_dock"] for r in rows
            if isinstance(r.get("binder_rmsd_dock"), (int, float))]
    if not vals:
        return {"n": 0, "frac": None}
    k = sum(1 for v in vals if v <= thresh)
    lo, hi = wilson_interval(k, len(vals))
    return {"n": len(vals), "k": k, "frac": round(k / len(vals), 4),
            "ci95": [round(lo, 4), round(hi, 4)], "threshold_A": thresh}


def _is_bimodal(rows: list[dict], lo: float = 5.0, hi: float = 15.0) -> bool:
    """True when both tails are populated and the middle is nearly empty.

    Deliberately crude — it exists to stop a median being quoted as an effect,
    not to characterise the distribution.
    """
    vals = [r["binder_rmsd_dock"] for r in rows
            if isinstance(r.get("binder_rmsd_dock"), (int, float))]
    if len(vals) < 10:
        return False
    below = sum(1 for v in vals if v <= lo)
    middle = sum(1 for v in vals if lo < v < hi)
    above = sum(1 for v in vals if v >= hi)
    return below >= 3 and above >= 3 and middle <= 0.1 * len(vals)


def _medians(rows: list[dict]) -> dict:
    """Median of each reported metric over one arm, skipping missing values.

    `binder_rmsd_dock` is the one that matters most: iPTM reports confidence
    in whatever interface the model chose, so a mis-docked design can carry a
    high iPTM, and the dock RMSD is what separates them (CLAUDE.md, "RF3
    templating cannot convey a docked pose").
    """
    import statistics as st

    out = {}
    for key in ("iptm", "binder_rmsd_dock", "binder_plddt", "ipsae_min",
                "hotspot_engagement", "patch_survival"):
        vals = [r[key] for r in rows
                if isinstance(r.get(key), (int, float))]
        out[key] = round(st.median(vals), 4) if vals else None
    return out


def _mwu(a: list[float], b: list[float]) -> dict:
    from scipy.stats import mannwhitneyu

    a = [x for x in a if x is not None]
    b = [x for x in b if x is not None]
    if len(a) < 3 or len(b) < 3:
        return {"n_a": len(a), "n_b": len(b), "p": None}
    import statistics as st

    u, p = mannwhitneyu(a, b, alternative="two-sided")
    return {"n_a": len(a), "n_b": len(b), "median_a": round(st.median(a), 4),
            "median_b": round(st.median(b), 4), "U": float(u),
            "p": round(float(p), 5)}


def phaseb_analyze(out_root: Path) -> dict:
    """The pre-registered readout: per rung, then pooled.

    Design-level is primary (4 sequences off one backbone are correlated);
    refold-level is reported as the secondary, better-powered test. The
    gate-pass ratio carries Wilson intervals on each arm and a Katz log
    interval on the ratio — `campaign_calibration.wilson_interval` is the
    pipeline's own, so the interval here and the one in a SCALE_UP verdict are
    computed the same way.
    """
    import math
    import statistics as st

    from src.campaign_calibration import wilson_interval

    import yaml

    from src.binder_ranking import DEFAULT_THRESHOLDS

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    thr = {**DEFAULT_THRESHOLDS,
           **((((cfg.get("design") or {}).get("binder_ranking")) or {})
              .get("thresholds") or {})}
    # The same number the gate uses, so the reported fraction and the
    # gate-pass rate cannot disagree about what "docked" means.
    dock_max = float(thr.get("binder_rmsd_dock_max") or 5.0)
    rows = json.loads(
        (out_root / "phaseb_scores.json").read_text(encoding="utf-8"))
    by_rung: dict[int, list[dict]] = {}
    for r in rows:
        by_rung.setdefault(int(r["rung"]), []).append(r)

    def _rate(rs):
        k = sum(1 for r in rs if r.get("gate_pass"))
        lo, hi = wilson_interval(k, len(rs)) if rs else (0.0, 0.0)
        return {"k": k, "n": len(rs),
                "rate": round(k / len(rs), 4) if rs else None,
                "ci95": [round(lo, 4), round(hi, 4)]}

    def _ratio(h, l):
        if not (h["k"] and l["k"]):
            return {"point": None, "note": "a zero cell — ratio undefined; "
                                           "read the two Wilson intervals"}
        rr = (h["k"] / h["n"]) / (l["k"] / l["n"])
        se = math.sqrt(1 / h["k"] - 1 / h["n"] + 1 / l["k"] - 1 / l["n"])
        return {"point": round(rr, 3),
                "ci95": [round(rr * math.exp(-1.96 * se), 3),
                         round(rr * math.exp(1.96 * se), 3)]}

    out = {"rungs": [], "pooled": {}, "single_arm_rungs": []}
    pooled = {"heavy": [], "light": [], "control": []}
    pooled_d = {"heavy": [], "light": [], "control": []}
    for budget in sorted(by_rung, reverse=True):
        rs = by_rung[budget]
        arms = {a: [r for r in rs if r.get("arm") == a]
                for a in ("heavy", "light", "control")}
        dl = {a: _design_level(v) for a, v in arms.items()}
        # ONLY a rung with both arms enters the paired pool. A saturated rung
        # contributes an unmatched heavy arm (3KYS rung 90: 40 designs, no
        # patch-light design exists to match), and pooling it in compares
        # those designs against light designs from OTHER rungs — the
        # confounded between-rung comparison this whole design exists to
        # avoid. It is not a small effect either: including 3KYS rung 90's
        # 0/40 moved the pooled gate ratio from 0.882 (no effect) to 0.526
        # with an interval excluding 1.0, and the dock U-test from p=0.34 to
        # p=0.013. A single-arm rung is reported on its own, against the
        # control, and never inside a paired statistic.
        if dl["heavy"] and dl["light"]:
            for a in pooled:
                pooled[a].extend(arms[a])
                pooled_d[a].extend(dl[a])
        else:
            pooled["control"].extend(arms["control"])
            pooled_d["control"].extend(dl["control"])
            if dl["heavy"]:
                out["single_arm_rungs"].append(budget)
        entry = {"budget": budget,
                 "design_level": {a: _rate(v) for a, v in dl.items() if v},
                 "refold_level": {a: _rate(v) for a, v in arms.items() if v},
                 # Per-arm medians are computed here, NOT read out of the
                 # Mann-Whitney block: that block only exists for a paired
                 # rung, so a control or single arm would otherwise report no
                 # numbers at all — and the control IS the baseline the
                 # paired arms are read against.
                 "medians": {a: _medians(v) for a, v in dl.items() if v},
                 "dock_ok": {a: _dock_fraction(v, dock_max)
                             for a, v in dl.items() if v},
                 "dock_bimodal": any(_is_bimodal(v) for v in arms.values())}
        if dl["heavy"] and dl["light"]:
            entry["iptm"] = _mwu([r.get("iptm") for r in dl["heavy"]],
                                 [r.get("iptm") for r in dl["light"]])
            entry["binder_rmsd_dock"] = _mwu(
                [r.get("binder_rmsd_dock") for r in dl["heavy"]],
                [r.get("binder_rmsd_dock") for r in dl["light"]])
            entry["gate_ratio_design"] = _ratio(entry["design_level"]["heavy"],
                                                entry["design_level"]["light"])
        for a, v in arms.items():
            surv = [r["patch_survival"] for r in v
                    if r.get("patch_survival") is not None]
            if surv:
                entry.setdefault("patch_survival", {})[a] = {
                    "n": len(surv), "median": round(st.median(surv), 4),
                    "mean": round(st.mean(surv), 4)}
        out["rungs"].append(entry)

    dl = {a: v for a, v in pooled_d.items() if v}
    out["pooled"] = {
        "design_level": {a: _rate(v) for a, v in dl.items()},
        "refold_level": {a: _rate(v) for a, v in pooled.items() if v},
        "medians": {a: _medians(v) for a, v in dl.items()},
        "dock_ok": {a: _dock_fraction(v, dock_max) for a, v in dl.items()},
        "dock_bimodal": any(_is_bimodal(v) for v in pooled.values() if v)}
    if dl.get("heavy") and dl.get("light"):
        out["pooled"]["iptm"] = _mwu([r.get("iptm") for r in dl["heavy"]],
                                     [r.get("iptm") for r in dl["light"]])
        out["pooled"]["binder_rmsd_dock"] = _mwu(
            [r.get("binder_rmsd_dock") for r in dl["heavy"]],
            [r.get("binder_rmsd_dock") for r in dl["light"]])
        out["pooled"]["gate_ratio_design"] = _ratio(
            out["pooled"]["design_level"]["heavy"],
            out["pooled"]["design_level"]["light"])
    (out_root / "phaseb_stats.json").write_text(
        json.dumps(out, indent=1), encoding="utf-8")
    _print_phaseb(out)
    return out


def _print_phaseb(stats: dict) -> None:
    print(f"\n{'rung':>6} {'arm':>8} {'designs':>8} {'pass':>6} {'rate':>7} "
          f"{'95% CI':>15} {'med iptm':>9} {'dock<=5':>8} {'~med dock':>10} "
          f"{'patch surv':>10}")
    for e in stats["rungs"]:
        for arm in ("heavy", "light", "control"):
            d = e["design_level"].get(arm)
            if not d:
                continue
            med = (e.get("medians") or {}).get(arm) or {}
            med_i, med_d = med.get("iptm"), med.get("binder_rmsd_dock")
            surv = (e.get("patch_survival") or {}).get(arm, {}).get("median")
            dok = ((e.get("dock_ok") or {}).get(arm) or {}).get("frac")
            c_i = f"{med_i:.3f}" if med_i is not None else "-"
            c_k = f"{dok:.2f}" if dok is not None else "-"
            c_d = f"{med_d:.2f}" if med_d is not None else "-"
            c_s = f"{surv:.3f}" if surv is not None else "-"
            rate = d["rate"] if d["rate"] is not None else 0
            print(f"{e['budget']:>6} {arm:>8} {d['n']:>8} {d['k']:>6} "
                  f"{rate:>7.3f} {str(d['ci95']):>15} "
                  f"{c_i:>9} {c_k:>8} {c_d:>10} {c_s:>10}")
        if e.get("iptm", {}).get("p") is not None:
            print(f"{'':>6} heavy vs light: iptm p={e['iptm']['p']:.4f}, "
                  f"dock p={e['binder_rmsd_dock']['p']:.4f}, "
                  f"gate ratio {e.get('gate_ratio_design', {}).get('point')}")
    if stats.get("single_arm_rungs"):
        print(f"\nrung(s) {stats['single_arm_rungs']} are SATURATED — no "
              f"patch-light design exists to match, so they contribute one "
              f"unmatched arm and are EXCLUDED from the pooled comparison "
              f"below. Read them against the control rung only, and remember "
              f"they differ from it in target size and segment count too.")
    p = stats["pooled"]
    if p.get("iptm"):
        print(f"\npooled (design level): iptm p={p['iptm']['p']}, "
              f"dock p={p['binder_rmsd_dock']['p']}, "
              f"gate ratio {p['gate_ratio_design']}")
    if any(e.get("dock_bimodal") for e in stats["rungs"]):
        print("\nNOTE: the dock-RMSD distribution is BIMODAL (docked under "
              "~5 A or lost beyond ~30 A, almost nothing between), so "
              "`~med dock` is marked and must not be read as an effect size "
              "— the middle record only reports which side of the gap it fell "
              "on. Read `dock<=5` and the U test instead.")
    print("\nThe decision rule is pre-registered in GLUE_PIPELINE_SCOPE.md "
          "(\"Pre-registered decision rule\") — read it BEFORE these numbers.")


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

    s = sub.add_parser("phaseb-select",
                       help="choose Phase B's paired arms from Phase A's scores")
    s.add_argument("--out", required=True)
    s.add_argument("--rungs", default="",
                   help="comma-separated budgets to PAIR (heavy vs matched "
                        "light), e.g. 117,106,90")
    s.add_argument("--control-rung", type=int, default=None,
                   help="the no-trim rung, taken as one unmatched arm")
    s.add_argument("--single-arm", default="",
                   help="budgets contributing ONE arm only — saturated rungs "
                        "where no patch-light designs exist to match")
    s.add_argument("--n-arm", type=int, default=40)
    s.add_argument("--n-control", type=int, default=100)
    s.add_argument("--caliper-contacts", type=int, default=3,
                   help="max allowed difference in TOTAL target contacts "
                        "between a matched pair; heavy designs with no "
                        "partner inside it are dropped")
    s.add_argument("--caliper-len", type=int, default=3,
                   help="max allowed binder-length difference in a pair")
    s.add_argument("--match-engagement", action="store_true",
                   help="also match on design-stage hotspot engagement. OFF "
                        "by default: it is a mediator, not a nuisance "
                        "covariate — see PHASEB_MATCH_WEIGHTS.")

    s = sub.add_parser("phaseb-launch",
                       help="one detached MPNN+RF3 job per rung (GPU)")
    s.add_argument("--out", required=True)
    s.add_argument("--launch", action="store_true",
                   help="actually launch; without it, plan and print only")
    s.add_argument("--rung", type=int, default=None,
                   help="launch ONE rung. Rungs must not share the card.")

    s = sub.add_parser("phaseb-score", help="score Phase B's refolds (CPU)")
    s.add_argument("--out", required=True)
    s.add_argument("--cutoff", type=float, default=8.0)
    s.add_argument("--workers", type=int, default=0)

    s = sub.add_parser("phaseb-analyze",
                       help="the pre-registered Phase B readout (CPU)")
    s.add_argument("--out", required=True)

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
    elif a.cmd == "phaseb-select":
        phaseb_select(
            Path(a.out),
            rungs=[int(x) for x in a.rungs.split(",") if x.strip()],
            control_rung=a.control_rung,
            single_arm=[int(x) for x in a.single_arm.split(",") if x.strip()],
            n_arm=a.n_arm, n_control=a.n_control,
            match_engagement=a.match_engagement,
            caliper_contacts=a.caliper_contacts, caliper_len=a.caliper_len)
    elif a.cmd == "phaseb-launch":
        phaseb_launch(Path(a.out), dry_run=not a.launch, only=a.rung)
    elif a.cmd == "phaseb-score":
        phaseb_score(Path(a.out), a.cutoff, workers=a.workers)
    elif a.cmd == "phaseb-analyze":
        phaseb_analyze(Path(a.out))
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
