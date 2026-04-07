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
from fastapi.encoders import jsonable_encoder
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


def _run_dict(run: Run) -> dict:
    """Serialize a Run to a plain dict via SQLAlchemy columns.

    Bypasses SQLModel/Pydantic v2 serialization which returns {} for table
    models in some versions.  Datetimes are ISO-formatted strings.
    """
    result: dict = {}
    for col in run.__table__.columns:
        val = getattr(run, col.key, None)
        if val is not None and hasattr(val, "isoformat"):
            val = val.isoformat()
        result[col.key] = val
    return result


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
    # Per-stage model overrides, e.g. {"pathway": "claude-haiku-4-5-20251001", "structure": "claude-sonnet-4-6"}
    # Omit or set to null for uniform model_id across all stages.
    stage_models: Optional[dict[str, str]] = None
    # Enable Claude extended thinking on the structure stage (off by default)
    extended_thinking: bool = False
    # When False (default for new runs), pipeline pauses at pathway_choice and
    # structure_choice so the user can review and confirm before continuing.
    # Set to True for fully-automatic end-to-end execution.
    auto_mode: bool = False


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
        stage_models_json=json.dumps(body.stage_models) if body.stage_models else None,
        extended_thinking=body.extended_thinking,
        auto_mode=body.auto_mode,
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

    return _run_dict(run)


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
        "run": _run_dict(run),
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
                            "pause_point": run.pause_point,
                            "pathway_choices_json": run.pathway_choices_json,
                            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                        }),
                    }
                    if run.status in ("COMPLETE", "FAILED", "BLOCKED", "PAUSED"):
                        break
            await asyncio.sleep(3)

    return EventSourceResponse(_events())


# ---------------------------------------------------------------------------
# Resume a paused run
# ---------------------------------------------------------------------------

class ResumeRunBody(BaseModel):
    chosen_target_index: Optional[int] = None  # for pathway_choice pause
    next_step: Optional[str] = None            # for structure_choice pause: "literature_and_design" | "design_only" | "stop"


@router.post("/runs/{run_id}/resume", status_code=202)
def resume_run(
    run_id: int,
    body: ResumeRunBody,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    """Resume a PAUSED run after the user makes a choice."""
    run = _get_owned_run(run_id, user_id, session)

    if run.status != "PAUSED":
        raise HTTPException(status_code=409, detail=f"Run is not paused (status={run.status})")

    if run.pause_point == "pathway_choice":
        if body.chosen_target_index is None:
            raise HTTPException(status_code=422, detail="chosen_target_index is required for pathway_choice")
        if not run.pathway_choices_json:
            raise HTTPException(status_code=409, detail="No pathway choices available on this run")

        choices = json.loads(run.pathway_choices_json)
        idx = body.chosen_target_index
        if idx < 0 or idx >= len(choices):
            raise HTTPException(status_code=422, detail=f"chosen_target_index {idx} out of range (0–{len(choices)-1})")

        chosen = choices[idx]
        pdb_ids = chosen.get("pdb_ids", [])
        if not pdb_ids:
            raise HTTPException(status_code=422, detail="Chosen target has no PDB ID — cannot continue")

        run.pdb_id = pdb_ids[0]
        run.target_complex = chosen.get("complex")
        # Do NOT clear pause_point here — resume_pipeline_task reads it from DB
        # and clears it itself when setting status="RUNNING"
        run.status = "QUEUED"
        session.add(run)
        session.commit()
        session.refresh(run)

        from web.backend.tasks import resume_pipeline_task
        task = resume_pipeline_task.delay(run.id)
        run.celery_task_id = task.id
        session.add(run)
        session.commit()
        session.refresh(run)

    elif run.pause_point == "structure_choice":
        if body.next_step not in ("literature_and_design", "design_only", "stop"):
            raise HTTPException(
                status_code=422,
                detail="next_step must be 'literature_and_design', 'design_only', or 'stop'",
            )

        if body.next_step == "stop":
            run.status = "COMPLETE"
            run.completed_at = datetime.utcnow()
            run.pause_point = None
            session.add(run)
            session.commit()
            session.refresh(run)
        else:
            run.structure_next_step = body.next_step
            # Do NOT clear pause_point here — resume_pipeline_task reads it
            run.status = "QUEUED"
            session.add(run)
            session.commit()
            session.refresh(run)

            from web.backend.tasks import resume_pipeline_task
            task = resume_pipeline_task.delay(run.id)
            run.celery_task_id = task.id
            session.add(run)
            session.commit()
            session.refresh(run)

    else:
        raise HTTPException(status_code=409, detail=f"Unknown pause_point: {run.pause_point!r}")

    return _run_dict(run)


# ---------------------------------------------------------------------------
# Retry a failed run
# ---------------------------------------------------------------------------

_STAGE_START_ORDER = ["pathway", "structure", "literature", "design"]


@router.post("/runs/{run_id}/retry", status_code=202)
def retry_run(
    run_id: int,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    """Retry a FAILED or BLOCKED run from the stage where it failed."""
    run = _get_owned_run(run_id, user_id, session)

    if run.status not in ("FAILED", "BLOCKED"):
        raise HTTPException(status_code=409, detail=f"Run is not failed (status={run.status})")

    # Determine start stage: use stage_current if known, else "pathway"
    stage = run.stage_current or "pathway"

    # If the run already has a pdb_id set (user had selected a target before
    # the failure) but stage_current is "pathway", start from structure instead
    # so we don't throw away the user's target selection.
    if stage == "pathway" and run.pdb_id:
        stage = "structure"

    if stage not in _STAGE_START_ORDER:
        stage = "pathway"

    run.status = "QUEUED"
    run.error = None
    run.completed_at = None
    session.add(run)
    session.commit()
    session.refresh(run)

    from web.backend.tasks import retry_run_task
    task = retry_run_task.delay(run.id, stage)
    run.celery_task_id = task.id
    session.add(run)
    session.commit()
    session.refresh(run)

    return _run_dict(run)


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
