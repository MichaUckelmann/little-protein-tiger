#!/usr/bin/env python3
"""Search fingerprint JSON files for protein keyword matches."""

import json
import sys
from pathlib import Path

FINGERPRINTS_DIR = Path(__file__).parent.parent / "data" / "fingerprints"


def search_fingerprints(keywords: list[str]) -> None:
    keywords_lower = [k.lower() for k in keywords]
    matches = []

    for fp in sorted(FINGERPRINTS_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [skip] {fp.name}: {e}", file=sys.stderr)
            continue

        # Collect all text fields where proteins appear
        protein_mentions: set[str] = set()

        # entities.proteins
        for p in data.get("entities", {}).get("proteins", []):
            if isinstance(p, str):
                protein_mentions.add(p)

        # key_findings[].protein_pair
        for finding in data.get("key_findings", []):
            for p in finding.get("protein_pair") or []:
                if isinstance(p, str):
                    protein_mentions.add(p)

        # title and context hook (broad text search)
        title = data.get("paper_metadata", {}).get("title", "")
        hook  = data.get("paper_metadata", {}).get("situational_context_hook", "")
        full_text = " ".join([title, hook] + list(protein_mentions)).lower()

        hit_keywords = [k for k in keywords_lower if k in full_text]
        if not hit_keywords:
            continue

        # Collect matched protein names for display
        matched_proteins = [p for p in protein_mentions if any(k in p.lower() for k in keywords_lower)]

        matches.append({
            "file": fp.name,
            "doi":   data.get("paper_metadata", {}).get("doi", "N/A"),
            "title": title,
            "hit_keywords": hit_keywords,
            "matched_proteins": matched_proteins,
        })

    if not matches:
        print(f"No papers found matching: {keywords}")
        return

    print(f"Found {len(matches)} paper(s) matching {keywords}:\n")
    for m in matches:
        print(f"  Title   : {m['title']}")
        print(f"  DOI     : {m['doi']}")
        print(f"  Keywords: {', '.join(m['hit_keywords'])}")
        if m["matched_proteins"]:
            print(f"  Proteins: {', '.join(m['matched_proteins'])}")
        print()


_USAGE = """usage: search_fingerprints.py KEYWORD [KEYWORD ...]

Plain substring scan over data/fingerprints/*.json (protein names, title,
context hook). This is NOT semantic search, and not word-aware: it matches
literal substrings, so "FACT" also matches the word "fact" (2,432 hits, most
of them prose) and "SRC" matches "resource". Prefer distinctive symbols.

For a question rather than a keyword, use the semantic path instead:
    python scripts/ask_corpus.py "how does FACT reposition H2A-H2B?"

examples:
    python scripts/search_fingerprints.py SPT16 SSRP1
    python scripts/search_fingerprints.py YAP1 TEAD1
"""

if __name__ == "__main__":
    # No argparse here on purpose — every argv token is a search keyword. That
    # means `--help` would otherwise be searched for and reported as "no papers
    # found", and a bare invocation used to silently search a leftover pair of
    # debug terms instead of saying what it wants.
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(_USAGE, end="")
        sys.exit(0 if args else 2)
    search_fingerprints(args)
