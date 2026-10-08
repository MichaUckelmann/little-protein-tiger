"""Read/write JSON fingerprint files; simple in-Python querying."""
import html
import json
import re
from pathlib import Path
from typing import Any


def clean_title(title: str | None) -> str:
    """Plain-text form of a database title: no JATS/HTML markup or entities.

    Titles reach the database from PMC and Semantic Scholar with inline markup
    (`<i>ndk</i>`, `CD8<sup>+</sup>T`). Tags are removed without inserting a
    space — the source already spaces where it means to — whitespace is
    collapsed, and one trailing full stop is dropped.
    """
    text = html.unescape(re.sub(r"<[^>]+>", "", title or ""))
    text = " ".join(text.split())
    # PubMed-sourced titles end in a full stop; Crossref and the journals' own
    # titles do not. One convention, so the same paper reads the same whichever
    # source supplied it.
    return text[:-1].rstrip() if text.endswith(".") and not text.endswith("..") else text


def save_fingerprint(paper_key: str, fingerprint: dict, output_dir: Path) -> Path:
    """Write fingerprint dict to data/fingerprints/<safe_key>.json. Returns path."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Sanitise paper_key for use as a filename
    safe_key = paper_key.replace("/", "_").replace(":", "_").replace(" ", "_")
    dest = output_dir / f"{safe_key}.json"
    dest.write_text(json.dumps(fingerprint, indent=2, ensure_ascii=False), encoding="utf-8")
    return dest


def load_fingerprint(paper_key: str, output_dir: Path) -> dict | None:
    """Load a fingerprint by paper_key. Returns None if not found."""
    output_dir = Path(output_dir)
    safe_key = paper_key.replace("/", "_").replace(":", "_").replace(" ", "_")
    dest = output_dir / f"{safe_key}.json"
    if not dest.exists():
        return None
    return json.loads(dest.read_text(encoding="utf-8"))


def query_fingerprints(output_dir: Path, filters: dict[str, Any]) -> list[dict]:
    """
    Load all fingerprints and filter by simple key=value equality.

    Example filters:
        {"relevant": True}
        {"paper_metadata.study_type": "experimental_in_vitro"}

    Dotted keys navigate nested dicts one level deep.
    """
    output_dir = Path(output_dir)
    if not output_dir.exists():
        return []

    results = []
    for fp_file in sorted(output_dir.glob("*.json")):
        try:
            data = json.loads(fp_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        if _matches(data, filters):
            results.append(data)

    return results


def _matches(data: dict, filters: dict[str, Any]) -> bool:
    for key, expected in filters.items():
        if "." in key:
            outer, inner = key.split(".", 1)
            value = (data.get(outer) or {}).get(inner)
        else:
            value = data.get(key)
        if value != expected:
            return False
    return True
