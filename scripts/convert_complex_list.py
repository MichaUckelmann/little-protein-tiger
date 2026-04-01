#!/usr/bin/env python3
"""
Convert the project's complex metadata JSON into:
  1. A clean complex list JSON for generate_complex_keywords.py (--output-list)
  2. NCBI keyword queries generated directly from the embedded GO terms and
     disease associations — no UniProt API calls needed (--output-keywords)
  3. Optionally append keywords directly to config.yaml (--append-to-config)

Usage:
    python scripts/convert_complex_list.py \
        --input info/meta_for_website_v20_041125_with_hash_corrected.json \
        --output-list  info/complex_list.json \
        --output-keywords info/complex_keywords.yaml

    # Or append directly to config:
    python scripts/convert_complex_list.py \
        --input info/meta_for_website_v20_041125_with_hash_corrected.json \
        --append-to-config config.yaml
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# GO term → NCBI search phrase extraction
# ---------------------------------------------------------------------------

# Strip GO IDs like "[GO:0007165]" from the end of GO term strings
_GO_ID_RE = re.compile(r"\s*\[GO:\d+\]$")

# GO term words that add no search value
_GO_STOPWORDS = {
    "process", "regulation", "positive", "negative", "of", "by", "via",
    "in", "to", "the", "and", "or", "a", "an", "with", "from", "for",
    "involved", "cellular", "biological", "metabolic",
}

# Minimum word length to keep from a GO term
_MIN_WORD_LEN = 3


def _clean_go_term(term: str) -> str:
    """Strip GO ID suffix and return clean lowercase phrase."""
    return _GO_ID_RE.sub("", term).strip().lower()


def _go_term_to_ncbi_phrase(term: str) -> str | None:
    """
    Convert a GO biological process term to a short NCBI search phrase.
    Returns None if the term is too generic to be useful.
    """
    cleaned = _clean_go_term(term)
    # Drop very generic terms
    generic = {
        "biological process", "cellular process", "metabolic process",
        "regulation of biological process", "signal transduction",
        "intracellular signal transduction",
    }
    if cleaned in generic:
        return None
    # Keep terms that are specific enough (> 2 meaningful words)
    words = [w for w in cleaned.split() if w not in _GO_STOPWORDS and len(w) >= _MIN_WORD_LEN]
    if len(words) < 2:
        return None
    # Use at most 4 words to keep queries tight
    return " ".join(words[:4])


# ---------------------------------------------------------------------------
# Keyword generation for a single complex
# ---------------------------------------------------------------------------

def generate_keywords(complex_id: str, genes: list[str], go_terms: list[str],
                      diseases: list[str]) -> list[str]:
    """
    Generate NCBI Title/Abstract keyword strings for one complex.
    Uses gene names, GO terms, and disease associations from the metadata.
    """
    keywords: list[str] = []
    seen: set[str] = set()

    def add(kw: str):
        if kw not in seen:
            seen.add(kw)
            keywords.append(kw)

    # --- Pairwise interaction queries (all pairs, cap at 3 for large complexes) ---
    pairs = [(genes[i], genes[j]) for i in range(len(genes)) for j in range(i + 1, len(genes))]
    for a, b in pairs[:3]:
        add(f'{a}[Title/Abstract] AND {b}[Title/Abstract] AND interaction[Title/Abstract]')

    # --- Full complex query (all members AND complex) ---
    if 2 <= len(genes) <= 4:
        frag = " AND ".join(f"{g}[Title/Abstract]" for g in genes)
        add(f"{frag} AND complex[Title/Abstract]")
    elif len(genes) > 4:
        # Anchor on first 3 to avoid overly restrictive queries
        frag = " AND ".join(f"{g}[Title/Abstract]" for g in genes[:3])
        add(f"{frag} AND complex[Title/Abstract]")

    # --- GO term–derived pathway queries ---
    anchor = genes[0]  # most informative anchor gene
    anchor2 = f"{genes[0]}[Title/Abstract] AND {genes[1]}[Title/Abstract]" if len(genes) >= 2 else f"{genes[0]}[Title/Abstract]"
    go_phrases_added = 0
    for term in go_terms:
        phrase = _go_term_to_ncbi_phrase(term)
        if phrase and go_phrases_added < 3:
            add(f'{anchor2} AND "{phrase}"[Title/Abstract]')
            go_phrases_added += 1

    # --- Disease association queries ---
    # Pick the most specific disease (avoid generic "cancer")
    specific_diseases = [d for d in diseases if d.lower() not in ("cancer", "disease")]
    disease_for_query = specific_diseases[0] if specific_diseases else (diseases[0] if diseases else None)
    if disease_for_query:
        add(f'{anchor}[Title/Abstract] AND "{disease_for_query}"[Title/Abstract]')
    # Always add a generic cancer anchor
    add(f'{anchor2} AND cancer[Title/Abstract]')

    # --- Binding / structure query for the top pair ---
    if len(genes) >= 2:
        add(f'{genes[0]}[Title/Abstract] AND {genes[1]}[Title/Abstract] AND structure[Title/Abstract]')

    return keywords


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert complex metadata JSON to keyword queries for literature search"
    )
    parser.add_argument(
        "--input", required=True,
        help="Path to complex metadata JSON (meta_for_website_v20_*.json)"
    )
    parser.add_argument(
        "--output-list",
        help="Write clean complex list to this JSON file (for generate_complex_keywords.py)"
    )
    parser.add_argument(
        "--output-keywords",
        help="Write generated NCBI keywords to this YAML file"
    )
    parser.add_argument(
        "--append-to-config",
        metavar="CONFIG_YAML",
        help="Append generated keywords to the 'keywords:' section of config.yaml"
    )
    parser.add_argument(
        "--limit", type=int,
        help="Process only the first N complexes (for testing)"
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    if args.limit:
        data = data[: args.limit]

    print(f"Loaded {len(data)} complexes from {input_path.name}", file=sys.stderr)

    # --- Build clean list and generate keywords ---
    clean_list = []
    all_keywords: list[str] = []
    keyword_seen: set[str] = set()

    for entry in data:
        complex_id = entry.get("complexID", "")
        genes = entry.get("GeneNames", [])
        uniprot_ids = entry.get("UniProtIDs", [])

        # GO terms: strip the GO ID suffix
        raw_go = entry.get("cluster_top5GO", [])
        go_terms = [_clean_go_term(t) for t in raw_go]

        diseases = entry.get("Cluster_OT_top10_Disease", [])

        if not genes:
            continue

        # Clean list entry
        clean_list.append({
            "id": complex_id,
            "proteins": genes,
            "uniprot_ids": uniprot_ids,
            "go_terms": go_terms,
            "diseases": diseases,
        })

        # Keywords
        kws = generate_keywords(complex_id, genes, go_terms, diseases)
        for kw in kws:
            if kw not in keyword_seen:
                keyword_seen.add(kw)
                all_keywords.append(kw)

    print(f"Generated {len(all_keywords)} unique keyword queries", file=sys.stderr)

    # --- Write clean list ---
    if args.output_list:
        out = Path(args.output_list)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(clean_list, f, indent=2, ensure_ascii=False)
        print(f"Wrote clean complex list ({len(clean_list)} entries) to {out}")

    # --- Serialise keywords as valid YAML (handles inner quotes automatically) ---
    def keywords_yaml_block(kws: list[str]) -> str:
        """Return a YAML list block with proper escaping via the yaml library."""
        return yaml.dump(kws, allow_unicode=True, default_flow_style=False,
                         width=200).rstrip()

    if args.output_keywords:
        out = Path(args.output_keywords)
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"# Auto-generated from {input_path.name}\n")
            f.write(f"# {len(data)} complexes -> {len(all_keywords)} unique queries\n\n")
            f.write("keywords:\n")
            # yaml.dump of a list produces "- item\n" lines; indent by 2
            block = yaml.dump(all_keywords, allow_unicode=True,
                              default_flow_style=False, width=200)
            for line in block.splitlines():
                f.write(f"  {line}\n")
        print(f"Wrote {len(all_keywords)} keywords to {out}")

    if args.append_to_config:
        config_path = Path(args.append_to_config)
        with open(config_path, encoding="utf-8") as f:
            config_text = f.read()

        # Build indented block
        kw_block_lines = []
        for line in yaml.dump(all_keywords, allow_unicode=True,
                              default_flow_style=False, width=200).splitlines():
            kw_block_lines.append(f"  {line}")
        insertion = (
            "\n\n  # --- Protein complex literature queries (auto-generated) ---\n"
            + "\n".join(kw_block_lines)
        )
        kw_section_end = config_text.rfind("\n\nsearch:")
        if kw_section_end == -1:
            kw_section_end = len(config_text)

        new_config = config_text[:kw_section_end] + insertion + config_text[kw_section_end:]
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_config)
        print(f"Appended {len(all_keywords)} keywords to {config_path}")

    # Default: print to stdout if no output specified
    if not args.output_list and not args.output_keywords and not args.append_to_config:
        print(f"\n# {len(data)} complexes → {len(all_keywords)} unique keyword queries")
        print("# Add these to the 'keywords:' section of config.yaml\n")
        for kw in all_keywords:
            print(f'  - "{kw}"')


if __name__ == "__main__":
    main()
