#!/usr/bin/env python3
"""
Fetch PDB structure metadata from RCSB GraphQL API and cache to data/pdb_metadata.json.

Collects all PDB IDs referenced in fingerprints (from paper_metadata.pdb_accessions
and pathway_context.target_nodes.suggested_pdb_structures), fetches title / method /
resolution / protein-chain descriptions from RCSB, and writes a compact cache file.

The cache is consumed by the find_pdb_structures tool (skill_runner.py) to enrich
PDB lookup results so the LLM can select the best structure for a given PPI.

Usage:
    python scripts/fetch_pdb_metadata.py           # fetch only new IDs
    python scripts/fetch_pdb_metadata.py --force   # re-fetch everything
    python scripts/fetch_pdb_metadata.py --id 5GN0 # single ID (for testing)
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
FINGERPRINT_DIR = ROOT / "data" / "fingerprints"
METADATA_PATH = ROOT / "data" / "pdb_metadata.json"
GRAPHQL_URL = "https://data.rcsb.org/graphql"
BATCH_SIZE = 50
REQUEST_DELAY_S = 0.5

# Fields we need for structure selection. Keep it tight to stay within response limits.
_QUERY = """
query EntriesMetadata($ids: [String!]!) {
  entries(entry_ids: $ids) {
    rcsb_id
    struct {
      title
    }
    rcsb_entry_info {
      resolution_combined
      experimental_method
      polymer_entity_count_protein
    }
    polymer_entities {
      rcsb_polymer_entity {
        pdbx_description
      }
      rcsb_entity_source_organism {
        ncbi_taxonomy_id
        ncbi_scientific_name
      }
    }
  }
}
"""


def collect_all_pdb_ids(fingerprint_dir: Path) -> set[str]:
    """Collect every 4-char PDB ID referenced in fingerprints from both sources."""
    ids: set[str] = set()
    for fp_path in fingerprint_dir.glob("*.json"):
        try:
            fp = json.loads(fp_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for pid in fp.get("paper_metadata", {}).get("pdb_accessions") or []:
            if pid and len(pid) == 4:
                ids.add(pid.upper())
        for node in (fp.get("pathway_context") or {}).get("target_nodes") or []:
            for pid in node.get("suggested_pdb_structures") or []:
                if pid and len(pid) == 4:
                    ids.add(pid.upper())
    return ids


def fetch_batch(ids: list[str]) -> list[dict]:
    """Fetch metadata for a batch of PDB IDs via RCSB GraphQL. Returns entry dicts."""
    try:
        resp = requests.post(
            GRAPHQL_URL,
            json={"query": _QUERY, "variables": {"ids": ids}},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            logger.warning(f"GraphQL errors: {body['errors']}")
        return (body.get("data") or {}).get("entries") or []
    except requests.RequestException as e:
        logger.warning(f"GraphQL request failed for batch {ids[:3]}: {e}")
        return []


def parse_entry(entry: dict) -> dict:
    """Distil an RCSB GraphQL entry down to selection-relevant fields."""
    info = entry.get("rcsb_entry_info") or {}
    res_list = [r for r in (info.get("resolution_combined") or []) if r is not None]
    resolution = round(min(res_list), 2) if res_list else None

    method = info.get("experimental_method")  # singular field in RCSB GraphQL schema

    entities = []
    for pe in entry.get("polymer_entities") or []:
        desc = (pe.get("rcsb_polymer_entity") or {}).get("pdbx_description") or ""
        orgs = pe.get("rcsb_entity_source_organism") or []
        taxid = orgs[0].get("ncbi_taxonomy_id") if orgs else None
        species = orgs[0].get("ncbi_scientific_name") if orgs else None
        entities.append({
            "description": desc,
            "organism_taxid": taxid,
            "organism_name": species,
        })

    return {
        "title": (entry.get("struct") or {}).get("title"),
        "method": method,
        "resolution_A": resolution,
        "protein_chain_count": info.get("polymer_entity_count_protein") or 0,
        "entities": entities,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch PDB entry metadata from RCSB GraphQL and cache locally."
    )
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch IDs already present in the cache")
    parser.add_argument("--id", dest="pdb_id", default=None,
                        help="Fetch a single PDB ID only (e.g. 5GN0)")
    args = parser.parse_args()

    # Load existing cache
    cache: dict[str, dict] = {}
    if METADATA_PATH.exists():
        cache = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
        logger.info(f"Loaded {len(cache)} cached entries from {METADATA_PATH.name}")

    # Determine which IDs to fetch
    if args.pdb_id:
        to_fetch = {args.pdb_id.upper()}
    else:
        all_ids = collect_all_pdb_ids(FINGERPRINT_DIR)
        to_fetch = all_ids if args.force else (all_ids - set(cache.keys()))
        logger.info(f"{len(all_ids)} unique PDB IDs in corpus; {len(to_fetch)} need fetching")

    if not to_fetch:
        logger.info("Cache is up to date — nothing to fetch.")
        return

    id_list = sorted(to_fetch)
    fetched = 0
    failed = 0

    for i in range(0, len(id_list), BATCH_SIZE):
        batch = id_list[i : i + BATCH_SIZE]
        preview = batch[:5]
        logger.info(f"Batch {i // BATCH_SIZE + 1}: {preview}{'...' if len(batch) > 5 else ''}")

        entries = fetch_batch(batch)
        returned_ids = {e["rcsb_id"] for e in entries}
        missing = set(batch) - returned_ids
        if missing:
            logger.warning(f"  Not found in RCSB: {sorted(missing)}")
            failed += len(missing)

        for entry in entries:
            pdb_id = entry["rcsb_id"]
            parsed = parse_entry(entry)
            cache[pdb_id] = parsed
            fetched += 1
            logger.debug(
                f"  {pdb_id}: {parsed['method']} "
                f"@ {parsed['resolution_A']}Å — "
                f"{parsed['protein_chain_count']} chain(s)"
            )

        if i + BATCH_SIZE < len(id_list):
            time.sleep(REQUEST_DELAY_S)

    METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    METADATA_PATH.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(f"Saved {len(cache)} entries to {METADATA_PATH}")
    print(f"\nDone.  fetched={fetched}  not_in_rcsb={failed}  total_cached={len(cache)}")


if __name__ == "__main__":
    main()
