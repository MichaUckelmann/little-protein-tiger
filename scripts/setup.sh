#!/usr/bin/env bash
# Bootstrap a fresh checkout: venv -> deps -> .env -> reference data, then hand
# off to scripts/doctor.py for the environment report and scripts/quickstart.py
# for proof that it works.
#
# Safe to re-run. Never overwrites an existing .env. Never fails because an
# optional external tool isn't installed — those are reported, not enforced.
#
#   ./scripts/setup.sh                 # base install (~650 MB)
#   ./scripts/setup.sh --with-corpus   # + the corpus extra and the corpus itself
#   ./scripts/setup.sh --check         # report only; install nothing
#
# Bash only. Windows users: follow the manual steps in README.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WITH_CORPUS=0
CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --with-corpus) WITH_CORPUS=1 ;;
        --check)       CHECK_ONLY=1 ;;
        -h|--help)
            sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

VENV_DIR="$ROOT/.venv"
PY="$VENV_DIR/bin/python"

if [ "$CHECK_ONLY" -eq 1 ]; then
    [ -x "$PY" ] || { echo "No venv at $VENV_DIR — run ./scripts/setup.sh first."; exit 1; }
    exec "$PY" "$ROOT/scripts/doctor.py"
fi

# ---------------------------------------------------------------------------
# 1. Virtual environment
# ---------------------------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating virtual environment at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
else
    echo "==> Reusing existing virtual environment at $VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# LPT needs 3.12+. Catch it here rather than three imports later.
"$PY" - <<'PYEOF'
import sys
if sys.version_info < (3, 12):
    v = sys.version_info
    raise SystemExit(
        f"  Python {v.major}.{v.minor} is too old — LPT needs 3.12+.\n"
        f"  Create the venv with a newer interpreter:\n"
        f"      python3.12 -m venv .venv")
PYEOF

# ---------------------------------------------------------------------------
# 2. Dependencies
# ---------------------------------------------------------------------------
# The base install deliberately excludes sentence-transformers and lancedb:
# they pull torch and the full CUDA stack (~3 GB) for the ONE feature that
# needs them, semantic corpus search.
if [ "$WITH_CORPUS" -eq 1 ]; then
    echo "==> Installing project (base + corpus + dev)  — ~3.7 GB, includes torch"
    pip install -e ".[corpus,dev]"
else
    echo "==> Installing project (base + dev)  — ~650 MB, no torch"
    echo "    Add --with-corpus for semantic corpus search (pulls torch, ~3 GB more)."
    pip install -e ".[dev]"
fi

# ---------------------------------------------------------------------------
# 3. .env
# ---------------------------------------------------------------------------
if [ ! -f "$ROOT/.env" ]; then
    echo "==> Copying .env.example -> .env"
    cp "$ROOT/.env.example" "$ROOT/.env"
    echo "    Add GEMINI_API_KEY — it is the default provider for every LLM stage."
else
    echo "==> .env already exists, leaving it untouched"
fi

mkdir -p "$ROOT/data/structures" "$ROOT/data/depmap"

# ---------------------------------------------------------------------------
# 4. Reference data — a hard requirement for the ppi and binder tracks
# ---------------------------------------------------------------------------
echo "==> Fetching reference data (UniProt id-mapping + HGNC, ~52 MB)"
"$PY" "$ROOT/scripts/fetch_reference_data.py" || {
    echo "    Non-fatal here, but --workflow binder cannot start without it."
    echo "    Re-run: python scripts/fetch_reference_data.py"
}

# ---------------------------------------------------------------------------
# 5. Corpus (optional)
# ---------------------------------------------------------------------------
if [ "$WITH_CORPUS" -eq 1 ]; then
    echo "==> Fetching the pre-built corpus (~83 MB, free)"
    "$PY" "$ROOT/scripts/fetch_corpus.py" || {
        echo "    No corpus installed. Re-run: python scripts/fetch_corpus.py"
    }
fi

# The PDB metadata cache derives its ID list from data/fingerprints/. With no
# fingerprints it is a no-op — say so, rather than printing "Cache is up to
# date", which reads as success for work that never happened.
if [ -d "$ROOT/data/fingerprints" ] && [ -n "$(ls -A "$ROOT/data/fingerprints" 2>/dev/null)" ]; then
    echo "==> Refreshing the RCSB PDB metadata cache"
    "$PY" "$ROOT/scripts/fetch_pdb_metadata.py" \
        || echo "    (non-fatal: metadata fetch failed, see above)"
else
    echo "==> Skipping the PDB metadata cache (no fingerprints yet)"
fi

# ---------------------------------------------------------------------------
# 6. Report and prove
# ---------------------------------------------------------------------------
# doctor.py replaces the diagnostic that used to live here. That one tested
# Path.exists() on values read from config.yaml, so on a fresh clone it
# cheerfully reported the MAINTAINER's tool paths as "found". doctor.py probes:
# it runs the binary, calls nvidia-smi, imports pyrosetta under the configured
# interpreter, and reports per track.
echo
"$PY" "$ROOT/scripts/doctor.py" || true

cat <<EOF

==> Setup complete.

    source $VENV_DIR/bin/activate

    python scripts/quickstart.py     see it work — ~7 s, no key, no GPU
    python scripts/doctor.py         re-check this environment any time

    Read docs/responsible-use.md before designing anything.
EOF
