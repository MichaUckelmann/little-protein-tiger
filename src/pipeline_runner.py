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

import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PipelineError(RuntimeError):
    """Non-recoverable pipeline failure."""


class PipelineBlockedError(PipelineError):
    """Pipeline cannot continue automatically — user input required."""


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
    ) -> None:
        self.config = config
        self.provider = provider
        self.model_id = model_id or _DEFAULT_MODELS[provider]
        self._output_dir_override = output_dir
        self.max_iter = max_iter
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        query: str,
        start_from: str = "pathway",
        pdb_id: str | None = None,
        context_file: Path | None = None,
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

            # ── Stage 0.5: ensure structure on disk ──────────────────────────
            if start_idx <= 1:
                pdb = result.pdb_id or handoff.get("pdb_id", "")
                if not pdb or pdb.upper() == "NOT_FOUND":
                    raise PipelineBlockedError(
                        "No PDB accession found in corpus.  Re-run with --pdb <accession> "
                        "or add more papers via `python scripts/fetch_papers.py` and re-curate."
                    )
                result.pdb_id = pdb
                self._ensure_structure(pdb)

            # ── Stage 1: complex-structure-analysis ──────────────────────────
            if start_idx <= 1:
                ctx = [f for f in [result.stage_files.get("pathway"), context_file] if f and f.exists()]
                handoff = self._stage_structure(handoff, run_dir, result, ctx)

            # ── Stage 2: molecular-biology-expert ────────────────────────────
            if start_idx <= 2:
                ctx = [
                    f for f in [
                        result.stage_files.get("pathway"),
                        result.stage_files.get("structure"),
                        context_file,
                    ]
                    if f and f.exists()
                ]
                handoff = self._stage_literature(handoff, run_dir, result, ctx)

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
        structure_path = (
            _ROOT
            / self.config.get("paths", {}).get("structures_dir", "data/structures")
            / f"{pdb_id.upper()}.cif"
        )

        query = prev_handoff.get("structure_query") or (
            f"Analyze PDB {pdb_id} at {structure_path}. "
            "Identify target and partner chains, map interface hotspot residues for binder design."
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
        self._run_stage("protein-design-script", query, context_files, design_report)
        result.stages_completed.append("design")
        result.stage_files["design"] = design_report
        result.design_files = [f for f in design_dir.iterdir() if f.is_file()]

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

        runner = SkillRunner(
            skill_name=skill_name,
            provider=self.provider,
            model_id=self.model_id,
            config=self.config,
            max_iter=self.max_iter,
            max_input_tokens=self.max_tokens,
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

    def _ensure_structure(self, pdb_id: str) -> Path:
        """Return local CIF path, downloading from RCSB if absent."""
        structures_dir = _ROOT / self.config.get("paths", {}).get("structures_dir", "data/structures")
        dest = structures_dir / f"{pdb_id.upper()}.cif"
        if dest.exists():
            logger.info(f"  Structure {pdb_id} on disk: {dest}")
            return dest

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
        return dest

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
