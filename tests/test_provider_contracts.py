"""Live contract checks: do the providers still return the fields we read?

This is the one thing a vendor SDK genuinely buys that hand-rolled REST does
not — it tracks the API and fails loudly when a field is renamed or a shape
changes. `_run_gemini` and `_run_openai` read specific keys out of raw JSON, so
a rename would surface as a token count silently reading 0, a refusal going
undetected, or a tool loop that never terminates. None of those raise.

So instead of migrating to SDKs, the fields are asserted directly against one
cheap real call per provider. Marked `network`, deselected by default (see
pyproject's `addopts`), and costs a fraction of a cent to run:

    .venv/bin/python -m pytest -m network tests/test_provider_contracts.py

Run it when a provider announces an API change, when adding a model, or when
token accounting starts looking wrong. Every assertion here corresponds to a
line in the loop that would otherwise fail silently.
"""

from __future__ import annotations

import os

import pytest

from src.env_config import load_env
from src.skill_runner import _OPENAI_RESPONSES_URL, _http

load_env()

pytestmark = pytest.mark.network

_TOOL = {
    "type": "function", "name": "add",
    "description": "Add two integers.",
    "parameters": {"type": "object",
                   "properties": {"a": {"type": "integer"},
                                  "b": {"type": "integer"}},
                   "required": ["a", "b"]},
}


def _need(var: str) -> str:
    key = os.environ.get(var, "")
    if not key or key.startswith("test-placeholder"):
        pytest.skip(f"{var} not set")
    return key


# ── OpenAI Responses ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def openai_tool_call():
    key = _need("OPENAI_API_KEY")
    r = _http().post(
        _OPENAI_RESPONSES_URL,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json={"model": "gpt-5.6-terra",
              "instructions": "Use the tool. Do not answer from memory.",
              "input": [{"role": "user", "content": "What is 2 + 3? Use add."}],
              "tools": [_TOOL]},
        timeout=120)
    assert r.status_code == 200, r.text[:400]
    return r.json()


def test_openai_usage_carries_the_four_billing_buckets(openai_tool_call):
    """`_run_openai` splits spend into uncached / cache-write / cache-read /
    output. A renamed detail key would make a bucket read 0 and misprice the
    run without any error."""
    usage = openai_tool_call["usage"]
    assert "input_tokens" in usage and "output_tokens" in usage
    assert "input_tokens_details" in usage
    detail = usage["input_tokens_details"]
    assert "cached_tokens" in detail, detail
    assert "cache_write_tokens" in detail, detail
    assert "reasoning_tokens" in usage.get("output_tokens_details", {})


def test_openai_input_tokens_still_includes_the_cached_share(openai_tool_call):
    """The convention the accounting depends on. If OpenAI switched to
    Anthropic's (input EXCLUDES cache), subtracting would under-report."""
    usage = openai_tool_call["usage"]
    detail = usage["input_tokens_details"]
    assert usage["input_tokens"] >= (detail["cached_tokens"]
                                     + detail["cache_write_tokens"])


def test_openai_function_call_shape_is_unchanged(openai_tool_call):
    """`_run_openai` reads name/arguments/call_id off a `function_call` item
    and echoes it back. A renamed field means the loop never dispatches a
    tool and runs to max_iter instead."""
    calls = [o for o in openai_tool_call["output"]
             if o.get("type") == "function_call"]
    assert calls, [o.get("type") for o in openai_tool_call["output"]]
    call = calls[0]
    assert call["name"] == "add"
    assert isinstance(call["arguments"], str)      # JSON string, not a dict
    assert call["call_id"]


def test_openai_still_rejects_a_replay_missing_its_reasoning_item():
    """The rule the loop is built around: echo output items VERBATIM.

    If this ever starts passing, the reasoning-replay requirement has been
    relaxed and `_run_openai`'s docstring is out of date — worth knowing, but
    replaying is still correct, so this is a canary rather than a constraint.
    """
    key = _need("OPENAI_API_KEY")
    base = {"model": "gpt-5.6-terra", "instructions": "Use the tool.",
            "tools": [_TOOL]}
    inp = [{"role": "user", "content": "What is 2 + 3? Use add."}]
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    first = _http().post(_OPENAI_RESPONSES_URL, headers=h, timeout=120,
                         json={**base, "input": inp}).json()
    out = first["output"]
    call = next(o for o in out if o.get("type") == "function_call")
    if not any(o.get("type") == "reasoning" for o in out):
        pytest.skip("this model returned no reasoning item to drop")
    # Replay the call WITHOUT the reasoning item that preceded it.
    bad = _http().post(_OPENAI_RESPONSES_URL, headers=h, timeout=120, json={
        **base, "input": inp + [call, {"type": "function_call_output",
                                       "call_id": call["call_id"],
                                       "output": "5"}]})
    assert bad.status_code == 400, (
        "a function_call replayed without its reasoning item was accepted — "
        "the verbatim-replay requirement may have been relaxed")
    # And the full replay still works.
    good = _http().post(_OPENAI_RESPONSES_URL, headers=h, timeout=120, json={
        **base, "input": inp + out + [{"type": "function_call_output",
                                       "call_id": call["call_id"],
                                       "output": "5"}]})
    assert good.status_code == 200, good.text[:300]


# ── Gemini ───────────────────────────────────────────────────────────────────

def test_gemini_usage_metadata_keys_are_unchanged():
    """`_run_gemini` reads promptTokenCount / candidatesTokenCount /
    thoughtsTokenCount / cachedContentTokenCount. `thoughtsTokenCount` is the
    one that matters most: it is billed at the output rate and is NOT inside
    candidatesTokenCount, so losing it under-reports every thinking call."""
    key = _need("GEMINI_API_KEY")
    from src.skill_runner import _GEMINI_GENERATE_URL

    r = _http().post(
        _GEMINI_GENERATE_URL.format(model="gemini-3.7-flash"),
        headers={"x-goog-api-key": key}, timeout=120,
        json={"contents": [{"role": "user",
                            "parts": [{"text": "Reply with exactly: ok"}]}]})
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    usage = body["usageMetadata"]
    assert "promptTokenCount" in usage
    assert "candidatesTokenCount" in usage
    candidate = body["candidates"][0]
    assert "finishReason" in candidate
    assert (candidate.get("content") or {}).get("parts") is not None


# ── the key is never in a URL ────────────────────────────────────────────────

def test_no_provider_url_template_carries_a_key_placeholder():
    """`requests` embeds the request URL in every exception it raises, so a
    key in a query string is printed by any connection failure — observed
    live, an SSL error printed a working Gemini key. Cheap, offline, and the
    reason it lives in this file is that it is the same contract."""
    from src.skill_runner import _GEMINI_GENERATE_URL

    for url in (_GEMINI_GENERATE_URL, _OPENAI_RESPONSES_URL):
        assert "key=" not in url and "api_key" not in url, url
