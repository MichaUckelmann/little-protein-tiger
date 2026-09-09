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

# Same palette as the pages' legends, so a figure and its key agree.
BINDER_COL = "#2f8f74"
TARGET_COL = "#9aa79d"
HOTSPOT_COL = "#c0872b"


def lead_design() -> dict:
    with (BINDER / "scoring/top_k.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit("top_k.csv is empty")
    return rows[0]


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

    with tempfile.NamedTemporaryFile("w", suffix=".cxc", delete=False) as fh:
        fh.write(script)
        path = fh.name
    print(f"  rendering {design['name']}")
    print(f"  hotspots  author {[a for _n, a in facts['hotspots']]}")
    print(f"            refold {nums}  (offset {offset}, verified)")
    r = subprocess.run([CHIMERAX, "--offscreen", "--nogui", "--exit", "--silent", path],
                       capture_output=True, text=True)
    if "ERROR" in (r.stdout + r.stderr):
        sys.stderr.write((r.stdout + r.stderr)[-3000:] + "\n")
        return 1
    if r.returncode != 0:
        sys.stderr.write(r.stdout[-3000:] + r.stderr[-3000:])
        return r.returncode

    # Crop each to its alpha bounding box, so the framing is set by the model
    # rather than by whatever margin ChimeraX left, then write webp.
    from PIL import Image
    wrote = 0
    for stem in ("pain_design", "pain_epitope"):
        src = tmp_dir / f"{stem}.png"
        if not src.is_file():
            print(f"  MISSING {stem}.png — ChimeraX wrote nothing")
            continue
        im = Image.open(src).convert("RGBA")
        box = im.getbbox()
        if box:
            im = im.crop(box)
        out = ASSETS / f"{stem}.webp"
        im.save(out, "WEBP", quality=88, method=6)
        wrote += 1
        print(f"  wrote assets/{stem}.webp  {im.size[0]}x{im.size[1]}  "
              f"{out.stat().st_size / 1024:.0f} KB")
    return 0 if wrote == 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
