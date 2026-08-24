# PyRosetta setup

Operational notes for using PyRosetta from LittleProteinTiger (or any other
Python project on the same machine). The paths below are what one contributor's
workstation happened to use — adjust them to wherever PyRosetta actually lives
on yours, and set `design.pyrosetta.python_executable` in `config.yaml`
accordingly. The mistakes and fixes here generalise to any project that needs
PyRosetta from a venv whose Python version doesn't match the PyRosetta build.

## TL;DR

PyRosetta needs its **own conda env**, separate from this repo's venv, because
the installed build is ABI-pinned to a specific Python version. Don't try to
import it from your project's venv. Call it as a subprocess.

```bash
# Example interpreter path (yours will differ):
/path/to/miniconda3/envs/pyrosetta/bin/python

# Quick sanity check (any project, any venv):
$ /path/to/miniconda3/envs/pyrosetta/bin/python -c \
    "import pyrosetta; pyrosetta.init('-mute all'); print('OK')"
```

## The Python-version trap

A PyRosetta source-distribution install's `rosetta.so` is compiled against one
specific Python minor version (e.g. 3.11). If your project's venv is on a
different minor version (3.12, say — the current `uv`/Ubuntu default), then:

```python
sys.path.insert(0, '/path/to/pyrosetta')
import pyrosetta
# ImportError: Python version mismatch: module was compiled for Python 3.11,
# but the interpreter version is incompatible: 3.12.x
```

`sys.path` injection **does not work** across Python minor versions for
compiled extensions. Don't try. Use a dedicated conda env pinned to the
matching Python version instead, and call it via subprocess (below).

## The subprocess-worker pattern

For any project that needs PyRosetta:

1. Write a small worker script (Python file) that does only the PyRosetta
   work. Make it read its inputs from stdin (JSON) and emit results on
   stdout (JSON).
2. From your project, subprocess that worker under the PyRosetta conda env's
   python. Done.

### Worker template

```python
#!/usr/bin/env python
"""Tiny PyRosetta worker. Runs under the pyrosetta conda env."""
import contextlib
import json
import sys
import pyrosetta

def main():
    spec = json.load(sys.stdin)

    # IMPORTANT: pyrosetta.init() prints its license banner to stdout.
    # Redirect to stderr so it doesn't corrupt the JSON we emit.
    with contextlib.redirect_stdout(sys.stderr):
        pyrosetta.init(spec.get("init_flags", "-mute all -ignore_unrecognized_res -load_PDB_components false"))
        pose = pyrosetta.pose_from_file(spec["cif_path"])

    # ... do work, build a result dict ...
    result = {"status": "ok", "n_residues": pose.total_residue()}
    json.dump(result, sys.stdout)
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

### Caller template (from your project's venv)

```python
import json
import subprocess

PYROSETTA_PY = "/path/to/miniconda3/envs/pyrosetta/bin/python"
WORKER = "/path/to/your/worker.py"

spec = {"cif_path": "/path/to/complex.cif"}
proc = subprocess.run(
    [PYROSETTA_PY, WORKER],
    input=json.dumps(spec),
    capture_output=True,
    text=True,
    timeout=120,
    check=True,
)
result = json.loads(proc.stdout)
```

## Gotchas

- **`pyrosetta.init()` writes to stdout.** Even with `-mute all`, the license
  banner prints. If the worker emits JSON on stdout, wrap init in
  `contextlib.redirect_stdout(sys.stderr)`. Otherwise your caller gets garbage
  before the JSON.
- **Init is ~0.6s per process.** Cheap enough for batch-of-100 work via
  one-shot subprocesses. If you're doing >1000 calls per run, build a
  persistent worker (stdin loop, one init per process lifetime) rather than
  spawning a process per call.
- **Don't activate the conda env in your shell.** The env's `bin/python`
  has the right interpreter and `site-packages` baked in via its shebang and
  prefix — just call it directly. No `conda activate`, no `source`.
- **Standard init flags** that you almost always want:
  - `-mute all` — silence per-call logging spam.
  - `-ignore_unrecognized_res` — don't crash on ligands / non-canonical AAs
    in the input CIF/PDB.
  - `-load_PDB_components false` — skip the multi-second component dictionary
    load; the canonical residues are enough for most scoring work.
- **mmCIF support is fine** in current builds — `pose_from_file('foo.cif')`
  works. PDB also works.
- **Chain handling.** `pose.pdb_info().chain(i)` returns the original PDB
  chain ID for residue index `i`. `pose.pdb_info().number(i)` returns the
  author-numbered residue ID (mmCIF `auth_seq_id`). Use these for any
  cross-referencing against external residue lists.
- **Splitting a complex by chain:** `pose.split_by_chain()` returns a
  `utility_vector1<PoseOP>` indexed 1..n. Pick by `sub.pdb_info().chain(1)`.

## Common SASA recipe

```python
from pyrosetta.rosetta.core.scoring.sasa import SasaCalc

calc = SasaCalc()
total = calc.calculate(pose)           # float, total SASA
per_res = calc.get_residue_sasa()      # 1-indexed vector; per_res[i] for residue i
```

For bound-vs-unbound (e.g. how much surface a binder buries on a target):

```python
calc.calculate(complex_pose)
bound_per_res = calc.get_residue_sasa()

target_only = next(
    complex_pose.split_by_chain()[i]
    for i in range(1, complex_pose.num_chains() + 1)
    if complex_pose.split_by_chain()[i].pdb_info().chain(1) == "A"
)
calc.calculate(target_only)
unbound_per_res = calc.get_residue_sasa()

# Per-residue: unbound - bound = SASA buried by the binder partner.
```

## Reference implementations in this repo

- `scripts/_sasa_worker.py` — worker for hotspot SASA.
- `src/pyrosetta_sasa.py` — caller wrapper with a `HotspotSasaResult`
  dataclass and friendly error messages.

These are good starting points for copy/paste into a new project — change the
input/output schema and the SASA computation, keep the subprocess plumbing.

## Licensing

PyRosetta is free for academic / non-commercial use. Commercial use requires a
license: <license@uw.edu>. See the `LICENSE.PyRosetta.md` that ships alongside
your PyRosetta install for the full terms.
