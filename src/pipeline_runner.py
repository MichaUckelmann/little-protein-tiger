"""
Programmatic pipeline orchestrator for the LittleProteinTiger design pipeline.

Sequences the PPI track's three discovery stages:
  Stage 0  — pathway-expert              discovers PPI target + PDB from corpus
  Stage 1  — molecular-biology-expert    prior art + tractability + go/no-go
  Stage 2  — complex-structure-analysis  interface geometry + hotspot mapping

On GO/CONDITIONAL_GO the run hands off to the binder track's own stage machine
(`_bridge_ppi_to_binder_track` -> trim -> spec -> pilot -> calibration ->
production -> scoring -> design-analyst), with the generator stages dispatched
to foundry or BoltzGen. The older PPI-only design/execution/analysis/summary
chain is retired — see LEGACY_RETIREMENT_SCOPE.md.

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
from loguru import logger

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.env_config import load_env  # noqa: E402
load_env(_ROOT / ".env")

from src import handoff as _handoff
from src.env_config import resolve_env_path
# `src.design_metrics` and `src.design_ranking` are imported lazily, inside
# the bridged-BoltzGen stages that use them (`parse_boltzgen_outputs`,
# `gate_boltzgen_records`, `resolve_boltzgen_ranking`). Everything this module
# used at import time belonged to the retired legacy chain.
from src.fingerprint_store import load_fingerprint
from src.skill_runner import SkillRefusedError, SkillRunner
from src.campaign_calibration import MIN_HITS_FOR_ESTIMATE
from src.token_budget import BudgetExceeded, TokenLedger, Usage, load_pricing

#: The design generators, and which of them share the binder track's stage
#: machine. `foundry` and `boltzgen` both go through
#: `_bridge_ppi_to_binder_track` on the PPI track and both dispatch inside
#: `_run_binder_track`. `boltzgen_legacy` is RETIRED — its stage chain is
#: deleted (LEGACY_RETIREMENT_SCOPE.md) — and the NAME is kept here only so
#: that naming it earns a specific refusal saying what replaced it, rather
#: than degrading to a generic "invalid design_engine". Ordered so error
#: messages list the default first.
_DESIGN_ENGINES = ("foundry", "boltzgen", "boltzgen_legacy")

#: Retired engines: recognised as names only so `__init__` can refuse them
#: with a message that says what replaced them, instead of degrading to a
#: generic "invalid design_engine". Never runnable.
_RETIRED_ENGINES = ("boltzgen_legacy",)

#: Engines whose PPI runs bridge into the binder-track stage machine. The
#: complement is exactly `boltzgen_legacy`, and writing it this way round
#: means adding a generator opts it IN rather than silently leaving it on the
#: legacy path.
_BRIDGED_ENGINES = ("foundry", "boltzgen")

_DEFAULT_MODELS = {
    "claude": "claude-sonnet-5",
    "gemini": "gemini-3.7-flash",
    "openai": "gpt-5.6-terra",
}

# Haiku cannot do extended thinking; stages that ask for it get upgraded here.
_THINKING_UPGRADE_MODEL = "claude-sonnet-5"

# There is NO automatic refusal fallback, deliberately. A safety classifier
# declining a stage ends the run; choosing a different model is the operator's
# decision, taken after reading the refusal. See docs/responsible-use.md and
# `_run_stage`.
#
# The measurement behind that, read the right way round: across three separate
# interface-stage refusals on one target (PD-L1), claude-sonnet-5 refused
# (category "bio") and claude-opus-5 then refused too — same category, every
# time, ~$0.13 apiece for nothing. Within a provider a categorised refusal is
# CONSISTENT rather than arbitrary, which is a reason to take the verdict
# seriously; it is not a map of which model to try next. See
# docs/responsible-use.md, "If you override a refusal".
#
# Model-id prefix -> provider, for a per-stage model override written WITHOUT
# an explicit "provider:model" prefix.
# `gpt-` matters as much as the other two: a bare `gpt-5.6-terra` under
# `models.<provider>.stages` would otherwise inherit the CURRENT provider and
# get POSTed to whichever endpoint that is, which is the 404-kills-the-run
# failure documented below.
_MODEL_ID_PROVIDERS = (("claude-", "claude"), ("gemini-", "gemini"),
                       ("gpt-", "openai"))


def _split_model_spec(spec: str, current_provider: str) -> tuple[str, str]:
    """Resolve a configured model id to (provider, model_id).

    An explicit "provider:model" always wins. Otherwise the provider is inferred
    from the model-id prefix, and only falls back to the CURRENT provider for an
    id we don't recognise (a local/Ollama model, say).

    Inferring rather than inheriting matters, and it was learned from the
    refusal chains this used to serve: those named Claude ids with no prefix
    while gemini was the default provider, so inheriting the current provider
    POSTed a Claude id to the Gemini endpoint, which 404s — and a 404 raises
    HTTPError, not SkillRefusedError, turning a recoverable refusal into a hard
    crash. The chains are gone (a refusal is terminal now — see `_run_stage`),
    but `models.<provider>.stages` overrides have exactly the same shape and
    the same hazard, so the inference moved there rather than being deleted.
    """
    if ":" in spec:
        provider, model_id = spec.split(":", 1)
        return provider, model_id
    for prefix, provider in _MODEL_ID_PROVIDERS:
        if spec.startswith(prefix):
            return provider, spec
    return current_provider, spec

# Per-stage default model overrides keyed by stage name. Picks up before the
# global _default_model but after an explicit user override via stage_models.
#
# DELIBERATELY EMPTY: LPT ships no per-stage model default for any provider.
# It used to put `summary` and `binder_summary` on claude-haiku-4-5, because
# Sonnet-class models trigger a biosecurity refusal on the "review of designed
# binders" task regardless of prompt wording and Haiku answers it. That is a
# smaller model producing content a larger one declined — the same thing the
# removed refusal-fallback chain did, pre-declared instead of reached at
# runtime, and a config file is not a good enough reason for the pipeline to
# ship it as a default. So `--provider claude` now runs those two stages on
# claude-sonnet-5 and MAY BE REFUSED there, which ends the run; the default
# provider (gemini) answers them cleanly and is unaffected.
#
# The table stays as the hook: `_resolve_stage` reads it, and an operator who
# has read a refusal and made the call sets `models.<provider>.stages.<stage>`
# in config.yaml, where the decision is theirs and is recorded.
_DEFAULT_STAGE_MODELS: dict[str, dict[str, str]] = {
    "claude": {},
    "gemini": {},
    "openai": {},
}

# Maps stage name → skill name (inverse of tasks.py _SKILL_TO_STAGE)
_STAGE_TO_SKILL: dict[str, str] = {
    "pathway":    "pathway-expert",
    "structure":  "complex-structure-analysis",
    "literature": "molecular-biology-expert",
    # Binder (target-name-first) workflow stages.  Only the LLM stages appear
    # here; trim / binder_spec / pilot / calibration / production /
    # binder_scoring are deterministic Python and carry no skill entry.
    "target_intel":       "binder-target-intel",
    "interface":          "complex-structure-analysis",
    "binder_summary":     "design-analyst",
}


#: Written into the label_seq_id column when the structure carries no
#: label_seq for that residue — a PDB-format file has none at all. It is a
#: deliberately non-numeric token: anything numeric here would be read
#: downstream as a measured value, and BoltzGen consumes this column as
#: label_seq.
_LABEL_SEQ_UNAVAILABLE = "UNAVAILABLE"


class _TrimFromDisk:
    """
    The subset of TrimResult the later binder stages use, rebuilt from
    trim_map.json so a resume does not have to re-run the trim.
    """

    def __init__(self, mapping: dict):
        self.kept_segments = [tuple(s) for s in mapping.get("kept_segments", [])]
        # `.get(k, default)`, not `or`: a stored 0 must stay 0.
        self.n_segments = int(mapping.get("n_segments", len(self.kept_segments)))
        self.contig = mapping.get("contig", "")
        self.trimmed_path = Path(mapping.get("trimmed_path")
                                 or mapping.get("source", ""))
        self.bsa_retention = float(mapping.get("bsa_retention", 1.0))
        # Needed by `plan_campaign`'s token estimate, which sizes RF3 cost as
        # (tokens/195)**1.62. Missing here since the size law landed (b9eb184,
        # 2026-08-29), so EVERY fresh-process resume — `--start-from
        # production` is the documented normal case after a multi-day campaign
        # — died with "'_TrimFromDisk' object has no attribute
        # 'n_residues_after'" before launching anything. trim_map.json has
        # carried the value all along; nothing read it.
        self.n_residues_after = int(
            mapping.get("n_residues_after")
            or sum(hi - lo + 1 for lo, hi in self.kept_segments))
        self.n_residues_before = int(
            mapping.get("n_residues_before") or self.n_residues_after)
        self.target_chain = mapping.get("target_chain", "")
        self.partner_chain = mapping.get("partner_chain", "")
        # Reconstructed for the trim maps written before this key existed —
        # every one of the 53 on disk — but ONLY when a target_chain is known.
        # A mapping without one would otherwise reconstruct under the key `""`,
        # which is truthy, and step 9's chain-aware cross-check would compare
        # `{"": [...]}` against `{"A": [...]}` and refuse in front of a GPU
        # launch. `tests/test_audit_fixes.py` builds exactly that shape.
        stored = mapping.get("kept_by_chain") or {}
        self.kept_by_chain = (
            {c: [tuple(x) for x in segs] for c, segs in stored.items()}
            if stored else
            ({self.target_chain: list(self.kept_segments)}
             if self.target_chain else {}))
        self.pdb_id = mapping.get("pdb_id", "")
        self.warnings = list(mapping.get("warnings") or [])
        # Read back so a `--start-from` resume still reports a modified
        # residue the trim converted. The reflection test in
        # tests/test_audit_fixes.py requires every `trim.<attr>` the runner
        # reads to exist here, and that test is what caught the missing
        # `n_residues_after` that killed ten days of fresh-process resumes.
        self.modified_residues = list(mapping.get("modified_residues") or [])
        # Written by `structure_trim` since the exposure gate became
        # scale-free. Carried here for the same reason as everything above:
        # a fresh-process `--start-from` rebuilds the trim from this file and
        # must not silently lack a field. Absent from every trim_map.json
        # written before that change, hence the defaults.
        self.exposed_hydrophobic_A2 = float(
            mapping.get("exposed_hydrophobic_A2") or 0.0)
        self.n_exposed_hydrophobic = int(
            mapping.get("n_exposed_hydrophobic") or 0)
        self.exposed_hydrophobic_fraction = mapping.get(
            "exposed_hydrophobic_fraction")
        self.exposed_hydrophobic_auth = [
            int(a) for a in (mapping.get("exposed_hydrophobic_auth") or [])]


#: Words a stage writes when it means "there isn't one". They must be read as
#: ABSENT, not as chain ids, and the length test alone does not do it: `none`,
#: `None`, `null`, `na`, `nan`, `nil`, `TBD` and `tbd` are all <= 4 characters
#: and all `isalnum()`, so every one of them used to validate as an auth chain
#: id. `_binder_sites`'s own docstring says it exists to catch `"TBD (PD-L1)"`
#: — the parenthesised form is caught by the length limit and the bare word
#: was not, which defeats the check's stated purpose.
#:
#: Observed, not hypothetical: `projects/gpcr_metabolic_v3` ran with
#: `partner_chain: none` through BOTH chain validators, and
#: `structure_trim._per_residue_bsa` then logged "interface analysis
#: unavailable: Chain 'none' not found in structure" and failed open, so the
#: trim measured a zero interface for a target it believed had a partner.
#:
#: A real chain called `NA` would now be refused. That trade is deliberate:
#: a refusal is one clear error message, and accepting a placeholder is a
#: campaign that silently measures nothing.
_CHAIN_PLACEHOLDERS = frozenset({
    "none", "null", "na", "n/a", "n.a.", "nan", "nil", "nothing",
    "tbd", "todo", "unknown", "unspecified", "absent", "-", "--", "?",
})


def chain_id_or_blank(value: Any) -> str:
    """An auth chain id, or `""` when a stage meant "there isn't one".

    ONE predicate for both places that validate a chain. There were two copies
    of it — `_binder_sites` and `_stage_trim` — written against different
    dicts (target-intel's handoff and the interface stage's hotspot table),
    and a placeholder had to be rejected in both or it simply moved one stage
    later. Returning the normalised id rather than a bool is what lets a
    caller tell "absent" from "malformed", which is the distinction
    single-target mode turns on.
    """
    text = str(value or "").strip()
    if not text or text.lower() in _CHAIN_PLACEHOLDERS:
        return ""
    return text if (len(text) <= 4 and text.isalnum()) else "\x00"


#: The design intents a run can carry. `disrupt` breaks an interface,
#: `inhibit_active_site` occupies a pocket on a single target, and `stabilize`
#: GLUES two proteins together — the only one whose epitope spans two chains,
#: which is why it needs its own predicates rather than a boolean somewhere.
_DESIGN_INTENTS = ("disrupt", "stabilize", "inhibit_active_site")

#: Intents that design against ONE protein and therefore need no partner.
_SINGLE_TARGET_INTENTS = frozenset({"inhibit_active_site"})


def is_glue_intent(intent: Any) -> bool:
    """True for a molecular-glue run — the two-chain-epitope mode."""
    return str(intent or "").strip().lower() == "stabilize"


def waives_partner_chain(intel: Any) -> bool:
    """Does this run's own `design_intent` say a partner is optional?

    The discriminator is in the data and is written deterministically, so a
    missing partner is waived only where the objective never needed one. A
    `disrupt` or `stabilize` run whose partner went missing — an LLM slip, a
    truncated handoff — still refuses, rather than quietly designing against
    one protein's surface when the objective was an interface.

    Takes the handoff dict or the intent string, because the two call sites
    hold different shapes of the same fact.
    """
    intent = intel.get("design_intent") if isinstance(intel, dict) else intel
    return str(intent or "").strip().lower() in _SINGLE_TARGET_INTENTS


#: What `chain_id_or_blank` returns for a value that is neither a chain id nor
#: a recognisable "there isn't one" — `"TBD (PD-L1)"`, a sentence, a number
#: with punctuation. Distinct from `""` because absent is legal in
#: single-target mode and malformed never is.
MALFORMED_CHAIN = "\x00"


def _stage_for_skill(skill_name: str) -> str:
    """
    Best-effort inverse of _STAGE_TO_SKILL.

    First match wins, so this is ambiguous for any skill serving more than one
    stage (complex-structure-analysis -> structure | interface).  Callers that
    know their stage must pass it explicitly; this exists only as the fallback.

    `design-analyst` served `summary` as well as `binder_summary` until the
    `boltzgen_legacy` retirement deleted the former, so it is single-stage for
    now — which changes nothing for callers: pass `stage=` anyway.
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


def _binder_midpoint(contig: str, default: int = 78) -> int:
    """Midpoint of the binder length range at the head of an RFD3 contig.

    A contig looks like ``70-86,/0,A195-229`` — the leading token is the
    binder's length range. Used only to size the folded complex for a runtime
    estimate, so the midpoint is enough and a malformed contig just falls back.
    """
    m = re.match(r"\s*(\d+)\s*-\s*(\d+)", contig or "")
    return (int(m.group(1)) + int(m.group(2))) // 2 if m else default


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
    error: str | None = None
    # JSON string: {"target_chain": "A", "partner_chain": "B", "residues": [...]}
    # Populated after the structure stage; None if MODEL-READY HOTSPOTS not found.
    hotspot_residues_json: str | None = None
    # Persisted handoff dicts so later stages can read fields from earlier
    # stages even when prev_handoff has been rebound. Set by _stage_pathway,
    # _stage_structure, and _stage_literature.
    #: Set when `_select_designable_structure` overrode the pathway stage's
    #: choice, so the substitution is visible in artifacts a person reads and
    #: not only in the log and the manifest checkpoint.
    structure_switch: dict | None = None
    pathway_handoff: dict | None = None
    structure_handoff: dict | None = None
    literature_handoff: dict | None = None
    # Deterministic PDB-vs-expected-target identity check result, populated
    # in run() right after _ensure_structure. Always present; the structure
    # stage surfaces it to the LLM as evidence (synonym → proceed,
    # paralog mismatch → NO_GO).
    pdb_identity_check: dict | None = None
    # Populated only when the target chain turned out to be an ORTHOLOG of the
    # human protein: the per-hotspot conservation table, the human accession,
    # and the human AlphaFold model fetched alongside it. See
    # PipelineRunner._check_ortholog_conservation.
    ortholog_conservation: dict | None = None


# ---------------------------------------------------------------------------
# PipelineRunner
# ---------------------------------------------------------------------------

_LOCAL_PREFIX = "LOCAL-"


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
        "bio"). Gemini can decline too, and when it does the run STOPS:
        there is no automatic fallback to another model. Picking a
        different one is the operator's call, made after reading the
        refusal — `--provider`, or a `models.<provider>.stages` override
        for a single stage.
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

    #: The PPI track's own stages. It ends at `structure`: on GO the run
    #: bridges into `BINDER_STAGE_ORDER` (see `_bridge_ppi_to_binder_track`),
    #: and a binder-stage `start_from` is dispatched straight there. The
    #: retired legacy chain used to occupy indices 3-6 —
    #: LEGACY_RETIREMENT_SCOPE.md.
    STAGE_ORDER = ["pathway", "literature", "structure"]

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
        uniprot: str | None = None,
        chains: str | None = None,
        hotspots: str | None = None,
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
        design_engine: str | None = None,
        modality: str = "mini_protein",
        design_intent: str | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        # config.yaml `models:` is the source of truth; the module-level
        # _DEFAULT_* tables are the fallback when it is absent.
        self._models_cfg = (config.get("models") or {}).get(provider) or {}
        # A config carried over from before refusals became terminal will still
        # list `refusal_fallbacks`. Silently ignoring it would be the worst
        # outcome: the operator believes a declined stage will be retried
        # elsewhere, and it will not be. Say so once, loudly, per run.
        if self._models_cfg.get("refusal_fallbacks"):
            logger.warning(
                f"models.{provider}.refusal_fallbacks is set and is NO LONGER "
                f"READ — automatic retry on another model was removed. A stage "
                f"a safety classifier declines now ends the run; choosing a "
                f"different model is your decision, via --provider or "
                f"models.{provider}.stages.<stage>. Delete the key to silence "
                f"this. See docs/responsible-use.md.")
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
        # "ppi" (literature-driven) | "binder" (target-name-first) |
        # "structure" (the operator already has the structure).
        if workflow not in {"ppi", "binder", "structure"}:
            raise ValueError(
                f"Invalid workflow={workflow!r}; expected 'ppi', 'binder' "
                f"or 'structure'."
            )
        self._workflow = workflow
        # Structure-first track only. `uniprot` is optional and re-arms the
        # three identity-keyed guards (see `_structure_first_caveats`);
        # `chains` overrides the measured target/partner choice.
        self._uniprot = (uniprot or "").strip()
        self._chains = (chains or "").strip()
        # Operator-specified epitope. When set, the interface stage does not
        # call a model at all — `--modality`'s posture ("stages propose, the
        # operator decides"), taken to its conclusion.
        self._hotspots = (hotspots or "").strip()
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
        self._last_switch_reason: str | None = None
        # Set by _verify_target_chain_assignment when the target chain is an
        # ortholog rather than the human protein; read by
        # _check_ortholog_conservation, which is the gate that decides
        # whether that ortholog's epitope is worth designing against.
        self._ortholog = None
        self._ortholog_human_acc = ""
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
        # Which generator builds the designs: "foundry" (default) |
        # "boltzgen". (`boltzgen_legacy` is a name the constructor still
        # recognises, in order to refuse it specifically — see below.)
        #
        # The two are the SAME stage machine — target_intel/interface/
        # trim are generator-neutral, and only spec/pilot/calibration/
        # production/scoring dispatch (see `_boltzgen_backend`). A PPI run on
        # either one therefore takes the same route: PPI's own pathway/
        # literature/structure stages, then `_bridge_ppi_to_binder_track`.
        # That is what gives BoltzGen a measured calibration, a resumable
        # manifest, `--stop-after` and a trim, none of which the older path
        # had.
        #
        # `boltzgen_legacy` was the older PPI-only design/execution/analysis/
        # summary chain. It is RETIRED: the stage methods, its driver scripts
        # and `src/design_runner.py` are deleted, so the name now names
        # nothing runnable and is refused on every workflow below.
        #
        # `design.backend` in config.yaml sets the project-wide default, with
        # the same override-precedence pattern as `pathway_mode` above: an
        # explicit kwarg (what the CLI's --design-engine passed) wins.
        # None means "not specified" — config decides. Using a real engine
        # name as the default made an EXPLICIT choice of that engine
        # indistinguishable from silence, so config could override the caller.
        cfg_design_engine = (config.get("design") or {}).get("backend", "foundry")
        resolved_engine = design_engine or cfg_design_engine
        if resolved_engine not in _DESIGN_ENGINES:
            raise ValueError(
                f"Invalid design_engine={resolved_engine!r}; expected one of "
                f"{', '.join(repr(e) for e in _DESIGN_ENGINES)}."
            )
        if resolved_engine in _RETIRED_ENGINES:
            # Refused here, not only at the CLI: `design.backend` in
            # config.yaml can name it, and a library caller constructs this
            # class directly (the one driver that did — scripts/
            # e2e_ppi_boltzgen.py — is deleted with the chain).
            #
            # Refused rather than silently mapped onto "boltzgen", because the
            # two were never the same campaign: the legacy chain honoured
            # neither `--stop-after` nor a calibration verdict, so an operator
            # who asked for one and got the other would be told nothing.
            raise ValueError(
                f"design_engine={resolved_engine!r} is RETIRED — its stage "
                f"chain (design -> execution -> analysis -> summary) and "
                f"src/design_runner.py are deleted. Use 'boltzgen': it "
                f"dispatches this track's own generator stages to BoltzGen "
                f"and gives you the trim, a MEASURED production size, "
                f"--stop-after and a resumable manifest. (If the value came "
                f"from config.yaml, change design.backend.) See "
                f"LEGACY_RETIREMENT_SCOPE.md. Reports over campaigns the "
                f"legacy path already produced still work — "
                f"scripts/generate_ppi_report.py reads them off disk."
            )
        self._design_engine = resolved_engine
        # What the operator asked to design. LLM stages PROPOSE a modality;
        # this decides. See _resolve_modality.
        if modality not in ("mini_protein", "cyclic_peptide"):
            raise PipelineError(
                f"Invalid modality={modality!r}; expected 'mini_protein' or "
                f"'cyclic_peptide'.")
        if modality == "cyclic_peptide" and resolved_engine == "foundry":
            raise PipelineError(
                "modality='cyclic_peptide' cannot run on the foundry design "
                "engine — RFD3 has no cyclic-peptide path. Use "
                "design_engine='boltzgen' for a cyclic-peptide campaign.")
        self._modality = modality

        if design_intent is not None and design_intent not in _DESIGN_INTENTS:
            raise PipelineError(
                f"Invalid design_intent={design_intent!r}; expected one of "
                f"{', '.join(_DESIGN_INTENTS)}.")
        if design_intent is not None and workflow != "structure":
            # Same posture as --hotspots on the PPI track: refuse the flag
            # rather than accept it and run a campaign the operator did not
            # ask for. Only the structure-first track measures an intent that
            # an operator could sensibly override — the other two take it from
            # a stage handoff that also carries the chains it was derived
            # from, so overriding it there would contradict the report.
            raise PipelineError(
                f"--design-intent applies to --workflow structure only (got "
                f"{workflow!r}). The binder and ppi tracks take their intent "
                f"from the stage handoff that also names the chains it was "
                f"derived from. Re-run with --workflow structure --structure "
                f"<file|PDB ID>.")
        self._design_intent = design_intent
        # Flag-level: catch a combination that cannot work before any stage
        # runs. Re-checked at the two places the intent is actually known, so
        # a stage-MEASURED stabilize is refused too.
        self._refuse_unbuilt_glue_paths(design_intent, source="--design-intent")

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
        target: str | None = None,
    ) -> PipelineResult:
        """
        Run the pipeline from `start_from` onwards.

        Parameters
        ----------
        query : str
            User's initial query (e.g. "design PPI inhibitors for MRSA").
        start_from : str
            Stage to begin at: pathway | literature | structure, or any
            binder-track stage name (a PPI run bridges into that track after
            the go/no-go decision, so `--start-from production` resumes
            there).
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

        if self._project is None:
            # Fail before creating a run dir or spending any tokens. Every
            # track now ends in the same GPU stages — the binder and structure
            # tracks enter them directly, and a PPI run bridges into them on
            # either generator — so every track needs the round-based,
            # resumable manifest those stages checkpoint into. The binder and
            # structure tracks were already refused here by the CLI and the
            # foundry bridge by the runner; the retired `boltzgen_legacy` was
            # the one combination that ran without one, which is also the one
            # whose multi-hour campaign had nowhere to record it had started.
            raise PipelineBlockedError(
                f"--workflow {self._workflow} requires --project: the GPU "
                f"stages every track reaches (pilot/calibration/production) "
                f"are multi-hour to multi-day campaigns, and the project "
                f"manifest is what makes them resumable and what "
                f"--start-from reads."
            )

        safe_slug = re.sub(r"[^a-zA-Z0-9]+", "_", query[:40]).strip("_").lower()
        run_dir = self._output_dir_override or (
            _ROOT / "outputs" / f"{safe_slug}_{date.today().isoformat()}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Pipeline run dir: {run_dir}")
        self._init_ledger(run_dir)

        result = PipelineResult(run_dir=run_dir, pdb_id=pdb_id, target_complex=target_complex)

        # ── Structure-first track: the operator already has the structure ────
        # Neither discovery question applies. There is no target to find and no
        # entry to choose, so stage 0 is deterministic — chains enumerated,
        # the interface MEASURED — and the track joins the binder stage machine
        # at `interface`, one stage in, where a model reads the real
        # coordinates and picks the epitope. Everything from `trim` onward is
        # the same code a --workflow binder campaign runs.
        if self._workflow == "structure":
            if start_from in ("pathway", "structure", "literature",
                              "target_intel"):
                start_from = "interface"
            dirs = self._binder_dirs(run_dir)
            if start_from == "interface":
                self._stage_structure_intel(
                    pdb_id or "", query, dirs, result,
                    uniprot=self._uniprot, chains=self._chains)
            return self._run_binder_track(
                query, run_dir, result,
                start_from=start_from, context_file=context_file,
                auto_mode=auto_mode, target=target,
                attach=not self._detach, n_batches=self._n_batches,
            )

        # ── Binder workflow: target-name-first track ─────────────────────────
        # Skips pathway/literature discovery entirely: the target is already
        # named, so stage 0 is a structural choice, not a biological search.
        # Runs foundry (RFD3 -> solubleMPNN -> RF3) on the local GPU rather than
        # BoltzGen, and sizes the production run from a measured calibration.
        if self._workflow == "binder":
            if start_from in ("pathway", "structure", "literature"):
                start_from = "target_intel"
            return self._run_binder_track(
                query, run_dir, result,
                start_from=start_from, context_file=context_file,
                auto_mode=auto_mode, target=target,
                attach=not self._detach, n_batches=self._n_batches,
            )

        # ── PPI-track run resuming INSIDE the hand-off ───────────────────────
        # A fresh run always enters below at pathway/literature/structure and
        # reaches `_bridge_ppi_to_binder_track` after the go/no-go decision
        # (see that block further down). Resuming a later GPU stage in a new
        # process (e.g. --start-from production) has no PPI stage to
        # re-enter — go straight into the binder-track stage machine that the
        # earlier bridge call already handed off to and wrote checkpoints for.
        # Both generators resume this way. A name that is neither a PPI stage
        # nor a binder stage falls through to the stage-index lookup below,
        # which rejects it rather than silently restarting at pathway.
        if self._workflow == "ppi" and self._bridges_to_binder_track \
                and start_from in self.BINDER_STAGE_ORDER:
            return self._run_binder_track(
                query, run_dir, result,
                start_from=start_from, context_file=context_file,
                auto_mode=auto_mode, target=None,
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

        # A structure switch has to survive a restart. `_select_designable_structure`
        # only re-runs while start_idx <= 1, but the pathway report it overrode is
        # still on disk recommending the rejected entry in its handoff block
        # (`- pdb_id: 6E3Y`), and that is what a resume parses. So
        # `--start-from structure` — the normal way to re-enter a run after
        # editing a prompt or recovering from a stage failure — silently reverted
        # to the entry the pipeline had already measured as the wrong one, and
        # then analysed it. The decision is in the manifest; re-apply it rather
        # than re-deriving (which would cost another RCSB round-trip) or trusting
        # the stale file.
        # `--pdb` always wins, on a resume exactly as on a fresh run.
        if start_idx > 1 and result.pdb_id and not pdb_id:
            self._reapply_recorded_structure_switch(result, handoff)

        # Hotspot recovery on a PPI resume PAST the structure stage lived
        # here. It served the retired `analysis` chain; the binder track solves
        # the same problem for itself in B1 of `_run_binder_track`, which
        # re-reads `21_interface.md` when resuming past the interface stage —
        # and a binder-stage `start_from` is dispatched straight there above,
        # never reaching this point. STAGE_ORDER now ends at `structure`, so
        # there is no PPI index past it left to recover for.

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

            # Its own block, and BEFORE the pdb_id one below: a stabilize
            # handoff that named no structure must not slip past unrefused.
            # The intent is read from the LITERATURE handoff first, because
            # `_bridge_ppi_to_binder_track` and `_stage_structure` both prefer
            # that value — refusing on a pathway intent the run was never
            # going to use would be wrong. (Inert on both archived stabilize
            # runs, which agree across stages; the precedence mismatch is
            # real regardless.)
            if start_idx <= 1:
                self._refuse_unbuilt_glue_paths(
                    ((result.literature_handoff or {}).get("design_intent")
                     or handoff.get("design_intent")),
                    source="the pathway stage's target")

            # Two deterministic checks on what the pathway stage just chose,
            # BEFORE paying for another LLM stage. Neither needs the structure
            # on disk; between them they cost one GraphQL call.
            if start_idx <= 1 and result.pdb_id:
                self._check_af_model_intent(handoff)
                # Structure choice has to be settled HERE. The structure stage
                # cannot switch entries — by the time it emits a handoff it has
                # already analysed one, and its hotspots refer to those
                # coordinates.
                better = self._select_designable_structure(
                    result.target_complex or "", result.pdb_id,
                    operator_pinned=bool(pdb_id))
                if better:
                    stale = result.pdb_id
                    self._retarget_stale_structure(handoff, stale, better)
                    handoff["structure_query"] = (
                        (handoff.get("structure_query") or "").rstrip()
                        + f" (Structure switched from {stale} to {better} "
                          f"deterministically: cleaner entry for this "
                          f"interface. Analyse {better}.)")
                    result.structure_switch = {
                        "from": stale, "to": better,
                        "reason": self._last_switch_reason or "",
                    }
                    self._note_structure_switch(result, stale, better)
                    result.pdb_id = better
                    handoff["pdb_id"] = better
                self._check_structure_organism(
                    result.pdb_id, result.target_complex or "")
                self._screen_select_agents(
                    "pathway", [query, result.target_complex or ""],
                    pdb_id=result.pdb_id)

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

            # ── Hand-off: the binder track's stage machine ───────────────────
            # PPI's own pathway/literature/structure stages above are
            # unchanged; from here the run continues inside the binder track's
            # own stage machine, which dispatches its generator stages to
            # whichever backend was chosen. See
            # `_bridge_ppi_to_binder_track`, UNIFY_DESIGN_BACKEND_NOTES.md
            # (foundry) and UNIFY_BOLTZGEN_BACKEND_NOTES.md (BoltzGen).
            #
            # The gate is a tautology now that `boltzgen_legacy` is retired
            # (there is nothing left for a PPI run to fall through TO), and it
            # stays deliberately: `tests/test_audit_fixes.py` asserts by source
            # inspection that this predicate gates `_designable_chain_sizes`,
            # which is the lesson that a designable-size relaxation is only
            # sound where a trim actually follows. See
            # LEGACY_RETIREMENT_SCOPE.md.
            if self._bridges_to_binder_track:
                return self._bridge_ppi_to_binder_track(
                    query, run_dir, result, auto_mode=auto_mode)
            raise PipelineError(
                f"design_engine={self._design_engine!r} has no PPI stages "
                f"after the go/no-go decision. Use 'foundry' or 'boltzgen'.")

        except PipelineBlockedError:
            raise
        except PipelinePausedError as exc:
            # A pause is a checkpoint, not a failure — state is saved and the
            # run resumes with --start-from. It subclasses PipelineError, so
            # without this branch every `--stop-after` and every --detach
            # handoff was logged as "Pipeline error", the same confusion that
            # once recorded a healthy detached campaign as FAILED.
            logger.info(f"Paused at {exc.pause_point} — state checkpointed, "
                        f"resume when ready")
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

    def _cluster_slug(self, dirs: dict[str, Path]) -> str:
        """Name this campaign gets on the SHARED cluster filesystem.

        Must be unique across projects. `dirs["binder"].parent.name` is the
        run directory, which for a project run is literally "round-1" — so
        every project's first round staged into the same directory under
        `pipeline_root`, overwriting each other's spec and mixing outputs.
        A site trial nests one level deeper (`binder/sites/<site_id>/binder`),
        so the site id is carried too.

        `_run_cluster_stage` (which stages) and `_cluster_paths_for_mode`
        (which reads back) must derive this identically — hence one method.
        """
        parent = dirs["binder"].parent
        site = parent.name if parent.parent.name == "sites" else None
        if self._project is not None:
            base = f"{self._project.slug}_{self._round_id or 'round'}"
        elif site is not None:
            base = parent.parent.parent.name
        else:
            base = parent.name
        base = base or "campaign"
        return f"{base}_{site}" if site else base

    def _is_site_dirs(self, dirs: dict[str, Path]) -> bool:
        """True when `dirs` belongs to a `--trial-sites` site, not the top level."""
        return dirs["binder"].parent.parent.name == "sites"

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

    def _resolve_modality(self, proposed: str | None, *, source: str) -> str:
        """The modality a run will ACTUALLY design, given what a stage proposed.

        The operator's `--modality` decides; an LLM stage only proposes. Two
        reasons this is not just "trust the handoff":

        * cyclic_peptide is opt-in. It needs specialised synthesis, costs
          substantially more, and has a thinner experimental record than
          mini-protein binders — so a stage suggesting it must not silently
          commit a campaign to it.
        * RFD3/foundry has no cyclic-peptide path at all, and
          `binder_sizes.cyclic_peptide` is 12-15 residues. Feeding that to RFD3
          asks for something it cannot build, and it fails quietly.
        """
        wanted = self._modality or "mini_protein"
        proposed = (proposed or "").strip() or wanted
        if proposed == wanted or proposed == "either":
            return wanted
        if proposed == "cyclic_peptide" and wanted != "cyclic_peptide":
            logger.info(
                f"  {source} proposed modality=cyclic_peptide; designing a "
                f"mini_protein instead. Cyclic peptides are opt-in — re-run "
                f"with --modality cyclic_peptide (which also selects the "
                f"boltzgen engine) if that is what you want.")
            return wanted
        logger.info(f"  {source} proposed modality={proposed!r}; using "
                    f"{wanted!r} (--modality decides)")
        return wanted

    def _resolve_design_intent(self, measured: str, *, source: str,
                               has_partner: bool) -> str:
        """The intent a run will ACTUALLY pursue, given what was measured.

        Same posture as `_resolve_modality`: the structure-first track
        MEASURES an intent from the geometry it found (`disrupt` when there
        are two chains, `inhibit_active_site` when there is one), and the
        operator's `--design-intent` decides. Without the flag this returns
        `measured` unchanged and logs nothing, so every existing run is
        untouched.

        The one thing it refuses outright is `stabilize` on a single chain.
        A molecular glue holds two proteins together; with one chain there is
        no second protein to hold, and the request is not a preference that
        can be honoured differently — it is a request for something the
        structure cannot express.
        """
        wanted = (self._design_intent or "").strip()
        if not wanted or wanted == measured:
            return measured
        if is_glue_intent(wanted) and not has_partner:
            raise PipelineBlockedError(
                "--design-intent stabilize needs two chains: a molecular glue "
                "holds two proteins together, and this structure offers one "
                "designable chain. Pass --chains <target>,<partner> if the "
                "partner is in the file, or pick a structure of the complex.")
        logger.info(f"  {source} measured design_intent={measured!r}; using "
                    f"{wanted!r} (--design-intent decides)")
        return wanted

    def _refuse_unbuilt_glue_paths(self, intent: Any, *, source: str) -> None:
        """Refuse the glue combinations Stage 3 has not built.

        Self-contained rather than trusting its caller's `if`, for the same
        reason `_refuse_undispatched_site_trials` is: each of these would
        otherwise run a long way before failing in a way that does not name
        the cause. `div_standard_diabetes` spent three LLM stages and then
        died at hotspot grounding blaming "textbook/literature numbering",
        which is not what was wrong with it.

        Each refusal names the scope item that would build it, so the message
        is a pointer rather than a dead end.
        """
        if not is_glue_intent(intent):
            return
        if self._boltzgen_backend:
            raise PipelineBlockedError(
                f"{source} is a molecular-glue run (design_intent=stabilize) "
                f"and --design-engine boltzgen has no two-chain target path: "
                f"`binding:` addresses ONE chain, so the co-target would be "
                f"silently dropped. Use --design-engine foundry (scope item "
                f"22 covers the second engine).")
        if self._modality == "cyclic_peptide":
            raise PipelineBlockedError(
                f"{source} is a molecular-glue run and --modality "
                f"cyclic_peptide selects BoltzGen, which has no two-chain "
                f"target path. Use the default mini_protein modality.")
        if self._trial_sites > 1:
            raise PipelineBlockedError(
                f"{source} is a molecular-glue run and --trial-sites > 1 "
                f"routes through `_run_site_trials`, which has no two-chain "
                f"site shape yet. Run one site at a time.")
        if self._workflow in ("ppi", "binder"):
            raise PipelineBlockedError(
                f"{source} is a molecular-glue run on --workflow "
                f"{self._workflow}, which cannot ask for one: only "
                f"--workflow structure takes --design-intent. Re-run with "
                f"--workflow structure --structure <file|PDB ID> "
                f"--design-intent stabilize.")

    def _binder_length_range(self, intel: dict) -> tuple[int, int]:
        """`(min, max)` binder length for this run, defaulting BY MODALITY.

        The handoff normally carries `binder_length_min` / `binder_length_max`,
        set from `design.constraints.binder_sizes[modality]` by whichever stage
        composed it. But a hardcoded mini-protein fallback here is wrong by 5.8x
        for a cyclic-peptide campaign, and it is reachable: `_run_binder_track`
        re-parses `20_target_intel.md` off disk on a resume, and a report written
        before those keys existed carries neither. Falling back through the same
        `binder_sizes` table the producing sites use keeps the two in step.

        **The handoff's numbers are only usable when its own modality survived
        `_resolve_modality`.** A stage that PROPOSED one modality sized its
        lengths for that one, so when the operator's `--modality` overrides the
        proposal those numbers belong to the rejected modality and must be
        discarded, not preferred over the table. Caught on the first real PD-L1
        macrocycle run: `binder-target-intel` proposed `mini_protein` with
        70-86, `--modality cyclic_peptide` correctly overrode the modality, and
        the lengths stayed 70-86 — so the trim contig recorded a 78-residue
        binder for a campaign designing a 13-residue one, which over-stated the
        folded complex by 65 residues (195 tokens against 130) and over-costed
        it 74% in BOTH cost laws. Those feed `campaign_calibration.calibrate`'s
        budget check, so it can turn a SCALE_UP into a STOP.

        The reverse is worse and is the same bug: a stage proposing
        `cyclic_peptide` with 12-15 on a default foundry run would have handed
        RFD3 a 12-15mer it cannot build — quietly — with the modality coercion
        already applied and apparently working.
        """
        sizes = (self._binder_cfg().get("constraints") or {}).get("binder_sizes") or {}
        proposed = str(intel.get("modality") or "").strip()
        modality = self._resolve_modality(proposed or None,
                                          source="the stored handoff")
        stated_is_usable = (not proposed or proposed in (modality, "either"))
        default = sizes.get(modality) or {}
        # Last resort only when `binder_sizes` itself is absent; still per
        # modality, because the point of this method is that one number cannot
        # serve both.
        fallback_lo, fallback_hi = (
            (12, 15) if modality == "cyclic_peptide" else (70, 86))
        stated_lo = intel.get("binder_length_min") if stated_is_usable else None
        stated_hi = intel.get("binder_length_max") if stated_is_usable else None
        lo = stated_lo or default.get("min") or fallback_lo
        hi = stated_hi or default.get("max") or fallback_hi
        if proposed and not stated_is_usable:
            logger.info(
                f"  the stored handoff sized its binder for modality="
                f"{proposed!r} ({intel.get('binder_length_min')}-"
                f"{intel.get('binder_length_max')}); using {lo}-{hi} for "
                f"{modality!r}, which is what this run designs")
        return int(lo), int(hi)

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
        # Single-target mode is legitimate and has no partner chain: an
        # AlphaFold monomer, a structure with one designable chain, an
        # enzyme active site. `_stage_structure_intel` MEASURES that case
        # (`design_intent = "disrupt" if partner_chain else
        # "inhibit_active_site"`) and every stage downstream of here is
        # already partner-optional — `trim_target` guards every interface
        # measurement on `if partner_chain`, `foundry_spec` mentions a
        # partner nowhere at all, scoring reads chains off the RFD3 sidecar
        # as A/B regardless, and `exposure_verdict`'s no-denominator branch
        # was written for this case by name.
        #
        # So the partner is required only when the run's OWN stated intent
        # says there should be one. That is what keeps this from becoming a
        # silent mode switch: a `disrupt` campaign whose partner went
        # missing — an LLM slip, a truncated handoff — still refuses here,
        # rather than quietly designing against one protein's surface when
        # the whole objective was to disrupt an interface.
        single_target = waives_partner_chain(intel)

        def valid_chain(value: Any) -> bool:
            return chain_id_or_blank(value) not in ("", MALFORMED_CHAIN)

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
            partner = chain_id_or_blank(primary["partner_chain"])
            if not valid_chain(primary["target_chain"]):
                raise PipelineBlockedError(
                    f"target-intel did not name a usable target chain "
                    f"(target={primary['target_chain']!r}). Chain ids must come "
                    f"from the candidate table; re-run, or pass --pdb and the "
                    f"chains explicitly.")
            if partner == MALFORMED_CHAIN:
                raise PipelineBlockedError(
                    f"target-intel gave partner_chain="
                    f"{primary['partner_chain']!r}, which is neither an auth "
                    f"chain id nor a recognisable 'no partner' — so it cannot "
                    f"be read either way. Re-run, or pass --chains "
                    f"{primary['target_chain']},<partner> explicitly.")
            if not partner and not single_target:
                raise PipelineBlockedError(
                    f"target-intel named no partner chain "
                    f"(partner={primary['partner_chain']!r}) but declared "
                    f"design_intent="
                    f"{intel.get('design_intent', '')!r}, which is an "
                    f"interface campaign and needs two chains. If this target "
                    f"really is a single protein, the intent must say so "
                    f"(inhibit_active_site); otherwise re-run, or pass "
                    f"--chains <target>,<partner>.")
            if partner and single_target and intel.get("partner_name"):
                # Incoherent rather than merely odd: single-target mode
                # disables the wrong-molecule and ortholog guards, so a run
                # that both claims it and names a partner must not pick
                # whichever reading is more convenient.
                raise PipelineBlockedError(
                    f"target-intel declared design_intent=inhibit_active_site "
                    f"(single target) but also named partner "
                    f"{intel.get('partner_name')!r} on chain {partner!r}. "
                    f"Those disagree; single-target mode switches off the "
                    f"chain-assignment and ortholog checks, so pick one.")
            primary["partner_chain"] = partner
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

    # Chains smaller than this are ligands, tags and crystallisation peptides,
    # not things to design a binder against or from.
    _MIN_DESIGNABLE_CHAIN = 25
    # Pairwise interface analysis is O(n^2) in chains; a cryo-EM assembly can
    # carry a dozen. The largest few are where a real interface lives.
    _MAX_CHAINS_CONSIDERED = 6

    def _structure_chains(self, path: Path) -> list[tuple[str, int]]:
        """(chain id, residue count), largest first, backbone-defined.

        Membership is decided by BACKBONE, not residue name — the same rule
        `structure_tools.is_chain_residue` applies, and for the same reason:
        gemmi's component table does not know every modification a depositor
        may make, and filtering on the name silently deletes residues that
        carry a full N/CA/C (3KYS A344, S-palmitoyl-cysteine).
        """
        import gemmi
        from src.structure_tools import is_chain_residue

        st = gemmi.read_structure(str(path))
        st.setup_entities()
        out = [(ch.name, sum(1 for r in ch if is_chain_residue(r)))
               for ch in st[0]]
        return sorted([c for c in out if c[1] > 0], key=lambda c: -c[1])

    def _largest_interface(self, path: Path,
                           chains: list[tuple[str, int]]) -> tuple[str, str, float] | None:
        """The chain pair burying the most surface, measured. None if none touch.

        Contacts first, BSA second. Counting heavy-atom pairs inside 4.5 A with
        a KD-tree is cheap and rules out the chains that merely sit near each
        other in the asymmetric unit; SASA is then computed once, for the
        winner, because it is the expensive half.
        """
        import gemmi
        import numpy as np
        from scipy.spatial import cKDTree
        from src.structure_tools import analyze_interface, is_chain_residue

        st = gemmi.read_structure(str(path))
        st.setup_entities()
        coords = {}
        for ch in st[0]:
            pts = [[a.pos.x, a.pos.y, a.pos.z] for r in ch if is_chain_residue(r)
                   for a in r if a.element != gemmi.Element("H")]
            if pts:
                coords[ch.name] = np.asarray(pts)

        ranked = []
        ids = [c for c, _n in chains]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if a not in coords or b not in coords:
                    continue
                n = sum(len(x) for x in
                        cKDTree(coords[a]).query_ball_tree(cKDTree(coords[b]), 4.5))
                if n:
                    ranked.append((n, a, b))
        if not ranked:
            return None
        ranked.sort(reverse=True)
        _n, a, b = ranked[0]
        try:
            # `bsa_total_A2`, nested under "interface" — the flat `bsa_total`
            # this first read does not exist, and `.get` returned 0 silently,
            # so the report said "0 A^2 buried" for a 2,449 A^2 interface.
            iface = analyze_interface(str(path), a, b)["interface"]
            bsa = float(iface.get("bsa_total_A2") or 0.0)
        except Exception as exc:                       # noqa: BLE001
            logger.debug(f"BSA unavailable for {a}/{b}: {exc}")
            bsa = 0.0
        return a, b, bsa

    def _stage_structure_intel(
        self, pdb_id: str, query: str, dirs: dict[str, Path],
        result: PipelineResult, *, uniprot: str = "", chains: str = "",
    ) -> dict[str, str]:
        """Stage 0 for the structure-first track. Deterministic — no LLM call.

        The binder track's own stage 0 is name-first: it resolves a gene to
        UniProt and has a model choose among RCSB entries. When the operator
        already has the structure, that entire question is answered, and the
        only things left to decide are which chain is the target and which
        interface to aim at — both of which can be MEASURED.

        Same manoeuvre as `_bridge_ppi_to_binder_track`: compose the handoff
        `_run_binder_track` would have got from `binder-target-intel`, write it
        to the artifact path the stage machine reads, and enter one stage
        later. The difference is where it enters — the bridge starts at `trim`
        because PPI's structure stage already picked hotspots, while here
        nothing has looked at the structure yet, so `interface` still runs and
        the epitope is still chosen by a model reading the real coordinates.
        """
        self._ensure_structure(pdb_id)
        # Biological assembly 1, NOT the ASU `_ensure_structure` returns.
        # Every stage after this one — `_correct_label_seq_ids`,
        # `_verify_hotspot_grounding`, the trim, the RFD3 spec — addresses the
        # structure through `_binder_structure_path`, which prefers the
        # assembly. Measuring the chain pair on the ASU meant a crystal with
        # more copies in the asymmetric unit than in the assembly picked a
        # chain the rest of the run cannot see: on 8ZNL the ASU has A-H and the
        # assembly only A/B, so the largest measured interface was C/D and the
        # run died in `_correct_label_seq_ids` with "cannot build the
        # auth->label map for chain D" — after the LLM stage had been paid for.
        # Found by `scripts/bench_models.py`, which ran this track on four
        # structures and had both models fail identically on that one.
        path = self._binder_structure_path(pdb_id)
        found = self._structure_chains(path)
        designable = [c for c in found if c[1] >= self._MIN_DESIGNABLE_CHAIN]
        if not designable:
            raise PipelineBlockedError(
                f"{pdb_id} has no chain of at least {self._MIN_DESIGNABLE_CHAIN} "
                f"residues (found: {found or 'none'}). There is nothing here to "
                f"design a binder against.")

        note = ""
        if chains:
            wanted = [c.strip() for c in chains.split(",") if c.strip()]
            known = {c for c, _n in found}
            missing = [c for c in wanted if c not in known]
            if missing:
                raise PipelineBlockedError(
                    f"--chains named {missing} which are not in {pdb_id} "
                    f"(chains present: {sorted(known)})")
            target_chain = wanted[0]
            partner_chain = wanted[1] if len(wanted) > 1 else ""
            note = "Chains were named by the operator."
        elif len(designable) == 1:
            target_chain, partner_chain = designable[0][0], ""
            note = (f"Only one designable chain ({target_chain}, "
                    f"{designable[0][1]} residues).")
        else:
            pair = self._largest_interface(
                path, designable[:self._MAX_CHAINS_CONSIDERED])
            if pair is None:
                target_chain, partner_chain = designable[0][0], ""
                note = ("No two chains touch, so this was treated as a single "
                        "target with no partner interface.")
            else:
                a, b, bsa = pair
                size = dict(found)
                # Which of a touching pair is "the target" is the OPERATOR's
                # call, not a fact about the structure: either chain is a
                # legitimate thing to design against. Defaulting to the larger
                # one picks the substantial surface over the peptide or
                # nanobody that is usually the other half, and `--chains`
                # overrides it. Stated in the report either way, because a
                # silent default here is a whole campaign aimed at the wrong
                # molecule.
                target_chain, partner_chain = (
                    (a, b) if size.get(a, 0) >= size.get(b, 0) else (b, a))
                note = (f"Largest measured interface: chains {a}/{b}, "
                        f"{bsa:,.0f} A^2 buried. Chain {target_chain} was taken "
                        f"as the target because it is the larger of the two; "
                        f"pass --chains {partner_chain},{target_chain} to swap "
                        f"them.")

        design_intent = self._resolve_design_intent(
            "disrupt" if partner_chain else "inhibit_active_site",
            source="the structure-first track",
            has_partner=bool(partner_chain))
        self._refuse_unbuilt_glue_paths(
            design_intent, source="the structure-first track")
        modality = self._resolve_modality(None, source="the structure-first track")
        # Through `_binder_length_range` rather than reading `binder_sizes`
        # here: a cyclic-peptide campaign is 12-15 residues and a mini-protein
        # one 70-86, so a second copy of that lookup is a 5.8x error waiting
        # to drift (the mistake commit 5c17881 fixed once already). Same
        # table, same modality-aware fallback every other site uses.
        length_min, length_max = self._binder_length_range({"modality": modality})
        label = self._local_structure_stem(pdb_id) or pdb_id.upper()

        handoff = {
            "pdb_id": pdb_id,
            "target_gene": f"{label} chain {target_chain}",
            "target_uniprot": (uniprot or "").upper(),
            "partner_name": f"{label} chain {partner_chain}" if partner_chain else "",
            "target_chain": target_chain,
            "partner_chain": partner_chain,
            "design_intent": design_intent,
            "modality": modality,
            "binder_length_min": length_min,
            "binder_length_max": length_max,
            "interface_rationale": note,
            # Without this the interface stage falls back to its generic
            # "select model-ready hotspots for a ..." sentence and the
            # operator's brief never reaches the stage that chooses the
            # epitope — the one stage on this track where it matters.
            "structure_query": query.strip(),
            "go_recommendation": "GO",
        }
        result.pdb_id = pdb_id
        result.target_complex = (
            f"{handoff['target_gene']} / {handoff['partner_name']}"
            if partner_chain else handoff["target_gene"])

        chain_table = "\n".join(
            f"- chain {c}: {n} residues"
            + ("  <- target" if c == target_chain else
               "  <- partner" if c == partner_chain else "")
            for c, n in found)
        out = dirs["binder"] / self._BINDER_STAGE_FILES["target_intel"]
        self._write_binder_report(
            out, "Target intelligence (structure-first, deterministic)",
            f"{query}\n\n**Structure:** `{pdb_id}` ({path.name}).\n\n"
            f"{chain_table}\n\n{note}\n\n"
            + self._structure_first_caveats(uniprot),
            handoff)
        result.stage_files["target_intel"] = out
        result.stages_completed.append("target_intel")
        return handoff

    @staticmethod
    def _structure_first_caveats(uniprot: str) -> str:
        """Say plainly which guards a structure with no accession loses.

        Three of the pipeline's checks are keyed to IDENTITY rather than
        geometry, and all three fail open — quietly — when there is no UniProt
        accession to resolve. Failing open is right (an operator's own file is
        often a construct or a prediction that no database describes), but
        going quiet about it is not: one of the three is the guard that caught
        a full multi-hour campaign running against an anti-PD-L1 nanobody
        instead of PD-L1.
        """
        if uniprot:
            return (f"Identity checks are ACTIVE: chain assignment, organism "
                    f"and membrane topology all resolve against {uniprot}.")
        return (
            "**No UniProt accession was given, so three checks are inactive "
            "for this run:**\n\n"
            "- `_verify_target_chain_assignment` — cannot tell a target/partner "
            "swap from a correct assignment, because it compares the chain's "
            "modelled sequence against UniProt's. This is the guard that caught "
            "a campaign designed against an anti-PD-L1 nanobody's CDR loop "
            "instead of PD-L1.\n"
            "- `_check_structure_organism` — cannot warn that the structure is "
            "an ortholog rather than the human protein.\n"
            "- membrane topology — **transmembrane residues will NOT be "
            "stripped**. On a receptor that matters: in an isolated structure a "
            "TM helix is an exposed hydrophobic slab, and RFD3 preferentially "
            "binds it, producing designs that cannot work in a cell.\n\n"
            "Pass `--uniprot <ACC>` to switch all three back on. Hotspot "
            "grounding is unaffected — it reads residue names straight from "
            "the coordinates.")

    # Interface-facing sidechain heavy atoms per residue type. Lifted verbatim
    # from the table `complex-structure-analysis` applies in Phase 3
    # (skills/complex-structure-analysis/SKILL.md), so an operator-specified
    # hotspot and a model-chosen one describe the same geometry the same way.
    # GLY and ALA legitimately have no sidechain to reach with, and fall back
    # to CA/CB below.
    _RFD3_SIDECHAIN_ATOMS = {
        "ILE": ("CD1", "CG2"), "LEU": ("CD1", "CD2"), "VAL": ("CG1", "CG2"),
        "PHE": ("CD2", "CZ"), "TYR": ("CD2", "OH"), "TRP": ("CD2", "NE1"),
        "MET": ("CG", "SD"), "ARG": ("CZ", "NH1"), "LYS": ("NZ", "CE"),
        "ASP": ("CG", "OD1"), "GLU": ("CD", "OE1"),
        "ASN": ("CG", "OD1"), "GLN": ("CD", "OE1"), "HIS": ("CD2", "NE2"),
        "SER": ("CB", "OG"), "THR": ("CB", "OG1"), "CYS": ("CB", "SG"),
        "PRO": ("CB", "CG"), "ALA": ("CB",), "GLY": ("CA",),
    }

    @staticmethod
    def parse_hotspot_spec(spec: str) -> dict[str, list[int]]:
        """`"B74,B83,B84"` or `"74,83,84"` -> {chain: [auth_seq_id, ...]}.

        Same syntax `scripts/check_input_pdb.py --hotspots` already accepts. A
        bare number inherits whichever chain the caller resolves as the target,
        so `--hotspots 74,83,84` reads naturally when there is only one.
        """
        out: dict[str, list[int]] = {}
        for raw in (spec or "").replace(" ", "").split(","):
            if not raw:
                continue
            chain, num = ("", raw) if raw[0].isdigit() or raw[0] == "-" else (raw[0], raw[1:])
            try:
                resnum = int(num)
            except ValueError:
                raise PipelineBlockedError(
                    f"--hotspots entry {raw!r} is not <chain><number> or "
                    f"<number> (e.g. B83, or 83 for the target chain)") from None
            out.setdefault(chain, []).append(resnum)
        if not out:
            raise PipelineBlockedError("--hotspots was empty")
        return out

    def _resolve_hotspot_override(self, spec: str, pdb_id: str,
                                  target_chain: str, *,
                                  partner_chain: str = "") -> list[dict]:
        """Turn an operator's residue numbers into fully-formed hotspot dicts.

        The operator supplies `auth_seq_id` and nothing else. Everything a
        downstream consumer needs is LOOKED UP from the structure, never taken
        on trust:

        * `residue` — read from the coordinates. A user-typed name would
          either fail `_verify_hotspot_grounding` or, worse, make it
          tautological: grounding exists to prove the residue at auth 83 in
          THIS file is the one intended, and it can only do that if the name
          came from the file.
        * `rfd3_atoms` — the type's interface-facing pair, intersected with the
          atoms actually present on that residue. `validate_spec` checks every
          named atom exists, and it runs after the trim; catching it here
          instead means the operator hears about it before a multi-day
          campaign is staged rather than after.
        * `label_seq_id` — left `**UNVERIFIED**` in the markdown so
          `_correct_label_seq_ids` fills it from gemmi, which is the mechanism
          this repo already trusts for the model's own tables. Never derived
          by counting.
        """
        import gemmi
        from src.foundry_spec import MAX_HOTSPOTS
        from src.structure_tools import is_chain_residue

        wanted = self.parse_hotspot_spec(spec)
        # A bare number means the target chain.
        if "" in wanted:
            wanted.setdefault(target_chain, []).extend(wanted.pop(""))
        # A molecular glue's epitope spans BOTH chains, so the partner is a
        # legal hotspot chain for it and for nothing else. `partner_chain` is
        # passed only by the glue caller, so every existing call gets the
        # identical refusal string.
        allowed = {target_chain} | ({partner_chain} if partner_chain else set())
        foreign = [c for c in wanted if c not in allowed]
        if foreign:
            if partner_chain:
                raise PipelineBlockedError(
                    f"--hotspots names chain(s) {foreign} but this glue run "
                    f"designs against {target_chain!r} and {partner_chain!r}. "
                    f"A glue's epitope spans exactly the two chains it holds "
                    f"together; to use different ones pass --chains.")
            raise PipelineBlockedError(
                f"--hotspots names chain(s) {foreign} but the target chain is "
                f"{target_chain!r}. Hotspots are the epitope ON the target; "
                f"to design against a different chain pass --chains.")

        path = self._binder_structure_path(pdb_id)
        st = gemmi.read_structure(str(path))
        st.setup_entities()
        by_chain_auth: dict[str, dict[int, Any]] = {}
        for cid in [c for c in (target_chain, partner_chain) if c]:
            chain = next((c for c in st[0] if c.name == cid), None)
            if chain is None:
                raise PipelineBlockedError(
                    f"chain {cid!r} is not in {path.name} "
                    f"(chains: {sorted(c.name for c in st[0])})")
            by_chain_auth[cid] = {r.seqid.num: r for r in chain
                                  if is_chain_residue(r)}

        residues: list[dict] = []
        # Target chain first, then the partner, so the table reads in the same
        # order the contig will: the glue's own chain order, not the order the
        # operator happened to type.
        ordered = [(c, a) for c in (target_chain, partner_chain) if c
                   for a in wanted.get(c, [])]
        for cid, auth in ordered:
            res = by_chain_auth[cid].get(auth)
            if res is None:
                raise PipelineBlockedError(
                    f"--hotspots names {cid}{auth}, which is not a "
                    f"residue of chain {cid} in {path.name}. Author "
                    f"numbering in a crystal structure often differs from the "
                    f"canonical isoform's — check the file, not UniProt.")
            present = {a.name for a in res}
            picked = [a for a in self._RFD3_SIDECHAIN_ATOMS.get(res.name, ())
                      if a in present]
            if not picked:
                for fallback in ("CB", "CA"):
                    if fallback in present:
                        picked = [fallback]
                        break
            if not picked:
                raise PipelineBlockedError(
                    f"{res.name}{auth} in {path.name} carries none of the "
                    f"atoms RFD3 could use as a hotspot (present: "
                    f"{sorted(present)}). Pick another residue.")
            residues.append({"residue": res.name, "auth_seq_id": auth,
                             "label_seq_id": "**UNVERIFIED**",
                             "rfd3_atoms": ",".join(picked),
                             "chain": cid})

        if len(residues) > MAX_HOTSPOTS:
            # A warning is right when a SKILL overshoots the cap (the builder
            # cannot know which to drop). An operator can, so this is an
            # error: more hotspots is not stricter, and RFD3's hit rate on a
            # large set falls as the set grows, which weakens the engagement
            # gate rather than tightening it.
            raise PipelineBlockedError(
                f"--hotspots names {len(residues)} residues; the cap is "
                f"{MAX_HOTSPOTS} ({', '.join(r['residue'] + str(r['auth_seq_id']) for r in residues)}). "
                f"Keep the compact hydrophobic cluster and drop rim/polar "
                f"positions — a bigger set lowers RFD3's hit rate and weakens "
                f"the hotspot-engagement gate.")
        return residues

    def _write_override_interface_report(
        self, out: Path, intel: dict[str, str], residues: list[dict],
        query: str,
    ) -> None:
        """A `21_interface.md` in the shape the LLM stage would have written.

        Deliberately the DISRUPT four-column form verbatim, because two
        separate regexes must match it: `_correct_label_seq_ids`
        (`| RES | auth | label |`, three-letter name) and
        `handoff.parse_hotspot_residues`. Writing the artifact rather than
        short-circuiting past it is what keeps `--start-from trim` resumable
        and keeps every guard on the override's path.
        """
        target_chain = intel.get("target_chain", "")

        def _table(rows: list[dict]) -> str:
            return "\n".join(
                f"| {r['residue']} | {r['auth_seq_id']} | {r['label_seq_id']} | "
                f"{r['rfd3_atoms']} |" for r in rows)

        # Each residue under ITS OWN chain, not under target_chain. A glue's
        # partner-chain hotspots would otherwise be written as though they sat
        # on the target — which on 4ZGM is invisible, because every partner
        # hotspot number also exists on chain A.
        picks = "\n".join(
            f"    {r.get('chain') or target_chain}{r['auth_seq_id']}: "
            f"{r['rfd3_atoms']}" for r in residues)

        chains_present: list[str] = []
        for r in residues:
            c = r.get("chain") or target_chain
            if c not in chains_present:
                chains_present.append(c)

        if len(chains_present) > 1:
            # One chain-headed sub-table per chain, in the four-column form
            # both regexes read, with the heading spelt `Chain <id> — ...`
            # and NO colon after `Chain`: that is the anchor
            # `handoff._CHAIN_HEADING` matches, and it is the form every
            # report already on disk uses, which matters because
            # `_correct_label_seq_ids` re-parses those on every resume.
            blocks = []
            for c in chains_present:
                rows = [r for r in residues if (r.get("chain") or target_chain) == c]
                blocks.append(
                    f"Chain {c} — Region 1: operator-specified — selected "
                    f"{len(rows)} of {len(rows)} residues:\n\n"
                    f"| Residue | auth_seq_id | label_seq_id | RFD3 sidechain "
                    f"atoms |\n|---|---|---|---|\n{_table(rows)}\n")
            hotspot_block = "\n".join(blocks)
        else:
            hotspot_block = (
                f"Target chain {target_chain} — Region 1: operator-specified — "
                f"selected {len(residues)} of {len(residues)} residues:\n\n"
                f"| Residue | auth_seq_id | label_seq_id | RFD3 sidechain atoms |\n"
                f"|---|---|---|---|\n{_table(residues)}\n")
        body = (
            f"**The epitope was specified by the operator, not chosen by a "
            f"model.** `--hotspots` was given, so the "
            f"`complex-structure-analysis` stage was not called and no LLM "
            f"selected these residues. Residue names and sidechain atoms were "
            f"read from the structure; the numbers are the operator's.\n\n"
            f"Objective as stated: {query}\n\n"
            f"### MODEL-READY HOTSPOTS [{intel.get('design_intent', 'disrupt').upper()}]\n\n"
            f"{hotspot_block}\n"
            f"#### RFD3 select_hotspots\n\nselect_hotspots:\n{picks}\n")
        self._write_binder_report(
            out, "Interface analysis (operator-specified hotspots)", body,
            {"pdb_id": intel.get("pdb_id", ""),
             "target_chain": target_chain,
             # Normalised, so an operator-specified epitope on a single target
             # writes a blank the trim now accepts rather than a placeholder
             # it would read as a chain. This path used to emit whatever
             # target-intel held and then fail in `_stage_trim`.
             "partner_chain": chain_id_or_blank(intel.get("partner_chain")),
             # The authoritative statement of which chains carry hotspots,
             # same field the STABILIZE skill template writes. Without it the
             # parser falls back to the positional A/B reading on re-parse.
             "target_chains": ", ".join(chains_present),
             "design_intent": intel.get("design_intent", "disrupt"),
             "modality": intel.get("modality", "mini_protein"),
             "hotspot_source": "operator"})

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
        # A local structure has no accession to look up, so the FILE is named.
        # Left as "PDB LOCAL-MY_TARGET" the skill has an id it cannot resolve
        # against RCSB and no path to open — the same shape of failure as the
        # PD-L1 case below, where a stage given only a description concluded no
        # structure existed and asked for one.
        where = (f"the local file {self._binder_structure_path(pdb)}"
                 if self._local_structure_stem(pdb)
                 else f"PDB {pdb} (already downloaded to data/structures/)")
        # The partner half is OMITTED for a single target rather than
        # rendered from empty strings. With no partner the old line read
        # "..., 8FYU chain B = chain B,  = chain . Analyse this interface
        # directly", which is the precise shape this method's own docstring
        # blames for the PD-L1/7CZD failure — a stage handed a degenerate
        # description decided no usable structure existed and asked for a
        # different one. It also tells the model what to write back, because
        # `skills/complex-structure-analysis/SKILL.md`'s handoff template has
        # no single-target variant (it documents an INHIBIT_ACTIVE_SITE
        # hotspot table but still asks for `partner_chain: <chain ID of the
        # binding partner>`), and left to invent a value it wrote `none` —
        # which used to validate as a chain id.
        partner_chain = chain_id_or_blank(intel.get("partner_chain"))
        if partner_chain in ("", MALFORMED_CHAIN):
            what = (
                f"{intel.get('target_gene')} = chain "
                f"{intel.get('target_chain', '?')}. This is a SINGLE-TARGET "
                f"campaign: there is no partner chain and no interface to "
                f"analyse. Pick the pocket or functional surface a binder "
                f"should occupy, and leave `partner_chain` BLANK in the "
                f"handoff — do not write 'none' or invent a chain id")
        else:
            what = (
                f"{intel.get('target_gene')} = chain "
                f"{intel.get('target_chain', '?')}, "
                f"{intel.get('partner_name') or 'the partner'} = chain "
                f"{partner_chain}. Analyse this interface")
        q = (
            f"Structure: {where}, {what} directly "
            f"with the structure tools — do not search for a different "
            f"structure or ask for one. Goal: {goal}")
        out = dirs["binder"] / self._BINDER_STAGE_FILES["interface"]
        if self._hotspots:
            # The operator named the epitope, so there is nothing for a model
            # to choose. Write the artifact the stage would have written and
            # fall through to EVERY guard below unchanged: grounding matters
            # more here, not less, because the commonest way this goes wrong
            # is canonical-isoform numbering pasted against a construct-
            # numbered crystal, and grounding is exactly what catches it.
            # The partner is a legal hotspot chain for a molecular glue and
            # for nothing else, so it is passed only when the run's own intent
            # says so — a disrupt run keeps the identical refusal.
            residues = self._resolve_hotspot_override(
                self._hotspots, pdb, intel.get("target_chain", "") or "A",
                partner_chain=(chain_id_or_blank(intel.get("partner_chain"))
                               if is_glue_intent(intel.get("design_intent"))
                               else ""))
            self._write_override_interface_report(out, intel, residues, goal)
            logger.info(
                f"interface: {len(residues)} operator-specified hotspots "
                f"({', '.join(r['residue'] + str(r['auth_seq_id']) for r in residues)})"
                f" — no LLM call")
            handoff = self._parse_handoff(out.read_text(encoding="utf-8"))
        else:
            handoff = self._run_stage("complex-structure-analysis", q, [], out,
                                      stage="interface")
        text = out.read_text(encoding="utf-8")
        # Same skill, same table, same failure — and until now only the PPI
        # track corrected it. label_seq_id is a lookup in a file already on
        # disk, and models derive it by counting instead: 23/23 wrong on 5GRS,
        # 10/10 on 5GN0, every one off by the same amount. This is the
        # binder-track half of that fix, deliberately OUTSIDE any try/except —
        # an unverifiable numbering is not a warning.
        text = self._correct_label_seq_ids(
            text, out, self._binder_structure_path(result.pdb_id or pdb),
            handoff.get("target_chain") or intel.get("target_chain") or "A",
            handoff=handoff)
        hotspots = self._parse_hotspot_residues(text, handoff)
        if not hotspots:
            raise PipelineError(
                "the interface stage produced no MODEL-READY HOTSPOTS table; the "
                "RFD3 spec cannot be built without atom-level hotspots")
        self._verify_target_chain_assignment(intel, handoff, result.pdb_id or pdb)
        # ADDED alongside the three existing guards, not in place of any of
        # them. The binder track is less exposed than PPI — `target_gene` comes
        # from target_intel and the interface stage cannot rewrite it — but the
        # interface stage does choose `partner_chain`, and nothing checked that
        # it is still the partner target_intel picked out of the candidate
        # table. Same failure shape as the PPI CALCRL/CGRP swap.
        self._verify_partner_chain_is_requested(
            " / ".join(n for n in (intel.get("target_gene"),
                                   intel.get("partner_name")) if n),
            handoff, result.pdb_id or pdb,
            source="The target-intel stage")
        self._verify_hotspot_grounding(hotspots, result.pdb_id or pdb,
                                       intel.get("target_uniprot"))
        self._check_hotspot_atoms_are_buildable(hotspots, result.pdb_id or pdb)
        self._check_ortholog_conservation(hotspots, result.pdb_id or pdb, result)
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

    #: Which of a PPI pair's two named proteins the chain assignment was
    #: finally accepted against, so the numbering-frame advisory has an
    #: accession on the PPI track too. Class-level, because several tests
    #: build a PipelineRunner without running `__init__`.
    _ppi_target_uniprot: str | None = None

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

    def _infer_membrane_side(self, pdb_id: str, chain: str, uniprot: str,
                             hotspots: list[dict], topo) -> str | None:
        """
        Which face of the membrane is the declared epitope actually on?

        Returns a side name to restrict to, or None for "do not restrict" —
        which covers a soluble protein, an unmappable structure, and the two
        cases where restricting would be a guess: no hotspot could be placed,
        or the hotspots straddle both faces.

        This replaces a hardcoded "extracellular" default. A binder against a
        cell-surface receptor does have to bind the outside, but that is a
        property of THAT target, not a law: SCAP sits in the ER membrane and
        its SREBP-binding WD40 domain faces the cytosol, so "extracellular"
        names no real surface at all. The interface stage read the structure
        and chose an interface; the topology's job here is to keep the trim on
        the same face as that choice and strip the lipid-buried helices, not to
        veto the choice.

        A hotspot lying INSIDE the membrane is still an error, and is raised by
        the caller's `restriction_for` path — that one is not a matter of side.
        """
        from src.membrane_topology import (CYTOPLASMIC, EXTRACELLULAR,
                                           TRANSMEMBRANE, uniprot_to_auth)

        if not topo.fetched or not topo.is_membrane:
            return None
        mapping = uniprot_to_auth(pdb_id, chain, uniprot)
        if not mapping:
            logger.info(
                f"topology: no UniProt->author alignment for {pdb_id} chain "
                f"{chain}; not restricting by membrane side")
            return None
        auth_to_pos = {auth: pos for pos, auth in mapping.items()}

        counts: dict[str, int] = {}
        in_membrane = []
        for h in hotspots:
            try:
                auth = int(h["auth_seq_id"])
            except (KeyError, TypeError, ValueError):
                continue
            pos = auth_to_pos.get(auth)
            if pos is None:
                continue
            kind = topo.kind_at(pos)
            if kind == TRANSMEMBRANE:
                in_membrane.append(auth)
                continue
            if kind in (EXTRACELLULAR, CYTOPLASMIC):
                counts[kind] = counts.get(kind, 0) + 1
        if in_membrane:
            raise PipelineError(
                f"hotspot(s) {in_membrane} on {uniprot} lie INSIDE "
                f"the membrane (UniProt annotates them transmembrane). That "
                f"surface is buried in lipid in a cell, so a binder against it "
                f"cannot work whichever side the rest of the epitope is on. "
                f"Pick a site on one face.")
        if not counts:
            logger.info("topology: no hotspot could be placed on either face; "
                        "not restricting by membrane side")
            return None
        if len(counts) > 1:
            logger.warning(
                f"topology: hotspots straddle both faces "
                f"({counts}) — not restricting by membrane side. The epitope "
                f"spans the membrane, which no single binder can engage; check "
                f"the interface stage's chain assignment.")
            return None
        side = next(iter(counts))
        logger.info(
            f"topology: membrane_side inferred as {side!r} from {counts[side]} "
            f"declared hotspot(s) — TM residues are dropped either way")
        return side

    def _check_ortholog_conservation(self, hotspots_json: str, pdb_id: str,
                                     result: "PipelineResult") -> None:
        """
        When the target chain turned out to be an ORTHOLOG, is the epitope it
        carries actually present in the human protein?

        An ortholog structure is a legitimate, often unavoidable choice — no
        human SCAP/SREBP complex has ever been solved, and a site conserved
        between the two is the same site. What is not legitimate is designing
        against residues the human protein does not have: the binder is then
        optimised for a surface that exists only in the other organism.

        So this is the gate that replaces "reject every ortholog". It maps each
        declared hotspot onto human numbering (SIFTS, then a full-length global
        alignment — see `src.ortholog_check.hotspot_conservation` for why the
        obvious fragment alignment is not good enough) and requires
        `MIN_HOTSPOT_CONSERVATION` of them to be identical. It also pulls the
        human AlphaFold model, because the same alignment yields the epitope's
        human residue numbers, which is what makes that model usable as a
        design target instead of the ortholog crystal.

        Fail-open on anything it cannot compute: a UniProt outage must not
        halt a run. Only a MEASURED shortfall raises.
        """
        verdict = getattr(self, "_ortholog", None)
        if not verdict or not verdict.is_ortholog or not hotspots_json:
            return
        from src.ortholog_check import (MIN_HOTSPOT_CONSERVATION,
                                        fetch_alphafold_model,
                                        hotspot_conservation)

        data = json.loads(hotspots_json)
        chain = data.get("target_chain")
        hotspots = data.get("residues") or []
        human_acc = getattr(verdict, "human_uniprot", "") or self._ortholog_human_acc
        if not chain or not hotspots or not human_acc:
            return
        try:
            cons = hotspot_conservation(
                pdb_id, chain, hotspots, human_uniprot=human_acc,
                chain_uniprot=verdict.chain_uniprot)
        except Exception as exc:
            logger.warning(f"ortholog conservation check failed: {exc}")
            return
        if not cons.total:
            logger.warning("ortholog conservation check produced no rows — "
                           "the epitope could not be mapped to human numbering")
            return

        logger.info(f"  ortholog conservation ({cons.method}): {cons.summary()}")
        for row in cons.rows:
            logger.info(
                f"    {row['residue']}{row['auth_seq_id']} "
                f"({verdict.organism or 'ortholog'}) -> "
                f"{row['human_aa'] or '?'}{row['human_auth'] or '?'} (human) "
                f"— {row['status']}")

        af = None
        try:
            structures_dir = _ROOT / ((self.config.get("paths") or {}).get(
                "structures_dir", "data/structures"))
            af = fetch_alphafold_model(human_acc, structures_dir / "alphafold")
        except Exception as exc:
            logger.warning(f"could not fetch the human AlphaFold model: {exc}")

        result.ortholog_conservation = {
            "pdb_id": pdb_id, "chain": chain,
            "ortholog_uniprot": verdict.chain_uniprot,
            "organism": verdict.organism, "human_uniprot": human_acc,
            "identity": verdict.identity, "method": cons.method,
            "low_confidence": cons.low_confidence,
            "fraction_conserved": cons.fraction_conserved,
            "summary": cons.summary(), "rows": cons.rows,
            "human_alphafold_model": str(af) if af else None,
        }
        self._binder_checkpoint("ortholog_target", "structure", "gate",
                                result.ortholog_conservation)

        if cons.fraction_conserved < MIN_HOTSPOT_CONSERVATION:
            raise PipelineError(
                f"{pdb_id} chain {chain} is an ortholog "
                f"({verdict.organism or 'non-human'}, {verdict.chain_uniprot}) "
                f"and its declared epitope is NOT conserved in human "
                f"{human_acc}: {cons.summary()} — below the "
                f"{MIN_HOTSPOT_CONSERVATION:.0%} bar. Designing here would "
                f"optimise a binder for residues the human protein does not "
                f"carry. Pick a human structure, pick a conserved site on this "
                f"one, or design against the human AlphaFold model"
                + (f" now at {af}" if af else " (AlphaFold has no model for it)")
                + ". Per-hotspot human equivalents are in the "
                  "'ortholog_target' manifest checkpoint.")

    def _classify_target_chain(self, pdb_id: str, chain: str,
                               identity: float | None, gene: str,
                               uniprot: str):
        """
        Is a low-identity chain an ortholog of the target, or a different
        molecule? Fail-open to `mismatch` if the metadata lookup dies — the
        caller raises on `mismatch`, and a network blip must not silently
        convert a wrong-molecule campaign into an accepted one.
        """
        from src.ortholog_check import MISMATCH, ChainVerdict, classify_chain
        from src.target_resolve import entry_metadata

        desc, accs = "", []
        try:
            meta = (entry_metadata([pdb_id]) or {}).get(pdb_id.upper()) or {}
            info = (meta.get("chains") or {}).get(chain) or {}
            desc = info.get("description") or ""
            accs = list(info.get("uniprots") or [])
        except Exception as exc:
            logger.warning(f"could not read {pdb_id} chain metadata: {exc}")
        try:
            return classify_chain(identity=identity, description=desc,
                                  gene=gene, uniprot=uniprot,
                                  chain_accessions=accs)
        except Exception as exc:
            logger.warning(f"ortholog classification failed for {pdb_id}: {exc}")
            return ChainVerdict(verdict=MISMATCH, identity=identity,
                                description=desc,
                                reason="ortholog classification was not possible")

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
                            # High identity settles WHICH PROTEIN this is, not
                            # which ORGANISM. Mouse Tead4 is 96% identical to
                            # human TEAD4 — past every "same protein" threshold
                            # — so returning here skipped the conservation
                            # check entirely on a non-human structure. It is
                            # 12/12 conserved on that target, and the only way
                            # to know is to measure it; the alternative is
                            # trusting the discovery stage's prose.
                            verdict = self._classify_target_chain(
                                pdb_id, target_chain, target_id, gene, uniprot)
                            if verdict.is_ortholog:
                                self._ortholog = verdict
                                self._ortholog_human_acc = uniprot
                                logger.info(
                                    f"  — but it is a NON-HUMAN ortholog: "
                                    f"{verdict.reason}. The declared hotspots "
                                    f"will be checked for conservation in human "
                                    f"{gene or uniprot}.")
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
                        # Low identity is NOT automatically the wrong molecule.
                        # An ortholog of the target sits in exactly the same
                        # 20-40% band as an unrelated chain (5GRS's S. pombe
                        # Scp1 is 29% identical to human SCAP; the PD-L1
                        # incident's VHH was ~20%), so sequence alone cannot
                        # separate them. `classify_chain` asks the chain's OWN
                        # UniProt entry for its recommended protein name and
                        # organism — an ortholog carries the identical protein
                        # name under a different taxid. An ortholog is a
                        # legitimate design target when the site is conserved,
                        # so it is recorded and passed to the conservation
                        # check, not rejected here.
                        verdict = self._classify_target_chain(
                            pdb_id, target_chain, target_id, gene, uniprot)
                        if verdict.is_ortholog:
                            self._ortholog = verdict
                            self._ortholog_human_acc = uniprot
                            logger.warning(
                                f"  ⚠ target_chain={target_chain} in {pdb_id} "
                                f"is an ORTHOLOG, not the human protein: "
                                f"{verdict.reason}. Continuing — the declared "
                                f"hotspots will be checked for conservation in "
                                f"human {gene or uniprot} before any GPU work.")
                            return
                        raise PipelineError(
                            f"target_chain={target_chain} in {pdb_id} is only "
                            f"{target_id:.0%} identical to {gene or uniprot} "
                            f"({uniprot})"
                            + (f", and partner_chain={partner_chain} is only "
                               f"{partner_id:.0%}" if partner_id is not None
                               else "")
                            + " — neither chain looks like the intended target "
                              "by sequence. " + verdict.reason)
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

    def _verify_hotspot_grounding(self, hotspots_json: str, pdb_id: str,
                                  uniprot: str | None = None) -> None:
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
        clearly had memorised. Measured since: 8ZNL numbers chain B exactly
        `canonical + 1` (its deposited `_struct_ref_seq` maps Q9NZQ7 19-132 onto
        auth 20-133), while 7CZD numbers author == canonical, so the same
        remembered numbers are RIGHT on 7CZD and one short on 8ZNL. The residues
        were the right ones; the frame was wrong. `validate_spec` caught THIS
        case only by luck (the stated atoms happened not to exist on VAL); a
        mismatch that happened to share atom names would have silently trimmed
        and designed against the wrong residues. Fail loud here, before a design
        spec is even built.

        **What this check does and does not cover.** It answers "is the residue
        at auth N what the table claims", never "is auth N the residue the
        literature means". Against a uniform frame offset it is therefore a
        PROXY, and a measured one: over 8ZNL chain B's modelled span a +1 offset
        is caught at 105 of 113 positions and silent at 8, where the neighbour
        happens to share a residue type — ~93% per hotspot, so a whole table
        slipping through is vanishingly unlikely but a single row can. The
        primitive that would answer the second question directly exists
        (`membrane_topology.uniprot_to_auth` returns a uniform {+1} here and {0}
        for 7CZD), and `_hotspot_numbering_frame` now routes the declared
        hotspots through it. That is ADVISORY, never a gate — a non-zero offset
        is ordinary and legal in a deposited structure, as 8ZNL shows — so it
        runs only after every check below has passed, and only when the caller
        already resolved a target accession.

        **Every row is looked up on the chain it was attributed to.** A
        molecular-glue pocket spans both proteins, so its table legitimately
        carries rows on two chains; grounding all of them against
        `target_chain` reports real residues of the co-target as absent or
        misnamed. A row with no `chain` key is a legacy table that never said
        which chain it meant, and still groups under `target_chain` — so the
        cross-chain refusal is preserved for exactly the shape that has not
        answered the question, and relaxed only for a table that has.

        `_hotspot_numbering_frame` is likewise called once per chain, and the
        accession only ever reaches the chain the caller resolved it for.
        Passing it for both would translate the co-target's rows through the
        target's UniProt alignment and print canonical ids belonging to a
        different protein: on 5VAI all seven rows would be reported in chain
        R's frame, four of them wrongly.
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
        # Group each row under the chain it was attributed to. A row with no
        # `chain` key groups under the declared target_chain, so a
        # single-chain table collapses to exactly one pass and every message
        # below is produced verbatim.
        by_chain: dict[str, list[dict]] = {}
        for h in residues:
            by_chain.setdefault(str(h.get("chain") or chain), []).append(h)
        if len(by_chain) > 1:
            logger.info(
                "hotspot grounding: verifying "
                + ", ".join(f"{len(v)} residue(s) on chain {k}"
                            for k, v in by_chain.items()))

        mismatches: list[tuple[str, str]] = []
        absent: list[tuple[str, str, int]] = []
        for c, rows in by_chain.items():
            self._ground_one_chain(path, pdb_id, c, chain, rows,
                                   mismatches, absent)

        if absent:
            # Say WHICH chain the residues are on when one chain holds them
            # all under their claimed names — that is the real diagnosis, and
            # the commonest cause is a table whose rows belong to the partner
            # without saying so (`div_wildcard_tnbc` put chain U's 447-453
            # under target_chain A). A table that DID attribute its rows is
            # grounded per chain above and never reaches here for that reason.
            ordered: list[str] = []
            for c, _n, _a in absent:
                if c not in ordered:
                    ordered.append(c)
            parts = []
            for c in ordered:
                rows_c = [(n, a) for cc, n, a in absent if cc == c]
                hint = self._chain_holding_residues(path, rows_c, exclude=c)
                names = ", ".join(f"{n}{a}" for n, a in rows_c)
                parts.append(
                    f"hotspot table names residue(s) that do not exist in "
                    f"{pdb_id} chain {c}: {names}."
                    + (f" All of them are present under those names in chain "
                       f"{hint} — the table is attributing another chain's "
                       f"residues to the target. Re-run the stage against one "
                       f"chain, or pick a structure whose target chain carries "
                       f"the epitope." if hint else
                       " Re-run the stage, or pick a different structure — an "
                       "unmodelled residue cannot be designed against."))
            raise PipelineError(" ".join(parts))
        if mismatches:
            # The chain is named per entry only when more than one is
            # involved, so a single-chain run's message is unchanged.
            multi = len({c for c, _ in mismatches}) > 1
            rendered = ", ".join(f"{t} on chain {c}" if multi else t
                                 for c, t in mismatches)
            raise PipelineError(
                f"hotspot table is not grounded in {pdb_id}'s actual numbering: "
                f"{rendered}. The interface stage likely reported "
                f"textbook/literature numbering for a well-known protein instead "
                f"of reading this specific structure's residues — re-run the "
                f"stage, or pick a different structure.")

        # Every hard check above has passed, which means each declared residue
        # is real and correctly named. That leaves the one question grounding
        # structurally cannot answer — whether auth N is the residue the
        # literature means — so ask it here, advisorily. The accession belongs
        # to the target chain alone; `_hotspot_numbering_frame` returns
        # immediately without one, so a co-target is silent rather than wrong.
        for c, rows in by_chain.items():
            self._hotspot_numbering_frame(
                rows, c, pdb_id, uniprot if c == chain else None)

    def _ground_one_chain(self, path, pdb_id: str, c: str, chain: str,
                          rows: list[dict],
                          mismatches: list[tuple[str, str]],
                          absent: list[tuple[str, str, int]]) -> None:
        """Ground the rows attributed to ONE chain. Appends, never raises
        except for a chain that is not in the file at all.

        Split out of `_verify_hotspot_grounding` unchanged so the per-chain
        loop runs the identical checks it always did; the caller owns the
        refusals so a two-chain table reports both chains at once instead of
        stopping at the first.
        """
        from src.structure_tools import get_sequence_map

        try:
            seq_map = get_sequence_map(str(path), c)
            # get_sequence_map RETURNS {"error": ...} for a missing/empty
            # chain rather than raising, so this subscript has to be inside
            # the guard too — outside it, an absent chain aborted the run
            # with a bare KeyError instead of the intended fail-open warning.
            #
            # Bind under its OWN name. This used to reuse `residues`, which
            # shadowed the CLAIMED hotspot list parsed above, so the loop
            # below compared the structure against itself: `three_letter`
            # rows carry no "residue" key, `claimed` was always "",
            # every iteration hit the `continue`, and `mismatches` could
            # never be non-empty. The guard this docstring describes has
            # never once fired.
            struct_residues = seq_map.get("residues")
        except Exception as exc:
            logger.warning(
                f"could not verify hotspot grounding against {path}: {exc}")
            return
        if struct_residues is None:
            # get_sequence_map RETURNS {"error": ...} rather than raising, so
            # an absent chain used to land in the except above and be treated
            # as "could not verify" — a fail-open for a condition that is not
            # inconclusive at all. Distinguish the two: a file we cannot read
            # still fails open (the warning above), a chain that is not in a
            # file we read fine does not.
            logger.warning(
                f"hotspot grounding: {seq_map.get('error') or 'no residues'} "
                f"for chain {c} in {path.name}")
            struct_residues = []
        by_auth = {r["auth_seq_id"]: r["three_letter"] for r in struct_residues}
        if not by_auth:
            # get_sequence_map returns {"error": ...} for an absent chain and
            # an empty list for an empty one, so reaching here means the
            # target_chain the stage declared is not in this file. That is not
            # an inconclusive read to fail open on — every subsequent stage
            # addresses residues by that chain id. Measured over the 67 reports
            # on disk with a local structure: one hit, `div_standard_tuberculosis`
            # on 3FLN, which has exactly ONE chain (C) and got 11 hotspots on a
            # chain A that has never existed.
            if c == chain:
                raise PipelineError(
                    f"the interface stage declared target_chain {chain!r}, which "
                    f"does not exist in {path.name} — no hotspot can be grounded "
                    f"against it. Re-run the stage, or name a chain the entry "
                    f"actually has.")
            raise PipelineError(
                f"the hotspot table attributes residues to chain {c!r}, which "
                f"does not exist in {path.name} — no hotspot can be grounded "
                f"against it. Re-run the stage, or name a chain the entry "
                f"actually has.")

        for h in rows:
            auth = h.get("auth_seq_id")
            claimed = str(h.get("residue", "")).upper()
            if auth is None or not claimed:
                continue
            actual = by_auth.get(auth)
            if actual is None:
                # A residue that is NOT THERE used to `continue` — counted as
                # "no mismatch", which is the opposite of what this guard is
                # for. Nothing downstream fails cleanly on it either: the
                # ortholog check scored six such residues into a
                # `fraction_conserved: 0.625` and came within one residue of
                # rejecting a run for a conservation failure that was really a
                # chain-attribution bug, and the trim finally died several
                # stages later with a bare residue list. Fail here instead.
                absent.append((c, claimed, auth))
                continue
            if actual != claimed:
                mismatches.append(
                    (c, f"{claimed}{auth} (structure has {actual}{auth})"))

    @staticmethod
    def _hotspot_numbering_frame(residues: list[dict], chain: str,
                                 pdb_id: str, uniprot: str | None) -> None:
        """Say what numbering FRAME the declared hotspots are in. Advisory.

        `_verify_hotspot_grounding` answers "is the residue at auth N what the
        table claims". It cannot answer "is auth N the residue the literature
        means", and against a uniform frame offset it is only a PROXY: measured
        over 8ZNL chain B's modelled span, a +1 offset is caught at 105 of 113
        positions and SILENT at 8, where the neighbouring residue happens to
        share a type. ~93% per hotspot — a ten-row table slipping through
        whole is ~1e-11, a single row is one in fourteen.

        So state the frame instead of inferring it. `uniprot_to_auth` returns
        the DEPOSITED correspondence (RCSB's entity<->UniProt alignment, two
        hops, no local guesswork): a uniform `{+1}` for 8ZNL chain B, `{0}` for
        7CZD. Both are correct deposits — a non-zero offset is ordinary and
        entirely legal — which is exactly why this must never gate. It is here
        so the numbers a reader will compare against a paper are printed in the
        paper's own frame, and so the commonest real mistake (canonical-isoform
        numbers pasted against a construct-numbered crystal) is visible in the
        log rather than only in a campaign's results.

        Fails open on everything: no accession, no alignment for it in this
        entry, a network failure, an unmapped hotspot. Nothing here can end a
        run.
        """
        if not uniprot or not chain or not residues:
            return
        declared = sorted({int(h["auth_seq_id"]) for h in residues
                           if h.get("auth_seq_id") is not None})
        if not declared:
            return
        try:
            from src.membrane_topology import uniprot_to_auth

            u2a = uniprot_to_auth(pdb_id, chain, uniprot)
        except Exception as exc:            # pragma: no cover - network
            logger.debug(f"numbering-frame advisory unavailable: {exc}")
            return
        if not u2a:
            logger.debug(
                f"numbering frame: {pdb_id} has no {uniprot} alignment for "
                f"chain {chain} — hotspot frame not stated")
            return

        auth_to_uni = {a: u for u, a in u2a.items()}
        offsets = {a - u for u, a in u2a.items()}
        mapped = [(a, auth_to_uni[a]) for a in declared if a in auth_to_uni]
        unmapped = [a for a in declared if a not in auth_to_uni]

        if offsets == {0}:
            # Worth one line: it is the case where a literature number can be
            # used as-is, and a reader otherwise cannot tell this entry from
            # the one where it cannot.
            logger.info(
                f"  numbering frame: {pdb_id} chain {chain} numbers author == "
                f"{uniprot} canonical, so the hotspot ids are canonical ids")
            return

        pairs = ", ".join(f"{a}={u}" for a, u in mapped[:8])
        if len(offsets) == 1:
            off = next(iter(offsets))
            logger.warning(
                f"  ⚠ numbering frame: {pdb_id} chain {chain} numbers author = "
                f"{uniprot} canonical {off:+d} throughout, so the declared "
                f"hotspots are auth=canonical {pairs}"
                + (f" ({unmapped} outside the alignment)" if unmapped else "")
                + f". Grounding has confirmed each residue is real at its auth "
                f"id — which it would also be had the table been written in "
                f"canonical numbering and landed on same-named neighbours. So "
                f"if these ids came from a paper rather than from this file, "
                f"each points {abs(off)} residue"
                + ("s" if abs(off) != 1 else "")
                + (" short" if off > 0 else " long") + ".")
        else:
            logger.warning(
                f"  ⚠ numbering frame: {pdb_id} chain {chain} aligns to "
                f"{uniprot} with {len(offsets)} different offsets, so there is "
                f"no single frame to state (an insertion, a chimera or several "
                f"aligned regions). Per hotspot, auth=canonical {pairs}"
                + (f" ({unmapped} outside the alignment)" if unmapped else "")
                + ".")

    #: A hotspot whose only atoms are backbone, or CB on a residue whose
    #: sidechain IS a CB, tells RFD3 almost nothing about what to pack
    #: against. Measured on 5VAI's 7-hotspot glue table: 3 hotspots name
    #: atoms the file does not model at all and the 4 that pass are two ALA
    #: (CB is the whole sidechain) and two GLY (`CA,C` is pure backbone), for
    #: a net informative sidechain steer of ZERO.
    _BACKBONE_ATOMS = frozenset({"N", "CA", "C", "O", "OXT"})
    _NO_SIDECHAIN_AA = frozenset({"GLY", "ALA"})

    def _check_hotspot_atoms_are_buildable(self, hotspots_json: str,
                                           pdb_id: str) -> None:
        """Do the declared `rfd3_atoms` exist, and do they steer anything?

        `foundry_spec.validate_spec` already refuses a hotspot naming an atom
        its residue does not have — and on 5VAI it was the ONLY check that
        caught anything. But it runs at SPEC-BUILD time, which is after the
        interface stage's LLM call, after grounding, and after the trim. The
        same question is answerable the moment the hotspot table is parsed,
        off a file already on disk.

        Deliberately a WARNING, not a refusal, in both places it is called.
        Whether the atoms matter depends on the GENERATOR: BoltzGen steers
        from `binding:` label_seq entries and never reads `rfd3_atoms`, so a
        hard failure here would end a legitimate BoltzGen campaign over a
        column its engine ignores. The run that does build an RFD3 contig
        still hard-fails at `validate_spec`, unchanged — this only moves the
        DIAGNOSIS earlier, to where an operator can still pick a different
        structure without paying for another stage.

        Two separate findings, because they have different causes and
        different fixes:

        * **Atoms that do not exist** — usually a low-resolution structure
          with truncated sidechains. 5VAI is 3.3 A cryo-EM at 83.6%/93.0%
          sidechain completeness, so a global "this file models no
          sidechains" test (which an earlier scope proposed) would NOT have
          fired; what is wrong is the specific residues chosen. The fix is a
          better entry, and `human_alternatives` in the pathway checkpoint
          usually names one.
        * **Atoms that exist but steer weakly** — backbone-only or
          CB-on-ALA/GLY. Resolution-independent, and no spec check catches it
          because every named atom is really there.
        """
        import gemmi

        try:
            data = json.loads(hotspots_json)
        except (TypeError, ValueError):
            return
        residues = data.get("residues") or []
        chain = data.get("target_chain")
        if not residues or not chain:
            return
        structures_dir = _ROOT / (
            (self.config.get("paths") or {}).get("structures_dir", "data/structures"))
        ba1 = structures_dir / f"{pdb_id.upper()}_ba1.cif"
        path = ba1 if ba1.exists() else structures_dir / f"{pdb_id.upper()}.cif"
        try:
            st = gemmi.read_structure(str(path))
            st.setup_entities()
            atoms_at = {(ch.name, int(res.seqid.num)): (res.name, {a.name for a in res})
                        for ch in st[0] for res in ch}
        except Exception as exc:                                  # noqa: BLE001
            logger.warning(f"could not check hotspot atoms against {path}: {exc}")
            return

        absent, weak = [], []
        for h in residues:
            auth = h.get("auth_seq_id")
            if auth is None:
                continue
            found = atoms_at.get((str(h.get("chain") or chain), int(auth)))
            if found is None:
                continue            # _verify_hotspot_grounding owns this case
            res_name, have = found
            wanted = [a.strip() for a in str(h.get("rfd3_atoms") or "").split(",")
                      if a.strip()]
            missing = [a for a in wanted if a not in have]
            if missing:
                absent.append(f"{res_name}{auth} wants {','.join(missing)} "
                              f"(has {','.join(sorted(have))})")
            elif (res_name.upper() in self._NO_SIDECHAIN_AA
                  or all(a in self._BACKBONE_ATOMS for a in wanted)):
                weak.append(f"{res_name}{auth} ({','.join(wanted)})")

        if absent:
            logger.warning(
                f"  ⚠ {len(absent)} of {len(residues)} hotspots name atoms "
                f"{pdb_id} does not model: {'; '.join(absent[:4])}"
                + (f" (+{len(absent) - 4} more)" if len(absent) > 4 else "")
                + ". RFD3 spec-building will REFUSE these. Truncated "
                  "sidechains in a low-resolution entry are the usual cause; "
                  "a higher-resolution structure of the same complex is the "
                  "fix, not a re-run.")
        if len(weak) >= max(1, len(residues) // 2):
            logger.warning(
                f"  ⚠ {len(weak)} of {len(residues)} hotspots steer weakly — "
                f"backbone-only atoms, or CB on a residue whose sidechain is "
                f"CB: {', '.join(weak[:6])}. RFD3 accepts these and they "
                f"carry little information about what to pack against.")

    @staticmethod
    def _chain_holding_residues(path: Path, wanted: list[tuple[str, int]],
                                exclude: str) -> str | None:
        """Which OTHER chain carries every (name, auth) pair? Diagnosis only.

        Returns a chain id when exactly one other chain holds all of them
        under the claimed names, else None. Best-effort: a failure to read
        the file costs a sentence of the error message, never the error.
        """
        try:
            import gemmi

            st = gemmi.read_structure(str(path))
            st.setup_entities()
            hits = []
            for ch in st[0]:
                if ch.name == exclude:
                    continue
                by_auth = {r.seqid.num: r.name.upper() for r in ch}
                if all(by_auth.get(a) == n for n, a in wanted):
                    hits.append(ch.name)
            return hits[0] if len(hits) == 1 else None
        except Exception:
            return None

    @staticmethod
    def _combine_allowed(restrict, chimera_keep: set[int] | None) -> set[int] | None:
        """Intersect the topology restriction with the chimera filter.

        Either may be absent. Both are "keep only these author ids", so the
        conjunction is the intersection; None means no restriction at all.
        """
        topo = set(restrict.allowed_auth) if (restrict and restrict.applies) else None
        if topo is None:
            return chimera_keep
        if chimera_keep is None:
            return topo
        return topo & chimera_keep

    def _target_accession_residues(self, pdb_id: str, chain: str,
                                   uniprot: str) -> set[int] | None:
        """
        On a CHIMERA, the author residues that are actually the target.

        Crystallisation constructs routinely fuse a soluble partner into a
        receptor to make it behave — 5XEZ is GCGR-endolysin, 5TGZ is
        CB1R-flavodoxin — and RCSB reports both accessions on the one chain.
        The fusion partner is not the target: it should never carry a hotspot,
        never be trimmed *to*, and never count against the residue budget.

        The deposited RCSB entity alignment already says which author residues
        correspond to which UniProt sequence, so this needs no sequence
        alignment of our own and no heuristic — the same primitive the membrane
        topology path maps its segments through.

        Returns None (meaning "no restriction") unless the chain really does
        carry more than one accession and the mapping is non-empty, so a normal
        single-protein chain is untouched.
        """
        if not (pdb_id and chain and uniprot):
            return None
        from src.membrane_topology import uniprot_to_auth
        from src.target_resolve import entry_metadata

        try:
            meta = (entry_metadata([pdb_id]) or {}).get(pdb_id.upper()) or {}
            accs = [a for a in ((meta.get("chains") or {}).get(chain) or {})
                    .get("uniprots", []) if a]
        except Exception as exc:
            logger.debug(f"chimera check skipped for {pdb_id} {chain}: {exc}")
            return None
        if len(accs) < 2:
            return None                      # not a fusion construct
        try:
            mapping = uniprot_to_auth(pdb_id, chain, uniprot)
        except Exception as exc:
            logger.warning(
                f"  ⚠ {pdb_id} chain {chain} is a fusion construct ({accs}) but "
                f"the {uniprot} alignment could not be read ({exc}) — the "
                f"fusion partner is NOT being excluded")
            return None
        keep = {int(a) for a in mapping.values() if a is not None}
        if not keep:
            return None
        logger.info(
            f"  {pdb_id} chain {chain} is a fusion construct ({', '.join(accs)}); "
            f"keeping the {len(keep)} residues that align to {uniprot} and "
            f"excluding the fusion partner from the design target")
        return keep

    def _designable_chain_sizes(self, pdb_id: str, chain_counts: dict[str, int],
                                over: int) -> dict[str, int]:
        """
        For each oversized chain, how many residues survive TM stripping.

        A membrane protein's raw chain length is not the number that will be
        designed against. `structure_trim` drops the transmembrane span AND the
        opposite face before anything reaches RFD3 — always, because in an
        isolated structure a TM helix is an exposed hydrophobic slab that
        preferentially attracts binders which cannot work in a cell. Judging the
        target-size policy on the raw length therefore refuses targets that are
        comfortably designable once cropped.

        Measured on the run that motivated this: 5XEZ chain A is a
        GCGR-endolysin fusion, 574 residues, so the structure stage returned
        NO_GO against a 500-residue limit and recommended "crop to the ECD or
        use an isolated-ECD PDB" — which is exactly what the trim stage two
        steps later does automatically. GCGR's UniProt topology leaves 167
        extracellular residues, inside even the 220-residue trim budget, and all
        four declared hotspots sit in the 26-136 ECD.

        No new heuristic: the segments are deposited UniProt annotation mapped
        into author numbering through the deposited RCSB entity alignment, and
        the real cut still happens in `structure_trim` with every one of its
        refusals intact (MIN_TARGET_RESIDUES, the exposed-hydrophobic rules,
        BSA retention). This only stops the structure stage refusing early on a
        number that is not the operative one.

        Returns `{chain: designable_count}` for oversized chains where the
        answer is both known and smaller. Silent for soluble proteins, for
        chains with no resolvable accession, and whenever the topology lookup
        fails — in each case the caller keeps the raw number.
        """
        from src.membrane_topology import fetch_topology, restriction_for
        from src.target_resolve import entry_metadata

        oversized = {c: n for c, n in chain_counts.items() if n and n > over}
        if not oversized:
            return {}
        try:
            meta = (entry_metadata([pdb_id]) or {}).get(pdb_id.upper()) or {}
        except Exception as exc:
            logger.debug(f"designable-size lookup skipped for {pdb_id}: {exc}")
            return {}
        chains = meta.get("chains") or {}

        out: dict[str, int] = {}
        for chain, raw in oversized.items():
            accs = [a for a in ((chains.get(chain) or {}).get("uniprots") or []) if a]
            for acc in accs:
                try:
                    topo = fetch_topology(acc)
                    if not (topo.fetched and topo.is_membrane):
                        continue
                    restrict = restriction_for(pdb_id, chain, acc,
                                               side="extracellular", topology=topo)
                except Exception as exc:
                    logger.debug(f"topology lookup failed for {acc}: {exc}")
                    continue
                if restrict.applies and 0 < len(restrict.allowed_auth) < raw:
                    out[chain] = len(restrict.allowed_auth)
                    logger.info(
                        f"  chain {chain} is {raw} residues but only "
                        f"{out[chain]} are designable once the trim drops the "
                        f"transmembrane span and the cytoplasmic face")
                    break
        return out

    #: Chains that are crystallisation or cryo-EM scaffolding rather than
    #: biology: a designed binder never targets them, and their presence means
    #: the entry was solved to study something other than the requested pair.
    _SCAFFOLD_MARKERS = (
        "nanobody", "fab ", "antibody", "single-chain", "scfv",
        "guanine nucleotide-binding", "g(s) subunit", "g(i)/g(s)",
        "lysozyme", "endolysin", "flavodoxin", "bril", "apocytochrome",
        "rubredoxin", "thermostabilised apocytochrome", "green fluorescent",
        "maltose", "thioredoxin", "legobody",
    )

    def _select_designable_structure(self, target_complex: str, chosen: str,
                                     operator_pinned: bool) -> str | None:
        """
        Prefer a structure whose dominant interface IS the requested one.

        "Best-evidenced structure" and "best structure to design against" are
        different questions, and the pathway stage only answers the first: it
        takes whichever PDB id the corpus cites, which is normally the landmark
        paper's. For a receptor that is typically the full-length, agonist-bound,
        G-protein-coupled cryo-EM complex — the hardest possible design target,
        carrying a nanobody, a heterotrimeric G protein and a fusion partner,
        with the requested interface buried among them.

        On CALCRL the corpus cites 6E3Y (3.3 A, 7 chains, Gs + Nb35 + CGRP).
        RCSB also holds 3N7S: CALCRL ECD with RAMP1 ECD at 2.1 A and nothing
        else in the box. Same interface, better resolution, no scaffolding.

        Deliberately conservative — this OVERRIDES an evidence-based choice, so
        it only fires when the chosen entry is measurably unfit and a candidate
        is measurably fit. All from metadata; no interfaces are computed here.
        Never fires when the operator pinned `--pdb`.
        """
        if operator_pinned or not chosen or chosen.upper().startswith("AF-"):
            return None
        names = self._split_target_complex_names(target_complex)[:2]
        if len(names) < 2:
            return None
        from src.target_resolve import (
            entry_metadata, find_complex_structures, resolve_target,
        )

        try:
            resolved = [resolve_target(n) for n in names]
            accs = [r.uniprot for r in resolved if r and r.ok and r.uniprot]
            if len(accs) < 2:
                return None
            ids = find_complex_structures(accs[0], rows=40)
            if chosen.upper() not in {i.upper() for i in ids}:
                ids = [chosen] + ids
            meta = entry_metadata(ids[:25])
        except Exception as exc:
            logger.debug(f"structure preference skipped: {exc}")
            return None
        if not meta:
            return None

        def profile(entry: dict) -> dict | None:
            chains = entry.get("chains") or {}
            if not chains:
                return None
            have = {a.upper() for i in chains.values()
                    for a in (i.get("uniprots") or []) if a}
            if not {a.upper() for a in accs} <= have:
                return None                      # requested pair not both present
            target_chains = [i for i in chains.values()
                             if accs[0].upper() in
                             {a.upper() for a in (i.get("uniprots") or [])}]
            chimeric = any(len([a for a in (i.get("uniprots") or []) if a]) > 1
                           for i in target_chains)
            scaffold = sum(
                1 for i in chains.values()
                if any(m in (i.get("description") or "").lower()
                       for m in self._SCAFFOLD_MARKERS))
            return {
                "chimeric": chimeric,
                "scaffold": scaffold,
                "entities": len({(i.get("description") or "") for i in chains.values()}),
                "res": entry.get("resolution_A") or 99.0,
                "target_len": min((i.get("length") or 9999) for i in target_chains),
            }

        profiles = {pid: pr for pid, entry in meta.items()
                    if (pr := profile(entry)) is not None}
        here = profiles.get(chosen.upper())
        if not profiles:
            return None

        def rank(item):
            pid, pr = item
            return (pr["chimeric"], pr["scaffold"], pr["entities"], pr["res"])

        best_id, best = min(profiles.items(), key=rank)
        if best_id == chosen.upper():
            return None

        # Only override on a measurable defect in what was chosen.
        if here is None:
            reason = f"{chosen} does not contain both {names[0]} and {names[1]}"
        elif here["chimeric"] and not best["chimeric"]:
            reason = (f"{chosen}'s {names[0]} chain is a fusion construct and "
                      f"{best_id}'s is not")
        elif here["scaffold"] - best["scaffold"] >= 2 and best["res"] <= here["res"]:
            reason = (f"{chosen} carries {here['scaffold']} scaffolding chain(s) "
                      f"(nanobody / G protein / fusion partner) against "
                      f"{best['scaffold']} in {best_id}, at no worse resolution "
                      f"({best['res']} A vs {here['res']} A)")
        else:
            return None

        self._last_switch_reason = reason
        logger.warning(
            f"  ⚠ switching design structure {chosen} -> {best_id}: {reason}. "
            f"Both contain {names[0]} and {names[1]}; {best_id} is the cleaner "
            f"target for the requested interface. Pin --pdb {chosen} to keep "
            f"the original.")
        self._binder_checkpoint(
            "structure_switched", "pathway", "choice",
            {"from": chosen.upper(), "to": best_id, "reason": reason,
             "profiles": {k: v for k, v in profiles.items()
                          if k in (chosen.upper(), best_id)}})
        return best_id

    def _ppi_interface_options(self, pdb_id: str, target_complex: str) -> str:
        """
        MEASURED interfaces in the chosen entry, plus alternatives, for the prompt.

        The binder track resolves its target to UniProt and ranks real,
        computed interfaces before an LLM sees anything
        (`target_resolve.build_candidate_table`). The PPI track had no
        equivalent: its pathway stage picks whichever PDB id the corpus
        mentions, and the structure stage then infers the chain pair from
        entity descriptions alone. That is how two independent runs on 6E3Y
        both chose the 38-residue CGRP peptide over chain E, RAMP1 — the
        peptide is the most conspicuous thing in an agonist-bound cryo-EM
        structure, and nothing had measured the alternative.

        This gives the same stage measured ground truth: every chain pair in
        the entry that buries a real interface with the target, ranked, with
        BSA and H-bond counts. Advisory only — the skill still chooses, and the
        deterministic guards still check what it chose.

        Bounded cost, deliberately: interfaces are computed for THIS entry
        only, and alternative entries are listed from metadata without
        analysing them. Returns "" on any failure; the stage runs as before.
        """
        names = self._split_target_complex_names(target_complex)
        if not names or not pdb_id or pdb_id.upper().startswith("AF-"):
            return ""
        from src.target_resolve import (
            analyse_entry, entry_metadata, find_complex_structures,
            rank_interfaces, resolve_target,
        )

        try:
            resolved = resolve_target(names[0])
            uniprot = resolved.uniprot if (resolved and resolved.ok) else ""
            if not uniprot:
                return ""
            path = self._binder_structure_path(pdb_id)
            if not path.exists():
                return ""
            meta = (entry_metadata([pdb_id]) or {}).get(pdb_id.upper()) or {}
            cands = rank_interfaces(analyse_entry(path, pdb_id, meta, uniprot))
        except Exception as exc:
            logger.debug(f"interface options unavailable for {pdb_id}: {exc}")
            return ""
        if not cands:
            return ""

        rows = "\n".join(
            f"  {c.target_chain} / {c.partner_chain}  "
            f"BSA {c.bsa_A2:>7.0f} A^2  {c.n_interface_residues:>3} residues  "
            f"{c.n_hbonds:>2} H-bonds  partner: {c.partner_entity[:44]}"
            for c in cands[:6])

        alt = ""
        others_for_manifest: list[dict] = []
        try:
            others = [p for p in find_complex_structures(uniprot, rows=25)
                      if p.upper() != pdb_id.upper()][:6]
            ometa = entry_metadata(others) if others else {}
            lines = []
            for oid in others:
                m = ometa.get(oid.upper()) or {}
                partners = ", ".join(
                    sorted({(i.get("description") or "")[:30]
                            for c, i in (m.get("chains") or {}).items()
                            if uniprot not in (i.get("uniprots") or [])}))[:70]
                lines.append(
                    f"  {oid}  {m.get('method', '?')[:12]:12s} "
                    f"{(str(m.get('resolution_A')) + ' A') if m.get('resolution_A') else '':>8s}  "
                    f"partners: {partners or '(none - unbound)'}")
                others_for_manifest.append(
                    {"pdb_id": oid, "method": m.get("method"),
                     "resolution_A": m.get("resolution_A"), "partners": partners})
            if lines:
                alt = ("\n\nOther deposited structures containing "
                       f"{resolved.gene or names[0]} (NOT analysed — metadata only):\n"
                       + "\n".join(lines))
        except Exception as exc:
            logger.debug(f"alternative-entry listing failed: {exc}")

        logger.info(
            f"  measured {len(cands)} interface(s) in {pdb_id}; best is "
            f"{cands[0].target_chain}/{cands[0].partner_chain} at "
            f"{cands[0].bsa_A2:.0f} A^2")
        # Recorded, not acted on. The structure stage cannot switch entries —
        # by the time it emits a handoff it has already analysed this one — so
        # choosing a better STRUCTURE has to happen before this stage runs.
        # Until it does, put the ranked options where an operator will see them
        # and can re-run with `--pdb <id>`.
        self._binder_checkpoint(
            "structure_alternatives", "structure", "choice",
            {"chosen": pdb_id.upper(),
             "measured_interfaces": [
                 {"target_chain": c.target_chain, "partner_chain": c.partner_chain,
                  "partner": c.partner_entity, "bsa_A2": round(c.bsa_A2, 1),
                  "n_hbonds": c.n_hbonds} for c in cands[:6]],
             "other_entries": others_for_manifest})
        return (
            f"\n\nMEASURED interfaces in {pdb_id.upper()} involving "
            f"{resolved.gene or names[0]}, computed from the coordinates and "
            f"ranked (BSA, H-bonds, hydrophobic fraction, smaller target "
            f"preferred):\n{rows}\n"
            f"\nThese are measurements, not suggestions — use them instead of "
            f"guessing the chain pair from entity descriptions. Pick the pair "
            f"that matches the interface the campaign was chosen for, which is "
            f"not always the largest: an agonist-bound structure buries a lot "
            f"of area against its LIGAND, and designing there targets a "
            f"different biology than a receptor/accessory-protein interface."
            + alt)

    def _screen_select_agents(self, stage: str, texts: list[str], *,
                              pdb_id: str = "") -> list[dict]:
        """Name-screen the campaign against the Federal Select Agent list.

        Warns and records a manifest checkpoint; **never blocks**. See
        `src/select_agents.py` for why this is a select-agent screen rather
        than the "exclude viral targets" filter it might look like it should
        be — briefly: a binder against a viral protein is an antiviral, so
        that filter is inverted relative to the risk, and `--workflow
        structure` takes any local file so an input-side block is bypassed by
        renaming one.

        A hit means "confirm you have institutional approval", which is a true
        and actionable statement. A clean result means only that no listed
        name appeared in the text — never that a target is cleared.
        """
        from src import select_agents

        candidates = list(texts)
        if pdb_id and not pdb_id.upper().startswith(("AF-", "LOCAL-")):
            # The entry title and chain descriptions are where a structure
            # names itself ("Crystal structure of ricin A chain"), and the
            # user's query often does not.
            try:
                from src.target_resolve import entry_metadata

                meta = (entry_metadata([pdb_id]) or {}).get(pdb_id.upper()) or {}
                candidates.append(meta.get("title") or "")
                candidates += [(c.get("description") or "")
                               for c in (meta.get("chains") or {}).values()]
            except Exception as exc:                          # noqa: BLE001
                logger.debug(f"select-agent screen: no metadata for {pdb_id}: {exc}")

        try:
            hits = select_agents.screen(candidates)
        except Exception as exc:                              # noqa: BLE001
            logger.warning(f"select-agent screen failed: {exc}")
            return []
        if not hits:
            return []

        logger.warning("  ⚠ SELECT AGENT SCREEN\n"
                       + select_agents.describe(hits))
        self._binder_checkpoint(
            "select_agent_screen", stage, "gate",
            {"hits": hits,
             "list_source": select_agents.SOURCE_URL,
             "list_reviewed": select_agents.SOURCE_REVIEWED,
             "blocking": False,
             "note": ("Name screening only, and advisory: the run was NOT "
                      "stopped. Confirm institutional approval before "
                      "synthesising anything.")})
        return hits

    def _check_structure_organism(self, pdb_id: str, target_complex: str) -> None:
        """
        Say at TARGET-SELECTION time that the chosen structure is not human.

        The conservation gate (`_check_ortholog_conservation`) is the thing
        that can actually refuse a non-human epitope, and it cannot run until
        hotspots exist — two LLM stages later. On the orphan-GPCR run that cost
        $0.57 and three stages to reach a stop that was foreseeable at stage 0:
        the pathway report itself printed "5GRS (Schizosaccharomyces pombe —
        ortholog)" next to the id it had just chosen.

        This does not refuse anything. An ortholog is often a perfectly good
        template — that is *why* the conservation check measures rather than
        assumes — so this warns, names the human alternatives if RCSB has any,
        and records a checkpoint. Costs one GraphQL call and no LLM tokens.
        """
        if (not pdb_id or pdb_id.upper().startswith("AF-")
                or pdb_id.upper() == "NOT_FOUND"):
            return
        names = self._split_target_complex_names(target_complex)[:2]
        if not names:
            return
        from src.target_resolve import (
            entry_metadata, find_complex_structures, resolve_target,
        )

        try:
            meta = (entry_metadata([pdb_id]) or {}).get(pdb_id.upper()) or {}
        except Exception as exc:
            logger.debug(f"organism pre-check skipped for {pdb_id}: {exc}")
            return
        chains = meta.get("chains") or {}
        if not chains:
            return
        present = {str(a).upper()
                   for info in chains.values()
                   for a in (info.get("uniprots") or []) if a}
        if not present:
            return

        missing: list[tuple[str, str]] = []
        for name in names:
            try:
                resolved = resolve_target(name)
            except Exception:
                continue
            acc = resolved.uniprot if (resolved and resolved.ok) else ""
            if acc and acc.upper() not in present:
                missing.append((name, acc))
        if not missing:
            return

        alternatives: list[str] = []
        try:
            alternatives = [
                pid for pid in find_complex_structures(missing[0][1], rows=25)
                if pid.upper() != pdb_id.upper()
            ][:5]
        except Exception as exc:
            logger.debug(f"human-alternative lookup failed: {exc}")

        detail = ", ".join(f"{n} ({acc})" for n, acc in missing)
        logger.warning(
            f"  ⚠ {pdb_id} carries no human accession for {detail} — this is "
            f"very likely an ortholog structure. The epitope will not be "
            f"checked against the human protein until after the structure "
            f"stage, so a non-conserved site costs two more LLM stages before "
            f"it is caught."
            + (f" Human structures containing {missing[0][0]}: "
               f"{', '.join(alternatives)}." if alternatives else
               " RCSB lists no human co-complex for it."))
        self._binder_checkpoint(
            "non_human_structure", "pathway", "gate",
            {"pdb_id": pdb_id, "target_complex": target_complex,
             "not_present_as_human": [{"name": n, "human_uniprot": a}
                                      for n, a in missing],
             "human_alternatives": alternatives})

    @staticmethod
    def _check_af_model_intent(handoff: dict) -> None:
        """
        An AlphaFold model is a MONOMER, so it cannot supply a PPI interface.

        `_ensure_structure` accepts an `AF-<accession>` pseudo-id and fetches
        the model, which is what makes a structure-less target designable at
        all — the orphan-GPCR run wrote "de novo AlphaFold structural modeling
        is required" and then fell back to a downstream complex, because the
        pathway skill did not know the id was legal. It is legal now, but only
        for the single-chain mode: `disrupt` and `stabilize` both need two
        chains to compute an interface from, and there is only one here.
        """
        pdb_id = str(handoff.get("pdb_id") or "")
        if not pdb_id.upper().startswith("AF-"):
            return
        intent = str(handoff.get("design_intent") or "").strip().lower()
        if intent in ("", "inhibit_active_site"):
            handoff["design_intent"] = "inhibit_active_site"
            return
        raise PipelineBlockedError(
            f"design_intent={intent!r} was chosen with the AlphaFold model "
            f"{pdb_id}, but an AlphaFold model is a single chain — there is no "
            f"partner in it to disrupt or stabilise. Either target it as a "
            f"single-chain pocket (design_intent: inhibit_active_site), or "
            f"pick an experimental co-complex structure.")

    def _verify_partner_chain_is_requested(
        self, requested_complex: str, handoff: dict, pdb_id: str, *,
        source: str,
    ) -> None:
        """
        The partner chain must be a protein the campaign was JUSTIFIED against.

        `_verify_ppi_chain_assignment` cannot catch a substitution here, by
        construction: it reads `target_complex` out of the SAME handoff whose
        chain choice is under test, so an analysing stage that rewrites the
        complex to name whatever it actually looked at ends up validating its
        own substitution. This guard is given the complex the UPSTREAM stages
        settled on and never lets the analysing stage redefine it.

        Caught on a real run. Asked for the CALCRL/RAMP1 heterodimer in 6E3Y —
        where chain E *is* RAMP1 — the structure stage analysed CALCRL against
        chain P, the 38-residue CGRP agonist PEPTIDE, and rewrote
        target_complex to "CALCRL / CGRP". Chain R really is CALCRL, so the
        chain-assignment guard, hotspot grounding and the identity check all
        passed. The substitution is also what put three hotspots inside the
        membrane: the CGRP peptide binds down a class-B vestibule that
        penetrates the TM bundle, which the RAMP1 interface does not.

        Accepts when the partner chain is EITHER requested protein — a
        deliberate target/partner swap is legitimate, the interface skill is
        explicitly told to correct a backwards assignment — or an ORTHOLOG of
        one, which is the ortholog guard's business, not this one. Hard fails
        only when it is neither. Fail-open wherever the evidence is missing,
        like every other verify here.
        """
        names = self._split_target_complex_names(requested_complex)[:2]
        if len(names) < 2:
            return  # single-protein / inhibit_active_site mode has no partner
        partner_chain = str(handoff.get("partner_chain")
                            or handoff.get("chain_b") or "").strip()
        if not partner_chain or not pdb_id:
            return
        structure_path = self._binder_structure_path(pdb_id)
        if not structure_path.exists():
            logger.warning(
                f"  ⚠ partner chain not verified — {structure_path} is not on "
                f"disk")
            return

        from src.ortholog_check import MISMATCH
        from src.target_resolve import fetch_uniprot_sequence, resolve_target

        def classify(chain: str, gene: str, acc: str):
            identity = None
            ref_seq = fetch_uniprot_sequence(acc)
            if ref_seq:
                identity = self._chain_identity_to_uniprot(
                    structure_path, chain, ref_seq)
            return self._classify_target_chain(pdb_id, chain, identity, gene, acc)

        resolved: dict[str, tuple[str, str]] = {}
        for name in names:
            try:
                r = resolve_target(name)
            except Exception as exc:
                logger.debug(f"could not resolve {name!r}: {exc}")
                continue
            if r and r.ok and r.uniprot:
                resolved[name] = (r.gene or name, r.uniprot)
        if not resolved:
            logger.warning(
                f"  ⚠ partner chain not verified — could not resolve either of "
                f"{names} to a UniProt accession")
            return

        # Which requested protein is the TARGET? Everything else is the partner.
        # Without this the loop below compares the partner chain against the
        # TARGET's accession, gets a mismatch, and raises — which is how an
        # antibody partner (`mAb1`, `anti-PD-L1 VHH`, `Nanobody 35`: none of
        # them resolve to a gene) turned a correct chain assignment into a hard
        # failure on a real GCGR run.
        target_chain = str(handoff.get("target_chain")
                           or handoff.get("chain_a") or "").strip()
        # No `len(resolved) > 1` shortcut here: the case that matters most is
        # exactly the one where only ONE name resolved, because then the
        # unresolved one is the partner and there is nothing to check it with.
        target_name = None
        if target_chain:
            for name, (gene, acc) in resolved.items():
                if classify(target_chain, gene, acc).verdict != MISMATCH:
                    target_name = name
                    break

        partner_candidates = {n: v for n, v in resolved.items() if n != target_name}
        if not partner_candidates:
            unresolved = [n for n in names if n not in resolved]
            logger.warning(
                f"  ⚠ partner chain not verified — the requested partner "
                f"{unresolved or names} did not resolve to a UniProt accession "
                f"(antibodies, nanobodies and peptides usually do not). "
                f"Chain {partner_chain} is taken on trust.")
            return

        # A target/partner swap is legitimate, so accept EITHER requested
        # protein; only "neither" is evidence of a substitution.
        verdicts: dict[str, object] = {}
        for name, (gene, acc) in resolved.items():
            verdict = classify(partner_chain, gene, acc)
            verdicts[name] = verdict
            if verdict.verdict != MISMATCH:
                logger.info(
                    f"  partner chain OK — {partner_chain} in {pdb_id} is "
                    f"{name} ({verdict.verdict}): {verdict.reason}")
                return

        detail = "; ".join(f"vs {n}: {v.reason}" for n, v in verdicts.items())
        raise PipelineError(
            f"partner_chain={partner_chain} in {pdb_id} is not one of the "
            f"proteins this campaign was chosen for. {source} settled on "
            f"{requested_complex!r}, but chain {partner_chain} matches neither "
            f"of {names}. {detail}. "
            f"{self._chain_inventory(pdb_id)} "
            f"Designing here would target an interface nobody selected — "
            f"re-run the stage against the intended partner, or change the "
            f"target upstream if the substitution was deliberate.")

    @staticmethod
    def _chain_inventory(pdb_id: str) -> str:
        """One-line 'what is actually in this entry' for a guard message."""
        from src.target_resolve import entry_metadata

        try:
            meta = (entry_metadata([pdb_id]) or {}).get(pdb_id.upper()) or {}
        except Exception:
            return ""
        chains = meta.get("chains") or {}
        if not chains:
            return ""
        listing = ", ".join(
            f"{cid}={(info.get('description') or '?')[:40]}"
            f" ({info.get('length')} aa)"
            for cid, info in sorted(chains.items()))
        return f"Chains in {pdb_id.upper()}: {listing}."

    def _verify_ppi_chain_assignment(self, target_complex: str,
                                     handoff: dict[str, str], pdb_id: str) -> None:
        """
        PPI-track counterpart to `_verify_target_chain_assignment`.

        Binder's target_intel names a single, unambiguous target gene up
        front, so its guard can ask "is target_chain THIS gene". PPI's
        structure stage instead picks target_chain/partner_chain itself out
        of a `target_complex` naming BOTH proteins (e.g. "YAP1 / TEAD1") —
        either named protein is a legitimate target_chain choice, so there is
        no single pre-declared answer to check against. What is still a bug,
        and exactly the PD-L1 failure mode this guards against, is
        target_chain resolving to NEITHER named protein. Resolves each name
        to a gene/UniProt pair and reuses `_verify_target_chain_assignment`'s
        sequence-first/metadata-fallback check verbatim against each
        candidate in turn, accepting the assignment as soon as one candidate
        does not flag it as backwards or unrelated.

        A candidate that comes back silent (inconclusive — no structure
        downloaded yet, no RCSB metadata, gene not found) is treated the same
        as a pass rather than tried further: this is deliberately fail-open,
        matching every other verify check in this pipeline, and it still
        catches the incident this was built for — a well-characterised target
        resolves to a UniProt accession and hits the sequence-identity tier,
        which gives a decisive, non-silent verdict.
        """
        from src.target_resolve import resolve_target

        if not target_complex or not handoff.get("target_chain"):
            return
        names = self._split_target_complex_names(target_complex)
        if not names:
            return

        candidates = []
        for name in names[:2]:
            resolved = resolve_target(name)
            candidates.append({"target_gene": resolved.gene or name,
                               "target_uniprot": resolved.uniprot or ""})

        if len(candidates) == 1:
            self._verify_target_chain_assignment(candidates[0], handoff, pdb_id)
            self._ppi_target_uniprot = candidates[0]["target_uniprot"] or None
            return

        errors = []
        for intel in candidates:
            try:
                self._verify_target_chain_assignment(intel, handoff, pdb_id)
            except PipelineError as exc:
                errors.append(str(exc))
                continue
            # Remember WHICH of the two named proteins the assignment was
            # accepted against, so the numbering-frame advisory has an
            # accession on this track too. The binder track gets it from
            # target_intel; PPI names both halves of a pair and only this
            # loop knows which one chain the target chain turned out to be.
            # An accession that is really the PARTNER's is harmless rather
            # than wrong: `uniprot_to_auth` looks the alignment up on the
            # TARGET chain's own entity and returns {} when that entity has
            # no alignment for it, which fails the advisory open.
            self._ppi_target_uniprot = intel["target_uniprot"] or None
            return
        raise PipelineError(
            f"chain assignment in {pdb_id} matches neither protein named in "
            f"{target_complex!r} (target_chain={handoff.get('target_chain')!r}, "
            f"partner_chain={handoff.get('partner_chain')!r}): " + " | ".join(errors))

    @staticmethod
    def _split_target_complex_names(target_complex: str) -> list[str]:
        """
        "ProteinA / ProteinB" -> ["ProteinA", "ProteinB"]; a single-protein
        inhibit_active_site label like "DPP4 (active site)" -> ["DPP4"].
        """
        names = [re.sub(r"\s*\([^)]*\)\s*$", "", n).strip()
                 for n in re.split(r"\s*/\s*", target_complex or "")]
        return [n for n in names if n]

    #: Handoff fields that are INSTRUCTIONS to a later stage, as opposed to
    #: claims about the evidence. Only these get a switched PDB id substituted
    #: into them. The distinction matters: `go_rationale` saying "high-resolution
    #: cryo-EM structure (PDB 6E3Y)" is a true statement about what the evidence
    #: was, and rewriting it to name the replacement would corrupt the record —
    #: worse, a sentence like "structure 6E3Y (doi:10.1038/s41586-018-0535-y)"
    #: would end up pairing the new id with the old entry's paper. Rewrite what
    #: the pipeline is being told to do; leave what it was told as true.
    _STRUCTURE_INSTRUCTION_FIELDS = ("structure_query", "design_query",
                                     "literature_query")

    def _reapply_recorded_structure_switch(self, result: "PipelineResult",
                                           handoff: dict) -> None:
        """Restore a previous process's structure switch on resume.

        Reads the `structure_switched` checkpoint and, if the handoff parsed off
        disk still names the entry that was rejected, points this run back at
        the one it actually designed against. Fails open: no project, no
        checkpoint, or a value that does not match the recorded stale entry, and
        nothing happens. The caller skips this entirely when the operator pinned
        `--pdb`, which overrides the switch on a resume just as it does on a
        fresh run.
        """
        if self._project is None or self._round_id is None:
            return
        try:
            cp = self._project.checkpoint("structure_switched", self._round_id)
        except Exception as exc:                      # a manifest read is not worth a crash
            logger.warning(f"could not read structure_switched checkpoint: {exc}")
            return
        payload = (cp or {}).get("payload") or {}
        stale, better = payload.get("from"), payload.get("to")
        if not stale or not better:
            return
        if (result.pdb_id or "").upper() != stale.upper():
            return                                    # already correct, or pinned elsewhere

        logger.warning(
            f"  ⚠ resumed handoff names {stale}, which this campaign replaced with "
            f"{better} — restoring {better} from the manifest checkpoint. Pass "
            f"--pdb {stale} if you really want the original.")
        result.pdb_id = better
        handoff["pdb_id"] = better
        self._retarget_stale_structure(handoff, stale, better)
        result.structure_switch = {"from": stale, "to": better,
                                   "reason": payload.get("reason") or ""}

    def _retarget_stale_structure(self, handoff: dict, stale: str,
                                  better: str) -> None:
        """Point a handoff's forward-looking instructions at the chosen entry.

        `_select_designable_structure` can replace the entry the pathway stage
        picked, and the free-text fields it wrote still name the old one. Left
        alone, they flow into the next stage's prompt and it reasons about a
        structure the pipeline is not using.

        Applied to the LITERATURE handoff as well as the pathway one, because
        the literature stage writes its own `design_query` after the switch has
        already happened, from a pathway report that still recommends the old
        entry eleven times over. On the CALCRL/RAMP1 run that produced a
        `01_literature.md` naming 6E3Y four times while the structure stage
        analysed 3N7S — and the retired legacy design stage passed
        `design_query` VERBATIM to the design-script skill, so the designer
        was being handed the wrong entry, not merely a stale narrative. The
        `*_query` fields are still instructions to whatever reads the report
        next, so they still get corrected.
        """
        if not stale or not better or stale.upper() == better.upper():
            return
        pattern = re.compile(re.escape(stale), re.IGNORECASE)
        for field in self._STRUCTURE_INSTRUCTION_FIELDS:
            text = handoff.get(field)
            if isinstance(text, str) and pattern.search(text):
                handoff[field] = pattern.sub(better, text)
                logger.info(f"retargeted {field}: {stale} -> {better}")

    def _note_structure_switch(self, result: "PipelineResult", stale: str,
                               better: str) -> None:
        """
        Write the substitution into the artifact whose choice it overrode.

        The switch happens between stages, so `00_pathway.md` is already on
        disk naming the entry that was replaced — on the first campaign that
        used this, the pathway report said 6E3Y eight times, the structure
        report said 3N7S, and the only account of why sat in a log line and a
        manifest checkpoint. A reader of the run had no way to find it.

        Appended rather than rewritten: what the pathway stage concluded, on
        the evidence it had, is worth keeping intact.
        """
        path = result.stage_files.get("pathway")
        if not path or not Path(path).exists():
            return
        reason = self._last_switch_reason or "a cleaner entry for this interface"
        try:
            with Path(path).open("a", encoding="utf-8") as fh:
                fh.write(
                    f"\n\n---\n\n## STRUCTURE SUBSTITUTION (deterministic, "
                    f"post-stage)\n\n"
                    f"This report recommends **{stale}**. The pipeline designed "
                    f"against **{better}** instead.\n\n"
                    f"- **Why:** {reason}\n"
                    f"- **Decided by:** `_select_designable_structure`, between "
                    f"the pathway and literature stages — structure choice has "
                    f"to be settled before the structure stage analyses "
                    f"anything, because its hotspots then refer to those "
                    f"coordinates.\n"
                    f"- **Both entries contain the requested proteins.** The "
                    f"switch only fires on a measurable defect in the chosen "
                    f"one: partner absent, target chain a fusion construct, or "
                    f"materially more scaffolding at no better resolution.\n"
                    f"- **To keep the original:** re-run with `--pdb {stale}`.\n"
                    f"\nThe reasoning above is the pathway stage's own, on the "
                    f"evidence it had, and is left unedited.\n")
        except OSError as exc:
            logger.warning(f"could not annotate {path} with the switch: {exc}")

    def _order_names_by_chain(self, names: list[str], structure_handoff: dict,
                              pdb_id: str, primary: str, partner: str
                              ) -> tuple[str, str]:
        """
        Which of the two named proteins is on `target_chain`?

        Returns (target_name, partner_name) ordered to match the structure
        stage's chain assignment, or the inputs unchanged when the question
        cannot be answered — the accessions do not resolve, the entry metadata
        is unavailable, or the target chain carries neither accession.
        """
        target_chain = str(structure_handoff.get("target_chain") or "").strip()
        if len(names) < 2 or not target_chain or not pdb_id:
            return primary, partner
        from src.target_resolve import entry_metadata, resolve_target

        try:
            meta = (entry_metadata([pdb_id]) or {}).get(pdb_id.upper()) or {}
            accs = {a.upper() for a in
                    ((meta.get("chains") or {}).get(target_chain) or {})
                    .get("uniprots", []) if a}
            if not accs:
                return primary, partner
            for name in names[:2]:
                r = resolve_target(name)
                if r and r.ok and r.uniprot and r.uniprot.upper() in accs:
                    other = next(n for n in names[:2] if n != name)
                    if name != primary:
                        logger.info(
                            f"  target/partner ordered by chain assignment: "
                            f"chain {target_chain} of {pdb_id} is {name}, not "
                            f"{primary} — swapping so target_gene matches the "
                            f"chain actually being designed against")
                    return name, other
        except Exception as exc:
            logger.debug(f"could not order names by chain: {exc}")
        return primary, partner

    def _bridge_ppi_to_binder_track(
        self, query: str, run_dir: Path, result: PipelineResult, *,
        auto_mode: bool,
    ) -> PipelineResult:
        """
        Hand a PPI-discovered target off to the binder track's stage machine,
        whichever generator is going to build the designs.

        Backend-agnostic on purpose. Nothing below reads `_design_engine`:
        the bridge composes a target_intel handoff and an interface artifact,
        and `_run_binder_track` dispatches spec/pilot/calibration/production/
        scoring to foundry or BoltzGen from there. So a BoltzGen PPI run gets
        the trim, the measured production size, `--stop-after` and the
        resumable manifest for free, rather than needing a second bridge.
        See UNIFY_DESIGN_BACKEND_NOTES.md (foundry, which came first) and
        UNIFY_BOLTZGEN_BACKEND_NOTES.md (BoltzGen).

        PPI's own pathway + literature + structure stages already ran
        unchanged before this is called. `_stage_structure` calls the SAME
        `complex-structure-analysis` skill the binder track's own `interface`
        stage calls, and (since the verify-gap fix alongside this bridge)
        runs the identical `_verify_ppi_chain_assignment` /
        `_verify_hotspot_grounding` guards — so its output is reused directly
        as the binder-track's `interface` artifact rather than paying for a
        second LLM call that would just re-derive the same interface.
        `_run_binder_track` is entered at "trim", one stage past its own
        "interface", for exactly that reason.

        A synthetic "target_intel" artifact is still written (deterministic,
        no LLM call) because every downstream binder stage — trim, spec,
        summary — reads its `intel` dict from the target_intel stage file on
        disk, not from the interface stage; skipping that write would leave
        those stages loading `{}` and falling back to un-derived defaults.
        """
        dirs = self._binder_dirs(run_dir)
        structure_file = result.stage_files.get("structure")
        if structure_file is None or not structure_file.exists():
            raise PipelineError(
                f"design_engine={self._design_engine} needs a completed "
                f"structure stage before it can hand off to the binder "
                f"track — resume from an earlier PPI stage first.")
        if not result.hotspot_residues_json:
            raise PipelineError(
                "the structure stage produced no MODEL-READY HOTSPOTS table; "
                "neither generator can build a spec without atom-level "
                "hotspots — RFD3 needs them as contigs and BoltzGen as its "
                "`binding:` list (this should already have failed inside "
                "_stage_structure — check 02_structure.md).")

        lit = result.literature_handoff or {}
        pathway = result.pathway_handoff or {}
        target_complex = result.target_complex or "the target"
        names = self._split_target_complex_names(target_complex)
        primary_name = names[0] if names else target_complex
        partner_name = names[1] if len(names) > 1 else ""

        from src.target_resolve import resolve_target
        # Order the pair by the CHAIN ASSIGNMENT the structure stage made, not
        # by the order they happen to appear in `target_complex`. The structure
        # stage picks whichever chain carries the epitope, and that is often the
        # second name: on 3N7S it chose chain D (RAMP1) as the target while
        # `target_complex` reads "CALCRL / RAMP1", so target_gene said CALCRL
        # for a campaign designed against RAMP1's ectodomain.
        #
        # More than a label. `_stage_trim` does fetch_topology(target_uniprot)
        # and restriction_for(pdb, target_chain, target_uniprot): with the
        # accession of the OTHER protein, `uniprot_to_auth` finds no alignment,
        # the restriction quietly does not apply, and NO transmembrane stripping
        # happens. Harmless on an ectodomain-only entry, silent on a full-length
        # one.
        primary_name, partner_name = self._order_names_by_chain(
            names, (result.structure_handoff or {}), result.pdb_id or "",
            primary_name, partner_name)
        resolved = resolve_target(primary_name)

        design_intent = (lit.get("design_intent") or pathway.get("design_intent")
                         or "disrupt")

        structure_handoff = result.structure_handoff or {}
        modality = self._resolve_modality(
            structure_handoff.get("modality") or lit.get("modality"),
            source="the PPI structure/literature stages")

        # Through `_binder_length_range` rather than reading `binder_sizes`
        # here: a cyclic-peptide campaign is 12-15 residues and a mini-protein
        # one 70-86, so a second copy of that lookup is a 5.8x error waiting
        # to drift (the mistake commit 5c17881 fixed once already). Same
        # table, same modality-aware fallback every other site uses.
        length_min, length_max = self._binder_length_range({"modality": modality})

        # The chain assignment lives in the INTERFACE handoff (PPI's structure
        # stage chose it). `_binder_sites` reads it from the TARGET-INTEL
        # handoff, so without carrying it across, every path that builds a site
        # list — `--stop-after spec`, `--stop-after trial`, `--trial-sites N` —
        # died with "target-intel did not name usable chains". Found by a real
        # bridged GPU run; `--stop-after spec` is the recommended first command
        # for a new user, so this was the first thing they would have hit.
        intel_handoff = {
            "pdb_id": result.pdb_id or "",
            "target_gene": primary_name,
            "target_uniprot": resolved.uniprot or "",
            "partner_name": partner_name,
            "target_chain": structure_handoff.get("target_chain", ""),
            "partner_chain": structure_handoff.get("partner_chain", ""),
            "design_intent": design_intent,
            "modality": modality,
            "binder_length_min": length_min,
            "binder_length_max": length_max,
            "interface_rationale": (lit.get("go_rationale")
                                    or structure_handoff.get("interface_summary", "")),
            "go_recommendation": result.go_recommendation or "GO",
        }
        target_intel_out = dirs["binder"] / self._BINDER_STAGE_FILES["target_intel"]
        self._write_binder_report(
            target_intel_out, "Target intelligence (bridged from PPI literature)",
            (f"**Structure substituted:** the pathway stage recommended "
             f"{result.structure_switch['from']}; this campaign was designed "
             f"against {result.structure_switch['to']} instead — "
             f"{result.structure_switch['reason']}. Re-run with `--pdb "
             f"{result.structure_switch['from']}` to keep the original.\n\n"
             if result.structure_switch else "") +
            f"Bridged from the PPI track's pathway/literature/structure stages "
            f"for {target_complex} — see 00_pathway.md / 01_literature.md / "
            f"02_structure.md for the full reasoning. This file exists only "
            f"so the binder track's stage machine has a target_intel artifact "
            f"to resume from; no LLM call was made to produce it.",
            intel_handoff)
        result.stage_files["target_intel"] = target_intel_out
        result.stages_completed.append("target_intel")

        interface_out = dirs["binder"] / self._BINDER_STAGE_FILES["interface"]
        interface_out.write_text(
            structure_file.read_text(encoding="utf-8"), encoding="utf-8")
        result.stage_files["interface"] = interface_out
        result.stages_completed.append("interface")

        logger.info(
            f"design_engine={self._design_engine}: handing {target_complex} "
            f"({result.pdb_id}) off to the binder track at the trim stage")
        return self._run_binder_track(
            query, run_dir, result, start_from="trim", context_file=None,
            auto_mode=auto_mode, target=None, attach=not self._detach,
            n_batches=self._n_batches)

    def _note_single_target_guards(self, intel: dict[str, str],
                                   raw: Any = None) -> None:
        """Say which checks a no-partner run turns off. Once, loudly.

        Failing open for a single target is RIGHT — there is no partner to
        verify, no interface area to retain, no BSA to divide by — but going
        quiet about it is not, which is the same posture
        `--workflow structure` already takes for the three guards that need
        `--uniprot`. Three protections are inactive here and none of them
        announces itself:

        - `_verify_target_chain_assignment` returns at its
          `if not target_chain or not partner_chain` line, so the
          wrong-molecule check that caught the PD-L1/7CZD campaign is off.
        - **Second-order, and the one worth knowing:** `self._ortholog` is
          only ever set INSIDE that guard, after the point it returns from —
          so a missing partner also disables ortholog detection and with it
          `_check_ortholog_conservation`. Nothing about that is obvious from
          either function.
        - `min_bsa_retention` cannot gate: `trim_target` hardcodes
          `bsa_retention = 1.0` with no partner, so the epitope-damage floor
          has nothing to measure.

        The exposure guard does NOT go inactive — it falls back from the
        scale-free fraction to `MAX_EXPOSED_HYDROPHOBIC`, which is the branch
        `exposure_verdict` documents for exactly this case. It is, however,
        the branch with no benchmark coverage (`docs/trim-benchmark.md`).
        """
        intent = str(intel.get("design_intent") or "").strip() or "unstated"
        note = "" if not raw or str(raw).strip() == "" else (
            f" (the stage wrote partner_chain={raw!r}, read as 'no partner')")
        logger.warning(
            f"  ⚠ single-target mode: no partner chain, design_intent="
            f"{intent}{note}. Three checks are therefore INACTIVE — "
            f"chain-assignment (the wrong-molecule guard), ortholog "
            f"conservation (it is only armed inside the chain-assignment "
            f"guard), and the BSA-retention floor. The hydrophobic-exposure "
            f"guard stays on but gates on the residue COUNT, since there is "
            f"no interface area to scale by.")

    def _stage_trim(self, intel: dict[str, str], hotspots_json: str,
                    dirs: dict[str, Path],
                    result: PipelineResult) -> dict[str, Any]:
        from src.structure_trim import TrimBudgetError, TrimError, trim_target

        hs = json.loads(hotspots_json)
        # The hotspot table is parsed out of markdown, so a malformed table
        # yields an empty chain id that only fails four stages later, inside
        # gemmi, as "chain '' not found".
        #
        # THIS is the validator that blocks the default path, not
        # `_binder_sites`'s near-identical one: that is reached only via
        # `--trial-sites N` or `--stop-after trial|spec`
        # (`_run_binder_track`'s `if self._trial_sites > 1 or ...`), so a
        # plain single-site run arrives here instead. The two validate
        # different dicts — target-intel's handoff there, the interface
        # stage's hotspot table here — which is why a placeholder had to be
        # rejected in both or it simply moved one stage later.
        #
        # The target chain is mandatory. The PARTNER is not: single-target
        # mode has none, and `trim_target` is already partner-optional
        # throughout (every interface measurement in it is guarded on
        # `if partner_chain`, and the result then carries
        # `interface_bsa_target_side_A2 = 0.0` / `bsa_retention = 1.0`).
        target = chain_id_or_blank(hs.get("target_chain"))
        if target in ("", MALFORMED_CHAIN):
            raise PipelineError(
                f"the interface stage's MODEL-READY HOTSPOTS table gave "
                f"target_chain={hs.get('target_chain')!r}, which is not an "
                f"auth chain id — the table is malformed and the RFD3 spec "
                f"cannot be built")
        partner = chain_id_or_blank(hs.get("partner_chain"))
        if partner == MALFORMED_CHAIN:
            raise PipelineError(
                f"the interface stage's MODEL-READY HOTSPOTS table gave "
                f"partner_chain={hs.get('partner_chain')!r}, which is neither "
                f"an auth chain id nor a recognisable 'no partner' — the "
                f"table is malformed and the RFD3 spec cannot be built")
        hs["target_chain"], hs["partner_chain"] = target, partner
        if not partner:
            # `_binder_sites` makes exactly this check against target-intel's
            # handoff, but it is reached only via `--trial-sites N` or
            # `--stop-after trial|spec`; a plain single-site run is stopped
            # here instead, against the INTERFACE stage's table. The two
            # validate different dicts, so an intent check in one alone just
            # moves the failure a stage later — which is how a `disrupt` or
            # `stabilize` run whose partner went missing degraded silently
            # into single-target mode.
            if not waives_partner_chain(intel):
                raise PipelineError(
                    f"the MODEL-READY HOTSPOTS table names no partner_chain "
                    f"(design_intent={intel.get('design_intent')!r}). A "
                    f"partner is optional only for a single-target intent "
                    f"({', '.join(sorted(_SINGLE_TARGET_INTENTS))}); an "
                    f"interface this run intends to "
                    f"{intel.get('design_intent') or 'act on'} needs both "
                    f"sides named. Re-run the interface stage, or state the "
                    f"intent the run actually has.")
            self._note_single_target_guards(
                intel, raw=hs.get("partner_chain"))
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

        # Membrane topology. Two separate jobs, and only one of them is a rule.
        #
        # Always drop TRANSMEMBRANE residues: an exposed TM helix is a
        # hydrophobic slab that preferentially attracts binders which cannot
        # work in a cell, where that surface is buried in lipid. That part is
        # physics and stays unconditional.
        #
        # Which SIDE to design against is not a rule. This used to default to
        # "extracellular" and hard-fail any hotspot outside it, which is wrong
        # in two ways at once: for an intracellular-organelle membrane protein
        # (SCAP in the ER) "extracellular" is not even a meaningful side, and
        # the cytosolic face is the correct thing to target; and the interface
        # stage has already looked at the real structure and picked a real
        # interface, so its choice is evidence, not a proposal to be overruled
        # by a default. `membrane_side` now defaults to "auto": the side is
        # INFERRED from where the declared hotspots actually sit, and an
        # explicit value is still honoured. Only two things still fail: a
        # hotspot inside the membrane, and hotspots split across both faces —
        # neither is a site a binder can engage as one epitope.
        restrict = None
        side = (intel.get("membrane_side") or "auto").strip().lower()
        uniprot = intel.get("target_uniprot")
        if uniprot and side not in ("not_applicable", "any"):
            from src.membrane_topology import fetch_topology, restriction_for

            topo = fetch_topology(uniprot)
            if side == "auto":
                side = self._infer_membrane_side(
                    result.pdb_id, hs["target_chain"], uniprot, hs["residues"],
                    topo)
            if side is None:
                restrict = None
            else:
                restrict = restriction_for(result.pdb_id, hs["target_chain"],
                                           uniprot, side=side, topology=topo)
                logger.info(f"topology: {restrict.note}")
                if restrict.applies:
                    bad = [h for h in hs["residues"]
                           if int(h["auth_seq_id"]) not in restrict.allowed_auth]
                    if bad:
                        # Reaching here means the side was pinned explicitly and
                        # the hotspots disagree with it — inference would have
                        # followed them. Say which, so the operator can drop the
                        # override rather than guess.
                        raise PipelineError(
                            f"hotspot(s) "
                            f"{[h.get('auth_seq_id') for h in bad]} lie outside "
                            f"the {side} region of {intel.get('target_gene')}, "
                            f"which was requested explicitly via membrane_side. "
                            f"Set membrane_side=auto to design against the side "
                            f"the hotspots are actually on, or any to disable "
                            f"the topology restriction entirely.")

        binder_lo, binder_hi = self._binder_length_range(intel)

        def _trim(with_budget: int):
            return trim_target(
                structure,
                target_chain=hs["target_chain"],
                partner_chain=hs.get("partner_chain"),
                hotspots=hs["residues"],
                allowed_auth=self._combine_allowed(
                    restrict,
                    self._target_accession_residues(
                        result.pdb_id, hs["target_chain"], uniprot or "")),
                budget=with_budget,
                out_dir=dirs["trim"],
                pdb_id=result.pdb_id,
                binder_min=binder_lo,
                binder_max=binder_hi,
                chainsaw_cmd=trim_cfg.get("chainsaw_cmd"),
                min_bsa_retention=float(trim_cfg.get("min_bsa_retention", 0.90)),
            )

        try:
            res = _trim(budget)
        except (TrimError, TrimBudgetError) as exc:
            # A trim that fails a QUALITY guard is telling us this particular
            # cut is bad, not that the target is undesignable — and its own
            # error already names the alternative ("design against the
            # untrimmed target"). Nothing used to take it.
            #
            # It matters most exactly where the trim is least worth doing. A
            # 227-residue TEAD4 against a 220 budget has to shed SEVEN
            # residues, and the cut that does it opened hydrophobic core
            # inside 10 A of the epitope — so a campaign died over a 3%
            # overshoot. Keeping the target whole is strictly safer for the
            # design (no fresh hydrophobic face at all); it only costs GPU
            # time, and RF3 scales as (tokens/195)**1.62, so 15% more target
            # is ~18% more refold time on a stage the calibration gate sizes
            # from measurement anyway.
            #
            # Only for a marginal overshoot, and only once: a target far over
            # budget genuinely has to be cut, and re-raising there keeps the
            # operator's decision in front of them.
            overshoot = float((cfg.get("foundry") or {}).get(
                "target_budget_overshoot", 0.15))
            n_target = self._target_chain_residue_count(
                structure, hs["target_chain"])
            # round(), not int(): 220 * 1.15 is 252.99999... in binary, so a
            # truncating ceiling quietly excludes the residue count the
            # allowance was written to include.
            ceiling = round(budget * (1.0 + overshoot))
            if n_target and budget < n_target <= ceiling:
                logger.warning(
                    f"  ⚠ the trim failed its own quality guard ({exc}). The "
                    f"target is {n_target} residues against a {budget} budget "
                    f"— only {n_target - budget} over, within the "
                    f"{overshoot:.0%} overshoot allowance — so it is kept "
                    f"WHOLE instead. This costs GPU time, not design quality.")
                try:
                    res = _trim(n_target)
                except (TrimError, TrimBudgetError) as exc2:
                    raise PipelineError(
                        f"target trimming failed: {exc}; keeping the "
                        f"{n_target}-residue target whole failed too: "
                        f"{exc2}") from exc2
                res.warnings.append(
                    f"the trim was skipped: its cut failed a quality guard "
                    f"({exc}), and at {n_target} residues the target is only "
                    f"{n_target - budget} over the {budget} budget, so it is "
                    f"used whole")
                self._binder_checkpoint(
                    "trim_skipped_overshoot", "trim", "gate",
                    {"n_target": n_target, "budget": budget,
                     "overshoot_allowance": overshoot, "reason": str(exc)})
            else:
                raise PipelineError(f"target trimming failed: {exc}") from exc

        if res.bsa_retention < 0.95 or res.warnings:
            self._binder_checkpoint(
                "trim_gate", "trim", "gate",
                {"bsa_retention": res.bsa_retention,
                 "kept_segments": [list(s) for s in res.kept_segments],
                 "warnings": res.warnings})

        out = dirs["binder"] / self._BINDER_STAGE_FILES["trim"]
        body = "\n".join([
            f"- Method: **{res.method}**",
            *( [f"- Topology: {restrict.note}"] if restrict else [] ),
            f"- Residues: {res.n_residues_before} -> {res.n_residues_after} "
            f"in {res.n_segments} segment(s) {res.kept_segments}",
            f"- Interface area of the kept residues retained: {res.bsa_retention:.1%}",
            # Stated on every trim, including the many that expose nothing.
            # The AREA is the physically meaningful number and it used to
            # appear only inside a warning string, so a clean trim recorded
            # no exposure measurement at all and a refused one recorded none
            # either — leaving the gate's own input absent from the record of
            # every run it judged.
            "- Newly exposed hydrophobic surface away from the epitope: "
            + self._exposure_note(res),
            f"- Hotspots kept: {len(res.hotspots_retained)}/"
            f"{len(res.hotspots_retained) + len(res.hotspots_lost)}",
            "",
            *(f"- warning: {w}" for w in res.warnings),
            *self._modified_residue_notes(res),
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

    @staticmethod
    def _exposure_note(res) -> str:
        """One line of the trim report: what the cut opened, as an area.

        A count of residues is what the guard used to report and is not
        scale-free; the fraction of the epitope's own interface area is what
        now gates (`structure_trim.MAX_EXPOSED_HYDROPHOBIC_FRACTION`), and the
        absolute area is what a reader can compare against a structure. State
        all three, and say plainly when there is no denominator — that is also
        the case where the residue COUNT is still the gate, so a reader who
        sees no percentage should know why.
        """
        from src.structure_trim import (MAX_EXPOSED_HYDROPHOBIC,
                                        MAX_EXPOSED_HYDROPHOBIC_FRACTION)

        n = getattr(res, "n_exposed_hydrophobic", 0) or 0
        area = getattr(res, "exposed_hydrophobic_A2", 0.0) or 0.0
        frac = getattr(res, "exposed_hydrophobic_fraction", None)
        if not n:
            return ("none — the cut opened no hydrophobic surface"
                    if frac is not None else
                    "none (no partner chain, so no epitope area to scale by)")
        head = f"{area:.0f} A^2 across {n} residue(s)"
        if frac is None:
            return (f"{head}; no partner chain, so this is gated on the "
                    f"residue count ({MAX_EXPOSED_HYDROPHOBIC} tolerated) "
                    f"rather than as a fraction of the epitope")
        return (f"{head}, {frac:.1%} of the target-side interface area "
                f"(gate: {MAX_EXPOSED_HYDROPHOBIC_FRACTION:.0%})")

    @staticmethod
    def _modified_residue_notes(res) -> list[str]:
        """A prominent note for every modified residue the trim CONVERTED.

        The conversion is necessary — RFD3 builds its polymer from ATOM
        records and cannot parse a component it does not know, which aborted a
        real campaign ten times on 3KYS's `Residue A344 not found in atom
        array` — but it is also a change to the chemistry the target is being
        designed against, and it was previously invisible. The 3KYS trim
        report said nothing at all about A344 while silently replacing
        S-palmitoyl-cysteine with plain cysteine and dropping a 16-carbon
        tail, and that lipid is what the entire TEAD-inhibitor literature is
        about.

        So this is deliberately a HEADED section rather than another
        `- warning:` bullet: an operator reading the report has to see that a
        post-translational modification was present, what it was, and that
        the design will not account for it. The pipeline cannot judge whether
        the modification matters — sometimes it is a crystallography artifact
        (selenomethionine), sometimes it is the mechanism (palmitoylation,
        an acetyl-lysine an epigenetic reader binds) — so it says what
        happened and asks the reader to make that call.
        """
        mods = list(getattr(res, "modified_residues", None) or [])
        if not mods:
            return []
        out = ["", "## ⚠ Modified residues converted for the generator", ""]
        for m in mods:
            where = f"{m.get('chain', '?')}{m.get('auth_seq_id', '?')}"
            dep = m.get("deposited", "?")
            desc = m.get("description") or ""
            atoms = m.get("atoms_dropped") or []
            out.append(
                f"- **Residue {where} was modified in the target structure: "
                f"{dep}" + (f" ({desc})" if desc else "")
                + f"**. It has been converted to {m.get('parent', '?')} so the "
                f"generator's parser can read it"
                + (f", dropping {len(atoms)} atom(s) ({', '.join(atoms[:8])}"
                   + ("…" if len(atoms) > 8 else "") + ")" if atoms else "")
                + ". **The designed binder therefore does not account for "
                  "this modification — worth checking the biology behind it.** "
                  "A modification at or near the epitope may be the point of "
                  "the target rather than an artifact.")
        out.append("")
        return out

    def _stage_binder_spec(self, intel: dict[str, str], hotspots_json: str,
                           trim, dirs: dict[str, Path],
                           result: PipelineResult) -> Path:
        from src.foundry_spec import build_rfd3_spec

        hs = json.loads(hotspots_json)
        kept = {a for lo, hi in trim.kept_segments for a in range(lo, hi + 1)}
        hotspots = [h for h in hs["residues"] if int(h["auth_seq_id"]) in kept]
        # Slugified, because the structure-first and local-file tracks compose
        # a synthetic `target_gene` like "3KYS chain A" or "my_own_structure
        # chain A" — which produced `spec/3kys chain a_binder_001.json`, a
        # filename with SPACES in it. That file is the one artifact an operator
        # carries to a GPU box, `23_binder_spec.md` records its path unquoted,
        # and the binder track's compute model is a generated bash driver.
        raw = (intel.get("target_gene") or "target").lower()
        name = re.sub(r"[^a-z0-9]+", "_", raw).strip("_") or "target"
        name = f"{name}_binder_001"
        # RFD3 reads the target from a PDB; the trim writes both formats.
        pdb_input = Path(str(trim.trimmed_path)).with_suffix(".pdb")
        spec_lo, spec_hi = self._binder_length_range(intel)
        spec = build_rfd3_spec(
            name=name,
            structure_path=pdb_input if pdb_input.exists() else trim.trimmed_path,
            contig=trim.contig, hotspots=hotspots,
            target_chain=hs["target_chain"],
            out_path=dirs["spec"] / f"{name}.json",
            binder_min=spec_lo,
            binder_max=spec_hi,
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
        slug = self._cluster_slug(dirs)

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
            proj = self._project.slug if self._project else "<slug>"
            resume_cmd = (
                # resume_cluster_calibration.py's --site is a directory name
                # under binder/sites/, so it only applies to a site trial.
                f"python scripts/resume_cluster_calibration.py "
                f"--project {proj} --site {dirs['binder'].parent.name} "
                f"--n-batches {plan.n_batches} --n-gpus {plan.n_gpus}"
                if mode == "calibration" and self._is_site_dirs(dirs) else
                f"python scripts/run_pipeline.py --project {proj} "
                f"--workflow binder --start-from {mode} --compute cluster"
                if mode == "calibration" else
                # No standalone resume script for other modes yet — this is
                # the one the current workflow needs; ask if production ever
                # needs the same treatment.
                f"python scripts/run_pipeline.py --project {proj} "
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

    # ------------------------------------------------------------------
    # BoltzGen as a binder-track backend
    # ------------------------------------------------------------------

    @property
    def _boltzgen_backend(self) -> bool:
        """Whether the binder-track GPU stages should run BoltzGen.

        The binder track was foundry-only by construction (`config.yaml` said
        so outright), so this is the seam that did not exist. It is read once
        per stage rather than threaded through, and foundry remains the branch
        that does not change.

        False for `boltzgen_legacy`, which is correct and is the point: that
        engine never reaches these stages at all.
        """
        return self._design_engine == "boltzgen"

    @property
    def _bridges_to_binder_track(self) -> bool:
        """Whether a PPI run hands its target to the binder-track machine.

        True for both generators that share that machine, so the three places
        that ask — the resume dispatch, the hand-off after go/no-go, and the
        structure stage's designable-size hint — cannot drift apart or answer
        the question for only one backend, which is how `--design-engine
        boltzgen` spent its first release on the legacy path.
        """
        return self._design_engine in _BRIDGED_ENGINES

    def _refuse_undispatched_site_trials(self) -> None:
        """Refuse the ONE binder-track path that is not backend-dispatched.

        `_run_site_trials` calls `_stage_binder_spec` and `_stage_calibration`
        unconditionally — the FOUNDRY stages. On a BoltzGen run it would build
        an RFD3 contig JSON where a BoltzGen YAML was asked for, then wait for
        RF3 output that never arrives: a silent backend swap, not an error.
        `--stop-after spec` is also the documented first command for a new
        user, so it is the one they would hit first.

        `--stop-after calibration` and an unset `--stop-after` take the plain
        single-site route, which IS dispatched — and calibration is where a
        campaign is sized, so the refusal has somewhere useful to point.
        """
        if not self._boltzgen_backend:
            return
        if self._trial_sites > 1:
            what = "--trial-sites > 1"
        elif self._stop_after in ("trial", "spec"):
            what = f"--stop-after {self._stop_after}"
        else:
            # Self-contained rather than trusting the caller's `if`: the
            # single-site route IS dispatched, and refusing it would make the
            # backend unusable.
            return
        raise PipelineBlockedError(
            f"--design-engine boltzgen does not support {what} yet: the "
            f"multi-site trial path still builds foundry specs and would "
            f"silently run RFD3 instead of BoltzGen. Use --stop-after "
            f"calibration (which is dispatched, and is also where the "
            f"campaign is SIZED), or --design-engine foundry.")

    def _boltzgen_stage_sizes(self, mode: str) -> tuple[int, int]:
        """`(num_designs, budget)` for one BoltzGen stage, from config."""
        block = ((self._binder_cfg().get("boltzgen") or {}).get(mode)) or {}
        defaults = {"pilot": (24, 8), "calibration": (1000, 100),
                    "production": (10000, 200)}[mode]
        return (int(block.get("num_designs", defaults[0])),
                int(block.get("budget", defaults[1])))

    def _boltzgen_tokens(self, trim, intel: dict | None = None) -> int | None:
        """Complex size in residues — target kept + binder midpoint.

        Both cost laws key on this, and taking the target from the TRIM rather
        than the deposited chain matters: `res_index` is what BoltzGen actually
        builds against, so a trimmed target is genuinely smaller and cheaper.

        `_binder_midpoint`'s own fallback is 78, a mini-protein midpoint, which
        would over-state a macrocycle complex by ~65 residues if the contig
        were ever malformed — and the cost laws are superlinear in this number.
        So the fallback is taken from the resolved binder window instead.
        """
        try:
            target = int(getattr(trim, "n_residues_after", 0) or 0)
        except (TypeError, ValueError):
            target = 0
        if not target:
            return None
        lo, hi = self._binder_length_range(intel or {})
        binder = _binder_midpoint(getattr(trim, "contig", "") or "",
                                  default=(lo + hi) // 2)
        return target + int(binder or 0)

    def _stage_boltzgen_spec(self, intel: dict[str, str], hotspots_json: str,
                             trim, dirs: dict[str, Path],
                             result: PipelineResult) -> Path:
        """Write the BoltzGen design YAML. The `binder_spec` stage's other half.

        Two deliberate differences from `_stage_binder_spec`:

        * It points at the DEPOSITED structure, not `trim.trimmed_path`. Gemmi's
          `make_mmcif_document()` writes no `_entity_poly_seq` loop and
          BoltzGen's parser requires one, so our trimmed CIF is unparseable to
          it; the `.pdb` twin parses but puts `binding:` in a third numbering.
          The trim travels as `res_index` instead — see `src/boltzgen_spec.py`.
        * The modality reaches it through `_resolve_modality`, because it
          decides both the binder length window and the protocol, and the
          operator's `--modality` is what settles that.
        """
        from src.boltzgen_spec import SpecError, build_boltzgen_spec

        hs = json.loads(hotspots_json)
        target_chain = str(hs.get("target_chain") or "").strip()
        residues = hs.get("residues") or []
        if not target_chain or not residues:
            raise PipelineError(
                "the interface stage's MODEL-READY HOTSPOTS table gave no "
                "target chain or no residues; a BoltzGen spec cannot be built")

        modality = self._resolve_modality(intel.get("modality"),
                                          source="the target-intel stage")
        structure = self._binder_structure_path(result.pdb_id)
        raw = (intel.get("target_gene") or result.pdb_id or "target").lower()
        slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_") or "target"
        out = dirs["spec"] / f"{slug}_boltzgen.yaml"

        kept = list(getattr(trim, "kept_segments", None) or [])
        try:
            spec = build_boltzgen_spec(
                name=slug, structure_path=structure, target_chain=target_chain,
                hotspots=residues, out_path=out, modality=modality,
                kept_segments=kept or None)
        except SpecError as exc:
            # A spec that cannot be built is a hard stop: every SpecError here
            # corresponds to a failure that is SILENT downstream (a binding
            # index outside the trim is ignored with no error, an equal-length
            # binder aborts the run two steps later naming neither cause).
            raise PipelineError(f"BoltzGen spec refused: {exc}") from exc

        body = [
            "# BoltzGen design specification", "",
            f"- target: `{structure.name}` chain `{spec.target_chain}`",
            f"- modality: `{spec.modality}`  protocol: `{spec.protocol}`",
            f"- binder: chain `{spec.binder_chain}`, "
            f"{spec.binder_min}-{spec.binder_max} residues",
            f"- hotspots ({len(spec.binding)}), as deposited label_seq: "
            f"`{','.join(str(b) for b in spec.binding)}`",
        ]
        if spec.res_index:
            spans = ", ".join(f"{lo}-{hi}" for lo, hi in spec.res_index)
            body.append(f"- target restricted to label_seq `{spans}` "
                        f"(the trim, expressed as res_index)")
        else:
            body.append("- target: whole chain (the trim kept everything)")
        for w in spec.warnings:
            body.append(f"- **warning**: {w}")
        body += ["", "### PIPELINE HANDOFF", "",
                 f"- spec_path: {spec.path}",
                 f"- design_name: {spec.name}",
                 f"- modality: {spec.modality}",
                 f"- protocol: {spec.protocol}",
                 f"- binder_chain: {spec.binder_chain}"]
        report = dirs["binder"] / self._BINDER_STAGE_FILES["binder_spec"]
        report.write_text("\n".join(body) + "\n", encoding="utf-8")
        result.stage_files["binder_spec"] = report
        result.stages_completed.append("binder_spec")
        self._record_stage("binder_spec", "complete", artifacts=[spec.path])
        return spec.path

    def _run_boltzgen_stage(self, mode: str, spec_path: Path, trim,
                            dirs: dict[str, Path], result: PipelineResult, *,
                            attach: bool,
                            num_designs: int | None = None) -> dict:
        """Launch (or re-attach to) one BoltzGen stage, optionally waiting.

        Detached for the same reason the foundry stages are: a production
        campaign runs for hours, and a blocking call inside a Celery task would
        hit the visibility timeout and be redelivered — two BoltzGens racing one
        GPU. `src/boltzgen_runner.py` supplies the plumbing; this method is the
        stage's bookkeeping.
        """
        from src.boltzgen_runner import (
            BoltzGenPaths, build_argv, plan_campaign, progress, resume,
            sec_per_design_observed,
        )
        from src.boltzgen_spec import PROTOCOL_BY_MODALITY
        from src.env_config import resolve_env_path
        from src.job_registry import JobRegistry

        cfg = self._binder_cfg()
        ws = cfg.get("workstation") or {}
        paths = BoltzGenPaths.under(dirs["campaign"], mode)
        paths.mkdirs()

        default_n, budget = self._boltzgen_stage_sizes(mode)
        n_designs = int(num_designs or default_n)
        modality = self._resolve_modality(None, source="the campaign")
        protocol = PROTOCOL_BY_MODALITY[modality]

        # Prefer a rate this campaign's EARLIER stages actually achieved over
        # the size law — the same precedence foundry uses, and for the same
        # reason: the law has real scatter (+39% on one of three fitted points)
        # while an observed rate tracked production closely.
        measured = 0.0
        for prior in ("pilot", "calibration", "production"):
            if prior == mode:
                break
            r = sec_per_design_observed(BoltzGenPaths.under(dirs["campaign"], prior))
            if r > 0:
                measured = r
        plan = plan_campaign(
            paths, num_designs=n_designs, budget=budget,
            n_tokens=self._boltzgen_tokens(trim), protocol=protocol,
            sec_per_design=measured or None,
            disk_budget_gb=float(cfg.get("foundry", {}).get("disk_budget_gb", 120.0)),
        )
        for w in plan.warnings:
            logger.warning(f"  {w}")
        paths.plan_path.write_text(json.dumps(plan.as_dict(), indent=2),
                                   encoding="utf-8")
        logger.info(
            f"BoltzGen {mode}: {plan.num_designs:,} designs ({protocol}), "
            f"{plan.est_gpu_hours:.1f} GPU-h / {plan.est_disk_gb:.1f} GB "
            f"via {plan.rate_source}")

        exe = (resolve_env_path("LPT_BOLTZGEN_EXECUTABLE",
                                ws.get("boltzgen_executable")) or "boltzgen")
        argv = build_argv(exe, spec_path, paths, protocol=protocol,
                          num_designs=plan.num_designs, budget=plan.budget)
        rec = resume(paths, argv, cuda_device=ws.get("cuda_device", 0),
                     note=f"{mode}: {plan.num_designs} designs")

        self._binder_checkpoint(f"{mode}_running", mode, "job", {
            "backend": "boltzgen", "job_id": rec.job_id, "pid": rec.pid,
            "campaign_dir": str(paths.campaign_dir),
            "num_designs": plan.num_designs,
            "est_gpu_hours": plan.est_gpu_hours,
            "resume_stage": mode,
        })

        report = dirs["binder"] / self._BINDER_STAGE_FILES[mode]

        if not attach:
            prog = progress(paths, plan.num_designs)
            report.write_text(
                f"# BoltzGen {mode} (detached)\n\n"
                f"- {plan.num_designs:,} designs, {protocol}\n"
                f"- estimate: {plan.est_gpu_hours:.1f} GPU-h, "
                f"{plan.est_disk_gb:.1f} GB ({plan.rate_source})\n"
                f"- progress: {prog.render()}\n"
                f"- log: `{paths.log_path}`\n",
                encoding="utf-8")
            result.stage_files[mode] = report
            self._record_stage(mode, "awaiting_user")
            raise PipelinePausedError(f"{mode}_running", {
                "backend": "boltzgen", "campaign_dir": str(paths.campaign_dir),
                "num_designs": plan.num_designs,
                "est_gpu_hours": plan.est_gpu_hours,
                "resume": f"--start-from {mode}",
            })

        # Attached: poll DISK, never process state. A BoltzGen campaign spends
        # real time off-GPU in its CPU-bound `analysis` step, so neither
        # nvidia-smi nor "is the pid alive" distinguishes working from stuck.
        poll = float(cfg.get("foundry", {}).get("poll_interval_s", 60))
        reg = JobRegistry(paths.registry_path)
        reg.poll_until(rec.job_id,
                       until=lambda: progress(paths, plan.num_designs).complete,
                       tick_s=poll,
                       on_tick=lambda _rec: logger.info(
                           f"  {progress(paths, plan.num_designs).render()}"))
        final = progress(paths, plan.num_designs)
        if final.n_metrics == 0:
            raise PipelineError(
                f"BoltzGen {mode} produced no scored designs. "
                f"{final.render()}. The log is at {paths.log_path}; a crash in "
                f"the folding step is usually a corrupt design (see "
                f"boltzgen_spec.safe_binder_range).")
        report.write_text(
            f"# BoltzGen {mode}\n\n"
            f"- {protocol}, {plan.num_designs:,} designs requested\n"
            f"- {final.render()}\n"
            f"- estimate was {plan.est_gpu_hours:.1f} GPU-h "
            f"({plan.rate_source}); observed "
            f"{sec_per_design_observed(paths):.2f} s/design\n"
            f"- metrics: `{paths.metrics_csv}`\n",
            encoding="utf-8")
        result.stage_files[mode] = report
        result.stages_completed.append(mode)
        self._record_stage(mode, "complete", artifacts=[paths.metrics_csv])
        return {"paths": paths, "plan": plan, "progress": final}

    def _boltzgen_records(self, paths) -> list[dict]:
        """BoltzGen's metrics table, shaped for `calibrate`.

        `iptm` is aliased from `design_to_target_iptm` because that is the
        column `SUCCESS_METRICS` names, and `design_family` is the design's own
        id: BoltzGen is single-stage, so each design is its own family and
        k_backbone == k_refold. The native columns are kept alongside, because
        the injected gate reads them.
        """
        from src.design_metrics import parse_boltzgen_outputs

        out = []
        for rec in parse_boltzgen_outputs(paths.campaign_dir):
            name = str(rec.get("design_id") or rec.get("id") or "")
            out.append({**rec, "name": name, "design_family": name,
                        "error": "", "iptm": rec.get("design_to_target_iptm")})
        return out

    def _stage_boltzgen_calibration(self, spec_path: Path, trim,
                                    dirs: dict[str, Path],
                                    result: PipelineResult, *, attach: bool,
                                    num_designs: int | None = None) -> dict:
        """Measure the hit rate, then size production from it.

        Reuses `campaign_calibration` wholesale — the Wilson interval, the bar
        ladder, `MIN_HITS_FOR_ESTIMATE`, the SCALE_UP/ITERATE/STOP tree — with
        BoltzGen's own gate and cost model injected. What "excellent" means
        comes from `design.boltzgen_ranking`, per modality, because one scalar
        demonstrably cannot serve both.
        """
        from src.boltzgen_runner import (
            design_bytes, sec_per_design_observed, seconds_per_design,
        )
        from src.boltzgen_spec import PROTOCOL_BY_MODALITY
        from src.campaign_calibration import CostModel, calibrate, render_report
        from src.cluster_runner import ClusterConfig
        from src.design_ranking import (
            gate_boltzgen_records, resolve_boltzgen_ranking,
        )

        run = self._run_boltzgen_stage("calibration", spec_path, trim, dirs,
                                       result, attach=attach,
                                       num_designs=num_designs)
        paths = run["paths"]
        records = self._boltzgen_records(paths)
        modality = self._resolve_modality(None, source="the campaign")
        rank = resolve_boltzgen_ranking(self.config, modality)
        n_tokens = self._boltzgen_tokens(trim)
        rate = (sec_per_design_observed(paths)
                or seconds_per_design(n_tokens, PROTOCOL_BY_MODALITY[modality]))
        cfg = self._binder_cfg()

        res = calibrate(
            records, thresholds=rank.thresholds,
            success_metric=rank.success_metric,
            excellence_bar=rank.excellence_bar,
            target_designs=rank.target_designs,
            gate=gate_boltzgen_records,
            cost=CostModel.boltzgen(sec_per_design=rate,
                                    bytes_per_design=design_bytes(n_tokens)),
            disk_budget_gb=float((cfg.get("foundry") or {}).get("disk_budget_gb", 120.0)),
            max_campaign_days=float((cfg.get("foundry") or {}).get("max_campaign_days", 5.0)),
            adaptive_bar=bool((cfg.get("binder_ranking") or {})
                              .get("adaptive_bar", True)),
            # Only used to cost the "run it in parallel" option an ITERATE
            # verdict offers; the local-vs-cluster DECISION is still
            # `choose_compute`'s, and is made only for a scale-up.
            n_gpus_cluster=ClusterConfig.from_cfg(self.config).n_gpus,
        )

        # Production is sized in DESIGNS and clamped to the configured ceiling.
        ceiling, _ = self._boltzgen_stage_sizes("production")
        needed = int(res.pessimistic.required_refolds or 0)
        n_production = max(1, min(needed, ceiling)) if needed else ceiling
        if needed > ceiling:
            logger.warning(
                f"  calibration asks for {needed:,} designs; "
                f"design.boltzgen.production.num_designs caps it at "
                f"{ceiling:,}. Expect proportionally fewer excellent designs.")

        report = dirs["binder"] / self._BINDER_STAGE_FILES["calibration"]
        report.write_text(
            f"# BoltzGen calibration\n\n"
            f"- modality: `{modality}`  bar: {rank.success_metric} > "
            f"{rank.excellence_bar:g}  target: {rank.target_designs}\n"
            f"- rate used: {rate:.2f} s/design\n"
            f"- production sized at {n_production:,} designs "
            f"(ceiling {ceiling:,})\n\n"
            + render_report(res), encoding="utf-8")
        result.stage_files["calibration"] = report
        result.stages_completed.append("calibration")

        (dirs["calibration"]).mkdir(parents=True, exist_ok=True)
        (dirs["calibration"] / "calibration.json").write_text(
            json.dumps({**res.as_dict(), "backend": "boltzgen",
                        "n_production": n_production,
                        "sec_per_design": rate, "modality": modality},
                       indent=2, default=str), encoding="utf-8")
        self._binder_checkpoint("calibration_verdict", "calibration", "gate", {
            "backend": "boltzgen", "verdict": res.verdict,
            "reason": res.verdict_reason,
            "est_gpu_hours": res.pessimistic.est_gpu_hours,
            "est_disk_gb": res.pessimistic.est_disk_gb,
            "n_production": n_production,
        })
        self._record_stage("calibration", "complete", artifacts=[report])
        return {"result": res, "n_designs": n_production, "compute": "local",
                "backend": "boltzgen"}

    def _resolve_boltzgen_production(self, calib: dict | None,
                                     dirs: dict[str, Path]) -> int:
        """How many designs production should run, surviving a restart.

        The same hazard `_resolve_production_plan` exists for, and it bit the
        foundry track once: the calibration's recommendation only lived in the
        process that measured it, so resuming `--start-from production` in a
        fresh process — the NORMAL case for a multi-hour campaign — silently
        fell back to the config default and ran at the wrong scale. So the
        number is re-read from `calibration.json`.

        A verdict of ITERATE or STOP never reaches here: `_run_binder_track`
        returns at the gate. Refusing rather than guessing is still right if it
        somehow does, because scaling up a campaign the measurement said not to
        scale is the one mistake this stage can make that costs GPU-days.
        """
        ceiling, _ = self._boltzgen_stage_sizes("production")
        if calib and calib.get("n_designs"):
            return int(calib["n_designs"])

        path = dirs["calibration"] / "calibration.json"
        if not path.is_file():
            logger.warning(
                f"  no calibration.json in {dirs['calibration']} — sizing "
                f"production from config ({ceiling:,} designs) rather than "
                f"from a measurement. Run the calibration stage first if that "
                f"is not what you want.")
            return ceiling
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineError(
                f"could not read {path}: {exc}. It carries the measured "
                f"production size; re-run --start-from calibration.") from exc
        verdict = str(data.get("verdict") or "")
        if verdict and verdict not in ("SCALE_UP", "SCALE_UP_PARTIAL"):
            raise PipelineError(
                f"calibration recorded {verdict} for this campaign — "
                f"production is not what it recommends. Re-gate at a softer "
                f"bar or re-run the calibration; do not scale a campaign the "
                f"measurement said to stop.")
        n = int(data.get("n_production") or 0)
        if n <= 0:
            logger.warning(f"  {path.name} records no production size; using "
                           f"the config ceiling ({ceiling:,}).")
            return ceiling
        logger.info(f"  production sized at {n:,} designs, recovered from "
                    f"{path.name} (verdict {verdict or 'unrecorded'})")
        return n

    def _stage_boltzgen_scoring(self, dirs: dict[str, Path],
                                result: PipelineResult, *,
                                calib: dict | None = None) -> dict:
        """Rank with BOLTZGEN'S OWN ranking, and gate with the resolved config.

        Deliberately not `binder_metrics` + `binder_ranking`. BoltzGen's ranker
        is a MAXIMIN over six per-metric ranks — a design is judged by its
        WORST — and measured on a 994-design campaign that is a strong
        selector: its top-100 had a median design-vs-refold dock RMSD of 3.12 A
        with 94% under 5 A, against 8.89 A / 16.3% over the full set and
        8.01 A / 29% for iPTM-ranking. Re-scoring with the binder track's
        metrics is a separate, later question (and is possible — see
        `scripts/measure_boltzgen_binder_gates.py`).
        """
        import csv as _csv

        from src.boltzgen_runner import BoltzGenPaths
        from src.design_ranking import (
            gate_boltzgen_records, resolve_boltzgen_ranking,
        )

        for mode in ("production", "calibration", "pilot"):
            paths = BoltzGenPaths.under(dirs["campaign"], mode)
            if paths.metrics_csv.is_file():
                break
        else:
            raise PipelineError(
                f"no BoltzGen metrics table under {dirs['campaign']} — no "
                f"stage has produced scored designs yet")

        modality = self._resolve_modality(None, source="the campaign")
        rank = resolve_boltzgen_ranking(self.config, modality)
        records = self._boltzgen_records(paths)
        survivors, stats = gate_boltzgen_records(records, rank.thresholds)
        # BoltzGen already ordered them; `final_rank` is that order.
        survivors.sort(key=lambda r: float(r.get("final_rank") or 1e9))
        top_k = survivors[:int((self.config.get("design", {})
                                .get("ranking", {}) or {}).get("top_k", 20))]

        dirs["scoring"].mkdir(parents=True, exist_ok=True)
        cols = ["design_id", "final_rank", "design_to_target_iptm",
                "min_design_to_target_pae", "complex_plddt", "pass_filters",
                "designed_chain_sequence", "quality_score", "cif_path"]
        for rows, name in ((survivors, "ranked.csv"), (top_k, "top_k.csv")):
            path = dirs["scoring"] / name
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = _csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
        (dirs["scoring"] / "filter_stats.txt").write_text(
            stats.render() + f"\n\nranked by BoltzGen's own final_rank "
            f"(maximin over six per-metric ranks)\n", encoding="utf-8")

        report = dirs["binder"] / self._BINDER_STAGE_FILES["binder_scoring"]
        report.write_text(
            f"# BoltzGen scoring\n\n"
            f"- modality `{modality}`, gate "
            f"`{ {k: v for k, v in rank.thresholds.items() if v is not None} }`\n"
            f"- {stats.n_survivors:,} of {stats.n_input:,} designs pass; "
            f"top {len(top_k)} kept\n"
            f"- ranked by BoltzGen's own `final_rank`\n\n```\n"
            + stats.render() + "\n```\n", encoding="utf-8")
        result.stage_files["binder_scoring"] = report
        result.stages_completed.append("binder_scoring")
        self._record_stage("binder_scoring", "complete",
                           artifacts=[dirs["scoring"] / "top_k.csv"])
        return {"top_k": dirs["scoring"] / "top_k.csv", "ranking": None,
                "filter_stats": stats}

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
            FoundryError, collect, plan_campaign, prefilter_rate_observed,
            progress, render_progress, resume, run_design,
            sec_per_refold_observed, wait_for_campaign,
        )

        cfg = self._binder_cfg()
        paths = self._binder_paths(dirs, mode)
        paths.mkdirs()
        # This stage's OWN directory is empty when it is being planned, so
        # prefilter_rate_observed() returns 0 on a fresh stage and the fallback
        # decides the estimate. Prefer what the trial actually measured over the
        # generic default: on YAP1/TEAD1 the real rate was 0.83 while the
        # default is 0.59, which under-called production by ~380 refolds and
        # under-called the disk and GPU-hour estimates with it. This does not
        # affect completion — progress() swaps in the real MPNN count once MPNN
        # has written — but the disk CLAMP is computed from the estimate, so a
        # campaign sized near the budget could be under-clamped.
        observed = (prefilter_rate_observed(paths)
                    or self._persisted_prefilter_rate(dirs, mode)
                    or 0.59)
        # Same argument for the refold rate: a rate this target actually
        # achieved on this GPU beats any constant. Prefer this stage's own
        # (a resume mid-stage has one), then whatever the earlier stages
        # measured; failing both, plan_campaign scales its default by the
        # complex size, which a flat constant under-called by up to 54%.
        n_tokens = trim.n_residues_after + _binder_midpoint(trim.contig)
        rate = (sec_per_refold_observed(paths)
                or self._earlier_refold_rate(dirs, mode))
        plan = plan_campaign(cfg, paths, mode=mode, n_batches=n_batches,
                             prefilter_rate=observed, n_tokens=n_tokens,
                             sec_per_refold=rate or None)

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
        # `wait_for_campaign` returns when the work is done OR the driver
        # stopped — its own docstring calls those independent facts. So a
        # campaign that aborted (a half-installed foundry, a driver crash, a
        # GPU fault) lands here exactly like a finished one, and recording it
        # "complete" sends an empty campaign into calibration, which then
        # reports a STOP verdict phrased as a MEASURED rate. Nothing in that
        # chain ever says "this never ran".
        #
        # A campaign that produced FEWER refolds than planned is a different
        # thing and stays legitimate — a disk clamp or a per-shard GPU ECC
        # fault leaves a real, smaller sample, and the Wilson interval sizes
        # correctly off it. Only zero is unrecoverable.
        if final.n_rf3 == 0:
            log_hint = paths.logs_dir / "driver.log"
            raise FoundryError(
                f"the {mode} campaign produced no refolds at all "
                f"(RFD3 backbones: {final.n_rfd3:,}, MPNN sequences: "
                f"{final.n_mpnn:,}, RF3 refolds: 0 of {plan.expected_rf3:,} "
                f"planned). There is nothing to score, so this is not a weak "
                f"result — the campaign did not run.\n"
                f"The driver logs its own reason (look for a line starting "
                f"'ABORT:'):\n    tail -40 {log_hint}\n"
                f"Most common cause on a new machine is an incomplete foundry "
                f"install — check it with `python scripts/doctor.py`.")
        if not final.complete:
            logger.warning(
                f"[{mode}] campaign stopped short: {final.n_rf3:,} of "
                f"{plan.expected_rf3:,} planned refolds. Scoring the "
                f"{final.n_rf3:,} that exist — the interval widens, the "
                f"verdict stays honest. Driver log: "
                f"{paths.logs_dir / 'driver.log'}")
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
            workers=max(1, (os.cpu_count() or 4) - 2), limit=limit,
            patch=self._exposed_patch(dirs, sidecar))
        out_dir.mkdir(parents=True, exist_ok=True)
        write_scores(rows, out_dir / "refold_scores.csv")
        return rows

    @staticmethod
    def _exposed_patch(dirs: dict[str, Path], sidecar: Path) -> list[int]:
        """The trim's fresh hydrophobic patch, in RFD3 OUTPUT numbering.

        Read from `trim_map.json` rather than from a `TrimResult` in memory,
        for the same reason `_TrimFromDisk` exists at all: scoring is routinely
        reached by `--start-from binder_scoring` in a fresh process days after
        the trim ran, and a metric that silently became empty on a resume would
        be worse than not having it.

        Empty is the normal answer — 22 of the 25 trims in `projects/` are
        no-ops that expose nothing — and empty makes the patch columns read
        zero for every design, which `binder_ranking` z-scores to no
        contribution at all. A campaign written before the trim recorded this
        field also lands here, correctly: nothing is known about its patch, so
        nothing is claimed.
        """
        from src.binder_metrics import patch_from_rfd3

        trim_map = (dirs.get("trim") or Path(".")) / "trim_map.json"
        if not trim_map.exists():
            return []
        try:
            auth = json.loads(trim_map.read_text(encoding="utf-8")).get(
                "exposed_hydrophobic_auth") or []
        except Exception as exc:
            logger.warning(f"could not read the exposed patch from "
                           f"{trim_map}: {exc}")
            return []
        if not auth:
            return []
        out = patch_from_rfd3(sidecar, auth, "B")
        logger.info(
            f"  exposed patch: {len(auth)} residue(s) recorded by the trim, "
            f"{len(out)} remapped into the refolds' numbering — scored as a "
            f"per-design liability, not a gate")
        return out

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

    def _binder_compute_for_mode(self, mode: str, dirs: dict[str, Path],
                                 calib: dict | None) -> str:
        """
        Which compute path a given binder-track mode's `_run_gpu_stage` call
        actually dispatched to — `_stage_binder_scoring` needs this to pick
        local `FoundryPaths` vs. cluster `ClusterPaths` scoring, and has no
        other record of where a campaign ran (that decision was made, and
        possibly persisted, inside `_run_gpu_stage`/`_resolve_production_plan`
        during an earlier stage, potentially in a different process).

        Only "production" is ever placed dynamically (`choose_compute()` at
        the calibration gate, recovered via `_resolve_production_plan` —
        which itself reloads `calibration.json` from disk when `calib` is
        None, exactly the case on a fresh `--start-from binder_scoring`
        resume). Every other mode mirrors `_run_gpu_stage`'s own dispatch:
        `effective_compute = compute_override or self._compute`, no override
        passed for pilot/calibration, so the cluster branch triggers only on
        the literal string "cluster" — `self._compute == "auto"` always means
        local for those two.
        """
        if mode == "production":
            _, compute = self._resolve_production_plan(calib, dirs, None)
            return compute
        return "cluster" if self._compute == "cluster" else "local"

    def _cluster_paths_for_mode(self, mode: str, dirs: dict[str, Path],
                                cluster_cfg) -> "ClusterPaths":
        """
        Reconstruct the `ClusterPaths` a cluster stage staged, from just
        `dirs`/`mode`/`cluster_cfg` — enough to locate `refold_dir` for
        scoring, without calling `stage_campaign` again (which needs the
        spec/trim; a fresh `--start-from binder_scoring` resume in a
        separate process has neither, and re-staging isn't "resume" anyway
        — see `_run_cluster_stage`'s own docstring).

        `run_name`/`run_dir` are fully deterministic from `slug` + `mode`,
        mirroring `_run_cluster_stage`'s own derivation exactly.
        `spec_path`/`structure_path`/`msa_path`/`launch_script` are unused
        by `collect_campaign`/`_cluster_hotspots` (both only ever read
        `.refold_dir`), so placeholders here are harmless.
        """
        from src.cluster_runner import ClusterPaths

        stage_root = cluster_cfg.pipeline_root / cluster_cfg.stage_subdir
        run_name = f"{self._cluster_slug(dirs)}_{mode}"
        run_dir = stage_root / run_name
        if not run_dir.exists():
            # Campaigns staged before the slug carried the project name live
            # under the bare round/site directory name. Re-attach to one
            # rather than staging a duplicate beside it.
            legacy = f"{dirs['binder'].parent.name or 'campaign'}_{mode}"
            if (stage_root / legacy).exists():
                run_name, run_dir = legacy, stage_root / legacy
        return ClusterPaths(
            run_name=run_name, run_dir=run_dir,
            spec_path=run_dir / "unused.json",
            structure_path=run_dir / "unused.pdb",
            msa_path=None, launch_script=run_dir / "launch.sh")

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
        from src.foundry_runner import (prefilter_rate_observed,
                                        sec_per_refold_observed)

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
            # The SAME refold rate `plan_campaign` sizes production with.
            # Without this the gate costed every campaign at the flat 8.4 s
            # anchor while the planner used a measured or size-scaled rate, so
            # the gate's budget check and the plan it approved disagreed — by
            # 2.45x on MASH/TEAD4 (63 GPU-h claimed, 138 planned), which is the
            # difference between inside and outside the 120 h budget the
            # SCALE_UP verdict and the auto-raised bar were both decided on.
            # `or None`, not `or 0.0`: sec_per_refold_observed returns 0.0 when
            # it has too few timestamps to fit a rate, and calibrate() must see
            # None to fall through to the n_tokens size law rather than to the
            # bare anchor. The size-law fallback now lives in calibrate(), so
            # the gate and the planner cannot drift apart again.
            sec_per_rf3_refold=(
                sec_per_refold_observed(paths)
                or self._earlier_refold_rate(dirs, "calibration")
                or None),
            n_tokens=trim.n_residues_after + _binder_midpoint(trim.contig),
            disk_budget_gb=float(fcfg.get("disk_budget_gb", 120)),
            max_campaign_days=float(fcfg.get("max_campaign_days", 5)),
            adaptive_bar=bool(rcfg.get("adaptive_bar", True)),
            n_gpus_cluster=ClusterConfig.from_cfg(self.config).n_gpus,
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
        # `choose_compute` PROPOSES a placement; it only DECIDES under
        # `--compute auto`. An explicit `--compute local` / `--compute cluster`
        # forces every GPU stage onto one path unconditionally, so honouring the
        # proposal here would silently override the user: a 20 GPU-h estimate
        # under `--compute cluster` would run for a day on the workstation, and
        # a 60 GPU-h one under `--compute local` would stage a SLURM package and
        # pause on a machine explicitly told to stay local.
        if self._compute == "auto":
            compute = compute_choice.compute if compute_choice else "local"
        else:
            compute = self._compute
            if compute_choice is not None and compute_choice.compute != compute:
                logger.info(
                    f"compute choice {compute_choice.compute!r} overridden by "
                    f"explicit --compute {compute}")
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

    def _earlier_refold_rate(self, dirs: dict[str, Path], mode: str) -> float:
        """Seconds per refold measured by a stage that already ran.

        Stages get cheaper-to-more-expensive (pilot -> calibration ->
        production) against the same target on the same GPU, so an earlier
        stage's achieved rate is the best available predictor for the next
        one. Measured within ~10-17% of production on all three campaigns that
        have run both.
        """
        from src.foundry_runner import sec_per_refold_observed
        order = ["pilot", "calibration", "production"]
        earlier = order[:order.index(mode)] if mode in order else []
        for prior in reversed(earlier):          # nearest in size wins
            rate = sec_per_refold_observed(self._binder_paths(dirs, prior))
            if rate:
                return rate
        return 0.0

    def _persisted_prefilter_rate(self, dirs: dict[str, Path],
                                  mode: str = "production") -> float:
        """The prefilter rate an EARLIER stage measured, or 0.0.

        Two sources, and both are needed. `_stage_calibration` writes its rate
        to calibration.json alongside the batch counts, which is what lets a
        production stage planned in a FRESH process (the normal
        `--start-from production` case) size itself on measurement. But that
        file does not exist yet when CALIBRATION itself is being planned, and
        the pilot has already measured a rate by then — so fall back to
        reading it off the earlier stages' directories directly, nearest in
        size first, exactly as `_earlier_refold_rate` does for seconds per
        refold.

        That hop was missing, and it is not cosmetic. On the MASH/TEAD4
        campaign the pilot measured 0.86 and calibration was nonetheless
        planned at the 0.59 default: 580 backbones planned as
        int(580*0.59)*4 = 1,368 refolds where the real figure is
        int(580*0.86)*4 = 1,992, a 46% under-count of the work, and with it
        the disk clamp, the GPU-hour estimate, and the local-vs-cluster
        decision `choose_compute()` makes from it.
        """
        from src.foundry_runner import prefilter_rate_observed

        def _ok(rate: float) -> float:
            # A nonsense rate would silently distort every downstream estimate.
            return rate if 0.0 < rate <= 1.0 else 0.0

        path = dirs["calibration"] / "calibration.json"
        try:
            rate = _ok(float(json.loads(path.read_text(encoding="utf-8"))
                             .get("prefilter_rate") or 0.0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            rate = 0.0
        if rate:
            return rate

        order = ["pilot", "calibration", "production"]
        earlier = order[:order.index(mode)] if mode in order else []
        for prior in reversed(earlier):          # nearest in size wins
            try:
                rate = _ok(prefilter_rate_observed(self._binder_paths(dirs, prior)))
            except Exception as exc:
                logger.debug(f"could not read {prior} prefilter rate: {exc}")
                continue
            if rate:
                logger.info(f"  prefilter rate {rate:.2f} measured by the "
                            f"{prior} stage (default is 0.59)")
                return rate
        return 0.0

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
        # An explicit --compute on THIS invocation outranks whatever placement
        # was persisted, so a resume can be redirected (the cluster queue is
        # full; the workstation GPU is now free) without editing calibration
        # JSON. Under `auto` the persisted decision stands — re-deciding on a
        # resume is what `_resolve_production_plan` exists to prevent.
        forced = self._compute if self._compute != "auto" else None
        if calib:
            # `or`, not `.get(k, default)`: the key can be present-but-None
            # (a site-trial winner whose calibration never set a placement).
            compute = forced or calib.get("compute") or default_compute
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
            compute = forced or cc.get("compute") or default_compute
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
                              result: PipelineResult, *,
                              calib: dict | None = None) -> dict:
        from src.binder_ranking import (
            DEFAULT_Z_CLIP, rank_designs, read_scores, write_ranking_outputs,
        )

        cfg = self._binder_cfg()
        rcfg = cfg.get("binder_ranking") or {}
        # Prefer production output; fall back to calibration when production was
        # never run (a pilot-only or ITERATE round still deserves a ranking).
        #
        # `_binder_compute_for_mode` resolves the same compute decision each
        # mode's own `_run_gpu_stage` call made — for "production" specifically
        # that reuses `_resolve_production_plan`, which recovers the decision
        # from `calibration.json` when `calib` is None (a fresh
        # `--start-from binder_scoring` resume in a separate process, the
        # normal case after a multi-day GPU campaign finishes). A production
        # run placed on the cluster has no local RF3 output at all, so without
        # this a `--compute auto`/`--compute cluster` production campaign
        # could never be scored.
        rows, source = None, None
        # Set ONLY for a local RF3 campaign. `prune_confidences` walks the RF3
        # layout (`<id>/<id>_summary_confidences.json`), which a cluster
        # Protenix tree does not have, and a cluster tree is on shared storage
        # this process should not be deleting from anyway.
        scored_rf3_dir: Path | None = None
        for mode in ("production", "calibration", "pilot"):
            if self._binder_compute_for_mode(mode, dirs, calib) == "cluster":
                from src.cluster_runner import ClusterConfig, refold_counts

                cluster_cfg = ClusterConfig.from_cfg(self.config)
                cpaths = self._cluster_paths_for_mode(mode, dirs, cluster_cfg)
                if refold_counts(cpaths, cluster_cfg.refold_backend)["n_refolds"]:
                    rows = self._score_campaign(cpaths, dirs, dirs["scoring"],
                                                cluster_cfg=cluster_cfg)
                    source = mode
                    break
                continue

            paths = self._binder_paths(dirs, mode)
            if paths.rf3_dir.is_dir():
                from src.foundry_runner import count_rf3

                if count_rf3(paths.rf3_dir):
                    rows = self._score_campaign(paths, dirs, dirs["scoring"])
                    source = mode
                    scored_rf3_dir = paths.rf3_dir
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
        # `z_clip` is read once and applied to both this gate-time ranking and
        # the Rosetta-augmented re-rank below, so the two cannot disagree about
        # how much one metric may dominate.
        z_clip = rcfg.get("z_clip", DEFAULT_Z_CLIP)
        gated = rank_designs(
            rows, thresholds=rcfg.get("thresholds"), weights=rcfg.get("weights"),
            mmr=rcfg.get("mmr"), top_k=int(rcfg.get("top_k", 20)),
            max_per_backbone=int(rcfg.get("max_per_backbone", 1)),
            z_clip=z_clip)

        rosetta_note = ""
        rcfg_ros = rcfg.get("rosetta") or {}
        # Two switches, deliberately: `design.pyrosetta.enabled` is the global
        # "is PyRosetta available/wanted at all" (shared with the PPI track's
        # SASA stage), while `design.binder_ranking.rosetta.enabled` turns off
        # just this track's scoring even on a machine that has it. Checking
        # availability here rather than inside score_designs means a missing
        # install produces one clear line instead of 300 per-design failures.
        from src.pyrosetta_sasa import check_available

        ros_available, ros_reason = check_available(cfg)
        if rcfg_ros.get("enabled", True) and gated.survivors and ros_available:
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
                    max_per_backbone=int(rcfg.get("max_per_backbone", 1)),
                    z_clip=z_clip)
                ranking.filter_stats = gated.filter_stats
            else:
                rosetta_note = f"\n\nRosetta metrics skipped: {ros.skipped_reason}"
                ranking = gated
        else:
            if gated.survivors and not ros_available:
                # Say WHY, and say it in the report as well as the log — the
                # composite is weighted differently without these terms, so a
                # reader comparing two campaigns needs to know.
                logger.warning(
                    f"Rosetta interface metrics skipped — {ros_reason}. Designs "
                    f"are ranked on the folding/geometry terms only.")
                rosetta_note = (
                    f"\n\nRosetta interface metrics were **not** computed for "
                    f"this run ({ros_reason}). PyRosetta is optional and is "
                    f"used only here, after the gates; ranking used the "
                    f"folding-confidence and geometry terms only. Composite "
                    f"scores are not comparable with a run that had it.")
            ranking = gated
        paths_out = write_ranking_outputs(ranking, dirs["scoring"])

        prune_note = self._prune_scored_confidences(scored_rf3_dir, gated.survivors)

        out = dirs["binder"] / self._BINDER_STAGE_FILES["binder_scoring"]
        self._write_binder_report(
            out, "Design scoring and ranking",
            f"Scored **{len(rows):,}** refolds from the {source} campaign.\n\n"
            f"```\n{ranking.filter_stats.render()}\n```\n\n"
            f"{len(gated.survivors):,} survivors across "
            f"{gated.n_backbones:,} distinct backbones; "
            f"top {len(ranking.top_k)} selected.{rosetta_note}{prune_note}",
            {"scores_csv": str(dirs["scoring"] / "refold_scores.csv"),
             "top_k_csv": str(paths_out["top_k"]),
             "n_scored": len(rows), "n_survivors": len(ranking.survivors)})
        self._record_stage("binder_scoring", "complete", out,
                           stage="binder_scoring")
        result.stage_files["binder_scoring"] = out
        result.stages_completed.append("binder_scoring")
        return {"ranking": ranking, "top_k": paths_out["top_k"]}

    def _prune_scored_confidences(self, rf3_dir: Path | None,
                                  survivors: list[dict]) -> str:
        """
        Delete the PAE matrices of gate FAILURES, once scoring has finished.

        `*_confidences.json` is ~half an RF3 design directory (405 KB of a
        typical 792 KB), and disk — not GPU — is the binding constraint on a
        production campaign. This is the only call site of
        `binder_metrics.prune_confidences`, and it is deliberately placed after
        `write_ranking_outputs`: ipSAE is computed FROM these matrices and
        cannot be recomputed once they are gone, so nothing may run before the
        scores and the ranking are on disk.

        Three guards, in order:

        * off unless `design.foundry.prune_confidences` is set — it is
          irreversible, so the default keeps the data;
        * local RF3 campaigns only (`rf3_dir` is None for a cluster run);
        * **skipped entirely when nothing survived** — a zero-survivor run is
          exactly the one an ITERATE verdict tells you to re-gate at a softer
          bar, and pruning would delete the evidence needed to do that.

        Returns a markdown note for the stage report ("" when nothing ran), so
        a reader of the report knows the tree is no longer re-gateable.
        """
        if not bool((self.config.get("design") or {}).get("foundry", {})
                    .get("prune_confidences", False)):
            return ""
        if rf3_dir is None:
            logger.info("confidence pruning skipped: not a local RF3 campaign")
            return ""
        if not survivors:
            logger.warning(
                "confidence pruning skipped: nothing survived the gates, so the "
                "PAE matrices are the only way to re-gate this campaign at a "
                "softer bar")
            return ""

        from src.binder_metrics import prune_confidences

        keep = {str(r.get("name", "")) for r in survivors if r.get("name")}
        try:
            removed = prune_confidences(rf3_dir, keep)
        except OSError as exc:  # never fail a scored campaign over housekeeping
            logger.warning(f"confidence pruning failed: {exc}")
            return ""
        return (
            f"\n\nPruned **{removed / 1e9:.2f} GB** of PAE matrices from "
            f"{len(keep):,} kept / all scored designs "
            f"(`design.foundry.prune_confidences`). ipSAE cannot be recomputed "
            f"for the pruned designs — re-gating this campaign at a softer bar "
            f"would need a re-fold.")

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
    # BoltzGen writes NONE of the columns above, and its top_k.csv is what the
    # analyst is shown on a `--modality cyclic_peptide` run. Without this the
    # column intersection came out EMPTY and the analyst was handed a table
    # with no columns at all, which it correctly read as "no candidates" —
    # and then wrote NO_GO over a campaign with 1,700 gate survivors on disk
    # (pdl1_macrocycle, 2026-09-14). The sequence column is deliberately
    # excluded here for the same reason it is in the foundry set: sequences go
    # to the FASTA, not into LLM context.
    _BINDER_SUMMARY_COLS_BOLTZGEN = (
        "design_id", "final_rank", "quality_score", "design_to_target_iptm",
        "min_design_to_target_pae", "complex_plddt", "pass_filters",
    )
    # Either spelling of the sequence column: foundry's scorer writes
    # `binder_seq`, BoltzGen's `designed_chain_sequence`.
    _BINDER_SEQ_COLS = ("binder_seq", "designed_chain_sequence")

    @staticmethod
    def _top_k_header(top_k_csv: Path) -> list[str]:
        """The column names of a `top_k.csv`, or [] if it cannot be read.

        An unreadable file must not take the analyst stage with it: the
        designs are already on disk by then, and the write-up is what would
        be lost.
        """
        import csv as _csv

        try:
            with Path(top_k_csv).open(encoding="utf-8") as _fh:
                return next(_csv.reader(_fh), [])
        except OSError:
            return []

    @classmethod
    def _top_k_vocabulary(cls, header) -> str:
        """Which metric vocabulary a `top_k.csv` is written in.

        Decided from the COLUMNS, not from `self._design_engine`, so the
        track named in the analyst's prompt is provably the track whose
        columns the analyst was handed — the two cannot drift apart, and a
        report regenerated by `--start-from binder_summary` in a fresh
        process needs no runner state to get it right.
        """
        if any(c in header for c in cls._BINDER_SUMMARY_COLS):
            return "foundry"
        if any(c in header for c in cls._BINDER_SUMMARY_COLS_BOLTZGEN):
            return "boltzgen"
        return "unknown"

    @classmethod
    def _slim_binder_top_k(cls, top_k_csv: Path) -> str:
        import csv as _csv
        import io as _io

        with Path(top_k_csv).open(encoding="utf-8") as _fh:
            rows = list(_csv.DictReader(_fh))
        if not rows:
            return "(no designs survived ranking)"
        vocab = cls._top_k_vocabulary(rows[0])
        if vocab == "foundry":
            cols = [c for c in cls._BINDER_SUMMARY_COLS if c in rows[0]]
        elif vocab == "boltzgen":
            cols = [c for c in cls._BINDER_SUMMARY_COLS_BOLTZGEN
                    if c in rows[0]]
        else:
            cols = []
        if not cols:
            # Neither vocabulary matched. Show the file's own columns rather
            # than an empty table: a misread scorer output must look like a
            # surprise to whoever reads the report, not like an empty run.
            cols = [c for c in rows[0]
                    if c not in cls._BINDER_SEQ_COLS and "path" not in c]
            logger.warning(
                f"{Path(top_k_csv).name}: no known metric columns "
                f"({sorted(rows[0])[:6]}...) — showing the file's own columns")
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

        with Path(top_k_csv).open(encoding="utf-8") as _fh:
            rows = list(_csv.DictReader(_fh))
        # BoltzGen calls it `designed_chain_sequence`; keying only on foundry's
        # `binder_seq` wrote NO fasta at all for a completed cyclic-peptide
        # campaign — i.e. it silently withheld the one artifact a macrocycle
        # run exists to produce (pdl1_macrocycle, 2026-09-14).
        seq_col = next((c for c in PipelineRunner._BINDER_SEQ_COLS
                        if any(r.get(c) for r in rows)), None)
        if not seq_col:
            return None
        entries = [r for r in rows if r.get(seq_col)]
        if not entries:
            return None
        # Annotate with whichever metrics this track actually wrote, so the
        # header is informative on both and empty on neither.
        ann = [("ipsae_min", "ipsae_min"), ("dock_rmsd", "binder_rmsd_dock"),
               ("iptm", "design_to_target_iptm"),
               ("min_pae", "min_design_to_target_pae"),
               ("complex_plddt", "complex_plddt")]
        lines = []
        for i, r in enumerate(entries, 1):
            tags = " ".join(f"{label}={r[col]}" for label, col in ann
                            if r.get(col) not in (None, ""))
            name = r.get("name") or r.get("design_id") or ""
            lines.append(f">rank{i:03d}_{name} {tags}".rstrip())
            lines.append(r[seq_col])
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return dest

    def _stage_binder_summary(self, top_k_csv: Path, intel: dict[str, str],
                              dirs: dict[str, Path],
                              result: PipelineResult, *,
                              ranking=None, calib: dict | None = None,
                              ) -> dict[str, str]:
        fasta = self._write_binder_fasta(top_k_csv, dirs["scoring"] / "top_k.fasta")
        if fasta:
            logger.info(f"orderable sequences -> {fasta}")
        slim = self._slim_binder_top_k(top_k_csv)

        # What the run already DECIDED, stated up front. Without it the analyst
        # sees only a top-20 of the best surviving designs — which look good by
        # construction, because that is what a top-20 is — and has no way to
        # know the campaign was declared not worth scaling, or how many refolds
        # were dropped to produce them. Both completed campaigns' reports say
        # "No red flags identified" over funnels that discarded ~75% of refolds.
        # Stated from the ACTUAL engine, not hardcoded. A BoltzGen campaign
        # was being told its BoltzGen columns were foundry metrics, and the
        # analyst duly diagnosed it in foundry's vocabulary — recommending
        # more solubleMPNN sequences per backbone for a run that uses neither
        # solubleMPNN nor backbones.
        if self._top_k_vocabulary(self._top_k_header(top_k_csv)) == "boltzgen":
            facts = ["Track: BoltzGen (design -> inverse_folding -> folding). "
                     "The columns below are BoltzGen's OWN metrics, not "
                     "foundry's: `design_to_target_iptm` is its iPTM, "
                     "`min_design_to_target_pae` its interface PAE, and "
                     "`pass_filters` is its nine-check self-consistency AND, "
                     "dominated by a 2.0 A design-vs-refold RMSD. There is no "
                     "ipSAE, no dock RMSD and no hotspot_engagement on this "
                     "track, so do not ask for them."]
        else:
            facts = ["Track: foundry (RFD3 -> solubleMPNN -> RF3). The columns "
                     "below are foundry metrics, NOT BoltzGen's."]
        if calib and calib.get("result") is not None:
            cr = calib["result"]
            facts.append(
                f"Calibration verdict: {cr.verdict} — {cr.verdict_reason}")
        if result.go_recommendation == "NO_GO":
            facts.append(
                "The pipeline has already recorded NO_GO for this campaign on "
                "the calibration measurement above. Your verdict may not be "
                "GO. Report the best of these designs plainly and put the "
                "re-tune in Recommended next steps.")
        if ranking is not None:
            try:
                facts.append(
                    f"Gate funnel: {ranking.filter_stats.n_records:,} refolds "
                    f"scored, {ranking.filter_stats.n_survivors:,} passed every "
                    f"hard gate, top {len(ranking.top_k)} shown. Drop reasons:\n"
                    f"{ranking.filter_stats.render()}")
            except Exception as exc:      # a stats-shape change must not fail a run
                logger.debug(f"could not render filter stats for the analyst: {exc}")
        hs = result.hotspot_residues_json
        if hs:
            try:
                facts.append(f"The interface stage declared "
                             f"{len(json.loads(hs).get('residues') or [])} hotspots; "
                             f"`hotspot_engagement` is the FRACTION of those a "
                             f"design contacts, and the gate is 0.75, not 1.0.")
            except Exception:
                pass

        q = (f"Review the top designed binders against "
             f"{intel.get('target_gene', 'the target')} "
             f"({intel.get('partner_name', 'partner')} interface, "
             f"{intel.get('design_intent', 'disrupt')} mode).\n\n"
             + "\n\n".join(facts) + f"\n\n{slim}")
        out = dirs["binder"] / self._BINDER_STAGE_FILES["binder_summary"]
        handoff = self._run_stage("design-analyst", q, [], out,
                                  stage="binder_summary")
        result.stage_files["binder_summary"] = out
        result.stages_completed.append("binder_summary")
        # Validate the enum, and never let the analyst UPGRADE a deterministic
        # NO_GO. `_run_binder_track` sets NO_GO from the calibration verdict and
        # then calls this stage to report on what the trial did produce; an
        # unconditional assignment here let an LLM looking at a flattering
        # top-20 flip a measured STOP back to GO. The retired PPI sibling
        # (`_stage_summary`) had the enum check; this path had neither it nor
        # the floor.
        go = (handoff.get("go_recommendation") or "").upper().replace("-", "_")
        if go in ("GO", "CONDITIONAL_GO", "NO_GO"):
            if result.go_recommendation == "NO_GO" and go != "NO_GO":
                logger.warning(
                    f"  ⚠ design-analyst returned {go} for a campaign the "
                    f"calibration gate already declared NO_GO — keeping NO_GO. "
                    f"Its written report is still in {out.name}.")
            else:
                result.go_recommendation = go
        return handoff

    def _adopt_parent_interface(
        self, dirs: dict[str, Path], site_dirs: dict[str, Path],
        site_intel: dict[str, str], result: PipelineResult,
    ) -> str | None:
        """The interface analysis ALREADY on disk, adopted for a single site.

        `--stop-after spec|trial` routes into `_run_site_trials` (see its
        caller), which used to call `_stage_binder_interface` unconditionally.
        On a PPI-bridged run that is a SECOND `complex-structure-analysis`
        call: `_bridge_ppi_to_binder_track` has already copied the structure
        stage's own report to the parent `21_interface.md` and entered at
        `trim` precisely so the analysis is not paid for twice.

        Measured on `projects/e2e_foundry_r2` round-4 (2026-09-13), which is
        what found this: the two calls DISAGREED about which region was
        primary — the structure stage kept the Central Hydrophobic Core and
        dropped the Basic/Aromatic Flank, the second call did the reverse —
        and the spec was built from the later one, i.e. against the region the
        structure stage had rejected. `_prepared_site` then reuses that spec on
        every resume, so the divergence would have outlived the run that made
        it. Cost was the lesser half ($0.09 a call); a preview that previews a
        different campaign is the real defect, since `--stop-after spec` exists
        to show what the GPU run will do.

        Single-site ONLY, and the caller enforces that: with `--trial-sites N`
        each site is a DIFFERENT epitope and must get its own analysis — that
        is the whole point of comparing sites on measured yield.

        Returns the hotspot JSON, or None when there is nothing to adopt (a
        `--workflow binder` run with no prior artifact), in which case the
        caller runs the stage as before.
        """
        src_path = dirs["binder"] / self._BINDER_STAGE_FILES["interface"]
        if not src_path.exists():
            return None
        text = src_path.read_text(encoding="utf-8")
        handoff = self._parse_handoff(text)
        hotspots_json = self._parse_hotspot_residues(text, handoff)
        if not hotspots_json:
            # A file that exists but yields no table is a real problem, and
            # re-running the stage is the right recovery — not a hard failure.
            logger.warning(
                f"  {src_path.name} has no parsable MODEL-READY HOTSPOTS "
                f"table; running the interface stage instead of adopting it")
            return None

        pdb_id = (handoff.get("pdb_id") or site_intel.get("pdb_id") or "")
        # The guards `_stage_binder_interface` would have run. An adopted
        # artifact is exactly the case a stale file could poison, and both
        # checks read files already on disk.
        self._verify_target_chain_assignment(site_intel, handoff, pdb_id)
        self._verify_hotspot_grounding(hotspots_json, pdb_id,
                                       site_intel.get("target_uniprot"))

        dst = site_dirs["binder"] / self._BINDER_STAGE_FILES["interface"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        result.stage_files["interface"] = dst
        result.hotspot_residues_json = hotspots_json
        n = len(json.loads(hotspots_json).get("residues", []))
        logger.info(
            f"  adopting the interface analysis already on disk "
            f"({src_path}) — {n} hotspot(s), no second LLM call. The epitope "
            f"is the one the upstream stage chose.")
        return hotspots_json

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
                    # One site: adopt whatever analysis is already on disk
                    # rather than paying for a second, possibly DIFFERENT one.
                    # Several sites: each is its own epitope, so each needs
                    # its own analysis.
                    hotspots_json = (
                        self._adopt_parent_interface(
                            dirs, site_dirs, site_intel, site_result)
                        if len(sites) == 1 else None)
                    if hotspots_json is None:
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
                    # Both sizings + the placement, so the winner's production
                    # stage isn't re-derived (and mis-sized) downstream.
                    "compute": calib.get("compute"),
                    "n_batches_local": calib.get("n_batches_local"),
                    "n_batches_cluster": calib.get("n_batches_cluster"),
                    "dirs": site_dirs, "spec": spec, "trim": trim,
                    "n_refolds": count_rf3(paths.rf3_dir),
                    "contig": trim.contig, "error": None,
                })
            except PipelinePausedError:
                # A pause is the pipeline working as designed, not a failed
                # site: `--detach` raises "<mode>_running" and `--compute
                # cluster` raises "<mode>_cluster_pending". PipelinePausedError
                # subclasses PipelineError, so catching PipelineError below
                # swallowed it and recorded a perfectly healthy running campaign
                # as "FAILED: Paused at calibration_running" — and, if every
                # site paused, returned NO_GO "every site trial failed" while
                # the GPU jobs ran on. Let it propagate so the user sees the
                # pause and its resume instructions.
                raise
            except PipelineError as exc:
                # One unusable site must not abandon the others. (PipelineError
                # covers PipelineBlockedError, which subclasses it.)
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

    def resume_site_stage(self, run_dir: Path, site_id: str, stage: str,
                          n_batches: int, *, attach: bool = True) -> dict:
        """
        Re-enter ONE site's own calibration campaign directly, by site id.

        `_run_site_trials`'s normal re-entry points (`--trial-sites N>1` or
        `--stop-after trial`) always recompute `n_batches` from
        `--trial-backbones`, which must exactly match the value the site was
        originally staged with or the completion check
        (`ClusterPlan.expected_rf3` / foundry's own `plan.expected_rf3`)
        silently targets the wrong count — and they re-derive ALL sites from
        `target_intel`'s candidate list, not just the one being resumed.
        This is the narrow bypass: go straight to the named site's own
        `binder/sites/<site_id>/` stage files with an explicit `n_batches`.

        Shared implementation behind both
        `scripts/run_pipeline.py --start-from calibration --site <id>` and
        the standalone `scripts/resume_cluster_calibration.py` (kept for
        backward compatibility) — one code path, so a future fix to either
        caller's bug doesn't have to be made twice.

        Only `stage="calibration"` is supported today: production has no
        single owning method the same way calibration does (`_stage_calibration`
        both runs/collects AND scores AND writes the verdict in one call);
        ask if a per-site production resume is ever needed.
        """
        if stage != "calibration":
            raise PipelineError(
                f"per-site resume only supports stage='calibration' today, "
                f"got {stage!r} — production has no single owning method the "
                f"same way; ask if you need this.")

        dirs = self._binder_dirs(run_dir)
        site_dirs = self._binder_dirs(dirs["sites"] / site_id)

        # Reuses the exact same (trim, spec) loader + validate_spec check
        # `_run_site_trials` itself uses to skip re-preparing an already-
        # prepared site — a per-site resume is exactly that case.
        prepared = self._prepared_site(site_dirs)
        if prepared is None:
            raise PipelineError(
                f"no usable prepared site under {site_dirs['spec']} / "
                f"{site_dirs['trim']} — the site trial must have generated "
                f"a spec + trim_map.json before this can resume it.")
        trim, spec_path = prepared

        result = PipelineResult(run_dir=run_dir)
        return self._stage_calibration(
            spec_path, trim, site_dirs, result, attach=attach,
            n_batches=n_batches)

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

    # `_generate_ppi_report` lived here. Its only callers were the legacy
    # `analysis` and `summary` stages, which are retired — a bridged PPI run
    # gets `_generate_binder_report` instead. `src/ppi_report.py` stays: it
    # is still the renderer for the archived legacy runs in `outputs/`, and
    # `scripts/generate_ppi_report.py` imports it directly. See
    # LEGACY_RETIREMENT_SCOPE.md.

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
            self._refuse_unbuilt_glue_paths(
                intel.get("design_intent"), source="the target-intel stage")
            result.pdb_id = result.pdb_id or intel.get("pdb_id")
            # A `--start-from production` resume never runs the discovery
            # stages, so nothing else repopulates this and the completion
            # banner printed "Target complex: unknown" for a campaign that knew
            # exactly what it was designing against. target_intel has both.
            if not result.target_complex:
                gene = intel.get("target_gene")
                partner = intel.get("partner_name")
                if gene:
                    result.target_complex = (
                        f"{gene} / {partner}" if partner else gene)

            # Advisory select-agent screen, once the target is named and
            # BEFORE any GPU stage — the point is to reach the operator
            # before a multi-day campaign, not after it.
            self._screen_select_agents(
                "target_intel",
                [query, result.target_complex or "",
                 intel.get("target_gene") or "",
                 intel.get("partner_name") or ""],
                pdb_id=intel.get("pdb_id") or "")

            # ── Site trials: compare epitopes by measured yield ─────────────
            # Reasoning cannot settle which of two defensible sites is more
            # designable; a few hundred backbones each can.
            if self._trial_sites > 1 or self._stop_after in ("trial", "spec"):
                self._refuse_undispatched_site_trials()
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
                    # A stop-after is a SUCCESS only if a site got that far.
                    # This returned unconditionally, so a run whose every site
                    # failed — the commonest cause being a `--hotspots` residue
                    # that is not in the chain — printed "PIPELINE COMPLETE",
                    # reported GO/NO-GO INCOMPLETE, wrote no spec, and exited
                    # 0. Anything reading the exit code, or reading the tail of
                    # the log, saw a clean run.
                    if not any(t.get("error") is None for t in trials):
                        result.go_recommendation = "NO_GO"
                        errs = "; ".join(
                            str(t.get("error"))[:80] for t in trials
                            if t.get("error"))
                        result.go_rationale = (
                            f"every site trial failed before "
                            f"{self._stop_after}: {errs}")
                        result.error = result.go_rationale
                        return result
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
                # `binder_dir` was bound from the TOP-LEVEL run dir before the
                # site trials ran; rebinding `dirs` without it left B1's resume
                # branch reading a top-level 21_interface.md that a multi-site
                # run never writes (its real artifacts live under
                # binder/sites/<site_id>/binder/), killing the campaign right
                # after paying for N GPU trials.
                binder_dir = dirs["binder"]
                result.pdb_id = best["site"].get("pdb_id")
                start_idx = 6           # straight to production for the winner
                spec_path = best["spec"]
                trim = best["trim"]
                # Carry the winner's compute placement and BOTH sizings forward:
                # `n_batches` alone is whichever the trial chose, and
                # `_resolve_production_plan` would then read a cluster-sized
                # count (already divided by n_gpus) as a local total and run
                # production at 1/n_gpus of the intended size.
                calib = {"result": best["calibration"],
                         "n_batches": best.get("n_batches"),
                         "compute": best.get("compute"),
                         "n_batches_local": best.get("n_batches_local"),
                         "n_batches_cluster": best.get("n_batches_cluster")}

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

            # ── B3: generator spec ──────────────────────────────────────────
            # The backend seam. Everything above (target_intel, interface,
            # trim) is generator-neutral and unchanged; everything below is
            # dispatched, with foundry as the branch that does not move.
            bg = self._boltzgen_backend
            if start_idx <= 3:
                spec_path = (
                    self._stage_boltzgen_spec(intel, hotspots_json, trim,
                                              dirs, result)
                    if bg else
                    self._stage_binder_spec(intel, hotspots_json, trim,
                                            dirs, result))
            else:
                pattern = "*.yaml" if bg else "*.json"
                specs = sorted(dirs["spec"].glob(pattern))
                if not specs:
                    raise PipelineError(
                        f"no {'BoltzGen' if bg else 'RFD3'} spec ({pattern}) "
                        f"in {dirs['spec']}")
                spec_path = specs[0]

            # ── B4: pilot — proves the spec runs before anything big ────────
            if start_idx <= 4:
                if bg:
                    self._run_boltzgen_stage("pilot", spec_path, trim, dirs,
                                             result, attach=attach,
                                             num_designs=n_batches)
                else:
                    self._run_gpu_stage("pilot", spec_path, trim, dirs, result,
                                        attach=attach, n_batches=n_batches)

            if self._stop_after == "pilot":
                # The smallest stop that still proves the GPU path end to end:
                # a spec was built, the generator accepted it, and files landed
                # on disk. `--stop-after spec` stops one step short of the GPU
                # and `--stop-after calibration` is 300 backbones x 4 refolds —
                # hours, not minutes — so neither answers "is the pipeline
                # still working" cheaply. The pilot is 100 designs on foundry
                # (`design.foundry.pilot.n_batches: 25` x a diffusion batch of
                # 4) and 24 on BoltzGen.
                raise PipelinePausedError("pilot_complete", {
                    "designs": "see the pilot directory",
                    "spec": str(spec_path),
                    "resume": "--start-from calibration",
                })

            # ── B5: calibration — MEASURE the scale production needs ────────
            calib = locals().get("calib")
            if start_idx <= 5:
                calib = (
                    self._stage_boltzgen_calibration(
                        spec_path, trim, dirs, result, attach=attach,
                        num_designs=n_batches)
                    if bg else
                    self._stage_calibration(spec_path, trim, dirs, result,
                                            attach=attach,
                                            n_batches=n_batches))
                verdict = calib["result"].verdict
                if verdict in ("ITERATE", "STOP"):
                    result.go_recommendation = "NO_GO"
                    result.go_rationale = calib["result"].verdict_reason
                    logger.warning(
                        f"calibration says {verdict}; not scaling up. "
                        f"{calib['result'].verdict_reason}")
                    # Still score and rank what the calibration produced — an
                    # ITERATE round has real designs worth looking at.
                    scored = (self._stage_boltzgen_scoring(dirs, result,
                                                           calib=calib)
                              if bg else
                              self._stage_binder_scoring(dirs, result,
                                                         calib=calib))
                    self._stage_binder_summary(
                        scored["top_k"], intel, dirs, result,
                        ranking=scored.get("ranking"), calib=calib)
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
                if bg:
                    # Sized by the calibration, recovered from calibration.json
                    # on a fresh-process resume for the same reason
                    # `_resolve_production_plan` exists: the recommendation
                    # only lived in the process that measured it, and falling
                    # back to the config default silently ran a campaign at the
                    # wrong scale.
                    n_prod = self._resolve_boltzgen_production(calib, dirs)
                    self._run_boltzgen_stage(
                        "production", spec_path, trim, dirs, result,
                        attach=attach, num_designs=n_prod)
                else:
                    prod_n_batches, prod_compute = self._resolve_production_plan(
                        calib, dirs, n_batches)
                    self._run_gpu_stage(
                        "production", spec_path, trim, dirs, result,
                        attach=attach, n_batches=prod_n_batches,
                        compute_override=prod_compute)

            # ── B7: score + rank ────────────────────────────────────────────
            if start_idx <= 7:
                scored = (self._stage_boltzgen_scoring(dirs, result, calib=calib)
                          if bg else
                          self._stage_binder_scoring(dirs, result, calib=calib))
            else:
                scored = {"top_k": dirs["scoring"] / "top_k.csv"}

            # ── B8: analyst review ──────────────────────────────────────────
            if start_idx <= 8:
                H["binder_summary"] = self._stage_binder_summary(
                    scored["top_k"], intel, dirs, result,
                    ranking=scored.get("ranking"), calib=locals().get("calib"))

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

        # Only when a trim will actually follow. `--design-engine
        # boltzgen_legacy` has no trim stage, so there the raw length IS the
        # operative number and the existing refusal is correct. Both bridged
        # generators DO trim — `_stage_trim` is generator-neutral and runs
        # before the spec on either — so quoting a raw 574-residue GPCR at
        # them would have the stage reject a chain that is 167 designable
        # residues by the time anything builds against it.
        designable: dict[str, int] = {}
        if self._bridges_to_binder_track:
            designable = self._designable_chain_sizes(pdb_id, chain_counts, max_target)

        chain_hint = ""
        if chain_descs or chain_counts:
            lines: list[str] = []
            for ch in sorted(set(chain_descs) | set(chain_counts)):
                desc = chain_descs.get(ch, "")
                n = chain_counts.get(ch)
                size_tag = f" [{n} residues]" if n is not None else ""
                if ch in designable:
                    size_tag = (f" [{n} residues raw, {designable[ch]} designable "
                                f"after transmembrane stripping]")
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
                + ("\n**Judge the policy on the DESIGNABLE count where one is "
                   "given.** That chain is a membrane protein, and the pipeline's "
                   "own trim stage drops its transmembrane span and its "
                   "cytoplasmic face automatically before any design runs — a "
                   "binder against lipid-buried surface cannot work in a cell. "
                   "The raw length is not what will be designed against, so do "
                   "not return NO_GO on it, and do not recommend cropping the "
                   "structure by hand: that is what the next stage does. Pick "
                   "hotspots on the extracellular face.\n" if designable else "")
                + f"\nSelect the chains that form the biologically relevant "
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
            query += chain_hint + self._ppi_interface_options(pdb_id, target_complex)
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
            query += chain_hint + self._ppi_interface_options(pdb_id, target_complex)

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
        # What the pathway/literature stages actually settled on, captured
        # BEFORE this stage's handoff is allowed to overwrite it. Both verify
        # guards below are given THIS value: a stage that renames the complex
        # to whatever it happened to analyse must not get to validate its own
        # substitution (see _verify_partner_chain_is_requested).
        requested_complex = result.target_complex or target_complex
        delivered_complex = handoff.get("target_complex") or result.target_complex
        if (requested_complex and delivered_complex
                and delivered_complex.strip() != requested_complex.strip()):
            logger.warning(
                f"  ⚠ the structure stage renamed the target complex: "
                f"{requested_complex!r} -> {delivered_complex!r}. Verifying "
                f"chain assignment against the requested complex.")
        result.target_complex = delivered_complex
        # Persisted so later stages can read structure fields (the bridge
        # reads `target_chain`/`partner_chain` off it) after `handoff` has been
        # rebound.
        result.structure_handoff = handoff

        # Extract MODEL-READY HOTSPOTS as JSON so the bridge and the trim can
        # use them without re-reading the structure report.
        # Before parsing, auto-resolve any UNVERIFIED label_seq_id tokens via
        # gemmi (e.g. when the structure-tools tool_get_sequence_map call
        # failed inside the skill) and run a residue-name sanity check that
        # surfaces mouse/human numbering mismatches.
        hotspots_json = None
        # The numbering correction sits OUTSIDE the try below. It used to be
        # inside it, so any exception — including one raised by the correction
        # itself — was swallowed as "could not parse hotspot residues" and the
        # model's own counted label_seq_ids sailed through to the design spec.
        # A number that is a lookup in a file on disk must never degrade to a
        # log line.
        structure_text = output_file.read_text(encoding="utf-8")
        target_chain = (handoff.get("target_chain", "")
                        or handoff.get("chain_a", "")
                        or "A")
        structure_text = self._correct_label_seq_ids(
            structure_text, output_file, analysis_path, target_chain,
            handoff=handoff)
        try:
            hotspots_json = self._parse_hotspot_residues(structure_text, handoff)
            if hotspots_json:
                result.hotspot_residues_json = hotspots_json
                logger.info(f"  hotspot residues parsed: {len(json.loads(hotspots_json).get('residues', []))} residues")
        except Exception as exc:
            logger.warning(f"  could not parse hotspot residues from structure report: {exc}")

        # Same guards the binder track's interface stage runs after this
        # identical skill call (see CLAUDE.md's PD-L1 chain-swap / 8ZNL
        # hotspot-grounding incidents) — PPI's own structure stage never ran
        # them, leaving PPI-entered campaigns with no protection against the
        # exact failure modes that already burned a full binder-track
        # campaign once. Deliberately OUTSIDE the try/except above: a real
        # mismatch here must halt the run, not degrade to a warning the way a
        # missing hotspot table does.
        verify_pdb = result.pdb_id or pdb_id
        # Chain assignment needs only the handoff, NOT the hotspot table, so it
        # runs unconditionally. It used to sit behind `if hotspots_json:` — a
        # variable assigned inside the try above — so any parse failure there
        # was swallowed as a warning AND silently skipped both guards. That
        # defeated the stated intent: a chain swap produces real,
        # correctly-numbered residues on the WRONG protein, which is exactly
        # the failure that burned a full campaign, and it is most likely
        # precisely when the report is malformed enough to break parsing.
        self._verify_ppi_chain_assignment(
            requested_complex or result.target_complex, handoff, verify_pdb)
        # Separate question, separate guard: the one above asks "is the TARGET
        # chain one of the named proteins", which chain R (CALCRL) passes even
        # when the partner has been swapped for an unrelated molecule.
        self._verify_partner_chain_is_requested(
            requested_complex or result.target_complex, handoff, verify_pdb,
            source="The pathway and literature stages")

        # Grounding genuinely needs the hotspot table. If it's missing, say so
        # loudly rather than letting "no table" read as "table verified".
        if hotspots_json:
            self._verify_hotspot_grounding(hotspots_json, verify_pdb,
                                           self._ppi_target_uniprot)
            self._check_hotspot_atoms_are_buildable(hotspots_json, verify_pdb)
            self._check_ortholog_conservation(hotspots_json, verify_pdb, result)
        else:
            logger.warning(
                "  ⚠ hotspot grounding NOT verified — no parseable MODEL-READY "
                "HOTSPOTS table in the structure report. Residue names in any "
                "downstream spec are unchecked against the real structure.")

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
        # The pathway report this stage reads as context still recommends the
        # entry a switch replaced — `_note_structure_switch` appends the
        # correction, but it lands after the body, and the body names the old
        # one throughout. Say it in the instruction, where it cannot be missed.
        switch = getattr(result, "structure_switch", None)
        if switch:
            why = switch.get("reason") or "a cleaner entry for this interface"
            query += (
                f"\n\nIMPORTANT — the structure choice is already settled and is "
                f"not open: the pathway report you are given recommends PDB "
                f"{switch['from']}, but the pipeline designs against PDB "
                f"{switch['to']} ({why}). Refer to {switch['to']} in every "
                f"handoff field. You may still cite findings that came from "
                f"{switch['from']}, provided you attribute them to it.")

        logger.info("Stage 1: molecular-biology-expert")
        # Literature now runs BEFORE structure, so there is no structure report to
        # cross-reference. Pass the pathway report only — it has the disease/pathway
        # context the mol-bio queries need.
        pathway_ctx = [f for f in [result.stage_files.get("pathway")] if f and f.exists()]
        handoff = self._run_stage("molecular-biology-expert", query, pathway_ctx, output_file,
                                  stage="literature")
        # Prompt guidance is best-effort; this is not. `design_query` is read
        # by the bridge and by whatever reads this report afterwards, so a
        # stale PDB id in it names an entry the run is not using.
        if switch:
            self._retarget_stale_structure(handoff, switch["from"], switch["to"])
        result.stages_completed.append("literature")
        result.stage_files["literature"] = output_file
        result.literature_handoff = handoff
        return handoff

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
        # A per-stage override may name a different PROVIDER as "gemini:model",
        # which is how ONE stage gets routed elsewhere without moving the whole
        # pipeline — now the only supported way to change a stage's model, since
        # a refusal no longer picks one automatically.
        #
        # `_split_model_spec`, not a bare split: a prefixed id like
        # `gpt-5.6-terra` written without its provider would otherwise inherit
        # the current one and be POSTed to the wrong endpoint, which 404s.
        provider, model_id = _split_model_spec(model_id, self.provider)
        use_thinking = use_thinking and provider == "claude"

        if use_thinking and "haiku" in model_id.lower():
            upgrade = self._models_cfg.get("thinking_upgrade") or _THINKING_UPGRADE_MODEL
            logger.warning(
                f"Extended thinking requires a Sonnet-class model — auto-upgrading "
                f"{stage} stage from {model_id} to {upgrade}"
            )
            model_id = upgrade
        return model_id, use_thinking, provider

    #: Heading both HTML reports look for. Changing it means changing
    #: `report_common.extract_provenance_section` too.
    _PROVENANCE_HEADING = "## MODEL PROVENANCE"

    def _stage_provenance_note(self, skill_name: str, provider: str,
                               model: str) -> str:
        """A "who wrote this" block for every stage report.

        There is no "declined first" case to report here: a refusal is
        terminal, so a declined stage writes no report at all and the refusal
        is recorded in the manifest instead (`_record_refusal`). What this
        block is for is the other half of the same question — every stage
        naming the model behind it, so "which model produced this claim" is
        answerable from the artifact that circulates rather than from a log.
        """
        return "\n".join([
            "", "", self._PROVENANCE_HEADING, "",
            f"- skill: `{skill_name}`",
            f"- written by: **{provider}:{model}** — the model the operator "
            f"selected. LPT never substitutes a model on its own; a stage a "
            f"safety classifier declines ends the run.",
        ]) + "\n"

    def _record_refusal(self, stage: str, skill: str,
                        refusal: SkillRefusedError) -> None:
        """Put a refusal in the manifest, not only in the log.

        This is the whole audit trail for a run that a classifier stopped: the
        stage report was never written, the process is about to exit, and the
        manifest is the only account that survives either. A later `--start-from
        {stage}` on a different model can then be read against the record of
        why the first attempt ended.
        """
        self._binder_checkpoint(
            f"refusal:{stage}", stage, "gate",
            {"skill": skill,
             "declined_by": refusal.model,
             "category": refusal.category,
             "iteration": refusal.iteration,
             "automatic_fallback": False,
             "note": ("The run stopped. LPT does not retry a refused stage on "
                      "another model — selecting one is the operator's "
                      "decision, taken after reading the refusal. See "
                      "docs/responsible-use.md.")})

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
                runner, query, context_text, model_id, provider)
            projected_usd = price(model_id, estimate)
            try:
                self._ledger.preflight(stage=stage_key, model=model_id,
                                       estimated=estimate)
            except BudgetExceeded as exc:
                self._budget_pause(exc)
        try:
            try:
                output_text = runner.run(query, context_text=context_text)
            except SkillRefusedError as refusal:
                # A REFUSAL IS TERMINAL. There is no automatic fallback, by
                # design, and this is the second time that boundary moved: the
                # chain first stopped continuing down to a smaller model, and
                # now it does not retry at all.
                #
                # The reasoning that killed the last rung applies to every
                # rung. Any automatic retry is the pipeline deciding, on its
                # own, to go looking for a model that will produce content the
                # operator's chosen model declined to produce — and no reader
                # of the output can distinguish that from a legitimate
                # workaround for a miscalibrated classifier. Those refusals ARE
                # often miscalibrated for structural-biology analysis, which is
                # exactly why the judgement belongs to a person who has read
                # the refusal and can say why this target is legitimate, rather
                # than to a `for` loop that cannot.
                #
                # So: record it where it survives the process, tell the
                # operator precisely how to make that decision themselves, and
                # raise.
                self._record_refusal(stage_key, skill_name, refusal)
                logger.error(
                    f"  [{skill_name}] {provider}:{model_id} declined this "
                    f"stage (category {refusal.category or 'unspecified'}, "
                    f"call #{refusal.iteration}). THE RUN STOPS HERE — "
                    f"LPT does not automatically retry on another model.\n"
                    f"    Read the refusal and judge for yourself whether "
                    f"this target is legitimate work — you are accountable "
                    f"for that call, and if two frontier models decline it, "
                    f"treat that as a result.\n"
                    f"    To steer the run yourself:\n"
                    f"      --provider <claude|gemini|openai>      (whole run)\n"
                    f"      models.{provider}.stages.{stage_key}: "
                    f"\"<provider>:<model>\"   (this stage only, config.yaml)\n"
                    f"    Resume where you left off with "
                    f"--start-from {stage_key}. Do not reword the prompt to "
                    f"get past the classifier — see docs/responsible-use.md.")
                raise
        finally:
            if self._ledger is not None:
                entry = self._ledger.record(
                    # `provider`, not `self.provider`: a per-stage
                    # `models.<provider>.stages` override can name another
                    # provider, and billing the run's default for a call that
                    # one actually served makes per-provider spend wrong.
                    stage=stage_key, skill=skill_name, provider=provider,
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

        # Model provenance goes in BEFORE the citation note, not after:
        # `report_common.extract_citation_section` matches "## CITATION
        # VERIFICATION" through to end-of-file, so anything appended after it
        # is rendered inside the citation block in both HTML reports.
        output_text = output_text + self._stage_provenance_note(
            skill_name, provider, model_id)
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
        provider: str = "claude",
    ) -> Usage:
        """
        Project a stage's token usage for the pre-flight budget check.

        Call 1 pays full price for the system prompt (and writes it to cache);
        calls 2..n read it back at ~0.1x while the message history grows. This
        is a guard, not accounting — any failure degrades to a rough character
        heuristic rather than blocking the run.

        The counter is provider-specific. Anthropic's `count_tokens` 404s on a
        non-Claude model id, and the default provider is Gemini, so EVERY stage
        used to pay a wasted round trip and log a scary "count_tokens
        unavailable (404 ... model: gemini-3.7-flash)" before landing on the
        heuristic anyway. Ask only the provider that can answer.
        """
        n_calls = self._STAGE_CALL_PRIOR.get(runner.skill_name, 8)
        first_input = context_text and len(context_text) or 0
        # ~4 chars/token is close enough for a ceiling check.
        heuristic = (len(runner.system_prompt) + first_input + len(query)) // 4
        system_tokens = heuristic
        if provider == "claude":
            try:
                import anthropic

                client = anthropic.Anthropic()
                system_tokens = client.messages.count_tokens(
                    model=model_id,
                    system=[{"type": "text", "text": runner.system_prompt}],
                    messages=[{"role": "user",
                               "content": (context_text or "") + query}],
                ).input_tokens
            except Exception as exc:
                system_tokens = heuristic
                logger.debug(f"count_tokens unavailable ({exc}); using char heuristic")
        else:
            logger.debug(
                f"no token counter for provider {provider!r}; using char heuristic")

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

    @staticmethod
    def _target_chain_residue_count(structure: Path, chain: str) -> int:
        """
        Designable residues in the target chain, or 0 if it cannot be read.

        Counted the way `structure_trim` counts them — anything carrying an
        N/CA/C backbone, whatever the residue is called — so the number
        compared against the budget here is the same number the trim compares
        against it.
        """
        try:
            import gemmi

            from src.structure_tools import is_chain_residue

            st = gemmi.read_structure(str(structure))
            for ch in st[0]:
                if ch.name == chain:
                    return sum(1 for r in ch if is_chain_residue(r))
        except Exception as exc:
            logger.debug(f"could not count residues in chain {chain}: {exc}")
        return 0

    @staticmethod
    def _local_structure_stem(pdb_id: str) -> str:
        """The stem behind a `LOCAL-<stem>` pseudo-id, or "".

        Same trick as `AF-<accession>`: a real PDB accession is four
        characters, so a prefixed id cannot collide with one, and every lookup
        that wants RCSB metadata for it (entry_metadata, uniprot_to_auth,
        BA1 download) already fails open. That is what lets an operator's own
        file travel through a stage machine built around accessions.
        """
        raw = (pdb_id or "").strip().upper()
        return raw[len(_LOCAL_PREFIX):] if raw.startswith(_LOCAL_PREFIX) else ""

    def ingest_local_structure(self, path: Path) -> str:
        """Copy an operator's structure into `data/structures/` and name it.

        Returns the `LOCAL-<stem>` pseudo-id every later stage addresses it by.
        A `.pdb` is converted to mmCIF on the way in, because the whole
        pipeline builds paths as `<structures_dir>/<ID>.cif` and reads them
        with gemmi.
        """
        import gemmi

        src = Path(path).expanduser().resolve()
        if not src.is_file():
            raise PipelineBlockedError(f"--structure {src} does not exist")
        structures_dir = _ROOT / (self.config.get("paths") or {}).get(
            "structures_dir", "data/structures")
        structures_dir.mkdir(parents=True, exist_ok=True)

        stem = re.sub(r"[^A-Za-z0-9]+", "_", src.stem).strip("_").upper()[:40]
        if not stem:
            raise PipelineBlockedError(f"cannot derive a name from {src.name}")
        pdb_id = f"{_LOCAL_PREFIX}{stem}"
        dest = structures_dir / f"{pdb_id}.cif"

        try:
            st = gemmi.read_structure(str(src))
        except Exception as exc:                       # noqa: BLE001
            raise PipelineBlockedError(
                f"could not read {src.name} as a structure: {exc}") from exc
        st.setup_entities()
        if not any(len(ch) for model in st for ch in model):
            raise PipelineBlockedError(f"{src.name} contains no chains")
        st.make_mmcif_document().write_file(str(dest))
        logger.info(f"  local structure {src.name} -> {dest.name}")
        return pdb_id

    @staticmethod
    def _alphafold_accession(pdb_id: str) -> str:
        """
        The UniProt accession behind an `AF-<acc>` / `AF:<acc>` pseudo-id, or
        "". A real PDB accession is four characters, so there is no collision.
        """
        raw = (pdb_id or "").strip().upper()
        for prefix in ("AF-", "AF:", "ALPHAFOLD:"):
            if raw.startswith(prefix):
                acc = raw[len(prefix):].split("-", 1)[0]
                return acc if acc.isalnum() else ""
        return ""

    def _ensure_structure(self, pdb_id: str) -> Path:
        """Return local ASU CIF path, downloading from RCSB if absent.

        Also attempts to download biological assembly 1 ({PDB_ID}_ba1.cif) which
        is used by the structure stage to avoid crystal-contact confusion.
        """
        structures_dir = _ROOT / self.config.get("paths", {}).get("structures_dir", "data/structures")

        # `AF-<accession>` is not an RCSB id — it is the AlphaFold DB model for
        # a UniProt accession, and it exists so a campaign can be pointed at
        # the HUMAN protein when the only experimental structure is an
        # ortholog's. There is no biological assembly and no RCSB metadata for
        # one; every downstream lookup that would want them (entry_metadata,
        # uniprot_to_auth) already fails open, so a predicted monomer degrades
        # to "no restriction" rather than breaking.
        # An operator's own file is already where it needs to be. Falling
        # through would try to download `LOCAL-MYTARGET` from RCSB and fail the
        # run on a 404 for a structure that is sitting on disk.
        if self._local_structure_stem(pdb_id):
            dest = structures_dir / f"{pdb_id.upper()}.cif"
            if not dest.exists():
                raise PipelineError(
                    f"{pdb_id} is a local structure but {dest} is missing — "
                    f"re-run with --structure pointing at the file.")
            return dest

        af_acc = self._alphafold_accession(pdb_id)
        if af_acc:
            from src.ortholog_check import fetch_alphafold_model

            path = fetch_alphafold_model(af_acc, structures_dir)
            if path is None:
                raise PipelineError(
                    f"AlphaFold DB has no model for {af_acc}. Give a PDB "
                    f"accession instead, or check the accession is a UniProt "
                    f"entry AFDB covers.")
            # Land it under the id the caller used, not AFDB's own filename:
            # every later path in the pipeline is built as
            # `<structures_dir>/<PDB_ID>.cif` (and `_ba1.cif`), so a file named
            # AF-Q12770-F1.cif would be fetched and then never found again.
            dest = structures_dir / f"{pdb_id.upper()}.cif"
            if path != dest:
                dest.write_bytes(path.read_bytes())
            logger.info(f"  AlphaFold model for {af_acc}: {dest}")
            return dest

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
    def _build_label_seq_id_map(
            cif_path: Path, chain_id: str) -> dict[int, tuple[int | None, str]]:
        """Return ``{auth_seq_id: (label_seq_id, residue_name_3letter)}`` for one chain.

        Uses gemmi; returns empty dict on failure or missing chain. The
        residue name is included so a caller can sanity-check that a
        literature-claimed residue (e.g. ``THR238``) actually exists in the
        structure at that auth_seq_id — protecting against mouse/human
        numbering mismatches and similar errors that wouldn't be caught by
        just blindly resolving label_seq_id.

        ``label_seq_id`` is ``None`` when the structure does not carry one.
        That is not rare and not an error: label_seq is an mmCIF concept, so
        EVERY residue of a PDB-format file has None — including
        ``trim/trimmed.pdb``, which this pipeline writes itself and hands to
        RFD3 — and a real mmCIF still has None on het rows (5 of 227 on
        5GN0).

        This used to fall back to the residue's 1-indexed position in the
        chain, which is exactly the counting the caller exists to catch,
        wearing a lab coat: on a PDB input it would replace the model's
        counted numbers with different counted numbers and report the column
        verified. Returning None makes "the file does not say" a distinct
        answer from "the file says N", which is the only way the caller can
        decline to fabricate.
        """
        try:
            import gemmi  # type: ignore
            st = gemmi.read_structure(str(cif_path))
            if len(st) == 0:
                return {}
            for chain in st[0]:
                if chain.name != chain_id:
                    continue
                mapping: dict[int, tuple[int | None, str]] = {}
                for res in chain:
                    auth = int(res.seqid.num)
                    label = res.label_seq
                    mapping[auth] = (
                        int(label) if label is not None else None,
                        res.name.upper(),
                    )
                return mapping
            return {}
        except Exception as exc:
            logger.warning(f"Could not build auth→label map for {chain_id}@{cif_path}: {exc}")
            return {}

    def _correct_label_seq_ids(self, text: str, report_path: Path,
                               cif_path: Path, target_chain: str, *,
                               handoff: dict | None = None) -> str:
        """
        Overwrite the report's label_seq_id column with the structure's own
        values, persist the corrected report, and surface what changed.

        Both tracks call this — the PPI `structure` stage and the binder
        `interface` stage run the SAME skill and produce the SAME table, so a
        correction that only one of them applied was a track-parity gap of
        exactly the shape CLAUDE.md documents for the PD-L1 verify guards.
        """
        fixed, warns = self._resolve_unverified_label_seq_ids(
            text, cif_path, target_chain, handoff=handoff)
        if fixed != text:
            report_path.write_text(fixed, encoding="utf-8")
            logger.info("  label_seq_ids replaced with the structure's own "
                        "(gemmi auth→label map)")
        for w in warns:
            logger.warning(f"  ⚠ {w}")
        return fixed

    def _resolve_unverified_label_seq_ids(
        self,
        structure_text: str,
        cif_path: Path,
        target_chain: str,
        *,
        handoff: dict | None = None,
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
        has_table = bool(re.search(r"###\s*MODEL.READY HOTSPOTS", structure_text,
                                   re.IGNORECASE))

        # Which chain does the row at character offset N belong to?
        #
        # Built ONLY over `### MODEL-READY HOTSPOTS` sections, and that
        # restriction is load-bearing rather than tidy: the COMPLEX OVERVIEW
        # section carries lines like `Chain A: Hydrophobic: ...` that match
        # `_CHAIN_HEADING` perfectly well, so an index over the whole document
        # would start attributing rows from a prose heading that is not a
        # table heading at all. Every offset outside a hotspot section maps to
        # `target_chain`, which is today's behaviour exactly.
        #
        # `chain_blocks` is imported rather than re-implemented — it is the
        # same split `parse_hotspot_residues` uses, and CLAUDE.md's
        # "Common file pairs" names this pair for exactly this reason.
        from src.handoff import chain_blocks as _chain_blocks
        from src.handoff import _resolve_heading_chain as _resolve_chain

        spans: list[tuple[int, int, str]] = []
        for sec in re.finditer(
                r"###\s+MODEL.READY HOTSPOTS.*?(?=\n###|\Z)",
                structure_text, re.DOTALL | re.IGNORECASE):
            base = sec.start()
            cursor = 0
            for c, block in _chain_blocks(sec.group(0), handoff or {},
                                          target_chain):
                start = sec.group(0).index(block, cursor)
                spans.append((base + start, base + start + len(block), c))
                cursor = start + len(block)

        def _chain_at(offset: int) -> str:
            for lo, hi, c in spans:
                if lo <= offset < hi:
                    return c
            return target_chain

        _map_cache: dict[str, dict[int, tuple[int | None, str]]] = {}

        def _map_for(c: str) -> dict[int, tuple[int | None, str]]:
            if c not in _map_cache:
                _map_cache[c] = self._build_label_seq_id_map(cif_path, c)
                if not _map_cache[c] and c != target_chain:
                    if handoff:
                        # Same refusal as the target chain's, for the same
                        # reason: a label_seq that cannot be read must never
                        # be replaced by one the model counted, and BoltzGen
                        # consumes `binding:` as label_seq.
                        raise PipelineError(
                            f"cannot build the auth->label map for chain {c} "
                            f"in {cif_path.name}, so the MODEL-READY HOTSPOTS "
                            f"table's label_seq_id column cannot be verified "
                            f"against the structure. Those values are derived "
                            f"by the model, not read from the file, and "
                            f"BoltzGen consumes them as label_seq. Check the "
                            f"chain id and that the structure is on disk.")
                    # No handoff: the chain came from a heading token with
                    # nothing to resolve it against, so it is a GUESS, and
                    # refusing on a guess turns a missing argument into a
                    # failed run. Leave those rows exactly as the old
                    # single-chain code left them and say so. The real
                    # diagnosis is `_verify_hotspot_grounding`'s, which knows
                    # which chain each row claims.
                    warnings.append(
                        f"chain {c} was inferred from a table heading with no "
                        f"handoff to resolve it against, and is not in "
                        f"{cif_path.name}; its rows were left uncorrected. "
                        f"Pass `handoff=` to resolve the chain properly.")
            return _map_cache[c]

        auth_to_label_and_name = self._build_label_seq_id_map(cif_path, target_chain)
        _map_cache[target_chain] = auth_to_label_and_name
        if not auth_to_label_and_name:
            if has_table:
                # label_seq_id is not a judgement call, it is a lookup in a file
                # already on disk. If that lookup is impossible we cannot ship
                # the LLM's own number in its place: it is derived by counting,
                # and BoltzGen reads `binding:` as label_seq, so a wrong value
                # constrains the binder to the wrong residues — the documented
                # cause of zero hotspot occlusion on the YAP-TEAD run. Fail
                # loudly rather than pass through unverified numbers.
                raise PipelineError(
                    f"cannot build the auth->label map for chain {target_chain} "
                    f"in {cif_path.name}, so the MODEL-READY HOTSPOTS table's "
                    f"label_seq_id column cannot be verified against the "
                    f"structure. Those values are derived by the model, not "
                    f"read from the file, and BoltzGen consumes them as "
                    f"label_seq. Check the chain id and that the structure is "
                    f"on disk.")
            logger.warning(
                f"  cannot build gemmi auth→label map for {target_chain}@{cif_path.name} "
                f"— no hotspot table to correct"
            )
            return structure_text, warnings

        # Walk every table row, validate residue name, compare label_seq_id
        # against gemmi truth, and substitute when needed. Match rows in
        # `| NAME[digits] | auth | label_or_token | ... |` form.
        row_pat = re.compile(
            r"(\|\s*([A-Z]{3})\d*\s*\|\s*(\d+)\s*\|)\s*([^|]*?)\s*\|",
            re.MULTILINE,
        )
        seen: set[tuple[str, str, int]] = set()
        substitutions = 0
        filled = 0
        rows_seen = 0
        offsets: list[int] = []
        unavailable: list[int] = []
        # What BoltzGen would call these residues in THIS file. Only consulted
        # when the file itself has no label_seq to read.
        _bg_cache: dict[str, dict] = {}

        def _bg_for(c: str) -> dict:
            if c not in _bg_cache:
                try:
                    from src.structure_tools import boltzgen_residue_indices
                    _bg_cache[c] = boltzgen_residue_indices(str(cif_path), c)
                except Exception as exc:
                    logger.debug(f"could not compute BoltzGen indices: {exc}")
                    _bg_cache[c] = {}
            return _bg_cache[c]

        bg_index = _bg_for(target_chain)

        def _row_sub(match: re.Match) -> str:
            nonlocal substitutions, filled, rows_seen
            rows_seen += 1
            prefix = match.group(1)
            expected_name = match.group(2)
            auth_s = int(match.group(3))
            llm_label_raw = match.group(4).strip()

            # The chain this row was written under, not the declared target.
            # A glue table's co-target rows are real residues on their OWN
            # chain; resolving them against the target's map writes a label
            # that is precisely wrong rather than merely unverified.
            c = _chain_at(match.start())
            chain_map = _map_for(c)
            if not chain_map and c != target_chain:
                return match.group(0)  # unresolvable guessed chain, see _map_for
            bg_index = _bg_for(c)

            # Residue-name sanity (catches mouse↔human numbering offsets etc.)
            actual = chain_map.get(auth_s)
            if actual is None:
                key = (c, expected_name, auth_s)
                if key not in seen:
                    seen.add(key)
                    warnings.append(
                        f"residue at chain {c} auth_seq_id {auth_s} "
                        f"({expected_name}) not present in structure"
                    )
                return match.group(0)  # leave row unchanged (can't fix)

            true_label, actual_name = actual
            if actual_name.upper() != expected_name.upper():
                key = (c, expected_name, auth_s)
                if key not in seen:
                    seen.add(key)
                    warnings.append(
                        f"residue NAME mismatch at chain {c} "
                        f"auth_seq_id {auth_s}: report says {expected_name} but "
                        f"structure has {actual_name} — likely a numbering "
                        f"offset, or a row belonging to another chain; "
                        f"label_seq_id left as written"
                    )
                # Leave the row alone. The map we are holding describes a
                # DIFFERENT residue at this auth id, so substituting its label
                # writes a number that is precisely wrong rather than merely
                # unverified — and this function always overwrites, so it did.
                # Observed on `div_standard_diabetes`' 5VAI glue table: four
                # chain-P rows (ALA30/GLY35/ARG36/GLY37, correct for chain P,
                # labels 24/29/30/31) were rewritten with chain R's labels at
                # those same auth ids (69/74/75/76), which address VAL/THR/
                # VAL/GLN on the other protein. The names had already
                # mismatched, so the evidence that the rewrite was wrong was
                # in hand at the moment it happened. BoltzGen reads this
                # column as label_seq, which makes it the YAP-TEAD
                # zero-occlusion failure mode with a different cause.
                #
                # The caller's `_verify_hotspot_grounding` hard-fails on a
                # name mismatch, so on the normal path this row never reaches
                # a spec either way — this keeps the report on disk honest for
                # the paths that fail open, and for a human reading it.
                return match.group(0)

            # Compare LLM value to the structure's own, and emit the
            # structure's in the rewritten cell.
            try:
                llm_label = int(llm_label_raw)
            except ValueError:
                llm_label = None

            if true_label is None:
                # The file carries no label_seq for this residue (every
                # residue of a PDB-format file). That does NOT mean the number
                # is unknowable: BoltzGen synthesises one itself, and
                # `boltzgen_residue_indices` reproduces that algorithm, so the
                # right value is computed rather than left to the model — or
                # to a marker the design script would have to work around.
                bg = bg_index.get(auth_s)
                unavailable.append(auth_s)
                if bg is None:
                    if llm_label is not None:
                        substitutions += 1
                    return f"{prefix} {_LABEL_SEQ_UNAVAILABLE} |"
                if llm_label != bg:
                    if llm_label is None:
                        filled += 1
                    else:
                        substitutions += 1
                return f"{prefix} {bg} |"

            if llm_label != true_label:
                if llm_label is None:
                    filled += 1
                else:
                    substitutions += 1
                if llm_label is not None:
                    offsets.append(true_label - llm_label)
                    warnings.append(
                        f"label_seq_id correction at {expected_name}{auth_s}: "
                        f"LLM said {llm_label}, gemmi says {true_label} "
                        f"(BoltzGen `binding:` field uses label_seq)"
                    )
            return f"{prefix} {true_label} |"

        new_text = row_pat.sub(_row_sub, structure_text)

        if has_table and rows_seen == 0:
            # A table is present and not one row matched the row pattern, so
            # nothing was checked and nothing was corrected — silently. This is
            # the failure mode that left `_verify_hotspot_grounding` inert for
            # months: a guard that runs, finds nothing to do, and says so to
            # no one. The table format lives in a SKILL.md that gets edited.
            raise PipelineError(
                "the MODEL-READY HOTSPOTS table did not match the expected "
                "`| RES | auth_seq_id | label_seq_id | ... |` row format, so no "
                "label_seq_id could be verified against the structure. The "
                "skill's table format and this parser have drifted apart — fix "
                "one to match the other rather than shipping unverified "
                "numbering to the design spec.")

        if unavailable:
            resolved = sum(1 for a in unavailable if a in bg_index)
            warnings.append(
                f"{cif_path.name} carries no label_seq for {len(unavailable)} "
                f"of {rows_seen} hotspot row(s) — normal, since label_seq is an "
                f"mmCIF concept and a PDB-format file has none. "
                + (f"{resolved} were filled in with the index BoltzGen itself "
                   f"would assign to this file (1-based position among the "
                   f"chain's modelled polymer residues, per its pdb_parser), "
                   f"not with a number the model derived. "
                   if resolved else "")
                + f"These indices are valid for {cif_path.name} ONLY: the same "
                  f"residue can carry a different label_seq in a different "
                  f"file, so a BoltzGen `binding:` list must be recomputed "
                  f"against whatever path ends up in its yaml.")

        if offsets and len(offsets) >= 3 and len(set(offsets)) == 1:
            # Every disagreement identical means the column was DERIVED, not
            # read: the model counted positions instead of looking up
            # `auth_to_label`, and a constant offset is that signature. Worth
            # separating from the scattered single-residue case, because it
            # says something about the rest of the report — a model that
            # fabricated a whole column may have fabricated more than this one.
            warnings.append(
                f"SYSTEMATIC label_seq_id offset: all {len(offsets)} corrected "
                f"rows were off by exactly {offsets[0]:+d}. That is the "
                f"signature of the model COUNTING residue positions rather "
                f"than reading `auth_to_label` from tool_get_sequence_map. The "
                f"numbers have been replaced with the structure's own, but "
                f"treat other derived values in this report with suspicion.")

        # Rewrite the BoltzGen `binding:` line in each MODEL-READY HOTSPOTS
        # section independently. The structure-expert may produce one or
        # more sections (multi-region designs); each has its own table and
        # its own `binding:` line. Combining them into a global list (the
        # original bug) would corrupt multi-region runs.
        section_pat = re.compile(
            r"(### MODEL.READY HOTSPOTS[^\n]*\n.*?)(?=\n### MODEL.READY HOTSPOTS|\n## |\Z)",
            re.DOTALL | re.IGNORECASE,
        )

        def _label_ids_in(block: str) -> list[int]:
            """The numeric label_seq ids this block's table rows carry, in order."""
            out: list[int] = []
            for m in re.finditer(
                    r"\|\s*([A-Z]{3})\d*\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", block):
                try:
                    out.append(int(m.group(3)))
                except ValueError:
                    pass
            return out

        def _section_sub(sec_match: re.Match) -> str:
            section = sec_match.group(1)

            # A glue section carries one `Chain <id> binding:` line per chain,
            # and each must be rewritten from ITS OWN rows.
            #
            # Note what is NOT done here: splitting the section by chain and
            # running the single-chain body per block. The `binding:` lines
            # live in a `#### BoltzGen binding` subsection that carries no
            # table rows, so every binding-carrying block would have zero
            # local residues and take the "structure carries no label_seq"
            # branch — REPLACING `Chain B binding: 24,29,30,31` with a
            # statement, i.e. deleting the value. Measured on the real 5VAI
            # report: all four binding-carrying blocks come out with
            # local_residues == []. Today's code is inert on these lines only
            # because its pattern is `^\s*binding:` and they start with
            # `Chain`, which is also why a glue report currently ships the
            # model's own counted numbers to BoltzGen.
            blocks = [(c, b) for c, b in
                      _chain_blocks(section, handoff or {}, target_chain)]
            # ACCUMULATE, never assign: a chain contributes several blocks
            # here (its table, then its `Chain <id> binding:` lines, which
            # carry no rows), so a dict comprehension is last-wins and the
            # row-less block silently empties the chain's real list.
            per_chain: dict[str, list[int]] = {}
            for c, b in blocks:
                ids = _label_ids_in(b)
                if ids:
                    per_chain.setdefault(c, []).extend(ids)
            if len(per_chain) > 1:
                def _binding_sub(m: re.Match) -> str:
                    # The line's own chain token, resolved the SAME way the
                    # table headings are. `div_standard_diabetes` writes
                    # `Chain A binding:` where the chain is really R, so a
                    # literal lookup silently matches nothing and leaves the
                    # model's counted numbers standing — which is the whole
                    # failure this rewrite exists to prevent. Measured there:
                    # the model wrote 38,39,42 for residues whose real label
                    # ids are 105,106,109.
                    ids = per_chain.get(
                        _resolve_chain(m.group(1), handoff or {}, target_chain))
                    if not ids:
                        return m.group(0)
                    dedup: list[int] = []
                    for x in ids:
                        if x not in dedup:
                            dedup.append(x)
                    only = "only: " if m.group(2) else ""
                    return (f"Chain {m.group(1)} {only}binding: "
                            + ",".join(str(x) for x in dedup))

                out = re.sub(
                    r"^[ \t]*Chain\s+(\S+)\s+(only:\s*)?binding:[^\n]*$",
                    _binding_sub, section, flags=re.MULTILINE)
                if re.search(r"^[ \t]*binding:", out, re.MULTILINE):
                    # A chain-less `binding:` line inside a multi-chain
                    # section means the pooled list, which is what it means
                    # when nothing says otherwise. Left alone, and said so.
                    warnings.append(
                        "a multi-chain MODEL-READY HOTSPOTS section carries a "
                        "chain-less `binding:` line; it has been left as the "
                        "pooled list. BoltzGen reads `binding:` as label_seq "
                        "for ONE chain, so name the chain "
                        "(`Chain <id> binding: ...`) if that is not what was "
                        "meant.")
                return out

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
                # No row yielded a numeric label_seq — the structure has none
                # (a PDB-format file never does). Returning the section
                # unchanged would leave the model's own counted `binding:`
                # list standing, which is the one thing that must not happen:
                # BoltzGen reads it AS label_seq. Replace it with a statement.
                if re.search(r"^\s*binding:", section, re.MULTILINE):
                    return re.sub(
                        r"^\s*binding:\s*[^\n]+",
                        f"binding: {_LABEL_SEQ_UNAVAILABLE} — this structure "
                        f"carries no label_seq; use auth_seq_id (RFD3 "
                        f"`select_hotspots`), not this line",
                        section, flags=re.MULTILINE)
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

        # Two different things, and conflating them cost a reader real time:
        # a residue whose cell was `**UNVERIFIED**` being FILLED is the skill
        # (or the --hotspots writer) behaving exactly as instructed, while a
        # stated number being CORRECTED means something derived a label_seq_id
        # by counting. Only the second is a red flag.
        if filled:
            logger.info(
                f"  label_seq_id: {filled} residue(s) were UNVERIFIED and were "
                f"filled from the CIF's own auth->label map — expected")
        if substitutions:
            logger.warning(
                f"  label_seq_id corrections: {substitutions} residue(s) "
                f"STATED a label_seq that disagrees with gemmi; rewritten from "
                f"CIF ground truth. A stated-but-wrong label_seq means "
                f"something counted instead of looking it up.")

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
