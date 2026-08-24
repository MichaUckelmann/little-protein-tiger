#!/usr/bin/env bash
# Bootstrap a fresh checkout: venv -> deps -> .env -> PDB metadata cache ->
# GPU-tool diagnostic. Safe to re-run; never overwrites an existing .env and
# never fails just because an optional GPU tool isn't installed yet.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV_DIR="$ROOT/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating virtual environment at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
else
    echo "==> Reusing existing virtual environment at $VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "==> Installing project (core + dev extras)"
pip install -e ".[dev]"

if [ ! -f "$ROOT/.env" ]; then
    echo "==> Copying .env.example -> .env (fill in your API keys)"
    cp "$ROOT/.env.example" "$ROOT/.env"
else
    echo "==> .env already exists, leaving it untouched"
fi

echo "==> Fetching RCSB PDB metadata cache (data/pdb_metadata.json)"
python "$ROOT/scripts/fetch_pdb_metadata.py" || echo "    (non-fatal: metadata fetch failed, see output above)"

echo
echo "==> GPU tool diagnostic"
python - "$ROOT" <<'PYEOF'
import sys
from pathlib import Path

root = Path(sys.argv[1])
config_path = root / "config.yaml"

if not config_path.exists():
    print(f"  config.yaml not found at {config_path} -- skipping diagnostic")
    sys.exit(0)

try:
    import yaml
except ImportError:
    print("  pyyaml not installed -- skipping diagnostic")
    sys.exit(0)

with open(config_path) as f:
    config = yaml.safe_load(f) or {}


def get(path, default=None):
    node = config
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


checks = [
    ("BoltzGen executable", ["design", "workstation", "boltzgen_executable"]),
    ("PyRosetta python", ["design", "pyrosetta", "python_executable"]),
    ("Foundry (RFD3/RF3) root", ["design", "foundry", "root"]),
]

for label, path in checks:
    value = get(path)
    if not value:
        print(f"  [ ] {label}: not set in config.yaml ({'.'.join(path)})")
        continue
    p = Path(value)
    found = p.exists()
    mark = "x" if found else " "
    status = "found" if found else "NOT FOUND (configure manually per README)"
    print(f"  [{mark}] {label}: {value} -- {status}")

print()
print("  These are optional GPU tools with their own setup steps (see README")
print("  / docs/pyrosetta_setup.md). Missing ones don't block the literature")
print("  corpus pipeline or CLI skills -- only the binder/design track needs them.")
PYEOF

echo
echo "==> Setup complete. Activate the venv with:"
echo "    source $VENV_DIR/bin/activate"
