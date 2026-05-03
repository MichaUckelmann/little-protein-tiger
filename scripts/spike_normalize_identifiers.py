#!/usr/bin/env python3
"""
Sprint-1 spike: measure UniProt-resolution coverage on the fingerprint corpus.

Read-only. Walks every fingerprint, extracts protein names from each site
they appear, attempts a tiered lookup against the UniProt idmapping table,
and reports the coverage at each tier plus the unmatched names for review.

This script does NOT modify fingerprints. The production backfill
(`scripts/normalize_identifiers.py`) will be added in sprint 2 once we
have a coverage number to commit to.

Usage
-----
    python scripts/spike_normalize_identifiers.py
    python scripts/spike_normalize_identifiers.py --sample 500
    python scripts/spike_normalize_identifiers.py --dump-misses misses.txt
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import yaml
from loguru import logger

# Reuse existing normaliser so the spike measures the same string after the
# same preprocessing the graph already applies.
from src._corpus_graph import _normalize_protein, _PLACEHOLDERS  # noqa: E402


IDMAPPING_PATH = _ROOT / "data" / "depmap" / "HUMAN_9606_idmapping.dat.gz"


# ---------------------------------------------------------------------------
# UniProt index
# ---------------------------------------------------------------------------


def load_uniprot_index(idmapping_path: Path) -> dict[str, dict]:
    """Parse UniProt idmapping.dat.gz into lookup dicts.

    Returns:
        {
            "by_gene_symbol": {gene_symbol_upper: [uniprot_acc, ...]},
            "by_kb_id":       {uniprotkb_id: uniprot_acc},   # e.g. "1433B_HUMAN" -> "P31946"
            "by_kb_stem":     {stem_upper: [uniprot_acc, ...]},  # "1433B" -> ["P31946"]
            "all_accessions": set(uniprot_acc),
        }

    Lines are tab-separated: ``accession <TAB> key_type <TAB> value``.
    We keep only ``Gene_Name`` and ``UniProtKB-ID`` rows; everything else is
    irrelevant for sprint-1 resolution.
    """
    by_gene: dict[str, list[str]] = defaultdict(list)
    by_kb_id: dict[str, str] = {}
    by_kb_stem: dict[str, list[str]] = defaultdict(list)
    accessions: set[str] = set()

    keep = {"Gene_Name", "UniProtKB-ID", "Gene_Synonym"}
    by_synonym: dict[str, list[str]] = defaultdict(list)

    with gzip.open(idmapping_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 3:
                continue
            acc, key, value = parts
            if key not in keep:
                continue
            accessions.add(acc)
            if key == "Gene_Name":
                by_gene[value.upper()].append(acc)
            elif key == "Gene_Synonym":
                by_synonym[value.upper()].append(acc)
            elif key == "UniProtKB-ID":
                by_kb_id[value.upper()] = acc
                stem = value.split("_", 1)[0].upper()
                by_kb_stem[stem].append(acc)

    return {
        "by_gene_symbol": dict(by_gene),
        "by_synonym": dict(by_synonym),
        "by_kb_id": by_kb_id,
        "by_kb_stem": dict(by_kb_stem),
        "all_accessions": accessions,
    }


# ---------------------------------------------------------------------------
# Fingerprint name extraction
# ---------------------------------------------------------------------------


def _split_compound(name: str) -> list[str]:
    """Split obvious complex names like 'PD-1/PD-L1' into components.

    Conservative — only splits on '/' or ' / ' between identifier-shaped
    tokens. Does NOT split on hyphens, since 'SREBP-1' is one entity, not two.
    """
    if not name or "/" not in name:
        return [name] if name else []
    parts = [p.strip() for p in re.split(r"\s*/\s*", name) if p.strip()]
    # Re-glue if either side is too short to plausibly be a separate protein.
    out = [p for p in parts if len(p) >= 2]
    return out or ([name] if name else [])


def extract_protein_names(fp: dict) -> list[tuple[str, str, list[int]]]:
    """Pull every (raw_name, source_path, taxon_list) tuple from a fingerprint.

    Sites covered:
      - key_findings[i].protein_pair[j]
      - pathway_context.target_nodes[i].protein
      - pathway_context.upstream_regulators[i]
      - pathway_context.downstream_effectors[i]
      - entities.proteins[i]

    The same raw_name appearing in multiple sites is yielded once per site —
    deduplication happens later when we resolve.
    """
    taxa = (fp.get("methodology") or {}).get("protein_origin_organism") or []
    if not isinstance(taxa, list):
        taxa = [taxa]
    taxa = [t for t in taxa if isinstance(t, int)]

    out: list[tuple[str, str, list[int]]] = []

    for i, kf in enumerate(fp.get("key_findings") or []):
        pair = kf.get("protein_pair") or []
        for j, name in enumerate(pair):
            if name:
                for piece in _split_compound(name):
                    out.append((piece, f"key_findings[{i}].protein_pair[{j}]", taxa))

    pc = fp.get("pathway_context") or {}
    for i, tn in enumerate(pc.get("target_nodes") or []):
        name = tn.get("protein") if isinstance(tn, dict) else None
        if name:
            for piece in _split_compound(name):
                out.append((piece, f"pathway_context.target_nodes[{i}].protein", taxa))
    for i, name in enumerate(pc.get("upstream_regulators") or []):
        if name:
            for piece in _split_compound(name):
                out.append((piece, f"pathway_context.upstream_regulators[{i}]", taxa))
    for i, name in enumerate(pc.get("downstream_effectors") or []):
        if name:
            for piece in _split_compound(name):
                out.append((piece, f"pathway_context.downstream_effectors[{i}]", taxa))

    ents = fp.get("entities") or {}
    for i, name in enumerate(ents.get("proteins") or []):
        if name:
            for piece in _split_compound(name):
                out.append((piece, f"entities.proteins[{i}]", taxa))

    return out


# ---------------------------------------------------------------------------
# Tiered resolver
# ---------------------------------------------------------------------------


# Recognised mutant suffix patterns we strip before lookup. Examples:
# "KRAS-G12C", "BRAF-V600E", "p53-R175H".
_MUTANT_RE = re.compile(r"[-\s]?[A-Z]\d{1,4}[A-Z]$", re.IGNORECASE)


def _strip_mutant(name: str) -> str:
    """Return name with a trailing single-residue mutation stripped, if any."""
    s = name.strip()
    m = _MUTANT_RE.search(s)
    if m and m.start() > 1:
        return s[: m.start()].rstrip("-").rstrip()
    return s


def resolve(name: str, idx: dict) -> tuple[str | None, list[str], str]:
    """Try to resolve a raw name to a UniProt accession.

    Returns ``(tier, accessions, normalised_form_used)``:
      - ``tier``: one of "exact_gene", "kb_id", "kb_stem", "stripped_mutant",
        "synonym", "fuzzy", "low", or None when unresolved.
      - ``accessions``: list of candidate UniProt accessions (1+ for exact,
        possibly several for ambiguous gene-symbol aliases).

    The spike does not yet use taxon for disambiguation — sprint 2 will,
    once we have a non-human ortholog table.
    """
    if not name:
        return None, [], ""
    norm = _normalize_protein(name)
    if not norm or norm in _PLACEHOLDERS:
        return None, [], ""

    # 1. exact gene symbol
    hits = idx["by_gene_symbol"].get(norm)
    if hits:
        return "exact_gene", hits, norm

    # 2. UniProtKB-ID exact (e.g. "YAP1_HUMAN" -> P46937)
    hit = idx["by_kb_id"].get(norm)
    if hit:
        return "kb_id", [hit], norm

    # 3. UniProtKB-ID stem (e.g. "1433B" -> P31946; less common but seen)
    hits = idx["by_kb_stem"].get(norm)
    if hits:
        return "kb_stem", hits, norm

    # 4. Strip mutant suffix and retry exact gene symbol
    stripped = _normalize_protein(_strip_mutant(name))
    if stripped and stripped != norm:
        hits = idx["by_gene_symbol"].get(stripped)
        if hits:
            return "stripped_mutant", hits, stripped

    # 5. Synonym table
    hits = idx["by_synonym"].get(norm)
    if hits:
        return "synonym", hits, norm

    # 6. Fuzzy: gene symbol that is a prefix-match (covers "TEAD" -> TEAD1..4
    #    family-style queries; will produce multiple hits, low confidence).
    if len(norm) >= 4:
        prefix_hits: list[str] = []
        for sym, accs in idx["by_gene_symbol"].items():
            if sym.startswith(norm) and len(sym) <= len(norm) + 2:
                prefix_hits.extend(accs)
        if prefix_hits:
            return "fuzzy", sorted(set(prefix_hits)), norm

    return None, [], norm


# ---------------------------------------------------------------------------
# Spike main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="If set, only process the first N fingerprints (sorted lexicographically).",
    )
    parser.add_argument(
        "--dump-misses",
        type=Path,
        default=None,
        help="Path to write unmatched names (one per line, with mention count).",
    )
    parser.add_argument(
        "--dump-low",
        type=Path,
        default=None,
        help="Path to write fuzzy/low-confidence resolutions for review.",
    )
    args = parser.parse_args()

    cfg = yaml.safe_load((_ROOT / args.config).read_text(encoding="utf-8"))
    fingerprint_dir = (_ROOT / cfg["paths"]["fingerprint_dir"]).resolve()
    if not fingerprint_dir.exists():
        sys.exit(f"ERROR: fingerprint dir not found: {fingerprint_dir}")
    if not IDMAPPING_PATH.exists():
        sys.exit(f"ERROR: idmapping not found: {IDMAPPING_PATH}")

    logger.info(f"Loading UniProt idmapping from {IDMAPPING_PATH.name} ...")
    idx = load_uniprot_index(IDMAPPING_PATH)
    logger.info(
        f"  gene_symbol entries: {len(idx['by_gene_symbol']):,} | "
        f"kb_id entries: {len(idx['by_kb_id']):,} | "
        f"synonyms: {len(idx['by_synonym']):,} | "
        f"distinct accessions: {len(idx['all_accessions']):,}"
    )

    files = sorted(fingerprint_dir.glob("*.json"))
    if args.sample:
        files = files[: args.sample]
    logger.info(f"Scanning {len(files):,} fingerprints from {fingerprint_dir}")

    # Aggregate by raw-name (not per-occurrence) so coverage % is unique-name based.
    name_mentions: Counter[str] = Counter()
    name_first_source: dict[str, str] = {}
    name_taxa: dict[str, set[int]] = defaultdict(set)
    occurrence_count = 0
    parse_failures = 0

    for fp_file in files:
        try:
            fp = json.loads(fp_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            parse_failures += 1
            continue
        for raw_name, src, taxa in extract_protein_names(fp):
            occurrence_count += 1
            name_mentions[raw_name] += 1
            name_first_source.setdefault(raw_name, f"{fp_file.name}::{src}")
            for t in taxa:
                name_taxa[raw_name].add(t)

    logger.info(
        f"  parse failures: {parse_failures} | "
        f"protein-name occurrences: {occurrence_count:,} | "
        f"unique raw names: {len(name_mentions):,}"
    )

    # Resolve every unique name.
    tier_counts: Counter[str] = Counter()
    tier_mention_counts: Counter[str] = Counter()
    misses: list[tuple[str, int, str, list[int]]] = []
    fuzzy_or_kb_stem: list[tuple[str, int, list[str], str, list[int]]] = []
    ambiguous_exact: list[tuple[str, int, list[str], list[int]]] = []

    for name, count in name_mentions.most_common():
        tier, accs, _norm = resolve(name, idx)
        bucket = tier or "unmatched"
        tier_counts[bucket] += 1
        tier_mention_counts[bucket] += count
        if tier is None:
            misses.append((name, count, name_first_source.get(name, ""), sorted(name_taxa[name])))
        elif tier in {"fuzzy", "kb_stem"}:
            fuzzy_or_kb_stem.append((name, count, accs, tier, sorted(name_taxa[name])))
        elif tier == "exact_gene" and len(accs) > 1:
            ambiguous_exact.append((name, count, accs, sorted(name_taxa[name])))

    # ------------------------------------------------------------------
    # Coverage report
    # ------------------------------------------------------------------
    total_unique = len(name_mentions)
    total_mentions = sum(name_mentions.values())

    def pct(n: int, d: int) -> str:
        return f"{(100.0 * n / d):.1f}%" if d else "n/a"

    logger.info("--- coverage by tier (unique raw names) ---")
    for tier in [
        "exact_gene", "kb_id", "kb_stem", "stripped_mutant",
        "synonym", "fuzzy", "unmatched",
    ]:
        c = tier_counts.get(tier, 0)
        logger.info(f"  {tier:18s} {c:>6,}  ({pct(c, total_unique)})")

    logger.info("--- coverage by tier (weighted by mention count) ---")
    for tier in [
        "exact_gene", "kb_id", "kb_stem", "stripped_mutant",
        "synonym", "fuzzy", "unmatched",
    ]:
        c = tier_mention_counts.get(tier, 0)
        logger.info(f"  {tier:18s} {c:>8,}  ({pct(c, total_mentions)})")

    high_conf_unique = sum(
        tier_counts.get(t, 0) for t in ("exact_gene", "kb_id", "stripped_mutant", "synonym")
    )
    high_conf_mentions = sum(
        tier_mention_counts.get(t, 0) for t in ("exact_gene", "kb_id", "stripped_mutant", "synonym")
    )
    logger.info(
        f"  HIGH-CONF (exact+kb_id+stripped_mutant+synonym): "
        f"unique {pct(high_conf_unique, total_unique)} / "
        f"weighted {pct(high_conf_mentions, total_mentions)}"
    )
    logger.info(
        f"  ambiguous gene_symbol (multi-acc): "
        f"{len(ambiguous_exact):,} unique names"
    )

    # ------------------------------------------------------------------
    # Side files
    # ------------------------------------------------------------------
    if args.dump_misses:
        out_path = (_ROOT / args.dump_misses) if not args.dump_misses.is_absolute() else args.dump_misses
        out_path.parent.mkdir(parents=True, exist_ok=True)
        misses.sort(key=lambda r: -r[1])
        with out_path.open("w", encoding="utf-8") as fh:
            fh.write("# unmatched_name\tmention_count\tfirst_source\ttaxa\n")
            for name, count, src, taxa in misses:
                fh.write(f"{name}\t{count}\t{src}\t{taxa}\n")
        logger.info(f"  wrote {len(misses):,} unmatched names → {out_path}")

    if args.dump_low:
        out_path = (_ROOT / args.dump_low) if not args.dump_low.is_absolute() else args.dump_low
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fuzzy_or_kb_stem.sort(key=lambda r: -r[1])
        with out_path.open("w", encoding="utf-8") as fh:
            fh.write("# raw_name\tmention_count\ttier\taccessions\ttaxa\n")
            for name, count, accs, tier, taxa in fuzzy_or_kb_stem:
                fh.write(f"{name}\t{count}\t{tier}\t{','.join(accs[:5])}\t{taxa}\n")
        logger.info(f"  wrote {len(fuzzy_or_kb_stem):,} low-confidence resolutions → {out_path}")

    # Top-20 unmatched preview always, even without --dump-misses.
    if misses:
        logger.info("--- top-20 unmatched by mention count ---")
        misses.sort(key=lambda r: -r[1])
        for name, count, _src, taxa in misses[:20]:
            taxa_str = ",".join(str(t) for t in taxa) if taxa else "?"
            logger.info(f"  {count:>5}x  '{name}'  taxa={taxa_str}")


if __name__ == "__main__":
    main()
