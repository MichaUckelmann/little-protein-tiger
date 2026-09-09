#!/usr/bin/env python3
"""Render the CALCRL/RAMP1 lead design for the pain-receptor showcase.

Writes `assets/pain_design.webp` (design bound to RAMP1, hotspots highlighted)
and `assets/pain_epitope.webp` (the same view with the binder hidden), so the
pair reads as before/after on one camera.

The camera is COMPUTED, not hand-placed: it looks down the vector from the
target's centroid to the hotspot centroid, so the epitope faces the viewer and
both images of the pair share the orientation. Hand-placing gives a different
frame every re-render and the before/after stops lining up.

Three traps this script exists to get right, all documented in README.md and all
capable of silently producing a plausible, wrong picture:

  * RF3 refolds renumber the target 1-based, while the hotspots the pipeline
    declared are in author numbering. The offset is derived from the trim's own
    `kept_segments` and then CHECKED residue by residue against the structure —
    a wrong offset paints ten arbitrary residues and looks fine.
  * Binder is chain A and target chain B in an RF3 refold, the opposite of the
    deposited complexes.
  * ChimeraX labels missing-residue pseudobonds ("8 residues"), which lands as
    floating type over the model unless hidden.

    .venv/bin/python docs/showcase/render_pain.py
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

CHIMERAX = "/usr/bin/chimerax"
W, H = 1500, 1200
CARD_W, CARD_H = 1000, 800

# Same palette as the pages' legends, so a figure and its key agree.
BINDER_COL = "#2f8f74"
TARGET_COL = "#9aa79d"
HOTSPOT_COL = "#c0872b"


N_CARDS = 4          # matches the design cards build_pain.py renders


def top_designs(n: int = N_CARDS) -> list[dict]:
    with (BINDER / "scoring/top_k.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit("top_k.csv is empty")
    return rows[:n]


def lead_design() -> dict:
    return top_designs(1)[0]


def hotspot_offset(hotspots: list[list], structure_path: str) -> int:
    """author_seq_id -> refold residue number, verified against the coordinates.

    The trim keeps one segment starting at `kept_segments[0][0]`, and RF3
    renumbers the target from 1, so the offset is that start minus one. Derived
    rather than assumed, then checked: if any hotspot's residue name disagrees
    with the structure the mapping is wrong and rendering would mislabel the
    epitope, so this refuses instead.
    """
    import gemmi

    trim = json.loads((BINDER / "trim/trim_map.json").read_text(encoding="utf-8"))
    offset = int(trim["kept_segments"][0][0]) - 1

    st = gemmi.read_structure(structure_path)
    st.setup_entities()
    target = {res.seqid.num: res.name for res in st[0]["B"]}
    for name, auth in hotspots:
        got = target.get(int(auth) - offset)
        if got != name:
            raise SystemExit(
                f"hotspot mapping failed: author {name}{auth} -> refold "
                f"{int(auth) - offset} is {got}, not {name}. Refusing to render a "
                f"figure that would highlight the wrong residues.")
    return offset


def camera_matrix(structure_path: str, hotspot_nums: list[int],
                  tilt_deg: float = 52.0) -> str:
    """A 3x4 camera matrix aimed at the epitope, then tilted off that axis.

    Straight down the target-centroid -> epitope vector the binder sits exactly
    between the camera and the epitope, so it hides both the target and the
    hotspots it is supposed to be covering — the first render of this figure was
    a green helix bundle with the target barely visible behind it. Tilting keeps
    the epitope aimed at the viewer while opening the interface side-on, so both
    partners read as distinct objects. The tilt is shared by both images of the
    pair, so they still line up as before/after.
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

    # Look from beyond the epitope back toward the complex.
    v = [hc[i] - tc[i] for i in range(3)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    z = [x / n for x in v]                      # camera +Z points at the viewer

    # Any up-vector not parallel to z; Gram-Schmidt it into the frame.
    up = [0.0, 0.0, 1.0] if abs(z[2]) < 0.9 else [0.0, 1.0, 0.0]
    d = sum(up[i] * z[i] for i in range(3))
    y = [up[i] - d * z[i] for i in range(3)]
    ny = math.sqrt(sum(q * q for q in y)) or 1.0
    y = [q / ny for q in y]
    x = [y[1] * z[2] - y[2] * z[1], y[2] * z[0] - y[0] * z[2], y[0] * z[1] - y[1] * z[0]]

    # Swing the frame about its own up-axis so the interface is seen edge-on.
    a = math.radians(tilt_deg)
    ca, sa = math.cos(a), math.sin(a)
    z, x = ([z[i] * ca + x[i] * sa for i in range(3)],
            [x[i] * ca - z[i] * sa for i in range(3)])

    # Far enough out that `view` can still frame it without clipping.
    spread = max(math.dist(p, cc) for p in all_ca)
    pos = [cc[i] + z[i] * spread * 3.2 for i in range(3)]

    rows = [[x[i], y[i], z[i], pos[i]] for i in range(3)]
    return ",".join(f"{v:.5f}" for row in rows for v in row)


def card_script(designs: list[dict], spec: str, matrix: str,
                tmp_dir: pathlib.Path) -> str:
    """One .cxc that renders the top-N designs on a SHARED camera.

    Each design is a separate RF3 refold, so its target sits in its own frame.
    Rendered independently the four cards would each be aimed differently and
    could not be compared — which is the only reason to put them side by side.
    So every target chain is superposed onto rank 1's with `matchmaker`, one
    camera is computed and framed over the union, and the models are then shown
    one at a time WITHOUT re-running `view`: re-fitting per image is exactly
    what would silently break the shared framing.
    """
    lines = [f"open {d['refold_cif']}" for d in designs]
    for i in range(2, len(designs) + 1):
        # /B is the target in every refold; align on it, not on the binders,
        # which are different molecules with different folds.
        lines.append(f"matchmaker #{i}/B to #1/B")
    lines += [
        "hide atoms", "show cartoon", "hide pseudobonds",
        "set bgColor white", "lighting soft", "lighting shadows false",
        "graphics silhouettes true width 1.4",
    ]
    for i in range(1, len(designs) + 1):
        lines += [f"color #{i}/A {BINDER_COL}", f"color #{i}/B {TARGET_COL}",
                  f"color #{i}/B:{spec} {HOTSPOT_COL}"]
    # Frame once, over everything, then never touch the camera again.
    lines += [f"view matrix camera {matrix}", "view", "zoom 1.05"]
    for i in range(1, len(designs) + 1):
        others = [str(j) for j in range(1, len(designs) + 1) if j != i]
        if others:
            lines.append(f"hide #{','.join(others)} models")
        lines.append(f"show #{i} models")
        lines.append(f"save {tmp_dir / f'pain_rank{i}.png'} width {CARD_W} "
                     f"height {CARD_H} supersample 3 transparentBackground true")
    return "\n".join(lines) + "\n"


def main() -> int:
    if not pathlib.Path(CHIMERAX).exists():
        raise SystemExit(f"{CHIMERAX} not found — install ChimeraX or edit CHIMERAX")

    facts = json.loads((HERE / "facts/pain_receptors.json").read_text(encoding="utf-8"))
    design = lead_design()
    cif = design["refold_cif"]
    if not pathlib.Path(cif).is_file():
        raise SystemExit(f"refold not on this machine: {cif}")

    offset = hotspot_offset(facts["hotspots"], cif)
    nums = [int(a) - offset for _n, a in facts["hotspots"]]
    spec = ",".join(str(n) for n in nums)
    matrix = camera_matrix(cif, nums)
    ASSETS.mkdir(exist_ok=True)

    common = f"""
open {cif}
hide atoms
show cartoon
color /A {BINDER_COL}
color /B {TARGET_COL}
color /B:{spec} {HOTSPOT_COL}
hide pseudobonds
set bgColor white
lighting soft
lighting shadows false
graphics silhouettes true width 1.4
view matrix camera {matrix}
view
zoom 1.08
"""
    # ChimeraX has no webp writer ("No known data format for file suffix
    # '.webp'"), so render PNG and convert below.
    tmp_dir = pathlib.Path(tempfile.mkdtemp())
    script = common + f"""
save {tmp_dir / 'pain_design.png'} width {W} height {H} supersample 3 transparentBackground true
hide /A cartoon
view matrix camera {matrix}
view
zoom 1.08
save {tmp_dir / 'pain_epitope.png'} width {W} height {H} supersample 3 transparentBackground true
"""

    def run_cxc(body: str) -> bool:
        with tempfile.NamedTemporaryFile("w", suffix=".cxc", delete=False) as fh:
            fh.write(body)
            path = fh.name
        r = subprocess.run([CHIMERAX, "--offscreen", "--nogui", "--exit",
                            "--silent", path], capture_output=True, text=True)
        out = r.stdout + r.stderr
        # ChimeraX exits 0 after a failed `save`, so the return code alone is
        # not enough — it once reported success having written nothing.
        if "ERROR" in out or r.returncode != 0:
            sys.stderr.write(out[-3000:] + "\n")
            return False
        return True

    def to_webp(src: pathlib.Path, name: str) -> bool:
        """Crop to the alpha bounding box, so the model sets the framing."""
        if not src.is_file():
            print(f"  MISSING {src.name} — ChimeraX wrote nothing")
            return False
        from PIL import Image
        im = Image.open(src).convert("RGBA")
        box = im.getbbox()
        if box:
            im = im.crop(box)
        out = ASSETS / name
        im.save(out, "WEBP", quality=88, method=6)
        print(f"  wrote assets/{name}  {im.size[0]}x{im.size[1]}  "
              f"{out.stat().st_size / 1024:.0f} KB")
        return True

    print(f"  hotspots  author {[a for _n, a in facts['hotspots']]}")
    print(f"            refold {nums}  (offset {offset}, verified)")

    # ---- hero pair: the lead design, and the bare epitope on one camera -----
    print(f"  rendering hero pair from {design['name']}")
    if not run_cxc(script):
        return 1
    wrote = sum(to_webp(tmp_dir / f"{s}.png", f"{s}.webp")
                for s in ("pain_design", "pain_epitope"))

    # ---- design cards: top N, superposed, one shared camera ----------------
    designs = top_designs()
    missing = [d["name"] for d in designs if not pathlib.Path(d["refold_cif"]).is_file()]
    if missing:
        print(f"  skipping cards — refolds absent: {missing}")
        return 0 if wrote == 2 else 1
    card_dir = pathlib.Path(tempfile.mkdtemp())
    print(f"  rendering {len(designs)} design cards (superposed on rank 1)")
    if not run_cxc(card_script(designs, spec, matrix, card_dir)):
        return 1
    # Crop the cards to ONE box — the union of their bounding boxes — not each
    # to its own. Per-image cropping would shift and rescale every card
    # independently and quietly undo the shared camera the superposition exists
    # to provide; the target has to land in the same place in all four for the
    # differences between the binders to be the thing a reader sees.
    from PIL import Image
    srcs = [card_dir / f"pain_rank{i}.png" for i in range(1, len(designs) + 1)]
    if any(not s.is_file() for s in srcs):
        print(f"  MISSING one or more card renders: "
              f"{[s.name for s in srcs if not s.is_file()]}")
        return 1
    ims = [Image.open(s).convert("RGBA") for s in srcs]
    boxes = [im.getbbox() for im in ims]
    union = (min(b[0] for b in boxes), min(b[1] for b in boxes),
             max(b[2] for b in boxes), max(b[3] for b in boxes))
    for i, im in enumerate(ims, 1):
        out = ASSETS / f"pain_rank{i}.webp"
        cropped = im.crop(union)
        cropped.save(out, "WEBP", quality=88, method=6)
        print(f"  wrote assets/pain_rank{i}.webp  {cropped.size[0]}x{cropped.size[1]}"
              f"  {out.stat().st_size / 1024:.0f} KB")
    return 0 if wrote == 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
