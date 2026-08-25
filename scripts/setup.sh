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

mkdir -p "$ROOT/data/structures" "$ROOT/data/depmap"

echo "==> Fetching reference data (UniProt id-mapping + HGNC, ~52 MB)"
# Hard requirement for the binder track's first stage. Without these,
# `--workflow binder` dies about two seconds in.
python "$ROOT/scripts/fetch_reference_data.py" \
    || echo "    (non-fatal here, but the binder track cannot run without it —
    re-run: python scripts/fetch_reference_data.py)"

# The PDB metadata cache derives its ID list from data/fingerprints/. On a
# fresh clone there are none, so this is a no-op — say so rather than printing
# "Cache is up to date", which reads as success for work that didn't happen.
if [ -d "$ROOT/data/fingerprints" ] && [ -n "$(ls -A "$ROOT/data/fingerprints" 2>/dev/null)" ]; then
    echo "==> Fetching RCSB PDB metadata cache (data/pdb_metadata.json)"
    python "$ROOT/scripts/fetch_pdb_metadata.py" || echo "    (non-fatal: metadata fetch failed, see output above)"
else
    echo "==> Skipping PDB metadata cache (no curated fingerprints yet)"
    echo "    Run scripts/fetch_pdb_metadata.py after your first curation batch."
fi

echo
echo "==> Binder-design environment diagnostic"
python - "$ROOT" <<'PYEOF'
import sys
from pathlib import Path

root = Path(sys.argv[1])
config_path = root / "config.yaml"

try:
    from dotenv import load_dotenv
    load_dotenv(root / ".env")
except ImportError:
    pass

if not config_path.exists():
    print(f"  config.yaml not found at {config_path} -- skipping diagnostic")
    sys.exit(0)

try:
    import yaml
except ImportError:
    print("  pyyaml not installed -- skipping diagnostic")
    sys.exit(0)

sys.path.insert(0, str(root))
from src.env_config import resolve_env_path  # noqa: E402

with open(config_path) as f:
    config = yaml.safe_load(f) or {}


def get(path, default=None):
    node = config
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


# Each check: label, env var (see .env.example), config.yaml key path,
# whether "not set" is expected/fine to leave blank for now.
checks = [
    ("BoltzGen executable", "LPT_BOLTZGEN_EXECUTABLE",
     ["design", "workstation", "boltzgen_executable"]),
    ("PyRosetta python", "LPT_PYROSETTA_PYTHON",
     ["design", "pyrosetta", "python_executable"]),
    ("Foundry (RFD3/RF3/MPNN) root", "LPT_FOUNDRY_ROOT",
     ["design", "foundry", "root"]),
    ("Cluster pipeline_root (--compute cluster only)", "LPT_CLUSTER_PIPELINE_ROOT",
     ["design", "cluster", "pipeline_root"]),
    ("Cluster Protenix repo (--compute cluster only)", "LPT_CLUSTER_PROTENIX_REPO",
     ["design", "cluster", "protenix_repo"]),
]

for label, env_var, cfg_path in checks:
    value = resolve_env_path(env_var, get(cfg_path))
    if not value:
        print(f"  [ ] {label}: not set ({env_var}, or {'.'.join(cfg_path)} in config.yaml)")
        continue
    p = Path(value)
    found = p.exists()
    mark = "x" if found else " "
    source = "env" if __import__("os").environ.get(env_var) else "config.yaml"
    status = "found" if found else "NOT FOUND"
    print(f"  [{mark}] {label} ({source}): {value} -- {status}")

print()
print("  These are all optional -- the literature corpus pipeline and CLI skills")
print("  work without any of them. Only the binder/design track needs them, and")
print("  only when the corresponding stage actually runs. Each is its own")
print("  external install with its own setup docs; set the env var in .env")
print("  (never config.yaml -- that file is tracked in git). See")
print("  docs/environment_setup.md for what each is and docs/pyrosetta_setup.md")
print("  for PyRosetta specifically.")
PYEOF

echo
echo "==> Setup complete. Activate the venv with:"
echo "    source $VENV_DIR/bin/activate"
