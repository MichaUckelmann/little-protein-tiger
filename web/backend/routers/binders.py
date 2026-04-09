"""
Binders router.

Endpoints:
  POST /projects/{id}/campaigns               Create a binder campaign
  GET  /projects/{id}/campaigns               List campaigns for a project
  GET  /campaigns/{id}                        Get campaign + binders + measurements
  POST /campaigns/{id}/upload-csv             Parse CSV → create Binder rows
  POST /binders/{id}/upload-cif              Store CIF file for a binder
  GET  /binders/{id}/cif                     Serve CIF file
  GET  /binders/{id}/optimizer-report        Serve optimizer report markdown
  POST /binders/{id}/measurements            Add a BinderMeasurement
  DELETE /binders/{id}/measurements/{mid}   Delete a measurement
  POST /binders/{id}/run-optimizer           Queue binder-optimizer Celery task
  POST /binders/{id}/add-mutation            Create a manual child binder
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from web.backend.auth import get_current_user_id
from web.backend.db import get_session
from web.backend.models_db import Binder, BinderCampaign, BinderMeasurement, Project

router = APIRouter(tags=["binders"])

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_BINDER_DATA_DIR = _ROOT / "data" / "binders"


def _model_dict(obj) -> dict:
    """Serialize any SQLModel table model to a plain dict."""
    result: dict = {}
    for col in obj.__table__.columns:
        val = getattr(obj, col.key, None)
        if val is not None and hasattr(val, "isoformat"):
            val = val.isoformat()
        result[col.key] = val
    return result


def _get_owned_campaign(campaign_id: int, user_id: int, session: Session) -> BinderCampaign:
    campaign = session.get(BinderCampaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    project = session.get(Project, campaign.project_id)
    if not project or project.owner_id != user_id:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def _get_owned_binder(binder_id: int, user_id: int, session: Session) -> tuple[Binder, BinderCampaign]:
    binder = session.get(Binder, binder_id)
    if not binder:
        raise HTTPException(status_code=404, detail="Binder not found")
    campaign = _get_owned_campaign(binder.campaign_id, user_id, session)
    return binder, campaign


def _binder_with_measurements(binder: Binder, session: Session) -> dict:
    d = _model_dict(binder)
    measurements = session.exec(
        select(BinderMeasurement).where(BinderMeasurement.binder_id == binder.id)
    ).all()
    d["measurements"] = [_model_dict(m) for m in measurements]
    return d


# ---------------------------------------------------------------------------
# Campaign endpoints
# ---------------------------------------------------------------------------

class CampaignCreate(BaseModel):
    name: str
    target_name: Optional[str] = None


@router.post("/projects/{project_id}/campaigns", status_code=201)
def create_campaign(
    project_id: int,
    body: CampaignCreate,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project or project.owner_id != user_id:
        raise HTTPException(status_code=404, detail="Project not found")
    campaign = BinderCampaign(
        project_id=project_id,
        user_id=user_id,
        name=body.name,
        target_name=body.target_name,
    )
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return _model_dict(campaign)


@router.get("/projects/{project_id}/campaigns")
def list_campaigns(
    project_id: int,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project or project.owner_id != user_id:
        raise HTTPException(status_code=404, detail="Project not found")
    campaigns = session.exec(
        select(BinderCampaign).where(BinderCampaign.project_id == project_id)
    ).all()
    return [_model_dict(c) for c in campaigns]


@router.get("/campaigns/{campaign_id}")
def get_campaign(
    campaign_id: int,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    campaign = _get_owned_campaign(campaign_id, user_id, session)
    binders = session.exec(
        select(Binder).where(Binder.campaign_id == campaign_id)
    ).all()
    result = _model_dict(campaign)
    result["binders"] = [_binder_with_measurements(b, session) for b in binders]
    return result


@router.post("/campaigns/{campaign_id}/upload-csv", status_code=201)
async def upload_csv(
    campaign_id: int,
    file: UploadFile = File(...),
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    campaign = _get_owned_campaign(campaign_id, user_id, session)

    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="CSV is empty")

    # Validate required columns
    first = rows[0]
    if "designed_sequence" not in first:
        raise HTTPException(
            status_code=400,
            detail="CSV must contain a 'designed_sequence' column",
        )

    created = []
    for i, row in enumerate(rows):
        seq = row.get("designed_sequence", "").strip()
        if not seq:
            continue

        name = (
            row.get("id") or row.get("name") or row.get("design_id") or str(i + 1)
        )
        name = str(name).strip()

        def _float(key: str) -> Optional[float]:
            v = row.get(key, "").strip()
            try:
                return float(v) if v else None
            except ValueError:
                return None

        binder = Binder(
            campaign_id=campaign_id,
            name=name,
            sequence=seq,
            source="csv_import",
            design_to_target_iptm=_float("design_to_target_iptm"),
            min_design_to_target_pae=_float("min_design_to_target_pae"),
            filter_rmsd=_float("filter_rmsd"),
        )
        session.add(binder)
        created.append(binder)

    campaign.csv_filename = file.filename
    session.add(campaign)
    session.commit()
    for b in created:
        session.refresh(b)

    return {"created": len(created)}


# ---------------------------------------------------------------------------
# Binder file endpoints
# ---------------------------------------------------------------------------

@router.post("/binders/{binder_id}/upload-cif", status_code=200)
async def upload_cif(
    binder_id: int,
    file: UploadFile = File(...),
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    binder, _ = _get_owned_binder(binder_id, user_id, session)

    _BINDER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    dest = _BINDER_DATA_DIR / f"{binder_id}.cif"
    content = await file.read()
    dest.write_bytes(content)

    binder.cif_path = str(dest.relative_to(_ROOT))
    session.add(binder)
    session.commit()
    return {"cif_path": binder.cif_path}


@router.get("/binders/{binder_id}/cif")
def get_cif(
    binder_id: int,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    binder, _ = _get_owned_binder(binder_id, user_id, session)
    if not binder.cif_path:
        raise HTTPException(status_code=404, detail="No CIF uploaded for this binder")
    path = _ROOT / binder.cif_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="CIF file not found on disk")
    return FileResponse(str(path), media_type="chemical/x-mmcif", filename=f"binder_{binder_id}.cif")


@router.get("/binders/{binder_id}/optimizer-report")
def get_optimizer_report(
    binder_id: int,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    binder, _ = _get_owned_binder(binder_id, user_id, session)
    if not binder.optimizer_report_path:
        raise HTTPException(status_code=404, detail="No optimizer report for this binder")
    path = _ROOT / binder.optimizer_report_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="Optimizer report not found on disk")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Measurement endpoints
# ---------------------------------------------------------------------------

class MeasurementCreate(BaseModel):
    method: str
    kd_molar: Optional[float] = None
    ki_molar: Optional[float] = None
    notes: Optional[str] = None


@router.post("/binders/{binder_id}/measurements", status_code=201)
def add_measurement(
    binder_id: int,
    body: MeasurementCreate,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    _get_owned_binder(binder_id, user_id, session)
    m = BinderMeasurement(
        binder_id=binder_id,
        method=body.method,
        kd_molar=body.kd_molar,
        ki_molar=body.ki_molar,
        notes=body.notes,
        uploaded_by=user_id,
    )
    session.add(m)
    session.commit()
    session.refresh(m)
    return _model_dict(m)


@router.delete("/binders/{binder_id}/measurements/{measurement_id}", status_code=204)
def delete_measurement(
    binder_id: int,
    measurement_id: int,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    _get_owned_binder(binder_id, user_id, session)
    m = session.get(BinderMeasurement, measurement_id)
    if not m or m.binder_id != binder_id:
        raise HTTPException(status_code=404, detail="Measurement not found")
    session.delete(m)
    session.commit()


# ---------------------------------------------------------------------------
# Optimizer endpoint
# ---------------------------------------------------------------------------

@router.post("/binders/{binder_id}/run-optimizer", status_code=202)
def run_optimizer(
    binder_id: int,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    binder, _ = _get_owned_binder(binder_id, user_id, session)
    if not binder.cif_path:
        raise HTTPException(
            status_code=400,
            detail="Upload a CIF file for this binder before running the optimizer",
        )
    cif_abs = str(_ROOT / binder.cif_path)
    from web.backend.tasks import run_binder_optimizer_task
    run_binder_optimizer_task.delay(binder_id, user_id, cif_abs)
    return {"status": "queued"}


# ---------------------------------------------------------------------------
# Manual mutation endpoint
# ---------------------------------------------------------------------------

class MutationCreate(BaseModel):
    sequence: str
    mutation_label: Optional[str] = None
    name: Optional[str] = None
    notes: Optional[str] = None


@router.post("/binders/{binder_id}/add-mutation", status_code=201)
def add_mutation(
    binder_id: int,
    body: MutationCreate,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    binder, _ = _get_owned_binder(binder_id, user_id, session)
    name = body.name or (
        f"{binder.name}_{body.mutation_label}" if body.mutation_label else f"{binder.name}_mut"
    )
    child = Binder(
        campaign_id=binder.campaign_id,
        parent_id=binder_id,
        name=name,
        sequence=body.sequence,
        mutation_label=body.mutation_label,
        source="manual",
        notes=body.notes,
    )
    session.add(child)
    session.commit()
    session.refresh(child)
    return _model_dict(child)
