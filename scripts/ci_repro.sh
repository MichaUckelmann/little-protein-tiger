#!/usr/bin/env bash
# Reproduce CI faithfully: fresh clone, BASE install only (no corpus extra),
# reference data fetched, no API keys, no GPU tools.
#
# The earlier simulation reused the dev venv, which HAS the corpus extra — so
# it passed while CI failed on `import src.mcp_server`. A CI reproduction that
# shares the developer's environment is not a reproduction.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=${1:?usage: ci_repro.sh <workdir>}
rm -rf "$DEST" && git clone -q "$SRC" "$DEST" && cd "$DEST"
# Overlay uncommitted work, so this tests what you are about to push.
cd "$SRC" && git status --porcelain | grep -v '^D ' | sed 's/^...//' | while read -r f; do
  [ -f "$SRC/$f" ] || continue; mkdir -p "$DEST/$(dirname "$f")"; cp "$SRC/$f" "$DEST/$f"; done
cd "$DEST"
uv venv -q .venv --python "${PY_VERSION:-3.12}"
uv pip install -q --python .venv/bin/python -e ".[dev]"     # NOT .[corpus]
.venv/bin/python scripts/fetch_reference_data.py >/dev/null 2>&1 || true
env -u GEMINI_API_KEY -u ANTHROPIC_API_KEY -u LPT_FOUNDRY_ROOT \
    -u LPT_PYROSETTA_PYTHON -u LPT_BOLTZGEN_EXECUTABLE -u LPT_CLUSTER_PIPELINE_ROOT \
  .venv/bin/python -m pytest tests/ -q
