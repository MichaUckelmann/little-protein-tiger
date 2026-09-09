#!/usr/bin/env python3
"""Place the lead CGRP-receptor design onto the FULL-LENGTH receptor (6E3Y).

The campaign designed against 3N7S — the 2.1 A CALCRL/RAMP1 extracellular
domain complex, 115 residues, no membrane, no peptide. 6E3Y is the 3.3 A
cryo-EM structure of the whole signalling assembly: CALCRL's seven-
transmembrane bundle, RAMP1 alongside it, the CGRP agonist threaded into the
extracellular face, and the Gs heterotrimer underneath.

Superposing the design's own refold onto 6E3Y by its target chain answers a
question the campaign never asked: on the real receptor, in the membrane, does
the binder sit where the agonist binds?

    .venv/bin/python docs/showcase/render_hero.py

Writes `assets/hero_receptor.webp` (design on the full receptor),
`assets/hero_apo.webp` (the same camera, binder hidden — the before) and
`assets/turntable/hero_NNN.png` for the launch video.

WHAT THIS IMAGE IS, AND IS NOT. It is a superposition, not a docking run and
not a modelled ternary complex: 6E3Y took no part in the campaign, and the
overlap it reveals is therefore an independent check rather than a restatement
of the pipeline's own output — the same role PDB 4ZQK plays for PD-L1 on the
campaign page. It is also not a surprise: the interface stage deliberately
aimed at the CGRP-contacting epitope, so the site was CHOSEN. What is
independently confirmed here is OCCUPANCY — that the binder the pipeline
actually produced lands on that site with the agonist's own footprint, which
the campaign, working on an ECD-only crystal form with no peptide present,
had no way to score for.

Conventions this script has to get right, each capable of a plausible wrong
picture (see docs/showcase/README.md):

  * RF3 refolds renumber the target from 1; the deposited entry uses author
    numbering. The offset comes from the trim's own `kept_segments` and is
    then CHECKED residue-name by residue-name against both structures.
  * Binder is chain A and target chain B in a refold — the opposite of the
    deposited complexes.
  * ChimeraX writes no WebP and exits 0 after failing to save, so renders go
    to PNG and failure is detected by scanning the output for ERROR.
"""
from __future__ import annotations

import csv
import json
import math
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ASSETS = HERE / "assets"
BINDER = ROOT / "projects/pain_receptors_v3/runs/round-1/binder"
FULL = ROOT / "data/structures/6E3Y.cif"

CHIMERAX = "/usr/bin/chimerax"
W, H = 1400, 1750          # portrait: a membrane receptor is a tall object
TURN_W, TURN_H = 1080, 1080
TURN_FRAMES = 90        # 3 s at 30 fps

# Same palette as the showcase figures, plus one new hue for the agonist.
BINDER_COL = "#2f8f74"     # the design
TARGET_COL = "#9aa79d"     # CALCRL + RAMP1
AGONIST_COL = "#b2503c"    # CGRP, the peptide the design has to displace
HOTSPOT_COL = "#c0872b"

# 6E3Y chains. R is CALCRL (full length, 7TM included), E is RAMP1, P is the
# CGRP agonist; A/B/G are the Gs heterotrimer and N is the Nb35 crystallisation
# nanobody, both intracellular scaffolding that would crowd an extracellular
# figure. A is kept only to DEFINE which way is up (see `frame()`).
RECEPTOR, RAMP, AGONIST, GALPHA = "R", "E", "P", "A"

CALCRL_UNIPROT = "Q16602"
MEMBRANE_COL = "#6b7f8c"
MEMBRANE_ALPHA = 74          # per-cent transparency: the bundle must read through it
MEMBRANE_RADIUS = 34.0
# A lipid bilayer's hydrophobic core is ~30 A. Not a fitted number and not
# eyeballed off the render: UniProt's own transmembrane spans for CALCRL,
# mapped into 6E3Y's author numbering, occupy a 28 A band (10th-90th centile)
# along the membrane normal, so the constant and the structure agree to within
# 2 A. `membrane()` prints both, and refuses if they diverge.
BILAYER_A = 30.0


def lead_design() -> dict:
    with (BINDER / "scoring/top_k.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit("top_k.csv is empty")
    return rows[0]


def target_offset(refold: str) -> int:
    """author_seq_id - refold_seq_id, verified against BOTH structures.

    render_pain.py checks this against the refold alone, which is enough to
    place hotspots there. Here the mapping also has to carry across to a
    different deposited entry, so it is checked against 6E3Y as well: an
    offset that is right for the refold and wrong for 6E3Y would superpose
    the wrong residues onto each other and produce a confident, false pose.
    """
    import gemmi

    trim = json.loads((BINDER / "trim/trim_map.json").read_text(encoding="utf-8"))
    offset = int(trim["kept_segments"][0][0]) - 1

    rf = gemmi.read_structure(refold)
    rf.setup_entities()
    fl = gemmi.read_structure(str(FULL))
    fl.setup_entities()

    ramp = {r.seqid.num: r.name for r in fl[0][RAMP]}
    bad = [(res.seqid.num, res.name, ramp.get(res.seqid.num + offset))
           for res in rf[0]["B"]
           if res.seqid.num + offset in ramp
           and ramp[res.seqid.num + offset] != res.name]
    if bad:
        raise SystemExit(
            f"target mapping failed at offset {offset}: {bad[:3]} — refusing to "
            f"superpose the design onto residues it does not correspond to.")
    return offset


def frame(refold: str, offset: int) -> tuple[str, str]:
    """A camera with the membrane horizontal and the binder facing the viewer.

    Both axes are derived from the structure rather than dialled in by hand:

      up   the extracellular direction, defined as RAMP1's centroid seen from
           the G protein's. The Gs heterotrimer is unambiguously intracellular,
           so this needs no guess about where the 7TM bundle starts — a residue
           range would be a guess, and a wrong one flips the receptor over.
      z    perpendicular to `up`, pointing at the binder, so the design is
           between the camera and the receptor instead of behind it.

    Returns (camera matrix, rotation axis) — the turntable spins about `up`,
    which is the only spin that keeps a membrane protein's membrane level.
    """
    import gemmi
    import numpy as np

    rf = gemmi.read_structure(refold)
    rf.setup_entities()
    fl = gemmi.read_structure(str(FULL))
    fl.setup_entities()

    def ca(chain, wanted=None):
        out = {}
        for res in chain:
            atom = res.find_atom("CA", "*")
            if atom is not None and (wanted is None or res.seqid.num in wanted):
                out[res.seqid.num] = np.array([atom.pos.x, atom.pos.y, atom.pos.z])
        return out

    # Superpose the refold's target onto 6E3Y's RAMP1 (Kabsch), then carry the
    # binder across on the same transform — mirroring what `matchmaker` will do
    # in ChimeraX, so the camera is computed in the frame it will be used in.
    rb, fe = ca(rf[0]["B"]), ca(fl[0][RAMP])
    pairs = [(v, fe[n + offset]) for n, v in rb.items() if n + offset in fe]
    if len(pairs) < 30:
        raise SystemExit(f"only {len(pairs)} paired residues — mapping is wrong")
    P = np.array([p for p, _ in pairs])
    Q = np.array([q for _, q in pairs])
    pc, qc = P.mean(0), Q.mean(0)
    U, _S, Vt = np.linalg.svd((P - pc).T @ (Q - qc))
    R = Vt.T @ np.diag([1.0, 1.0, np.sign(np.linalg.det(Vt.T @ U.T))]) @ U.T
    rms = float(np.sqrt((((R @ (P - pc).T).T + qc - Q) ** 2).sum(1).mean()))
    print(f"  superposition: {rms:.2f} A over {len(pairs)} CA")

    binder = (R @ (np.array(list(ca(rf[0]["A"]).values())) - pc).T).T + qc
    ecd = np.array(list(fe.values()))
    gp = np.array(list(ca(fl[0][GALPHA]).values()))
    shown = np.vstack([np.array(list(ca(fl[0][RECEPTOR]).values())), ecd,
                       np.array(list(ca(fl[0][AGONIST]).values())), binder])

    up = ecd.mean(0) - gp.mean(0)
    up /= np.linalg.norm(up)

    # Toward the binder, with the up-component removed so the horizon holds.
    z = binder.mean(0) - shown.mean(0)
    z -= z.dot(up) * up
    z /= np.linalg.norm(z) or 1.0

    x = np.cross(up, z)
    centre = shown.mean(0)
    spread = float(np.linalg.norm(shown - centre, axis=1).max())
    pos = centre + z * spread * 3.2

    matrix = ",".join(f"{v:.5f}" for i in range(3)
                      for v in (x[i], up[i], z[i], pos[i]))
    axis = ",".join(f"{v:.5f}" for v in up)
    return matrix, axis


def membrane(up) -> tuple[list[float], float]:
    """The bilayer slab, from the pipeline's own topology source.

    A membrane drawn where it looks about right is a figure that argues from a
    picture. This asks `src.membrane_topology` — the same module that decided
    which face of this receptor was designable in the first place, and the same
    UniProt annotation that dropped the transmembrane residues from the trim —
    for CALCRL's transmembrane spans, maps them into 6E3Y's author numbering
    through the RCSB entity alignment, and centres a `BILAYER_A` slab on those
    residues' own centroid.

    The answer is cached to `facts/hero_membrane.json`, which is tracked: the
    lookup needs UniProt and RCSB, and a figure in the repository should not
    stop being reproducible because a network is down or an annotation moved.
    """
    import numpy as np
    import gemmi

    cache = HERE / "facts/hero_membrane.json"
    if cache.is_file() and "--refresh-topology" not in sys.argv:
        blob = json.loads(cache.read_text(encoding="utf-8"))
        print(f"  membrane: {blob['n_tm']} TM residues (cached), "
              f"{blob['span_A']:.1f} A observed span")
        return blob["centre"], blob["thickness_A"]

    sys.path.insert(0, str(ROOT))
    from src.membrane_topology import fetch_topology, uniprot_to_auth

    topo = fetch_topology(CALCRL_UNIPROT)
    if not topo.is_membrane:
        raise SystemExit(f"{CALCRL_UNIPROT} carries no membrane topology")
    mapping = uniprot_to_auth("6E3Y", RECEPTOR, CALCRL_UNIPROT)
    wanted = {a for seg in topo.of_kind("transmembrane")
              for pos in range(seg.start, seg.end + 1)
              if (a := mapping.get(pos)) is not None}
    if not wanted:
        raise SystemExit("no transmembrane residue mapped into 6E3Y/R")

    st = gemmi.read_structure(str(FULL))
    st.setup_entities()
    tm = np.array([[a.pos.x, a.pos.y, a.pos.z]
                   for res in st[0][RECEPTOR] if res.seqid.num in wanted
                   for a in [res.find_atom("CA", "*")] if a is not None])
    h = tm.dot(np.asarray(up))
    span = float(np.percentile(h, 90) - np.percentile(h, 10))
    if abs(span - BILAYER_A) > 6.0:
        raise SystemExit(
            f"transmembrane span is {span:.1f} A but BILAYER_A is {BILAYER_A} — "
            f"the normal or the mapping is wrong; refusing to draw a membrane.")
    print(f"  membrane: {len(wanted)} TM residues, {span:.1f} A observed span "
          f"vs {BILAYER_A:.0f} A bilayer")
    centre = [round(v, 3) for v in tm.mean(0)]
    cache.write_text(json.dumps(
        {"uniprot": CALCRL_UNIPROT, "pdb_id": "6E3Y", "chain": RECEPTOR,
         "n_tm": len(wanted), "span_A": round(span, 2),
         "thickness_A": BILAYER_A, "centre": centre}, indent=2) + "\n",
        encoding="utf-8")
    return centre, BILAYER_A


def agonist_overlap(refold: str, offset: float) -> dict:
    """Does the design land on the agonist's own footprint? Measured, not claimed.

    Written to `facts/hero_check.json` so the launch material can quote it with
    the same provenance as every showcase number.

    Read this carefully before repeating it. The SITE was chosen: the interface
    stage deliberately targeted the CGRP-contacting epitope, so finding the
    binder near CGRP is the design working as instructed, not a discovery. What
    is independent is the OCCUPANCY — the campaign ran against 3N7S, an
    ectodomain crystal form with no peptide in it, and never scored a single
    design against CGRP. 6E3Y supplies the peptide afterwards, so the atom
    counts below are a check the pipeline could not have optimised toward, in
    the same way PDB 4ZQK checks the PD-L1 campaign.
    """
    import numpy as np
    import gemmi

    rf = gemmi.read_structure(refold)
    rf.setup_entities()
    fl = gemmi.read_structure(str(FULL))
    fl.setup_entities()

    def ca(chain):
        return {r.seqid.num: np.array([a.pos.x, a.pos.y, a.pos.z])
                for r in chain for a in [r.find_atom("CA", "*")] if a is not None}

    def heavy(chain):
        return np.array([[a.pos.x, a.pos.y, a.pos.z] for r in chain
                         if r.name != "HOH" for a in r
                         if a.element != gemmi.Element("H")])

    rb, fe = ca(rf[0]["B"]), ca(fl[0][RAMP])
    pairs = [(v, fe[n + offset]) for n, v in rb.items() if n + offset in fe]
    P = np.array([p for p, _ in pairs])
    Q = np.array([q for _, q in pairs])
    pc, qc = P.mean(0), Q.mean(0)
    U, _S, Vt = np.linalg.svd((P - pc).T @ (Q - qc))
    R = Vt.T @ np.diag([1.0, 1.0, np.sign(np.linalg.det(Vt.T @ U.T))]) @ U.T
    rmsd = float(np.sqrt((((R @ (P - pc).T).T + qc - Q) ** 2).sum(1).mean()))

    binder = (R @ (heavy(rf[0]["A"]) - pc).T).T + qc
    out = {"pdb_id": "6E3Y", "superposed_on": RAMP, "ca_pairs": len(pairs),
           "superposition_rmsd_A": round(rmsd, 2), "cutoff_A": 4.5}
    for cid, label in ((AGONIST, "cgrp"), (RECEPTOR, "calcrl"), (RAMP, "ramp1"),
                       (GALPHA, "g_alpha")):
        X = heavy(fl[0][cid])
        d = np.sqrt(((binder[:, None, :] - X[None, :, :]) ** 2).sum(-1))
        out[label] = {"min_distance_A": round(float(d.min()), 2),
                      "binder_atoms_in_contact": int((d < 4.5).any(1).sum())}
    (HERE / "facts/hero_check.json").write_text(
        json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"  agonist check: binder-CGRP min {out['cgrp']['min_distance_A']} A, "
          f"{out['cgrp']['binder_atoms_in_contact']} binder atoms within 4.5 A")
    return out


def hotspot_spec(hotspots: list[list]) -> str:
    """Hotspots in 6E3Y's own author numbering — which is what they already are.

    The pipeline declared them against the deposited entry, so no offset is
    applied here. They are painted on 6E3Y's chains, not on the refold's copy
    of the target, which is hidden.
    """
    return ",".join(str(int(auth)) for _name, auth in hotspots)


def _pt(centre: list[float], axis: str, offset: float) -> str:
    """A point `offset` A from `centre` along `axis`, as ChimeraX wants it."""
    a = [float(v) for v in axis.split(",")]
    return ",".join(f"{centre[i] + a[i] * offset:.3f}" for i in range(3))


def script(refold: str, matrix: str, axis: str, spec: str,
           centre: list[float], thickness: float,
           tmp: pathlib.Path, turntable: bool) -> str:
    lines = [
        f"open {FULL}",
        f"open {refold}",
        # Align on the target, never on the binder.
        f"matchmaker #2/B to #1/{RAMP}",
        # Representation FIRST. `show cartoon` is global, so any hide issued
        # before it is silently undone — the first render of this figure came
        # back with the whole Gs heterotrimer in default rainbow because the
        # hides were written above it.
        "hide atoms", "show cartoon", "hide pseudobonds",
        # The Gs heterotrimer and the Nb35 crystallisation nanobody are
        # intracellular; they are three times the receptor's bulk and carry
        # none of the story. Chain A here is 6E3Y's G-alpha, NOT the refold's
        # binder, which is #2/A.
        f"hide #1/{GALPHA},B,G,N cartoon",
        # The refold's own copy of the target is redundant once superposed;
        # drawing both leaves a doubled cartoon down the whole ECD.
        "hide #2/B cartoon",
        # The bilayer, so a reader outside the field can see that the epitope
        # is the part of this receptor a molecule in the bloodstream can reach.
        # Drawn before `view` so the frame includes it.
        # `fromPoint`/`toPoint`, NOT `center`+`axis`: ChimeraX 1.12 accepts the
        # latter pair on `shape cylinder` and then silently ignores both, so the
        # slab is built at the origin along z — 160 A from the receptor and
        # face-on to a camera that expected it edge-on. No error, and the frame
        # still renders, just with a disc floating in empty space beside a
        # shrunken protein.
        f"shape cylinder radius {MEMBRANE_RADIUS} "
        f"fromPoint {_pt(centre, axis, -thickness / 2)} "
        f"toPoint {_pt(centre, axis, thickness / 2)} "
        f"color {MEMBRANE_COL} name membrane",
        f"transparency #3 {MEMBRANE_ALPHA}",
        "set bgColor white", "lighting soft", "lighting shadows false",
        "graphics silhouettes true width 1.4",
        f"color #1/{RECEPTOR},{RAMP} {TARGET_COL}",
        f"color #1/{AGONIST} {AGONIST_COL}",
        f"color #1/{RAMP}:{spec} {HOTSPOT_COL}",
        f"color #2/A {BINDER_COL}",
        f"view matrix camera {matrix}", "view", "zoom 1.06",
    ]
    if turntable:
        out = tmp / "turntable"
        out.mkdir(parents=True, exist_ok=True)
        step = 360.0 / TURN_FRAMES
        for i in range(TURN_FRAMES):
            lines.append(f"save {out / f'hero_{i:03d}.png'} width {TURN_W} "
                         f"height {TURN_H} supersample 2 transparentBackground true")
            lines.append(f"turn {axis} {step:.4f} 1 center #1/{RAMP} coordinateSystem #1")
        return "\n".join(lines) + "\n"

    lines.append(f"save {tmp / 'hero_receptor.png'} width {W} height {H} "
                 f"supersample 3 transparentBackground true")
    lines.append("hide #2 models")
    lines.append(f"save {tmp / 'hero_apo.png'} width {W} height {H} "
                 f"supersample 3 transparentBackground true")
    return "\n".join(lines) + "\n"


def run(cxc: str, tmp: pathlib.Path) -> None:
    path = tmp / "hero.cxc"
    path.write_text(cxc, encoding="utf-8")
    proc = subprocess.run([CHIMERAX, "--offscreen", "--nogui", "--exit",
                           "--silent", str(path)],
                          capture_output=True, text=True)
    blob = proc.stdout + proc.stderr
    # ChimeraX exits 0 having written nothing, so the return code proves little.
    if "ERROR" in blob or proc.returncode != 0:
        sys.stderr.write(blob)
        raise SystemExit("ChimeraX reported an error")


def to_webp(src: pathlib.Path, dest: pathlib.Path, box=None) -> tuple:
    from PIL import Image

    img = Image.open(src)
    box = box or img.getbbox()
    img.crop(box).save(dest, "WEBP", quality=90, method=6)
    return box


def main() -> int:
    if not pathlib.Path(CHIMERAX).exists():
        raise SystemExit(f"{CHIMERAX} not found")
    if not FULL.is_file():
        raise SystemExit(f"{FULL} not on this machine — fetch 6E3Y first")

    facts = json.loads((HERE / "facts/pain_receptors.json").read_text(encoding="utf-8"))
    design = lead_design()
    refold = design["refold_cif"]
    if not pathlib.Path(refold).is_file():
        raise SystemExit(f"refold not on this machine: {refold}")

    print(f"  lead design {design['name']}  iptm {float(design['iptm']):.3f}  "
          f"dock {float(design['binder_rmsd_dock']):.2f} A")
    offset = target_offset(refold)
    matrix, axis = frame(refold, offset)
    centre, thickness = membrane([float(v) for v in axis.split(",")])
    agonist_overlap(refold, offset)
    spec = hotspot_spec(facts["hotspots"])

    ASSETS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        run(script(refold, matrix, axis, spec, centre, thickness,
                   tmp, turntable=False), tmp)
        # One crop box for the pair, so "before" and "after" stay comparable.
        box = to_webp(tmp / "hero_receptor.png", ASSETS / "hero_receptor.webp")
        to_webp(tmp / "hero_apo.png", ASSETS / "hero_apo.webp", box)
        print(f"  hero_receptor.webp / hero_apo.webp  {box[2]-box[0]}x{box[3]-box[1]}")

        if "--turntable" in sys.argv:
            run(script(refold, matrix, axis, spec, centre, thickness,
                       tmp, turntable=True), tmp)
            dest = ASSETS / "turntable"
            dest.mkdir(exist_ok=True)
            frames = sorted((tmp / "turntable").glob("hero_*.png"))
            # One union box again: per-frame cropping makes the model jitter.
            from PIL import Image
            union = None
            for f in frames:
                b = Image.open(f).getbbox()
                union = b if union is None else (min(union[0], b[0]), min(union[1], b[1]),
                                                 max(union[2], b[2]), max(union[3], b[3]))
            for f in frames:
                Image.open(f).crop(union).save(dest / f.name.replace(".png", ".png"))
            print(f"  {len(frames)} turntable frames  "
                  f"{union[2]-union[0]}x{union[3]-union[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
