#!/usr/bin/env python3
"""
Launcher for the structure-tools MCP server.
Mirrors the pattern of launch_mcp.py — re-execs into the venv if needed.
"""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent

for candidate in [
    root / ".venv" / "Scripts" / "python.exe",
    root / ".venv" / "bin" / "python3",
    root / ".venv" / "bin" / "python",
]:
    if candidate.exists():
        venv_python = candidate
        break
else:
    sys.exit(f"ERROR: venv Python not found under {root / '.venv'}")

if Path(sys.executable).resolve() != venv_python.resolve():
    import subprocess
    sys.exit(subprocess.run(
        [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:]
    ).returncode)

sys.path.insert(0, str(root))
from src.structure_tools_server import mcp
mcp.run()
