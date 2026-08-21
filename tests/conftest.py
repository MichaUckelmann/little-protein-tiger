"""Shared fixtures. Tests must run with no network and no GPU."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# The reference campaign. Present on the workstation, absent in CI — tests that
# need it skip rather than fail, so the suite stays runnable anywhere.
BCR = Path("/home/m.uckelmann_cbs-niob.local/data/BCR/outputs/production/CD79b")


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
