#!/usr/bin/env python3
"""
check_pdb_integrity.py
Checks a PDB file for common issues that can cause downstream failures
in binder design pipelines (Proteina-Complexa, RFDiffusion, etc.).

Usage:
    python check_pdb_integrity.py structure.pdb [--hotspots A314,A366,A344] [--input-spec "A195-229,A239-411"]
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path


# ── ANSI colours ────────────────────────────────────────────────────────────
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):    print(f"  {GREEN}✓{RESET}  {msg}")
def warn(msg):  print(f"  {YELLOW}⚠{RESET}  {msg}")
def err(msg):   print(f"  {RED}✗{RESET}  {msg}")
def info(msg):  print(f"  {CYAN}·{RESET}  {msg}")
def header(msg): print(f"\n{BOLD}{msg}{RESET}")


# ── PDB parser ───────────────────────────────────────────────────────────────
def parse_pdb(path: Path):
    """
    Returns:
        atoms      : list of dicts per ATOM/HETATM record
        chain_res  : {chain_id: sorted list of unique residue numbers}
        hetatms    : {resname: count}
        alt_locs   : set of (chain, resnum, alt_loc) tuples
        remarks    : list of REMARK lines
    """
    atoms     = []
    chain_res = defaultdict(set)
    hetatms   = defaultdict(int)
    alt_locs  = set()
    remarks   = []

    with open(path) as fh:
        for line in fh:
            rec = line[:6].strip()

            if rec == "REMARK":
                remarks.append(line.rstrip())

            if rec in ("ATOM", "HETATM"):
                try:
                    alt   = line[16].strip()
                    rname = line[17:20].strip()
                    chain = line[21].strip()
                    resnum = int(line[22:26].strip())
                    icode  = line[26].strip()        # insertion code
                except (ValueError, IndexError):
                    continue

                atom = {
                    "rec":    rec,
                    "rname":  rname,
                    "chain":  chain,
                    "resnum": resnum,
                    "icode":  icode,
                    "alt":    alt,
                    "line":   line.rstrip(),
                }
                atoms.append(atom)

                if rec == "ATOM":
                    chain_res[chain].add(resnum)
                    if alt and alt != " ":
                        alt_locs.add((chain, resnum, alt))
                else:
                    if rname not in ("HOH", "WAT", "DOD"):
                        hetatms[rname] += 1

    chain_res = {c: sorted(v) for c, v in chain_res.items()}
    return atoms, chain_res, dict(hetatms), alt_locs, remarks


# ── Gap finder ───────────────────────────────────────────────────────────────
def find_gaps(residues: list[int]):
    gaps = []
    for i in range(1, len(residues)):
        diff = residues[i] - residues[i - 1]
        if diff > 1:
            gaps.append((residues[i - 1], residues[i],
                         list(range(residues[i - 1] + 1, residues[i]))))
    return gaps  # [(before, after, [missing...]), ...]


# ── Range parser ─────────────────────────────────────────────────────────────
def parse_input_spec(spec: str):
    """
    Parse 'A195-229, A239-411' → {chain: [(start, end), ...]}
    """
    ranges = defaultdict(list)
    for part in spec.replace(" ", "").split(","):
        part = part.strip()
        if not part:
            continue
        chain = part[0]
        rest  = part[1:]
        if "-" in rest:
            s, e = rest.split("-", 1)
            ranges[chain].append((int(s), int(e)))
        else:
            n = int(rest)
            ranges[chain].append((n, n))
    return dict(ranges)


def parse_hotspots(spec: str):
    """
    Parse 'A314,A366,A344' → {chain: [resnum, ...]}
    """
    hs = defaultdict(list)
    for part in spec.replace(" ", "").split(","):
        part = part.strip()
        if not part:
            continue
        chain  = part[0]
        resnum = int(part[1:])
        hs[chain].append(resnum)
    return dict(hs)


# ── Main checks ──────────────────────────────────────────────────────────────
def run_checks(path: Path, hotspot_spec: str = "", input_spec: str = ""):
    issues = 0

    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  PDB Integrity Check: {path.name}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    if not path.exists():
        err(f"File not found: {path}")
        return 1

    atoms, chain_res, hetatms, alt_locs, remarks = parse_pdb(path)

    # ── 1. Basic file stats ──────────────────────────────────────────────────
    header("1. File overview")
    info(f"Total ATOM records : {sum(1 for a in atoms if a['rec']=='ATOM')}")
    info(f"Total HETATM records: {sum(1 for a in atoms if a['rec']=='HETATM')}")
    info(f"Chains found       : {list(chain_res.keys())}")
    for chain, res in chain_res.items():
        info(f"  Chain {chain}: {len(res)} residues  ({res[0]}–{res[-1]})")

    # ── 2. Gaps ──────────────────────────────────────────────────────────────
    header("2. Chain gaps")
    any_gaps = False
    for chain, res in chain_res.items():
        gaps = find_gaps(res)
        if gaps:
            any_gaps = True
            for before, after, missing in gaps:
                n = len(missing)
                issues += 1
                err(f"Chain {chain}: gap between {before} and {after} "
                    f"— {n} missing residue(s): {missing if n <= 10 else str(missing[:10])+'…'}")
        else:
            ok(f"Chain {chain}: no gaps")
    if not any_gaps:
        ok("All chains are gap-free")

    # ── 3. Insertion codes ───────────────────────────────────────────────────
    header("3. Insertion codes")
    icodes = [(a["chain"], a["resnum"], a["icode"])
              for a in atoms if a["rec"] == "ATOM" and a["icode"]]
    if icodes:
        unique_ic = sorted(set(icodes))
        for chain, resnum, ic in unique_ic:
            warn(f"Chain {chain} residue {resnum} has insertion code '{ic}' — "
                 "may confuse index-based pipelines")
            issues += 1
    else:
        ok("No insertion codes")

    # ── 4. Alternate locations ───────────────────────────────────────────────
    header("4. Alternate locations")
    if alt_locs:
        for chain, resnum, alt in sorted(alt_locs):
            warn(f"Chain {chain} residue {resnum} alt-loc '{alt}' — "
                 "consider keeping only alt A")
            issues += 1
    else:
        ok("No alternate locations")

    # ── 5. Non-solvent HETATM ────────────────────────────────────────────────
    header("5. Non-solvent HETATM records")
    if hetatms:
        for rname, count in sorted(hetatms.items()):
            warn(f"{rname}: {count} atom(s) — ligand/ion present, "
                 "check whether pipeline handles it")
            issues += 1
    else:
        ok("No non-solvent HETATM records")

    # ── 6. Input spec check ──────────────────────────────────────────────────
    if input_spec:
        header("6. Input spec residue check")
        spec_ranges = parse_input_spec(input_spec)
        for chain, ranges in spec_ranges.items():
            present = set(chain_res.get(chain, []))
            for start, end in ranges:
                expected = set(range(start, end + 1))
                missing  = sorted(expected - present)
                if missing:
                    err(f"Chain {chain} {start}–{end}: {len(missing)} missing residue(s): {missing}")
                    issues += len(missing)
                else:
                    ok(f"Chain {chain} {start}–{end}: all {end-start+1} residues present")
    else:
        header("6. Input spec check")
        info("No --input-spec provided, skipping")

    # ── 7. Hotspot check ─────────────────────────────────────────────────────
    if hotspot_spec:
        header("7. Hotspot residue check")
        hs = parse_hotspots(hotspot_spec)
        for chain, resnums in hs.items():
            present = set(chain_res.get(chain, []))
            for r in resnums:
                if r in present:
                    ok(f"Chain {chain} hotspot {r}: present")
                else:
                    err(f"Chain {chain} hotspot {r}: MISSING — will cause index-out-of-bounds crash")
                    issues += 1
    else:
        header("7. Hotspot check")
        info("No --hotspots provided, skipping")

    # ── 8. Numbering sanity ──────────────────────────────────────────────────
    header("8. Residue numbering sanity")
    for chain, res in chain_res.items():
        negs = [r for r in res if r <= 0]
        if negs:
            warn(f"Chain {chain}: non-positive residue numbers {negs[:10]} — "
                 "renumber before use")
            issues += 1
        else:
            ok(f"Chain {chain}: all residue numbers positive")

    # ── Summary ──────────────────────────────────────────────────────────────
    header("Summary")
    if issues == 0:
        print(f"  {GREEN}{BOLD}All checks passed — structure looks clean.{RESET}\n")
    else:
        print(f"  {RED}{BOLD}{issues} issue(s) found — review above before running pipeline.{RESET}\n")

    return issues


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Check PDB integrity for binder design pipelines."
    )
    parser.add_argument("pdb", type=Path, help="Path to PDB file")
    parser.add_argument(
        "--hotspots", default="",
        help="Comma-separated hotspot residues, e.g. A314,A366,A344"
    )
    parser.add_argument(
        "--input-spec", default="",
        help="Input spec string, e.g. 'A195-229,A239-411'"
    )
    args = parser.parse_args()

    issues = run_checks(args.pdb, args.hotspots, args.input_spec)
    sys.exit(0 if issues == 0 else 1)


if __name__ == "__main__":
    main()