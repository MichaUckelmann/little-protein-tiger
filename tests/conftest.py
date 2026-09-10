"""Shared fixtures.

Most tests run with no network and no GPU. A handful genuinely reach RCSB and
UniProt (the chain-assignment and hotspot-grounding guards are *about* real
structures, and mocking them would test the mock); those are marked `network`
and deselected by default — run them with `-m network`.

Anything that needs data this repo does not ship degrades to a skip rather than
a failure, so the suite is green on a fresh clone. CI fetches the reference data
first, so there it runs for real.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _dummy_api_keys() -> None:
    """Supply placeholder credentials so constructing a runner is valid.

    `SkillRunner.__init__` preflights the provider's API key — a fresh clone
    otherwise discovers a missing key as a bare `403 ... ?key=` from the
    provider, several expensive stages in. Unit tests construct runners and
    monkeypatch the transport, so they need a key to exist but never a real
    one. Setting a placeholder here also keeps the suite from behaving
    differently depending on whether the developer's `.env` happens to be
    populated.
    """
    for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        os.environ.setdefault(var, "test-placeholder-not-a-real-key")


_REFERENCE_DATA = [
    _ROOT / "data" / "depmap" / "HUMAN_9606_idmapping.dat.gz",
    _ROOT / "data" / "depmap" / "hgnc_complete_set.tsv",
]


@pytest.fixture(scope="session")
def reference_data() -> None:
    """Skip unless the UniProt/HGNC reference data is on disk.

    `data/` is gitignored, so these are absent on any fresh clone. Tests that
    resolve gene symbols (the chain-assignment guards, the PPI->foundry bridge)
    need them. Failing would say "this repo is broken" when the truth is "run
    scripts/fetch_reference_data.py" — CI does exactly that, so there these run
    for real rather than skipping.
    """
    missing = [p.name for p in _REFERENCE_DATA if not p.exists()]
    if missing:
        pytest.skip(f"reference data missing ({', '.join(missing)}) — "
                    f"run: python scripts/fetch_reference_data.py")

# The reference campaign. Present on the workstation, absent in CI — tests that
# need it skip rather than fail, so the suite stays runnable anywhere.
# LPT_BCR_REFERENCE_DIR points at the root of the reference campaign's data
# (the "BCR" directory); it lets another machine point at its own copy
# without editing this file, and defaults to this maintainer's workstation
# path. tests/test_foundry.py and scripts/test_e2e_binder.py derive their
# own sub-paths from the same env var.
_BCR_ROOT = Path(os.environ.get(
    "LPT_BCR_REFERENCE_DIR", str(Path.home() / "data" / "BCR")))
BCR = _BCR_ROOT / "outputs" / "production" / "CD79b"


@pytest.fixture(scope="session")
def bcr_dir() -> Path:
    if not BCR.is_dir():
        pytest.skip("CD79b reference campaign not available on this machine")
    return BCR


@pytest.fixture(scope="session")
def bcr_sidecar(bcr_dir: Path) -> Path:
    hits = sorted((bcr_dir / "rfd3").glob("*_model_0.json"))
    if not hits:
        pytest.skip("no RFD3 sidecars in the reference campaign")
    return hits[0]


@pytest.fixture
def foundry_root(tmp_path, monkeypatch) -> Path:
    """A placeholder LPT_FOUNDRY_ROOT for tests of the GENERATED driver script.

    `write_campaign_driver` refuses to emit a driver without a foundry root
    (correctly — there is no cross-machine default, and silently writing a
    driver that points nowhere is worse than failing). These tests assert on
    the script's *contents*, so a stub checkout will do; what they must not do
    is depend on the maintainer's `.env` being populated.

    It does need the three engine binaries, because `write_campaign_driver`
    resolves them against the checkout now — the configured paths default to
    the `.venv-blackwell` this project's reference machine hand-built for its
    sm_120 card, and every other GPU's foundry venv is named something else.
    A checkout with no binaries at all is not one any user would have.
    """
    root = tmp_path / "foundry"
    venv_bin = root / ".venv-blackwell" / "bin"
    venv_bin.mkdir(parents=True)
    for engine in ("rfd3", "mpnn", "rf3"):
        (venv_bin / engine).write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("LPT_FOUNDRY_ROOT", str(root))
    return root


@pytest.fixture
def roomy_disk(monkeypatch):
    """Pin free disk space so plans aren't clamped by the host machine.

    `foundry_runner.plan_campaign` reduces n_batches to fit available disk
    (~2.5 MB per RF3 design directory, minus a reserve). That is correct
    behaviour and has its own test — but it makes any OTHER assertion about
    plan sizing depend on the free space of whatever machine runs the suite.
    A GitHub runner has ~14 GB, which clamps every plan to the same floor and
    turned a linear-scaling assertion into 4 == 40.
    """
    import shutil as _shutil

    real = _shutil.disk_usage(".")
    monkeypatch.setattr(
        "src.foundry_runner.shutil.disk_usage",
        lambda _p: real._replace(total=2000 * 2**30, used=0, free=1500 * 2**30))
