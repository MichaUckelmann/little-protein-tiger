#!/usr/bin/env python3
"""
Generate a `.mcp.json` correct for THIS machine/checkout.

`.mcp.json` hardcodes absolute paths to the two MCP server launchers
(`scripts/launch_mcp.py`, `scripts/launch_structure_tools.py`) and the venv
Python that runs them. Those launchers already compute everything else
(project root, data paths) relative to `__file__` at runtime — see the
docstrings in both scripts — but the `.mcp.json` entry point itself has to
be an absolute path, so it goes stale the moment the repo is checked out
onto a new machine or moved.

This script regenerates `.mcp.json` in place, pointing at:
  - the venv Python at `<repo>/.venv/bin/python3` (computed from this file's
    own location, not `sys.executable`, so it's correct however the script
    was invoked — bare `python3 setup_mcp_json.py`, an activated venv, or
    otherwise)
  - the two launcher scripts, also resolved from this file's location

Usage:
    .venv/bin/python3 scripts/setup_mcp_json.py

Existing `.mcp.json` (if any) is backed up to `.mcp.json.bak` before being
overwritten.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MCP_JSON = ROOT / ".mcp.json"
BACKUP = ROOT / ".mcp.json.bak"


def venv_python() -> Path:
    """Resolve the repo's venv interpreter, Unix or Windows layout."""
    for candidate in [
        ROOT / ".venv" / "bin" / "python3",     # Unix/Mac
        ROOT / ".venv" / "bin" / "python",      # Unix/Mac fallback
        ROOT / ".venv" / "Scripts" / "python.exe",  # Windows
    ]:
        if candidate.exists():
            return candidate
    sys.exit(
        f"ERROR: no venv Python found under {ROOT / '.venv'} — "
        "create the venv and install dependencies first."
    )


def build_mcp_json() -> dict:
    python = str(venv_python())
    return {
        "mcpServers": {
            "literature-db": {
                "command": python,
                "args": [str(ROOT / "scripts" / "launch_mcp.py")],
            },
            "structure-tools": {
                "command": python,
                "args": [str(ROOT / "scripts" / "launch_structure_tools.py")],
            },
        }
    }


def main() -> int:
    # There was no argparse at all, so `--help` fell through to the write —
    # probing for a dry-run flag performed the side effect it was probing for.
    ap = argparse.ArgumentParser(
        description="Write .mcp.json for this checkout's venv and launchers.")
    ap.add_argument("--dry-run", "--print", dest="dry_run", action="store_true",
                    help="print the config and write nothing")
    args = ap.parse_args()

    new_config = build_mcp_json()

    if args.dry_run:
        print(json.dumps(new_config, indent=2))
        print(f"\n(dry run — {MCP_JSON} not written)")
        return 0

    if MCP_JSON.exists():
        shutil.copy2(MCP_JSON, BACKUP)
        print(f"backed up existing {MCP_JSON} -> {BACKUP}")

    MCP_JSON.write_text(json.dumps(new_config, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {MCP_JSON} for this checkout:")
    print(json.dumps(new_config, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
