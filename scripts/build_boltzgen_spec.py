"""CLI over :mod:`src.boltzgen_spec` — write one BoltzGen design YAML.

A thin shim on purpose. The logic (numbering, grounding, the equal-length
refusal, chain-id choice, trim-as-`res_index`) lives in the module so the
backend seam and this command cannot drift apart; this file only parses
arguments and prints what was built. Same division as
`src/structure_tools.py` vs `src/structure_tools_server.py`.

Usage:

    python scripts/build_boltzgen_spec.py \
        --pdb data/structures/3N7S_ba1.cif --chain D \
        --hotspots TRP74,PHE83,TRP84,PRO85,ASP90 \
        --modality cyclic_peptide --name RAMP1_cyclic --out /tmp/specs --check

`--hotspots` takes `NAME<auth>` pairs so the residue names can be checked
against the file — a bare number cannot be grounded, and an ungrounded
numbering is the documented way to design against the wrong residues (see
`boltzgen_spec.resolve_binding`). Bare numbers are still accepted and simply
skip that check, with a warning.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from src.boltzgen_spec import (  # noqa: E402
    PROTOCOL_BY_MODALITY, SpecError, build_boltzgen_spec,
)

_HOTSPOT_RE = re.compile(r"^([A-Za-z]{0,3})(\d+)$")


def parse_hotspots(text: str) -> list[dict]:
    """`NAME<auth>` or bare `<auth>`, comma-separated."""
    out: list[dict] = []
    bare = 0
    for tok in (t.strip() for t in text.split(",") if t.strip()):
        m = _HOTSPOT_RE.match(tok)
        if not m:
            raise SystemExit(f"cannot parse hotspot {tok!r}; expected e.g. "
                             f"TRP74 or 74")
        name, auth = m.group(1).upper(), int(m.group(2))
        if not name:
            bare += 1
        out.append({"residue": name, "auth_seq_id": auth})
    if bare:
        logger.warning(
            f"{bare} hotspot(s) given without a residue name — grounding is "
            f"skipped for those. A number alone cannot be checked against the "
            f"file, and a mis-numbered epitope is silent.")
    return out


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pdb", type=Path, required=True,
                    help="the DEPOSITED structure that will appear in `path:`. "
                         "Its numbering is what binding/res_index index into.")
    ap.add_argument("--chain", required=True, help="target chain (auth id)")
    ap.add_argument("--hotspots", required=True,
                    help="comma-separated NAME<auth> pairs, e.g. TRP74,PHE83")
    ap.add_argument("--modality", required=True,
                    choices=sorted(PROTOCOL_BY_MODALITY))
    ap.add_argument("--name", required=True, help="stem for <name>_boltzgen.yaml")
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    ap.add_argument("--kept-segments", default=None,
                    help="author-numbered trim spans as lo-hi[,lo-hi...]; "
                         "becomes res_index. Omit to use the whole chain.")
    ap.add_argument("--binder-chain", default=None,
                    help="override the auto-chosen binder chain id")
    ap.add_argument("--check", action="store_true",
                    help="run `boltzgen check` on the result and fail if it does")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    segs = None
    if args.kept_segments:
        segs = [[int(x) for x in s.split("-")]
                for s in args.kept_segments.split(",") if s.strip()]

    out = args.out / f"{args.name}_boltzgen.yaml"
    try:
        spec = build_boltzgen_spec(
            name=args.name, structure_path=args.pdb, target_chain=args.chain,
            hotspots=parse_hotspots(args.hotspots), out_path=out,
            modality=args.modality, kept_segments=segs,
            binder_chain=args.binder_chain)
    except SpecError as exc:
        raise SystemExit(f"refused: {exc}")

    print(f"wrote {spec.path}")
    print(f"  binding (label_seq): {','.join(map(str, spec.binding))}")
    print(f"  binder chain {spec.binder_chain}, length "
          f"{spec.binder_min}..{spec.binder_max}, protocol {spec.protocol}")
    if spec.res_index:
        print("  res_index: "
              + ",".join(f"{lo}..{hi}" for lo, hi in spec.res_index))
    for w in spec.warnings:
        print(f"  ! {w}")

    if args.check:
        import yaml as _yaml

        from src.env_config import load_env, resolve_env_path

        load_env()
        cfg = _yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        ws = ((cfg.get("design") or {}).get("workstation")) or {}
        exe = (resolve_env_path("LPT_BOLTZGEN_EXECUTABLE",
                                ws.get("boltzgen_executable")) or "boltzgen")
        try:
            proc = subprocess.run([str(exe), "check", str(spec.path)],
                                  capture_output=True, text=True)
        except FileNotFoundError:
            raise SystemExit(
                f"boltzgen executable not found at {exe!r}. Set "
                f"LPT_BOLTZGEN_EXECUTABLE in .env or "
                f"design.workstation.boltzgen_executable in config.yaml. "
                f"(The YAML was written to {spec.path}.)")
        if proc.returncode != 0:
            tail = (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:]
            raise SystemExit(f"`boltzgen check` rejected {spec.path}:\n{tail}")
        print("  boltzgen check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
