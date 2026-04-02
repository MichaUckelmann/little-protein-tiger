"""
CLI-compatible agentic skill runner.

Loads a SKILL.md as the system prompt, runs the standard tool-calling loop,
and routes tool calls directly to Python functions — no MCP subprocess required.

Supports Claude (Anthropic SDK) and Gemini (REST API).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import time

import anthropic
import requests
from dotenv import load_dotenv
from loguru import logger

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# AA normalisation (mirrors structure_tools_server.py)
# ---------------------------------------------------------------------------

_ONE_TO_THREE: dict[str, str] = {
    "A": "ALA", "R": "ARG", "N": "ASN", "D": "ASP", "C": "CYS",
    "Q": "GLN", "E": "GLU", "G": "GLY", "H": "HIS", "I": "ILE",
    "L": "LEU", "K": "LYS", "M": "MET", "F": "PHE", "P": "PRO",
    "S": "SER", "T": "THR", "W": "TRP", "Y": "TYR", "V": "VAL",
}


# ---------------------------------------------------------------------------
# Tool definitions — one source of truth, converted per-provider
# ---------------------------------------------------------------------------

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "search_corpus",
        "description": (
            "Semantically search the curated scientific literature corpus. "
            "Returns top-k paper fingerprints ranked by similarity, each with "
            "situational context, key quantitative findings (Kd, Ki), protein "
            "lists, DOI, study type, and study category. Use for proteins, "
            "mechanisms, assay results, inhibitors, or binding affinities."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural language query. Include protein names, mechanisms, "
                        "assay types, or quantitative terms for best results."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (1-20). Default 5.",
                },
                "study_type": {
                    "type": "string",
                    "description": "Optional methodology filter.",
                    "enum": [
                        "experimental_in_vitro", "experimental_in_vivo",
                        "experimental_structural", "computational", "review", "case_study",
                    ],
                },
                "study_category": {
                    "type": "string",
                    "description": (
                        "Optional domain filter. 'pathway_biology' for disease mechanism "
                        "papers; 'biochemistry' for binding assay papers."
                    ),
                    "enum": [
                        "biochemistry", "pathway_biology", "structural_biology",
                        "host_pathogen", "clinical", "review",
                    ],
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_fingerprint",
        "description": (
            "Retrieve the complete fingerprint for a specific paper by DOI or paper_key. "
            "Returns all key findings, methodology, contradictions, and entity lists. "
            "Use after search_corpus identifies a paper of interest."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": (
                        "DOI (e.g. '10.1021/jacs.5c12876') or paper_key "
                        "(e.g. 'doi:10.1021/jacs.5c12876'). Both forms accepted."
                    ),
                },
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "tool_analyze_interface",
        "description": (
            "Full interface analysis between two chains of a structure file. "
            "Computes BSA (total + per-residue), interface residues with type "
            "classification, H-bonds with geometry, pairwise contact map with "
            "interaction classification, gap residue flags, and pLDDT scores. "
            "Accepts .cif and .pdb files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to structure file (.cif or .pdb)"},
                "chain_a":   {"type": "string", "description": "Chain ID of first chain"},
                "chain_b":   {"type": "string", "description": "Chain ID of second chain"},
                "cutoff":    {"type": "number", "description": "Heavy-atom distance cutoff in Å (default 4.5)"},
            },
            "required": ["file_path", "chain_a", "chain_b"],
        },
    },
    {
        "name": "tool_get_residue_contacts",
        "description": (
            "All contacts between a single residue and a partner chain within the cutoff. "
            "Returns per-contact distances, interaction classification, H-bond details, "
            "and gap flag."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path":     {"type": "string", "description": "Absolute path to structure file"},
                "chain":         {"type": "string", "description": "Chain ID containing the residue"},
                "resnum":        {"type": "integer", "description": "Residue number (auth_seq_id)"},
                "partner_chain": {"type": "string", "description": "Chain ID to check contacts against"},
                "cutoff":        {"type": "number", "description": "Heavy-atom distance cutoff in Å (default 4.5)"},
            },
            "required": ["file_path", "chain", "resnum", "partner_chain"],
        },
    },
    {
        "name": "tool_check_mutation_clash",
        "description": (
            "Estimates whether a point mutation would clash with the partner chain. "
            "Uses a Cβ heuristic. Returns clash severity: none / minor / major. "
            "Accepts 1-letter or 3-letter amino acid codes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path":     {"type": "string", "description": "Absolute path to structure file"},
                "chain":         {"type": "string", "description": "Chain ID of the residue to mutate"},
                "resnum":        {"type": "integer", "description": "Residue number (auth_seq_id)"},
                "new_aa":        {"type": "string", "description": "Proposed amino acid (1-letter or 3-letter)"},
                "partner_chain": {"type": "string", "description": "Chain ID to check clashes against"},
            },
            "required": ["file_path", "chain", "resnum", "new_aa", "partner_chain"],
        },
    },
    {
        "name": "tool_get_sequence_map",
        "description": (
            "Returns the amino acid sequence of a chain with numbering maps for AF3 JSON. "
            "Provides: 1-letter sequence string, auth_seq_id → string index map, "
            "and auth_seq_id → label_seq_id map."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to structure file"},
                "chain":     {"type": "string", "description": "Chain ID"},
            },
            "required": ["file_path", "chain"],
        },
    },
    {
        "name": "tool_score_surface_patch",
        "description": (
            "Characterises a set of residues as a potential binding surface. "
            "Computes spatial spread (Cα RMSD), mean KD hydrophobicity, residue "
            "type breakdown, hydrophobic fraction, and a suitability rating "
            "(Excellent / Good / Marginal / Poor)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path":    {"type": "string", "description": "Absolute path to structure file"},
                "chain":        {"type": "string", "description": "Chain ID of the target surface"},
                "residue_list": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of residue numbers (auth_seq_id) defining the patch",
                },
            },
            "required": ["file_path", "chain", "residue_list"],
        },
    },
]


def _to_claude_tools(defs: list[dict]) -> list[dict]:
    return [
        {"name": d["name"], "description": d["description"], "input_schema": d["parameters"]}
        for d in defs
    ]


def _to_gemini_tools(defs: list[dict]) -> list[dict]:
    return [
        {
            "functionDeclarations": [
                {"name": d["name"], "description": d["description"], "parameters": d["parameters"]}
                for d in defs
            ]
        }
    ]


# ---------------------------------------------------------------------------
# SkillRunner
# ---------------------------------------------------------------------------

_GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


class SkillRunner:
    """
    Runs one skill against Claude or Gemini via an agentic tool-calling loop.

    Parameters
    ----------
    skill_name : str
        Directory name under ``skills/``.
    provider : str
        ``"claude"`` or ``"gemini"``.
    model_id : str
        Model identifier passed directly to the API.
    config : dict
        Parsed ``config.yaml`` — used for data paths.
    max_iter : int
        Hard cap on LLM calls per run (default 30).
    """

    def __init__(
        self,
        skill_name: str,
        provider: str,
        model_id: str,
        config: dict,
        max_iter: int = 30,
        max_input_tokens: int = 100_000,
    ) -> None:
        self.skill_name = skill_name
        self.provider = provider
        self.model_id = model_id
        self.config = config
        self.max_iter = max_iter
        self.max_input_tokens = max_input_tokens

        # Token usage tracking — populated during run()
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0

        # Resolve data paths (relative → absolute from project root)
        fp_dir = config.get("paths", {}).get("fingerprint_dir", "data/fingerprints")
        self._fingerprint_dir = (
            Path(fp_dir) if Path(fp_dir).is_absolute() else _ROOT / fp_dir
        )

        vs_path = config.get("vector_store", {}).get("db_path", "data/vectors")
        self._vector_db_path = str(
            Path(vs_path) if Path(vs_path).is_absolute() else _ROOT / vs_path
        )

        self._embedding_model = config.get("vector_store", {}).get(
            "embedding_model", "NeuML/pubmedbert-base-embeddings"
        )

        self._store = None  # lazy — sentence-transformers is slow to import

        self.system_prompt = self._load_system_prompt()
        logger.info(
            f"SkillRunner ready: skill={skill_name}, provider={provider}, model={model_id}"
        )

    # ------------------------------------------------------------------
    # System prompt loading
    # ------------------------------------------------------------------

    def _load_system_prompt(self) -> str:
        skills_root = _ROOT / "skills"
        skill_path = skills_root / self.skill_name / "SKILL.md"
        if not skill_path.exists():
            raise FileNotFoundError(f"SKILL.md not found: {skill_path}")

        system = skill_path.read_text(encoding="utf-8")

        if self.skill_name == "orchestrator":
            for sub_dir in sorted(skills_root.iterdir()):
                if sub_dir.name == "orchestrator" or not sub_dir.is_dir():
                    continue
                sub_md = sub_dir / "SKILL.md"
                if sub_md.exists():
                    system += (
                        f"\n\n---\n## SUB-SKILL: {sub_dir.name}\n"
                        + sub_md.read_text(encoding="utf-8")
                    )
            logger.info("Orchestrator mode: sub-skill SKILL.mds appended to system prompt")

        return system

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _get_store(self):
        if self._store is None:
            from src.vector_store import VectorStore
            logger.info("Initialising VectorStore (lazy, first search_corpus call)…")
            self._store = VectorStore(
                db_path=self._vector_db_path,
                embedding_model=self._embedding_model,
            )
        return self._store

    def _execute_tool(self, name: str, input_dict: dict) -> str:
        try:
            if name == "search_corpus":
                raw = self._get_store().execute_search_tool(input_dict)
                # Strip the 'papers' JSON array — it duplicates the formatted result_text
                # and can add 40k+ tokens when top_k is large. The model reads result_text
                # and calls get_fingerprint for papers it wants full details on.
                try:
                    parsed = json.loads(raw)
                    parsed.pop("papers", None)
                    return json.dumps(parsed, ensure_ascii=False)
                except (json.JSONDecodeError, AttributeError):
                    return raw

            if name == "get_fingerprint":
                from src.fingerprint_store import load_fingerprint
                identifier = input_dict.get("identifier", "")
                if not identifier.startswith("doi:") and not identifier.startswith("pmcid:"):
                    paper_key = f"doi:{identifier}"
                else:
                    paper_key = identifier
                fp = load_fingerprint(paper_key, self._fingerprint_dir)
                if fp is None:
                    return json.dumps({"error": f"No fingerprint found for '{identifier}'"})
                # Strip fields never used by any skill to reduce token cost.
                # Skills that need methodology or contradictions can override this.
                fp.pop("curation_metadata", None)
                fp.pop("contradictions_and_negative_results", None)
                fp.pop("methodology", None)
                return json.dumps(fp, ensure_ascii=False, indent=2)

            if name == "tool_analyze_interface":
                from src.structure_tools import analyze_interface
                result = analyze_interface(
                    input_dict["file_path"],
                    input_dict["chain_a"],
                    input_dict["chain_b"],
                    float(input_dict.get("cutoff", 4.5)),
                )
                return json.dumps(result, indent=2)

            if name == "tool_get_residue_contacts":
                from src.structure_tools import get_residue_contacts
                result = get_residue_contacts(
                    input_dict["file_path"],
                    input_dict["chain"],
                    int(input_dict["resnum"]),
                    input_dict["partner_chain"],
                    float(input_dict.get("cutoff", 4.5)),
                )
                return json.dumps(result, indent=2)

            if name == "tool_check_mutation_clash":
                from src.structure_tools import check_mutation_clash
                aa = str(input_dict["new_aa"]).strip().upper()
                if len(aa) == 1:
                    aa = _ONE_TO_THREE.get(aa, aa)
                result = check_mutation_clash(
                    input_dict["file_path"],
                    input_dict["chain"],
                    int(input_dict["resnum"]),
                    aa,
                    input_dict["partner_chain"],
                )
                return json.dumps(result, indent=2)

            if name == "tool_get_sequence_map":
                from src.structure_tools import get_sequence_map
                result = get_sequence_map(input_dict["file_path"], input_dict["chain"])
                return json.dumps(result, indent=2)

            if name == "tool_score_surface_patch":
                from src.structure_tools import score_surface_patch
                result = score_surface_patch(
                    input_dict["file_path"],
                    input_dict["chain"],
                    [int(r) for r in input_dict["residue_list"]],
                )
                return json.dumps(result, indent=2)

            return json.dumps({"error": f"Unknown tool: {name}"})

        except Exception as exc:
            logger.warning(f"Tool '{name}' raised: {exc}")
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, query: str, context_text: str | None = None) -> str:
        """
        Run the agentic loop and return the final report text.

        Parameters
        ----------
        query : str
            User query.
        context_text : str | None
            Optional prior report to include as context (prepended to query).
        """
        user_content = query
        if context_text:
            user_content = (
                f"## Context from prior report\n\n{context_text}\n\n---\n\n{query}"
            )

        if self.provider == "claude":
            messages: list[dict] = [{"role": "user", "content": user_content}]
            return self._run_claude(messages)
        else:
            messages = [{"role": "user", "parts": [{"text": user_content}]}]
            return self._run_gemini(messages)

    # ------------------------------------------------------------------
    # Claude agentic loop
    # ------------------------------------------------------------------

    def _run_claude(self, messages: list[dict]) -> str:
        client = anthropic.Anthropic()
        claude_tools = _to_claude_tools(_TOOL_DEFS)

        for iteration in range(self.max_iter):
            logger.info(f"[claude] call #{iteration + 1} — messages={len(messages)}")

            # Retry up to 3 times on rate-limit errors (30k tokens/min window)
            for attempt in range(3):
                try:
                    response = client.messages.create(
                        model=self.model_id,
                        max_tokens=8192,
                        system=self.system_prompt,
                        tools=claude_tools,
                        messages=messages,
                    )
                    break
                except anthropic.RateLimitError:
                    if attempt == 2:
                        raise
                    wait = 65 * (attempt + 1)
                    logger.warning(f"Rate limit hit — waiting {wait}s then retrying…")
                    time.sleep(wait)

            # Serialise content blocks for history
            content_list: list[dict] = []
            text_parts: list[str] = []
            tool_use_blocks = []

            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                    content_list.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    tool_use_blocks.append(block)
                    content_list.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            in_tok = response.usage.input_tokens
            out_tok = response.usage.output_tokens
            self._total_input_tokens += in_tok
            self._total_output_tokens += out_tok
            logger.info(
                f"  tokens: {in_tok:,} in / {out_tok:,} out "
                f"(run cumulative: {self._total_input_tokens:,} in / "
                f"{self._total_output_tokens:,} out)"
            )

            if in_tok > self.max_input_tokens:
                raise RuntimeError(
                    f"Input token limit exceeded on call #{iteration + 1}: "
                    f"{in_tok:,} tokens in one request "
                    f"(limit: {self.max_input_tokens:,}). "
                    f"Run total so far: {self._total_input_tokens:,} in / "
                    f"{self._total_output_tokens:,} out. "
                    f"Reduce top_k, use a shorter query, or raise --max-tokens."
                )

            messages.append({"role": "assistant", "content": content_list})

            if not tool_use_blocks:
                logger.info(
                    f"Run complete — total tokens: {self._total_input_tokens:,} in / "
                    f"{self._total_output_tokens:,} out across {iteration + 1} LLM calls"
                )
                return "\n".join(text_parts)

            # Execute all tool calls and batch results
            tool_results: list[dict] = []
            for block in tool_use_blocks:
                logger.info(f"  → {block.name}({list(block.input.keys())})")
                result = self._execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

        raise RuntimeError(
            f"Max iterations ({self.max_iter}) exceeded for skill '{self.skill_name}'"
        )

    # ------------------------------------------------------------------
    # Gemini agentic loop
    # ------------------------------------------------------------------

    def _run_gemini(self, messages: list[dict]) -> str:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        url = _GEMINI_GENERATE_URL.format(model=self.model_id)
        gemini_tools = _to_gemini_tools(_TOOL_DEFS)

        for iteration in range(self.max_iter):
            logger.info(f"[gemini] call #{iteration + 1} — messages={len(messages)}")

            payload = {
                "system_instruction": {"parts": [{"text": self.system_prompt}]},
                "contents": messages,
                "tools": gemini_tools,
                "generationConfig": {"maxOutputTokens": 8192},
            }
            resp = requests.post(
                url, params={"key": api_key}, json=payload, timeout=180
            )
            resp.raise_for_status()
            body = resp.json()

            parts: list[dict] = body["candidates"][0]["content"]["parts"]

            usage = body.get("usageMetadata", {})
            in_tok = usage.get("promptTokenCount", 0)
            out_tok = usage.get("candidatesTokenCount", 0)
            self._total_input_tokens += in_tok
            self._total_output_tokens += out_tok
            logger.info(
                f"  tokens: {in_tok:,} in / {out_tok:,} out "
                f"(run cumulative: {self._total_input_tokens:,} in / "
                f"{self._total_output_tokens:,} out)"
            )

            if in_tok > self.max_input_tokens:
                raise RuntimeError(
                    f"Input token limit exceeded on call #{iteration + 1}: "
                    f"{in_tok:,} tokens in one request "
                    f"(limit: {self.max_input_tokens:,}). "
                    f"Run total so far: {self._total_input_tokens:,} in / "
                    f"{self._total_output_tokens:,} out. "
                    f"Reduce top_k, use a shorter query, or raise --max-tokens."
                )

            # Append model turn to history
            messages.append({"role": "model", "parts": parts})

            function_calls = [p["functionCall"] for p in parts if "functionCall" in p]

            if not function_calls:
                logger.info(
                    f"Run complete — total tokens: {self._total_input_tokens:,} in / "
                    f"{self._total_output_tokens:,} out across {iteration + 1} LLM calls"
                )
                text_parts = [p.get("text", "") for p in parts if "text" in p]
                return "\n".join(text_parts)

            # Execute tools; batch all functionResponses in one user turn
            function_responses: list[dict] = []
            for fc in function_calls:
                name = fc["name"]
                args = fc.get("args", {})
                logger.info(f"  → {name}({list(args.keys())})")
                result = self._execute_tool(name, args)
                function_responses.append({
                    "functionResponse": {
                        "name": name,
                        "response": {"result": result},
                    }
                })

            messages.append({"role": "user", "parts": function_responses})

        raise RuntimeError(
            f"Max iterations ({self.max_iter}) exceeded for skill '{self.skill_name}'"
        )
