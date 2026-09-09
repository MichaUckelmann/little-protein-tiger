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
import json, sys, traceback, warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path("/home/m.uckelmann_cbs-niob.local/code/little-protein-tiger")
sys.path.insert(0, str(ROOT))

import gemmi  # noqa: E402
from src.structure_tools import analyze_interface, is_solvent_or_additive  # noqa: E402
from src.structure_trim import trim_target, write_trimmed  # noqa: E402
from src.foundry_spec import MAX_HOTSPOTS  # noqa: E402

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
    return [{"auth_seq_id": r} for r in sorted(chosen)], iface


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


if __name__ == "__main__":
    files = [Path(a) for a in sys.argv[1:]]
    rows = []
    for f in files:
        try:
            rows.append(run(f))
            print(f"  ok   {f.name}", flush=True)
        except Exception as exc:
            rows.append({"file": f.name, "error": f"{type(exc).__name__}: {exc}"})
            print(f"  FAIL {f.name}: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc(limit=2)
    Path("/tmp/bench_out/results.json").write_text(json.dumps(rows, indent=1, default=str))
    print(f"\nwrote /tmp/bench_out/results.json  ({len(rows)} rows)")
