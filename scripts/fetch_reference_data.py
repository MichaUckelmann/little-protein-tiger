#!/usr/bin/env python3
"""Download the reference datasets LPT needs but does not ship.

`data/` is gitignored, so a fresh clone has none of it. Two of these files are
HARD requirements for the binder track's very first stage: without them
`--workflow binder --target KRAS` stops about two seconds in. It now stops
with `identifier_normalizer.ReferenceDataMissing`, which names both files and
this script; before that guard existed it was a bare FileNotFoundError several
layers below anything that said what the file was or where to get it.

    HUMAN_9606_idmapping.dat.gz   UniProt   ~35 MB   REQUIRED
    hgnc_complete_set.tsv         HGNC      ~17 MB   REQUIRED
    CRISPRGeneEffect.csv          DepMap   ~420 MB   optional (--with-depmap)

All three are public and free. Usage:

    python scripts/fetch_reference_data.py              # the two required files
    python scripts/fetch_reference_data.py --with-depmap
    python scripts/fetch_reference_data.py --check      # report, download nothing

Each download records provenance (URL, timestamp, size, sha256) in
`data/depmap/PROVENANCE.json`. These are versioned releases that change under
the same URL, so "which UniProt release is this corpus normalised against?" is
a question that has to be answerable months later.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.env_config import load_env  # noqa: E402

load_env(_ROOT / ".env")

DEST_DIR = _ROOT / "data" / "depmap"
PROVENANCE = DEST_DIR / "PROVENANCE.json"


@dataclass(frozen=True)
class Dataset:
    name: str
    url: str
    filename: str
    approx_mb: int
    required: bool
    purpose: str
    # DepMap's portal 403s a plain GET; such a dataset is reported by --check
    # with instructions, never silently attempted and half-downloaded.
    manual: bool = False
    # A file that downloads as HTML (a login wall, a moved endpoint) is worse
    # than a missing one: it fails much later, somewhere confusing.
    expect_prefix: bytes | None = None

    @property
    def path(self) -> Path:
        return DEST_DIR / self.filename


DATASETS: tuple[Dataset, ...] = (
    Dataset(
        name="uniprot-idmapping",
        url=("https://ftp.uniprot.org/pub/databases/uniprot/current_release/"
             "knowledgebase/idmapping/by_organism/HUMAN_9606_idmapping.dat.gz"),
        filename="HUMAN_9606_idmapping.dat.gz",
        approx_mb=35,
        required=True,
        purpose=("gene symbol / alias -> UniProt accession. Used by "
                 "target_resolve (binder stage 0), the PPI chain-assignment "
                 "guards, and every graph tool."),
        expect_prefix=b"\x1f\x8b",           # gzip magic
    ),
    Dataset(
        name="hgnc",
        url=("https://storage.googleapis.com/public-download-files/hgnc/"
             "tsv/tsv/hgnc_complete_set.txt"),
        filename="hgnc_complete_set.tsv",
        approx_mb=17,
        required=True,
        purpose=("approved symbols, aliases and withdrawn symbols. Resolves "
                 "the alias soup in curated fingerprints to one symbol."),
        expect_prefix=b"hgnc_id\t",
    ),
    # DepMap serves its downloads through an interactive portal that 403s a
    # plain GET (verified), so this one cannot be automated honestly. It is
    # listed here so `--check` can report it and tell the user where to get it,
    # rather than being discovered as a FileNotFoundError months later.
    Dataset(
        name="depmap-crispr",
        url="https://depmap.org/portal/data_page/?tab=allData  (file: CRISPRGeneEffect.csv)",
        filename="CRISPRGeneEffect.csv",
        approx_mb=420,
        required=False,
        manual=True,
        purpose=("CRISPR Chronos gene-effect matrix. Only the wildcard-expert "
                 "DepMap tools (get_genetic_codependency, "
                 "find_cocorrelated_genes) need it."),
    ),
)


def _human(n: int) -> str:
    return f"{n / 1_048_576:,.1f} MB"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_provenance() -> dict:
    if not PROVENANCE.exists():
        return {}
    try:
        return json.loads(PROVENANCE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _record_provenance(ds: Dataset, path: Path) -> None:
    data = _load_provenance()
    data[ds.name] = {
        "url": ds.url,
        "filename": ds.filename,
        "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }
    PROVENANCE.parent.mkdir(parents=True, exist_ok=True)
    PROVENANCE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def download(ds: Dataset, *, force: bool = False) -> bool:
    """Fetch one dataset. Returns True if the file is present afterwards."""
    if ds.path.exists() and not force:
        print(f"  [skip] {ds.filename} already present ({_human(ds.path.stat().st_size)})")
        return True

    if ds.manual:
        print(f"  [MANUAL] {ds.filename} (~{ds.approx_mb} MB) cannot be fetched "
              f"automatically.\n           Download it from:\n           {ds.url}\n"
              f"           and save it to {ds.path}")
        return False

    print(f"  [get ] {ds.filename}  (~{ds.approx_mb} MB)")
    print(f"         {ds.url}")
    # Download to a temp name so an interrupted transfer never leaves a
    # truncated file that looks complete on the next run.
    tmp = ds.path.with_suffix(ds.path.suffix + ".part")
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(ds.url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = 100 * done / total
                        print(f"\r         {pct:5.1f}%  {_human(done)}", end="")
            print("\r" + " " * 40, end="\r")
    except requests.RequestException as exc:
        tmp.unlink(missing_ok=True)
        print(f"  [FAIL] {ds.filename}: {exc}")
        msg = str(exc)
        if "Missing Authority Key Identifier" in msg:
            # Python 3.13 tightened X.509 validation and rejects re-signed
            # certificates that 3.12 accepts. Setting a CA bundle does NOT
            # help — the chain is structurally non-compliant, not untrusted.
            print("         This is Python "
                  f"{sys.version_info.major}.{sys.version_info.minor} refusing a "
                  "certificate re-signed by a TLS-inspecting\n"
                  "         proxy. 3.13 enforces stricter X.509 rules than 3.12, "
                  "and a CA bundle\n"
                  "         will NOT fix it. Set LPT_SSL_RELAX_STRICT=1 in "
                  ".env to clear that one\n"
                  "         flag — trust chain, hostname and expiry checks all "
                  "still apply.\n"
                  "         (Or build the venv with python3.12.)")
        elif "CERTIFICATE_VERIFY_FAILED" in msg:
            print("         Behind a TLS-inspecting proxy? Set LPT_CA_BUNDLE "
                  "in .env (see .env.example).")
        return False

    if ds.expect_prefix:
        with tmp.open("rb") as fh:
            head = fh.read(len(ds.expect_prefix))
        if head != ds.expect_prefix:
            tmp.unlink(missing_ok=True)
            print(f"  [FAIL] {ds.filename}: unexpected content (got {head!r}). "
                  f"The endpoint may now require a login or have moved; "
                  f"download it by hand into {DEST_DIR}/")
            return False

    tmp.replace(ds.path)
    _record_provenance(ds, ds.path)
    print(f"  [ok  ] {ds.filename}  {_human(ds.path.stat().st_size)}")
    return True


def check() -> int:
    """Report what's present. Exit 1 if a REQUIRED file is missing."""
    prov = _load_provenance()
    missing_required = 0
    print(f"Reference data in {DEST_DIR}\n")
    for ds in DATASETS:
        tag = "required" if ds.required else "optional"
        if ds.path.exists():
            when = (prov.get(ds.name) or {}).get("downloaded_at", "unknown date")
            print(f"  [ok  ] {ds.filename:32s} {_human(ds.path.stat().st_size):>12s}  "
                  f"({tag}, fetched {when})")
        else:
            tag = f"{tag}, manual download" if ds.manual else tag
            print(f"  [MISS] {ds.filename:32s} {'':>12s}  ({tag})")
            if ds.manual:
                print(f"         get it from: {ds.url}")
            print(f"         {ds.purpose}")
            if ds.required:
                missing_required += 1
    if missing_required:
        print(f"\n{missing_required} required file(s) missing — the binder track "
              f"cannot start.\nRun: python scripts/fetch_reference_data.py")
        return 1
    print("\nAll required reference data present.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download the reference datasets LPT needs but does not ship.")
    ap.add_argument("--check", action="store_true",
                    help="Report what's present and exit; download nothing.")
    ap.add_argument("--with-depmap", action="store_true",
                    help=f"Also fetch CRISPRGeneEffect.csv (~420 MB), needed "
                         f"only by the wildcard-expert DepMap tools.")
    ap.add_argument("--force", action="store_true",
                    help="Re-download even if the file is already present.")
    args = ap.parse_args()

    if args.check:
        return check()

    wanted = [d for d in DATASETS if d.required or (args.with_depmap and not d.required)]
    print(f"Fetching {len(wanted)} dataset(s) into {DEST_DIR}\n")
    results = [(d, download(d, force=args.force)) for d in wanted]
    # A manual dataset returning False is not a failure of this script.
    ok = all(res for d, res in results if not d.manual)

    if not args.with_depmap:
        print("\nSkipped CRISPRGeneEffect.csv (~420 MB) — pass --with-depmap if "
              "you want the wildcard-expert DepMap tools.")
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
