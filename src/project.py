"""Persistent project directory + manifest for little-protein-tiger runs.

A *project* groups one or more pipeline runs ("rounds") under
``projects/<slug>/`` with a human-diffable ``manifest.json`` as the filesystem
source of truth for stage and checkpoint state, plus a ``shared/`` asset store
and per-round run directories.

This is the persistence backbone for the iterative, human-in-the-loop enzyme
workflow (substrate -> theozyme -> grafting -> design -> validate, with external
ORCA/GPU steps and design-pilot iterations modelled as rounds), and it also
hosts ordinary PPI runs so CLI and web share one layout.

Authoritative-state split (so we don't fight the existing web layer):
  - ``manifest.json``  -> filesystem / CLI source of truth (stages, checkpoints,
    artifact pointers, rounds).
  - ``web.db`` Run row -> web UI / queue state (status, ownership, celery id),
    unchanged. The web TrackedRunner mirrors stage progress into the manifest.

Layout::

    projects/<slug>/
      manifest.json
      shared/
        structures/   ligands/   orca/
      runs/<run_id>/
        <stage files written by the pipeline, e.g. 00_pathway.md>
        enzyme/      (enzyme-track artifacts: theozyme, TS, motif, specs)
        scratch/     (disposable intermediates)

Concurrency: every mutating method reloads the manifest from disk, applies its
change, and writes atomically (tmp file + ``os.replace``). That keeps the CLI
and a web TrackedRunner from clobbering each other's updates for the common case
where they touch different keys.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

MANIFEST_SCHEMA_VERSION = "1.0"

# Default project root: <repo>/projects. _ROOT mirrors the convention used in
# the other src/ modules (this file lives at <repo>/src/project.py).
_ROOT = Path(__file__).resolve().parent.parent
_PROJECTS_DIR = _ROOT / "projects"

# Stage status vocabulary (kept loose on purpose — callers may add their own).
STAGE_STATUSES = (
    "pending", "running", "complete", "awaiting_user", "skipped", "failed",
)
SHARED_KINDS = ("structures", "ligands", "orca")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(text: str, max_len: int = 48) -> str:
    """Filesystem-safe slug, matching pipeline_runner's run-dir convention."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "")[:max_len]).strip("_").lower()
    return s or "project"


def round_run_id(n: int) -> str:
    return f"round-{n}"


class Project:
    """A project directory backed by ``manifest.json``."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.manifest_path = self.root / "manifest.json"
        self._manifest: dict[str, Any] = {}
        if self.manifest_path.exists():
            self._reload()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def projects_dir(cls, root: Optional[Path] = None) -> Path:
        return (Path(root) if root else _PROJECTS_DIR)

    @classmethod
    def path_for(cls, slug: str, root: Optional[Path] = None) -> Path:
        return cls.projects_dir(root) / slugify(slug)

    @classmethod
    def exists(cls, slug: str, root: Optional[Path] = None) -> bool:
        return (cls.path_for(slug, root) / "manifest.json").exists()

    @classmethod
    def create(
        cls,
        slug: str,
        name: Optional[str] = None,
        query: str = "",
        workflow: str = "ppi",
        root: Optional[Path] = None,
    ) -> "Project":
        """Create (or load+update) a project. Idempotent."""
        slug = slugify(slug)
        proj_root = cls.path_for(slug, root)
        if (proj_root / "manifest.json").exists():
            proj = cls(proj_root)
            # Fill in any newly-supplied metadata without clobbering existing.
            changed = False
            if name and not proj._manifest.get("name"):
                proj._manifest["name"] = name
                changed = True
            if query and not proj._manifest.get("query"):
                proj._manifest["query"] = query
                changed = True
            if changed:
                proj._save()
            return proj

        proj_root.mkdir(parents=True, exist_ok=True)
        for kind in SHARED_KINDS:
            (proj_root / "shared" / kind).mkdir(parents=True, exist_ok=True)
        (proj_root / "runs").mkdir(parents=True, exist_ok=True)

        proj = cls(proj_root)
        proj._manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "slug": slug,
            "name": name or slug,
            "query": query,
            "workflow": workflow,
            "created_at": _now(),
            "updated_at": _now(),
            "rounds": [],
            "checkpoints": [],
            "shared_assets": {k: [] for k in SHARED_KINDS},
        }
        proj._save()
        return proj

    @classmethod
    def load(cls, slug: str, root: Optional[Path] = None) -> "Project":
        proj_root = cls.path_for(slug, root)
        if not (proj_root / "manifest.json").exists():
            raise FileNotFoundError(f"No project manifest at {proj_root / 'manifest.json'}")
        return cls(proj_root)

    # ------------------------------------------------------------------
    # Manifest IO (atomic + reload-before-mutate)
    # ------------------------------------------------------------------
    def _reload(self) -> None:
        self._manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self._manifest["updated_at"] = _now()
        self.root.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp in the same dir, then os.replace.
        fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=".manifest.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.manifest_path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    @property
    def manifest(self) -> dict[str, Any]:
        return self._manifest

    @property
    def slug(self) -> str:
        return self._manifest.get("slug", self.root.name)

    # ------------------------------------------------------------------
    # Directories
    # ------------------------------------------------------------------
    def shared_dir(self, kind: str) -> Path:
        if kind not in SHARED_KINDS:
            raise ValueError(f"Unknown shared asset kind {kind!r}; expected one of {SHARED_KINDS}")
        d = self.root / "shared" / kind
        d.mkdir(parents=True, exist_ok=True)
        return d

    def run_dir(self, run_id: str | int) -> Path:
        rid = round_run_id(run_id) if isinstance(run_id, int) else run_id
        d = self.root / "runs" / rid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def enzyme_dir(self, run_id: str | int) -> Path:
        d = self.run_dir(run_id) / "enzyme"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def scratch_dir(self, run_id: str | int) -> Path:
        d = self.run_dir(run_id) / "scratch"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------------
    # Rounds
    # ------------------------------------------------------------------
    def _round_entry(self, run_id: str) -> Optional[dict]:
        for r in self._manifest.get("rounds", []):
            if r.get("run_id") == run_id:
                return r
        return None

    def new_round(self, note: str = "", parent_round: Optional[int] = None) -> dict:
        """Create the next round + its run dir tree; return the round entry."""
        self._reload()
        n = len(self._manifest.get("rounds", [])) + 1
        run_id = round_run_id(n)
        entry = {
            "round": n,
            "run_id": run_id,
            "run_dir": f"runs/{run_id}",
            "created_at": _now(),
            "parent_round": parent_round,
            "note": note,
            "stages": {},
        }
        self._manifest.setdefault("rounds", []).append(entry)
        self._save()
        # Materialize the directory tree.
        self.run_dir(run_id)
        self.enzyme_dir(run_id)
        self.scratch_dir(run_id)
        return entry

    def latest_round(self) -> Optional[dict]:
        rounds = self._manifest.get("rounds", [])
        return rounds[-1] if rounds else None

    def ensure_round(self, note: str = "") -> dict:
        """Return the latest round, creating round-1 if none exists."""
        return self.latest_round() or self.new_round(note=note)

    # ------------------------------------------------------------------
    # Stage + checkpoint state
    # ------------------------------------------------------------------
    def update_stage(
        self,
        run_id: str | int,
        stage: str,
        status: str,
        artifacts: Optional[list] = None,
        handoff: Optional[dict] = None,
        note: Optional[str] = None,
    ) -> None:
        """Upsert a stage record on a round. Merges artifacts/handoff if given."""
        rid = round_run_id(run_id) if isinstance(run_id, int) else run_id
        self._reload()
        entry = self._round_entry(rid)
        if entry is None:
            raise KeyError(f"No round {rid!r} in project {self.slug!r}")
        stages = entry.setdefault("stages", {})
        rec = stages.setdefault(stage, {})
        rec["status"] = status
        rec["updated_at"] = _now()
        if artifacts is not None:
            rec["artifacts"] = [self._relpath(a) for a in artifacts]
        if handoff is not None:
            rec["handoff"] = handoff
        if note is not None:
            rec["note"] = note
        self._save()

    def get_stage(self, run_id: str | int, stage: str) -> Optional[dict]:
        rid = round_run_id(run_id) if isinstance(run_id, int) else run_id
        entry = self._round_entry(rid)
        if not entry:
            return None
        return entry.get("stages", {}).get(stage)

    def set_checkpoint(
        self,
        checkpoint_id: str,
        run_id: str | int,
        stage: str,
        kind: str,
        payload: Optional[dict] = None,
    ) -> dict:
        """Open (or replace) a pending checkpoint. kind: external_step|choice|gate."""
        rid = round_run_id(run_id) if isinstance(run_id, int) else run_id
        self._reload()
        cps = self._manifest.setdefault("checkpoints", [])
        # Replace any existing pending checkpoint with the same id+run.
        cps[:] = [c for c in cps if not (c.get("id") == checkpoint_id and c.get("run_id") == rid and c.get("status") == "pending")]
        cp = {
            "id": checkpoint_id,
            "run_id": rid,
            "stage": stage,
            "kind": kind,
            "status": "pending",
            "payload": payload or {},
            "created_at": _now(),
            "resolved_at": None,
        }
        cps.append(cp)
        self._save()
        return cp

    def resolve_checkpoint(self, checkpoint_id: str, run_id: str | int, resolution: Optional[dict] = None) -> None:
        rid = round_run_id(run_id) if isinstance(run_id, int) else run_id
        self._reload()
        for c in self._manifest.get("checkpoints", []):
            if c.get("id") == checkpoint_id and c.get("run_id") == rid and c.get("status") == "pending":
                c["status"] = "resolved"
                c["resolved_at"] = _now()
                if resolution is not None:
                    c["resolution"] = resolution
        self._save()

    def open_checkpoints(self, run_id: Optional[str | int] = None) -> list[dict]:
        rid = round_run_id(run_id) if isinstance(run_id, int) else run_id
        out = [c for c in self._manifest.get("checkpoints", []) if c.get("status") == "pending"]
        if rid is not None:
            out = [c for c in out if c.get("run_id") == rid]
        return out

    def add_shared_asset(self, kind: str, path: str | Path) -> None:
        if kind not in SHARED_KINDS:
            raise ValueError(f"Unknown shared asset kind {kind!r}")
        self._reload()
        rel = self._relpath(path)
        bucket = self._manifest.setdefault("shared_assets", {}).setdefault(kind, [])
        if rel not in bucket:
            bucket.append(rel)
            self._save()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _relpath(self, p: str | Path) -> str:
        """Store artifact paths relative to the project root when possible."""
        p = Path(p)
        try:
            return p.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return p.as_posix()
