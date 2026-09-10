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
from loguru import logger

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



# ---------------------------------------------------------------------------
# Python 3.13's stricter certificate validation vs TLS-inspecting proxies
# ---------------------------------------------------------------------------
#
# Python 3.13 turns on ssl.VERIFY_X509_STRICT by default; 3.12 does not.
# Verified on this machine:
#     3.12  verify_flags = VERIFY_X509_TRUSTED_FIRST
#     3.13  verify_flags = VERIFY_X509_TRUSTED_FIRST | VERIFY_X509_STRICT
#                          | VERIFY_X509_PARTIAL_CHAIN
#
# STRICT enforces RFC 5280 structural rules that many corporate TLS-inspection
# proxies violate when they re-sign a certificate — most commonly by omitting
# the Authority Key Identifier extension. The failure is
# "CERTIFICATE_VERIFY_FAILED: Missing Authority Key Identifier", and pointing at
# a CA bundle does NOT fix it: the chain is structurally non-compliant, not
# untrusted.
#
# `LPT_SSL_RELAX_STRICT=1` clears that ONE flag. It is opt-in, because silently
# relaxing a security default is not something a tool should decide for you.
# What it does NOT relax, verified against badssl.com on 3.13 with the flag
# cleared: self-signed certificates, untrusted roots and expired certificates
# are all still rejected, and hostname verification still fails a mismatch.
# Only the structural pedantry is dropped — which is exactly what Python 3.12,
# curl and every browser already do.

_STRICT_RELAXED = False


def relax_x509_strict() -> bool:
    """Clear VERIFY_X509_STRICT on newly-created default SSL contexts.

    Wraps `ssl.create_default_context`, so `requests` (via urllib3), `httpx`
    and `urllib` all pick it up — they all build their context through it.
    Idempotent. Returns True if the relaxation is now active.
    """
    global _STRICT_RELAXED
    import ssl
    import sys

    if _STRICT_RELAXED:
        return True
    if not hasattr(ssl, "VERIFY_X509_STRICT"):
        return False

    def _clear(ctx):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return ctx

    # 1. The stdlib path — urllib, httpx, and anything calling it directly.
    _std = ssl.create_default_context

    def _relaxed_std(*args, **kwargs):
        return _clear(_std(*args, **kwargs))

    _relaxed_std.__wrapped__ = _std
    ssl.create_default_context = _relaxed_std

    # 2. urllib3's own builder — `requests` goes through this, NOT through
    #    ssl.create_default_context, so patching only the stdlib silently
    #    leaves every requests call still failing. urllib3 mirrors 3.13's
    #    defaults itself (verified: same urllib3 2.7.0 yields STRICT on 3.13
    #    and not on 3.12).
    try:
        import urllib3.util.ssl_ as _u3
    except ImportError:
        pass
    else:
        _orig_u3 = _u3.create_urllib3_context

        def _relaxed_u3(*args, **kwargs):
            return _clear(_orig_u3(*args, **kwargs))

        _relaxed_u3.__wrapped__ = _orig_u3
        _u3.create_urllib3_context = _relaxed_u3
        # requests imports the symbol into its own namespace at import time,
        # so rebinding the module attribute alone would not reach it.
        for mod_name in ("urllib3.util", "urllib3.connection",
                         "requests.adapters"):
            mod = sys.modules.get(mod_name)
            if mod is not None and hasattr(mod, "create_urllib3_context"):
                mod.create_urllib3_context = _relaxed_u3

    _STRICT_RELAXED = True
    return True


#: Values `.env.example` ships as prompts, which are NOT keys. `.env.example`
#: cannot ship empty values for these — the file doubles as documentation of
#: each key's shape — so a `.env` copied and not edited has every one of them
#: set to something truthy. Anything that tests a key with a bare
#: `os.environ.get(var)` therefore reports "configured" for a file nobody
#: touched, and the failure resurfaces phases later as the provider's own
#: opaque HTTP error. `scripts/doctor.py` had this right and everything else
#: had it wrong; it lives here so there is one list.
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "GEMINI_API_KEY": ("", "..."),
    "ANTHROPIC_API_KEY": ("", "...", "sk-ant-..."),
    "OPENAI_API_KEY": ("", "...", "sk-..."),
    "NCBI_EMAIL": ("", "you@example.com"),
}


def key_is_usable(name: str) -> tuple[bool, str]:
    """(usable, why-not) for one env var, placeholders counted as unset."""
    raw = (os.environ.get(name) or "").strip()
    if raw in PLACEHOLDERS.get(name, ("",)):
        return False, ("still the .env.example placeholder" if raw else "not set")
    return True, ""


def load_env(dotenv_path: Path | None = None) -> None:
    """Load `.env` and reconcile the CA-bundle vars.

    Every entry point that makes a network call goes through this rather than
    calling `load_dotenv` directly, so the CA reconciliation above cannot be
    forgotten at a new call site — which is exactly how the httpx half of the
    stack came to be missed.
    """
    load_dotenv(dotenv_path if dotenv_path is not None else _ROOT / ".env")
    apply_ca_bundle()
    if os.environ.get("LPT_SSL_RELAX_STRICT", "").strip().lower() in (
            "1", "true", "yes", "on"):
        if relax_x509_strict():
            logger.debug("VERIFY_X509_STRICT cleared (LPT_SSL_RELAX_STRICT)")
