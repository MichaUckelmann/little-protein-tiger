"""
Programmatic pipeline orchestrator for the LittleProteinTiger design pipeline.

Sequences four SkillRunner calls:
  Stage 0  — pathway-expert              discovers PPI target + PDB from corpus
  Stage 1  — complex-structure-analysis  interface geometry + hotspot mapping
  Stage 2  — molecular-biology-expert    prior art + tractability + go/no-go
  Stage 4  — protein-design-script       BoltzGen YAML + RFD3 JSON (if GO/CONDITIONAL_GO)

Each stage reads a '### PIPELINE HANDOFF' block from the previous output to obtain
the exact query and key fields for the next stage.  If the block is absent the runner
logs a warning and synthesises a fallback query from what it knows.

Designed as a plain synchronous class so it can be:
  - Invoked directly from scripts/run_pipeline.py (CLI)
  - Wrapped in a FastAPI background task for web deployment (no changes needed)
  - Unit-tested without subprocess overhead
"""
from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import requests
import yaml
from dotenv import load_dotenv
from loguru import logger

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

from src import handoff as _handoff
from src.design_metrics import (
    enrich_with_hotspot_sasa,
    parse_boltzgen_outputs,
    write_enriched_csv,
)
from src.design_ranking import rank_designs, write_ranking_outputs
from src.design_runner import (
    BoltzGenRunError,
    BoltzGenValidationError,
    run_pilot_then_production,
    validate_yaml,
)
from src.fingerprint_store import load_fingerprint
from src.skill_runner import SkillRefusedError, SkillRunner
from src.campaign_calibration import MIN_HITS_FOR_ESTIMATE
from src.token_budget import BudgetExceeded, TokenLedger, Usage, load_pricing

_DEFAULT_MODELS = {
    "claude": "claude-sonnet-5",
    "gemini": "gemini-3.7-flash",
}

# Haiku cannot do extended thinking; stages that ask for it get upgraded here.
_THINKING_UPGRADE_MODEL = "claude-sonnet-5"

# Models to retry on when a safety classifier declines a request, in order.
#
# Gemini goes FIRST, not last. Measured across three separate interface-stage
# refusals on the same target (PD-L1): claude-sonnet-5 refused (category "bio"),
# then claude-opus-5 ALSO refused (same category) every single time, at a higher
# per-token cost than the model that follows it — pure wasted spend, ~$0.13 for
# nothing, three times over. Anthropic's safety classifiers are consistent
# across the Claude family for a given category, so trying a second Claude
# model after a categorised refusal is a bet that has not once paid off here.
# claude-haiku-4-5 is kept as a same-provider fallback in case Gemini itself
# declines or errors (see SkillRefusedError handling in `_run_gemini`) —
# reached only when the immediate switch does not resolve it.
_REFUSAL_FALLBACK_MODELS = ["gemini:gemini-3.7-flash", "claude-opus-5",
                           "claude-haiku-4-5"]

# Per-stage default model overrides keyed by stage name. Picks up before the
# global _default_model but after an explicit user override via stage_models.
# Currently: stage 6 (summary) defaults to Haiku because Sonnet-class models
# trigger a biosecurity refusal on the "review of designed binders" task
# regardless of prompt wording, and Haiku handles tabular summarisation
# correctly and cheaply.
_DEFAULT_STAGE_MODELS = {
    "claude": {
        "summary": "claude-haiku-4-5",
        "binder_summary": "claude-haiku-4-5",
    },
    "gemini": {},
}

# Maps stage name → skill name (inverse of tasks.py _SKILL_TO_STAGE)
_STAGE_TO_SKILL: dict[str, str] = {
    "pathway":    "pathway-expert",
    "structure":  "complex-structure-analysis",
    "literature": "molecular-biology-expert",
    "design":     "protein-design-script",
    "summary":    "design-analyst",
    # Binder (target-name-first) workflow stages.  Only the LLM stages appear
    # here; trim / binder_spec / pilot / calibration / production /
    # binder_scoring are deterministic Python and follow the execution+analysis
    # convention of having no skill entry.
    "target_intel":       "binder-target-intel",
    "interface":          "complex-structure-analysis",
    "binder_summary":     "design-analyst",
}


class _TrimFromDisk:
    """
    The subset of TrimResult the later binder stages use, rebuilt from
    trim_map.json so a resume does not have to re-run the trim.
    """

    def __init__(self, mapping: dict):
        self.kept_segments = [tuple(s) for s in mapping.get("kept_segments", [])]
        self.n_segments = int(mapping.get("n_segments", len(self.kept_segments)))
        self.contig = mapping.get("contig", "")
        self.trimmed_path = Path(mapping.get("trimmed_path")
                                 or mapping.get("source", ""))
        self.bsa_retention = float(mapping.get("bsa_retention", 1.0))
        self.target_chain = mapping.get("target_chain", "")
        self.partner_chain = mapping.get("partner_chain", "")
        self.pdb_id = mapping.get("pdb_id", "")
        self.warnings = list(mapping.get("warnings") or [])


def _stage_for_skill(skill_name: str) -> str:
    """
    Best-effort inverse of _STAGE_TO_SKILL.

    First match wins, so this is ambiguous for any skill serving more than one
    stage (complex-structure-analysis -> structure | interface, design-analyst
    -> summary | binder_summary).  Callers that know their stage must pass it
    explicitly; this exists only as the legacy fallback.
    """
    return next(
        (s for s, sk in _STAGE_TO_SKILL.items() if sk == skill_name),
        skill_name,
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PipelineError(RuntimeError):
    """Non-recoverable pipeline failure."""


class PipelineBlockedError(PipelineError):
    """Pipeline cannot continue automatically — user input required."""


class PipelinePausedError(PipelineError):
    """
    Pipeline is pausing for user input.  Not an error — the run resumes after
    the user makes a choice via the web UI.

    Attributes
    ----------
    pause_point : str
        ``"pathway_choice"`` or ``"structure_choice"``
    payload : dict
        Data to persist alongside the pause point (e.g. parsed target choices).
    """

    def __init__(self, pause_point: str, payload: dict) -> None:
        super().__init__(f"Paused at {pause_point}")
        self.pause_point = pause_point
        self.payload = payload


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    run_dir: Path
    stages_completed: list[str] = field(default_factory=list)
    go_recommendation: str = "INCOMPLETE"   # GO | CONDITIONAL_GO | NO_GO | INCOMPLETE
    go_rationale: str = ""
    stage_files: dict[str, Path] = field(default_factory=dict)
    target_complex: str | None = None
    pdb_id: str | None = None
    design_files: list[Path] = field(default_factory=list)
    error: str | None = None
    # JSON string: {"target_chain": "A", "partner_chain": "B", "residues": [...]}
    # Populated after the structure stage; None if MODEL-READY HOTSPOTS not found.
    hotspot_residues_json: str | None = None
    # Persisted handoff dicts so later stages can read fields from earlier
    # stages even when prev_handoff has been rebound. Set by _stage_pathway,
    # _stage_structure, and _stage_literature.
    pathway_handoff: dict | None = None
    structure_handoff: dict | None = None
    literature_handoff: dict | None = None
    # Deterministic PDB-vs-expected-target identity check result, populated
    # in run() right after _ensure_structure. Always present; the structure
    # stage surfaces it to the LLM as evidence (synonym → proceed,
    # paralog mismatch → NO_GO).
    pdb_identity_check: dict | None = None


# ---------------------------------------------------------------------------
# PipelineRunner
# ---------------------------------------------------------------------------

class PipelineRunner:
    """
    Run the full LittleProteinTiger design pipeline as a sequence of SkillRunner calls.

    Parameters
    ----------
    config : dict
        Loaded config.yaml content.
    provider : str
        LLM provider — "claude" or "gemini". Default "gemini"
        (gemini-3.7-flash): ~4x cheaper input than claude-sonnet-5, and
        this pipeline's own ledger shows it reliably answering the same
        target-intel/interface prompts claude-sonnet-5 refuses (category
        "bio") — see `models.claude.refusal_fallbacks` in config.yaml,
        which leads with Gemini for exactly that reason. Gemini can still
        decline in principle; `models.gemini.refusal_fallbacks` covers
        that case by falling through to Claude.
    model_id : str | None
        Override model ID; defaults to provider default.
    output_dir : Path | None
        Root directory for run outputs.  Defaults to
        <repo>/outputs/<query_slug>_<date>/.
    max_iter : int
        Maximum LLM iterations per stage (passed to each SkillRunner).
    max_tokens : int
        Abort guard — stage aborts if input token count exceeds this.
    """

    STAGE_ORDER = ["pathway", "literature", "structure", "design", "execution", "analysis", "summary"]

    def __init__(
        self,
        config: dict,
        provider: str = "gemini",
        model_id: str | None = None,
        output_dir: Path | None = None,
        max_iter: int = 30,
        max_tokens: int = 100_000,
        stage_models: dict[str, str] | None = None,
        extended_thinking_stages: set[str] | None = None,
        pathway_mode: str = "standard",
        capture_traces: bool = False,
        project: "Project | None" = None,
        round_id: str | None = None,
        workflow: str = "ppi",
        budget_usd: float | None = None,
        budget_mode: str = "hard",
        detach: bool = False,
        n_batches: int | None = None,
        compute: str = "auto",
        max_local_hours: float | None = None,
        trial_sites: int = 1,
        trial_backbones: int = 300,
        escalate_to: int | None = 1000,
        stop_after: str | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        # config.yaml `models:` is the source of truth; the module-level
        # _DEFAULT_* tables are the fallback when it is absent.
        self._models_cfg = (config.get("models") or {}).get(provider) or {}
        self._default_model = (
            model_id
            or self._models_cfg.get("default")
            or _DEFAULT_MODELS[provider]
        )
        load_pricing(config)
        self._output_dir_override = output_dir
        # Optional persistent project (src/project.py). When supplied, stage
        # files land in project.run_dir(round_id) and stage state is mirrored
        # into the project manifest. None => legacy outputs/<slug>_<date>/ layout.
        self._project = project
        self._round_id = round_id
        # "ppi" (default binder/inhibitor track) | "binder" (target-name-first).
        if workflow not in {"ppi", "binder"}:
            raise ValueError(
                f"Invalid workflow={workflow!r}; expected 'ppi' or 'binder'."
            )
        self._workflow = workflow
        self.max_iter = max_iter
        self.max_tokens = max_tokens
        # Per-stage overrides: {stage_name: model_id}. Empty = uniform default.
        self._stage_models: dict[str, str] = stage_models or {}
        # Stages that get Claude extended thinking. Ignored for Gemini.
        self._ext_thinking: set[str] = extended_thinking_stages or set()
        # When True, every LLM stage dumps trace_raw.json + trace_rendered.md
        # under <run_dir>/traces/<stage>/ after the call returns. Off by
        # default — adds disk + slight extra latency per stage but is the
        # only way to audit the full conversation including thinking blocks
        # and tool call/response chains after a run.
        self._capture_traces = capture_traces
        # API dollar budget. The ledger is constructed in run(), once run_dir
        # is known; deterministic (non-LLM) stages never consume from it.
        self._budget_usd = budget_usd
        self._budget_mode = budget_mode
        self._ledger: TokenLedger | None = None
        # Binder track: whether GPU stages block or return immediately, and an
        # optional override of the RFD3 batch count.
        self._detach = detach
        self._n_batches = n_batches
        # "auto" (default) | "local" (foundry on this workstation's GPU) |
        # "cluster" (src/cluster_runner.py: LPT stages inputs + a launch
        # script onto shared storage, a human submits, LPT reads results back
        # on resume — this machine has no SLURM login-node access).
        # "auto" only affects the *production* stage: calibration's estimated
        # single-GPU wall-clock is compared against `max_local_hours` and the
        # decision (local vs. cluster) is persisted into calibration.json so
        # it survives a resume in a fresh process. Every other GPU stage
        # (pilot, calibration itself) always runs locally regardless of this
        # setting — they're deliberately small.
        if compute not in {"auto", "local", "cluster"}:
            raise ValueError(
                f"Invalid compute={compute!r}; expected 'auto', 'local', or 'cluster'.")
        self._compute = compute
        # Hours threshold for choose_compute()'s local-vs-cluster call at the
        # calibration stage. None => fall back to
        # config.yaml design.foundry.max_local_hours (default 48.0).
        self._max_local_hours = max_local_hours
        # Site trials: how many epitopes to compare, at what size, and where to
        # escalate when a trial is too small to measure a rate.
        self._trial_sites = max(1, int(trial_sites))
        self._trial_backbones = int(trial_backbones)
        self._escalate_to = escalate_to
        self._stop_after = stop_after
        # "standard" = pathway-expert | "wildcard" = wildcard-expert.
        # CLI / explicit kwarg overrides config; if caller passed the default
        # "standard" verbatim, fall back to whatever config says so users can
        # set the default per-project in config.yaml without touching code.
        cfg_pathway_mode = (
            (config.get("design") or {}).get("pathway", {}).get("mode", "standard")
        )
        resolved_mode = pathway_mode if pathway_mode != "standard" else cfg_pathway_mode
        if resolved_mode not in {"standard", "wildcard"}:
            raise ValueError(
                f"Invalid pathway_mode={resolved_mode!r}; expected "
                f"'standard' or 'wildcard'."
            )
        self._pathway_mode: str = resolved_mode

    @property
    def model_id(self) -> str:
        """Uniform model for callers that don't care about per-stage routing."""
        return self._default_model

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        query: str,
        start_from: str = "pathway",
        pdb_id: str | None = None,
        context_file: Path | None = None,
        auto_mode: bool = True,
        structure_next_step: str | None = None,
        target_complex: str | None = None,
        force_production: bool = False,
        target: str | None = None,
    ) -> PipelineResult:
        """
        Run the pipeline from `start_from` onwards.

        Parameters
        ----------
        query : str
            User's initial query (e.g. "design PPI inhibitors for MRSA").
        start_from : str
            Stage to begin at: pathway | structure | literature | design.
        pdb_id : str | None
            Known PDB accession.  Skips pathway stage when provided
            (implies start_from="structure" unless explicitly overridden).
        context_file : Path | None
            Prior stage output .md file to seed context.  Required when
            start_from != "pathway" and pdb_id is not given.
        auto_mode : bool
            When True (default) the pipeline runs end-to-end without pausing.
            When False, raises PipelinePausedError after pathway and structure
            stages so the web UI can collect user choices before continuing.
        structure_next_step : str | None
            Injected on resume from a structure_choice pause.
            "literature_and_design" | "design_only" | "stop".
            Ignored when auto_mode=True.
        target : str | None
            Binder workflow only: the protein to design against, e.g. "KRAS".
            Skips discovery — the target is a given, not something to find.
        target_complex : str | None
            Explicitly-set target complex from the user's choice (e.g.
            "EapH2 / Cathepsin-G").  Takes precedence over the target_complex
            in the pathway handoff, which always contains the primary
            recommendation and would be wrong when the user picks a different
            choice.
        """
        if pdb_id and start_from == "pathway":
            start_from = "structure"

        safe_slug = re.sub(r"[^a-zA-Z0-9]+", "_", query[:40]).strip("_").lower()
        run_dir = self._output_dir_override or (
            _ROOT / "outputs" / f"{safe_slug}_{date.today().isoformat()}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Pipeline run dir: {run_dir}")
        self._init_ledger(run_dir)

        result = PipelineResult(run_dir=run_dir, pdb_id=pdb_id, target_complex=target_complex)

        # ── Binder workflow: target-name-first track ─────────────────────────
        # Skips pathway/literature discovery entirely: the target is already
        # named, so stage 0 is a structural choice, not a biological search.
        # Runs foundry (RFD3 -> solubleMPNN -> RF3) on the local GPU rather than
        # BoltzGen, and sizes the production run from a measured calibration.
        if self._workflow == "binder":
            if start_from in ("pathway", "structure", "literature", "design"):
                start_from = "target_intel"
            return self._run_binder_track(
                query, run_dir, result,
                start_from=start_from, context_file=context_file,
                auto_mode=auto_mode, target=target,
                attach=not self._detach, n_batches=self._n_batches,
            )

        handoff: dict[str, str] = {}

        # Seed handoff from a pre-existing context file when resuming.
        # Prefer explicitly-passed target_complex (set from user's choice) over
        # the handoff value, which always reflects the primary recommendation.
        if context_file and context_file.exists():
            handoff = self._parse_handoff(context_file.read_text(encoding="utf-8"))
            result.pdb_id = result.pdb_id or handoff.get("pdb_id")
            result.target_complex = result.target_complex or handoff.get("target_complex")
            # Route the resumed handoff into the right slot so downstream
            # stages can read pathway/literature fields independently after the
            # chunk-5 reorder. Heuristic: presence of structure_query indicates
            # a pathway handoff; presence of go_recommendation indicates a
            # literature handoff. Both can be set if the user concatenated
            # multiple reports into one context_file.
            if "structure_query" in handoff or "choices_json" in handoff:
                result.pathway_handoff = handoff
            if "go_recommendation" in handoff or "tractability" in handoff:
                result.literature_handoff = handoff

        try:
            start_idx = self.STAGE_ORDER.index(start_from)
        except ValueError:
            raise PipelineError(
                f"Unknown start_from: {start_from!r}. Must be one of {self.STAGE_ORDER}"
            )

        try:
            # ── Stage 0: pathway-expert ──────────────────────────────────────
            if start_idx == 0:
                handoff = self._stage_pathway(query, run_dir, result)

                # ── Pause point 1: let the user pick a target ────────────────
                if not auto_mode:
                    choices = self._parse_pathway_choices(
                        (run_dir / "00_pathway.md").read_text(encoding="utf-8"),
                        handoff,
                    )
                    if not choices:
                        raise PipelineBlockedError(
                            "Pathway report had no TARGET OPPORTUNITY LANDSCAPE section — "
                            "cannot pause for target selection.  Check the pathway report "
                            "or re-run in auto mode."
                        )
                    raise PipelinePausedError("pathway_choice", {"choices": choices})

            # ── Stage 1: molecular-biology-expert ────────────────────────────
            # Runs BEFORE structure: literature-derived target_site_hint goes
            # into the next stage's query so structure analysis focuses on
            # residues literature has already implicated.
            if start_idx <= 1:
                ctx = [
                    f for f in [
                        result.stage_files.get("pathway"),
                        context_file,
                    ]
                    if f and f.exists()
                ]
                handoff = self._stage_literature(handoff, run_dir, result, ctx)

                # Pause point: review literature go/no-go before committing
                # to structure analysis (and any subsequent GPU work).
                if not auto_mode:
                    go_prelim = handoff.get("go_recommendation", "").upper().replace("-", "_")
                    raise PipelinePausedError("literature_choice", {
                        "go_recommendation": go_prelim,
                        "go_rationale": handoff.get("go_rationale", ""),
                    })

            # ── Stage 1.5: ensure structure on disk + identity check ─────────
            if start_idx <= 2:
                pdb = result.pdb_id or (result.pathway_handoff or {}).get("pdb_id", "")
                if not pdb or pdb.upper() == "NOT_FOUND":
                    raise PipelinePausedError(
                        "structure_needed",
                        {
                            "target_complex": result.target_complex or "",
                            "message": (
                                "The pathway analysis could not find a PDB structure in the corpus "
                                "for the recommended target. Provide a 4-character PDB accession "
                                "or upload a .cif file to continue."
                            ),
                        },
                    )
                result.pdb_id = pdb
                asu_path = self._ensure_structure(pdb)

                # PDB identity check. Naive substring match flags real bugs
                # (5DLT/ENPP1→ENPP2) but also flags benign name variants
                # (3KYS: "TEAD1" expected, entity says "Transcriptional
                # enhancer factor TEF-1" — same protein). Instead of failing
                # hard at the orchestrator, the result is recorded and surfaced
                # to the structure-expert which has the biological knowledge
                # to distinguish synonym ≠ paralog: TEF-1 = TEAD1 (proceed),
                # ENPP1 vs ENPP2 (NO_GO).
                expected = result.target_complex or (result.pathway_handoff or {}).get("target_complex", "")
                ba1 = asu_path.with_name(f"{pdb.upper()}_ba1.cif")
                analysis_path = ba1 if ba1.exists() else asu_path
                ok, summary = self._verify_pdb_identity(analysis_path, expected)
                # Stash the check result on result so _stage_structure can
                # include it in the query. The structure-expert is the
                # judge — it produces the actual NO_GO if the mismatch is
                # not a synonym/canonical-name case.
                result.pdb_identity_check = {
                    "ok": ok,
                    "summary": summary,
                    "expected": expected,
                }
                if ok:
                    logger.info(f"  PDB identity check PASS for {pdb}: {summary[:160]}")
                else:
                    logger.warning(
                        f"  PDB identity check flagged a possible mismatch for {pdb} "
                        f"— structure-expert will adjudicate. {summary[:200]}"
                    )

            # ── Stage 2: complex-structure-analysis ──────────────────────────
            # prev_handoff here is the literature handoff (carries target_site_hint).
            # Pathway info (structure_query, choices_json) is read from
            # result.pathway_handoff inside _stage_structure.
            if start_idx <= 2:
                ctx = [f for f in [result.stage_files.get("literature"), context_file] if f and f.exists()]
                handoff = self._stage_structure(handoff, run_dir, result, ctx)

                # Pause point: user can override or stop after structure.
                # The legacy literature_and_design vs design_only branch is no
                # longer meaningful (literature already ran) — structure_next_step
                # is treated as a kill switch only: None or anything → continue;
                # "stop" → halt.
                if not auto_mode and structure_next_step is None:
                    raise PipelinePausedError("structure_choice", {
                        "tractability": (result.literature_handoff or {}).get("tractability"),
                        "modality":     (result.literature_handoff or {}).get("modality"),
                        "bsa_A2":       handoff.get("bsa_A2"),
                        "target_complex": result.target_complex,
                    })
                if structure_next_step == "stop":
                    logger.info("structure_next_step=stop — halting pipeline.")
                    return result

            # ── Stage 3: go/no-go ────────────────────────────────────────────
            # go_recommendation comes from the literature stage (mol-bio-expert),
            # which now runs at index 1. `handoff` at this point is the structure
            # handoff, so read from result.literature_handoff explicitly.
            lit_handoff = result.literature_handoff or {}
            go = lit_handoff.get("go_recommendation", "").upper().replace("-", "_")
            result.go_recommendation = go or "INCOMPLETE"
            result.go_rationale = lit_handoff.get("go_rationale", "")

            if go == "NO_GO":
                logger.info(f"Decision: NO_GO — {result.go_rationale}")
                self._write_no_go_report(result, lit_handoff)
                return result
            if go == "CONDITIONAL_GO":
                logger.warning(f"Decision: CONDITIONAL_GO — {result.go_rationale}")
            elif go == "GO":
                logger.info(f"Decision: GO — {result.go_rationale}")
            else:
                logger.warning("go_recommendation not in mol-bio handoff — proceeding to design anyway")

            # ── Stage 3 skill: protein-design-script ─────────────────────────
            # _stage_design reads modality / design_query / etc. from lit_handoff,
            # which we pass as prev_handoff. Structure context is still attached
            # so the design skill can reference MODEL-READY HOTSPOTS.
            if start_idx <= 3:
                ctx = [
                    f for f in [
                        result.stage_files.get("structure"),
                        result.stage_files.get("literature"),
                        context_file,
                    ]
                    if f and f.exists()
                ]
                self._stage_design(lit_handoff, run_dir, result, ctx)

            # ── Stage 4: execution (boltzgen on workstation) ─────────────────
            # Deterministic Python; raises PipelinePausedError("pilot_failed")
            # when the pilot gate fails and force_production is False. The
            # execution stage reads modality from the literature handoff.
            if start_idx <= 4:
                self._stage_execution(lit_handoff, run_dir, result, force_production=force_production)

            # ── Stage 5: analysis (deterministic; no LLM) ────────────────────
            if start_idx <= 5:
                self._stage_analysis(run_dir, result)
                self._generate_ppi_report(run_dir)

            # ── Stage 6: summary (terminal LLM stage) ────────────────────────
            # Pass the literature handoff so _stage_summary can read modality /
            # design_intent / target_complex from a single source. It also
            # falls back to parsing 03_design_report.md if needed.
            if start_idx <= 6:
                self._stage_summary(lit_handoff, run_dir, result)
                self._generate_ppi_report(run_dir)

        except PipelineBlockedError:
            raise
        except Exception as exc:
            result.error = str(exc)
            logger.error(f"Pipeline error: {exc}")
            raise

        return result

    # ==================================================================
    # Binder workflow (target-name-first)
    # ==================================================================
    # A second track alongside PPI. The target is named by the user, so
    # there is no discovery stage: stage 0 chooses a structure and an interface
    # from a deterministically pre-computed candidate table. Design runs on the
    # local GPU through foundry (RFD3 -> solubleMPNN -> RF3), detached and
    # resumable, and the production scale is MEASURED by a calibration run rather
    # than guessed. Only three stages call an LLM.

    BINDER_STAGE_ORDER = [
        "target_intel", "interface", "trim", "binder_spec",
        "pilot", "calibration", "production", "binder_scoring", "binder_summary",
    ]
    _BINDER_STAGE_FILES = {
        "target_intel":   "20_target_intel.md",
        "interface":      "21_interface.md",
        "trim":           "22_trim.md",
        "binder_spec":    "23_binder_spec.md",
        "pilot":          "24_pilot.md",
        "calibration":    "25_calibration.md",
        "production":     "26_production.md",
        "binder_scoring": "27_scoring.md",
        "binder_summary": "28_summary.md",
    }
    # Deterministic stages have no skill; they still record into the manifest.
    _BINDER_DETERMINISTIC = {
        "trim", "binder_spec", "pilot", "calibration", "production", "binder_scoring",
    }

    def _binder_dirs(self, run_dir: Path) -> dict[str, Path]:
        binder = run_dir / "binder"
        dirs = {
            "binder": binder,
            "candidates": binder / "candidates",
            "trim": binder / "trim",
            "spec": binder / "spec",
            "campaign": binder / "campaign",
            "calibration": binder / "calibration",
            "scoring": binder / "scoring",
            "sites": binder / "sites",
        }
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        return dirs

    def _load_binder_handoff(self, binder_dir: Path, stage: str) -> dict[str, str]:
        """Parse a prior binder stage's handoff from its .md, for resume."""
        f = binder_dir / self._BINDER_STAGE_FILES.get(stage, "")
        if f.exists():
            return self._parse_handoff(f.read_text(encoding="utf-8"))
        return {}

    def _binder_checkpoint(self, cp_id: str, stage: str, kind: str,
                           payload: dict) -> None:
        if self._project is None or self._round_id is None:
            return
        try:
            self._project.set_checkpoint(cp_id, self._round_id, stage, kind,
                                         payload=payload)
        except Exception as exc:
            logger.warning(f"manifest checkpoint {cp_id} failed: {exc}")

    def _binder_cfg(self) -> dict:
        return self.config.get("design") or {}

    def _write_binder_report(self, path: Path, title: str, body: str,
                             handoff: dict[str, Any] | None = None) -> None:
        """Deterministic stages write their own report + handoff block."""
        text = [f"# {title}", "", body]
        if handoff:
            text += ["", "### PIPELINE HANDOFF"]
            text += [f"- {k}: {v}" for k, v in handoff.items()]
        path.write_text("\n".join(text) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # Binder stages
    # ------------------------------------------------------------------

    def _binder_prepare_candidates(self, target_name: str,
                                   dirs: dict[str, Path]) -> tuple[str, dict]:
        """
        Deterministic pre-pass: name -> ranked interface table. Costs no tokens.

        The table is what keeps the target-intel skill to a handful of tool
        calls instead of ~15; the skill's job is the judgement, not the lookup.
        """
        from src.target_resolve import (
            TargetResolutionError, build_candidate_table, render_candidate_table,
            write_candidates,
        )

        structures_dir = _ROOT / (
            (self.config.get("paths") or {}).get("structures_dir", "data/structures"))
        try:
            target, candidates = build_candidate_table(
                target_name, structures_dir=structures_dir)
        except TargetResolutionError as exc:
            raise PipelineBlockedError(str(exc)) from exc
        paths = write_candidates(target, candidates, dirs["candidates"])
        logger.info(f"candidate table -> {paths['markdown']}")
        return render_candidate_table(candidates), {
            "gene": target.gene, "uniprot": target.uniprot,
            "n_candidates": len(candidates),
        }

    def _stage_target_intel(self, query: str, target_name: str,
                            dirs: dict[str, Path],
                            result: PipelineResult) -> dict[str, str]:
        table, meta = self._binder_prepare_candidates(target_name, dirs)
        topo_note = ""
        if meta.get("uniprot"):
            from src.membrane_topology import fetch_topology

            topo = fetch_topology(meta["uniprot"])
            if topo.is_membrane:
                topo_note = (
                    f"\n\nTOPOLOGY: {topo.describe()}. This is a membrane "
                    f"protein — design against the EXTRACELLULAR region unless "
                    f"the objective explicitly says otherwise, and never pick an "
                    f"interface inside the transmembrane helix (in an isolated "
                    f"structure it is an exposed hydrophobic slab that attracts "
                    f"binders which cannot work in a membrane).")
            elif topo.fetched:
                topo_note = "\n\nTOPOLOGY: soluble protein, no membrane restriction."
            (dirs["candidates"] / "topology.json").write_text(
                json.dumps(topo.as_dict(), indent=2), encoding="utf-8")
        constraints = (self._binder_cfg().get("constraints") or {})
        sizes = constraints.get("binder_sizes") or {}
        budget = (self._binder_cfg().get("foundry") or {}).get(
            "target_residue_budget", 220)

        full_query = "\n\n".join([
            f"Design objective: {query}",
            f"Target: {target_name} ({meta['gene']} / {meta['uniprot']})",
            f"Candidate interfaces ({meta['n_candidates']} above 500 A^2), "
            f"measured on biological assembly 1:",
            table,
            f"Constraints: the target chain will be trimmed to at most {budget} "
            f"residues. Binder sizes: cyclic_peptide "
            f"{sizes.get('cyclic_peptide', {}).get('min', 12)}-"
            f"{sizes.get('cyclic_peptide', {}).get('max', 15)}, mini_protein "
            f"{sizes.get('mini_protein', {}).get('min', 70)}-"
            f"{sizes.get('mini_protein', {}).get('max', 86)}.",
        ]) + topo_note
        out = dirs["binder"] / self._BINDER_STAGE_FILES["target_intel"]
        handoff = self._run_stage("binder-target-intel", full_query, [], out,
                                  stage="target_intel")
        result.pdb_id = handoff.get("pdb_id") or result.pdb_id
        result.target_complex = (
            f"{handoff.get('target_gene', target_name)} / "
            f"{handoff.get('partner_name', '?')}")
        result.stage_files["target_intel"] = out
        result.stages_completed.append("target_intel")
        return handoff

    @staticmethod
    def _binder_sites(intel: dict[str, str], limit: int = 1) -> list[dict]:
        """
        Candidate sites to trial, primary first.

        `sites_json` is the skill's list of genuinely competitive epitopes. When
        more than one is trialled, the comparison is made on measured success
        rates rather than on argument — which is the only way to settle it.
        """
        primary = {
            "site_id": "primary",
            "pdb_id": intel.get("pdb_id"),
            "target_chain": intel.get("target_chain"),
            "partner_chain": intel.get("partner_chain"),
            "partner_name": intel.get("partner_name", ""),
            "rationale": intel.get("interface_rationale", ""),
        }
        def valid_chain(value: Any) -> bool:
            """
            An auth chain id, not a placeholder.

            The skill has emitted things like "TBD (PD-L1)"; passing that through
            fails four stages later inside gemmi with an unhelpful "chain not
            found". Real ids are short alphanumeric tokens.
            """
            text = str(value or "").strip()
            return bool(text) and len(text) <= 4 and text.isalnum()

        sites: list[dict] = []
        raw = intel.get("sites_json")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    for i, entry in enumerate(parsed):
                        if not isinstance(entry, dict) or not entry.get("pdb_id"):
                            continue
                        if not (valid_chain(entry.get("target_chain"))
                                and valid_chain(entry.get("partner_chain"))):
                            logger.warning(
                                f"site {entry.get('site_id', i + 1)!r} names "
                                f"chain(s) {entry.get('target_chain')!r}/"
                                f"{entry.get('partner_chain')!r} which are not "
                                f"auth chain ids — skipping it")
                            continue
                        entry.setdefault("site_id", f"site{i + 1}")
                        sites.append(entry)
            except json.JSONDecodeError as exc:
                logger.warning(f"sites_json is not valid JSON ({exc}); using the "
                               f"primary handoff fields only")
        if not sites:
            if not (valid_chain(primary["target_chain"])
                    and valid_chain(primary["partner_chain"])):
                raise PipelineBlockedError(
                    f"target-intel did not name usable chains "
                    f"(target={primary['target_chain']!r}, "
                    f"partner={primary['partner_chain']!r}). Chain ids must come "
                    f"from the candidate table; re-run, or pass --pdb and the "
                    f"chains explicitly.")
            sites = [primary]
        # De-duplicate on the actual interface, not the label.
        seen, unique = set(), []
        for site in sites:
            key = (site.get("pdb_id"), site.get("target_chain"),
                   site.get("partner_chain"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(site)
        if len(unique) > limit:
            logger.info(
                f"{len(unique)} candidate site(s) proposed; trialling the first "
                f"{limit}. Raise --trial-sites to compare more.")
        return unique[:limit]

    def _stage_binder_interface(self, intel: dict[str, str], dirs: dict[str, Path],
                                result: PipelineResult) -> tuple[dict[str, str], str]:
        """Reuse complex-structure-analysis to pick model-ready hotspots."""
        pdb = intel.get("pdb_id")
        if not pdb or pdb == "NOT_FOUND":
            raise PipelineBlockedError(
                "target-intel did not choose a PDB entry. Re-run with an explicit "
                "--pdb, or check the candidate table for a usable complex.")
        self._ensure_structure(pdb)

        # The concrete PDB id/chains are ALWAYS stated up front, even when
        # target-intel supplied its own structure_query — that field is a
        # freeform analytical goal ("map the front β-sheet epitope..."), not a
        # grounding statement, and has no reason to mention the accession at
        # all. Leaving it to imply the structure risked exactly what it did on
        # PD-L1/7CZD twice in a row: the interface skill, given only a
        # description and no stated accession, decided no structure existed
        # and asked for one instead of running tool_analyze_interface on the
        # file _ensure_structure had already resolved right above.
        goal = intel.get("structure_query") or (
            f"select model-ready hotspots for a "
            f"{intel.get('modality', 'mini_protein')} binder "
            f"({intel.get('design_intent', 'disrupt')} mode).")
        q = (
            f"Structure: PDB {pdb} (already downloaded to data/structures/), "
            f"{intel.get('target_gene')} = chain {intel.get('target_chain', '?')}, "
            f"{intel.get('partner_name', 'partner')} = chain "
            f"{intel.get('partner_chain', '?')}. Analyse this interface directly "
            f"with the structure tools — do not search for a different "
            f"structure or ask for one. Goal: {goal}")
        out = dirs["binder"] / self._BINDER_STAGE_FILES["interface"]
        handoff = self._run_stage("complex-structure-analysis", q, [], out,
                                  stage="interface")
        text = out.read_text(encoding="utf-8")
        hotspots = self._parse_hotspot_residues(text, handoff)
        if not hotspots:
            raise PipelineError(
                "the interface stage produced no MODEL-READY HOTSPOTS table; the "
                "RFD3 spec cannot be built without atom-level hotspots")
        self._verify_target_chain_assignment(intel, handoff, result.pdb_id or pdb)
        self._verify_hotspot_grounding(hotspots, result.pdb_id or pdb)
        result.hotspot_residues_json = hotspots
        result.stage_files["interface"] = out
        result.stages_completed.append("interface")
        return handoff, hotspots

    # Local-alignment identity above this is "same protein, engineered
    # variant" (point mutants, tags, species orthologs land ~90%+ over the
    # aligned region); unrelated proteins land well under it — BLOSUM62 local
    # alignment of two random sequences rarely clears ~30% over any
    # significant aligned length.
    _CHAIN_SEQ_IDENTITY_THRESHOLD = 0.85

    def _binder_structure_path(self, pdb_id: str) -> Path:
        """Biological assembly 1 if already downloaded, else the ASU."""
        structures_dir = _ROOT / (
            (self.config.get("paths") or {}).get("structures_dir", "data/structures"))
        ba1 = structures_dir / f"{pdb_id.upper()}_ba1.cif"
        return ba1 if ba1.exists() else structures_dir / f"{pdb_id.upper()}.cif"

    def _chain_identity_to_uniprot(self, structure_path: Path, chain: str,
                                   uniprot_seq: str) -> float | None:
        """% identity of a chain's MODELLED sequence to a UniProt sequence, or
        None if the chain's sequence could not be extracted."""
        from src.structure_tools import get_sequence_map, sequence_identity

        try:
            observed = get_sequence_map(str(structure_path), chain).get("sequence", "")
        except Exception as exc:
            logger.debug(f"could not extract chain {chain} sequence: {exc}")
            return None
        if not observed:
            return None
        return sequence_identity(observed, uniprot_seq)

    def _verify_target_chain_assignment(self, intel: dict[str, str],
                                        handoff: dict[str, str],
                                        pdb_id: str) -> None:
        """
        Confirm the interface stage assigned target_chain to the TARGET, not
        the partner.

        `_verify_hotspot_grounding` confirms a hotspot's residue is real; this
        confirms it is on the right MOLECULE. The two are independent: a chain
        swap produces perfectly-grounded hotspots — real, correctly-numbered
        residues — on the wrong protein, so grounding alone cannot catch it.

        Caught in practice: for PD-L1 (7CZD, PD-L1 on chains B/D per RCSB), the
        interface stage assigned target_chain=A and wrote hotspots on the
        anti-PD-L1 VHH nanobody's own CDR loop (Tyr32/Trp33/Tyr35/Trp47) instead
        of PD-L1's IgV domain — its own summary even said "Target chain A
        (VHH)". Chain letters, entity descriptions and the hotspot atoms were
        all internally consistent, so validate_spec and hotspot grounding both
        passed cleanly; a full multi-hour campaign ran against the wrong
        molecule before anyone noticed.

        Two independent signals, primary first:

        1. SEQUENCE — align the chain's actual MODELLED residues (from the
           structure file already on disk) against UniProt's canonical
           sequence for the target. This is ground truth: it asks what the
           atoms in the file actually are, not what a metadata field claims
           about them, so it is immune to a curation error and works even when
           RCSB has no cross-reference for that entity at all. Cheap — one
           small UniProt FASTA fetch (cached per accession) plus a local
           alignment over a ~100-500 residue chain, milliseconds.
        2. METADATA — RCSB's per-chain entity description / SIFTS-derived
           UniProt accession. Used only as the fallback when a sequence
           comparison isn't possible (no uniprot accession resolved, the
           structure file or chain sequence isn't available, or the fetch
           fails) — the case this incident actually hit, since PD-L1's
           accession WAS resolved and metadata alone would have sufficed here,
           but the fallback exists for entries where it doesn't.
        """
        target_chain = handoff.get("target_chain")
        partner_chain = handoff.get("partner_chain")
        if not target_chain or not partner_chain:
            return
        gene = (intel.get("target_gene") or "").upper()
        uniprot = (intel.get("target_uniprot") or "").upper()
        if not gene and not uniprot:
            return   # nothing to check the assignment against

        # -- 1. Sequence, when we have a UniProt accession and the structure --
        if uniprot:
            structure_path = self._binder_structure_path(pdb_id)
            if structure_path.exists():
                from src.target_resolve import fetch_uniprot_sequence

                ref_seq = fetch_uniprot_sequence(uniprot)
                if ref_seq:
                    target_id = self._chain_identity_to_uniprot(
                        structure_path, target_chain, ref_seq)
                    if target_id is not None:
                        if target_id >= self._CHAIN_SEQ_IDENTITY_THRESHOLD:
                            logger.info(
                                f"  chain assignment OK — target_chain "
                                f"{target_chain} is {target_id:.0%} identical "
                                f"to {uniprot}")
                            return
                        partner_id = self._chain_identity_to_uniprot(
                            structure_path, partner_chain, ref_seq)
                        if partner_id is not None and \
                                partner_id >= self._CHAIN_SEQ_IDENTITY_THRESHOLD:
                            raise PipelineError(
                                f"the interface stage assigned target_chain="
                                f"{target_chain} and partner_chain={partner_chain} "
                                f"BACKWARDS in {pdb_id}: by sequence, chain "
                                f"{partner_chain} is {partner_id:.0%} identical "
                                f"to {gene or uniprot}, but chain {target_chain} "
                                f"(labelled 'target') is only {target_id:.0%} "
                                f"identical. Designing against target_chain as "
                                f"stated would build binders against the wrong "
                                f"molecule.")
                        raise PipelineError(
                            f"target_chain={target_chain} in {pdb_id} is only "
                            f"{target_id:.0%} identical to {gene or uniprot} "
                            f"({uniprot})"
                            + (f", and partner_chain={partner_chain} is only "
                               f"{partner_id:.0%}" if partner_id is not None
                               else "")
                            + " — neither chain looks like the intended target "
                              "by sequence. The interface stage may have picked "
                              "the wrong entry or chains entirely.")
                    logger.debug(
                        f"could not extract a sequence for chain {target_chain} "
                        f"in {structure_path}; falling back to metadata")
                else:
                    logger.debug(
                        f"no UniProt sequence for {uniprot}; falling back to "
                        f"metadata for the chain-assignment check")

        # -- 2. Metadata fallback: entity description / SIFTS accession -------
        from src.target_resolve import entry_metadata

        try:
            meta = (entry_metadata([pdb_id]) or {}).get(pdb_id.upper())
        except Exception as exc:
            logger.warning(f"could not verify chain assignment for {pdb_id}: {exc}")
            return
        if not meta:
            logger.warning(
                f"could not verify chain assignment for {pdb_id}: no RCSB "
                f"metadata returned")
            return
        chains = meta.get("chains") or {}

        def is_target(chain: str) -> bool:
            info = chains.get(chain) or {}
            accs = {str(a).upper() for a in (info.get("uniprots") or [])}
            if uniprot and uniprot in accs:
                return True
            return bool(gene) and gene in (info.get("description") or "").upper()

        target_ok = is_target(target_chain)
        if target_ok:
            return
        partner_is_target = is_target(partner_chain)
        target_desc = (chains.get(target_chain) or {}).get("description", "?")
        if partner_is_target:
            raise PipelineError(
                f"the interface stage assigned target_chain={target_chain} and "
                f"partner_chain={partner_chain} BACKWARDS in {pdb_id}: chain "
                f"{partner_chain} is {gene or uniprot}, and chain {target_chain} "
                f"(labelled 'target') is actually {target_desc!r}. Designing "
                f"against target_chain as stated would build binders against "
                f"the wrong molecule.")
        logger.warning(
            f"could not confirm target_chain={target_chain} in {pdb_id} is "
            f"{gene or uniprot} (RCSB describes it as {target_desc!r}) — "
            f"proceeding, but verify the design target manually if results "
            f"look wrong")

    def _verify_hotspot_grounding(self, hotspots_json: str, pdb_id: str) -> None:
        """
        Confirm every hotspot's stated residue NAME matches the real structure.

        `_verify_pdb_identity` checks that the downloaded file is the right
        PROTEIN; this checks that the auth_seq_id/residue-name pairs in the
        hotspot table are actually grounded in THAT FILE'S numbering, not in
        textbook numbering the model recalls from training.

        Caught in practice on a well-studied target: the interface stage was
        asked to analyse 8ZNL and returned PD-L1's canonical literature numbering
        (Tyr56, Gln66, Arg113, ...) verbatim, but chain B residue 56 in 8ZNL is
        actually VAL — a different numbering offset from the structure the model
        clearly had memorised. `validate_spec` caught THIS case only by luck (the
        stated atoms happened not to exist on VAL); a mismatch that happened to
        share atom names would have silently trimmed and designed against the
        wrong residues. Fail loud here, before a design spec is even built.
        """
        from src.structure_tools import get_sequence_map

        data = json.loads(hotspots_json)
        chain = data.get("target_chain")
        residues = data.get("residues") or []
        if not chain or not residues:
            return
        structures_dir = _ROOT / (
            (self.config.get("paths") or {}).get("structures_dir", "data/structures"))
        ba1 = structures_dir / f"{pdb_id.upper()}_ba1.cif"
        path = ba1 if ba1.exists() else structures_dir / f"{pdb_id.upper()}.cif"
        try:
            seq_map = get_sequence_map(str(path), chain)
        except Exception as exc:
            logger.warning(
                f"could not verify hotspot grounding against {path}: {exc}")
            return
        by_auth = {r["auth_seq_id"]: r["three_letter"] for r in seq_map["residues"]}

        mismatches = []
        for h in residues:
            auth = h.get("auth_seq_id")
            claimed = str(h.get("residue", "")).upper()
            actual = by_auth.get(auth)
            if auth is None or not claimed or actual is None:
                continue
            if actual != claimed:
                mismatches.append(f"{claimed}{auth} (structure has {actual}{auth})")
        if mismatches:
            raise PipelineError(
                f"hotspot table is not grounded in {pdb_id}'s actual numbering: "
                f"{', '.join(mismatches)}. The interface stage likely reported "
                f"textbook/literature numbering for a well-known protein instead "
                f"of reading this specific structure's residues — re-run the "
                f"stage, or pick a different structure.")

    def _stage_trim(self, intel: dict[str, str], hotspots_json: str,
                    dirs: dict[str, Path],
                    result: PipelineResult) -> dict[str, Any]:
        from src.structure_trim import TrimBudgetError, TrimError, trim_target

        hs = json.loads(hotspots_json)
        # The hotspot table is parsed out of markdown, so a malformed table
        # yields an empty chain id that only fails four stages later, inside
        # gemmi, as "chain '' not found".
        for key in ("target_chain", "partner_chain"):
            value = str(hs.get(key) or "").strip()
            if not value or len(value) > 4 or not value.isalnum():
                raise PipelineError(
                    f"the interface stage's MODEL-READY HOTSPOTS table gave "
                    f"{key}={hs.get(key)!r}, which is not an auth chain id — the "
                    f"table is malformed and the RFD3 spec cannot be built")
        if not hs.get("residues"):
            raise PipelineError(
                "the MODEL-READY HOTSPOTS table listed no residues")
        cfg = self._binder_cfg()
        trim_cfg = cfg.get("trim") or {}
        budget = int((cfg.get("foundry") or {}).get("target_residue_budget", 220))
        # Biological assembly 1, not the ASU: the ASU can split a biological
        # dimer across symmetry copies, so the pair you measure is not the one
        # that exists in solution. _ensure_structure is a cheap no-op if
        # already on disk; it guarantees the ASU and attempts BA1 as a side
        # effect, so re-resolve afterwards to pick BA1 up if it just landed.
        self._ensure_structure(result.pdb_id)
        structure = self._binder_structure_path(result.pdb_id)

        # Membrane topology: keep the design target on the reachable side and
        # always drop transmembrane residues. An exposed TM helix is a
        # hydrophobic slab that preferentially attracts binders which cannot
        # work in a cell, where that surface is buried in lipid.
        restrict = None
        side = (intel.get("membrane_side") or "extracellular").strip()
        uniprot = intel.get("target_uniprot")
        if uniprot and side != "not_applicable":
            from src.membrane_topology import fetch_topology, restriction_for

            topo = fetch_topology(uniprot)
            restrict = restriction_for(result.pdb_id, hs["target_chain"], uniprot,
                                       side=side, topology=topo)
            logger.info(f"topology: {restrict.note}")
            if restrict.applies:
                bad = [h for h in hs["residues"]
                       if int(h["auth_seq_id"]) not in restrict.allowed_auth]
                if bad:
                    raise PipelineError(
                        f"hotspot(s) "
                        f"{[h.get('auth_seq_id') for h in bad]} lie outside the "
                        f"{side} region of {intel.get('target_gene')} — the chosen "
                        f"interface is not reachable by a binder. Pick a different "
                        f"site, or pass membrane_side explicitly if this is "
                        f"deliberate.")

        try:
            res = trim_target(
                structure,
                target_chain=hs["target_chain"],
                partner_chain=hs.get("partner_chain"),
                hotspots=hs["residues"],
                allowed_auth=(restrict.allowed_auth
                              if restrict and restrict.applies else None),
                budget=budget,
                out_dir=dirs["trim"],
                pdb_id=result.pdb_id,
                binder_min=int(intel.get("binder_length_min", 70)),
                binder_max=int(intel.get("binder_length_max", 86)),
                chainsaw_cmd=trim_cfg.get("chainsaw_cmd"),
                min_bsa_retention=float(trim_cfg.get("min_bsa_retention", 0.90)),
            )
        except (TrimError, TrimBudgetError) as exc:
            raise PipelineError(f"target trimming failed: {exc}") from exc

        if res.bsa_retention < 0.95 or res.warnings:
            self._binder_checkpoint(
                "trim_gate", "trim", "gate",
                {"bsa_retention": res.bsa_retention,
                 "kept_segments": [list(s) for s in res.kept_segments],
                 "warnings": res.warnings})

        out = dirs["binder"] / self._BINDER_STAGE_FILES["trim"]
        body = "\n".join([
            f"Method: **{res.method}**",
            *( [f"Topology: {restrict.note}"] if restrict else [] ),
            f"Residues: {res.n_residues_before} -> {res.n_residues_after} "
            f"in {res.n_segments} segment(s) {res.kept_segments}",
            f"Interface area of the kept residues retained: {res.bsa_retention:.1%}",
            f"Hotspots kept: {len(res.hotspots_retained)}/"
            f"{len(res.hotspots_retained) + len(res.hotspots_lost)}",
            "",
            *(f"- warning: {w}" for w in res.warnings),
        ])
        self._write_binder_report(out, "Target trimming", body, {
            "trimmed_structure": str(res.trimmed_path),
            "trim_map": str(res.mapping_path),
            "contig": res.contig,
            "n_segments": res.n_segments,
            "n_residues": res.n_residues_after,
        })
        if self._project is not None:
            try:
                self._project.add_shared_asset("structures", res.trimmed_path)
            except Exception as exc:
                logger.warning(f"could not register the trimmed structure: {exc}")
        self._record_stage("trim", "complete", out, stage="trim")
        result.stage_files["trim"] = out
        result.stages_completed.append("trim")
        return {"result": res, "report": out}

    def _stage_binder_spec(self, intel: dict[str, str], hotspots_json: str,
                           trim, dirs: dict[str, Path],
                           result: PipelineResult) -> Path:
        from src.foundry_spec import build_rfd3_spec

        hs = json.loads(hotspots_json)
        kept = {a for lo, hi in trim.kept_segments for a in range(lo, hi + 1)}
        hotspots = [h for h in hs["residues"] if int(h["auth_seq_id"]) in kept]
        name = f"{(intel.get('target_gene') or 'target').lower()}_binder_001"
        # RFD3 reads the target from a PDB; the trim writes both formats.
        pdb_input = Path(str(trim.trimmed_path)).with_suffix(".pdb")
        spec = build_rfd3_spec(
            name=name,
            structure_path=pdb_input if pdb_input.exists() else trim.trimmed_path,
            contig=trim.contig, hotspots=hotspots,
            target_chain=hs["target_chain"],
            out_path=dirs["spec"] / f"{name}.json",
            binder_min=int(intel.get("binder_length_min", 70)),
            binder_max=int(intel.get("binder_length_max", 86)),
        )
        out = dirs["binder"] / self._BINDER_STAGE_FILES["binder_spec"]
        self._write_binder_report(
            out, "RFD3 design specification",
            f"Contig `{spec.contig}` with {len(spec.hotspots)} atom-level "
            f"hotspot(s): {', '.join(sorted(spec.hotspots))}.",
            {"spec_path": str(spec.path), "design_name": name})
        self._record_stage("binder_spec", "complete", out, stage="binder_spec")
        result.stage_files["binder_spec"] = out
        result.stages_completed.append("binder_spec")
        return spec.path

    def _binder_paths(self, dirs: dict[str, Path], mode: str):
        from src.foundry_runner import FoundryPaths

        return FoundryPaths.under(dirs["campaign"] / mode)

    def _run_cluster_stage(self, mode: str, spec_path: Path, trim,
                           dirs: dict[str, Path], result: PipelineResult, *,
                           n_batches: int | None = None) -> dict:
        """
        Cluster-compute counterpart of `_run_gpu_stage`.

        Always pauses: this machine has no SLURM login-node access, so a
        human must run the generated launch script. Re-entering this stage
        (--start-from <mode> after that) checks whether the results already
        landed on shared storage; if not, it pauses again with the same
        instructions rather than re-staging (idempotent, like foundry's
        `resume()` — a second `stage_campaign()` call would just overwrite an
        already-submitted run's inputs with identical content, which is
        harmless, but re-scoring an in-progress run is not what "resume"
        should mean here).
        """
        from src.cluster_runner import (
            ClusterConfig, collect_campaign, is_complete, refold_counts,
            stage_campaign,
        )
        from src.foundry_spec import parse_contig

        ccfg = ClusterConfig.from_cfg(self.config)
        _, spans = parse_contig(trim.contig)
        target_chain = spans[0][0]
        slug = dirs["binder"].parent.name or "campaign"

        paths, plan = stage_campaign(
            spec_path, trim, dirs, ccfg, mode=mode, slug=slug,
            target_chain=target_chain, n_batches=n_batches)

        out = dirs["binder"] / self._BINDER_STAGE_FILES[mode]
        if not is_complete(paths, plan, ccfg.refold_backend):
            counts = refold_counts(paths, ccfg.refold_backend)
            self._binder_checkpoint(
                f"{mode}_cluster_pending", mode, "job",
                {"run_dir": str(paths.run_dir), "launch_script": str(paths.launch_script),
                 "expected_rf3": plan.expected_rf3, **counts})
            self._write_binder_report(
                out, f"{mode.title()} campaign staged for the cluster",
                f"Launch script: `{paths.launch_script}`\n\n"
                f"{ccfg.submit_instructions}\n\n"
                f"    bash {paths.launch_script.relative_to(ccfg.pipeline_root)}\n\n"
                f"Expecting {plan.expected_rf3:,} refolds under "
                f"`{paths.refold_dir}` (currently {counts['n_refolds']:,}). "
                f"Resume with the command below once the SLURM jobs finish.",
                {"run_dir": str(paths.run_dir), "resume_stage": mode})
            self._record_stage(mode, "awaiting_user", out, stage=mode)
            resume_cmd = (
                f"python scripts/resume_cluster_calibration.py "
                f"--project {self._project.slug if self._project else '<slug>'} "
                f"--site {slug} --n-batches {plan.n_batches} --n-gpus {plan.n_gpus}"
                if mode == "calibration" else
                # No standalone resume script for other modes yet — this is
                # the one the current workflow needs; ask if production ever
                # needs the same treatment.
                f"--workflow binder --start-from {mode}  # NOTE: only works "
                f"for a top-level (non-site-trial) campaign"
            )
            raise PipelinePausedError(f"{mode}_cluster_pending", {
                "launch_script": str(paths.launch_script),
                "submit_instructions": ccfg.submit_instructions,
                "expected_rf3": plan.expected_rf3,
                "progress": counts,
                "resume": resume_cmd,
            })

        self._write_binder_report(
            out, f"{mode.title()} campaign (cluster)",
            f"{plan.expected_rf3:,} refolds complete under `{paths.refold_dir}`.",
            {"run_dir": str(paths.run_dir)})
        self._record_stage(mode, "complete", out, stage=mode)
        result.stage_files[mode] = out
        result.stages_completed.append(mode)
        return {"paths": paths, "plan": plan, "cluster": True, "cluster_cfg": ccfg}

    def _run_gpu_stage(self, mode: str, spec_path: Path, trim, dirs: dict[str, Path],
                       result: PipelineResult, *, attach: bool,
                       n_batches: int | None = None,
                       compute_override: str | None = None) -> dict:
        """
        Launch (or re-attach to) one foundry stage and optionally wait for it.

        `compute_override` (falling back to `self._compute`) `== "cluster"`
        dispatches to `_run_cluster_stage` instead — a completely different
        compute path (stage + human-submits + resume, never a local
        subprocess), but the same call shape so callers (`_stage_calibration`
        etc.) don't need to know which one ran. The override lets a single run
        mix compute per stage — e.g. `self._compute == "auto"` with
        calibration deciding production should go to the cluster while pilot
        and calibration itself always run locally.

        Detached by default: RF3 alone runs for days at production scale, and a
        blocking call inside a Celery task would hit the visibility timeout and
        be redelivered to a second worker — two campaigns racing one GPU.
        """
        effective_compute = compute_override or self._compute
        if effective_compute == "cluster":
            return self._run_cluster_stage(mode, spec_path, trim, dirs, result,
                                           n_batches=n_batches)

        from src.foundry_runner import (
            collect, plan_campaign, prefilter_rate_observed, progress,
            render_progress, resume, run_design, wait_for_campaign,
        )

        cfg = self._binder_cfg()
        paths = self._binder_paths(dirs, mode)
        paths.mkdirs()
        observed = prefilter_rate_observed(paths)
        plan = plan_campaign(cfg, paths, mode=mode, n_batches=n_batches,
                             prefilter_rate=observed or 0.59)

        if paths.driver_path.exists():
            job = resume(paths, cfg, plan)
        else:
            job = run_design(spec_path, paths, cfg=cfg, plan=plan,
                             n_target_segments=trim.n_segments,
                             kept_segments=trim.kept_segments)
        self._binder_checkpoint(
            f"{mode}_running", mode, "job",
            {"job_id": job.job_id, "pid": job.pid,
             "campaign_dir": str(paths.campaign_dir),
             "expected_rfd3": plan.expected_rfd3,
             "expected_rf3": plan.expected_rf3,
             "resume_stage": mode})

        out = dirs["binder"] / self._BINDER_STAGE_FILES[mode]
        if not attach:
            self._write_binder_report(
                out, f"{mode.title()} campaign launched",
                render_progress(progress(paths, plan)),
                {"campaign_dir": str(paths.campaign_dir), "pid": str(job.pid),
                 "resume_stage": mode})
            self._record_stage(mode, "awaiting_user", out, stage=mode)
            raise PipelinePausedError(f"{mode}_running", {
                "campaign_dir": str(paths.campaign_dir),
                "pid": job.pid,
                "expected_rf3": plan.expected_rf3,
                "status_command":
                    f"scripts/campaign_status.py {paths.campaign_dir}",
                "resume": f"--workflow binder --start-from {mode}",
            })

        final = wait_for_campaign(
            paths, plan,
            poll_s=float((cfg.get("foundry") or {}).get("poll_interval_s", 120)))
        summary = collect(paths)
        self._write_binder_report(
            out, f"{mode.title()} campaign", render_progress(final), summary)
        self._record_stage(mode, "complete", out, stage=mode)
        result.stage_files[mode] = out
        result.stages_completed.append(mode)
        return {"paths": paths, "plan": plan, "summary": summary}

    def _score_campaign(self, paths, dirs: dict[str, Path], out_dir: Path,
                        limit: int = 0, cluster_cfg=None):
        """
        Score every refold of one campaign; returns the scored rows.

        `cluster_cfg` (from a cluster `_run_gpu_stage` result's
        `run["cluster_cfg"]`) switches to `src.cluster_runner.collect_campaign`
        — Protenix's differently-shaped output, scored with
        `binder_metrics.score_campaign_protenix` — instead of the local RF3
        path. Same FIELDS, same `refold_scores.csv` shape either way, so every
        downstream consumer (ranking, calibration) is unaware which ran.
        """
        from src.binder_metrics import write_scores

        if cluster_cfg is not None:
            from src.cluster_runner import collect_campaign

            hotspots = self._cluster_hotspots(paths)
            rows = collect_campaign(
                paths, dirs, hotspots, dirs["binder"], cluster_cfg,
                limit=limit, workers=max(1, (os.cpu_count() or 4) - 2))
            out_dir.mkdir(parents=True, exist_ok=True)
            write_scores(rows, out_dir / "refold_scores.csv")
            return rows

        from src.binder_metrics import (
            ScoreConfig, hotspots_from_rfd3, score_campaign,
        )
        from src.foundry_runner import find_design_sidecar

        sidecar = find_design_sidecar(paths)
        if sidecar is None:
            raise PipelineError(
                f"no RFD3 design sidecar under {paths.rfd3_dir}; hotspots cannot "
                f"be remapped into the refolds' numbering")
        # From a SIDECAR, never the input spec: only the sidecar carries
        # diffused_index_map, and the spec's numbers silently address the wrong
        # residues.
        hotspots = hotspots_from_rfd3(sidecar, "B")
        mcfg = (self._binder_cfg().get("binder_metrics") or {})
        rows = score_campaign(
            paths.rf3_dir, paths.rfd3_dir, hotspots=hotspots,
            cfg=ScoreConfig(contact_cutoff=float(mcfg.get("contact_cutoff", 8.0)),
                            ipsae_pae_cutoff=float(
                                mcfg.get("ipsae_pae_cutoff", 10.0))),
            workers=max(1, (os.cpu_count() or 4) - 2), limit=limit)
        out_dir.mkdir(parents=True, exist_ok=True)
        write_scores(rows, out_dir / "refold_scores.csv")
        return rows

    def _cluster_hotspots(self, paths) -> list[int]:
        """
        Hotspot residue ids (OUTPUT/RFD3 numbering) for a cluster campaign.

        The cluster path's RFD3 stage writes the same design sidecars
        (`*_model_*.json`) as the local foundry path — same binary, same
        checkpoint — so the identical diffused_index_map remap applies, but
        under a subdirectory named `diffuse`, not `rfd3` (confirmed against
        a real cluster campaign; the two pipelines share the binary but not
        this naming). Reads the first sidecar found under any completed run,
        since the map is per-target, not per-design.
        """
        from src.binder_metrics import hotspots_from_rfd3

        sidecars = sorted(paths.refold_dir.glob("run_*/diffuse/*_model_*.json"))
        if not sidecars:
            raise PipelineError(
                f"no RFD3 design sidecar found under {paths.refold_dir} yet — "
                f"the cluster's diffuse stage likely hasn't produced output.")
        return hotspots_from_rfd3(sidecars[0], "B")

    def _stage_calibration(self, spec_path: Path, trim, dirs: dict[str, Path],
                           result: PipelineResult, *, attach: bool,
                           n_batches: int | None = None) -> dict:
        """
        Refold a small sample, then MEASURE the scale production needs.

        A production run is a multi-day, ~100 GB commitment. Sizing it by guess
        is how you spend four days to learn the target was wrong.
        """
        from src.binder_ranking import EXCELLENT_IPSAE_MIN
        from src.campaign_calibration import calibrate, choose_compute, render_report
        from src.cluster_runner import ClusterConfig
        from src.foundry_runner import prefilter_rate_observed

        run = self._run_gpu_stage("calibration", spec_path, trim, dirs, result,
                                  attach=attach, n_batches=n_batches)
        paths, plan = run["paths"], run["plan"]
        rows = self._score_campaign(paths, dirs, dirs["calibration"],
                                    cluster_cfg=run.get("cluster_cfg"))

        cfg = self._binder_cfg()
        rcfg = cfg.get("binder_ranking") or {}
        fcfg = cfg.get("foundry") or {}
        # Which metric SIZES the campaign. iPTM > 0.7 is 5-20x more common than
        # ipsae_min > 0.5, so it is the one a trial-sized sample can measure;
        # ipsae_min is computed on every design regardless and carries the
        # heaviest weight in the ranking.
        metric = rcfg.get("success_metric", "iptm")
        bar = rcfg.get("excellence_bar")
        if bar is None and metric == "ipsae_min":
            bar = rcfg.get("excellence_ipsae_min", EXCELLENT_IPSAE_MIN)
        res = calibrate(
            rows,
            success_metric=metric,
            target_designs=rcfg.get("target_designs"),
            excellence_bar=(float(bar) if bar is not None else None),
            thresholds=rcfg.get("thresholds"),
            n_seq=int((fcfg.get("mpnn") or {}).get("n_seq", 4)),
            prefilter_rate=(plan.prefilter_rate if run.get("cluster_cfg")
                           else (prefilter_rate_observed(paths) or plan.prefilter_rate)),
            disk_budget_gb=float(fcfg.get("disk_budget_gb", 120)),
            max_campaign_days=float(fcfg.get("max_campaign_days", 5)),
            adaptive_bar=bool(rcfg.get("adaptive_bar", True)),
        )
        # Local-vs-cluster: purely a TIME decision against this workstation's
        # one GPU. `design.foundry.max_local_hours` (CLI: --max-local-hours)
        # is the threshold; design.cluster.n_gpus is the cluster size assumed
        # available (default 8). None when the verdict isn't a scale-up.
        cluster_cfg_for_choice = ClusterConfig.from_cfg(self.config)
        compute_choice = choose_compute(
            res,
            max_local_hours=(self._max_local_hours
                             if self._max_local_hours is not None
                             else float(fcfg.get("max_local_hours", 48.0))),
            n_gpus_cluster=cluster_cfg_for_choice.n_gpus,
        )
        if compute_choice is not None:
            logger.info(
                f"compute choice: {compute_choice.compute} "
                f"(local ~{compute_choice.local_hours:,.1f} h, "
                f"cluster ~{compute_choice.cluster_hours:,.1f} h on "
                f"{compute_choice.n_gpus_cluster} GPUs)")

        out = dirs["binder"] / self._BINDER_STAGE_FILES["calibration"]
        out.write_text(render_report(res, compute_choice) + "\n", encoding="utf-8")
        n_batches_local = self._batches_for(res, cfg, n_gpus=1)
        n_batches_cluster = self._batches_for(res, cfg, n_gpus=cluster_cfg_for_choice.n_gpus)
        persisted = {
            **res.as_dict(),
            "n_batches_local": n_batches_local,
            "n_batches_cluster": n_batches_cluster,
            "compute_choice": compute_choice.as_dict() if compute_choice else None,
        }
        (dirs["calibration"] / "calibration.json").write_text(
            json.dumps(persisted, indent=2), encoding="utf-8")

        # Always a checkpoint: how much GPU to spend is the user's call.
        self._binder_checkpoint("calibration_verdict", "calibration", "gate", {
            "verdict": res.verdict, "reason": res.verdict_reason,
            "required_refolds": res.pessimistic.required_refolds,
            "est_gpu_hours": res.pessimistic.est_gpu_hours,
            "est_disk_gb": res.pessimistic.est_disk_gb,
            "suggested_bar": res.suggested_bar,
            "compute_choice": compute_choice.as_dict() if compute_choice else None,
        })
        self._record_stage("calibration", "complete", out, stage="calibration")
        result.stage_files["calibration"] = out
        if "calibration" not in result.stages_completed:
            result.stages_completed.append("calibration")
        logger.info(f"calibration verdict: {res.verdict} — {res.verdict_reason}")
        compute = compute_choice.compute if compute_choice else "local"
        n_batches_chosen = n_batches_cluster if compute == "cluster" else n_batches_local
        return {"result": res, "n_batches": n_batches_chosen, "compute": compute,
               "n_batches_local": n_batches_local, "n_batches_cluster": n_batches_cluster}

    @staticmethod
    def _batches_for(res, cfg: dict, *, n_gpus: int = 1) -> int | None:
        """
        Production batch count implied by the calibration, if any.

        `n_gpus=1` (default) is the local single-GPU sizing: `n_batches` is a
        campaign TOTAL there. `n_gpus>1` sizes for the cluster path instead,
        where the underlying pipeline's own `NB` knob runs independently on
        EVERY GPU array task (`NARRAY x NB x DBS = total designs`, that
        pipeline's own diagnostic line) — dividing the full pessimistic
        target by n_gpus here gives the per-GPU count it actually expects.
        Getting this backwards once undercounted a real campaign by exactly
        `n_gpus` (see CLAUDE.md's cluster section).
        """
        fcfg = cfg.get("foundry") or {}
        dbs = int((fcfg.get("rfd3") or {}).get("diffusion_batch_size", 4))
        designs = res.pessimistic.required_backbones
        if res.verdict not in ("SCALE_UP", "SCALE_UP_PARTIAL") or not designs:
            return None
        return max(1, int(designs / max(dbs, 1) / max(n_gpus, 1)))

    def _resolve_production_plan(self, calib: dict | None, dirs: dict[str, Path],
                                  n_batches: int | None) -> tuple[int | None, str]:
        """
        (n_batches, compute) for the production stage, surviving a resume.

        `calib`'s fields only exist in the SAME process that ran calibration
        — resuming with `--start-from production` in a fresh process (the
        whole point of `--stop-after calibration`, and of any multi-day
        resume) starts with `calib is None`, and production would silently
        fall back to config.yaml's raw default instead of what the trial
        actually measured AND which compute path it was sized for (observed
        once on real PD-L1 data: an 18 GPU-h / 15 GB pessimistic-bound
        recommendation silently became an 87 GPU-h local run).
        `calibration.json` (written by `_stage_calibration`, next to the
        report) is the on-disk record of both `n_batches_{local,cluster}`
        and the compute decision — reload it rather than trusting values
        that only lived in memory.
        """
        default_compute = self._compute if self._compute != "auto" else "local"
        if calib:
            compute = calib.get("compute", default_compute)
            got = calib.get(f"n_batches_{compute}") or calib.get("n_batches")
            if got:
                return got, compute
        path = dirs["calibration"] / "calibration.json"
        if not path.exists():
            return n_batches, default_compute
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            verdict = data.get("verdict")
            if verdict not in ("SCALE_UP", "SCALE_UP_PARTIAL"):
                return n_batches, default_compute
            cc = data.get("compute_choice") or {}
            compute = cc.get("compute", default_compute)
            resolved = data.get(f"n_batches_{compute}")
            if not resolved:
                return n_batches, compute
            logger.info(
                f"production plan recovered from {path} (fresh resume, no "
                f"in-memory calibration result): compute={compute} "
                f"n_batches={resolved}")
            return resolved, compute
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning(f"could not recover production plan from {path}: {exc}")
            return n_batches, default_compute

    def _stage_binder_scoring(self, dirs: dict[str, Path],
                              result: PipelineResult) -> dict:
        from src.binder_ranking import (
            rank_designs, read_scores, write_ranking_outputs,
        )

        cfg = self._binder_cfg()
        rcfg = cfg.get("binder_ranking") or {}
        # Prefer production output; fall back to calibration when production was
        # never run (a pilot-only or ITERATE round still deserves a ranking).
        rows, source = None, None
        for mode in ("production", "calibration", "pilot"):
            paths = self._binder_paths(dirs, mode)
            if paths.rf3_dir.is_dir():
                from src.foundry_runner import count_rf3

                if count_rf3(paths.rf3_dir):
                    rows = self._score_campaign(paths, dirs, dirs["scoring"])
                    source = mode
                    break
        if rows is None:
            scores = dirs["calibration"] / "refold_scores.csv"
            if not scores.exists():
                raise PipelineError(
                    "no refolds found to score — run the campaign first")
            rows, source = read_scores(scores), "calibration (cached)"

        # Gate FIRST, then Rosetta. A mis-docked or low-confidence model is
        # still a physical pose, so relax and InterfaceAnalyzer return
        # well-defined, meaningless numbers for it; ranking on those promotes
        # confident nonsense. Gating first also makes the cost affordable —
        # PyRosetta is ~10-30 s per design, and the gate removes >99% of them.
        gated = rank_designs(
            rows, thresholds=rcfg.get("thresholds"), weights=rcfg.get("weights"),
            mmr=rcfg.get("mmr"), top_k=int(rcfg.get("top_k", 20)),
            max_per_backbone=int(rcfg.get("max_per_backbone", 1)))

        rosetta_note = ""
        rcfg_ros = rcfg.get("rosetta") or {}
        if rcfg_ros.get("enabled", True) and gated.survivors:
            from src.rosetta_metrics import (
                merge_into, score_designs, select_for_rosetta,
            )

            shortlist = select_for_rosetta(
                gated.survivors, limit=int(rcfg_ros.get("max_designs", 300)))
            ros = score_designs(
                [r["refold_cif"] for r in shortlist],
                dirs["scoring"] / "rosetta_metrics.csv", cfg=cfg,
                relax=bool(rcfg_ros.get("relax", True)))
            if ros.ok:
                merge_into(gated.survivors, ros)
                rosetta_note = (
                    f"\n\nRosetta interface metrics computed for "
                    f"{ros.n_scored:,} of {len(gated.survivors):,} gated designs "
                    f"({ros.n_failed} failed). They enter the composite only "
                    f"here — never the gate.")
                # Re-rank with the Rosetta terms folded in.
                weights = {**(rcfg.get("weights") or {}),
                           **(rcfg_ros.get("weights") or {})}
                ranking = rank_designs(
                    gated.survivors, thresholds={}, weights=weights,
                    mmr=rcfg.get("mmr"), top_k=int(rcfg.get("top_k", 20)),
                    max_per_backbone=int(rcfg.get("max_per_backbone", 1)))
                ranking.filter_stats = gated.filter_stats
            else:
                rosetta_note = f"\n\nRosetta metrics skipped: {ros.skipped_reason}"
                ranking = gated
        else:
            ranking = gated
        paths_out = write_ranking_outputs(ranking, dirs["scoring"])

        out = dirs["binder"] / self._BINDER_STAGE_FILES["binder_scoring"]
        self._write_binder_report(
            out, "Design scoring and ranking",
            f"Scored **{len(rows):,}** refolds from the {source} campaign.\n\n"
            f"```\n{ranking.filter_stats.render()}\n```\n\n"
            f"{len(gated.survivors):,} survivors across "
            f"{gated.n_backbones:,} distinct backbones; "
            f"top {len(ranking.top_k)} selected.{rosetta_note}",
            {"scores_csv": str(dirs["scoring"] / "refold_scores.csv"),
             "top_k_csv": str(paths_out["top_k"]),
             "n_scored": len(rows), "n_survivors": len(ranking.survivors)})
        self._record_stage("binder_scoring", "complete", out,
                           stage="binder_scoring")
        result.stage_files["binder_scoring"] = out
        result.stages_completed.append("binder_scoring")
        return {"ranking": ranking, "top_k": paths_out["top_k"]}

    # Columns the analyst actually needs. `binder_seq` is deliberately ABSENT:
    # the sequences are not needed to review a ranking, and including them
    # reliably triggers a biosecurity refusal. `binder_len` is stamped instead,
    # and the orderable FASTA is written deterministically alongside.
    _BINDER_SUMMARY_COLS = (
        "name", "mmr_rank", "composite_rank", "composite_score",
        "ipsae_min", "ipsae_max", "iptm", "iface_pae", "binder_plddt",
        "binder_rmsd_dock", "binder_rmsd_fold", "binder_tm",
        "epitope_recall", "hotspot_engagement", "clash_severe", "binder_len",
        "design_family", "mmr_max_similarity",
    )

    @classmethod
    def _slim_binder_top_k(cls, top_k_csv: Path) -> str:
        import csv as _csv
        import io as _io

        rows = list(_csv.DictReader(Path(top_k_csv).open(encoding="utf-8")))
        if not rows:
            return "(no designs survived ranking)"
        cols = [c for c in cls._BINDER_SUMMARY_COLS if c in rows[0]]
        buf = _io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue()

    @staticmethod
    def _write_binder_fasta(top_k_csv: Path, dest: Path) -> Path | None:
        """
        Write the orderable FASTA deterministically.

        Not via the LLM: the sequences are withheld from its context on purpose,
        and a transcription slip in an ordered construct is expensive.
        """
        import csv as _csv

        rows = list(_csv.DictReader(Path(top_k_csv).open(encoding="utf-8")))
        entries = [r for r in rows if r.get("binder_seq")]
        if not entries:
            return None
        lines = []
        for i, r in enumerate(entries, 1):
            lines.append(
                f">rank{i:03d}_{r.get('name', '')} "
                f"ipsae_min={r.get('ipsae_min', '')} "
                f"dock_rmsd={r.get('binder_rmsd_dock', '')}")
            lines.append(r["binder_seq"])
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return dest

    def _stage_binder_summary(self, top_k_csv: Path, intel: dict[str, str],
                              dirs: dict[str, Path],
                              result: PipelineResult) -> dict[str, str]:
        fasta = self._write_binder_fasta(top_k_csv, dirs["scoring"] / "top_k.fasta")
        if fasta:
            logger.info(f"orderable sequences -> {fasta}")
        slim = self._slim_binder_top_k(top_k_csv)
        q = (f"Review the top designed binders against "
             f"{intel.get('target_gene', 'the target')} "
             f"({intel.get('partner_name', 'partner')} interface, "
             f"{intel.get('design_intent', 'disrupt')} mode).\n\n{slim}")
        out = dirs["binder"] / self._BINDER_STAGE_FILES["binder_summary"]
        handoff = self._run_stage("design-analyst", q, [], out,
                                  stage="binder_summary")
        result.stage_files["binder_summary"] = out
        result.stages_completed.append("binder_summary")
        result.go_recommendation = handoff.get("go_recommendation",
                                               result.go_recommendation)
        return handoff

    def _run_site_trials(
        self,
        intel: dict[str, str],
        sites: list[dict],
        dirs: dict[str, Path],
        result: PipelineResult,
        *,
        attach: bool,
        trial_backbones: int,
        escalate_to: int | None,
    ) -> list[dict]:
        """
        Run one design trial per candidate site and compare measured yields.

        Reasoning cannot settle which of two defensible epitopes is more
        designable; a few hundred backbones can. Each site gets its own
        interface / trim / spec / trial under `binder/sites/<site_id>/`.

        Escalation: at the reference campaign's rate a 300-backbone trial yields
        ~1-3 hits, below the 5 needed for a usable estimate (measured: 0/10 seeds
        gave one at 300, 8/10 at 1000). So a trial that comes back "enlarge the
        sample" is automatically re-run at `escalate_to` rather than reported as
        a failure.
        """
        from src.foundry_runner import count_rf3

        trials: list[dict] = []
        for site in sites:
            site_id = str(site.get("site_id") or "primary")
            logger.info(
                f"=== site {site_id}: {site.get('pdb_id')} "
                f"{site.get('target_chain')}/{site.get('partner_chain')} "
                f"({site.get('partner_name', '?')}) ===")
            site_dirs = self._binder_dirs(dirs["sites"] / site_id)
            site_intel = {**intel, **{
                "pdb_id": site.get("pdb_id"),
                "target_chain": site.get("target_chain"),
                "partner_chain": site.get("partner_chain"),
                "partner_name": site.get("partner_name", ""),
                "interface_rationale": site.get("rationale", ""),
            }}
            site_result = PipelineResult(run_dir=site_dirs["binder"],
                                         pdb_id=site.get("pdb_id"))
            try:
                # Reuse an already-prepared site. The interface stage is the
                # expensive and refusal-prone one, so a resumed run must not
                # re-run it just to reach the GPU — and a campaign that takes
                # days will be resumed.
                prepared = self._prepared_site(site_dirs)
                if prepared is not None:
                    trim, spec = prepared
                    # validate_spec (inside _prepared_site) only confirms the
                    # atoms/residues named are real — it has no opinion on
                    # which MOLECULE they belong to. A stale spec generated
                    # before this check existed could carry a target/partner
                    # swap forever without this re-verification on every
                    # resume, not just at fresh generation.
                    self._verify_target_chain_assignment(
                        site_intel,
                        {"target_chain": trim.target_chain,
                         "partner_chain": trim.partner_chain},
                        trim.pdb_id or site.get("pdb_id", ""))
                    logger.info(
                        f"site {site_id}: reusing the prepared spec "
                        f"({spec.name}, contig {trim.contig})")
                else:
                    _, hotspots_json = self._stage_binder_interface(
                        site_intel, site_dirs, site_result)
                    trim = self._stage_trim(site_intel, hotspots_json, site_dirs,
                                            site_result)["result"]
                    spec = self._stage_binder_spec(site_intel, hotspots_json,
                                                   trim, site_dirs, site_result)
                if self._stop_after == "spec":
                    # Everything up to the GPU is prepared and validated; stop
                    # here so the specs can be reviewed before committing days
                    # of compute to them.
                    trials.append({
                        "site_id": site_id, "site": site, "calibration": None,
                        "dirs": site_dirs, "spec": spec, "trim": trim,
                        "contig": trim.contig, "n_refolds": 0,
                        "error": None, "prepared_only": True,
                    })
                    continue
                calib = self._stage_calibration(
                    spec, trim, site_dirs, site_result, attach=attach,
                    n_batches=self._backbones_to_batches(trial_backbones))
                res = calib["result"]

                if (escalate_to and escalate_to > trial_backbones
                        and res.backbone_rate.k < MIN_HITS_FOR_ESTIMATE):
                    logger.info(
                        f"site {site_id}: {res.backbone_rate.k} hit(s) in "
                        f"{res.backbone_rate.n} backbones is too few to size a "
                        f"campaign — escalating to {escalate_to}")
                    calib = self._stage_calibration(
                        spec, trim, site_dirs, site_result, attach=attach,
                        n_batches=self._backbones_to_batches(escalate_to))
                    res = calib["result"]

                paths = self._binder_paths(site_dirs, "calibration")
                trials.append({
                    "site_id": site_id, "site": site, "calibration": res,
                    "n_batches": calib.get("n_batches"),
                    "dirs": site_dirs, "spec": spec, "trim": trim,
                    "n_refolds": count_rf3(paths.rf3_dir),
                    "contig": trim.contig, "error": None,
                })
            except (PipelineError, PipelineBlockedError) as exc:
                # One unusable site must not abandon the others.
                logger.error(f"site {site_id} failed: {exc}")
                trials.append({"site_id": site_id, "site": site,
                               "calibration": None, "error": str(exc),
                               "dirs": site_dirs})
        return trials

    @staticmethod
    def _prepared_site(site_dirs: dict[str, Path]):
        """
        The (trim, spec) a previous run left on disk, or None.

        Both must be present and consistent: a spec without its trim map cannot
        be cross-checked against the segments it claims to target.
        """
        from src.foundry_spec import SpecError, validate_spec
        from src.structure_trim import load_mapping

        mapping_path = site_dirs["trim"] / "trim_map.json"
        specs = sorted(site_dirs["spec"].glob("*.json"))
        if not (mapping_path.exists() and specs):
            return None
        try:
            trim = _TrimFromDisk(load_mapping(mapping_path))
            validate_spec(specs[0], kept_segments=trim.kept_segments)
        except (SpecError, OSError, ValueError, KeyError) as exc:
            logger.warning(
                f"prepared site at {site_dirs['spec']} is unusable ({exc}); "
                f"rebuilding it")
            return None
        return trim, specs[0]

    @staticmethod
    def _backbones_to_batches(n_backbones: int,
                              diffusion_batch_size: int = 4) -> int:
        """
        RFD3 batches needed to end up with roughly `n_backbones` refolded.

        Backbones are the sampling unit that matters, but `n_batches` is the
        knob — and the prefilter drops ~40% in between, so ask for more.
        """
        # The prefilter's survival rate is target-dependent and cannot be known
        # before the first RFD3 run: measured 53% (8TAC), 59% (CD79b), 82%
        # (PD-L1). 0.55 is the conservative end, so a trial tends to over-sample
        # rather than come back too small to measure — which is the failure that
        # costs a whole extra run.
        prefilter_rate = 0.55
        return max(1, int(round(n_backbones / prefilter_rate
                                / max(diffusion_batch_size, 1))))

    def _write_trial_comparison(self, trials: list[dict], dirs: dict[str, Path],
                                result: PipelineResult) -> Path:
        """A table comparing the sites, and which one to take forward."""
        rows = ["| site | interface | contig | refolds | hits/backbones | "
                "rate | required refolds | verdict |",
                "|---|---|---|---|---|---|---|---|"]
        best, best_rate = None, -1.0
        for t in trials:
            site = t["site"]
            label = (f"{site.get('pdb_id')} {site.get('target_chain')}/"
                     f"{site.get('partner_chain')} ({site.get('partner_name', '')})")
            if t.get("prepared_only"):
                rows.append(f"| {t['site_id']} | {label} | `{t.get('contig','')}` "
                            f"| — | — | — | — | PREPARED (no GPU run) |")
                continue
            if t.get("error") or t["calibration"] is None:
                rows.append(f"| {t['site_id']} | {label} | — | — | — | — | — | "
                            f"FAILED: {str(t.get('error'))[:60]} |")
                continue
            c = t["calibration"]
            br = c.backbone_rate
            need = (f"{c.pessimistic.required_refolds:,.0f}"
                    if c.pessimistic.required_refolds else "—")
            rows.append(
                f"| {t['site_id']} | {label} | `{t.get('contig', '')}` | "
                f"{t.get('n_refolds', 0):,} | {br.k}/{br.n} | "
                f"{br.p_hat:.2%} | {need} | {c.verdict} |")
            if br.p_hat > best_rate:
                best, best_rate = t, br.p_hat

        body = ["\n".join(rows), ""]
        if best is not None and best_rate > 0:
            c = best["calibration"]
            body.append(
                f"**Most promising site: `{best['site_id']}`** — "
                f"{c.backbone_rate.k}/{c.backbone_rate.n} backbones produced a "
                f"design clearing {c.excellence_bar} "
                f"({c.backbone_rate.p_hat:.2%}).")
            if len(trials) > 1:
                body.append(
                    "\nRates this small carry wide intervals; treat a small "
                    "difference between sites as a tie rather than a ranking.")
        elif all(t.get("prepared_only") for t in trials):
            body.append(
                "**Prepared only** — specs written and validated, no GPU run yet.")
        else:
            body.append(
                "**No site produced a design clearing the bar.** Either the "
                "trials are too small to measure the rate, or these epitopes are "
                "not designable as specified — the per-site gate attribution "
                "says which.")

        out = dirs["binder"] / "29_site_comparison.md"
        self._write_binder_report(
            out, "Site trial comparison", "\n".join(body),
            {"best_site": (best or {}).get("site_id", "none"),
             "n_sites": len(trials)})
        self._record_stage("site_trials", "complete", out, stage="site_trials")
        result.stage_files["site_trials"] = out
        logger.info(f"site comparison -> {out}")
        return out

    def _generate_ppi_report(self, run_dir: Path) -> Path | None:
        """Best-effort illustrated HTML report for one PPI-track run.

        Deterministic (no LLM, no GPU) — see src/ppi_report.py. Called at
        every natural stopping point in the PPI track (after analysis, and
        after the final summary) so a report is always available for
        whatever data actually exists, without gating the run on it: report
        generation is a side effect of a completed stage, never a stage of
        its own, so a bug here must never fail — or even pause — a real run.
        """
        from src.ppi_report import ReportError, build_report

        try:
            out = build_report(run_dir, cfg=self.config)
        except ReportError as exc:
            logger.info(f"run report not generated yet for {run_dir}: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001 - reporting must never fail the run
            logger.warning(f"run report generation failed for {run_dir}: {exc}")
            return None
        logger.info(f"run report -> {out}")
        return out

    def _generate_binder_report(self, binder_dir: Path) -> Path | None:
        """Best-effort illustrated HTML report for one binder run directory.

        Deterministic (no LLM, no GPU) — see src/binder_report.py. Called at
        every natural stopping point in the binder track (after a trial, and
        after scoring) so a report is always available for whatever data
        actually exists, without gating the campaign on it: report generation
        is a side effect of a completed stage, never a stage of its own, so a
        bug here must never fail — or even pause — a real campaign.
        """
        from src.binder_report import ReportError, build_report

        try:
            out = build_report(binder_dir, cfg=self.config)
        except ReportError as exc:
            logger.info(f"campaign report not generated yet for {binder_dir}: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001 - reporting must never fail the run
            logger.warning(f"campaign report generation failed for {binder_dir}: {exc}")
            return None
        logger.info(f"campaign report -> {out}")
        return out

    def _run_binder_track(
        self,
        query: str,
        run_dir: Path,
        result: PipelineResult,
        *,
        start_from: str = "target_intel",
        context_file: Path | None = None,
        auto_mode: bool = True,
        target: str | None = None,
        attach: bool = True,
        n_batches: int | None = None,
    ) -> PipelineResult:
        """
        Target name -> ranked binders, on the local GPU.

        Positional guards live inside this method, so `run()`'s PPI state
        machine is untouched.
        """
        from src.structure_trim import load_mapping

        try:
            start_idx = self.BINDER_STAGE_ORDER.index(start_from)
        except ValueError:
            raise PipelineError(
                f"Unknown binder start_from {start_from!r}; must be one of "
                f"{self.BINDER_STAGE_ORDER}")

        dirs = self._binder_dirs(run_dir)
        binder_dir = dirs["binder"]
        H = {s: self._load_binder_handoff(binder_dir, s)
             for s in self.BINDER_STAGE_ORDER}
        target_name = target or H["target_intel"].get("target_gene") or query

        try:
            # ── B0: target intelligence ─────────────────────────────────────
            if start_idx <= 0:
                H["target_intel"] = self._stage_target_intel(
                    query, target_name, dirs, result)
                if H["target_intel"].get("go_recommendation") == "NO_GO":
                    result.go_recommendation = "NO_GO"
                    result.go_rationale = H["target_intel"].get("go_rationale", "")
                    logger.warning(f"target-intel says NO_GO: {result.go_rationale}")
                    return result
                if not auto_mode:
                    raise PipelinePausedError("target_choice", {
                        "pdb_id": H["target_intel"].get("pdb_id"),
                        "partner": H["target_intel"].get("partner_name"),
                        "rationale": H["target_intel"].get("interface_rationale"),
                        "alternatives": H["target_intel"].get("alternatives_json"),
                    })
            intel = H["target_intel"]
            result.pdb_id = result.pdb_id or intel.get("pdb_id")

            # ── Site trials: compare epitopes by measured yield ─────────────
            # Reasoning cannot settle which of two defensible sites is more
            # designable; a few hundred backbones each can.
            if self._trial_sites > 1 or self._stop_after in ("trial", "spec"):
                sites = self._binder_sites(intel, limit=self._trial_sites)
                trials = self._run_site_trials(
                    intel, sites, dirs, result, attach=attach,
                    trial_backbones=self._trial_backbones,
                    escalate_to=self._escalate_to)
                self._write_trial_comparison(trials, dirs, result)
                result.stages_completed.append("site_trials")
                for t in trials:
                    if t.get("calibration") is not None:
                        self._generate_binder_report(t["dirs"]["binder"])
                if self._stop_after in ("trial", "spec"):
                    logger.info("stopping after the design trial, as requested")
                    return result
                ok = [t for t in trials if t.get("calibration") is not None]
                if not ok:
                    result.go_recommendation = "NO_GO"
                    result.go_rationale = "every site trial failed"
                    return result
                best = max(ok, key=lambda t: t["calibration"].backbone_rate.p_hat)
                logger.info(f"carrying site {best['site_id']} forward")
                intel = {**intel, **{
                    "pdb_id": best["site"].get("pdb_id"),
                    "target_chain": best["site"].get("target_chain"),
                    "partner_chain": best["site"].get("partner_chain"),
                }}
                dirs = best["dirs"]
                result.pdb_id = best["site"].get("pdb_id")
                start_idx = 6           # straight to production for the winner
                spec_path = best["spec"]
                trim = best["trim"]
                calib = {"result": best["calibration"],
                         "n_batches": best.get("n_batches")}

            # ── B1: interface + model-ready hotspots ────────────────────────
            if start_idx <= 1:
                H["interface"], hotspots_json = self._stage_binder_interface(
                    intel, dirs, result)
            else:
                text_path = binder_dir / self._BINDER_STAGE_FILES["interface"]
                hotspots_json = (
                    self._parse_hotspot_residues(
                        text_path.read_text(encoding="utf-8"), H["interface"])
                    if text_path.exists() else None)
                if not hotspots_json:
                    raise PipelineError(
                        f"resuming at {start_from!r} needs the hotspot table from "
                        f"{text_path}, which is missing or unparseable")
                result.hotspot_residues_json = hotspots_json

            # ── B2: domain-aware trim ───────────────────────────────────────
            if start_idx <= 2:
                trim = self._stage_trim(intel, hotspots_json, dirs, result)["result"]
            else:
                trim = _TrimFromDisk(load_mapping(dirs["trim"] / "trim_map.json"))

            # ── B3: RFD3 spec ───────────────────────────────────────────────
            if start_idx <= 3:
                spec_path = self._stage_binder_spec(
                    intel, hotspots_json, trim, dirs, result)
            else:
                specs = sorted(dirs["spec"].glob("*.json"))
                if not specs:
                    raise PipelineError(f"no RFD3 spec in {dirs['spec']}")
                spec_path = specs[0]

            # ── B4: pilot — proves the spec runs before anything big ────────
            if start_idx <= 4:
                self._run_gpu_stage("pilot", spec_path, trim, dirs, result,
                                    attach=attach, n_batches=n_batches)

            # ── B5: calibration — MEASURE the scale production needs ────────
            calib = locals().get("calib")
            if start_idx <= 5:
                calib = self._stage_calibration(spec_path, trim, dirs, result,
                                                attach=attach,
                                                n_batches=n_batches)
                verdict = calib["result"].verdict
                if verdict in ("ITERATE", "STOP"):
                    result.go_recommendation = "NO_GO"
                    result.go_rationale = calib["result"].verdict_reason
                    logger.warning(
                        f"calibration says {verdict}; not scaling up. "
                        f"{calib['result'].verdict_reason}")
                    # Still score and rank what the calibration produced — an
                    # ITERATE round has real designs worth looking at.
                    scored = self._stage_binder_scoring(dirs, result)
                    self._stage_binder_summary(scored["top_k"], intel, dirs, result)
                    self._generate_binder_report(dirs["binder"])
                    return result
                if not auto_mode or self._stop_after == "calibration":
                    raise PipelinePausedError("calibration_verdict", {
                        "verdict": verdict,
                        "reason": calib["result"].verdict_reason,
                        "est_gpu_hours": calib["result"].pessimistic.est_gpu_hours,
                        "est_disk_gb": calib["result"].pessimistic.est_disk_gb,
                        "resume": "--start-from production",
                    })

            # ── B6: production, sized by the calibration ────────────────────
            if start_idx <= 6:
                prod_n_batches, prod_compute = self._resolve_production_plan(
                    calib, dirs, n_batches)
                self._run_gpu_stage(
                    "production", spec_path, trim, dirs, result, attach=attach,
                    n_batches=prod_n_batches, compute_override=prod_compute)

            # ── B7: score + rank ────────────────────────────────────────────
            if start_idx <= 7:
                scored = self._stage_binder_scoring(dirs, result)
            else:
                scored = {"top_k": dirs["scoring"] / "top_k.csv"}

            # ── B8: analyst review ──────────────────────────────────────────
            if start_idx <= 8:
                H["binder_summary"] = self._stage_binder_summary(
                    scored["top_k"], intel, dirs, result)

            self._generate_binder_report(dirs["binder"])

        except PipelinePausedError:
            raise
        except PipelineBlockedError:
            raise
        except Exception as exc:
            result.error = str(exc)
            logger.error(f"Binder pipeline error: {exc}")
            raise

        return result

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    def _stage_pathway(self, query: str, run_dir: Path, result: PipelineResult) -> dict[str, str]:
        output_file = run_dir / "00_pathway.md"
        skill = "wildcard-expert" if self._pathway_mode == "wildcard" else "pathway-expert"
        logger.info(f"Stage 0: {skill}")
        handoff = self._run_stage(skill, query, [], output_file, stage="pathway")
        result.stages_completed.append("pathway")
        result.stage_files["pathway"] = output_file
        result.pathway_handoff = handoff       # persist so later stages can read structure_query, etc.
        result.pdb_id = result.pdb_id or handoff.get("pdb_id")
        result.target_complex = handoff.get("target_complex")
        return handoff

    def _stage_structure(
        self,
        prev_handoff: dict,
        run_dir: Path,
        result: PipelineResult,
        context_files: list[Path],
    ) -> dict[str, str]:
        output_file = run_dir / "02_structure.md"
        # After the chunk-5 reorder, structure runs AFTER literature, so
        # prev_handoff is the literature handoff (carries target_site_hint).
        # Pathway info we still need (structure_query, choices_json) lives on
        # result.pathway_handoff, persisted by _stage_pathway.
        pathway_handoff = result.pathway_handoff or {}
        literature_handoff = prev_handoff or {}
        pdb_id = result.pdb_id or pathway_handoff.get("pdb_id", "")
        structures_dir = _ROOT / self.config.get("paths", {}).get("structures_dir", "data/structures")
        asu_path = structures_dir / f"{pdb_id.upper()}.cif"
        ba1_path = structures_dir / f"{pdb_id.upper()}_ba1.cif"

        # Prefer biological assembly 1: it contains only the physiological complex,
        # eliminating crystal-contact chains that mislead chain selection.
        analysis_path = ba1_path if ba1_path.exists() else asu_path
        using_ba1 = analysis_path == ba1_path
        if using_ba1:
            logger.info(f"  Using biological assembly 1 for structure analysis: {ba1_path}")

        # ─────────────────────────────────────────────────────────────────────
        # CHAIN ASSIGNMENT CONVENTION
        # ---------------------------
        # Chain identity (which mmCIF chain is the design target vs. partner)
        # is determined HERE, deterministically, from the mmCIF entity
        # descriptions + residue counts. The structure-analysis stage is the
        # single source of truth for chain letters; downstream stages read
        # chain IDs out of its PIPELINE HANDOFF.
        #
        # Upstream skills (pathway-expert, molecular-biology-expert,
        # wildcard-expert, ...) MUST NOT emit chain letters in their handoff
        # `structure_query` / `design_query` fields. Their SKILL.md templates
        # were corrected to drop `target chain {chain}` placeholders, because
        # at those stages the LLM has not inspected the mmCIF and any chain
        # claim is a guess — observed failure mode: a pathway-expert run on
        # 3KYS hallucinated "Chain B = TEAD1, Chain A = YAP1" (reversed), and
        # the structure-expert had to spend tokens reconciling the conflict.
        #
        # When adding a new upstream skill that talks about a structure,
        # follow the same rule: name proteins, not chain letters.
        # ─────────────────────────────────────────────────────────────────────
        chain_descs = self._chain_entity_descriptions(analysis_path)
        chain_counts = self._count_chain_residues(analysis_path)
        target_complex = result.target_complex or prev_handoff.get("target_complex", "the target complex")

        # Surface size constraints to the LLM so it can reject oversized chains
        # or recommend cropping. The pipeline does not deterministically fail
        # here — the LLM is in a better position to judge whether a 600-residue
        # protein can be cropped to its binding domain.
        constraints_cfg = self.config.get("design", {}).get("constraints", {})
        max_target = int(constraints_cfg.get("max_target_residues", 500))
        warn_target = int(constraints_cfg.get("target_residues_warn", 250))

        chain_hint = ""
        if chain_descs or chain_counts:
            lines: list[str] = []
            for ch in sorted(set(chain_descs) | set(chain_counts)):
                desc = chain_descs.get(ch, "")
                n = chain_counts.get(ch)
                size_tag = f" [{n} residues]" if n is not None else ""
                lines.append(f"  Chain {ch}: {desc}{size_tag}")
            chain_hint = (
                "\n\nChain entity descriptions and sizes from the mmCIF header "
                f"({'biological assembly 1' if using_ba1 else 'asymmetric unit'}):\n"
                + "\n".join(lines)
                + "\n\n"
                f"**Target size policy** (from config.design.constraints): the chain "
                f"selected as the design target must be ≤ {max_target} residues; "
                f"≤ {warn_target} is preferred. If no candidate chain fits, recommend "
                f"cropping to the binding domain or selecting a different PDB rather "
                f"than proceeding.\n"
                f"\nSelect the chains that form the biologically relevant "
                f"{target_complex} interface. "
                f"Do NOT analyse crystal-packing contacts between identical chain copies."
            )

        # Use the pathway handoff's structure_query only when its PDB matches the
        # user-selected PDB. If the user picked a different structure (non-primary
        # choice), look the selected choice up in choices_json so we can build a
        # rich, accurate query — this prevents the structure expert from reasoning
        # about the wrong complex when the file and the query disagree.
        handoff_pdb = pathway_handoff.get("pdb_id", "")
        handoff_query = pathway_handoff.get("structure_query")
        if handoff_query and handoff_pdb.upper() == pdb_id.upper():
            # Primary choice — use the full structure_query from the pathway handoff
            query = handoff_query.replace(str(asu_path), str(analysis_path))
            if str(analysis_path) not in query:
                query = query + f"\n\nStructure file to use: {analysis_path}"
            query += chain_hint
        else:
            # Non-primary choice: look up the matching entry in choices_json to get
            # the correct complex name, design_intent, and evidence context.
            query = self._build_structure_query_for_choice(
                pdb_id=pdb_id,
                target_complex=target_complex,
                analysis_path=analysis_path,
                prev_handoff=pathway_handoff,
                context_files=context_files,
            )
            query += chain_hint

        # Append literature-derived design_intent + target_site_hint so the
        # structure stage knows whether to run DISRUPT / STABILIZE /
        # INHIBIT_ACTIVE_SITE mode and which residues literature has flagged.
        # Prefer literature_handoff's design_intent (mol-bio may refine) over
        # pathway's original. Same for any target_site_hint.
        design_intent = (
            literature_handoff.get("design_intent")
            or pathway_handoff.get("design_intent")
            or "disrupt"
        )
        site_hint = literature_handoff.get("target_site_hint")
        query += f"\n\ndesign_intent: {design_intent}"
        if site_hint:
            query += f"\ntarget_site_hint: {site_hint}"

        # Inject the orchestrator's PDB-identity check result so the structure
        # expert can judge synonym ≠ paralog. The check is intentionally
        # strict (substring match); the LLM filters out benign name variants
        # like "Transcriptional enhancer factor TEF-1" ≡ TEAD1, while
        # rejecting real paralog mismatches like ENPP1 vs ENPP2.
        idc = result.pdb_identity_check or {}
        if idc:
            verdict = "PASS" if idc.get("ok") else "MISMATCH"
            query += (
                f"\n\n### Orchestrator PDB identity check: {verdict}\n"
                f"- Expected target: {idc.get('expected', '(unknown)')!r}\n"
                f"- Structure metadata: {idc.get('summary', '(no summary)')}\n"
                f"- Adjudication rule: a MISMATCH between expected target name and the canonical "
                f"RCSB entity description (e.g. 'YAP1' vs 'Yes-associated protein', "
                f"'TEAD1' vs 'TEF-1') is a SYNONYM — proceed and note the equivalence in your "
                f"report. A MISMATCH between different paralogs / family members (e.g. ENPP1 vs "
                f"ENPP2, JAK1 vs JAK2, TEAD1 vs TEAD4) is a REAL mismatch — emit NO_GO with the "
                f"expected and actual proteins named in your rationale."
            )

        logger.info("Stage 1: complex-structure-analysis")
        # Don't pass prior stage context: structure_query already contains everything
        # the skill needs, and the full pathway.md adds ~8k tokens per LLM call.
        handoff = self._run_stage("complex-structure-analysis", query, [], output_file,
                                  stage="structure")
        result.stages_completed.append("structure")
        result.stage_files["structure"] = output_file
        result.target_complex = handoff.get("target_complex") or result.target_complex
        result.structure_handoff = handoff  # persist so _stage_design can compare modalities

        # Extract MODEL-READY HOTSPOTS as JSON so the analysis stage can compute
        # hotspot-restricted SASA without re-reading the structure report.
        # Before parsing, auto-resolve any UNVERIFIED label_seq_id tokens via
        # gemmi (e.g. when the structure-tools tool_get_sequence_map call
        # failed inside the skill) and run a residue-name sanity check that
        # surfaces mouse/human numbering mismatches.
        try:
            structure_text = output_file.read_text(encoding="utf-8")
            target_chain = (handoff.get("target_chain", "")
                            or handoff.get("chain_a", "")
                            or "A")
            fixed_text, name_warnings = self._resolve_unverified_label_seq_ids(
                structure_text, analysis_path, target_chain,
            )
            if fixed_text != structure_text:
                output_file.write_text(fixed_text, encoding="utf-8")
                structure_text = fixed_text
                logger.info("  resolved UNVERIFIED label_seq_ids via gemmi auth→label map")
            for w in name_warnings:
                logger.warning(f"  ⚠ structure stage: {w}")
            hotspots_json = self._parse_hotspot_residues(structure_text, handoff)
            if hotspots_json:
                result.hotspot_residues_json = hotspots_json
                logger.info(f"  hotspot residues parsed: {len(json.loads(hotspots_json).get('residues', []))} residues")
        except Exception as exc:
            logger.warning(f"  could not parse hotspot residues from structure report: {exc}")

        return handoff

    def _stage_literature(
        self,
        prev_handoff: dict,
        run_dir: Path,
        result: PipelineResult,
        context_files: list[Path],
    ) -> dict[str, str]:
        output_file = run_dir / "01_literature.md"
        complex_name = result.target_complex or prev_handoff.get("target_complex", "the target complex")
        design_intent = prev_handoff.get("design_intent", "disrupt")

        query = prev_handoff.get("literature_query") or (
            f"Assess target feasibility for {complex_name}. "
            f"Design intent from pathway analysis: {design_intent}. "
            "Identify literature-validated key residues (interface hotspots for disrupt/stabilize, "
            "or catalytic / pocket residues for inhibit_active_site) and emit them in the "
            "target_site_hint handoff field so the structure stage can focus on them. "
            "Use DepMap / cluster tools if the target is in a cancer or disease pathway."
        )
        logger.info("Stage 1: molecular-biology-expert")
        # Literature now runs BEFORE structure, so there is no structure report to
        # cross-reference. Pass the pathway report only — it has the disease/pathway
        # context the mol-bio queries need.
        pathway_ctx = [f for f in [result.stage_files.get("pathway")] if f and f.exists()]
        handoff = self._run_stage("molecular-biology-expert", query, pathway_ctx, output_file,
                                  stage="literature")
        result.stages_completed.append("literature")
        result.stage_files["literature"] = output_file
        result.literature_handoff = handoff
        return handoff

    def _stage_design(
        self,
        prev_handoff: dict,
        run_dir: Path,
        result: PipelineResult,
        context_files: list[Path],
    ) -> None:
        design_dir = run_dir / "03_design_inputs"
        design_dir.mkdir(exist_ok=True)
        design_report = run_dir / "03_design_report.md"

        complex_name = result.target_complex or prev_handoff.get("target_complex", "the target complex")
        pdb_id = result.pdb_id or prev_handoff.get("pdb_id", "")
        modality = prev_handoff.get("modality", "cyclic_peptide or mini_protein")

        query = prev_handoff.get("design_query") or (
            f"Generate {modality} design inputs for {complex_name}, PDB {pdb_id}."
        )
        if str(design_dir) not in query:
            query += f"\n\nWrite all output files to: {design_dir}"

        # Surface modality disagreement between mol-bio (literature) and the
        # structure-analysis stage. Both can emit a `modality:` line in their
        # handoff; mol-bio reasons from prior-art affinity precedent, structure
        # reasons from interface BSA and patch geometry. They sometimes
        # disagree (e.g. mol-bio says cyclic_peptide because of a 31 nM probe
        # in the corpus; structure says mini_protein because BSA > 2000 Å²).
        # Without surfacing it, the design-script silently follows mol-bio's
        # recommendation. Let the design-script know and pick explicitly.
        struct_modality = ((result.structure_handoff or {}).get("modality") or "").strip().lower()
        lit_modality = (prev_handoff.get("modality") or "").strip().lower()
        if struct_modality and lit_modality and struct_modality != lit_modality:
            query += (
                f"\n\nMODALITY DISAGREEMENT — literature stage recommended "
                f"`{lit_modality}`; structure stage recommended `{struct_modality}`. "
                f"Pick one and justify briefly in your report (one sentence on which "
                f"signal you weighted higher: literature prior-art affinity, or "
                f"interface size / hotspot patch geometry)."
            )

        logger.info("Stage 4: protein-design-script")
        design_handoff = self._run_stage("protein-design-script", query, context_files,
                                         design_report, stage="design")
        result.stages_completed.append("design")
        result.stage_files["design"] = design_report
        result.design_files = [f for f in design_dir.iterdir() if f.is_file()]
        # If literature was skipped, pull go_recommendation from design handoff;
        # fall back to GO (design completing implies at least a conditional go-ahead).
        if result.go_recommendation == "INCOMPLETE":
            go = design_handoff.get("go_recommendation", "").upper().replace("-", "_")
            result.go_recommendation = go or "GO"

    # ------------------------------------------------------------------
    # Stage 4 — execution (workstation, deterministic; no LLM)
    # ------------------------------------------------------------------

    _MODALITY_TO_PROTOCOL = {
        "cyclic_peptide": "peptide-anything",
        "mini_protein":   "protein-anything",
        "either":         "protein-anything",  # default
    }

    def _find_design_yaml(self, run_dir: Path) -> tuple[Path, list[Path]]:
        """Pick the boltzgen YAML out of 03_design_inputs/.

        Prefers files matching ``*_boltzgen.yaml`` (the protein-design-script
        skill's convention). Falls back to ``*.yaml`` if no match.
        Raises :class:`PipelineError` if no YAML is present.

        Returns ``(picked, skipped)`` where ``picked`` is the YAML stage 4
        will actually execute and ``skipped`` is the list of additional
        YAMLs found but not run (multi-region case; stage 4 is single-YAML
        for now). Callers are expected to surface ``skipped`` so the
        analyst can flag the unsampled design space — see ``_stage_execution``
        which writes ``multi_region_skipped.txt`` and ``_stage_summary``
        which forwards that file to the design-analyst.
        """
        design_dir = run_dir / "03_design_inputs"
        if not design_dir.exists():
            raise PipelineError(
                f"design inputs directory missing: {design_dir}. "
                "Stage 3 must run before stage 4."
            )
        bg_yamls = sorted(design_dir.glob("*_boltzgen.yaml"))
        if not bg_yamls:
            bg_yamls = sorted(p for p in design_dir.glob("*.yaml") if p.is_file())
        if not bg_yamls:
            raise PipelineError(
                f"no design YAML found under {design_dir}. "
                "Check stage 3 (protein-design-script) output."
            )
        picked = bg_yamls[0]
        skipped = bg_yamls[1:]
        if skipped:
            logger.warning(
                f"  multiple design YAMLs found ({[p.name for p in bg_yamls]}) — "
                f"using first: {picked.name}. Multi-region runs are not "
                "yet supported by the execution stage; remaining YAMLs will "
                "be recorded in 04_execution_outputs/multi_region_skipped.txt "
                "and surfaced in the design-analyst report."
            )
        return picked, skipped

    def _stage_execution(
        self,
        prev_handoff: dict,
        run_dir: Path,
        result: PipelineResult,
        *,
        force_production: bool = False,
    ) -> Path:
        """Run boltzgen on the workstation: pilot → gate → production.

        Reads ``design.workstation.*`` and ``design.pilot|production.*`` from
        config. Writes outputs under ``<run_dir>/04_execution_outputs/`` and
        a markdown summary at ``<run_dir>/04_execution.md``.

        Raises :class:`PipelinePausedError` (``pilot_failed``) when the pilot
        gate fails and ``force_production`` is False — the caller / web UI
        is expected to either re-tune hotspots (and re-run from earlier) or
        retry this stage with ``force_production=True``.

        Returns the boltzgen output directory for downstream consumption by
        :meth:`_stage_analysis`.
        """
        logger.info("Stage 4: execution (boltzgen on workstation)")
        design_cfg = self.config.get("design", {})
        ws_cfg = design_cfg.get("workstation", {})
        pilot_cfg = design_cfg.get("pilot", {})
        prod_cfg = design_cfg.get("production", {})

        yaml_path, skipped_yamls = self._find_design_yaml(run_dir)
        bg_output = run_dir / "04_execution_outputs"

        # Stage 4 is single-YAML for now (no multi-region orchestration).
        # When stage 3 emitted multiple YAMLs (typically Region 1 + Region 2
        # for a wide interface), write a metadata file the analyst can read.
        # Without this surfacing the half-sampled design space goes unnoticed.
        if skipped_yamls:
            bg_output.mkdir(parents=True, exist_ok=True)
            skipped_path = bg_output / "multi_region_skipped.txt"
            skipped_lines = [
                "Stage 4 executes a single design YAML; the following were generated",
                "by stage 3 (protein-design-script) but NOT run. The corresponding",
                "design space is unsampled. To sample it, run boltzgen manually on",
                "each YAML, place the outputs in a parallel run directory, and",
                "re-run stage 5 ranking against the combined set.",
                "",
                f"Executed YAML : {yaml_path.name}",
                "Skipped YAMLs :",
                *[f"  - {p.name}" for p in skipped_yamls],
            ]
            skipped_path.write_text("\n".join(skipped_lines) + "\n", encoding="utf-8")
            logger.warning(f"  wrote multi-region notice → {skipped_path}")

        # Map modality → protocol. modality lives in the design handoff.
        modality = (prev_handoff.get("modality") or "either").strip().lower()
        protocol = self._MODALITY_TO_PROTOCOL.get(modality, "protein-anything")
        logger.info(f"  modality={modality} → protocol={protocol}")
        logger.info(f"  yaml={yaml_path.name}  output={bg_output}")

        executable = ws_cfg.get("boltzgen_executable", "boltzgen")

        # `boltzgen check` first — abort fast on a malformed YAML.
        try:
            validate_yaml(yaml_path, executable=executable)
        except BoltzGenValidationError as exc:
            raise PipelineError(f"design YAML failed boltzgen check:\n{exc}") from exc

        try:
            pilot, prod = run_pilot_then_production(
                yaml_path=yaml_path,
                output_dir=bg_output,
                protocol=protocol,
                pilot_num_designs=int(pilot_cfg.get("num_designs", 1000)),
                pilot_budget=int(pilot_cfg.get("budget", 30)),
                production_num_designs=int(prod_cfg.get("num_designs", 20000)),
                production_budget=int(prod_cfg.get("budget", 100)),
                min_completion_rate=float(pilot_cfg.get("min_completion_rate", 0.5)),
                min_final_fill_rate=float(pilot_cfg.get("min_final_fill_rate", 0.5)),
                executable=executable,
                cuda_device=ws_cfg.get("cuda_device", 0),
                timeout_h=float(ws_cfg.get("timeout_hours", 24.0)),
                force_production=force_production,
            )
        except BoltzGenRunError as exc:
            raise PipelineError(f"boltzgen run failed during execution stage:\n{exc}") from exc

        # Write the markdown report regardless of pilot outcome so reruns
        # have a paper trail.
        report = run_dir / "04_execution.md"
        report.write_text(
            self._render_execution_report(yaml_path, protocol, bg_output, pilot, prod),
            encoding="utf-8",
        )
        result.stage_files["execution"] = report

        if prod is None:
            # Pilot gate failed and force_production=False — pause for user.
            raise PipelinePausedError("pilot_failed", {
                "pilot_completion_rate": pilot.completion_rate,
                "pilot_final_fill_rate": pilot.final_fill_rate,
                "gate_reason": pilot.gate_reason,
                "pilot_log": str(pilot.log_path),
                "output_dir": str(bg_output),
                "yaml_path": str(yaml_path),
            })

        result.stages_completed.append("execution")
        logger.info(f"  execution complete: production output at {bg_output}")
        return bg_output

    @staticmethod
    def _render_execution_report(
        yaml_path: Path,
        protocol: str,
        bg_output: Path,
        pilot,
        prod,
    ) -> str:
        """Human-readable summary of the execution stage outcome."""
        lines = [
            "# Stage 4 — Execution report",
            "",
            f"- design YAML: `{yaml_path}`",
            f"- protocol:    `{protocol}`",
            f"- output dir:  `{bg_output}`",
            "",
            "## Pilot",
            f"- requested: {pilot.num_designs_requested} designs, budget {pilot.budget_requested}",
            f"- completed: {pilot.total_completed}  ({pilot.completion_rate:.1%})",
            f"- final-fill: {pilot.final_count}/{pilot.budget_requested}  ({pilot.final_fill_rate:.1%})",
            f"- runtime: {pilot.runtime_s:.0f}s",
            f"- gate: {'PASS' if pilot.passes_gate else 'FAIL'} — {pilot.gate_reason}",
            f"- log: `{pilot.log_path}`",
        ]
        if prod is None:
            lines += [
                "",
                "## Production",
                "**Not run** — pilot gate failed.",
            ]
        else:
            lines += [
                "",
                "## Production",
                f"- requested: {prod.num_designs_requested} designs, budget {prod.budget_requested}",
                f"- runtime: {prod.runtime_s:.0f}s",
                f"- exit: rc={prod.returncode}",
                f"- log: `{prod.log_path}`",
            ]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # Stage 5 — analysis (deterministic; no LLM)
    # ------------------------------------------------------------------

    def _stage_analysis(self, run_dir: Path, result: PipelineResult) -> None:
        """Parse boltzgen outputs → enrich top-K with hotspot SASA → rank.

        Writes:

        * ``05_metrics_enriched.csv`` — every design with SASA columns
          appended (top-K is enriched; the tail has empty lpt_ cells).
        * ``05_ranking/ranked.csv``    — filter survivors, composite-ranked.
        * ``05_ranking/top_k.csv``     — MMR-diversified top-K.
        * ``05_ranking/filter_stats.txt`` — drop reasons.
        * ``05_analysis.md``           — markdown summary.
        """
        logger.info("Stage 5: analysis (deterministic, no LLM)")
        design_cfg = self.config.get("design", {})
        thresholds = design_cfg.get("thresholds", {})
        ranking_cfg = design_cfg.get("ranking", {})
        weights = ranking_cfg.get("weights", {})
        mmr = ranking_cfg.get("mmr", {})
        top_k = int(ranking_cfg.get("top_k", 20))
        enrich_top_k = int(ranking_cfg.get("enrich_top_k", 200))

        pyr_cfg = design_cfg.get("pyrosetta", {})
        ws_cfg = design_cfg.get("workstation", {})

        bg_output = run_dir / "04_execution_outputs"
        if not bg_output.exists():
            raise PipelineError(
                f"execution output dir missing: {bg_output}. "
                "Stage 4 must run before stage 5."
            )

        # Recover target chain + hotspot residues. hotspot_residues_json was
        # populated in _stage_structure when the report had a MODEL-READY
        # HOTSPOTS table; without it we can still rank on iptm/ipae but
        # hotspot SASA filtering will drop everything.
        if not result.hotspot_residues_json:
            raise PipelineError(
                "no hotspot residues parsed from the structure stage — cannot "
                "compute target-hotspot SASA. Re-run from start_from=structure "
                "with a structure report that includes a MODEL-READY HOTSPOTS table."
            )
        hs_data = json.loads(result.hotspot_residues_json)
        target_chain = hs_data["target_chain"]
        hotspots = hs_data["residues"]
        binder_chain = ws_cfg.get("binder_chain", "B")
        logger.info(
            f"  target_chain={target_chain}  binder_chain={binder_chain}  "
            f"hotspots={len(hotspots)}"
        )

        # 1. Parse boltzgen output
        records = parse_boltzgen_outputs(bg_output)

        # IMPORTANT — boltzgen renumbers the target chain in its output CIFs.
        # Specifically, the original mmCIF `label_seq` becomes the new
        # `auth_seq_id`. So if the original 3KYS PHE314 had label_seq=122,
        # the boltzgen output CIF has that residue at auth_seq_id=122, not
        # 314. The SASA worker looks up residues by auth_seq_id via
        # pdb_info.number(), so we must rewrite each hotspot's auth_seq_id
        # to its original label_seq_id before passing to the worker.
        #
        # Without this remap, every hotspot is "missing" in the boltzgen
        # output (auth=314 doesn't exist when output runs 3..217) and the
        # worker reports sasa_delta=0.0 across the board — exactly the
        # YAP-TEAD off-target signature.
        hotspots_remapped = []
        any_remapped = False
        for hs in hotspots:
            auth = int(hs["auth_seq_id"])
            label = hs.get("label_seq_id")
            if label is None or label == auth:
                # Either no label resolution available or original CIF was
                # already label-aligned (label_seq == auth_seq, common for
                # de-novo designed targets and some experimental structures).
                hotspots_remapped.append(hs)
                continue
            remapped = dict(hs)
            remapped["auth_seq_id"] = int(label)
            remapped["_original_auth_seq_id"] = auth  # kept for debug
            hotspots_remapped.append(remapped)
            any_remapped = True
        if any_remapped:
            logger.info(
                f"  remapped {sum(1 for h in hotspots if h.get('label_seq_id') != h['auth_seq_id'])} "
                f"hotspot auth_seq_ids → original label_seq_ids for "
                f"BoltzGen-renumbered output CIFs"
            )

        # 2. Enrich top-K by quality_score with hotspot SASA.
        # We sort records in-place by quality_score (descending) so the
        # first N get enriched. Records without quality_score sort last.
        records.sort(
            key=lambda r: (r.get("quality_score") if r.get("quality_score") is not None else -1.0),
            reverse=True,
        )
        enrich_with_hotspot_sasa(
            records,
            target_chain=target_chain,
            binder_chain=binder_chain,
            hotspots=hotspots_remapped,
            python_executable=pyr_cfg["python_executable"],
            init_flags=pyr_cfg.get("init_flags"),
            max_designs=enrich_top_k,
        )

        # Write enriched CSV before ranking so the artifact survives a
        # later ranking-time crash.
        enriched_path = run_dir / "05_metrics_enriched.csv"
        write_enriched_csv(records, enriched_path)

        # 3. Rank
        ranking = rank_designs(
            records,
            thresholds=thresholds,
            weights=weights,
            mmr=mmr,
            top_k=top_k,
        )

        ranking_dir = run_dir / "05_ranking"
        ranked_p, top_k_p, stats_p = write_ranking_outputs(ranking, ranking_dir)

        # 4. Markdown summary
        report = run_dir / "05_analysis.md"
        report.write_text(
            self._render_analysis_report(
                bg_output=bg_output,
                hotspots=hotspots,
                target_chain=target_chain,
                binder_chain=binder_chain,
                ranking=ranking,
                enriched_path=enriched_path,
                ranked_path=ranked_p,
                top_k_path=top_k_p,
                stats_path=stats_p,
            ),
            encoding="utf-8",
        )
        result.stage_files["analysis"] = report
        result.stages_completed.append("analysis")
        logger.info(
            f"  analysis complete: {ranking.filter_stats.n_survivors} survivors, "
            f"top-K={len(ranking.top_k)} written to {ranking_dir}"
        )

    # ------------------------------------------------------------------
    # Stage 6 — summary (terminal LLM stage)
    # ------------------------------------------------------------------

    # Columns we slice out of top_k.csv before passing to the analyst skill.
    # The full ranked CSV is ~300 columns wide; the LLM only needs these to
    # form a verdict. Notably absent: `designed_chain_sequence`. Protein
    # sequences are deliberately withheld from the LLM (a) because they
    # trigger biosafety refusals on Sonnet-class models and (b) because
    # the review task — composite_score + iPTM/iPAE/SASA — doesn't need
    # them. `binder_length` is computed from the sequence and stamped in
    # so the LLM can still reason about size.
    _SUMMARY_CONTEXT_COLS = (
        "design_id",
        "mmr_rank",
        "composite_rank",
        "composite_score",
        "design_to_target_iptm",
        "min_design_to_target_pae",
        "lpt_hotspot_sasa_delta",
        "complex_plddt",
        "binder_length",
        "mmr_max_similarity",
        # BoltzGen's developability / synthesis-risk signals. liability_score is
        # a composite of cleavage motifs, oxidation hotspots, hydrophobic
        # patches etc.; the high-severity count is the most important — those
        # are the violations that would degrade a cyclic peptide in serum
        # (DPP4 sites, ProtTryp sites) or fail at synthesis (Asp-Pro cleavage,
        # disulfide misassembly). The analyst weighs these alongside binding
        # metrics so the recommended designs are actually orderable.
        "liability_score",
        "liability_high_severity_violations",
        "liability_num_violations",
    )

    def _stage_summary(
        self,
        prev_handoff: dict,
        run_dir: Path,
        result: PipelineResult,
    ) -> None:
        """Final LLM review of the top-K plus order-ready FASTA writeout.

        Reads ``05_ranking/top_k.csv``, slices it to the columns the
        design-analyst skill cares about, hands it to the skill as context,
        and writes the resulting markdown to ``06_summary.md``.
        Also writes ``06_top_k.fasta`` deterministically (not LLM-generated)
        so the human always has a clean orderable artifact even if the LLM
        botches the FASTA in its report.
        """
        logger.info("Stage 6: summary (design-analyst)")

        top_k_path = run_dir / "05_ranking" / "top_k.csv"
        if not top_k_path.exists():
            raise PipelineError(
                f"top_k.csv missing at {top_k_path}. Stage 5 must run before stage 6."
            )

        # Build slim context_text. If the CSV is empty (no survivors), still
        # run the skill — it should produce a NO_GO verdict in that case.
        context_text = self._slim_top_k_for_context(top_k_path)

        # Recover modality from the design stage's handoff (passed in via
        # prev_handoff) or, failing that, by re-parsing 03_design_report.md.
        modality = (prev_handoff.get("modality") or "").strip().lower()
        if not modality:
            design_report = run_dir / "03_design_report.md"
            if design_report.exists():
                modality = (
                    self._parse_handoff(design_report.read_text(encoding="utf-8"))
                    .get("modality", "")
                    .strip()
                    .lower()
                )
        modality = modality or "mini_protein"  # default if all else fails

        # Build the human-readable hotspot summary for the query.
        hotspots: list[str] = []
        if result.hotspot_residues_json:
            try:
                hs = json.loads(result.hotspot_residues_json)
                hotspots = [
                    f"{r.get('residue', '?')}{r['auth_seq_id']}"
                    for r in hs.get("residues", [])
                ]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning(f"  could not parse hotspot_residues_json for summary: {exc}")

        target_complex = result.target_complex or prev_handoff.get("target_complex", "the target complex")

        # Surface the multi-region skipped metadata (if stage 4 found multiple
        # YAMLs and ran only the first). The analyst SKILL.md instructs the
        # model to flag this in section 3 — without it, half-sampled design
        # campaigns get a clean GO verdict without anyone noticing.
        skipped_path = run_dir / "04_execution_outputs" / "multi_region_skipped.txt"
        multi_region_note = ""
        if skipped_path.exists():
            multi_region_note = (
                f"\n**Multi-region status (surface this in your section 3 as a red flag):**\n"
                f"```\n{skipped_path.read_text(encoding='utf-8').rstrip()}\n```\n"
            )

        # The slim CSV goes *inside* the query (not as context_text) so the
        # model sees one coherent user message — passing tabular data as a
        # "## Context from prior report" block confused both Sonnet (which
        # refused) and Haiku (which hallucinated numbers).
        query = (
            f"Review the metrics table below and produce the structured "
            f"candidate review specified in your system prompt.\n\n"
            f"Run inputs:\n"
            f"- Target complex: {target_complex}\n"
            f"- Design intent: {prev_handoff.get('design_intent') or 'design binders that disrupt the target interface'}\n"
            f"- Modality: {modality}\n"
            f"- Hotspot residues ({len(hotspots)}): "
            + (", ".join(hotspots) if hotspots else "_(none recorded)_")
            + "\n"
            + multi_region_note
            + "\n"
            f"Metrics table for the MMR-selected top-K (read every value from "
            f"this CSV — do not invent or estimate numbers):\n\n"
            f"{context_text}\n"
            f"Artifact paths (quote these verbatim in your section 6):\n"
            f"- top_k.csv: `{top_k_path}`\n"
            f"- ranked.csv: `{run_dir / '05_ranking' / 'ranked.csv'}`\n"
            f"- filter_stats.txt: `{run_dir / '05_ranking' / 'filter_stats.txt'}`\n"
            f"- order-ready FASTA: `{run_dir / '06_top_k.fasta'}` (written by orchestrator)\n"
            f"- per-design CIFs: `{run_dir / '04_execution_outputs' / 'intermediate_designs'}/`\n"
        )

        output_file = run_dir / "06_summary.md"
        # Empty context_files — the data lives inline in the query above.
        handoff = self._run_stage("design-analyst", query, [], output_file, stage="summary")
        result.stage_files["summary"] = output_file
        result.stages_completed.append("summary")

        # Always write a deterministic FASTA — independent of whatever the
        # LLM put in its report — so the human has an unambiguous file to
        # send to the synthesis vendor.
        fasta_path = self._write_top_k_fasta(top_k_path, run_dir / "06_top_k.fasta", modality)
        logger.info(f"  wrote deterministic FASTA → {fasta_path}")

        # Let the final verdict propagate to result.go_recommendation. The
        # skill's handoff is the authoritative source at this stage.
        go = (handoff.get("go_recommendation") or "").upper().replace("-", "_")
        if go in ("GO", "CONDITIONAL_GO", "NO_GO"):
            result.go_recommendation = go
        if handoff.get("go_rationale"):
            result.go_rationale = handoff["go_rationale"]

    @staticmethod
    def _slim_top_k_for_context(top_k_csv: Path) -> str:
        """Return a CSV string containing only the columns the analyst skill
        needs. Drops any column not in _SUMMARY_CONTEXT_COLS so we don't
        blow context with z-scores and per-design metric noise.
        """
        import csv as _csv
        import io as _io

        with top_k_csv.open() as f:
            reader = _csv.DictReader(f)
            available = set(reader.fieldnames or ())
            cols = [c for c in PipelineRunner._SUMMARY_CONTEXT_COLS if c in available or c == "binder_length"]
            # binder_length is derived from designed_chain_sequence (which we
            # don't pass through). Keep it in cols only if the source CSV has
            # the sequence to derive from.
            include_length = "designed_chain_sequence" in available
            if not include_length:
                cols = [c for c in cols if c != "binder_length"]
            buf = _io.StringIO()
            writer = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            for row in reader:
                out = {c: row.get(c, "") for c in cols if c != "binder_length"}
                if include_length:
                    out["binder_length"] = len((row.get("designed_chain_sequence") or "").strip())
                writer.writerow(out)
        return f"```csv\n{buf.getvalue()}```\n"

    @staticmethod
    def _write_top_k_fasta(top_k_csv: Path, fasta_path: Path, modality: str) -> Path:
        """Emit FASTA from top_k.csv ordered by mmr_rank.

        Header per record:
            >design_NN mmr_rank=N comp=X iptm=X ipae=X sasa_delta=X
        Last header line includes the modality so downstream tooling
        (synthesis vendor portals, etc.) sees it without re-reading the
        markdown report.
        """
        import csv as _csv

        rows: list[dict] = []
        with top_k_csv.open() as f:
            for row in _csv.DictReader(f):
                rows.append(row)

        def _rank_key(r: dict) -> int:
            try:
                return int(r.get("mmr_rank") or 999999)
            except (TypeError, ValueError):
                return 999999

        rows.sort(key=_rank_key)

        def _fmt(value: str, ndigits: int) -> str:
            try:
                return f"{float(value):.{ndigits}f}"
            except (TypeError, ValueError):
                return "NA"

        lines: list[str] = [f"; modality={modality}", f"; n_designs={len(rows)}"]
        for i, r in enumerate(rows, start=1):
            seq = (r.get("designed_chain_sequence") or "").strip()
            if not seq:
                continue
            lines.append(
                f">design_{i:02d} "
                f"mmr_rank={r.get('mmr_rank', '?')} "
                f"comp={_fmt(r.get('composite_score', ''), 2)} "
                f"iptm={_fmt(r.get('design_to_target_iptm', ''), 3)} "
                f"ipae={_fmt(r.get('min_design_to_target_pae', ''), 2)} "
                f"sasa_delta={_fmt(r.get('lpt_hotspot_sasa_delta', ''), 1)}"
            )
            lines.append(seq)

        fasta_path.parent.mkdir(parents=True, exist_ok=True)
        fasta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return fasta_path

    @staticmethod
    def _render_analysis_report(
        *,
        bg_output: Path,
        hotspots: list[dict],
        target_chain: str,
        binder_chain: str,
        ranking,
        enriched_path: Path,
        ranked_path: Path,
        top_k_path: Path,
        stats_path: Path,
    ) -> str:
        stats = ranking.filter_stats
        lines = [
            "# Stage 5 — Analysis report",
            "",
            f"- boltzgen output: `{bg_output}`",
            f"- target chain: `{target_chain}`  binder chain: `{binder_chain}`",
            f"- hotspot residues ({len(hotspots)}): "
            + ", ".join(f"{h.get('residue','?')}{h['auth_seq_id']}" for h in hotspots),
            "",
            "## Funnel",
            f"- input designs: {stats.n_input}",
            f"- survived hard filters: {stats.n_survivors}",
            f"- MMR top-K: {len(ranking.top_k)}",
        ]
        if stats.dropped:
            lines.append("")
            lines.append("### Drop reasons")
            for reason, count in sorted(stats.dropped.items(), key=lambda kv: -kv[1]):
                lines.append(f"- {reason}: {count}")

        lines.append("")
        lines.append("## Top-K (by MMR rank)")
        if not ranking.top_k:
            lines.append("- _(none — no survivors after hard filters)_")
        else:
            lines.append("| mmr | comp_rank | comp_score | iptm | ipae | sasa_delta | sequence |")
            lines.append("|----:|---------:|---------:|----:|----:|----------:|:---------|")
            for r in ranking.top_k:
                seq = (r.get("designed_chain_sequence") or "")
                if len(seq) > 60:
                    seq = seq[:57] + "..."
                lines.append(
                    f"| {r.get('mmr_rank','?')} "
                    f"| {r.get('composite_rank','?')} "
                    f"| {r.get('composite_score', 0):+.2f} "
                    f"| {r.get('design_to_target_iptm', 0):.3f} "
                    f"| {r.get('min_design_to_target_pae', 0):.2f} "
                    f"| {r.get('lpt_hotspot_sasa_delta', 0):.1f} "
                    f"| `{seq}` |"
                )

        lines += [
            "",
            "## Artifacts",
            f"- enriched metrics: `{enriched_path}`",
            f"- ranked survivors: `{ranked_path}`",
            f"- top-K:            `{top_k_path}`",
            f"- filter stats:     `{stats_path}`",
            f"- composite columns used: `{ranking.composite_columns}`",
        ]
        return "\n".join(lines) + "\n"

    def _write_no_go_report(self, result: PipelineResult, handoff: dict) -> None:
        path = result.run_dir / "02_campaign_recommendation.md"
        complex_name = result.target_complex or handoff.get("target_complex", "Unknown")
        tractability = handoff.get("tractability", "Unknown")
        rationale = result.go_rationale or "See literature report for details."
        path.write_text(
            f"# Campaign Recommendation: NO-GO\n\n"
            f"**Target complex:** {complex_name}  \n"
            f"**PDB:** {result.pdb_id or 'N/A'}  \n"
            f"**Literature tractability:** {tractability}\n\n"
            f"## Decision Rationale\n{rationale}\n\n"
            f"## Next Steps\n"
            f"- Review `02_literature.md` for specific blockers\n"
            f"- Consider an alternative interface or target\n"
            f"- Re-run with `--start-from literature` after expanding the corpus\n",
            encoding="utf-8",
        )
        result.stage_files["recommendation"] = path
        logger.info(f"NO_GO report written to {path}")

    # ------------------------------------------------------------------
    # Core helpers
    # ------------------------------------------------------------------

    def _resolve_stage(
        self, skill_name: str, stage: str | None = None
    ) -> tuple[str, bool, str]:
        """
        Return (model_id, use_extended_thinking) for a given skill.

        Per-stage model overrides are keyed by stage name (pathway / structure /
        literature / design).  Extended thinking is silently ignored for Gemini.
        If an override specifies Haiku but extended thinking is requested, the
        model is auto-upgraded to Sonnet with a warning.

        `stage` should be passed explicitly whenever the caller knows it.  The
        skill -> stage inversion below is first-match-wins, so a skill reused by
        two stages (complex-structure-analysis serves both the PPI `structure`
        stage and the binder track's `interface` stage) would otherwise always
        resolve to whichever stage is declared first in _STAGE_TO_SKILL.
        """
        if stage is None:
            stage = _stage_for_skill(skill_name)
        # Lookup order: explicit user override → config models.<provider>.stages
        # → module fallback table → global default.
        per_stage_default = (
            (self._models_cfg.get("stages") or {}).get(stage)
            or _DEFAULT_STAGE_MODELS.get(self.provider, {}).get(stage)
        )
        model_id = self._stage_models.get(stage, per_stage_default or self._default_model)
        use_thinking = (
            self.provider == "claude"
            and stage in self._ext_thinking
        )
        # A per-stage override may name a different PROVIDER as "gemini:model".
        # That is how a stage whose prompt one provider's safety classifier
        # declines gets routed elsewhere without moving the whole pipeline.
        provider = self.provider
        if ":" in model_id:
            provider, model_id = model_id.split(":", 1)
            use_thinking = use_thinking and provider == "claude"

        if use_thinking and "haiku" in model_id.lower():
            upgrade = self._models_cfg.get("thinking_upgrade") or _THINKING_UPGRADE_MODEL
            logger.warning(
                f"Extended thinking requires a Sonnet-class model — auto-upgrading "
                f"{stage} stage from {model_id} to {upgrade}"
            )
            model_id = upgrade
        return model_id, use_thinking, provider

    def _run_stage(
        self,
        skill_name: str,
        query: str,
        context_files: list[Path],
        output_file: Path,
        *,
        stage: str | None = None,
    ) -> dict[str, str]:
        """
        Invoke one skill and return the parsed PIPELINE HANDOFF fields.

        `stage` names the pipeline stage this invocation belongs to.  Pass it
        whenever a skill is shared between stages; it defaults to the (ambiguous)
        skill -> stage inversion for backwards compatibility.
        """
        context_text: str | None = None
        if context_files:
            context_text = self._merge_context(*context_files)

        model_id, use_thinking, provider = self._resolve_stage(skill_name, stage)
        runner = SkillRunner(
            skill_name=skill_name,
            provider=provider,
            model_id=model_id,
            config=self.config,
            max_iter=self.max_iter,
            max_input_tokens=self.max_tokens,
            use_extended_thinking=use_thinking,
        )

        logger.info(f"  [{skill_name}] {query[:100]}{'...' if len(query) > 100 else ''}")

        # Budget guard. Refuse to START a stage whose projected cost would pass
        # the cap; record what it actually spent in `finally`, because a stage
        # that dies on iteration 25 of 30 still burned that money.
        stage_key = stage or _stage_for_skill(skill_name)
        projected_usd = 0.0
        if self._ledger is not None:
            from src.token_budget import price

            estimate = self._estimate_stage_usage(
                runner, query, context_text, model_id)
            projected_usd = price(model_id, estimate)
            try:
                self._ledger.preflight(stage=stage_key, model=model_id,
                                       estimated=estimate)
            except BudgetExceeded as exc:
                self._budget_pause(exc)
        try:
            try:
                output_text = runner.run(query, context_text=context_text)
            except SkillRefusedError as first_refusal:
                # Work down the fallback chain. A refusal is model- AND
                # query-dependent, so "another model declined too" is real
                # information and worth reporting rather than retrying forever.
                chain = [m for m in (self._models_cfg.get("refusal_fallbacks")
                                     or _REFUSAL_FALLBACK_MODELS)
                         if m != model_id]
                output_text, refusals = None, [first_refusal]
                for fallback in chain:
                    logger.warning(f"{refusals[-1]}. Retrying on {fallback}.")
                    if self._ledger is not None:
                        self._ledger.record(
                            stage=stage_key, skill=skill_name,
                            provider=provider, model=model_id,
                            usage=runner.usage(),
                            note=f"refused (category={refusals[-1].category})")
                    fb_provider, fb_model = (
                        fallback.split(":", 1) if ":" in fallback
                        else (provider, fallback))
                    runner = SkillRunner(
                        skill_name=skill_name, provider=fb_provider,
                        model_id=fb_model, config=self.config,
                        max_iter=self.max_iter, max_input_tokens=self.max_tokens,
                        use_extended_thinking=use_thinking,
                    )
                    model_id, provider = fb_model, fb_provider
                    try:
                        output_text = runner.run(query, context_text=context_text)
                        break
                    except SkillRefusedError as again:
                        refusals.append(again)
                if output_text is None:
                    tried = ", ".join(sorted({r.model for r in refusals}))
                    raise SkillRefusedError(
                        skill=skill_name, model=tried,
                        category=refusals[-1].category,
                        iteration=refusals[-1].iteration,
                    ) from first_refusal
        finally:
            if self._ledger is not None:
                entry = self._ledger.record(
                    stage=stage_key, skill=skill_name, provider=self.provider,
                    model=model_id, usage=runner.usage(),
                )
                if projected_usd:
                    ratio = projected_usd / max(entry.usd, 1e-9)
                    logger.info(
                        f"  [{stage_key}] estimate ${projected_usd:.4f} vs actual "
                        f"${entry.usd:.4f} ({ratio:.1f}x) — tune "
                        f"_STAGE_CALL_PRIOR / _HISTORY_GROWTH_PER_CALL if this "
                        f"is consistently off")

        # Verify corpus citations and append a summary section to the output.
        # This runs after every stage so hallucinated DOIs are flagged before
        # the file is written to disk and before the next stage reads it.
        citation_note = self._verify_citations(output_text, skill_name)
        if citation_note:
            output_text = output_text + citation_note

        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(output_text, encoding="utf-8")
        logger.info(f"  [{skill_name}] → {output_file} ({len(output_text):,} chars)")

        # Audit dump: full conversation history (thinking blocks, tool calls,
        # tool responses, final text) so a human can review after the run.
        if self._capture_traces:
            try:
                trace_dest = output_file.parent / "traces" / skill_name
                runner.write_trace(trace_dest)
            except Exception as exc:
                logger.warning(f"  [{skill_name}] trace dump failed: {exc}")

        handoff = self._parse_handoff(output_text)
        if handoff:
            logger.info(f"  [{skill_name}] handoff: {list(handoff.keys())}")
        else:
            logger.warning(
                f"  [{skill_name}] No '### PIPELINE HANDOFF' block found — "
                "next stage will use a fallback query"
            )

        # Mirror stage completion into the persistent project manifest, if one
        # is attached. The manifest is the filesystem/CLI source of truth for
        # stage state + artifact pointers (web.db stays authoritative for the
        # web UI). Best-effort: a manifest hiccup must never fail the pipeline.
        self._record_stage(skill_name, "complete", output_file, handoff, stage=stage)

        # A stage that overshot its own estimate should pause cleanly here
        # rather than mid-flight in the next one.
        if self._ledger is not None:
            try:
                self._ledger.check_cap(next_stage=f"after:{stage_key}")
            except BudgetExceeded as exc:
                self._budget_pause(exc)
        return handoff

    # Typical number of API calls a skill's agentic loop makes. Used only to
    # project a stage's cost BEFORE it runs; the recorded actuals are the truth.
    # Log the estimate/actual ratio over a few runs and re-tune these.
    _STAGE_CALL_PRIOR: dict[str, int] = {
        "pathway-expert": 12,
        "wildcard-expert": 18,
        "molecular-biology-expert": 10,
        "complex-structure-analysis": 14,
        "binder-target-intel": 4,
        "protein-design-script": 8,
        "design-analyst": 2,
    }
    # Observed mean visible output per call. Deliberately NOT max_tokens
    # (24 000) — projecting off the ceiling would refuse almost every stage.
    _OUTPUT_TOKENS_PER_CALL = 2500
    # How much the resent conversation grows per turn (the model's own output
    # plus the tool result that provoked it). The loop resends everything each
    # call, so total input is quadratic in the number of calls and this constant
    # dominates the projection. 3000 is calibrated against observed stage spend;
    # an earlier 8000 projected $3.14 for a single structure stage and refused
    # runs that in fact cost well under a dollar.
    _HISTORY_GROWTH_PER_CALL = 3000

    def _estimate_stage_usage(
        self,
        runner: SkillRunner,
        query: str,
        context_text: str | None,
        model_id: str,
    ) -> Usage:
        """
        Project a stage's token usage for the pre-flight budget check.

        Call 1 pays full price for the system prompt (and writes it to cache);
        calls 2..n read it back at ~0.1x while the message history grows. This
        is a guard, not accounting — any failure degrades to a rough character
        heuristic rather than blocking the run.
        """
        n_calls = self._STAGE_CALL_PRIOR.get(runner.skill_name, 8)
        first_input = context_text and len(context_text) or 0
        system_tokens = 0
        try:
            import anthropic

            client = anthropic.Anthropic()
            system_tokens = client.messages.count_tokens(
                model=model_id,
                system=[{"type": "text", "text": runner.system_prompt}],
                messages=[{"role": "user", "content": (context_text or "") + query}],
            ).input_tokens
        except Exception as exc:
            # ~4 chars/token is close enough for a ceiling check.
            system_tokens = (len(runner.system_prompt) + first_input + len(query)) // 4
            logger.debug(f"count_tokens unavailable ({exc}); using char heuristic")

        # Each turn re-sends every prior turn, so the total input across a stage
        # is quadratic in the call count. Per-call input is capped at the same
        # ceiling the runner enforces, so a long loop is projected as expensive
        # but not unboundedly so.
        history_growth = sum(
            min(self._HISTORY_GROWTH_PER_CALL * i, self.max_tokens)
            for i in range(1, n_calls)
        )
        return Usage(
            input_tokens=history_growth,
            cache_creation_tokens=system_tokens,
            cache_read_tokens=system_tokens * max(0, n_calls - 1),
            output_tokens=self._OUTPUT_TOKENS_PER_CALL * n_calls,
        )

    def _budget_pause(self, exc: BudgetExceeded) -> "NoReturn":
        """
        Convert a budget overrun into a resumable pause.

        Writes a manifest checkpoint carrying a working resume command, then
        raises PipelinePausedError.  A budget overrun must never abandon work
        already paid for, and must never kill a GPU campaign already running.
        """
        led = self._ledger
        resume_stage = exc.stage.removeprefix("after:")
        payload = {
            "spent_usd": round(exc.spent_usd, 6),
            "projected_usd": round(exc.projected_usd, 6),
            "cap_usd": exc.cap_usd,
            "next_stage": resume_stage,
            "by_stage": led.by_stage() if led else {},
            "resume": (
                f"scripts/run_pipeline.py --workflow {self._workflow} "
                + (f"--project {self._project.slug} " if self._project else "")
                + f"--start-from {resume_stage} --budget <a-larger-number>"
            ),
        }
        if self._project is not None and self._round_id is not None:
            try:
                self._project.set_checkpoint(
                    "budget_exceeded", self._round_id, resume_stage, "gate",
                    payload=payload,
                )
            except Exception as cp_exc:
                logger.warning(f"budget checkpoint failed: {cp_exc}")
        logger.error(str(exc))
        raise PipelinePausedError("budget_exceeded", payload) from exc

    def _init_ledger(self, run_dir: Path) -> None:
        """
        Attach the API-spend ledger.

        Lives at the PROJECT root when there is one, so the cap spans every
        round rather than resetting each time; otherwise beside the run output.
        Replaying the existing JSONL is what makes `--budget` cumulative across
        a resume instead of handing the run a fresh allowance.
        """
        if self._ledger is not None:
            return
        if self._project is not None:
            path = self._project.root / "ledger.jsonl"
            sink = self._project.set_budget
        else:
            path = run_dir / "ledger.jsonl"
            sink = None
        self._ledger = TokenLedger(
            path, cap_usd=self._budget_usd, mode=self._budget_mode,
            manifest_sink=sink,
        )
        if self._budget_usd is not None:
            logger.info(
                f"API budget: ${self._budget_usd:.2f} cap ({self._budget_mode} mode), "
                f"${self._ledger.spent_usd:.4f} already spent on this project"
            )

    def _record_stage(
        self,
        skill_name: str,
        status: str,
        output_file: Path | None = None,
        handoff: dict | None = None,
        *,
        stage: str | None = None,
        artifacts: list[Path] | None = None,
    ) -> None:
        """
        Mirror a stage's state into the project manifest (no-op without one).

        `stage` overrides the ambiguous skill -> stage inversion; `artifacts`
        overrides the single-output-file default so deterministic stages (which
        have no skill at all) can record several artifacts.  Deterministic
        stages should pass ``skill_name=stage``.
        """
        if self._project is None or self._round_id is None:
            return
        stage_name = stage or _stage_for_skill(skill_name)
        if artifacts is None:
            artifacts = [output_file] if output_file else None
        try:
            self._project.update_stage(
                self._round_id,
                stage_name,
                status,
                artifacts=artifacts,
                handoff=handoff,
            )
        except Exception as exc:
            logger.warning(f"  [{skill_name}] manifest update failed: {exc}")

    def _verify_citations(self, output_text: str, skill_name: str) -> str:
        """
        Extract DOI citations from a stage output and verify each against the
        fingerprint store.

        Logs a warning for every DOI not found in the corpus and returns a
        ``## CITATION VERIFICATION`` section (placed after the PIPELINE HANDOFF
        block so it never interferes with handoff parsing) suitable for
        appending to the output file.

        Returns an empty string when no DOIs are present in the text.
        """
        # Standard DOI prefix: 10.XXXX/... — match the identifier up to the
        # first whitespace or common punctuation that would follow a citation.
        doi_pattern = re.compile(r'\b(10\.\d{4,9}/[^\s,;:\)\]\}]+)')
        raw_matches = doi_pattern.findall(output_text)
        # Deduplicate while preserving order; strip trailing punctuation that
        # the regex may have captured (e.g. trailing period in a sentence).
        seen: set[str] = set()
        dois: list[str] = []
        for m in raw_matches:
            # Strip trailing punctuation that commonly follows a DOI in markdown:
            # backticks (inline code), closing brackets/parens, periods, commas.
            doi = m.rstrip("`.,'\")")
            if doi not in seen:
                seen.add(doi)
                dois.append(doi)

        if not dois:
            return ""

        fp_dir_str = self.config.get("paths", {}).get("fingerprint_dir", "data/fingerprints")
        fp_dir = Path(fp_dir_str) if Path(fp_dir_str).is_absolute() else _ROOT / fp_dir_str

        verified: list[str] = []
        hallucinated: list[str] = []
        for doi in dois:
            fp = load_fingerprint(f"doi:{doi}", fp_dir)
            if fp is None:
                hallucinated.append(doi)
                logger.warning(f"  [{skill_name}] citation not in corpus: {doi}")
            else:
                verified.append(doi)

        lines = ["\n\n## CITATION VERIFICATION"]
        lines.append(f"- Citations checked: {len(dois)}")
        lines.append(f"- Verified in corpus: {len(verified)}")
        if hallucinated:
            lines.append(f"- **NOT IN CORPUS ({len(hallucinated)}):** "
                         + ", ".join(hallucinated))
        else:
            lines.append("- All cited DOIs verified against fingerprint store.")
        return "\n".join(lines)

    def _parse_handoff(self, text: str) -> dict[str, str]:
        """Extract key:value fields from a '### PIPELINE HANDOFF' block.

        See :func:`src.handoff.parse_handoff` — logic lives there so
        ``src/binder_report.py`` can reuse it without depending on this class.
        """
        return _handoff.parse_handoff(text)

    def _parse_hotspot_residues(self, text: str, handoff: dict) -> str | None:
        """Parse the MODEL-READY HOTSPOTS table(s) from structure stage output.

        See :func:`src.handoff.parse_hotspot_residues` — logic lives there so
        ``src/binder_report.py`` can reuse it without depending on this class.
        """
        return _handoff.parse_hotspot_residues(text, handoff)

    def _ensure_structure(self, pdb_id: str) -> Path:
        """Return local ASU CIF path, downloading from RCSB if absent.

        Also attempts to download biological assembly 1 ({PDB_ID}_ba1.cif) which
        is used by the structure stage to avoid crystal-contact confusion.
        """
        structures_dir = _ROOT / self.config.get("paths", {}).get("structures_dir", "data/structures")
        dest = structures_dir / f"{pdb_id.upper()}.cif"
        if not dest.exists():
            logger.info(f"  Downloading {pdb_id} from RCSB...")
            proc = subprocess.run(
                [sys.executable, str(_ROOT / "scripts" / "download_pdb_structures.py"), "--id", pdb_id],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0 or not dest.exists():
                raise PipelineError(
                    f"Failed to download PDB {pdb_id}.\n"
                    f"stderr: {proc.stderr.strip() or '(none)'}\n"
                    f"Try manually: python scripts/download_pdb_structures.py --id {pdb_id}"
                )
            logger.info(f"  Downloaded {dest} ({dest.stat().st_size // 1024} KB)")
        else:
            logger.info(f"  Structure {pdb_id} on disk: {dest}")

        # Biological assembly 1 — download once; skip silently if unavailable.
        # BA1 contains only the physiological complex, eliminating crystal contacts
        # that mislead the structure analysis skill.
        ba1_dest = structures_dir / f"{pdb_id.upper()}_ba1.cif"
        if not ba1_dest.exists():
            try:
                ba1_url = f"https://files.rcsb.org/download/{pdb_id.upper()}-assembly1.cif.gz"
                r = requests.get(ba1_url, timeout=30)
                if r.status_code == 200:
                    ba1_dest.write_bytes(gzip.decompress(r.content))
                    logger.info(f"  Downloaded BA1 {ba1_dest} ({ba1_dest.stat().st_size // 1024} KB)")
                else:
                    logger.warning(f"  BA1 not available for {pdb_id} (HTTP {r.status_code}) — will use ASU")
            except Exception as exc:
                logger.warning(f"  BA1 download failed for {pdb_id}: {exc} — will use ASU")

        return dest

    @staticmethod
    def _read_pdb_identity(cif_path: Path) -> dict:
        """Return ``{title, entity_descriptions, organisms}`` from a CIF header.

        Three independent identity signals — the model is more confident
        when all three agree, and a mismatch in any one is a strong red flag.
        Returns empty values on parse failure (caller decides whether to
        abort or proceed without verification).
        """
        out: dict = {"title": "", "entity_descriptions": [], "organisms": []}
        try:
            import gemmi  # type: ignore
            doc = gemmi.cif.read(str(cif_path))
            block = doc.sole_block()

            title_pair = block.find_pair("_struct.title")
            if title_pair:
                out["title"] = title_pair[1].strip('"').strip("'").strip()

            descs: list[str] = []
            for row in block.find(["_entity.id", "_entity.type", "_entity.pdbx_description"]):
                # Only protein/polymer entities — skip waters, ions, ligands.
                if row[1].lower() == "polymer":
                    descs.append(row[2].strip('"').strip("'").strip())
            out["entity_descriptions"] = descs

            orgs: list[str] = []
            for row in block.find(["_entity_src_gen.ncbi_taxonomy_id",
                                   "_entity_src_gen.pdbx_gene_src_scientific_name"]):
                if row[1]:
                    orgs.append(row[1].strip('"').strip("'").strip())
            out["organisms"] = orgs
        except Exception as exc:
            logger.warning(f"Could not read PDB identity from {cif_path}: {exc}")
        return out

    def _verify_pdb_identity(
        self,
        cif_path: Path,
        expected_target: str,
    ) -> tuple[bool, str]:
        """Check that the structure at ``cif_path`` matches the expected target.

        Strict substring match (case-insensitive, both directions) of the
        expected protein name(s) against:
          - the structure title (``_struct.title``)
          - every polymer entity description (``_entity.pdbx_description``)

        For a target like ``"YAP1 / TEAD4"`` the check passes only when **at
        least one** of the named proteins is found — partner-chain misses
        are caller-handled (we generally trust the entity descriptions).
        For inhibit_active_site mode (one protein), exact recognition of
        the single name is required.

        Returns ``(passed, message)``. ``message`` always names the actual
        polymer entities present so the caller can produce a useful error.
        """
        meta = self._read_pdb_identity(cif_path)
        title = meta["title"]
        descs = meta["entity_descriptions"]
        actual_summary = f'title="{title or "(empty)"}", entities={descs or "(none)"}'

        if not title and not descs:
            return False, f"could not read identity metadata from {cif_path}"

        # Extract candidate protein names from the expected target. Strip
        # parenthetical annotations ("ENPP1 (active site)" → "ENPP1") and
        # split on common separators.
        cleaned = re.sub(r"\([^)]*\)", " ", expected_target)
        tokens = re.split(r"[/,;+&]| and ", cleaned, flags=re.IGNORECASE)
        names = [t.strip() for t in tokens if t.strip()]
        if not names:
            return False, f"could not extract a protein name from expected target {expected_target!r}"

        hay_lower = (title + " " + " ".join(descs)).lower()
        hits: list[str] = []
        for name in names:
            n = name.lower()
            if n in hay_lower:
                hits.append(name)
                continue
            # Reverse: maybe the expected name is the long form ("Yes-
            # associated protein 1") and entities use the short form. Try
            # token-overlap as a fallback.
            for word in re.split(r"\s+", n):
                if len(word) >= 4 and word in hay_lower:
                    hits.append(name)
                    break

        if hits:
            logger.info(f"  PDB identity check OK — matched {hits}  ({actual_summary[:160]})")
            return True, actual_summary

        return False, (
            f"PDB identity mismatch — expected target {expected_target!r} "
            f"(extracted names: {names}) does not appear in structure metadata. "
            f"Actual: {actual_summary}"
        )

    @staticmethod
    def _chain_entity_descriptions(cif_path: Path) -> dict[str, str]:
        """Parse a CIF file and return {chain_id: entity_description}.

        Uses gemmi (already a project dep).  Returns empty dict on any failure so
        the caller can degrade gracefully.
        """
        try:
            import gemmi  # type: ignore
            doc = gemmi.cif.read(str(cif_path))
            block = doc.sole_block()

            entity_desc: dict[str, str] = {}
            for row in block.find(["_entity.id", "_entity.pdbx_description"]):
                entity_desc[row[0]] = row[1].strip('"').strip("'")

            chain_to_desc: dict[str, str] = {}
            for row in block.find(["_struct_asym.id", "_struct_asym.entity_id"]):
                desc = entity_desc.get(row[1], "")
                if desc:
                    chain_to_desc[row[0]] = desc
            return chain_to_desc
        except Exception as exc:
            logger.warning(f"Could not read chain descriptions from {cif_path}: {exc}")
            return {}

    @staticmethod
    def _build_label_seq_id_map(cif_path: Path, chain_id: str) -> dict[int, tuple[int, str]]:
        """Return ``{auth_seq_id: (label_seq_id, residue_name_3letter)}`` for one chain.

        Uses gemmi; returns empty dict on failure or missing chain. The
        residue name is included so a caller can sanity-check that a
        literature-claimed residue (e.g. ``THR238``) actually exists in the
        structure at that auth_seq_id — protecting against mouse/human
        numbering mismatches and similar errors that wouldn't be caught by
        just blindly resolving label_seq_id.
        """
        try:
            import gemmi  # type: ignore
            st = gemmi.read_structure(str(cif_path))
            if len(st) == 0:
                return {}
            for chain in st[0]:
                if chain.name != chain_id:
                    continue
                mapping: dict[int, tuple[int, str]] = {}
                # gemmi exposes label_seq directly; if a residue's
                # label_seq is None (rare — typically only for het rows),
                # fall back to 1-indexed position in the chain.
                for i, res in enumerate(chain, start=1):
                    auth = int(res.seqid.num)
                    label = res.label_seq if res.label_seq is not None else i
                    mapping[auth] = (int(label), res.name.upper())
                return mapping
            return {}
        except Exception as exc:
            logger.warning(f"Could not build auth→label map for {chain_id}@{cif_path}: {exc}")
            return {}

    def _resolve_unverified_label_seq_ids(
        self,
        structure_text: str,
        cif_path: Path,
        target_chain: str,
    ) -> tuple[str, list[str]]:
        """Validate and normalise the MODEL-READY HOTSPOTS table.

        Always overwrites the label_seq_id column with the value gemmi
        computes from the CIF, even when the LLM wrote a specific number.
        Empirically the LLM often gets label_seq_id wrong — typically using
        1-indexed chain position rather than the true mmCIF label_seq, which
        can disagree by several residues when the resolved structure starts
        at a non-zero label_seq offset (e.g. 3KYS chain A starts at
        label_seq=3 so position 111 ≠ label_seq=111). Boltzgen reads
        `binding:` entries as label_seq values, so a wrong label_seq_id
        constrains the binder to the wrong residues — the dominant cause
        of zero hotspot occlusion in the YAP-TEAD mesothelioma run.

        Also runs a residue-name sanity check on every row and rewrites
        the BoltzGen `binding: ...` line so it carries the corrected
        label_seq_ids.

        Returns ``(updated_text, warnings)``. ``warnings`` is a list of
        one-line strings naming any residue whose expected name from the
        table does not match the residue actually present at that
        auth_seq_id, plus any case where the LLM-supplied label_seq_id
        disagreed with gemmi's.
        """
        warnings: list[str] = []
        auth_to_label_and_name = self._build_label_seq_id_map(cif_path, target_chain)
        if not auth_to_label_and_name:
            logger.warning(
                f"  cannot build gemmi auth→label map for {target_chain}@{cif_path.name} "
                f"— hotspot table left as-is, downstream stages may misnumber residues"
            )
            return structure_text, warnings

        # Walk every table row, validate residue name, compare label_seq_id
        # against gemmi truth, and substitute when needed. Match rows in
        # `| NAME[digits] | auth | label_or_token | ... |` form.
        row_pat = re.compile(
            r"(\|\s*([A-Z]{3})\d*\s*\|\s*(\d+)\s*\|)\s*([^|]*?)\s*\|",
            re.MULTILINE,
        )
        seen: set[tuple[str, int]] = set()
        substitutions = 0

        def _row_sub(match: re.Match) -> str:
            nonlocal substitutions
            prefix = match.group(1)
            expected_name = match.group(2)
            auth_s = int(match.group(3))
            llm_label_raw = match.group(4).strip()

            # Residue-name sanity (catches mouse↔human numbering offsets etc.)
            actual = auth_to_label_and_name.get(auth_s)
            if actual is None:
                key = (expected_name, auth_s)
                if key not in seen:
                    seen.add(key)
                    warnings.append(
                        f"residue at chain {target_chain} auth_seq_id {auth_s} "
                        f"({expected_name}) not present in structure"
                    )
                return match.group(0)  # leave row unchanged (can't fix)

            true_label, actual_name = actual
            if actual_name.upper() != expected_name.upper():
                key = (expected_name, auth_s)
                if key not in seen:
                    seen.add(key)
                    warnings.append(
                        f"residue NAME mismatch at chain {target_chain} "
                        f"auth_seq_id {auth_s}: report says {expected_name} but "
                        f"structure has {actual_name} — likely a numbering offset"
                    )

            # Compare LLM value to gemmi truth; always emit gemmi truth in
            # the rewritten cell.
            try:
                llm_label = int(llm_label_raw)
            except ValueError:
                llm_label = None
            if llm_label != true_label:
                substitutions += 1
                if llm_label is not None:
                    warnings.append(
                        f"label_seq_id correction at {expected_name}{auth_s}: "
                        f"LLM said {llm_label}, gemmi says {true_label} "
                        f"(BoltzGen `binding:` field uses label_seq)"
                    )
            return f"{prefix} {true_label} |"

        new_text = row_pat.sub(_row_sub, structure_text)

        # Rewrite the BoltzGen `binding:` line in each MODEL-READY HOTSPOTS
        # section independently. The structure-expert may produce one or
        # more sections (multi-region designs); each has its own table and
        # its own `binding:` line. Combining them into a global list (the
        # original bug) would corrupt multi-region runs.
        section_pat = re.compile(
            r"(### MODEL.READY HOTSPOTS[^\n]*\n.*?)(?=\n### MODEL.READY HOTSPOTS|\n## |\Z)",
            re.DOTALL | re.IGNORECASE,
        )

        def _section_sub(sec_match: re.Match) -> str:
            section = sec_match.group(1)
            local_residues: list[int] = []
            for m in re.finditer(
                r"\|\s*([A-Z]{3})\d*\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|",
                section,
            ):
                try:
                    local_residues.append(int(m.group(3)))
                except ValueError:
                    pass
            if not local_residues:
                return section
            # Dedupe preserving order
            dedup: list[int] = []
            seen_lbl: set[int] = set()
            for x in local_residues:
                if x not in seen_lbl:
                    seen_lbl.add(x)
                    dedup.append(x)
            new_binding = "binding: " + ",".join(str(x) for x in dedup)
            return re.sub(
                r"^\s*binding:\s*[^\n]+",
                new_binding,
                section,
                flags=re.MULTILINE,
            )

        new_text = section_pat.sub(_section_sub, new_text)

        # Resolve any leftover `UNVERIFIED_NNN` placeholders globally.
        new_text = re.sub(
            r"UNVERIFIED_(\d+)",
            lambda m: str(auth_to_label_and_name.get(int(m.group(1)), (m.group(0),))[0]),
            new_text,
        )

        if substitutions:
            logger.info(
                f"  label_seq_id corrections: {substitutions} residue(s) had "
                f"LLM-provided label_seq disagreeing with gemmi; rewritten "
                f"from CIF ground truth (this prevents BoltzGen from "
                f"constraining the wrong residues)"
            )

        return new_text, warnings

    @staticmethod
    def _count_chain_residues(cif_path: Path) -> dict[str, int]:
        """Return {chain_id: protein_residue_count}.

        Uses gemmi's higher-level Structure parser (more reliable than tabular
        loops because it correctly resolves polymer entities and skips water /
        ions / ligands). Counts only standard protein residues — solvent and
        heteroatoms are excluded so the count matches what BoltzGen will treat
        as the target.
        """
        try:
            import gemmi  # type: ignore
            st = gemmi.read_structure(str(cif_path))
            if len(st) == 0:
                return {}
            model = st[0]
            counts: dict[str, int] = {}
            for chain in model:
                # ResidueKind.AA covers the 20 standard amino acids + a few
                # modified ones; this matches BoltzGen's polymer view.
                n = sum(
                    1 for r in chain
                    if r.entity_type == gemmi.EntityType.Polymer
                    and r.het_flag.upper() != "H"
                )
                if n > 0:
                    counts[chain.name] = n
            return counts
        except Exception as exc:
            logger.warning(f"Could not count chain residues from {cif_path}: {exc}")
            return {}

    def _build_structure_query_for_choice(
        self,
        pdb_id: str,
        target_complex: str,
        analysis_path: Path,
        prev_handoff: dict,
        context_files: list[Path],
    ) -> str:
        """
        Build a rich structure_query for a non-primary pathway choice.

        When the user picks a choice other than the primary recommendation the
        pathway handoff's ``structure_query`` refers to the wrong complex.  This
        method finds the correct choice entry in ``choices_json``, builds a query
        that includes the target complex name, design_intent, and evidence context,
        and optionally appends the relevant TARGET OPPORTUNITY LANDSCAPE section
        from the pathway report so the structure expert has full biological context.
        """
        chosen: dict = {}

        # ── Step 1: find matching entry in choices_json ───────────────────────
        choices_raw = prev_handoff.get("choices_json", "")
        if choices_raw:
            try:
                choices_list = json.loads(choices_raw)
                pdb_upper = pdb_id.upper()
                tc_lower = target_complex.lower() if target_complex else ""
                for ch in choices_list:
                    ch_pdbs = [p.upper() for p in ch.get("pdb_ids", [])]
                    ch_complex_lower = ch.get("complex", "").lower()
                    if pdb_upper in ch_pdbs or (tc_lower and ch_complex_lower == tc_lower):
                        chosen = ch
                        break
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning(f"_build_structure_query_for_choice: choices_json parse error: {exc}")

        complex_name = chosen.get("complex") or target_complex or "the target complex"
        design_intent = chosen.get("design_intent", "disrupt")
        evidence_basis = chosen.get("evidence_basis", "")
        key_uncertainty = chosen.get("key_uncertainty", "")

        query = (
            f"Analyze the {complex_name} interface in PDB {pdb_id}.\n"
            f"Structure file: {analysis_path}\n"
            f"Design intent: {design_intent}.\n"
        )
        if evidence_basis:
            query += f"Evidence context from pathway analysis: {evidence_basis}\n"
        if key_uncertainty:
            query += f"Key uncertainty: {key_uncertainty}\n"

        # ── Step 2: append the TARGET OPPORTUNITY LANDSCAPE section ───────────
        # This gives the structure expert the full biological reasoning including
        # key residues, prior therapeutic strategies, and PDB suggestions.
        pathway_section = self._extract_landscape_section(
            context_files, complex_name, chosen.get("tier", "")
        )
        if pathway_section:
            query += (
                f"\nRelevant section from pathway analysis "
                f"(use for biological context only — do NOT infer chain IDs from "
                f"protein names here; confirm chains from the structure file):\n"
                f"{pathway_section}\n"
            )

        query += (
            f"\nIdentify hotspot residues for a {design_intent} strategy "
            f"against the {complex_name} complex."
        )
        return query

    def _extract_landscape_section(
        self,
        context_files: list[Path],
        complex_name: str,
        tier: str,
    ) -> str:
        """
        Extract the ``#### [TIER] ComplexName`` block for a given complex from
        the TARGET OPPORTUNITY LANDSCAPE section of any context file.
        Returns the raw markdown block, or empty string if not found.
        """
        if not complex_name:
            return ""

        # Escape for regex — complex names often contain "/"
        name_escaped = re.escape(complex_name.strip())
        # Also build a relaxed variant without tier prefix
        pattern = re.compile(
            r"(####\s*\*{0,2}\[[^\]]*\]\s*\*{0,2}\s*" + name_escaped + r".*?)(?=\n####|\Z)",
            re.DOTALL | re.IGNORECASE,
        )

        for cf in context_files:
            if not cf.exists():
                continue
            try:
                text = cf.read_text(encoding="utf-8")
                m = pattern.search(text)
                if m:
                    return m.group(1).strip()
            except OSError:
                continue
        return ""

    def _parse_pathway_choices(
        self, report_text: str, primary_handoff: dict
    ) -> list[dict]:
        """
        Parse the ``### TARGET OPPORTUNITY LANDSCAPE`` section of a pathway report.

        Returns a list of up to 4 dicts:
        ```
        {
            "index": int,
            "tier": str,               # VALIDATED | BIOLOGICALLY_JUSTIFIED | PATHWAY_INFERRED
            "complex": str,            # "ProteinA / ProteinB"
            "pdb_ids": list[str],      # may be empty if not found in corpus
            "evidence_basis": str,
            "key_uncertainty": str,
            "structure_query": str,    # from handoff for primary; synthesised for others
            "chain_ids_inferred": bool,
        }
        ```
        Returns ``[]`` if the section is absent or no candidates can be extracted.
        """
        primary_structure_query = primary_handoff.get("structure_query", "")
        primary_complex = (primary_handoff.get("target_complex") or "").strip().lower()
        primary_pdb = (primary_handoff.get("pdb_id") or "").strip().upper()

        # ── Path A: choices_json in PIPELINE HANDOFF (preferred) ─────────────
        # The skill emits a compact JSON line:
        #   - choices_json: [{"tier":...,"complex":...,"pdb_ids":[...],...}, ...]
        # This is unambiguous and requires no markdown parsing.
        raw_json = primary_handoff.get("choices_json", "").strip()
        if raw_json:
            try:
                raw_choices = json.loads(raw_json)
                return self._annotate_choices(
                    raw_choices, primary_complex, primary_pdb, primary_structure_query
                )
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning(
                    f"choices_json in PIPELINE HANDOFF is not valid JSON ({exc}) — "
                    "falling back to markdown parsing"
                )

        # ── Path B: regex fallback for old reports without choices_json ───────
        logger.debug("_parse_pathway_choices: falling back to markdown regex parser")

        section_match = re.search(
            r"###\s+TARGET OPPORTUNITY LANDSCAPE\s*\n(.*?)(?=\n###(?!#)|\Z)",
            report_text,
            re.DOTALL | re.IGNORECASE,
        )
        if not section_match:
            return []

        section = section_match.group(1)
        candidate_blocks = re.split(r"(?=####)", section)
        candidate_blocks = [b.strip() for b in candidate_blocks if b.strip().startswith("####")]
        if not candidate_blocks:
            return []

        tier_map = {
            "VALIDATED": "VALIDATED",
            "BIOLOGICALLY JUSTIFIED": "BIOLOGICALLY_JUSTIFIED",
            "PATHWAY INFERRED": "PATHWAY_INFERRED",
        }

        def _extract_field(pattern: str, text: str) -> str:
            m = re.search(
                r"\*\*" + pattern + r":?\*\*\s*:?\s*(.+?)(?=\n\s*-\s*\*\*|\Z)",
                text,
                re.DOTALL,
            )
            return " ".join(m.group(1).split()).strip() if m else ""

        raw_choices = []
        for block in candidate_blocks[:4]:
            header_match = re.match(
                r"####\s*\*{0,2}\[([^\]]+)\]\s*\*{0,2}\s+(.+?)(?:\*{0,2})?\s*[\r\n]",
                block,
            )
            if not header_match:
                continue
            raw_tier = header_match.group(1).strip().upper()
            complex_name = header_match.group(2).strip()
            pdb_raw = _extract_field(r"Suggested PDB ID\(s\)", block)
            pdb_ids = [
                p.upper()
                for p in re.findall(r"\b[0-9][A-Za-z0-9]{3}\b", pdb_raw)
                if not p.isdigit()
            ]
            raw_choices.append({
                "tier": tier_map.get(raw_tier, raw_tier.replace(" ", "_")),
                "complex": complex_name,
                "pdb_ids": pdb_ids,
                "evidence_basis": _extract_field("Evidence basis", block),
                "key_uncertainty": _extract_field("Key uncertainty", block),
            })

        return self._annotate_choices(
            raw_choices, primary_complex, primary_pdb, primary_structure_query
        )

    def _annotate_choices(
        self,
        raw_choices: list[dict],
        primary_complex: str,
        primary_pdb: str,
        primary_structure_query: str,
    ) -> list[dict]:
        """
        Add ``index``, ``structure_query``, and ``chain_ids_inferred`` to each
        choice dict.  The first choice whose complex name or PDB matches the
        primary handoff gets the real ``structure_query``; all others get a
        synthesised one.
        """
        choices: list[dict] = []
        primary_claimed = False

        for raw in raw_choices[:4]:
            complex_name = raw.get("complex", "")
            pdb_ids = [p.upper() for p in raw.get("pdb_ids", [])]
            cn_lower = complex_name.lower()

            is_primary = not primary_claimed and (
                cn_lower == primary_complex
                or (primary_complex and primary_complex in cn_lower)
                or (primary_complex and cn_lower in primary_complex)
                or (primary_pdb and primary_pdb in pdb_ids)
            )
            if is_primary:
                primary_claimed = True

            if is_primary and primary_structure_query:
                structure_query = primary_structure_query
                chain_ids_inferred = False
            else:
                ref_pdb = pdb_ids[0] if pdb_ids else "UNKNOWN"
                structure_query = (
                    f"Analyze PDB {ref_pdb} at data/structures/{ref_pdb}.cif. "
                    f"{complex_name} interface for PPI inhibitor design."
                )
                chain_ids_inferred = True

            choices.append({
                "index": len(choices),
                "tier": raw.get("tier", "UNKNOWN"),
                "complex": complex_name,
                "pdb_ids": pdb_ids,
                "evidence_basis": raw.get("evidence_basis", ""),
                "key_uncertainty": raw.get("key_uncertainty", ""),
                "structure_query": structure_query,
                "chain_ids_inferred": chain_ids_inferred,
                # Pass-through fields. Pathway-expert (standard mode) leaves
                # most of these blank; wildcard-expert populates them for the
                # graph-driven novelty triage + hypothesis articulation.
                # Future-compat: hypothesis fields plumb through here so a
                # later probe-mode design-analyst can read them without any
                # additional schema change.
                "design_intent": raw.get("design_intent"),
                "novelty_score": raw.get("novelty_score"),
                "classification": raw.get("classification"),
                "depmap_r_to_anchor": raw.get("depmap_r_to_anchor"),
                "depmap_max_r_to_hubs": raw.get("depmap_max_r_to_hubs"),
                "depmap_neighborhood": raw.get("depmap_neighborhood"),
                "predicted_consequence": raw.get("predicted_consequence"),
                "falsifying_readout": raw.get("falsifying_readout"),
            })

        # Bubble primary to index 0 if it wasn't first
        primary_idx = next(
            (i for i, c in enumerate(choices) if not c["chain_ids_inferred"]), None
        )
        if primary_idx is not None and primary_idx != 0:
            choices.insert(0, choices.pop(primary_idx))
            for i, c in enumerate(choices):
                c["index"] = i

        return choices

    def _merge_context(self, *paths: Path) -> str:
        """
        Concatenate multiple report files into a single context string,
        deduplicating by resolved path.
        """
        seen: set[Path] = set()
        parts: list[str] = []
        for p in paths:
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            parts.append(p.read_text(encoding="utf-8"))
        return "\n\n---\n\n".join(parts)
