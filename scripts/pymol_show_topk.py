"""
Load top-K designs from any e2e pipeline run, superimpose on a single target,
highlight per-design hotspots (parsed from the BoltzGen YAML), and bind
F1 / F2 / F3 to cycle / overlay-all.

Run from inside PyMOL:
    run /home/.../scripts/pymol_show_topk.py
Then:
    show_topk                                 # cwd is a run dir
    show_topk /path/to/outputs/<slug>         # explicit run dir
    show_topk outputs/<slug>, 5               # top-5 instead of default 10
    show_topk <run>, 10, A, B                 # explicit target/binder chains

Via ~/.pymolrc alias:
    topk          # registers show_topk; run it after.

Style follows ~/g/Group_Sahtoe/shared/scripts/pymol_functions_cycle.py
(black bg, gold target, cartoon+lines, ambient occlusion, transparency 0.2).
"""

import csv
import os
import re
from pymol import cmd, util


# --- Sahtoe palette (re-registered after every reinitialize) ---------------
_SAHTOE_COLORS = [
    ("goud",          [255, 193,  37]),
    ("cadmiumorange", [255,  97,   3]),
    ("blauw3",        [  0, 154, 205]),
    ("lichtblauw3",   [171, 218, 233]),
    ("lichtgoud",     [255, 229, 165]),
    ("lichtgroen",    [207, 219, 180]),
    ("lichtpaars",    [203, 178, 207]),
    ("lichtcadmium",  [255, 193, 156]),
    ("lichtteal",     [140, 236, 235]),
    ("paars",         [142,  79, 153]),
    ("palecyan",      [180, 255, 255]),
    ("splitpea",      [144, 238, 144]),
]

_BINDER_PALETTE = [
    "white", "palecyan", "blauw3", "lichtblauw3", "lichtgroen",
    "lichtgoud", "lichtpaars", "lichtcadmium", "lichtteal", "paars",
]


def _register_colors():
    for name, rgb in _SAHTOE_COLORS:
        cmd.set_color(name, rgb)


# --- Helpers ---------------------------------------------------------------
def _read_topk_csv(run_dir, k):
    csv_path = os.path.join(run_dir, "05_ranking", "top_k.csv")
    rows = []
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            rows.append(r)
    rows.sort(key=lambda r: int(r["mmr_rank"]))
    return rows[:k]


def _parse_hotspots_yaml(yaml_path):
    """Return the int list from the first `binding: N,N,N` line found."""
    if not yaml_path or not os.path.exists(yaml_path):
        return []
    pat = re.compile(r"^\s*binding\s*:\s*(.+?)\s*$")
    with open(yaml_path) as f:
        for line in f:
            m = pat.match(line)
            if not m:
                continue
            spec = m.group(1).strip().strip("[]")
            out = []
            for tok in spec.split(","):
                tok = tok.strip()
                if tok.isdigit():
                    out.append(int(tok))
            if out:
                return out
    return []


def _design_yaml(run_dir, design_id):
    """
    design_id pattern: '<target>_<binder>_<region>_boltzgen_<NN>'
    YAML stem        : '<target>_<binder>_<region>_boltzgen'
    """
    stem = re.sub(r"_\d+$", "", design_id)
    return os.path.join(run_dir, "03_design_inputs", stem + ".yaml")


def _parse_native_from_yaml(yaml_path):
    """
    Returns (native_cif_path, native_target_chain_id) parsed from the
    first `file:` entity of a BoltzGen YAML. (Path, chain) or (None, None).
    """
    if not yaml_path or not os.path.exists(yaml_path):
        return None, None
    # Prefer PyYAML if available
    try:
        import yaml  # type: ignore
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        for ent in (data or {}).get("entities", []):
            file_block = ent.get("file") if isinstance(ent, dict) else None
            if not file_block:
                continue
            path = file_block.get("path")
            chain = None
            for inc in file_block.get("include") or []:
                ch = (inc or {}).get("chain") or {}
                if ch.get("id"):
                    chain = ch["id"]
                    break
            return path, chain
    except Exception:
        pass
    # Regex fallback
    with open(yaml_path) as f:
        text = f.read()
    m_path = re.search(r"file:\s*\n\s*path:\s*(\S+)", text)
    m_chain = re.search(r"include:\s*\n(?:\s*-\s*)?chain:\s*\n\s*id:\s*(\S+)", text)
    path = m_path.group(1).strip().strip("'\"") if m_path else None
    chain = m_chain.group(1).strip() if m_chain else None
    return path, chain


def _apply_starting_view():
    cmd.bg_color("black")
    cmd.hide("(h.)")
    cmd.set("dash_color", "yellow")
    cmd.set("dash_radius", 0.05)
    cmd.set("cartoon_oval_length", 1.15)
    cmd.set("cartoon_oval_width", 0.35)
    cmd.set("specular", 0)
    cmd.set("surface_quality", 1)
    cmd.set("ambient_occlusion_mode", 1)
    cmd.set("ray_trace_mode", 1)
    cmd.set("cartoon_transparency", 0.2)
    cmd.set("cartoon_highlight_color", "gray60")
    cmd.set("valence", 0, "", 0)
    cmd.set("cartoon_gap_cutoff", 0)


# --- Cycler state ----------------------------------------------------------
_state = {
    "designs": [],          # [(obj_name, [hotspot_resi, ...]), ...]
    "index":   0,
    "target":  "target_chain",
    "binder_chain": "B",
    "native":  None,        # name of the native-complex object, or None
}


def _highlight_hotspots(hotspot_ids):
    cmd.delete("hotspots")
    if not hotspot_ids:
        return
    sel = "+".join(str(r) for r in hotspot_ids)
    cmd.select("hotspots", "{} and resi {}".format(_state["target"], sel))
    cmd.show("sticks",  "hotspots and not name C+N+O")
    cmd.show("spheres", "hotspots and name CA")
    cmd.set("sphere_scale", 0.4, "hotspots and name CA")
    cmd.color("cadmiumorange", "hotspots")
    util.cnc("hotspots")
    cmd.label("hotspots and name CA", "'%s%s' % (resn, resi)")
    cmd.set("label_size", 14)
    cmd.set("label_color", "cadmiumorange")
    cmd.set("label_outline_color", "black")


def _show_only(index):
    designs = _state["designs"]
    if not designs:
        return
    index = index % len(designs)
    _state["index"] = index
    obj, hs = designs[index]
    bc = _state["binder_chain"]

    cmd.disable("all")
    cmd.enable(_state["target"])
    if _state["native"]:
        cmd.enable(_state["native"])
    cmd.enable(obj)

    _highlight_hotspots(hs)

    cmd.show("sticks",
             "{} and chain {} and not name C+N+O and not (h.)".format(obj, bc))
    cmd.select("binder_interface",
               "byres ({} and chain {} within 5 of hotspots)".format(obj, bc))

    cmd.wizard("message",
               "{}/{}  {}".format(index + 1, len(designs), obj))
    cmd.zoom("hotspots or ({} and chain {})".format(obj, bc), 6)


def cycle_up():
    _show_only(_state["index"] - 1)


def cycle_down():
    _show_only(_state["index"] + 1)


def show_all_designs():
    cmd.enable(_state["target"])
    if _state["native"]:
        cmd.enable(_state["native"])
    for obj, _ in _state["designs"]:
        cmd.enable(obj)
    # Show every binder's hotspots collectively (target side only)
    all_hs = sorted({r for _, hs in _state["designs"] for r in hs})
    _highlight_hotspots(all_hs)
    cmd.zoom("hotspots", 8)


def toggle_native():
    """Show / hide the native complex overlay."""
    nat = _state["native"]
    if not nat:
        print("No native complex loaded.")
        return
    enabled = nat in cmd.get_names("public_objects", enabled_only=1)
    (cmd.disable if enabled else cmd.enable)(nat)


# --- Main entry ------------------------------------------------------------
def show_topk(run_dir=None, k=10, target_chain="A", binder_chain="B"):
    """
    show_topk [run_dir [, k [, target_chain [, binder_chain]]]]

    run_dir defaults to cwd. k defaults to 10.
    """
    k = int(k)
    run_dir = os.path.abspath(run_dir or os.getcwd())
    if not os.path.exists(os.path.join(run_dir, "05_ranking", "top_k.csv")):
        print("ERROR: no 05_ranking/top_k.csv under {}".format(run_dir))
        print("       Pass a run dir explicitly:  show_topk /path/to/run")
        return

    cmd.reinitialize()
    _register_colors()
    cmd.bg_color("black")

    rows = _read_topk_csv(run_dir, k)
    if not rows:
        print("ERROR: top_k.csv is empty")
        return

    designs = []
    for r in rows:
        rank = int(r["mmr_rank"])
        path = r["cif_path"]
        design_id = r["design_id"]
        name = "rank{:02d}_{}".format(rank, design_id)
        cmd.load(path, name)
        hs = _parse_hotspots_yaml(_design_yaml(run_dir, design_id))
        designs.append((name, hs))

    ref = designs[0][0]
    for n, _ in designs[1:]:
        cmd.align(
            "{} and chain {} and name CA".format(n, target_chain),
            "{} and chain {} and name CA".format(ref, target_chain),
        )

    cmd.create("target_chain", "{} and chain {}".format(ref, target_chain))
    for n, _ in designs:
        cmd.hide("everything", "{} and chain {}".format(n, target_chain))

    # --- Native complex overlay (from the first design's YAML) -------------
    native_obj = None
    first_design_id = rows[0]["design_id"]
    nat_path, nat_target_chain = _parse_native_from_yaml(
        _design_yaml(run_dir, first_design_id))
    if nat_path and os.path.exists(nat_path):
        cmd.load(nat_path, "native_complex")
        native_obj = "native_complex"
        nat_target_chain = nat_target_chain or target_chain
        cmd.align(
            "native_complex and chain {} and name CA".format(nat_target_chain),
            "target_chain and name CA",
        )
        # Hide native's target-chain copy (duplicates the gold cartoon)
        cmd.hide("everything",
                 "native_complex and chain {}".format(nat_target_chain))
        # Anything that is NOT the target chain is the native partner(s)
        cmd.color("paars", "native_complex")
        util.cnc("native_complex")
    elif nat_path:
        print("WARN: native CIF referenced in YAML not found: {}".format(nat_path))

    cmd.show("cartoon")
    cmd.show("lines")
    _apply_starting_view()
    cmd.color("goud", "target_chain")
    for i, (n, _) in enumerate(designs):
        cmd.color(_BINDER_PALETTE[i % len(_BINDER_PALETTE)],
                  "{} and chain {}".format(n, binder_chain))
    if native_obj:
        cmd.color("paars",
                  "{} and not chain {}".format(native_obj, nat_target_chain))
        # Sticks on the native partner's side chains to make the
        # interface geometry directly comparable to the designs.
        cmd.show("sticks",
                 "{} and not chain {} and not name C+N+O and not (h.)".format(
                     native_obj, nat_target_chain))
    util.cnc("all")

    _state.update({
        "designs": designs,
        "index": 0,
        "target": "target_chain",
        "binder_chain": binder_chain,
        "native":  native_obj,
    })

    cmd.set_key("F1", cycle_up)
    cmd.set_key("F2", cycle_down)
    cmd.set_key("F3", show_all_designs)

    _show_only(0)

    n_designs = len(designs)
    n_with_hs = sum(1 for _, hs in designs if hs)
    print("Loaded {} designs from {}".format(n_designs, run_dir))
    print("Hotspot YAML parse: {}/{} designs".format(n_with_hs, n_designs))
    if native_obj:
        print("Native overlay   : {} (target chain {} hidden, partner in paars)".format(
              os.path.basename(nat_path), nat_target_chain))
    else:
        print("Native overlay   : (none — YAML had no parseable file: path)")
    print("F1 = prev,  F2 = next,  F3 = overlay all")
    print("Commands  : show_topk, cycle_up, cycle_down, show_all_designs, toggle_native")


# Expose to PyMOL command line
cmd.extend("show_topk",          show_topk)
cmd.extend("cycle_up",           cycle_up)
cmd.extend("cycle_down",         cycle_down)
cmd.extend("show_all_designs",   show_all_designs)
cmd.extend("toggle_native",      toggle_native)
