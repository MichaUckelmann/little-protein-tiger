"""
Runs router.

Endpoints:
  POST /projects/{id}/runs         Submit a new pipeline run
  GET  /runs/{id}                  Get run details + stage file contents
  GET  /runs/{id}/status           SSE stream of live status updates
  GET  /runs/{id}/children         List optimizer child runs
  POST /runs/{id}/measurements     Add experimental measurement to a run
  GET  /runs/{id}/measurements     List measurements for a run
  GET  /runs/{id}/files/{filename} Download a run output file
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, select
from sse_starlette.sse import EventSourceResponse

from web.backend.auth import get_current_user_id
from web.backend.db import engine, get_session
from web.backend.models_db import AuditEvent, ExperimentalMeasurement, Project, Run

router = APIRouter(tags=["runs"])

_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Stage filenames in order — used when returning stage contents
_STAGE_FILES = [
    "00_pathway.md",
    "01_structure.md",
    "02_literature.md",
    "03_design_report.md",
    "04_binder_optimizer.md",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_owned_run(run_id: int, user_id: int, session: Session) -> Run:
    """Return Run only if user owns its project, else 404."""
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    project = session.get(Project, run.project_id)
    if not project or project.owner_id != user_id:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def _stage_contents(run_id: int) -> dict[str, str]:
    """Read all existing stage .md files for a run into a dict."""
    run_dir = _ROOT / "web" / "runs" / str(run_id)
    contents: dict[str, str] = {}
    for filename in _STAGE_FILES:
        p = run_dir / filename
        if p.exists():
            contents[filename] = p.read_text(encoding="utf-8")
    return contents


# ---------------------------------------------------------------------------
# Create run
# ---------------------------------------------------------------------------

class RunCreate(BaseModel):
    query: str
    pdb_id: Optional[str] = None
    provider: str = "claude"
    model_id: Optional[str] = None


@router.post("/projects/{project_id}/runs", status_code=201)
def create_run(
    project_id: int,
    body: RunCreate,
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project or project.owner_id != user_id:
        raise HTTPException(status_code=404, detail="Project not found")

    run = Run(
        project_id=project_id,
        user_id=user_id,
        query=body.query,
        pdb_id=body.pdb_id,
        provider=body.provider,
        model_id=body.model_id,
        status="QUEUED",
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    # Audit (log run_id only, never the query text)
    session.add(AuditEvent(
        user_id=user_id,
        event_type="run_created",
        resource_id=str(run.id),
        ip_addr=request.client.host if request.client else None,
    ))

    # Queue Celery task
    from web.backend.tasks import run_pipeline_task
    task = run_pipeline_task.delay(run.id)
    run.celery_task_id = task.id
    session.add(run)
    session.commit()
    session.refresh(run)

    return run


# ---------------------------------------------------------------------------
# Get run
# ---------------------------------------------------------------------------

@router.get("/runs/{run_id}")
def get_run(
    run_id: int,
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    run = _get_owned_run(run_id, user_id, session)

    session.add(AuditEvent(
        user_id=user_id,
        event_type="run_viewed",
        resource_id=str(run_id),
        ip_addr=request.client.host if request.client else None,
    ))
    session.commit()

    return {
        "run": run,
        "stages": _stage_contents(run_id),
    }


# ---------------------------------------------------------------------------
# SSE status stream
# ---------------------------------------------------------------------------

@router.get("/runs/{run_id}/status")
async def run_status_sse(
    run_id: int,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    """Stream run status updates as Server-Sent Events until terminal state."""
    # Verify access once before opening the stream
    _get_owned_run(run_id, user_id, session)

    async def _events():
        from sqlmodel import Session as SyncSession
        while True:
            with SyncSession(engine) as s:
                run = s.get(Run, run_id)
                if run:
                    yield {
                        "event": "status",
                        "data": json.dumps({
                            "status": run.status,
                            "stage_current": run.stage_current,
                            "pdb_id": run.pdb_id,
                            "target_complex": run.target_complex,
                            "go_recommendation": run.go_recommendation,
                            "error": run.error,
                            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                        }),
                    }
                    if run.status in ("COMPLETE", "FAILED", "BLOCKED"):
                        break
            await asyncio.sleep(3)

    return EventSourceResponse(_events())


# ---------------------------------------------------------------------------
# Child runs (optimizer lineage)
# ---------------------------------------------------------------------------

@router.get("/runs/{run_id}/children")
def get_child_runs(
    run_id: int,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    _get_owned_run(run_id, user_id, session)
    children = session.exec(
        select(Run).where(Run.parent_run_id == run_id).order_by(Run.round_number)
    ).all()
    return children


# ---------------------------------------------------------------------------
# Experimental measurements
# ---------------------------------------------------------------------------

class MeasurementCreate(BaseModel):
    design_name: str
    method: str  # SPR | ITC | FP | other
    kd_molar: Optional[float] = None
    ki_molar: Optional[float] = None
    notes: Optional[str] = None


@router.post("/runs/{run_id}/measurements", status_code=201)
def add_measurement(
    run_id: int,
    body: MeasurementCreate,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    _get_owned_run(run_id, user_id, session)

    measurement = ExperimentalMeasurement(
        run_id=run_id,
        uploaded_by=user_id,
        **body.model_dump(),
    )
    session.add(measurement)
    session.commit()
    session.refresh(measurement)
    return measurement


@router.get("/runs/{run_id}/measurements")
def list_measurements(
    run_id: int,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    _get_owned_run(run_id, user_id, session)
    measurements = session.exec(
        select(ExperimentalMeasurement)
        .where(ExperimentalMeasurement.run_id == run_id)
        .order_by(ExperimentalMeasurement.measured_at)
    ).all()
    return measurements


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------

_ALLOWED_FILES = set(_STAGE_FILES) | {"03_design_inputs"}


@router.get("/runs/{run_id}/files/{filename}")
def download_run_file(
    run_id: int,
    filename: str,
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    """Download a stage report or design input file."""
    _get_owned_run(run_id, user_id, session)

    # Restrict to known filenames to prevent path traversal
    if filename not in _ALLOWED_FILES and not filename.startswith("03_design_inputs/"):
        raise HTTPException(status_code=400, detail="File not allowed")
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    path = _ROOT / "web" / "runs" / str(run_id) / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    session.add(AuditEvent(
        user_id=user_id,
        event_type="data_exported",
        resource_id=f"run:{run_id}:{filename}",
        ip_addr=request.client.host if request.client else None,
    ))
    session.commit()

    media_type = "text/markdown" if filename.endswith(".md") else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=filename)
