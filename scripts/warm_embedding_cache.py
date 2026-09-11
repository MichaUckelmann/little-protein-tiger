#!/usr/bin/env python3
"""Download the embedding model once, so `search_corpus` works offline.

The MCP launcher sets `HF_HUB_OFFLINE=1` (see `scripts/launch_mcp.py`), so the
model has to be in the HuggingFace cache before the literature-db server ever
starts. An uncached model there fails with a HuggingFace `OSError` that names
nothing about LPT, nothing about the cache, and nothing about what to do.

    .venv/bin/python scripts/warm_embedding_cache.py

**This exists because the bare one-liner it replaces does not load `.env`.**
`SETUP_AGENT.md` used to print

    python -c "from sentence_transformers import SentenceTransformer as S; \\
               S('NeuML/pubmedbert-base-embeddings')"

which is correct on an open network and dead behind a TLS-inspecting proxy:
without `src.env_config.load_env()` neither `LPT_CA_BUNDLE` nor
`LPT_SSL_RELAX_STRICT` is applied, so every HTTPS call to huggingface.co fails
verification, `huggingface_hub` retries five times, and the final error is the
same unhelpful `OSError` the caching failure produces — the one message the
document was trying to pre-empt. Measured on the reference workstation: 73 s to
fail, versus 12 s to succeed with `load_env()` called first.

Every other LPT entry point calls `load_env()`. A documented command that does
not is a documented command that only works on some networks.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# BEFORE sentence_transformers, which builds its HTTP session at import.
from src.env_config import load_env  # noqa: E402

#: Kept in sync with `src/vector_store.py`; asserted by tests.
MODEL = "NeuML/pubmedbert-base-embeddings"
#: Measured on the reference workstation, blobs only. Say it, because the
#: download is not small and no document mentioned a size at all.
APPROX_MB = 439


def _cache_size(root: pathlib.Path) -> int:
    """Bytes on disk. Symlinks are SKIPPED: the hub keeps one copy under
    `blobs/` and a symlink farm under `snapshots/`, so following them reports
    exactly double (878 MB for a 439 MB model) — a wrong number in the one
    place the user is checking a number."""
    if not root.is_dir():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*")
               if f.is_file() and not f.is_symlink())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default=MODEL, help=f"default: {MODEL}")
    ap.add_argument("--check", action="store_true",
                    help="report whether it is already cached; download nothing")
    args = ap.parse_args()

    load_env()

    # Resolved after load_env, since HF_HOME may be set there.
    from huggingface_hub import constants

    hub = pathlib.Path(constants.HF_HUB_CACHE)
    slug = "models--" + args.model.replace("/", "--")
    before = _cache_size(hub / slug)

    if args.check:
        if before:
            print(f"[ok] {args.model} cached ({before / 1e6:.0f} MB) in {hub}")
            return 0
        print(f"[--] {args.model} NOT cached in {hub}\n"
              f"     run: .venv/bin/python scripts/warm_embedding_cache.py "
              f"(~{APPROX_MB} MB)")
        return 1

    if before:
        print(f"[ok] already cached ({before / 1e6:.0f} MB) — nothing to do")
        return 0

    print(f"downloading {args.model} (~{APPROX_MB} MB) into {hub}")
    t0 = time.time()
    try:
        from sentence_transformers import SentenceTransformer

        SentenceTransformer(args.model)
    except ImportError:
        print("sentence-transformers is not installed — this is the literature\n"
              "track's dependency:  "
              f'"{sys.executable}" -m pip install -e ".[corpus]"', file=sys.stderr)
        return 2
    except Exception as exc:                                  # noqa: BLE001
        # Name the two causes that actually happen, because the upstream
        # message names neither.
        print(f"\nFAILED after {time.time() - t0:.0f}s: {type(exc).__name__}: "
              f"{exc}\n", file=sys.stderr)
        print("The two causes seen in practice:\n"
              "  * no network route to huggingface.co, or a TLS-inspecting\n"
              "    proxy. Run `python scripts/doctor.py` — it names the exact\n"
              "    LPT_CA_BUNDLE / LPT_SSL_RELAX_STRICT setting for this box.\n"
              "  * HF_HUB_OFFLINE=1 in the environment, which forbids the\n"
              "    download this script exists to perform.", file=sys.stderr)
        return 1

    after = _cache_size(hub / slug)
    print(f"[ok] cached {after / 1e6:.0f} MB in {time.time() - t0:.0f}s -> {hub}")
    print("`search_corpus` now works offline, which is how the MCP server "
          "runs it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
