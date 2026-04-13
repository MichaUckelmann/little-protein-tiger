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

    # Per-stage model overrides: JSON string {"structure": "claude-sonnet-4-6", ...}
    # NULL = use model_id for all stages (default uniform behaviour)
    stage_models_json: Optional[str] = None
    # Enable Claude extended thinking on the structure stage (off by default)
    extended_thinking: bool = Field(default=False)

    # Interactive pause points (auto_mode=True keeps the old fully-automatic behaviour)
    auto_mode: bool = Field(default=True)
    # Set to "pathway_choice" or "structure_choice" while the run is paused
    pause_point: Optional[str] = None
    # JSON list of parsed target choices shown to the user after pathway stage
    pathway_choices_json: Optional[str] = None
    # User's decision at the structure pause: "literature_and_design" | "design_only" | "stop"
    structure_next_step: Optional[str] = None

    # Pathway analysis mode: "standard" = pathway-expert | "wildcard" = wildcard-expert
    pathway_mode: str = Field(default="standard")

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


class BinderCampaign(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    user_id: int = Field(foreign_key="user.id")
    name: str                          # user-given label, e.g. "YAP1 round 1"
    target_name: Optional[str] = None  # optional target descriptor
    csv_filename: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Binder(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="bindercampaign.id", index=True)
    parent_id: Optional[int] = Field(default=None, foreign_key="binder.id")
    name: str                          # e.g. "design_001" or "design_001_A265E"
    sequence: str
    mutation_label: Optional[str] = None   # e.g. "A265E"
    source: str = "csv_import"         # csv_import | optimizer | manual
    # CSV quality metrics (null for optimizer/manual entries)
    design_to_target_iptm: Optional[float] = None
    min_design_to_target_pae: Optional[float] = None
    filter_rmsd: Optional[float] = None
    cif_path: Optional[str] = None          # relative path: data/binders/{id}.cif
    binder_chain: Optional[str] = None     # chain ID identified by sequence match on upload
    notes: Optional[str] = None
    optimizer_report_path: Optional[str] = None  # data/binders/optimizer_{id}.md
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BinderMeasurement(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    binder_id: int = Field(foreign_key="binder.id", index=True)
    method: str                        # SPR | ITC | FP | other
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
