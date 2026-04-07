"""
Projects router.

Endpoints:
  GET  /projects            List all projects owned by current user
  POST /projects            Create a new project
  GET  /projects/{id}       Get project details + run list
  DELETE /projects/{id}     Delete project (no runs allowed)
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from web.backend.auth import get_current_user_id
from web.backend.db import get_session
from web.backend.models_db import Project, Run

router = APIRouter(prefix="/projects", tags=["projects"])


def _model_dict(obj) -> dict:
    """Serialize a SQLModel table instance via SQLAlchemy columns."""
    result: dict = {}
    for col in obj.__table__.columns:
        val = getattr(obj, col.key, None)
        if val is not None and hasattr(val, "isoformat"):
            val = val.isoformat()
        result[col.key] = val
    return result


class ProjectCreate(BaseModel):
    name: str


@router.get("")
def list_projects(
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    projects = session.exec(
        select(Project).where(Project.owner_id == user_id).order_by(Project.created_at.desc())
    ).all()
    return [_model_dict(p) for p in projects]


@router.post("", status_code=201)
def create_project(
    body: ProjectCreate,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name cannot be empty")

    slug_base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    # Ensure unique slug by appending a counter if needed
    slug = slug_base
    counter = 1
    while session.exec(select(Project).where(Project.slug == slug)).first():
        slug = f"{slug_base}-{counter}"
        counter += 1

    project = Project(name=name, slug=slug, owner_id=user_id)
    session.add(project)
    session.commit()
    session.refresh(project)
    return _model_dict(project)


@router.get("/{project_id}")
def get_project(
    project_id: int,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    project = _get_owned_project(project_id, user_id, session)
    runs = session.exec(
        select(Run)
        .where(Run.project_id == project_id, Run.parent_run_id == None)  # noqa: E711
        .order_by(Run.created_at.desc())
    ).all()
    return {"project": _model_dict(project), "runs": [_model_dict(r) for r in runs]}


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: int,
    user_id: int = Depends(get_current_user_id),
    session: Session = Depends(get_session),
):
    project = _get_owned_project(project_id, user_id, session)
    has_runs = session.exec(select(Run).where(Run.project_id == project_id)).first()
    if has_runs:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete a project that has runs. Archive or delete runs first.",
        )
    session.delete(project)
    session.commit()


def _get_owned_project(project_id: int, user_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project or project.owner_id != user_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project
