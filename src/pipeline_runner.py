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
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv
from loguru import logger

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

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
from src.skill_runner import SkillRunner

_DEFAULT_MODELS = {
    "claude": "claude-sonnet-4-6",
    "gemini": "gemini-3.1-flash-lite-preview",
}

# Per-stage default model overrides keyed by stage name. Picks up before the
# global _default_model but after an explicit user override via stage_models.
# Currently: stage 6 (summary) defaults to Haiku because Sonnet-class models
# trigger a biosecurity refusal on the "review of designed binders" task
# regardless of prompt wording, and Haiku handles tabular summarisation
# correctly and cheaply.
_DEFAULT_STAGE_MODELS = {
    "claude": {"summary": "claude-haiku-4-5-20251001"},
    "gemini": {},
}

# Maps stage name → skill name (inverse of tasks.py _SKILL_TO_STAGE)
_STAGE_TO_SKILL: dict[str, str] = {
    "pathway":    "pathway-expert",
    "structure":  "complex-structure-analysis",
    "literature": "molecular-biology-expert",
    "design":     "protein-design-script",
    "summary":    "design-analyst",
}


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
        LLM provider — "claude" or "gemini".
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
        provider: str = "claude",
        model_id: str | None = None,
        output_dir: Path | None = None,
        max_iter: int = 30,
        max_tokens: int = 100_000,
        stage_models: dict[str, str] | None = None,
        extended_thinking_stages: set[str] | None = None,
        pathway_mode: str = "standard",
        capture_traces: bool = False,
    ) -> None:
        self.config = config
        self.provider = provider
        self._default_model = model_id or _DEFAULT_MODELS[provider]
        self._output_dir_override = output_dir
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

        result = PipelineResult(run_dir=run_dir, pdb_id=pdb_id, target_complex=target_complex)
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

            # ── Stage 6: summary (terminal LLM stage) ────────────────────────
            # Pass the literature handoff so _stage_summary can read modality /
            # design_intent / target_complex from a single source. It also
            # falls back to parsing 03_design_report.md if needed.
            if start_idx <= 6:
                self._stage_summary(lit_handoff, run_dir, result)

        except PipelineBlockedError:
            raise
        except Exception as exc:
            result.error = str(exc)
            logger.error(f"Pipeline error: {exc}")
            raise

        return result

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    def _stage_pathway(self, query: str, run_dir: Path, result: PipelineResult) -> dict[str, str]:
        output_file = run_dir / "00_pathway.md"
        skill = "wildcard-expert" if self._pathway_mode == "wildcard" else "pathway-expert"
        logger.info(f"Stage 0: {skill}")
        handoff = self._run_stage(skill, query, [], output_file)
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
        handoff = self._run_stage("complex-structure-analysis", query, [], output_file)
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
        handoff = self._run_stage("molecular-biology-expert", query, pathway_ctx, output_file)
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
        design_handoff = self._run_stage("protein-design-script", query, context_files, design_report)
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
        handoff = self._run_stage("design-analyst", query, [], output_file)
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

    def _resolve_stage(self, skill_name: str) -> tuple[str, bool]:
        """
        Return (model_id, use_extended_thinking) for a given skill.

        Per-stage model overrides are keyed by stage name (pathway / structure /
        literature / design).  Extended thinking is silently ignored for Gemini.
        If an override specifies Haiku but extended thinking is requested, the
        model is auto-upgraded to Sonnet with a warning.
        """
        # Invert the skill name back to a stage name for lookup
        stage = next(
            (s for s, sk in _STAGE_TO_SKILL.items() if sk == skill_name),
            skill_name,
        )
        # Lookup order: explicit user override → per-stage default → global default.
        per_stage_default = _DEFAULT_STAGE_MODELS.get(self.provider, {}).get(stage)
        model_id = self._stage_models.get(stage, per_stage_default or self._default_model)
        use_thinking = (
            self.provider == "claude"
            and stage in self._ext_thinking
        )
        if use_thinking and "haiku" in model_id.lower():
            logger.warning(
                f"Extended thinking requires Sonnet — auto-upgrading {stage} "
                f"stage from {model_id} to claude-sonnet-4-6"
            )
            model_id = "claude-sonnet-4-6"
        return model_id, use_thinking

    def _run_stage(
        self,
        skill_name: str,
        query: str,
        context_files: list[Path],
        output_file: Path,
    ) -> dict[str, str]:
        """Invoke one skill and return the parsed PIPELINE HANDOFF fields."""
        context_text: str | None = None
        if context_files:
            context_text = self._merge_context(*context_files)

        model_id, use_thinking = self._resolve_stage(skill_name)
        runner = SkillRunner(
            skill_name=skill_name,
            provider=self.provider,
            model_id=model_id,
            config=self.config,
            max_iter=self.max_iter,
            max_input_tokens=self.max_tokens,
            use_extended_thinking=use_thinking,
        )

        logger.info(f"  [{skill_name}] {query[:100]}{'...' if len(query) > 100 else ''}")
        output_text = runner.run(query, context_text=context_text)

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
        return handoff

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
        """
        Extract key:value fields from a '### PIPELINE HANDOFF' block.

        Accepts both canonical format ('- key: value') and bare format
        ('key: value'), and strips markdown code fences that models
        sometimes wrap the block in.

        Stops at the next markdown heading or end of string.
        Returns {} if no block is found (non-fatal).
        """
        match = re.search(
            r"###\s+PIPELINE HANDOFF\s*\n(.*?)(?=\n##|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return {}
        fields: dict[str, str] = {}
        for line in match.group(1).splitlines():
            # Strip code-fence lines (``` or ~~~)
            if re.match(r"^\s*```", line) or re.match(r"^\s*~~~", line):
                continue
            # Accept '- key: value' (canonical) or 'key: value' (bare)
            m = re.match(r"^\s*(?:-\s+)?(\w+):\s*(.+)$", line)
            if m:
                fields[m.group(1).strip()] = m.group(2).strip()
        return fields

    def _parse_hotspot_residues(self, text: str, handoff: dict) -> str | None:
        """
        Parse the MODEL-READY HOTSPOTS table(s) from structure stage output.

        Returns a JSON string:
            {"target_chain": "A", "partner_chain": "B",
             "residues": [{"residue": "LEU", "auth_seq_id": 245,
                           "label_seq_id": 245, "rfd3_atoms": "CD1,CG2"}, ...]}

        Returns None if the section is absent (non-fatal).
        """
        # target_chain / partner_chain were added to the PIPELINE HANDOFF
        # template after some runs were created.  Fall back to chain_a / chain_b
        # for older runs that only emitted those fields.
        target_chain = handoff.get("target_chain", "") or handoff.get("chain_a", "")
        partner_chain = handoff.get("partner_chain", "") or handoff.get("chain_b", "")

        sections = re.findall(
            r"###\s+MODEL.READY HOTSPOTS.*?(?=\n###|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if not sections:
            return None

        # label_seq_id is allowed to be non-integer (e.g. "UNVERIFIED" or
        # similar when tool_get_sequence_map could not be called). Match any
        # non-pipe content and try to parse as int; fall back to auth_seq_id
        # if it isn't a number. Only auth_seq_id is required to be an int.
        row_pat = re.compile(
            r"^\|\s*([A-Z]+)\d*\s*\|\s*(\d+)\s*\|\s*([^|]*?)\s*\|\s*([^|]+?)\s*\|",
            re.MULTILINE,
        )
        residues: list[dict] = []
        seen: set[tuple] = set()
        for section in sections:
            for m in row_pat.finditer(section):
                residue, auth_id, label_raw, atoms = m.groups()
                auth_id_int = int(auth_id)
                try:
                    label_id_int = int(label_raw.strip())
                except (ValueError, AttributeError):
                    # Non-numeric label (e.g. "**UNVERIFIED**") — fall back to
                    # auth_seq_id. Downstream SASA enrichment uses auth_seq_id
                    # anyway; label_seq_id is only needed for BoltzGen YAML
                    # `binding:` lines, and those are written by the
                    # design-script skill from its own copy of the table.
                    label_id_int = auth_id_int
                key = (residue, auth_id_int)
                if key not in seen:
                    seen.add(key)
                    residues.append({
                        "residue": residue,
                        "auth_seq_id": auth_id_int,
                        "label_seq_id": label_id_int,
                        "rfd3_atoms": atoms.strip(),
                    })

        if not residues:
            return None

        return json.dumps({
            "target_chain": target_chain,
            "partner_chain": partner_chain,
            "residues": residues,
        })

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
