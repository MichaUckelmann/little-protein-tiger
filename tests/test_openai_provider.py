"""The OpenAI Responses provider, and the budget hole adding it exposed.

Three providers now share one agentic loop, one refusal contract and one
four-bucket token ledger. The tests here pin the parts of that contract which
fail *silently* when they are wrong: an unrecognised provider that quietly
POSTs to a different vendor's endpoint, a refusal that reads as an empty
report instead of firing the fallback chain, and token accounting that
misprices a cached prefix.

Every wire-format fact asserted below was verified against a live call before
being written down; see `_run_openai`'s docstring.
"""

from __future__ import annotations

import copy
import json

import pytest

from src.skill_runner import (
    SkillRefusedError,
    SkillRunner,
    _TOOL_DEFS,
    _to_openai_tools,
)


def _runner(**kw) -> SkillRunner:
    return SkillRunner(skill_name="design-analyst", provider="openai",
                       model_id="gpt-5.6-terra", config={}, **kw)


def _response(*, output, usage=None, **extra) -> dict:
    return {"status": "completed", "output": output,
            "usage": usage or {"input_tokens": 10, "output_tokens": 5,
                               "input_tokens_details": {},
                               "output_tokens_details": {}},
            **extra}


class _Resp:
    """Minimal stand-in for a `requests` response."""

    def __init__(self, body, status=200):
        self._body, self.status_code, self.headers = body, status, {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status {self.status_code}")


# ── tool schema ──────────────────────────────────────────────────────────────

def test_tools_use_the_flat_responses_shape():
    """The Responses API rejects Chat-Completions' nesting under "function"."""
    tools = _to_openai_tools(_TOOL_DEFS[:2])
    for t in tools:
        assert t["type"] == "function"
        assert set(t) == {"type", "name", "description", "parameters"}
        assert "function" not in t


def test_tools_do_not_claim_strict_schemas():
    """`_TOOL_DEFS` schemas omit `additionalProperties: false` and do not list
    every property in `required`, so `strict: true` would reject all 24."""
    assert all("strict" not in t for t in _to_openai_tools(_TOOL_DEFS))


# ── dispatch ─────────────────────────────────────────────────────────────────

def test_run_dispatches_to_openai_and_not_the_gemini_fallthrough(monkeypatch):
    """`run()`'s provider branch used to be `if claude: ... else: gemini`, so a
    third provider silently sent Gemini-shaped messages to Gemini's endpoint."""
    r = _runner()
    monkeypatch.setattr(r, "system_prompt", "sys", raising=False)
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        # A snapshot: `_run_openai` appends the response's output items to the
        # SAME list it was passed (the documented contract both other
        # providers follow), so holding the reference would assert against
        # the post-call state.
        seen["payload"] = copy.deepcopy(kw["json"])
        return _Resp(_response(output=[{
            "type": "message",
            "content": [{"type": "output_text", "text": "done"}]}]))

    monkeypatch.setattr("src.skill_runner.requests.post", fake_post)
    assert r.run("hello") == "done"
    assert "api.openai.com" in seen["url"]
    # Responses items, not Gemini `parts`.
    assert seen["payload"]["input"] == [{"role": "user", "content": "hello"}]
    assert seen["payload"]["instructions"] == "sys"


def test_the_api_key_is_a_header_never_a_query_string(monkeypatch):
    """`requests` embeds the URL in every exception it raises, so a key in the
    query string is printed by any connection error — observed live for Gemini."""
    r = _runner()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    seen = {}

    def fake_post(url, **kw):
        seen["url"], seen["headers"] = url, kw.get("headers", {})
        return _Resp(_response(output=[{
            "type": "message",
            "content": [{"type": "output_text", "text": "x"}]}]))

    monkeypatch.setattr("src.skill_runner.requests.post", fake_post)
    r.run("hi")
    assert "sk-secret-value" not in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer sk-secret-value"


# ── refusals ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body,category", [
    (_response(output=[{"type": "message", "content": [
        {"type": "refusal", "refusal": "I can't help with that."}]}]), "refusal"),
    (_response(output=[{"type": "message", "content": []}],
               incomplete_details={"reason": "content_filter"}), "content_filter"),
])
def test_a_declined_request_raises_the_shared_refusal_error(
        body, category, monkeypatch):
    """Must be the SAME error Claude's stop_reason and Gemini's blockReason
    raise, or the cross-provider fallback chain cannot fire and the stage
    writes a 0-byte report that fails three stages later."""
    r = _runner()
    monkeypatch.setattr("src.skill_runner.requests.post",
                        lambda url, **kw: _Resp(body))
    with pytest.raises(SkillRefusedError) as exc:
        r.run("something")
    assert exc.value.category == category
    assert exc.value.iteration == 1


def test_an_incomplete_response_warns_rather_than_refusing(monkeypatch, caplog):
    """A truncation is not a refusal; treating it as one would send the run
    down the fallback chain for a prompt no model will finish either."""
    r = _runner()
    body = _response(output=[{"type": "message", "content": [
        {"type": "output_text", "text": "partial"}]}],
        incomplete_details={"reason": "max_output_tokens"})
    monkeypatch.setattr("src.skill_runner.requests.post",
                        lambda url, **kw: _Resp(body))
    assert r.run("x") == "partial"


# ── token accounting ─────────────────────────────────────────────────────────

def test_cached_tokens_are_not_billed_as_fresh_input(monkeypatch):
    """`input_tokens` INCLUDES the cached and cache-written share (measured:
    a repeated 4,635-token prefix reported input_tokens=4635 with
    cache_write=4632 then cached=4632, the total unchanged).
    `Usage.input_tokens` is the UNCACHED share and `price()` adds all three
    buckets, so failing to subtract bills a cached prefix at 1.0x + 0.1x."""
    r = _runner()
    body = _response(
        output=[{"type": "message",
                 "content": [{"type": "output_text", "text": "ok"}]}],
        usage={"input_tokens": 4635, "output_tokens": 20,
               "input_tokens_details": {"cached_tokens": 4000,
                                        "cache_write_tokens": 600},
               "output_tokens_details": {"reasoning_tokens": 12}})
    monkeypatch.setattr("src.skill_runner.requests.post",
                        lambda url, **kw: _Resp(body))
    r.run("x")
    u = r.usage()
    assert u.input_tokens == 4635 - 4000 - 600
    assert u.cache_read_tokens == 4000
    assert u.cache_creation_tokens == 600
    # reasoning_tokens are ALREADY inside output_tokens; adding them again
    # double-bills every thinking-model call.
    assert u.output_tokens == 20


def test_the_input_ceiling_measures_the_whole_request(monkeypatch):
    """The ceiling is about how large one request was, so it reads the full
    prompt, not the uncached remainder — a 4,635-token request that is 99%
    cached is still a 4,635-token request."""
    r = _runner(max_input_tokens=1000)
    body = _response(
        output=[{"type": "message",
                 "content": [{"type": "output_text", "text": "ok"}]}],
        usage={"input_tokens": 4635, "output_tokens": 5,
               "input_tokens_details": {"cached_tokens": 4630,
                                        "cache_write_tokens": 0},
               "output_tokens_details": {}})
    monkeypatch.setattr("src.skill_runner.requests.post",
                        lambda url, **kw: _Resp(body))
    with pytest.raises(RuntimeError, match="Input token limit exceeded"):
        r.run("x")


# ── tool loop ────────────────────────────────────────────────────────────────

def test_every_output_item_is_replayed_including_reasoning(monkeypatch):
    """Echoing a `function_call` back WITHOUT the `reasoning` item that
    preceded it is a 400 from the API — verified live. Same class of rule as
    Claude's "thinking blocks must carry their signature"."""
    r = _runner()
    calls = []
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": []}
    fc = {"type": "function_call", "name": "search_corpus", "call_id": "c1",
          "id": "fc_1", "arguments": json.dumps({"query": "x"})}

    def fake_post(url, **kw):
        calls.append(kw["json"]["input"])
        if len(calls) == 1:
            return _Resp(_response(output=[reasoning, fc]))
        return _Resp(_response(output=[{"type": "message", "content": [
            {"type": "output_text", "text": "final"}]}]))

    monkeypatch.setattr("src.skill_runner.requests.post", fake_post)
    monkeypatch.setattr(r, "_execute_tool", lambda n, a: json.dumps({"ok": True}))
    assert r.run("go") == "final"
    second = calls[1]
    assert reasoning in second, "reasoning item was dropped from the replay"
    assert fc in second
    assert {"type": "function_call_output", "call_id": "c1",
            "output": json.dumps({"ok": True})} in second


def test_unparseable_tool_arguments_are_fed_back_not_raised(monkeypatch):
    """A malformed argument blob is the model's mistake to correct, not a
    pipeline fault that should abort a multi-stage run."""
    r = _runner()
    seen = []

    def fake_post(url, **kw):
        seen.append(kw["json"]["input"])
        if len(seen) == 1:
            return _Resp(_response(output=[{
                "type": "function_call", "name": "search_corpus",
                "call_id": "c1", "arguments": "{not json"}]))
        return _Resp(_response(output=[{"type": "message", "content": [
            {"type": "output_text", "text": "recovered"}]}]))

    monkeypatch.setattr("src.skill_runner.requests.post", fake_post)
    assert r.run("go") == "recovered"
    fed_back = [m for m in seen[1] if m.get("type") == "function_call_output"]
    assert "not valid JSON" in fed_back[0]["output"]


# ── trace ────────────────────────────────────────────────────────────────────

def test_the_trace_counts_turns_not_items(monkeypatch, tmp_path):
    """Responses items carry `type` and no `role`, so counting roles reported
    "Total LLM calls: 0". Counting every `function_call` instead would
    multiply a turn that requested three tools."""
    r = _runner()
    fc = [{"type": "function_call", "name": "search_corpus", "call_id": f"c{i}",
           "arguments": "{}"} for i in range(3)]

    def fake_post(url, **kw):
        if not any(m.get("type") == "function_call_output"
                   for m in kw["json"]["input"]):
            return _Resp(_response(output=fc))
        return _Resp(_response(output=[{"type": "message", "content": [
            {"type": "output_text", "text": "done"}]}]))

    monkeypatch.setattr("src.skill_runner.requests.post", fake_post)
    monkeypatch.setattr(r, "_execute_tool", lambda n, a: "{}")
    r.run("go", trace_path=tmp_path / "t")
    assert r._count_assistant_turns() == 2      # one tool turn, one final
    rendered = (tmp_path / "t" / "trace_rendered.md").read_text(encoding="utf-8")
    assert "**Total LLM calls:** 2" in rendered
    assert "### Tool Call — `search_corpus`" in rendered
    assert "### Tool Result" in rendered


# ── wiring ───────────────────────────────────────────────────────────────────

def test_the_provider_has_a_declared_api_key():
    """`_require_api_key` treats an UNKNOWN provider as keyless and returns
    silently, so a provider missing from the map surfaces its missing key as a
    raw 401 mid-run instead of a pre-flight error."""
    assert SkillRunner._PROVIDER_KEYS["openai"] == "OPENAI_API_KEY"


def test_a_bare_gpt_model_in_a_fallback_chain_resolves_to_openai():
    """Without a prefix rule a bare `gpt-…` inherits the CURRENT provider and
    gets POSTed to that vendor's endpoint — a 404 that kills the run."""
    from src.pipeline_runner import _split_fallback

    assert _split_fallback("gpt-5.6-terra", "gemini") == ("openai", "gpt-5.6-terra")
    assert _split_fallback("openai:gpt-5.6-luna", "claude") == ("openai", "gpt-5.6-luna")


def test_cli_provider_choices_match_the_runner():
    """A track the parser advertises and the runner rejects, or vice versa."""
    import scripts.run_pipeline as rp
    import scripts.run_skill as rs

    for parser, dest in ((rp._build_parser(), "provider"),
                         (rs._build_parser() if hasattr(rs, "_build_parser")
                          else None, "model")):
        if parser is None:
            continue
        action = next(a for a in parser._actions if a.dest == dest)
        assert "openai" in action.choices
    assert "openai" in rs._DEFAULT_MODELS
