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
    CRISPRGeneEffect.csv          DepMap   ~440 MB   optional (--with-depmap)
    12 mmCIF entries (ASU + BA1)   RCSB     ~25 MB   optional (--with-structures)

All are public and free. Usage:

    python scripts/fetch_reference_data.py              # the two required files
    python scripts/fetch_reference_data.py --with-depmap
    python scripts/fetch_reference_data.py --with-structures
    python scripts/fetch_reference_data.py --check      # report, download nothing

`--with-structures` fetches the entries the test suite names — 24 tests SKIP
without them, including the two that pin the 3KYS A344 palmitoyl-cysteine
lesson (which cost a real campaign ten RFD3 aborts) and the five guarding
hotspot grounding against a wrong chain. A skip reads as a pass in a CI
summary.

BOTH the deposited ASU and biological assembly 1 are fetched for every entry.
Assembly 1 because the ASU can split a biological dimer across symmetry
copies, so the chain pair you measure is not the one that exists in solution
(`target_resolve.ensure_assembly` says the same). The ASU as well because
`PipelineRunner._ensure_structure` RETURNS the ASU path and downloads it when
absent — so an entry with only an assembly file on disk still reaches RCSB
from inside a unit test, which is how fetching assemblies alone turned a
skipping module into a live download that then failed on the CI runner.

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
        # The CRISPR matrix is relocatable (LPT_DEPMAP_CSV / paths.depmap_csv),
        # and `src.depmap` is the one place that precedence is implemented.
        # Asking it, rather than reproducing the rule, is what keeps `--check`
        # and the hand-download instructions pointing where the loader will
        # actually look — telling a user to save 440 MB to a path nothing
        # reads is the whole failure this avoids.
        if self.filename == "CRISPRGeneEffect.csv":
            try:
                from src.depmap import _configured_path

                return _configured_path()
            except Exception:                                # noqa: BLE001
                pass
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
        approx_mb=440,
        required=False,
        manual=True,
        purpose=("CRISPR Chronos gene-effect matrix. Needed by "
                 "find_cocorrelated_genes, get_genetic_codependency and "
                 "export_subgraph(with_depmap=True) — reachable from the "
                 "pathway, literature and corpus-explorer skills, so a "
                 "design run can call them. Without it those tools return "
                 "an error and the run continues."),
    ),
)


def _human(n: int) -> str:
    """Decimal MB, because that is what the reader is comparing against.

    RCSB, GitHub's release pages and every `ls -h` alternative the docs quote
    report decimal; dividing by 1_048_576 and writing "MB" gave a third
    number for the same file, which is how a reader learns to distrust the
    tool (`test_sizes_are_reported_in_the_unit_they_claim`). That test greps
    for `2**20` and this spelt the constant out, so it was the one place in
    the script still doing it.
    """
    return f"{n / 1_000_000:,.1f} MB"


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
                # Both halves, matching `download`'s manual branch: a
                # hand-download needs the exact destination as much as the
                # URL, and the header's DEST_DIR only implies it.
                print(f"         get it from: {ds.url}")
                print(f"         save it as:  {ds.path}")
            print(f"         {ds.purpose}")
            if ds.required:
                missing_required += 1
    # Structures live elsewhere (`data/structures/`) and are not `Dataset`s —
    # they are cached by the pipeline itself as runs touch entries, so most of
    # them arrive without this script. Reported so `--check` does not imply
    # `data/depmap` is the whole of what the suite reads.
    sdir = _ROOT / "data" / "structures"
    wanted = [f"{n}_ba1.cif" for n in REFERENCE_STRUCTURES]
    wanted += [f"{n}.cif" for n in REFERENCE_STRUCTURES]
    wanted += [n for v in REFERENCE_ALIASES.values() for n in v]
    have = [n for n in wanted if (sdir / n).exists()]
    n_want = len(wanted)
    print(f"\nTest structures in {sdir}: {len(have)}/{n_want} present"
          + ("" if len(have) == n_want else
             "  — 24 tests skip; run with --with-structures (~25 MB)"))

    if missing_required:
        print(f"\n{missing_required} required file(s) missing — the binder track "
              f"cannot start.\nRun: python scripts/fetch_reference_data.py")
        return 1
    print("\nAll required reference data present.")
    return 0


#: The entries the non-network tests read off disk. Each is named by a test's
#: own skip guard, so this list is derived from the suite rather than chosen:
#: grep `reason="... not downloaded"` / `not in this checkout`.
#:
#: 8ZNL is here for `scripts/bench_models.py`'s INTERFACE_CASES rather than
#: for a skip guard — `test_every_interface_case_names_a_structure_that_is_in_the_checkout`
#: derives its list from the benchmark, whose own reason for wanting them on
#: disk is that "a download failure on one cell would show up as that model
#: being slower".
#: 3FLN, 5VAI, 6JJW and 6VJJ are named by the guard tests added with the
#: absent-residue/absent-chain fixes (9965bb6) and by the trim benchmark.
#: 3FLN has exactly ONE chain and 6JJW's hotspot numbers exist on only one of
#: its two, which is what makes them the fixtures those guards need.
REFERENCE_STRUCTURES = ("3FLN", "3KYS", "3N7S", "5GN0", "5HYN", "5VAI",
                        "6E3Y", "6JJW", "6VJJ", "7CZD", "7XQ8", "8ZNL")

#: Extra filenames beyond `<ID>.cif` and `<ID>_ba1.cif`. 3KYS is read as
#: `3kys.cif` — LOWERCASE — by `test_release_fixes.py`'s
#: `test_the_bsa_mismatch_is_real_and_the_fix_silences_a_no_op_trim`, and a
#: case-sensitive filesystem is not persuaded by `3KYS.cif` being close.
REFERENCE_ALIASES = {"3KYS": ("3kys.cif",)}


def fetch_structures(force: bool = False) -> bool:
    """Download the reference mmCIF entries into `data/structures/`.

    Reuses `target_resolve.ensure_assembly` rather than re-implementing the
    RCSB URL and the gzip step: it already caches on disk, already prefers
    assembly 1, and is the function the pipeline itself uses, so this script
    cannot drift from what a real run downloads.
    """
    import gzip

    structures_dir = _ROOT / "data" / "structures"
    structures_dir.mkdir(parents=True, exist_ok=True)
    from src.target_resolve import ensure_assembly

    print(f"Fetching {len(REFERENCE_STRUCTURES)} entries into {structures_dir}\n")
    ok = True
    for pdb_id in REFERENCE_STRUCTURES:
        dest = structures_dir / f"{pdb_id}_ba1.cif"
        if force and dest.exists():
            dest.unlink()
        got = ensure_assembly(pdb_id, structures_dir)
        if got:
            print(f"  OK   {got.name}  {_human(got.stat().st_size)}")
        else:
            print(f"  FAIL {pdb_id}_ba1.cif")
            ok = False

        # The deposited ASU as well, for every entry, not just the ones a test
        # opens by name: `_ensure_structure` returns the ASU path and downloads
        # it when absent, so an entry with only an assembly file on disk still
        # reaches RCSB from inside a test. That is how a whole module
        # (`test_structure_first_track.py`, 7CZD) went from skipping to making
        # a live download the moment assemblies were fetched — and the download
        # then failed on the runner, which is a worse outcome than the skip.
        # Six megabytes buys the property that no test needs the network.
        names = (f"{pdb_id}.cif", *REFERENCE_ALIASES.get(pdb_id, ()))
        raw = None
        for name in names:
            dest = structures_dir / name
            if dest.exists() and not force:
                continue
            if raw is None:
                url = f"https://files.rcsb.org/download/{pdb_id.upper()}.cif.gz"
                try:
                    resp = requests.get(url, timeout=60)
                    resp.raise_for_status()
                    raw = gzip.decompress(resp.content)
                except Exception as exc:                     # noqa: BLE001
                    print(f"  FAIL {name}: {exc}")
                    ok = False
                    break
            dest.write_bytes(raw)
            print(f"  OK   {dest.name}  {_human(len(raw))}")
    print()
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download the reference datasets LPT needs but does not ship.")
    ap.add_argument("--check", action="store_true",
                    help="Report what's present and exit; download nothing.")
    ap.add_argument("--with-depmap", action="store_true",
                    help=f"Also fetch CRISPRGeneEffect.csv (~440 MB), needed "
                         f"only by find_cocorrelated_genes, "
                         f"get_genetic_codependency and "
                         f"export_subgraph(with_depmap=True). Runs continue "
                         f"without it; those tools return an error.")
    ap.add_argument("--with-structures", action="store_true",
                    help=f"Also fetch the {len(REFERENCE_STRUCTURES)} mmCIF "
                         f"entries the test suite reads, ASU and assembly 1 "
                         f"(~25 MB). Without them 24 tests skip, including "
                         f"the two that pin the 3KYS A344 modified-residue "
                         f"lesson.")
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

    if args.with_structures:
        ok = fetch_structures(force=args.force) and ok

    if not args.with_depmap:
        print("\nSkipped CRISPRGeneEffect.csv (~440 MB) — pass --with-depmap "
              "if you want find_cocorrelated_genes / "
              "get_genetic_codependency. Every other corpus tool works "
              "without it, and a run that calls them continues.")
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
