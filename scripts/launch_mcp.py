#!/usr/bin/env python3
"""
Portable MCP server launcher.

Derives all paths from the project root (this file's grandparent directory),
re-execs with the venv Python if needed, then starts the MCP server.

When moving to a new machine, only the 'cwd' field in .mcp.json and
claude_desktop_config.json needs updating — everything else is relative.
"""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent

# Locate venv Python — handles both Windows (.exe) and Unix
for candidate in [
    root / ".venv" / "Scripts" / "python.exe",   # Windows
    root / ".venv" / "bin" / "python3",           # Unix/Mac
    root / ".venv" / "bin" / "python",            # Unix/Mac fallback
]:
    if candidate.exists():
        venv_python = candidate
        break
else:
    sys.exit(f"ERROR: venv Python not found under {root / '.venv'} — run pip install first")

# Re-exec with venv Python if we're not already running inside it.
# On Windows os.execv uses spawnv (not a true exec), so the original
# process exits and Claude Desktop loses track of the server.
# subprocess.run keeps the original process alive (blocking) while the
# venv Python inherits its stdin/stdout MCP pipes.
if Path(sys.executable).resolve() != venv_python.resolve():
    import subprocess
    sys.exit(subprocess.run(
        [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:]
    ).returncode)

# Running in venv — set data paths relative to project root
os.environ["VECTOR_DB_PATH"]  = str(root / "data" / "vectors")
os.environ["FINGERPRINT_DIR"] = str(root / "data" / "fingerprints")

os.environ.setdefault("EMBEDDING_MODEL",        "NeuML/pubmedbert-base-embeddings")
os.environ.setdefault("HF_HUB_OFFLINE",         "1")
os.environ.setdefault("CUDA_VISIBLE_DEVICES",   "-1")
os.environ.setdefault("OMP_NUM_THREADS",        "1")
os.environ.setdefault("MKL_NUM_THREADS",        "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

sys.path.insert(0, str(root))
from src.mcp_server import mcp
mcp.run()
