"""Resolve machine-specific config values with an env-var override.

A handful of `design.*` config.yaml keys point at paths that are unique to
one machine or one lab's compute environment (a BoltzGen checkout, a
PyRosetta conda env, a foundry checkout, a cluster pipeline mirror).
`config.yaml` itself is tracked in git, so committing real values there
means every clone inherits whoever last edited it, and every user who
configures their own environment either hand-edits a shared file or fights
`git diff` noise on every commit.

`resolve_env_path` lets each of those keys be supplied via an env var
instead (normally through `.env`, which is gitignored) — the same
mechanism this repo already uses for API keys — while `config.yaml` keeps
the key present but empty, purely as documentation of what's configurable.
The env var wins when set; the config.yaml value (if any) is a same-machine
fallback for anyone who'd rather not use `.env` for this. See
`docs/environment_setup.md` for the full list of keys and what each is for.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent

# Every env var some part of this stack reads to find a CA bundle. They are NOT
# interchangeable: `requests` reads REQUESTS_CA_BUNDLE and otherwise defaults to
# certifi, while `httpx` — which the `anthropic` SDK and `huggingface_hub` are
# both built on — ignores it entirely and honours only SSL_CERT_FILE, the one
# Python's own `ssl` module consults. Behind a TLS-inspecting proxy whose root CA
# is in the system bundle but not in certifi, setting only REQUESTS_CA_BUNDLE
# therefore fixes half the stack and leaves the other half raising
# CERTIFICATE_VERIFY_FAILED — the Gemini call succeeds and the post-curation
# vector-ingest hook dies fetching the embedding model. `apply_ca_bundle` exists
# so one configured value reaches all of them.
_CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def resolve_env_path(env_var: str, config_value: str | None) -> str | None:
    """Return the env var's value if set, else the config.yaml value, else None."""
    env_value = os.environ.get(env_var)
    if env_value:
        return env_value
    return config_value or None


def apply_ca_bundle() -> str | None:
    """Propagate one configured CA bundle to every var the stack reads.

    Looks for a bundle in `LPT_CA_BUNDLE` first (the documented canonical name),
    then in any of `_CA_ENV_VARS` that is already set — so an existing
    `REQUESTS_CA_BUNDLE` in a shell profile or `.env` keeps working untouched.
    Whichever value is found is written to every var in `_CA_ENV_VARS` that is
    not already set; a var the user set deliberately to something else is never
    overwritten.

    Returns the bundle path applied, or None when none is configured (the normal
    case off a corporate network, where certifi's defaults are correct).
    """
    bundle = os.environ.get("LPT_CA_BUNDLE") or next(
        (os.environ[v] for v in _CA_ENV_VARS if os.environ.get(v)), None)
    if not bundle:
        return None
    # A path that doesn't exist would break TLS everywhere rather than fix it:
    # `ssl` raises on a missing cafile instead of falling back to the defaults.
    if not Path(bundle).is_file():
        return None
    for var in _CA_ENV_VARS:
        os.environ.setdefault(var, bundle)
    return bundle


def load_env(dotenv_path: Path | None = None) -> None:
    """Load `.env` and reconcile the CA-bundle vars.

    Every entry point that makes a network call goes through this rather than
    calling `load_dotenv` directly, so the CA reconciliation above cannot be
    forgotten at a new call site — which is exactly how the httpx half of the
    stack came to be missed.
    """
    load_dotenv(dotenv_path if dotenv_path is not None else _ROOT / ".env")
    apply_ca_bundle()
