#!/usr/bin/env python3
"""Run a fixed paper set through claude / gemini / local providers side-by-side.

Writes to data/fingerprints/_provider_compare/<provider>/ — does NOT touch the
production fingerprint dir or update the database. Resumable: skips outputs that
already exist on disk.

Usage:
    python scripts/compare_providers.py            # run all
    python scripts/compare_providers.py --providers claude gemini
    python scripts/compare_providers.py --force    # overwrite existing outputs
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from src.database import Database, _paper_key  # noqa: E402
from src.text_extractor import extract_text  # noqa: E402
from src.curator import curate_paper  # noqa: E402

OUT_ROOT = ROOT / "data" / "fingerprints" / "_provider_compare"

# Fixed paper set (paper_key form, with leading "doi:")
PPI_HEAVY = [
    "doi:10.1038/s41467-026-68319-1",   # SHOC2-KRAS-PP1C
    "doi:10.1016/j.cell.2012.02.013",   # Bromodomain catalogue
    "doi:10.1126/science.abi6226",      # SARS-CoV-2 spike
    "doi:10.7554/eLife.36307",          # PTP1B allostery
    "doi:10.1038/s41586-023-06788-w",   # Tau filaments
]

RANDOM_UNCURATED = [
    "doi:10.1371/journal.pcbi.1002225",
    "doi:10.1016/j.molcel.2010.03.016",
    "doi:10.1016/j.cell.2019.08.037",
    "doi:10.1038/s41467-021-22129-9",
    "doi:10.1038/s41467-019-09483-5",
]

ALL_KEYS = PPI_HEAVY + RANDOM_UNCURATED


def safe_filename(paper_key: str) -> str:
    return paper_key.replace(":", "_").replace("/", "_")


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_pdf(paper, config) -> Path | None:
    if not paper.pdf_path:
        return None
    raw = paper.pdf_path.replace("\\", "/")
    for cand in (Path(raw), ROOT / raw, ROOT / config["paths"]["pdf_dir"] / Path(raw).name):
        if cand.exists():
            return cand
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", nargs="+", default=["claude", "gemini", "local"],
                    choices=["claude", "gemini", "local"])
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing per-provider outputs")
    ap.add_argument("--papers", nargs="+", default=None,
                    help="Override paper_keys (default: PPI_HEAVY + RANDOM_UNCURATED)")
    args = ap.parse_args()

    config = load_config()
    db_path = ROOT / config["paths"]["db_path"]
    db = Database(db_path)
    all_papers = {_paper_key(p.doi, p.pmcid, p.pmid, p.title): p for p in db.get_papers()}

    keys = args.papers or ALL_KEYS

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    for prov in args.providers:
        (OUT_ROOT / prov).mkdir(exist_ok=True)

    # Extract text once per paper, cache in memory
    paper_texts: dict[str, tuple[str, str, int]] = {}
    for key in keys:
        if key not in all_papers:
            logger.error(f"{key}: not in database, skipping")
            continue
        paper = all_papers[key]
        pdf = resolve_pdf(paper, config)
        if pdf is None:
            logger.error(f"{key}: PDF not on disk, skipping")
            continue
        text, fmt = extract_text(pdf, max_chars=config["curation"].get("max_input_chars", 150000))
        paper_texts[key] = (text, fmt, len(text))
        logger.info(f"{key}: extracted {len(text):,} chars from {fmt.upper()}")

    summary = {"runs": [], "extraction": {k: v[2] for k, v in paper_texts.items()}}

    for provider in args.providers:
        logger.info(f"=== provider: {provider} ===")
        # Per-provider config override
        cfg = copy.deepcopy(config)
        cfg["curation"]["provider"] = provider

        for key, (text, fmt, _n) in paper_texts.items():
            out_path = OUT_ROOT / provider / f"{safe_filename(key)}.json"
            if out_path.exists() and not args.force:
                logger.info(f"  [{provider}] {key}: cached, skip")
                continue

            t0 = time.monotonic()
            try:
                fp = curate_paper(
                    paper_key=key,
                    text=text,
                    source_format=fmt,
                    config=cfg,
                    prompt_path=ROOT / cfg["curation"].get("prompt_path", "curation_prompt.md"),
                )
                wall = time.monotonic() - t0
                cm = fp.get("curation_metadata", {}) or {}
                in_tok = cm.get("input_tokens", 0) or 0
                out_tok = cm.get("output_tokens", 0) or 0
                # Persist
                out_path.write_text(json.dumps(fp, indent=2), encoding="utf-8")
                summary["runs"].append({
                    "provider": provider, "paper_key": key, "ok": True,
                    "wall_s": round(wall, 2),
                    "input_tokens": in_tok, "output_tokens": out_tok,
                    "relevant": fp.get("relevant"),
                    "n_findings": len(fp.get("key_findings") or []),
                })
                logger.info(
                    f"  [{provider}] {key}: ok {wall:.1f}s "
                    f"in={in_tok} out={out_tok} "
                    f"findings={len(fp.get('key_findings') or [])}"
                )
            except Exception as exc:
                wall = time.monotonic() - t0
                logger.error(f"  [{provider}] {key}: FAIL after {wall:.1f}s — {exc}")
                summary["runs"].append({
                    "provider": provider, "paper_key": key, "ok": False,
                    "wall_s": round(wall, 2), "error": str(exc)[:300],
                })

    # Persist summary
    (OUT_ROOT / "_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_ROOT / '_summary.json'}")
    print("\n--- Run summary ---")
    for r in summary["runs"]:
        flag = "✓" if r.get("ok") else "✗"
        print(f"  {flag} {r['provider']:>7s} {r['paper_key']:55s} {r['wall_s']:>6.1f}s  "
              f"in={r.get('input_tokens', 0):>6d} out={r.get('output_tokens', 0):>5d}")


if __name__ == "__main__":
    main()
