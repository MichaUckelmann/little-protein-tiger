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

from src.skill_runner import SkillRunner

_DEFAULT_MODELS = {
    "claude": "claude-sonnet-4-6",
    "gemini": "gemini-3.1-flash-lite-preview",
}

# Maps stage name → skill name (inverse of tasks.py _SKILL_TO_STAGE)
_STAGE_TO_SKILL: dict[str, str] = {
    "pathway":    "pathway-expert",
    "structure":  "complex-structure-analysis",
    "literature": "molecular-biology-expert",
    "design":     "protein-design-script",
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
    # Populated after stage 1; None if MODEL-READY HOTSPOTS section was not found.
    hotspot_residues_json: str | None = None


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

    STAGE_ORDER = ["pathway", "structure", "literature", "design"]

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
        """
        if pdb_id and start_from == "pathway":
            start_from = "structure"

        safe_slug = re.sub(r"[^a-zA-Z0-9]+", "_", query[:40]).strip("_").lower()
        run_dir = self._output_dir_override or (
            _ROOT / "outputs" / f"{safe_slug}_{date.today().isoformat()}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Pipeline run dir: {run_dir}")

        result = PipelineResult(run_dir=run_dir, pdb_id=pdb_id)
        handoff: dict[str, str] = {}

        # Seed handoff from a pre-existing context file when resuming
        if context_file and context_file.exists():
            handoff = self._parse_handoff(context_file.read_text(encoding="utf-8"))
            result.pdb_id = result.pdb_id or handoff.get("pdb_id")
            result.target_complex = result.target_complex or handoff.get("target_complex")

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

            # ── Stage 0.5: ensure structure on disk ──────────────────────────
            if start_idx <= 1:
                pdb = result.pdb_id or handoff.get("pdb_id", "")
                if not pdb or pdb.upper() == "NOT_FOUND":
                    raise PipelinePausedError(
                        "structure_needed",
                        {
                            "target_complex": result.target_complex or handoff.get("target_complex", ""),
                            "message": (
                                "The pathway analysis could not find a PDB structure in the corpus "
                                "for the recommended target. Provide a 4-character PDB accession "
                                "or upload a .cif file to continue."
                            ),
                        },
                    )
                result.pdb_id = pdb
                self._ensure_structure(pdb)

            # ── Stage 1: complex-structure-analysis ──────────────────────────
            if start_idx <= 1:
                ctx = [f for f in [result.stage_files.get("pathway"), context_file] if f and f.exists()]
                handoff = self._stage_structure(handoff, run_dir, result, ctx)

                # ── Pause point 2: let the user choose the next step ─────────
                if not auto_mode and structure_next_step is None:
                    raise PipelinePausedError("structure_choice", {
                        "tractability": handoff.get("tractability"),
                        "modality":     handoff.get("modality"),
                        "bsa_A2":       handoff.get("bsa_A2"),
                        "target_complex": result.target_complex,
                    })

            # ── Stage 2: molecular-biology-expert ────────────────────────────
            # Skipped when the user chose "design_only" at the structure pause.
            _run_literature = structure_next_step in (None, "literature_and_design")
            if start_idx <= 2 and _run_literature:
                ctx = [
                    f for f in [
                        result.stage_files.get("pathway"),
                        result.stage_files.get("structure"),
                        context_file,
                    ]
                    if f and f.exists()
                ]
                handoff = self._stage_literature(handoff, run_dir, result, ctx)

                # ── Pause point 3: review literature before committing to design ──
                if not auto_mode:
                    go_prelim = handoff.get("go_recommendation", "").upper().replace("-", "_")
                    raise PipelinePausedError("literature_choice", {
                        "go_recommendation": go_prelim,
                        "go_rationale": handoff.get("go_rationale", ""),
                    })

            # ── Stage 3: go/no-go ────────────────────────────────────────────
            go = handoff.get("go_recommendation", "").upper().replace("-", "_")
            result.go_recommendation = go or "INCOMPLETE"
            result.go_rationale = handoff.get("go_rationale", "")

            if go == "NO_GO":
                logger.info(f"Decision: NO_GO — {result.go_rationale}")
                self._write_no_go_report(result, handoff)
                return result
            if go == "CONDITIONAL_GO":
                logger.warning(f"Decision: CONDITIONAL_GO — {result.go_rationale}")
            elif go == "GO":
                logger.info(f"Decision: GO — {result.go_rationale}")
            else:
                logger.warning("go_recommendation not in mol-bio handoff — proceeding to design anyway")

            # ── Stage 4: protein-design-script ───────────────────────────────
            if start_idx <= 3:
                ctx = [
                    f for f in [
                        result.stage_files.get("structure"),
                        result.stage_files.get("literature"),
                        context_file,
                    ]
                    if f and f.exists()
                ]
                self._stage_design(handoff, run_dir, result, ctx)

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
        logger.info("Stage 0: pathway-expert")
        handoff = self._run_stage("pathway-expert", query, [], output_file)
        result.stages_completed.append("pathway")
        result.stage_files["pathway"] = output_file
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
        output_file = run_dir / "01_structure.md"
        pdb_id = result.pdb_id or prev_handoff.get("pdb_id", "")
        structures_dir = _ROOT / self.config.get("paths", {}).get("structures_dir", "data/structures")
        asu_path = structures_dir / f"{pdb_id.upper()}.cif"
        ba1_path = structures_dir / f"{pdb_id.upper()}_ba1.cif"

        # Prefer biological assembly 1: it contains only the physiological complex,
        # eliminating crystal-contact chains that mislead chain selection.
        analysis_path = ba1_path if ba1_path.exists() else asu_path
        using_ba1 = analysis_path == ba1_path
        if using_ba1:
            logger.info(f"  Using biological assembly 1 for structure analysis: {ba1_path}")

        # Parse chain→entity descriptions from the CIF header so the skill can
        # unambiguously identify target vs. partner chains even in multi-copy ASUs.
        chain_descs = self._chain_entity_descriptions(analysis_path)
        target_complex = result.target_complex or prev_handoff.get("target_complex", "the target complex")

        chain_hint = ""
        if chain_descs:
            lines = [f"  Chain {ch}: {desc}" for ch, desc in sorted(chain_descs.items())]
            chain_hint = (
                "\n\nChain entity descriptions from the mmCIF header "
                f"({'biological assembly 1' if using_ba1 else 'asymmetric unit'}):\n"
                + "\n".join(lines)
                + "\n\nSelect the chains that form the biologically relevant "
                f"{target_complex} interface. "
                "Do NOT analyse crystal-packing contacts between identical chain copies."
            )

        # Use the pathway handoff's structure_query only when its PDB matches the
        # user-selected PDB. If the user picked a different structure, build a
        # fresh query so the skill is pointed at the correct file.
        handoff_pdb = prev_handoff.get("pdb_id", "")
        handoff_query = prev_handoff.get("structure_query")
        if handoff_query and handoff_pdb.upper() == pdb_id.upper():
            # Replace any ASU path in the pathway-generated query with the analysis path
            query = handoff_query.replace(str(asu_path), str(analysis_path))
            if str(analysis_path) not in query:
                query = query + f"\n\nStructure file to use: {analysis_path}"
            query += chain_hint
        else:
            query = (
                f"Analyse the {target_complex} interface in PDB {pdb_id}.\n"
                f"Structure file: {analysis_path}{chain_hint}"
            )

        logger.info("Stage 1: complex-structure-analysis")
        # Don't pass prior stage context: structure_query already contains everything
        # the skill needs, and the full pathway.md adds ~8k tokens per LLM call.
        handoff = self._run_stage("complex-structure-analysis", query, [], output_file)
        result.stages_completed.append("structure")
        result.stage_files["structure"] = output_file
        result.target_complex = handoff.get("target_complex") or result.target_complex
        return handoff

    def _stage_literature(
        self,
        prev_handoff: dict,
        run_dir: Path,
        result: PipelineResult,
        context_files: list[Path],
    ) -> dict[str, str]:
        output_file = run_dir / "02_literature.md"
        complex_name = result.target_complex or prev_handoff.get("target_complex", "the target complex")

        query = prev_handoff.get("literature_query") or (
            f"Search for published inhibitors and mutagenesis data for {complex_name}. "
            "Cross-reference the structural hotspot residues identified in the structure report."
        )
        logger.info("Stage 2: molecular-biology-expert")
        # Pass only the structure report (hotspot residues), not the full pathway report.
        # The literature_query already encodes the key structural findings.
        structure_ctx = [f for f in [result.stage_files.get("structure")] if f and f.exists()]
        handoff = self._run_stage("molecular-biology-expert", query, structure_ctx, output_file)
        result.stages_completed.append("literature")
        result.stage_files["literature"] = output_file
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
        model_id = self._stage_models.get(stage, self._default_model)
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

        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(output_text, encoding="utf-8")
        logger.info(f"  [{skill_name}] → {output_file} ({len(output_text):,} chars)")

        handoff = self._parse_handoff(output_text)
        if handoff:
            logger.info(f"  [{skill_name}] handoff: {list(handoff.keys())}")
        else:
            logger.warning(
                f"  [{skill_name}] No '### PIPELINE HANDOFF' block found — "
                "next stage will use a fallback query"
            )
        return handoff

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
        target_chain = handoff.get("target_chain", "")
        partner_chain = handoff.get("partner_chain", "")

        sections = re.findall(
            r"###\s+MODEL.READY HOTSPOTS.*?(?=\n###|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if not sections:
            return None

        row_pat = re.compile(
            r"^\|\s*([A-Z]+)\d*\s*\|\s*(\d+)\s*\|\s*(\d+)[^|]*\|\s*([^|]+?)\s*\|",
            re.MULTILINE,
        )
        residues: list[dict] = []
        seen: set[tuple] = set()
        for section in sections:
            for m in row_pat.finditer(section):
                residue, auth_id, label_id, atoms = m.groups()
                key = (residue, int(auth_id))
                if key not in seen:
                    seen.add(key)
                    residues.append({
                        "residue": residue,
                        "auth_seq_id": int(auth_id),
                        "label_seq_id": int(label_id),
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
