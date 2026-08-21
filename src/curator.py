"""LLM curation: extract structured fingerprint from paper text.

Supports two providers, selected via config["curation"]["provider"]:
  - "claude"  (default) — Anthropic Claude API
  - "gemini"             — Google Gemini API
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
import requests
from loguru import logger
from pydantic import BaseModel, field_validator


# ---------------------------------------------------------------------------
# Pydantic validation models
# ---------------------------------------------------------------------------

class CurationMetadata(BaseModel):
    model: str
    curated_at: str
    input_tokens: int
    output_tokens: int


class PaperMetadata(BaseModel):
    title: str
    doi: Optional[str] = None
    study_type: str
    situational_context_hook: str


class Methodology(BaseModel):
    experimental_methods_used: list[str] = []
    protein_origin_organism: list[int] = []
    controls: list[str] = []
    instruments_used: list[str] = []


class KeyFinding(BaseModel):
    claim: str
    evidence_value: Optional[object] = None
    quantitative_or_qualitative: Optional[str] = None
    protein_pair: Optional[list[Optional[str]]] = None
    experimental_context: Optional[str] = None
    affinities_kd_Molar: Optional[float] = None
    inhibitory_constant_Ki: Optional[float] = None
    key_amino_acid_residues: list[str] = []
    confidence_score: float
    is_statistically_significant: Optional[bool] = None
    source_span: str

    @field_validator("confidence_score")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class Contradiction(BaseModel):
    finding: str
    conflicts_with_prior_work: Optional[bool] = None
    reasoning: Optional[str] = None


class Entities(BaseModel):
    chemicals: list[str] = []
    proteins: list[str] = []
    equations: list[str] = []


class DiseaseAssociation(BaseModel):
    disease: str
    mechanism: str
    mutation_frequency: Optional[str] = None
    genetic_evidence_type: Optional[str] = None
    source_span: str


class TargetNode(BaseModel):
    protein: str
    pathway_position: str
    dysregulation: str
    genetic_dependency_evidence: Optional[str] = None
    prior_therapeutic_targeting: Optional[str] = None
    suggested_pdb_structures: list[str] = []
    source_span: str

    @field_validator("prior_therapeutic_targeting", mode="before")
    @classmethod
    def _coerce_list_to_str(cls, v):
        # Gemini sometimes returns a list (e.g. ["PARP inhibitors", "Olaparib"])
        # for this field even though the schema says string. Join into one string
        # rather than reject — losing the data on a formatting mismatch is worse.
        if isinstance(v, list):
            return ", ".join(str(x) for x in v if x) or None
        return v


class PathwayContext(BaseModel):
    pathways: list[str] = []
    disease_associations: list[DiseaseAssociation] = []
    target_nodes: list[TargetNode] = []
    pathway_logic: Optional[str] = None
    redundancy_risks: list[str] = []
    upstream_regulators: list[str] = []
    downstream_effectors: list[str] = []


class Fingerprint(BaseModel):
    schema_version: str = "2.0"
    relevant: bool
    study_category: Optional[str] = None
    pathway_context: Optional[PathwayContext] = None
    curation_metadata: Optional[CurationMetadata] = None
    paper_metadata: Optional[PaperMetadata] = None
    methodology: Optional[Methodology] = None
    key_findings: list[KeyFinding] = []
    contradictions_and_negative_results: list[Contradiction] = []
    entities: Optional[Entities] = None


# ---------------------------------------------------------------------------
# Provider helpers — each returns (raw_text, input_tokens, output_tokens)
# ---------------------------------------------------------------------------

def _call_claude(
    messages: list[dict],
    system_prompt: str,
    model: str,
    max_tokens: int,
) -> tuple[str, int, int]:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
    )
    return (
        response.content[0].text,
        response.usage.input_tokens,
        response.usage.output_tokens,
    )


_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


def _call_gemini(
    contents: list[dict],
    system_prompt: str,
    model: str,
    max_tokens: int,
    api_key: str,
) -> tuple[str, int, int]:
    """Call Gemini REST API. `contents` is the full conversation so far."""
    url = _GEMINI_URL.format(model=model)
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    resp = requests.post(url, params={"key": api_key}, json=payload, timeout=120)
    resp.raise_for_status()
    body = resp.json()
    text = body["candidates"][0]["content"]["parts"][0]["text"]
    usage = body.get("usageMetadata", {})
    return (
        text,
        usage.get("promptTokenCount", 0),
        usage.get("candidatesTokenCount", 0),
    )


def _call_local(
    messages: list[dict],
    system_prompt: str,
    model: str,
    max_tokens: int,
    endpoint: str,
    sampling: dict | None = None,
    options: dict | None = None,
    request_timeout: int = 600,
) -> tuple[str, int, int]:
    """Call a local OpenAI-compatible endpoint (Ollama / vLLM / SGLang).

    Parameters
    ----------
    sampling
        Sampling kwargs merged into the request body (temperature, top_p,
        response_format, etc.). Keep this provider-agnostic — Qwen / Gemma /
        Llama tunables come from config so the same code path serves any
        local model.
    options
        Ollama-specific nested ``options`` block (e.g. ``num_ctx`` to override
        the default context window). Ignored by non-Ollama backends, which
        is fine.
    """
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    payload: dict = {
        "model": model,
        "messages": full_messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    if sampling:
        payload.update(sampling)
    if options:
        payload["options"] = options
    resp = requests.post(f"{endpoint}/chat/completions", json=payload, timeout=request_timeout)
    resp.raise_for_status()
    body = resp.json()
    text = body["choices"][0]["message"]["content"]
    usage = body.get("usage", {})
    return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


_CORRECTION = (
    "Your previous response was not valid JSON or did not match the schema. "
    "Error: {error}. "
    "Please output ONLY valid JSON with no prose or markdown fences."
)


def _append_retry_turn(
    provider: str,
    assistant_text: str,
    error: str,
    claude_messages: list[dict],
    gemini_contents: list[dict],
) -> None:
    """Append the failed assistant turn + correction request to the conversation history."""
    correction = _CORRECTION.format(error=error)
    if provider == "gemini":
        gemini_contents.append({"role": "model", "parts": [{"text": assistant_text}]})
        gemini_contents.append({"role": "user", "parts": [{"text": correction}]})
    else:
        claude_messages.append({"role": "assistant", "content": assistant_text})
        claude_messages.append({"role": "user", "content": correction})


# ---------------------------------------------------------------------------
# Main curation function
# ---------------------------------------------------------------------------

def curate_paper(
    paper_key: str,
    text: str,
    source_format: str,
    config: dict,
    prompt_path: Optional[Path] = None,
) -> dict:
    """
    Call an LLM with extracted paper text. Returns validated fingerprint dict.
    Raises ValueError on persistent parse/validation failure.
    """
    curation_cfg = config.get("curation", {})
    provider = curation_cfg.get("provider", "claude")
    model = curation_cfg.get("model", "claude-haiku-4-5")
    gemini_model = curation_cfg.get("gemini_model", "gemini-2.0-flash")
    local_model = curation_cfg.get("local_model", "Qwen/Qwen3.5-9B")
    local_endpoint = curation_cfg.get("local_endpoint", "http://localhost:8000/v1")
    local_sampling = curation_cfg.get("local_sampling", {}) or {}
    local_options = curation_cfg.get("local_options", {}) or {}
    local_request_timeout = int(curation_cfg.get("local_request_timeout", 600))
    max_tokens = curation_cfg.get("max_tokens", 4096)
    max_retries = curation_cfg.get("max_retries", 3)

    if provider not in ("claude", "gemini", "local"):
        logger.warning(f"Unknown provider '{provider}', falling back to 'claude'")
        provider = "claude"

    # Load system prompt
    if prompt_path is None:
        prompt_path = Path(curation_cfg.get("prompt_path", "curation_prompt.md"))
    system_prompt = prompt_path.read_text(encoding="utf-8")

    source_note = (
        "This paper was extracted from a PDF. Use 'Page N, Para M' format for source_span."
        if source_format == "pdf"
        else "This paper was extracted from structured XML. Use 'Section: [heading], Para M' format for source_span."
    )

    user_message = f"{source_note}\n\n---\n\n{text}"

    gemini_api_key = os.environ.get("GEMINI_API_KEY", "")

    # Conversation history for multi-turn retry
    # Claude uses [{role, content}]; Gemini REST uses [{role, parts:[{text}]}]
    claude_messages: list[dict] = [{"role": "user", "content": user_message}]
    gemini_contents: list[dict] = [{"role": "user", "parts": [{"text": user_message}]}]

    last_error: Exception | None = None
    raw_response: str | None = None

    for attempt in range(1, max_retries + 1):
        try:
            if provider == "gemini":
                raw_response, input_tokens, output_tokens = _call_gemini(
                    gemini_contents, system_prompt, gemini_model, max_tokens, gemini_api_key
                )
            elif provider == "local":
                raw_response, input_tokens, output_tokens = _call_local(
                    claude_messages,
                    system_prompt,
                    local_model,
                    max_tokens,
                    local_endpoint,
                    sampling=local_sampling,
                    options=local_options,
                    request_timeout=local_request_timeout,
                )
            else:
                raw_response, input_tokens, output_tokens = _call_claude(
                    claude_messages, system_prompt, model, max_tokens
                )
        except Exception as exc:
            logger.warning(f"[{paper_key}] {provider} API error (attempt {attempt}): {exc}")
            last_error = exc
            time.sleep(2 ** attempt)
            continue

        # Strip accidental markdown fences
        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.warning(f"[{paper_key}] JSON parse error (attempt {attempt}): {exc}")
            last_error = exc
            _append_retry_turn(provider, raw_response, str(exc), claude_messages, gemini_contents)
            continue

        # Early exit for irrelevant papers
        if not data.get("relevant", True):
            return {"relevant": False, "schema_version": "2.0"}

        # Inject curation metadata
        active_model = gemini_model if provider == "gemini" else (local_model if provider == "local" else model)
        data["schema_version"] = "2.0"
        data["relevant"] = True
        data["curation_metadata"] = {
            "model": active_model,
            "curated_at": datetime.now(timezone.utc).isoformat(),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

        try:
            validated = Fingerprint.model_validate(data)
            return validated.model_dump(mode="json")
        except Exception as exc:
            logger.warning(f"[{paper_key}] Pydantic validation error (attempt {attempt}): {exc}")
            last_error = exc
            _append_retry_turn(provider, raw_response, str(exc), claude_messages, gemini_contents)
            continue

    raise ValueError(f"Curation failed after {max_retries} attempts. Last error: {last_error}")
