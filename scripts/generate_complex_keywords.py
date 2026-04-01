#!/usr/bin/env python3
"""
Generate NCBI-format literature search keywords for a list of predicted protein complexes.

For each complex, looks up each protein in UniProt to retrieve:
  - Gene name (for cleaner NCBI queries)
  - GO biological process terms (pathway signals)
  - Protein name / function (for context)

Then generates a set of targeted NCBI Title/Abstract queries covering:
  - Direct protein interaction searches
  - Pathway/function searches derived from shared GO terms
  - Disease association searches for medically relevant pathways

Output can be:
  - Printed to stdout (for inspection / manual addition to config.yaml)
  - Written directly to config.yaml --append-to-config

Usage:
    python scripts/generate_complex_keywords.py --input complexes.csv
    python scripts/generate_complex_keywords.py --input complexes.json
    python scripts/generate_complex_keywords.py --input complexes.csv --append-to-config config.yaml
    python scripts/generate_complex_keywords.py --input complexes.csv --output keywords.yaml --dry-run

Input formats:
    CSV: one complex per row, protein gene names comma-separated in a single column
         e.g.  complex_id,proteins
               complex_001,ARFRP1,JTB,SYS1,ARL1
         OR a headerless CSV where each row is a complex:
               ARFRP1,JTB,SYS1,ARL1

    JSON: list of objects or list of lists
         [{"id": "complex_001", "proteins": ["ARFRP1", "JTB", "SYS1", "ARL1"]}, ...]
         OR [["ARFRP1", "JTB", "SYS1", "ARL1"], ...]
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Optional

import requests
import yaml
from loguru import logger

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

UNIPROT_BASE = "https://rest.uniprot.org/uniprotkb"
UNIPROT_DELAY_S = 0.3   # ~3 req/s — well within UniProt fair-use limits

# GO biological process terms most useful for generating pathway search keywords.
# GO:0007165 = signal transduction; GO:0006915 = apoptosis; etc.
# We match on term name substrings rather than IDs so new GO terms are picked up.
PATHWAY_KEYWORDS = [
    "signaling", "signal transduction", "transport", "trafficking",
    "metabolism", "biosynthesis", "degradation", "ubiquitin",
    "autophagy", "apoptosis", "cell cycle", "proliferation",
    "differentiation", "immune", "inflammation", "transcription",
    "translation", "vesicle", "membrane", "golgi", "endosome",
    "lipid", "cholesterol", "fatty acid",
]

DISEASE_CONTEXTS = [
    "cancer", "tumor", "oncogene", "carcinoma", "neoplasm",
    "disease", "syndrome", "disorder",
]


def uniprot_lookup(gene: str, organism: str = "human") -> Optional[dict]:
    """Fetch UniProt entry for a gene symbol. Returns simplified dict or None."""
    org_query = "Homo sapiens" if organism == "human" else organism
    url = (
        f"{UNIPROT_BASE}/search?query=gene_exact:{gene}+AND+organism_name:{org_query}"
        f"+AND+reviewed:true&fields=gene_names,protein_name,go_p,cc_function&format=json&size=1"
    )
    try:
        resp = requests.get(url, timeout=10, headers={"Accept": "application/json"})
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        entry = results[0]
        # Primary gene name
        gene_names = entry.get("genes", [])
        primary_gene = gene
        if gene_names:
            primary_gene = gene_names[0].get("geneName", {}).get("value", gene)

        # GO biological process terms
        go_terms = []
        for xref in entry.get("uniProtKBCrossReferences", []):
            if xref.get("database") == "GO":
                props = {p["key"]: p["value"] for p in xref.get("properties", [])}
                if props.get("GoTerm", "").startswith("P:"):
                    go_terms.append(props["GoTerm"][2:])  # strip "P:"

        # Short function description from CC_FUNCTION
        function_text = ""
        for comment in entry.get("comments", []):
            if comment.get("commentType") == "FUNCTION":
                texts = comment.get("texts", [])
                if texts:
                    function_text = texts[0].get("value", "")[:200]
                    break

        return {
            "gene": primary_gene,
            "go_terms": go_terms,
            "function": function_text,
        }
    except Exception as exc:
        logger.warning(f"UniProt lookup failed for {gene}: {exc}")
        return None


def extract_pathway_signals(uniprot_entries: list[dict]) -> list[str]:
    """
    From a list of UniProt entries, find GO terms shared across ≥2 proteins
    (or present in ≥1 if the complex has only 2 proteins) and that match
    our pathway keyword list. Returns a deduplicated list of signal phrases.
    """
    if not uniprot_entries:
        return []

    min_proteins = max(1, len(uniprot_entries) - 1)
    term_counts: dict[str, int] = {}
    for entry in uniprot_entries:
        seen = set()
        for term in entry.get("go_terms", []):
            if term not in seen:
                term_counts[term] = term_counts.get(term, 0) + 1
                seen.add(term)

    signals = []
    for term, count in term_counts.items():
        if count >= min_proteins:
            term_lower = term.lower()
            if any(kw in term_lower for kw in PATHWAY_KEYWORDS):
                signals.append(term)

    return signals


def genes_ncbi_fragment(genes: list[str]) -> str:
    """Return an OR-combined Title/Abstract fragment for a list of gene symbols."""
    quoted = [f'"{g}"[Title/Abstract]' for g in genes]
    return " OR ".join(quoted)


def all_genes_fragment(genes: list[str]) -> str:
    """Return an AND-combined Title/Abstract fragment (intersection query)."""
    return " AND ".join(f"{g}[Title/Abstract]" for g in genes)


def generate_keywords_for_complex(
    proteins: list[str],
    complex_id: Optional[str],
    delay_s: float,
    skip_uniprot: bool,
) -> list[str]:
    """
    Generate a set of NCBI keyword strings for one protein complex.
    Returns a list of query strings.
    """
    genes = proteins  # use as-is; UniProt will resolve to canonical gene name

    # --- UniProt lookups ---
    canonical: list[str] = list(genes)  # fallback = input gene names
    pathway_signals: list[str] = []

    if not skip_uniprot:
        entries = []
        for gene in genes:
            info = uniprot_lookup(gene)
            if info:
                canonical_gene = info["gene"]
                # Use canonical if it looks like a real gene symbol (not a long name)
                if len(canonical_gene.split()) == 1:
                    idx = canonical.index(gene) if gene in canonical else -1
                    if idx >= 0:
                        canonical[idx] = canonical_gene
                entries.append(info)
            time.sleep(delay_s)
        pathway_signals = extract_pathway_signals(entries)

    keywords = []

    # --- Query 1: pairwise interaction (all pairs, up to 3 pairs for large complexes) ---
    pairs = []
    for i, a in enumerate(canonical):
        for b in canonical[i + 1:]:
            pairs.append((a, b))
    for a, b in pairs[:3]:
        keywords.append(
            f'{a}[Title/Abstract] AND {b}[Title/Abstract] AND protein interaction[Title/Abstract]'
        )

    # --- Query 2: full complex (all members together) ---
    if len(canonical) <= 4:
        keywords.append(all_genes_fragment(canonical) + " AND complex[Title/Abstract]")
    else:
        # For large complexes, anchor on the first 3 members
        keywords.append(
            all_genes_fragment(canonical[:3]) + " AND complex[Title/Abstract]"
        )

    # --- Query 3: pathway/function queries from shared GO terms ---
    for signal in pathway_signals[:3]:
        # Pick top 2-3 genes as anchors
        anchor_genes = canonical[:min(2, len(canonical))]
        anchor_frag = " AND ".join(f"{g}[Title/Abstract]" for g in anchor_genes)
        # Shorten the GO term to its most informative noun phrase (first 4 words)
        signal_short = " ".join(signal.split()[:4])
        keywords.append(f"{anchor_frag} AND {signal_short}[Title/Abstract]")

    # --- Query 4: disease/cancer association ---
    anchor_frag = " AND ".join(f"{g}[Title/Abstract]" for g in canonical[:2])
    keywords.append(f"{anchor_frag} AND cancer[Title/Abstract]")

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)

    label = complex_id or "+".join(canonical)
    logger.info(f"  {label}: {len(unique)} queries generated")
    return unique


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def parse_csv(path: Path) -> list[tuple[Optional[str], list[str]]]:
    """
    Parse CSV input. Accepts:
      - Header row with 'complex_id' and 'proteins' columns
      - Header row with 'complex_id' and then one protein per column
      - Headerless: each row is a complex, all columns are protein names
    Returns list of (complex_id, [protein, ...]) tuples.
    """
    complexes = []
    with open(path, newline="", encoding="utf-8") as f:
        sample = f.read(2048)
        f.seek(0)
        has_header = csv.Sniffer().has_header(sample)
        reader = csv.reader(f)

        if has_header:
            headers = [h.strip().lower() for h in next(reader)]
            id_col = next((i for i, h in enumerate(headers) if "id" in h or "complex" in h), None)
            protein_col = next((i for i, h in enumerate(headers) if "protein" in h), None)

            for row in reader:
                if not row or not any(row):
                    continue
                cid = row[id_col].strip() if id_col is not None and id_col < len(row) else None

                if protein_col is not None and protein_col < len(row):
                    # Proteins might be in one cell (comma-separated) or spread across columns
                    cell = row[protein_col].strip()
                    if "," in cell:
                        proteins = [p.strip() for p in cell.split(",") if p.strip()]
                    else:
                        # Proteins are in consecutive columns after the id column
                        start = (id_col + 1) if id_col is not None else 0
                        proteins = [row[i].strip() for i in range(start, len(row)) if row[i].strip()]
                else:
                    start = (id_col + 1) if id_col is not None else 0
                    proteins = [row[i].strip() for i in range(start, len(row)) if row[i].strip()]

                if proteins:
                    complexes.append((cid, proteins))
        else:
            for row in reader:
                proteins = [c.strip() for c in row if c.strip()]
                if proteins:
                    complexes.append((None, proteins))

    return complexes


def parse_json(path: Path) -> list[tuple[Optional[str], list[str]]]:
    """
    Parse JSON input. Accepts:
      - List of {"id": ..., "proteins": [...]} objects
      - List of lists: [["ARFRP1", "JTB", ...], ...]
      - Dict of {"complex_id": ["protein", ...]}
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    complexes = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                cid = item.get("id") or item.get("complex_id") or item.get("name")
                proteins = item.get("proteins") or item.get("members") or item.get("subunits") or []
                complexes.append((cid, [str(p) for p in proteins]))
            elif isinstance(item, list):
                complexes.append((None, [str(p) for p in item]))
    elif isinstance(data, dict):
        for cid, proteins in data.items():
            complexes.append((cid, [str(p) for p in proteins]))

    return complexes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate NCBI literature search keywords for predicted protein complexes"
    )
    parser.add_argument("--input", required=True, help="CSV or JSON file listing protein complexes")
    parser.add_argument(
        "--output", help="Write keyword list to this YAML file (instead of stdout)"
    )
    parser.add_argument(
        "--append-to-config",
        metavar="CONFIG_YAML",
        help="Append generated keywords directly to the 'keywords' list in config.yaml",
    )
    parser.add_argument(
        "--skip-uniprot",
        action="store_true",
        help="Skip UniProt lookups (faster but no GO-based pathway queries)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=UNIPROT_DELAY_S,
        help=f"Delay between UniProt API calls in seconds (default: {UNIPROT_DELAY_S})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print keywords to stdout only, do not write any files",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Process only the first N complexes (useful for testing)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    # Parse input
    suffix = input_path.suffix.lower()
    if suffix == ".json":
        complexes = parse_json(input_path)
    elif suffix in (".csv", ".tsv", ".txt"):
        complexes = parse_csv(input_path)
    else:
        # Try JSON first, then CSV
        try:
            complexes = parse_json(input_path)
        except json.JSONDecodeError:
            complexes = parse_csv(input_path)

    if args.limit:
        complexes = complexes[: args.limit]

    logger.info(f"Loaded {len(complexes)} complexes from {input_path}")
    if not args.skip_uniprot:
        logger.info("UniProt lookups enabled — this may take a while for large lists")
    else:
        logger.info("UniProt lookups skipped (--skip-uniprot)")

    all_keywords: list[str] = []
    for cid, proteins in complexes:
        kws = generate_keywords_for_complex(
            proteins=proteins,
            complex_id=cid,
            delay_s=args.delay,
            skip_uniprot=args.skip_uniprot,
        )
        all_keywords.extend(kws)

    # Deduplicate across all complexes
    seen: set[str] = set()
    unique_keywords = []
    for kw in all_keywords:
        if kw not in seen:
            seen.add(kw)
            unique_keywords.append(kw)

    logger.info(f"Total unique keywords generated: {len(unique_keywords)}")

    if args.dry_run:
        print(f"\n# Generated {len(unique_keywords)} keywords for {len(complexes)} complexes\n")
        for kw in unique_keywords:
            print(f'- "{kw}"')
        return

    # --- Output ---
    yaml_block = "\n".join(f'  - "{kw}"' for kw in unique_keywords)

    if args.append_to_config:
        config_path = Path(args.append_to_config)
        with open(config_path, encoding="utf-8") as f:
            config_text = f.read()

        # Find end of keywords list and append
        # We insert before the first blank line after the last keyword entry
        insertion = "\n  # --- Protein complex literature queries (auto-generated) ---\n" + yaml_block
        # Find the keywords: block and append at the end of it
        kw_section_end = config_text.rfind("\n\nsearch:")
        if kw_section_end == -1:
            kw_section_end = len(config_text)

        new_config = config_text[:kw_section_end] + insertion + config_text[kw_section_end:]
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_config)
        print(f"Appended {len(unique_keywords)} keywords to {config_path}")

    elif args.output:
        output_path = Path(args.output)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# Generated by generate_complex_keywords.py\n")
            f.write(f"# {len(complexes)} complexes → {len(unique_keywords)} unique queries\n\n")
            f.write("keywords:\n")
            f.write(yaml_block + "\n")
        print(f"Wrote {len(unique_keywords)} keywords to {output_path}")

    else:
        # Default: print to stdout in YAML list format, ready to paste into config.yaml
        print(f"\n# Generated {len(unique_keywords)} keywords for {len(complexes)} complexes")
        print("# Add these to the 'keywords:' section of config.yaml\n")
        for kw in unique_keywords:
            print(f'  - "{kw}"')


if __name__ == "__main__":
    main()
