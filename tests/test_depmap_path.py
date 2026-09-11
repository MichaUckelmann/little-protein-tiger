"""`LPT_DEPMAP_CSV` / `paths.depmap_csv` relocate the CRISPR matrix.

The path was hardcoded in `src/depmap.py`. That matrix is a ~420 MB HAND
download (DepMap's portal 403s a scripted GET), which is exactly the kind of
file a lab keeps one shared copy of — so a hardcoded path meant a duplicate
per checkout, a symlink, or going without. The same argument that got
`LPT_FOUNDRY_CKPT_DIR` added for the model weights.

What is pinned here is the precedence and, more importantly, that the two
things which REPORT on the file ask `src/depmap.py` where it is rather than
assuming `data/depmap/`. A `--check` that sends a user to save 420 MB where
the loader will not look is worse than no message at all.

No network, no GPU, and no read of the real matrix.
"""

from __future__ import annotations

import importlib
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def depmap(monkeypatch):
    """`src.depmap` with a clean environment, reloaded per test.

    Reloaded because the resolver runs at CALL time but `load_env()` inside it
    reads a process-wide `os.environ`; a stale module-level cache would make
    these pass or fail depending on test order.
    """
    monkeypatch.delenv("LPT_DEPMAP_CSV", raising=False)
    import src.depmap as dm

    importlib.reload(dm)
    # Neutralise config.yaml so the env-var tests measure the env var alone.
    monkeypatch.setattr(dm, "_config_value", lambda: None)
    return dm


def test_the_default_is_the_bundled_location(depmap):
    assert depmap._configured_path() == depmap._BUNDLED_PATH
    assert depmap._configured_path().name == "CRISPRGeneEffect.csv"


def test_an_absolute_env_var_wins(depmap, monkeypatch):
    monkeypatch.setenv("LPT_DEPMAP_CSV", "/mnt/lab/shared/CRISPRGeneEffect.csv")
    assert depmap._configured_path() == pathlib.Path(
        "/mnt/lab/shared/CRISPRGeneEffect.csv")


def test_a_relative_env_var_resolves_against_the_repo_root(depmap, monkeypatch):
    """Every other entry in `paths:` is repo-relative; this matches."""
    monkeypatch.setenv("LPT_DEPMAP_CSV", "data/alt/CRISPRGeneEffect.csv")
    assert depmap._configured_path() == _ROOT / "data/alt/CRISPRGeneEffect.csv"


def test_a_tilde_is_expanded(depmap, monkeypatch):
    """A shared-volume path is commonly written `~/shared/...`, and an
    unexpanded `~` becomes a literal directory named `~`."""
    monkeypatch.setenv("LPT_DEPMAP_CSV", "~/shared/CRISPRGeneEffect.csv")
    got = depmap._configured_path()
    assert "~" not in str(got)
    assert got == pathlib.Path.home() / "shared/CRISPRGeneEffect.csv"


def test_the_config_key_is_the_fallback_and_the_env_var_beats_it(
        depmap, monkeypatch):
    monkeypatch.setattr(depmap, "_config_value",
                        lambda: "/from/config/CRISPRGeneEffect.csv")
    assert depmap._configured_path() == pathlib.Path(
        "/from/config/CRISPRGeneEffect.csv")
    monkeypatch.setenv("LPT_DEPMAP_CSV", "/from/env/CRISPRGeneEffect.csv")
    assert depmap._configured_path() == pathlib.Path(
        "/from/env/CRISPRGeneEffect.csv")


def test_the_shipped_config_key_exists_and_is_blank():
    """Present as documentation of what is configurable, empty so a clone
    inherits nobody's machine — the rule `docs/environment_setup.md` states."""
    import yaml

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert "depmap_csv" in cfg["paths"]
    assert not cfg["paths"]["depmap_csv"]


def test_env_example_documents_it():
    text = (_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "LPT_DEPMAP_CSV=" in text


def test_a_broken_config_file_cannot_break_the_import(depmap, monkeypatch):
    """A low-level data module must not fail over a malformed config."""
    def boom():
        raise RuntimeError("unparseable config.yaml")

    monkeypatch.setattr(depmap, "_config_value", boom)
    # Falls back to the bundled default rather than propagating.
    assert depmap._configured_path() == depmap._BUNDLED_PATH


def test_the_missing_file_error_names_the_override(depmap, monkeypatch):
    """The message is the only place most users will learn the var exists."""
    monkeypatch.setenv("LPT_DEPMAP_CSV", "/nonexistent/CRISPRGeneEffect.csv")
    depmap._DATA = None
    depmap._DATA_PATH = None
    with pytest.raises(FileNotFoundError) as exc:
        depmap._ensure_loaded()
    msg = str(exc.value)
    assert "LPT_DEPMAP_CSV" in msg
    assert "/nonexistent/CRISPRGeneEffect.csv" in msg
    # And it must still say the run is not blocked.
    assert "continues" in msg or "NOT blocked" in msg


# ── the reporters must not reimplement the rule ──────────────────────────────

def test_doctor_and_the_fetcher_ask_depmap_where_to_look():
    """Reflection, not behaviour: both used to hardcode `data/depmap/`, and a
    reporter that disagrees with the loader is how a user gets told to put
    420 MB somewhere nothing reads."""
    import inspect

    import scripts.doctor as doctor
    import scripts.fetch_reference_data as frd

    assert "_configured_path" in inspect.getsource(doctor.check_reference_data)
    assert "_configured_path" in inspect.getsource(frd.Dataset.path.fget)


def test_the_fetchers_instructions_name_the_configured_path(monkeypatch):
    """`--check` and `--with-depmap` must print the path the loader reads."""
    import importlib

    import src.depmap as dm
    monkeypatch.setenv("LPT_DEPMAP_CSV", "/relocated/CRISPRGeneEffect.csv")
    importlib.reload(dm)
    monkeypatch.setattr(dm, "_config_value", lambda: None)

    import scripts.fetch_reference_data as frd

    ds = next(d for d in frd.DATASETS if d.filename == "CRISPRGeneEffect.csv")
    assert ds.path == pathlib.Path("/relocated/CRISPRGeneEffect.csv")
    # The two required datasets are NOT relocatable and must be unaffected.
    other = next(d for d in frd.DATASETS if d.filename.endswith("hgnc_complete_set.tsv"))
    assert other.path == frd.DEST_DIR / other.filename
