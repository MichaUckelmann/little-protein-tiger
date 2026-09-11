#!/usr/bin/env python3
"""Download the pre-built corpus. Free, no API key, no curation.

The maintainer's curated corpus ships as a release asset, so a new user does
NOT have to rebuild it: no LLM spend, no days of downloading, no PubMed rate
limits. You get the fingerprints, the vector index and the paper database, and
`search_corpus` works immediately.

    python scripts/fetch_corpus.py            # download and install
    python scripts/fetch_corpus.py --check    # what's here now, what's available

Source documents (PDFs/XMLs) are NOT included — they are ~95% of the corpus on
disk and nothing downstream reads them. The database still indexes every paper
the maintainer's searches found, so you can widen the search terms and fetch
documents for whatever you want to add. That, and only that, costs money.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.env_config import load_env  # noqa: E402

load_env(_ROOT / ".env")

REPO = "MichaUckelmann/little-protein-tiger"
ASSET_PREFIX = "lpt-corpus"
_API = f"https://api.github.com/repos/{REPO}/releases"

# Paths the archive installs into; used to report what is already present.
INSTALLS = ("data/fingerprints", "data/vectors", "data/literature.db",
            "data/depmap_edges.parquet", "data/clusters.json",
            "data/pdb_metadata.json")


def _human(n: int) -> str:
    # DECIMAL MB, not MiB. This divided by 2**20 and wrote "MB", so a
    # 104,535,812-byte asset printed as "100 MB" while every document said
    # ~105 MB and GitHub's own release page said 104 MB — three figures for one
    # file, and `SETUP_AGENT.md` tells the reader to trust this tool over its
    # own prose. Decimal is what GitHub, the docs and `ls -l` agree on.
    return f"{n/1e9:.2f} GB" if n >= 1e9 else f"{n/1e6:.0f} MB"


def find_asset() -> tuple[str, str, int] | None:
    """(name, url, size) of the newest corpus asset, or None."""
    try:
        resp = requests.get(_API, timeout=30,
                            headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  could not reach the GitHub API: {exc}")
        return None
    for release in resp.json():
        for asset in release.get("assets") or []:
            if asset.get("name", "").startswith(ASSET_PREFIX):
                return (asset["name"], asset["browser_download_url"],
                        asset.get("size", 0))
    return None


def local_state() -> list[tuple[str, int]]:
    out = []
    for rel in INSTALLS:
        p = _ROOT / rel
        if p.is_dir():
            out.append((rel, sum(f.stat().st_size
                                 for f in p.rglob("*") if f.is_file())))
        elif p.is_file():
            out.append((rel, p.stat().st_size))
        else:
            out.append((rel, 0))
    return out


def check() -> int:
    print("\nLocal corpus artifacts:\n")
    total = 0
    for rel, size in local_state():
        total += size
        print(f"  [{'ok  ' if size else '--  '}] {rel:30s} "
              f"{(_human(size) if size else 'absent'):>9s}")
    print(f"\n  {_human(total)} present locally")

    fps = _ROOT / "data" / "fingerprints"
    n = len(list(fps.glob("*.json"))) if fps.is_dir() else 0
    if n:
        print(f"  {n:,} fingerprints — a corpus is already installed")

    print("\nAvailable to download:")
    asset = find_asset()
    if asset:
        name, _, size = asset
        print(f"  {name}  ({_human(size)})")
    else:
        print(f"  no {ASSET_PREFIX}* asset found in {REPO}'s releases.")
        print("  Either none has been published yet, or the network is blocked.")
        print("  You can still build your own corpus — see docs/journal-filtering.md")
        print("  and README.md, but note it costs real API spend.")
        return 1
    return 0


def download(url: str, dest: Path, expect: int = 0) -> Path:
    print(f"  GET {url}")
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or expect or 0)
        done = 0
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r         {100*done/total:5.1f}%  {_human(done)}",
                          end="")
        print("\r" + " " * 44, end="\r")
    return dest


def install(archive: Path, force: bool) -> int:
    existing = [rel for rel, size in local_state() if size]
    if existing and not force:
        print("\n  A corpus is already installed:")
        for rel in existing:
            print(f"    {rel}")
        print("\n  Refusing to overwrite. Re-run with --force to replace it, or")
        print("  move data/ aside first. (Your own curated fingerprints would")
        print("  be replaced by the shipped set — that is rarely what you want.)")
        return 1

    with tempfile.TemporaryDirectory() as td:
        tar_path = Path(td) / "corpus.tar"
        print("  decompressing ...")
        if archive.suffix == ".zst":
            if not shutil.which("zstd"):
                print("  zstd is required to unpack this archive "
                      "(apt install zstd / brew install zstd).")
                return 1
            subprocess.run(["zstd", "-d", "-q", "-f", str(archive),
                            "-o", str(tar_path)], check=True)
        else:
            subprocess.run(["gunzip", "-c", str(archive)],
                           stdout=tar_path.open("wb"), check=True)

        print("  extracting ...")
        with tarfile.open(tar_path) as tf:
            # Refuse any member that would escape the repo root.
            for member in tf.getmembers():
                target = (_ROOT / member.name).resolve()
                if not str(target).startswith(str(_ROOT.resolve())):
                    print(f"  refusing unsafe archive member: {member.name}")
                    return 1
            # filter="data" explicitly: Python 3.14 turns this on by default
            # and warns loudly until then, and `pyproject.toml` already allows
            # 3.14. It rejects absolute paths, links escaping the tree and
            # special files — which is what the loop above checks for by hand,
            # so making it explicit costs nothing and silences a warning a new
            # user would otherwise hit on their very first command.
            tf.extractall(_ROOT, filter="data")

            manifest = _ROOT / "MANIFEST.json"
            if manifest.is_file():
                data = json.loads(manifest.read_text(encoding="utf-8"))
                manifest.unlink()
                # `papers_curated_shipped` FIRST, and not merely as a
                # nicety: the published v0.1.0 manifest carries a
                # `papers_curated` copied from the maintainer's own corpus
                # (14,517) while the archive holds the licence-permitted
                # subset (7,072). Reading the shipped field fixes the
                # report for assets that are already out there, which
                # re-packaging cannot.
                curated = (data.get("papers_curated_shipped")
                           or data.get("papers_curated") or 0)
                print(f"\n  {curated:,} curated papers, "
                      f"{data.get('papers_indexed', 0):,} indexed "
                      f"(built {data.get('created', 'unknown')})")
                if (data.get("licence_filter") or {}).get("applied"):
                    withheld = data.get("papers_curated_local")
                    print("  Fingerprints ship only for papers whose licence "
                          "permits derivative works"
                          + (f" — {withheld:,} are curated locally, the rest "
                             f"are withheld, not missing" if withheld else "")
                          + ". See docs/licensing.md.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="Report local and available corpora; download nothing.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite an existing local corpus.")
    ap.add_argument("--url", help="Download from this URL, or install from "
                                 "this local archive path, instead of a release.")
    args = ap.parse_args()

    if args.check:
        return check()

    if args.url:
        # A local path is a legitimate source: testing a freshly-built archive,
        # or installing on a machine with no route to GitHub.
        local = Path(args.url.removeprefix("file://"))
        if local.is_file():
            print(f"\nInstalling from {local} ({_human(local.stat().st_size)})")
            rc = install(local, args.force)
            if rc:
                return rc
            print("\n  Corpus installed. Try:")
            print('    python scripts/ask_corpus.py "what is known about KRAS-RAF1?"')
            print("    python scripts/doctor.py --track literature")
            return 0
        name, url, size = Path(args.url).name, args.url, 0
    else:
        asset = find_asset()
        if not asset:
            print(f"\nNo {ASSET_PREFIX}* asset found in {REPO}'s releases.\n"
                  "Nothing to download. Build your own corpus instead — see\n"
                  "README.md and docs/journal-filtering.md (it costs API spend).")
            return 1
        name, url, size = asset

    print(f"\nFetching {name} ({_human(size) if size else 'size unknown'})")
    with tempfile.TemporaryDirectory() as td:
        archive = download(url, Path(td) / name, size)
        print(f"  sha256 {hashlib.sha256(archive.read_bytes()).hexdigest()}")
        rc = install(archive, args.force)
    if rc:
        return rc

    print("\n  Corpus installed. Try:")
    print('    python scripts/ask_corpus.py "what is known about KRAS-RAF1?"')
    print("    python scripts/doctor.py --track literature")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
