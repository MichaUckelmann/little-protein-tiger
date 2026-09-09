# Migrating LPT to a new machine

Moved out of `README.md`, which had grown to serve four audiences at once.
Nothing here changed in the move except the heading levels.


## What to transfer

| Item | Size | Notes |
|------|------|-------|
| Git repo | small | `git clone` or copy |
| `data/literature.db` | ~15 MB | full paper catalog |
| `data/fingerprints/` | ~10 MB | curated JSON fingerprints |
| `data/vectors/` | ~10 MB | LanceDB semantic index |
| `data/pdfs/` | ~11 GB | only needed for re-curation |
| `.env` | — | recreate manually (never committed) |

If the new machine is **query/MCP use only**, skip `data/pdfs/` — the MCP server only needs the fingerprints and vectors.

## Steps

**1. Clone the repo and install dependencies**
```bash
git clone <repo> little_protein_tiger
cd little_protein_tiger
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/macOS
pip install -e ".[corpus,dev]"    # drop `corpus,` if you don't want the
                                  # literature track
python scripts/fetch_reference_data.py   # required by ppi/binder
```

**2. Copy data directories**

Transfer `data/literature.db`, `data/fingerprints/`, and `data/vectors/` to the same paths on the new machine. Optionally add `data/pdfs/` if you want curation capability.

**3. Recreate `.env`**

Copy `.env.example` to `.env` and fill in your API keys. If you use the
binder/design track, also fill in the `LPT_BOLTZGEN_EXECUTABLE` /
`LPT_PYROSETTA_PYTHON` / `LPT_FOUNDRY_ROOT` / `LPT_CLUSTER_*` vars for
wherever those tools live on the new machine — see
[Environment setup](environment_setup.md). `config.yaml` itself needs
no path edits; it never carries machine-specific values.

**4. Regenerate the MCP configs**

`.mcp.json` holds absolute, machine-specific paths, so it is not tracked in
git — a fresh clone has none. Generate one for this checkout:

```bash
python scripts/setup_mcp_json.py     # backs up any existing file to .mcp.json.bak
```

That writes both servers with the current venv's Python. The launchers
(`scripts/launch_mcp.py`, `scripts/launch_structure_tools.py`) derive every
data path from their own location; what is machine-specific is the absolute
`command` and `args`, which is exactly what the generator fills in. To write
it by hand instead:

```json
{
  "mcpServers": {
    "literature-db": {
      "command": "/path/to/little_protein_tiger/.venv/Scripts/python.exe",
      "args": ["/path/to/little_protein_tiger/scripts/launch_mcp.py"]
    },
    "structure-tools": {
      "command": "/path/to/little_protein_tiger/.venv/Scripts/python.exe",
      "args": ["/path/to/little_protein_tiger/scripts/launch_structure_tools.py"]
    }
  }
}
```

In **`%APPDATA%\Claude\claude_desktop_config.json`** (Claude Desktop, Windows), **`~/Library/Application Support/Claude/claude_desktop_config.json`** (macOS) or **`~/.config/Claude/claude_desktop_config.json`** (Linux):
```json
"literature-db": {
  "command": "/path/to/little_protein_tiger/.venv/Scripts/python.exe",
  "args": ["/path/to/little_protein_tiger/scripts/launch_mcp.py"]
},
"structure-tools": {
  "command": "/path/to/little_protein_tiger/.venv/Scripts/python.exe",
  "args": ["/path/to/little_protein_tiger/scripts/launch_structure_tools.py"]
}
```

Register both, or the 8 structure tools listed above are silently missing.

> Use absolute paths for both `command` and `args`. Claude Desktop does not reliably honour `cwd` on Windows — relative paths resolve to `C:\Windows\System32`. Point `command` directly at the venv Python so the launcher's re-exec logic is a no-op.

**5. Handle the embedding model cache**

`scripts/launch_mcp.py` sets `HF_HUB_OFFLINE=1` itself (it is not in
`.mcp.json`, so there is nothing there to edit), meaning the server needs
`NeuML/pubmedbert-base-embeddings` already in the HuggingFace cache. Two
options:
- **Copy the cache**: transfer `~/.cache/huggingface/` from the old machine
- **Warm it once**, which is what `doctor.py` recommends too:
  ```bash
  python -c "from sentence_transformers import SentenceTransformer as S; S('NeuML/pubmedbert-base-embeddings')"
  ```

**6. Verify**

```bash
python scripts/doctor.py --track literature
```

Restart Claude Desktop after updating its config.

---

