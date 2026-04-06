"""
Celery tasks for the LittleProteinTiger web platform.

Tasks:
  run_pipeline_task(run_id)   — Execute the 4-stage design pipeline
  run_optimizer_task(run_id)  — Execute the binder-optimizer skill (Sprint 4)
  curate_papers_task(...)     — Project-private corpus expansion (Sprint 5)

NOTE on API key isolation: each Celery worker process handles one task at a time
(worker_prefetch_multiplier=1), so injecting the user API key into os.environ is
safe per-process. Revisit if concurrency > 1 per process.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from loguru import logger

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from web.backend.celery_app import celery_app
from web.backend.db import engine
from web.backend.models_db import Run, User

_SKILL_TO_STAGE: dict[str, str] = {
    "pathway-expert": "pathway",
    "complex-structure-analysis": "structure",
    "molecular-biology-expert": "literature",
    "protein-design-script": "design",
    "binder-optimizer": "optimizer",
}


def _set_run_fields(run_id: int, **kwargs) -> None:
    """Update Run fields inside a short-lived session."""
    from sqlmodel import Session
    with Session(engine) as session:
        run = session.get(Run, run_id)
        if run:
            for k, v in kwargs.items():
                setattr(run, k, v)
            session.add(run)
            session.commit()


def _load_config() -> dict:
    config_path = _ROOT / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Pipeline task
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="run_pipeline")
def run_pipeline_task(self, run_id: int) -> None:
    """Execute the full 4-stage design pipeline for a queued Run."""
    from sqlmodel import Session
    from src.pipeline_runner import PipelineRunner, PipelineBlockedError

    # Load run + user data
    with Session(engine) as session:
        run = session.get(Run, run_id)
        if not run:
            logger.error(f"run_pipeline_task: Run {run_id} not found")
            return
        user = session.get(User, run.user_id)
        query = run.query
        pdb_id = run.pdb_id
        provider = run.provider
        model_id = run.model_id

    # Inject BYOK API key into this worker's environment
    from web.backend.crypto import decrypt_key
    if user and user.anthropic_key_enc:
        os.environ["ANTHROPIC_API_KEY"] = decrypt_key(user.anthropic_key_enc)
    if user and user.gemini_key_enc:
        os.environ["GEMINI_API_KEY"] = decrypt_key(user.gemini_key_enc)

    # Validate that the required key for the chosen provider is present
    if provider == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        _set_run_fields(
            run_id,
            status="FAILED",
            error="No Gemini API key configured. Set your key in account settings.",
            completed_at=datetime.utcnow(),
        )
        return
    elif provider != "gemini" and not os.environ.get("ANTHROPIC_API_KEY"):
        _set_run_fields(
            run_id,
            status="FAILED",
            error="No Anthropic API key configured. Set your key in account settings.",
            completed_at=datetime.utcnow(),
        )
        return

    config = _load_config()
    run_dir = _ROOT / "web" / "runs" / str(run_id)

    # Subclass PipelineRunner to update stage_current as each stage starts
    class _TrackedRunner(PipelineRunner):
        def _run_stage(self, skill_name, query, context_files, output_file):
            stage = _SKILL_TO_STAGE.get(skill_name, skill_name)
            _set_run_fields(run_id, stage_current=stage)
            return super()._run_stage(skill_name, query, context_files, output_file)

    _set_run_fields(run_id, status="RUNNING")

    try:
        runner = _TrackedRunner(
            config=config,
            provider=provider,
            model_id=model_id,
            output_dir=run_dir,
            max_tokens=200_000,  # structure analysis legitimately needs large context
        )
        result = runner.run(query=query, pdb_id=pdb_id)

        _set_run_fields(
            run_id,
            status="COMPLETE",
            stage_current=None,
            pdb_id=result.pdb_id,
            target_complex=result.target_complex,
            go_recommendation=result.go_recommendation,
            completed_at=datetime.utcnow(),
        )
        logger.info(
            f"Run {run_id} complete: {result.go_recommendation}, "
            f"stages={result.stages_completed}"
        )

    except PipelineBlockedError as exc:
        _set_run_fields(
            run_id,
            status="BLOCKED",
            error=str(exc),
            completed_at=datetime.utcnow(),
        )
        logger.warning(f"Run {run_id} blocked: {exc}")

    except Exception as exc:
        _set_run_fields(
            run_id,
            status="FAILED",
            error=str(exc),
            completed_at=datetime.utcnow(),
        )
        logger.error(f"Run {run_id} failed: {exc}")
        raise  # re-raise so Celery marks task as FAILURE


# ---------------------------------------------------------------------------
# Optimizer task (Sprint 4 — skeleton, not yet wired to router)
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="run_optimizer")
def run_optimizer_task(self, run_id: int) -> None:
    """Execute the binder-optimizer skill for a child optimizer Run."""
    from sqlmodel import Session
    from src.skill_runner import SkillRunner

    with Session(engine) as session:
        run = session.get(Run, run_id)
        if not run:
            logger.error(f"run_optimizer_task: Run {run_id} not found")
            return
        user = session.get(User, run.user_id)
        query = run.query
        input_structure = run.input_structure_path

    if user and user.anthropic_key_enc:
        from web.backend.crypto import decrypt_key
        os.environ["ANTHROPIC_API_KEY"] = decrypt_key(user.anthropic_key_enc)
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        _set_run_fields(
            run_id,
            status="FAILED",
            error="No Anthropic API key configured.",
            completed_at=datetime.utcnow(),
        )
        return

    config = _load_config()
    run_dir = _ROOT / "web" / "runs" / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    output_file = run_dir / "04_binder_optimizer.md"

    _set_run_fields(run_id, status="RUNNING", stage_current="optimizer")

    try:
        skill_runner = SkillRunner(
            skill_name="binder-optimizer",
            provider=run.provider if run else "claude",
            model_id=run.model_id or "claude-sonnet-4-6",
            config=config,
        )
        output_text = skill_runner.run(query)
        output_file.write_text(output_text, encoding="utf-8")

        _set_run_fields(
            run_id,
            status="COMPLETE",
            stage_current=None,
            completed_at=datetime.utcnow(),
        )
        logger.info(f"Optimizer run {run_id} complete")

    except Exception as exc:
        _set_run_fields(
            run_id,
            status="FAILED",
            error=str(exc),
            completed_at=datetime.utcnow(),
        )
        logger.error(f"Optimizer run {run_id} failed: {exc}")
        raise
