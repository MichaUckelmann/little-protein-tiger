"""
Celery tasks for the LittleProteinTiger web platform.

Tasks:
  run_pipeline_task(run_id)   — Execute the 4-stage design pipeline
  run_optimizer_task(run_id)  — Execute the binder-optimizer skill (Sprint 4)
  curate_papers_task(...)     — Project-private corpus expansion (Sprint 5)

NOTE on API key isolation: `_scoped_api_keys()` injects each task's BYOK
key(s) into this worker process's environment only for the duration of the
task, then restores whatever was there before (or clears it) in a `finally`.
Celery worker processes are long-lived and reused across tasks for different
users -- and the worker is started with `--concurrency=2` (the default
prefork pool forks a separate OS process per concurrent task, so concurrent
tasks never share an environment; this scoping is about *sequential* tasks
sharing one forked child over time). Without it, a key decrypted for one
user's task would silently leak into -- and get billed against -- a later
task for a different user who has no stored key of their own. Every call
site that sets ANTHROPIC_API_KEY / GEMINI_API_KEY must go through
_scoped_api_keys().
"""
from __future__ import annotations

import contextlib
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
from web.backend.models_db import Binder, BinderCampaign, Run, User

_SKILL_TO_STAGE: dict[str, str] = {
    "pathway-expert": "pathway",
    "wildcard-expert": "pathway",
    "complex-structure-analysis": "structure",
    "molecular-biology-expert": "literature",
    "protein-design-script": "design",
    "binder-optimizer": "optimizer",
}

# API key env vars that get injected into this worker process from a user's
# BYOK settings. See _scoped_api_keys() and the module docstring above.
_API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY")


@contextlib.contextmanager
def _scoped_api_keys(user: User | None):
    """Inject `user`'s BYOK API keys into the environment for the life of the
    `with` block, then restore (or clear) whatever was there before.

    This is the fix for a cross-tenant credential leak: a long-lived Celery
    worker handling task N for a keyed user, then task N+1 for a different
    user with no stored key of their own, would otherwise silently reuse
    and get billed for the first user's key. Every task that injects
    ANTHROPIC_API_KEY / GEMINI_API_KEY must route through this.
    """
    from web.backend.crypto import decrypt_key

    originals = {var: os.environ.get(var) for var in _API_KEY_ENV_VARS}
    try:
        if user and getattr(user, "anthropic_key_enc", None):
            os.environ["ANTHROPIC_API_KEY"] = decrypt_key(user.anthropic_key_enc)
        if user and getattr(user, "gemini_key_enc", None):
            os.environ["GEMINI_API_KEY"] = decrypt_key(user.gemini_key_enc)
        yield
    finally:
        for var, original in originals.items():
            if original is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = original


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
    from src.pipeline_runner import PipelineRunner, PipelineBlockedError, PipelinePausedError
    from cryptography.fernet import InvalidToken

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
        stage_models = json.loads(run.stage_models_json) if run.stage_models_json else None
        extended_thinking = run.extended_thinking
        auto_mode = run.auto_mode
        pathway_mode = run.pathway_mode

    try:
        with _scoped_api_keys(user):
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
                    handoff = super()._run_stage(skill_name, query, context_files, output_file)
                    if stage == "structure":
                        try:
                            hotspot_json = self._parse_hotspot_residues(
                                output_file.read_text(encoding="utf-8"), handoff
                            )
                            if hotspot_json:
                                _set_run_fields(run_id, hotspot_residues=hotspot_json)
                        except Exception as exc:
                            logger.warning(f"Run {run_id}: hotspot parse failed: {exc}")
                    return handoff

            _set_run_fields(run_id, status="RUNNING")

            try:
                runner = _TrackedRunner(
                    config=config,
                    provider=provider,
                    model_id=model_id,
                    output_dir=run_dir,
                    max_tokens=200_000,  # structure analysis legitimately needs large context
                    stage_models=stage_models,
                    extended_thinking_stages={"structure"} if extended_thinking else None,
                    pathway_mode=pathway_mode,
                )
                result = runner.run(query=query, pdb_id=pdb_id, auto_mode=auto_mode,
                                    target_complex=run.target_complex if pdb_id else None)

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

            except PipelinePausedError as exc:
                fields: dict = {"status": "PAUSED", "pause_point": exc.pause_point, "completed_at": None}
                if exc.pause_point == "pathway_choice":
                    fields["pathway_choices_json"] = json.dumps(exc.payload["choices"])
                    fields["stage_current"] = "pathway"
                elif exc.pause_point == "structure_choice":
                    fields["stage_current"] = "structure"
                elif exc.pause_point == "structure_needed":
                    fields["stage_current"] = "pathway"
                elif exc.pause_point == "literature_choice":
                    fields["stage_current"] = "literature"
                    fields["go_recommendation"] = exc.payload.get("go_recommendation")
                _set_run_fields(run_id, **fields)
                logger.info(f"Run {run_id} paused at {exc.pause_point}")
                # Do NOT re-raise — PAUSED is expected, not an error

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

    except InvalidToken:
        _set_run_fields(
            run_id,
            status="FAILED",
            error=(
                "API key could not be decrypted — the server encryption key may have changed. "
                "Please re-enter your API key in account settings."
            ),
            completed_at=datetime.utcnow(),
        )
        logger.error(f"Run {run_id}: InvalidToken decrypting API key (FERNET_KEY rotation?)")
        return


# ---------------------------------------------------------------------------
# Resume task — re-enters the pipeline after a PAUSED user choice
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="resume_pipeline")
def resume_pipeline_task(self, run_id: int) -> None:
    """Resume a PAUSED run from the stored pause_point."""
    from sqlmodel import Session
    from src.pipeline_runner import (
        PipelineRunner,
        PipelineBlockedError,
        PipelinePausedError,
    )
    from cryptography.fernet import InvalidToken

    with Session(engine) as session:
        run = session.get(Run, run_id)
        if not run:
            logger.error(f"resume_pipeline_task: Run {run_id} not found")
            return
        user = session.get(User, run.user_id)
        query = run.query
        pdb_id = run.pdb_id
        target_complex = run.target_complex
        provider = run.provider
        model_id = run.model_id
        stage_models = json.loads(run.stage_models_json) if run.stage_models_json else None
        extended_thinking = run.extended_thinking
        pause_point = run.pause_point
        structure_next_step = run.structure_next_step
        pathway_mode = run.pathway_mode

    try:
        with _scoped_api_keys(user):
            config = _load_config()
            run_dir = _ROOT / "web" / "runs" / str(run_id)

            class _TrackedRunner(PipelineRunner):
                def _run_stage(self, skill_name, query, context_files, output_file):
                    stage = _SKILL_TO_STAGE.get(skill_name, skill_name)
                    _set_run_fields(run_id, stage_current=stage)
                    handoff = super()._run_stage(skill_name, query, context_files, output_file)
                    if stage == "structure":
                        try:
                            hotspot_json = self._parse_hotspot_residues(
                                output_file.read_text(encoding="utf-8"), handoff
                            )
                            if hotspot_json:
                                _set_run_fields(run_id, hotspot_residues=hotspot_json)
                        except Exception as exc:
                            logger.warning(f"Run {run_id}: hotspot parse failed: {exc}")
                    return handoff

            _set_run_fields(run_id, status="RUNNING", pause_point=None)

            # Determine start_from and context_file based on which pause point we're resuming
            if pause_point == "pathway_choice":
                start_from = "structure"
                context_file = run_dir / "00_pathway.md"
            elif pause_point == "structure_needed":
                start_from = "structure"
                context_file = run_dir / "00_pathway.md"
            elif pause_point == "structure_choice":
                if structure_next_step == "design_only":
                    start_from = "design"
                else:
                    start_from = "literature"
                context_file = run_dir / "01_structure.md"
            elif pause_point == "literature_choice":
                start_from = "design"
                context_file = run_dir / "02_literature.md"
            else:
                logger.error(f"Resume run {run_id}: unknown pause_point {pause_point!r}")
                _set_run_fields(
                    run_id,
                    status="FAILED",
                    error=f"Cannot resume: unknown pause_point {pause_point!r}",
                    completed_at=datetime.utcnow(),
                )
                return

            try:
                runner = _TrackedRunner(
                    config=config,
                    provider=provider,
                    model_id=model_id,
                    output_dir=run_dir,
                    max_tokens=200_000,
                    stage_models=stage_models,
                    extended_thinking_stages={"structure"} if extended_thinking else None,
                    pathway_mode=pathway_mode,
                )
                result = runner.run(
                    query=query,
                    start_from=start_from,
                    pdb_id=pdb_id,
                    context_file=context_file if context_file.exists() else None,
                    auto_mode=False,
                    structure_next_step=structure_next_step,
                    # Pass the user-selected target_complex explicitly so the pipeline
                    # runner doesn't overwrite it with the primary recommendation's
                    # complex name from the pathway handoff.
                    target_complex=target_complex,
                )

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
                    f"Run {run_id} resumed+complete: {result.go_recommendation}, "
                    f"stages={result.stages_completed}"
                )

            except PipelinePausedError as exc:
                # Paused again (e.g. pathway_choice resume → paused at structure_choice)
                fields: dict = {"status": "PAUSED", "pause_point": exc.pause_point, "completed_at": None}
                if exc.pause_point == "pathway_choice":
                    fields["pathway_choices_json"] = json.dumps(exc.payload["choices"])
                    fields["stage_current"] = "pathway"
                elif exc.pause_point == "structure_choice":
                    fields["stage_current"] = "structure"
                elif exc.pause_point == "structure_needed":
                    fields["stage_current"] = "pathway"
                elif exc.pause_point == "literature_choice":
                    fields["stage_current"] = "literature"
                    fields["go_recommendation"] = exc.payload.get("go_recommendation")
                _set_run_fields(run_id, **fields)
                logger.info(f"Run {run_id} paused again at {exc.pause_point}")

            except PipelineBlockedError as exc:
                _set_run_fields(
                    run_id,
                    status="BLOCKED",
                    error=str(exc),
                    completed_at=datetime.utcnow(),
                )
                logger.warning(f"Run {run_id} blocked on resume: {exc}")

            except Exception as exc:
                _set_run_fields(
                    run_id,
                    status="FAILED",
                    error=str(exc),
                    completed_at=datetime.utcnow(),
                )
                logger.error(f"Run {run_id} failed on resume: {exc}")
                raise

    except InvalidToken:
        _set_run_fields(
            run_id,
            status="FAILED",
            error=(
                "API key could not be decrypted — the server encryption key may have changed. "
                "Please re-enter your API key in account settings."
            ),
            completed_at=datetime.utcnow(),
        )
        logger.error(f"Resume run {run_id}: InvalidToken decrypting API key")
        return


# ---------------------------------------------------------------------------
# Retry task — restart a FAILED run from its failed stage
# ---------------------------------------------------------------------------

_CONTEXT_FOR_STAGE: dict[str, str] = {
    "structure": "00_pathway.md",
    "literature": "01_structure.md",
    "design": "02_literature.md",
}


@celery_app.task(bind=True, name="retry_run")
def retry_run_task(self, run_id: int, start_from: str) -> None:
    """Retry a FAILED run starting from start_from stage."""
    from sqlmodel import Session
    from src.pipeline_runner import PipelineRunner, PipelineBlockedError, PipelinePausedError
    from cryptography.fernet import InvalidToken

    with Session(engine) as session:
        run = session.get(Run, run_id)
        if not run:
            logger.error(f"retry_run_task: Run {run_id} not found")
            return
        user = session.get(User, run.user_id)
        query = run.query
        pdb_id = run.pdb_id
        target_complex = run.target_complex
        provider = run.provider
        model_id = run.model_id
        stage_models = json.loads(run.stage_models_json) if run.stage_models_json else None
        extended_thinking = run.extended_thinking
        auto_mode = run.auto_mode
        structure_next_step = run.structure_next_step
        pathway_mode = run.pathway_mode

    try:
        with _scoped_api_keys(user):
            config = _load_config()
            run_dir = _ROOT / "web" / "runs" / str(run_id)

            class _TrackedRunner(PipelineRunner):
                def _run_stage(self, skill_name, query, context_files, output_file):
                    stage = _SKILL_TO_STAGE.get(skill_name, skill_name)
                    _set_run_fields(run_id, stage_current=stage)
                    handoff = super()._run_stage(skill_name, query, context_files, output_file)
                    if stage == "structure":
                        try:
                            hotspot_json = self._parse_hotspot_residues(
                                output_file.read_text(encoding="utf-8"), handoff
                            )
                            if hotspot_json:
                                _set_run_fields(run_id, hotspot_residues=hotspot_json)
                        except Exception as exc:
                            logger.warning(f"Run {run_id}: hotspot parse failed: {exc}")
                    return handoff

            _set_run_fields(run_id, status="RUNNING", error=None)

            # Use the previous stage's output as context if it exists
            context_filename = _CONTEXT_FOR_STAGE.get(start_from)
            context_file = run_dir / context_filename if context_filename else None
            if context_file and not context_file.exists():
                context_file = None

            try:
                runner = _TrackedRunner(
                    config=config,
                    provider=provider,
                    model_id=model_id,
                    output_dir=run_dir,
                    max_tokens=200_000,
                    stage_models=stage_models,
                    extended_thinking_stages={"structure"} if extended_thinking else None,
                    pathway_mode=pathway_mode,
                )
                result = runner.run(
                    query=query,
                    start_from=start_from,
                    pdb_id=pdb_id,
                    context_file=context_file,
                    auto_mode=auto_mode,
                    structure_next_step=structure_next_step,
                    target_complex=target_complex,
                )

                _set_run_fields(
                    run_id,
                    status="COMPLETE",
                    stage_current=None,
                    pdb_id=result.pdb_id,
                    target_complex=result.target_complex,
                    go_recommendation=result.go_recommendation,
                    completed_at=datetime.utcnow(),
                )
                logger.info(f"Run {run_id} retry complete: {result.go_recommendation}")

            except PipelinePausedError as exc:
                fields: dict = {"status": "PAUSED", "pause_point": exc.pause_point, "completed_at": None}
                if exc.pause_point == "pathway_choice":
                    fields["pathway_choices_json"] = json.dumps(exc.payload["choices"])
                    fields["stage_current"] = "pathway"
                elif exc.pause_point == "structure_choice":
                    fields["stage_current"] = "structure"
                elif exc.pause_point == "structure_needed":
                    fields["stage_current"] = "pathway"
                elif exc.pause_point == "literature_choice":
                    fields["stage_current"] = "literature"
                    fields["go_recommendation"] = exc.payload.get("go_recommendation")
                _set_run_fields(run_id, **fields)
                logger.info(f"Run {run_id} retry paused at {exc.pause_point}")

            except PipelineBlockedError as exc:
                _set_run_fields(run_id, status="BLOCKED", error=str(exc), completed_at=datetime.utcnow())
                logger.warning(f"Run {run_id} retry blocked: {exc}")

            except Exception as exc:
                _set_run_fields(run_id, status="FAILED", error=str(exc), completed_at=datetime.utcnow())
                logger.error(f"Run {run_id} retry failed: {exc}")
                raise

    except InvalidToken:
        _set_run_fields(
            run_id,
            status="FAILED",
            error="API key could not be decrypted — re-enter your key in account settings.",
            completed_at=datetime.utcnow(),
        )
        return


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
        provider = run.provider
        model_id = run.model_id

    with _scoped_api_keys(user):
        if not os.environ.get("ANTHROPIC_API_KEY"):
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
                provider=provider or "claude",
                model_id=model_id or "claude-sonnet-5",
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


# ---------------------------------------------------------------------------
# Binder optimizer task — operates on Binder rows, not Run rows
# ---------------------------------------------------------------------------

def _parse_mutation_output(report_text: str) -> list[dict]:
    """Extract the MUTATION OUTPUT JSON block from the optimizer report.

    Looks for a fenced JSON block after '### MUTATION OUTPUT' and returns
    a list of {mutation, mutated_sequence} dicts.  Returns [] on failure.
    """
    import re
    match = re.search(
        r"### MUTATION OUTPUT\s*```json\s*(\[.*?\])\s*```",
        report_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return []
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return []


@celery_app.task(bind=True, name="run_binder_optimizer")
def run_binder_optimizer_task(self, binder_id: int, user_id: int, cif_abs_path: str) -> None:
    """Run the binder-optimizer skill on a single Binder and create child rows."""
    from sqlmodel import Session
    from src.skill_runner import SkillRunner

    with Session(engine) as session:
        binder = session.get(Binder, binder_id)
        if not binder:
            logger.error(f"run_binder_optimizer_task: Binder {binder_id} not found")
            return
        user = session.get(User, user_id)
        sequence = binder.sequence
        binder_name = binder.name
        campaign_id = binder.campaign_id
        binder_chain = binder.binder_chain  # may be None for older rows

    with _scoped_api_keys(user):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            logger.error(f"run_binder_optimizer_task: No API key for user {user_id}")
            return

        config = _load_config()
        _BINDER_DATA_DIR = _ROOT / "data" / "binders"
        _BINDER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        report_path = _BINDER_DATA_DIR / f"optimizer_{binder_id}.md"

        # Build chain assignment clause from the sequence-matched chain stored at
        # upload time.  If unavailable (older rows), fall back to a warning so the
        # skill knows to confirm before proceeding rather than assuming chain A.
        if binder_chain:
            chain_clause = (
                f"Chain {binder_chain} is the designed binder (identified by sequence match). "
                f"All other chains are target — do NOT mutate them."
            )
        else:
            chain_clause = (
                "WARNING: binder chain could not be determined automatically. "
                "Inspect the sequence map to confirm which chain is the designed binder "
                "before proposing mutations — do not assume chain A."
            )

        prompt = (
            f"Optimize binder at {cif_abs_path}. {chain_clause} "
            f"Binder sequence: {sequence}"
        )

        try:
            skill_runner = SkillRunner(
                skill_name="binder-optimizer",
                provider="claude",
                model_id="claude-sonnet-5",
                config=config,
            )
            report_text = skill_runner.run(prompt)
            report_path.write_text(report_text, encoding="utf-8")

            # Parse mutations and create child Binder rows
            mutations = _parse_mutation_output(report_text)
            relative_report = str(report_path.relative_to(_ROOT))

            with Session(engine) as session:
                # Store report path on parent binder
                parent = session.get(Binder, binder_id)
                if parent:
                    parent.optimizer_report_path = relative_report
                    session.add(parent)

                for entry in mutations:
                    mut_label = entry.get("mutation", "")
                    mut_seq = entry.get("mutated_sequence", "")
                    if not mut_seq:
                        continue
                    child = Binder(
                        campaign_id=campaign_id,
                        parent_id=binder_id,
                        name=f"{binder_name}_{mut_label}" if mut_label else f"{binder_name}_mut",
                        sequence=mut_seq,
                        mutation_label=mut_label or None,
                        source="optimizer",
                    )
                    session.add(child)

                session.commit()

            logger.info(f"Binder optimizer complete for binder {binder_id}: {len(mutations)} mutations created")

        except Exception as exc:
            logger.error(f"run_binder_optimizer_task failed for binder {binder_id}: {exc}")
            raise
