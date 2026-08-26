#!/usr/bin/env python3
"""See LPT do something real, in about a minute, for nothing.

No API key, no GPU, no corpus, no foundry — just the base install and a network
connection. It downloads one structure from RCSB, analyses a real protein
interface, and (if the 52 MB reference data is present) resolves a gene name to
a ranked table of candidate epitopes: the first stage of the binder track, which
is the most convincing thing this pipeline does cheaply.

    python scripts/quickstart.py

The point is a working baseline. When something later fails, you will know the
install itself was fine.
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.env_config import load_env  # noqa: E402

load_env(_ROOT / ".env")

# TEAD1/YAP1 — a real, well-characterised protein-protein interface, small
# enough to analyse in seconds and a genuine drug-discovery target.
DEMO_PDB = "3KYS"
DEMO_CHAINS = ("A", "B")
DEMO_TARGET = "KRAS"

_BOLD, _DIM, _GREEN, _RESET = "\033[1m", "\033[90m", "\033[32m", "\033[0m"
if not sys.stdout.isatty():
    _BOLD = _DIM = _GREEN = _RESET = ""


def _step(n: int, total: int, text: str) -> None:
    print(f"\n{_BOLD}[{n}/{total}] {text}{_RESET}")


def _note(text: str) -> None:
    print(f"      {_DIM}{text}{_RESET}")


def fetch_structure(pdb_id: str) -> Path:
    """Download one assembly from RCSB into the normal structures cache."""
    import requests

    dest_dir = _ROOT / "data" / "structures"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{pdb_id.lower()}.cif"
    if dest.is_file():
        _note(f"already cached: {dest.relative_to(_ROOT)}")
        return dest

    url = f"https://files.rcsb.org/download/{pdb_id.upper()}.cif.gz"
    _note(f"GET {url}")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    tmp = dest.with_suffix(".cif.part")
    tmp.write_bytes(gzip.decompress(resp.content))
    tmp.replace(dest)
    _note(f"saved {dest.stat().st_size/1024:.0f} KB -> {dest.relative_to(_ROOT)}")
    return dest


def analyse(path: Path, chain_a: str, chain_b: str) -> dict:
    from src.structure_tools import analyze_interface

    res = analyze_interface(str(path), chain_a, chain_b)
    iface = res.get("interface") or {}
    bsa = iface.get("bsa_total_A2") or 0.0
    res_a = res.get("chain_a_interface_residues") or []
    res_b = res.get("chain_b_interface_residues") or []
    hbonds = sum(1 for r in res_a for c in (r.get("contacts") or [])
                 if c.get("interaction") == "h_bond")

    print(f"      buried surface area   {bsa:>8,.0f} A^2")
    print(f"      interface residues    {len(res_a):>8} on {chain_a}, "
          f"{len(res_b)} on {chain_b}")
    print(f"      hydrogen bonds        {hbonds:>8}")

    # Rank by contact count — the residues a binder would need to engage.
    hot = sorted(res_a, key=lambda r: -(r.get("n_contacts") or 0))[:5]
    if hot:
        print(f"\n      {_BOLD}most-contacted residues on chain {chain_a}{_RESET}")
        print(f"      {'residue':>10}  {'contacts':>8}  {'ddG est':>8}  type")
        for r in hot:
            label = f"{r.get('residue','?')}{r.get('resnum','')}"
            ddg = r.get("ddg_estimate_kcal_mol")
            print(f"      {label:>10}  {r.get('n_contacts',0):>8}  "
                  f"{(f'{ddg:.1f}' if ddg is not None else '-'):>8}  "
                  f"{r.get('type','')}")
    return res


def resolve_and_rank(target: str) -> bool:
    """The binder track's first stage: gene name -> ranked candidate epitopes.

    Returns False (without raising) when the reference data is absent, which is
    the normal state of a fresh clone.
    """
    from src.identifier_normalizer import ReferenceDataMissing

    try:
        resolved, rows = _candidate_table(target)
    except ReferenceDataMissing:
        _note("skipped — reference data not downloaded yet.")
        _note("run: python scripts/fetch_reference_data.py   (~52 MB, public)")
        return False

    if not resolved or not resolved.ok:
        _note(f"could not resolve {target!r} to a human UniProt accession")
        return False

    print(f"      {target}  ->  {resolved.gene} / {resolved.uniprot}  "
          f"({resolved.match_confidence})")
    _note("resolved offline from the UniProt/HGNC mapping — no API key involved")

    if not rows:
        _note("no interface cleared the minimum buried-area threshold")
        return True

    print(f"\n      {_BOLD}candidate epitopes, ranked by buried surface area{_RESET}")
    print(f"      {'PDB':>5}  {'chain':>5}  {'partner':<28} {'BSA A^2':>8}  {'res':>4}")
    for c in rows[:5]:
        partner = str(getattr(c, "partner_name", None)
                      or getattr(c, "partner_chain", "?"))[:28]
        print(f"      {str(getattr(c, 'pdb_id', '?')):>5}  "
              f"{str(getattr(c, 'target_chain', '?')):>5}  {partner:<28} "
              f"{(getattr(c, 'bsa_A2', 0) or 0):>8,.0f}  "
              f"{getattr(c, 'n_interface_residues', '?'):>4}")
    _note("this is exactly what `--workflow binder --target " + target
          + "` starts from")
    return True


def _candidate_table(target: str):
    from src.target_resolve import build_candidate_table

    return build_candidate_table(
        target, structures_dir=_ROOT / "data" / "structures", max_entries=6)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run a real LPT analysis in ~1 minute, with no keys or GPU.")
    ap.add_argument("--pdb", default=DEMO_PDB, help=f"PDB id (default {DEMO_PDB})")
    ap.add_argument("--chains", nargs=2, metavar=("A", "B"), default=list(DEMO_CHAINS),
                    help="the two chains to analyse")
    ap.add_argument("--target", default=DEMO_TARGET,
                    help=f"gene symbol for the resolve step (default {DEMO_TARGET})")
    ap.add_argument("--skip-target", action="store_true",
                    help="skip the target-resolution step (it needs reference data)")
    args = ap.parse_args()

    started = time.time()
    total = 2 if args.skip_target else 3

    print(f"\n{_BOLD}LPT quickstart{_RESET} — no API key, no GPU, no corpus needed.")

    try:
        _step(1, total, f"Fetch a real structure ({args.pdb}) from RCSB")
        path = fetch_structure(args.pdb)

        _step(2, total, f"Analyse the {args.chains[0]}/{args.chains[1]} interface")
        analyse(path, *args.chains)

        if not args.skip_target:
            _step(3, total,
                  f"Resolve {args.target!r} and rank its candidate epitopes")
            resolve_and_rank(args.target)
    except Exception as exc:
        print(f"\n{_BOLD}Quickstart failed:{_RESET} {type(exc).__name__}: {exc}")
        print("\nRun `python scripts/doctor.py` to see what this machine is "
              "missing.\nIf it reports STRUCTURE as READY, this is a bug — "
              "please open an issue.")
        return 1

    secs = time.time() - started
    print(f"\n{_GREEN}Done in {secs:.0f}s.{_RESET} Everything above ran locally "
          f"against public data.\n")
    print("Next:")
    print("  python scripts/doctor.py             what else this machine can run")
    print("  docs/journal-filtering.md            read BEFORE building a corpus")
    print("  README.md                            the two design workflows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
