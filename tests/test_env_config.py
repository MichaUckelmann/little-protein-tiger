"""Tests for `src/env_config.py` — env-var path resolution and CA-bundle fan-out.

The CA-bundle tests exist because a real curation run failed here: `.bashrc`
exported only REQUESTS_CA_BUNDLE, which `requests` honours and `httpx` does not,
so `curate_papers.py --provider gemini` made its Gemini call successfully and
then died in the post-curation vector-ingest hook with CERTIFICATE_VERIFY_FAILED
while huggingface_hub (httpx) fetched the embedding model. The fan-out is the
fix; these tests keep the two var families from drifting apart again.
"""
from __future__ import annotations

import os

import pytest

from src.env_config import _CA_ENV_VARS, apply_ca_bundle, resolve_env_path


@pytest.fixture(autouse=True)
def _clean_ca_env(monkeypatch):
    """Every CA var unset, so a developer's own proxy config can't mask a bug."""
    for var in ("LPT_CA_BUNDLE", *_CA_ENV_VARS):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------
# resolve_env_path
# --------------------------------------------------------------------------

def test_env_var_wins_over_config_value(monkeypatch):
    monkeypatch.setenv("LPT_THING", "/from/env")
    assert resolve_env_path("LPT_THING", "/from/config") == "/from/env"


def test_config_value_is_the_same_machine_fallback():
    assert resolve_env_path("LPT_UNSET_THING", "/from/config") == "/from/config"


def test_none_when_neither_is_set():
    assert resolve_env_path("LPT_UNSET_THING", None) is None


def test_empty_env_var_falls_through_to_config(monkeypatch):
    """config.yaml now ships these keys as null, so "" must not shadow it."""
    monkeypatch.setenv("LPT_THING", "")
    assert resolve_env_path("LPT_THING", "/from/config") == "/from/config"


# --------------------------------------------------------------------------
# apply_ca_bundle
# --------------------------------------------------------------------------

def test_no_bundle_configured_is_a_no_op():
    """Off a corporate network certifi's defaults are correct — touch nothing."""
    assert apply_ca_bundle() is None
    assert not any(os.environ.get(v) for v in _CA_ENV_VARS)


def test_canonical_var_fans_out_to_every_client_family(monkeypatch, tmp_path):
    bundle = tmp_path / "ca.crt"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("LPT_CA_BUNDLE", str(bundle))

    assert apply_ca_bundle() == str(bundle)
    # SSL_CERT_FILE is the httpx/ssl one; REQUESTS_CA_BUNDLE the requests one.
    for var in _CA_ENV_VARS:
        assert os.environ[var] == str(bundle), f"{var} not propagated"


def test_a_lone_requests_var_still_reaches_httpx(monkeypatch, tmp_path):
    """The exact shape of the reported bug: only REQUESTS_CA_BUNDLE was set."""
    bundle = tmp_path / "ca.crt"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))

    assert apply_ca_bundle() == str(bundle)
    assert os.environ["SSL_CERT_FILE"] == str(bundle)


def test_a_deliberately_different_value_is_never_overwritten(monkeypatch, tmp_path):
    canonical = tmp_path / "ca.crt"
    canonical.write_text("-----BEGIN CERTIFICATE-----\n")
    special = tmp_path / "other.crt"
    special.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("LPT_CA_BUNDLE", str(canonical))
    monkeypatch.setenv("SSL_CERT_FILE", str(special))

    apply_ca_bundle()
    assert os.environ["SSL_CERT_FILE"] == str(special)
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(canonical)


def test_a_missing_bundle_path_is_ignored_rather_than_applied(monkeypatch, tmp_path):
    """`ssl` raises on a missing cafile instead of falling back, so a typo here
    would break every HTTPS call rather than the one it was meant to fix."""
    monkeypatch.setenv("LPT_CA_BUNDLE", str(tmp_path / "does-not-exist.crt"))

    assert apply_ca_bundle() is None
    assert not any(os.environ.get(v) for v in _CA_ENV_VARS)


def test_a_directory_is_rejected_like_a_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("LPT_CA_BUNDLE", str(tmp_path))
    assert apply_ca_bundle() is None


def test_applying_twice_is_idempotent(monkeypatch, tmp_path):
    bundle = tmp_path / "ca.crt"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("LPT_CA_BUNDLE", str(bundle))

    assert apply_ca_bundle() == apply_ca_bundle() == str(bundle)
    for var in _CA_ENV_VARS:
        assert os.environ[var] == str(bundle)


def test_every_entry_point_routes_through_load_env():
    """`load_dotenv` must not be called directly — a new call site that used it
    would silently skip the CA fan-out, which is how httpx came to be missed."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for sub in ("src", "scripts", "web"):
        for path in (root / sub).rglob("*.py"):
            if path.name == "env_config.py" or "__pycache__" in path.parts:
                continue
            if "load_dotenv" in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(path.relative_to(root)))
    assert not offenders, f"call src.env_config.load_env instead: {offenders}"
