"""
SQLModel table definitions for the LittleProteinTiger web platform.

Tables:
  User                  — GitHub-authenticated users with optional BYOK API key
  Project               — Named target/disease areas, each with a private corpus
  Run                   — One pipeline execution (design run or optimizer round)
  ExperimentalMeasurement — SPR/ITC/FP results linked to a named design in a run
  AuditEvent            — Immutable event log for security/compliance
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    github_id: int = Field(unique=True, index=True)
    github_login: str
    email: Optional[str] = None
    # Fernet-encrypted API keys; NULL until user supplies one
    anthropic_key_enc: Optional[bytes] = None
    gemini_key_enc: Optional[bytes] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(unique=True, index=True)
    name: str
    owner_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Run(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    user_id: int = Field(foreign_key="user.id")
    query: str
    provider: str = "claude"
    model_id: Optional[str] = None

    # Lifecycle
    status: str = "QUEUED"           # QUEUED | RUNNING | COMPLETE | FAILED | BLOCKED
    stage_current: Optional[str] = None  # pathway | structure | literature | design

    # Pipeline outputs
    pdb_id: Optional[str] = None
    target_complex: Optional[str] = None
    go_recommendation: Optional[str] = None
    # JSON-serialised list of {chain, resnum, bsa_contribution} dicts populated after stage 1
    hotspot_residues: Optional[str] = None

    # Celery
    celery_task_id: Optional[str] = None

    # Optimizer lineage: NULL for first-pass design runs
    parent_run_id: Optional[int] = Field(default=None, foreign_key="run.id")
    round_number: int = 0                    # 0=design, 1+=optimizer rounds
    input_structure_path: Optional[str] = None  # CIF path fed to binder-optimizer

    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


class ExperimentalMeasurement(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    # Matches the design name used in 03_design_inputs/ (e.g. "binder_A265E")
    design_name: str
    method: str  # SPR | ITC | FP | other
    kd_molar: Optional[float] = None
    ki_molar: Optional[float] = None
    notes: Optional[str] = None
    measured_at: datetime = Field(default_factory=datetime.utcnow)
    uploaded_by: int = Field(foreign_key="user.id")


class AuditEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    # run_created | run_viewed | data_exported | user_invited | api_key_updated
    event_type: str
    resource_id: Optional[str] = None
    ip_addr: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
