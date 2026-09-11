#!/usr/bin/env python3
"""Render the five structure figures on `campaign_pdl1.html` (PD-L1, 8ZNL).

    .venv/bin/python docs/showcase/render_pdl1.py

Writes, all from `projects/pdl1_rc1` and all on ONE computed camera:

    assets/design_face.webp   the lead design bound, hotspots tinted
    assets/epitope.webp       the same camera with the binder hidden
    assets/native_face.webp   8ZNL's own crystallised partner, same axis
    assets/pd1_face.webp      4ZQK's PD-1 on PD-L1, its footprint tinted
    assets/footprint.webp     PD-L1 surface: shared / PD-1-only / design-only

The previous set was made by hand for the August 7CZD campaign, and the
README asked for the remaining renders to be ported to `render_pain.py`'s
shape when they were next regenerated. This is that port, so what the page
asserts about its own pictures — computed cameras, a verified residue
mapping — is now true of this page too.

FIVE THINGS THIS ENCODES, each of which silently produces a plausible-looking
wrong figure if you get it wrong:

  * **RF3 renumbers the target from 1** while the hotspot ids are author
    numbering from the deposited entry. The offset comes from the trim's own
    `kept_segments` and is then CHECKED against every hotspot's residue name
    in the coordinates; a wrong offset paints arbitrary residues and the image
    looks fine.
  * **Binder is chain A and target is chain B in a refold** — the opposite of
    the deposited complexes, where the target is usually chain A. 4ZQK's PD-L1
    is chain A and its PD-1 is chain B.
  * **Everything is superposed onto the refold's target before rendering**, so
    all five figures share one frame and one camera. Aligning on the target
    (`/B`), never on the binders: they are different molecules.
  * **The camera is tilted ~52 degrees off the epitope axis.** Straight down it
    the binder sits between the camera and everything it covers.
  * **ChimeraX cannot write WebP** (`No known data format for file suffix
    '.webp'`) and draws missing-residue pseudobonds with a floating text label
    that lands in the render. So: PNG, `hide pseudobonds`, convert after.

The footprint sets come from `facts/campaign_pdl1.json`'s `zqk.refold` block,
already in refold numbering, rather than being re-derived here — one offset,
computed once, in `build_campaign.py`.
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
PROJECT = ROOT / "projects/pdl1_rc1"
BINDER = PROJECT / "runs/round-1/binder"
FACTS = HERE / "facts/campaign_pdl1.json"
ZQK = ROOT / "data/structures/4ZQK_ba1.cif"

CHIMERAX = "/usr/bin/chimerax"
W, H = 1500, 1200

# The launch video's motion beat. Same shape as `render_hero.py`'s: 90 frames
# is 3 s at 30 fps, and 1080x1080 square because the video composites it into
# a fixed plate box rather than scaling to fit a caption.
TURN_W, TURN_H = 1080, 1080
TURN_FRAMES = 90
TURNTABLE = ASSETS / "turntable_pdl1"

# The palette the page's own legends use, so a figure and its key agree.
BINDER_COL = "#2f8f74"        # the design
TARGET_COL = "#9aa79d"        # PD-L1
HOTSPOT_COL = "#c0872b"       # chosen hotspots, and "both" in the footprint key
NATIVE_COL = "#8d97a4"        # 8ZNL's crystallised partner
PD1_COL = "#5e62b0"           # PD-1, and "PD-1 only"


def lead_design() -> dict:
    with (BINDER / "scoring/top_k.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit("top_k.csv is empty")
    return rows[0]


def hotspot_offset(hotspots: list[dict], structure_path: str) -> int:
    """author_seq_id -> refold residue number, verified against coordinates.

    Derived from the trim (`kept_segments[0][0] - 1`, because RF3 renumbers the
    kept span from 1), then proven: every hotspot's residue name must match
    what is actually at the mapped position, or this refuses. The check is the
    point — the arithmetic is one line and it is right until the day a trim
    keeps two segments.
    """
    import gemmi

    trim = json.loads((BINDER / "trim/trim_map.json").read_text(encoding="utf-8"))
    segments = trim["kept_segments"]
    offset = int(segments[0][0]) - 1

    st = gemmi.read_structure(structure_path)
    st.setup_entities()
    target = {res.seqid.num: res.name for res in st[0]["B"]}
    for hs in hotspots:
        want, auth = hs["name"].upper(), int(hs["auth"])
        got = target.get(auth - offset)
        if got != want:
            raise SystemExit(
                f"hotspot mapping failed: author {want}{auth} -> refold "
                f"{auth - offset} is {got}, not {want}. Refusing to render a "
                f"figure that would highlight the wrong residues. "
                f"(kept_segments={segments})")
    return offset


def camera_matrix(structure_path: str, hotspot_nums: list[int],
                  tilt_deg: float = 52.0) -> str:
    """A 3x4 camera matrix aimed at the epitope, then tilted off that axis.

    Lifted from `render_pain.py` unchanged, including the tilt: straight down
    the target-centroid -> epitope vector, the binder occludes the target and
    the very hotspots it is covering.
    """
    import gemmi

    st = gemmi.read_structure(structure_path)
    st.setup_entities()

    def centroid(atoms):
        n = len(atoms)
        return [sum(a[i] for a in atoms) / n for i in range(3)]

    target_ca, hot_ca, all_ca = [], [], []
    for ch in st[0]:
        for res in ch:
            ca = res.find_atom("CA", "*")
            if ca is None:
                continue
            p = [ca.pos.x, ca.pos.y, ca.pos.z]
            all_ca.append(p)
            if ch.name == "B":
                target_ca.append(p)
                if res.seqid.num in hotspot_nums:
                    hot_ca.append(p)
    if not hot_ca:
        raise SystemExit("no hotspot CA atoms found — cannot aim the camera")

    tc, hc, cc = centroid(target_ca), centroid(hot_ca), centroid(all_ca)
    v = [hc[i] - tc[i] for i in range(3)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    z = [x / n for x in v]

    up = [0.0, 0.0, 1.0] if abs(z[2]) < 0.9 else [0.0, 1.0, 0.0]
    d = sum(up[i] * z[i] for i in range(3))
    y = [up[i] - d * z[i] for i in range(3)]
    ny = math.sqrt(sum(q * q for q in y)) or 1.0
    y = [q / ny for q in y]
    x = [y[1] * z[2] - y[2] * z[1], y[2] * z[0] - y[0] * z[2],
         y[0] * z[1] - y[1] * z[0]]

    a = math.radians(tilt_deg)
    ca, sa = math.cos(a), math.sin(a)
    z, x = ([z[i] * ca + x[i] * sa for i in range(3)],
            [x[i] * ca - z[i] * sa for i in range(3)])

    spread = max(math.dist(p, cc) for p in all_ca)
    pos = [cc[i] + z[i] * spread * 3.2 for i in range(3)]
    rows = [[x[i], y[i], z[i], pos[i]] for i in range(3)]
    return ",".join(f"{v:.5f}" for row in rows for v in row)


def spec(nums) -> str:
    return ",".join(str(n) for n in sorted(nums))


def turntable_script(refold: str, matrix: str, hot: list[int]) -> str:
    """The lead design spun about the screen's vertical axis, for the video.

    Its own directory (`assets/turntable_pdl1/`), not the one
    `render_hero.py` writes: that holds the CGRP-receptor turntable the PPI
    video composites, and a single shared folder would mean whichever render
    ran last silently decided what both videos showed.

    Spun about the SCREEN y-axis centred on the WHOLE complex, and framed with
    headroom. Centring on the target instead (the obvious choice, since the
    target is what the binder orbits) swings the binder wide: `view` centres
    the frame at the starting angle, and once the pair has rotated the
    bounding box no longer sits where it was, so the complex wanders
    off-centre and shrinks. Measured on the first attempt — at frame 30 the
    pair sat in the lower-right quadrant of a 1080x1080 canvas.
    """
    TURNTABLE.mkdir(parents=True, exist_ok=True)
    for old in TURNTABLE.glob("pdl1_*.png"):
        old.unlink()
    lines = [
        f"open {refold}",
        "hide atoms", "show cartoon", "hide pseudobonds",
        "set bgColor white", "lighting soft", "lighting shadows false",
        "graphics silhouettes true width 1.4",
        f"color /A {BINDER_COL}", f"color /B {TARGET_COL}",
        f"color /B:{spec(hot)} {HOTSPOT_COL}",
        f"view matrix camera {matrix}", "view", "zoom 0.88",
    ]
    step = 360.0 / TURN_FRAMES
    for i in range(TURN_FRAMES):
        lines.append(f"save {TURNTABLE / f'pdl1_{i:03d}.png'} width {TURN_W} "
                     f"height {TURN_H} supersample 2 transparentBackground true")
        lines.append(f"turn y {step:.4f} 1 center #1 coordinateSystem #1")
    return "\n".join(lines) + "\n"


def build_script(refold: str, native: pathlib.Path, matrix: str,
                 hot: list[int], fp: dict, tmp: pathlib.Path,
                 native_chains: tuple[str, str] = ("B", "A")) -> str:
    """One .cxc for all five figures, sharing one camera ORIENTATION.

    Framing is deliberately not identical across all five, and the difference
    matters:

      * `design_face` and `epitope` are a true before/after PAIR — same
        models, binder shown then hidden — so they are framed ONCE and the
        camera is not touched between them. Re-fitting for the second would
        rescale the target and the pair would stop lining up, which is the
        whole reason to show them together.
      * The other three only need the same AXIS, which is what the page
        claims of them. Each re-applies the same camera matrix (identical
        orientation) and then re-fits distance to its own contents, because
        one `view` over the union of all three models leaves every individual
        figure occupying about a fifth of the canvas — measured: 332x251 out
        of 1500x1200 before this was split.
    """
    L: list[str] = [
        f"open {refold}",                                  # #1  A=design B=PD-L1
        f"open {native}",                                  # #2  8ZNL
        f"open {ZQK}",                                     # #3  4ZQK
        # Align every copy of PD-L1 onto the refold's own, never on a binder.
        f"matchmaker #2/{native_chains[0]} to #1/B",
        "matchmaker #3/A to #1/B",
        "hide atoms", "show cartoon", "hide pseudobonds",
        "set bgColor white", "lighting soft", "lighting shadows false",
        "graphics silhouettes true width 1.4",
        f"color #1/A {BINDER_COL}", f"color #1/B {TARGET_COL}",
        f"color #1/B:{spec(hot)} {HOTSPOT_COL}",
        # 8ZNL's asymmetric unit holds FOUR copies of the 1:1 complex
        # (chains A-H), and the interface stage analysed exactly one pair. Show
        # only that pair: the first render of this figure displayed all eight
        # chains, six of them in ChimeraX's default colours, which read as a
        # crystal packing diagram rather than "here is what a real binder does
        # at this site". The pair comes from the run's own handoff, not from a
        # guess about which chains a PDB entry happens to label A and B.
        "hide #2 cartoon",
        f"show #2/{native_chains[0]},{native_chains[1]} cartoon",
        f"color #2/{native_chains[1]} {NATIVE_COL}",
        f"color #2/{native_chains[0]} {TARGET_COL}",
        f"color #3/A {TARGET_COL}", f"color #3/B {PD1_COL}",
    ]

    def save(name: str) -> str:
        return (f"save {tmp / f'{name}.png'} width {W} height {H} "
                f"supersample 3 transparentBackground true")

    def aim() -> list[str]:
        """Same orientation, re-fitted to whatever is currently shown."""
        return [f"view matrix camera {matrix}", "view", "zoom 1.08"]

    # 1+2. the lead design bound, then the bare epitope — ONE framing.
    L += ["hide #2,3 models", "show #1 models"] + aim()
    L += [save("design_face")]
    L += ["hide #1/A cartoon", save("epitope"), "show #1/A cartoon"]
    # 3. 8ZNL's own crystallised partner, on the same axis.
    L += ["hide #1,3 models", "show #2 models"] + aim() + [save("native_face")]
    # 4. PD-1 from 4ZQK with its footprint tinted. PD-L1 is the REFOLD's copy
    #    (#1/B) in every figure, so the surface is the same object throughout;
    #    only PD-1 itself is borrowed from 4ZQK.
    #
    #    PD-L1 is drawn as a SURFACE and PD-1 half-transparent over it. As
    #    opaque cartoon on this shared axis, PD-1's β-sandwich sat squarely in
    #    front of the footprint it exists to reveal — the same occlusion the
    #    camera tilt exists to avoid, but PD-1 is large and central enough
    #    that the tilt alone does not clear it. The figure's whole job is to
    #    show WHICH PD-L1 residues PD-1 covers, so the covered surface has to
    #    be the thing you can see.
    L += ["hide #2 models", "show #1 models", "hide #1/A cartoon",
          "hide #1/B cartoon", "show #1/B surface",
          "show #3 models", "hide #3/A cartoon",
          f"color #1/B {TARGET_COL}",
          f"color #1/B:{spec(fp['pd1'])} {HOTSPOT_COL}",
          "transparency #3/B 55 target c"] + aim() + [save("pd1_face")]
    # 5. both footprints on one surface, three colours.
    L += ["hide #3 models", "hide #1 cartoon", "show #1/B surface",
          "transparency #1/B 0 target s",
          f"color #1/B {TARGET_COL}",
          f"color #1/B:{spec(fp['design_only'])} {BINDER_COL}",
          f"color #1/B:{spec(fp['pd1_only'])} {PD1_COL}",
          f"color #1/B:{spec(fp['shared'])} {HOTSPOT_COL}"] + aim() + [save("footprint")]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--turntable", action="store_true",
                    help=f"also write {TURN_FRAMES} rotation frames to "
                         f"assets/turntable_pdl1/ for build_video_pdl1.py")
    ap.add_argument("--only-turntable", action="store_true",
                    help="write only the turntable, leaving the five figures alone")
    args = ap.parse_args(argv)

    if not pathlib.Path(CHIMERAX).exists():
        raise SystemExit(f"{CHIMERAX} not found — install ChimeraX or edit CHIMERAX")
    if not FACTS.is_file():
        raise SystemExit(f"no facts snapshot at {FACTS} — run build_campaign.py first")

    facts = json.loads(FACTS.read_text(encoding="utf-8"))
    zqk = facts.get("zqk") or {}
    fp = zqk.get("refold")
    if not fp:
        raise SystemExit(
            "facts/campaign_pdl1.json has no zqk.refold block — rebuild with "
            "build_campaign.py on a machine that has the run")

    design = lead_design()
    refold = design["refold_cif"]
    native = ROOT / f"data/structures/{facts['pdb_id']}.cif"
    for path in (pathlib.Path(refold), native, ZQK):
        if not pathlib.Path(path).is_file():
            raise SystemExit(f"not on this machine: {path}")

    offset = hotspot_offset(facts["hotspots"], refold)
    hot = [int(h["auth"]) - offset for h in facts["hotspots"]]
    matrix = camera_matrix(refold, hot)
    ASSETS.mkdir(exist_ok=True)

    print(f"  lead design  {design['name']}")
    print(f"  hotspots     author {[h['auth'] for h in facts['hotspots']]}")
    print(f"               refold {hot}  (offset {offset}, verified)")
    print(f"  footprints   design {len(fp['design'])}, PD-1 {len(fp['pd1'])}, "
          f"shared {len(fp['shared'])}")

    def run_cxc(body: str) -> bool:
        with tempfile.NamedTemporaryFile("w", suffix=".cxc", delete=False) as fh:
            fh.write(body)
            path = fh.name
        r = subprocess.run([CHIMERAX, "--offscreen", "--nogui", "--exit",
                            "--silent", path], capture_output=True, text=True)
        out = r.stdout + r.stderr
        # ChimeraX exits 0 after a failed `save`, so the return code alone
        # proves nothing — it has reported success having written no file.
        if "ERROR" in out or r.returncode != 0:
            sys.stderr.write(out[-3000:] + "\n")
            return False
        return True

    if args.turntable or args.only_turntable:
        print(f"  turntable    {TURN_FRAMES} frames -> assets/{TURNTABLE.name}/")
        if not run_cxc(turntable_script(refold, matrix, hot)):
            return 1
        n_frames = len(list(TURNTABLE.glob("pdl1_*.png")))
        print(f"               wrote {n_frames} frames")
        if n_frames != TURN_FRAMES:
            print(f"               EXPECTED {TURN_FRAMES} — refusing")
            return 1
        if args.only_turntable:
            return 0

    tmp = pathlib.Path(tempfile.mkdtemp())
    native_chains = (facts.get("target_chain") or "B",
                     facts.get("partner_chain") or "A")
    print(f"  native pair  {facts['pdb_id']} chains "
          f"{native_chains[0]} (target) + {native_chains[1]} (partner)")
    body = build_script(refold, native, matrix, hot, fp, tmp, native_chains)
    with tempfile.NamedTemporaryFile("w", suffix=".cxc", delete=False) as fh:
        fh.write(body)
        script_path = fh.name
    r = subprocess.run([CHIMERAX, "--offscreen", "--nogui", "--exit", "--silent",
                        script_path], capture_output=True, text=True)
    out = r.stdout + r.stderr
    # ChimeraX exits 0 after a failed `save`, so the return code alone is not
    # enough — it has reported success having written nothing.
    if "ERROR" in out or r.returncode != 0:
        sys.stderr.write(out[-3000:] + "\n")
        return 1

    from PIL import Image

    # `design_face` + `epitope` are cropped to ONE box — the union of the two —
    # for the same reason `render_pain.py` crops its four cards to a union:
    # cropping each to its own alpha bbox rescales and shifts them
    # independently, which silently undoes the shared camera. Measured here
    # before it was fixed: 1119x909 against 946x725, so displayed at one
    # column width the "before" and "after" showed PD-L1 at different sizes.
    # The other three stand alone and are cropped to their own content.
    groups: list[tuple[str, ...]] = [
        ("design_face", "epitope"), ("native_face",), ("pd1_face",),
        ("footprint",),
    ]
    wrote = 0
    for group in groups:
        srcs = [tmp / f"{n}.png" for n in group]
        if any(not s.is_file() for s in srcs):
            print(f"  MISSING {[s.name for s in srcs if not s.is_file()]} — "
                  f"ChimeraX wrote nothing")
            continue
        ims = [Image.open(s).convert("RGBA") for s in srcs]
        boxes = [im.getbbox() for im in ims if im.getbbox()]
        if boxes:
            union = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                     max(b[2] for b in boxes), max(b[3] for b in boxes))
            ims = [im.crop(union) for im in ims]
        for name, im in zip(group, ims):
            out_path = ASSETS / f"{name}.webp"
            im.save(out_path, "WEBP", quality=88, method=6)
            print(f"  wrote assets/{name}.webp  {im.size[0]}x{im.size[1]}  "
                  f"{out_path.stat().st_size / 1024:.0f} KB"
                  + ("  [shared crop]" if len(group) > 1 else ""))
            wrote += 1
    return 0 if wrote == 5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
