"""Shared pieces for the Phase 2 scoring, judging and analysis scripts.

Plan and decision rules: docs/phase2_corpus_eval.md (written before any output).
Run everything with `.venv/bin/python` (3.12); 3.13 rejects the corporate
proxy's certificate and every API call fails before billing.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import random
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

OUT = ROOT / "outputs" / "phase2"
JUDGE_DIR = OUT / "judgements"
ARMS = ("live", "blank", "notools", "wildcard1", "wildcard2")
SALT = "phase2-2026-10-07"
PHASE2_PROMPTS = frozenset({"sting", "alt", "tau", "kras", "ra"})   # the pre-registered five; later prompts are not in Phase 2 analyses

DOI_RE = re.compile(r"\b(10\.\d{4,9}/(?:[^\s,;:()\[\]{}\"'<>]|\([^\s()]*\))+)")
_TAIL_SECTIONS = re.compile(r"\n#{1,3}\s*(CITATION VERIFICATION|MODEL PROVENANCE).*", re.S | re.I)

JUDGES = {
    # name: (provider, model id). Claude is a different family from the Gemini
    # generator; OpenAI is the second judge for agreement.
    "claude": ("anthropic", "claude-sonnet-5"),
    "openai": ("openai", "gpt-5.6-terra"),
}


def clean_doi(d: str) -> str:
    return d.rstrip(".*_`").lower()


def cells():
    """[(prompt, rep, arm, meta)] for every finished cell, from the metadata files."""
    out = []
    for p in sorted(OUT.glob("*__*.json")):
        stem = p.stem
        name, arm = stem.split("__")
        if arm not in ARMS or "_" not in name:
            continue
        prompt, rep = name.rsplit("_", 1)
        meta = json.loads(p.read_text(encoding="utf-8"))
        if meta.get("error"):
            continue
        out.append((prompt, rep, arm, meta))
    return out


def report_text(prompt: str, rep: str, arm: str, strip: bool = True) -> str:
    text = (OUT / f"{prompt}_{rep}__{arm}.md").read_text(encoding="utf-8")
    return _TAIL_SECTIONS.sub("", text).strip() if strip else text


def trace_stats(prompt: str, rep: str, arm: str) -> dict:
    """What the corpus tools actually returned in this run, from its trace."""
    tr = json.loads((OUT / f"{prompt}_{rep}__{arm}.trace" / "trace_raw.json").read_text(encoding="utf-8"))
    nonempty_fp = empty = nonempty_search = 0
    for msg in tr:
        for part in msg.get("parts", []):
            fr = part.get("functionResponse")
            if not fr or fr["name"] not in ("get_fingerprint", "search_corpus"):
                continue
            body = (fr.get("response") or {}).get("result", "")
            if "No results found in the corpus" in body:
                empty += 1
            elif fr["name"] == "get_fingerprint":
                nonempty_fp += 1
            else:
                nonempty_search += 1
    return {"fingerprints_returned": nonempty_fp, "search_calls_with_hits": nonempty_search, "empty_corpus_responses": empty}


def stated_coverage(text: str) -> dict:
    """The report's own CORPUS COVERAGE line: papers analysed / search hits, if it gives numbers."""
    m = re.search(r"Papers analy[sz]ed:\s*(\d+)\s*fingerprints?\s*from\s*(\d+)", text, re.I)
    return {"stated_fingerprints": int(m.group(1)), "stated_hits": int(m.group(2))} if m else {"stated_fingerprints": None, "stated_hits": None}


def blind_id(prompt: str, rep: str, arm: str) -> str:
    return hashlib.sha1(f"{SALT}:{prompt}_{rep}__{arm}".encode()).hexdigest()[:8]


def prompt_dois() -> set[str]:
    out: set[str] = set()
    for f in (ROOT / "skills").glob("*/SKILL.md"):
        out |= {clean_doi(m) for m in DOI_RE.findall(f.read_text(encoding="utf-8", errors="ignore"))}
    return out


# --------------------------------------------------------------------------
# claims
# --------------------------------------------------------------------------

def claim_lines(text: str, excluded: set[str]) -> list[dict]:
    """Report lines that carry at least one DOI, as {line, dois}."""
    out = []
    for raw in text.splitlines():
        dois = [clean_doi(d) for d in DOI_RE.findall(raw)]
        dois = [d for d in dict.fromkeys(dois) if d not in excluded]
        line = re.sub(r"[*_`#]+", "", raw).strip(" -|\t")
        if dois and len(line) > 40:
            out.append({"line": line[:700], "dois": dois})
    return out


def sample_claims(prompt: str, rep: str, arm: str, k: int = 8) -> list[dict]:
    claims = claim_lines(report_text(prompt, rep, arm), prompt_dois())
    rng = random.Random(f"{SALT}:{prompt}_{rep}__{arm}")
    return rng.sample(claims, min(k, len(claims)))


# --------------------------------------------------------------------------
# evidence for a cited DOI
# --------------------------------------------------------------------------

EVIDENCE = OUT / "evidence.json"


def _fp_index() -> dict:
    idx = {}
    for f in (ROOT / "data" / "fingerprints").glob("doi_*.json"):
        idx[f.stem[4:].replace("_", "/", 1).lower()] = f
    return idx


def fetch_evidence(dois: list[str]) -> dict[str, dict]:
    """{doi: {title, abstract, fingerprint, source}} - cached on disk."""
    import requests
    cache = json.loads(EVIDENCE.read_text()) if EVIDENCE.exists() else {}
    idx = _fp_index()
    s = requests.Session()
    todo = [d for d in dois if d not in cache]
    for d in todo:
        e = {"title": "", "abstract": "", "fingerprint": "", "source": []}
        try:
            r = s.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                      params={"query": f'DOI:"{d}"', "resultType": "core", "format": "json"}, timeout=25)
            res = (r.json().get("resultList") or {}).get("result") or []
            if res:
                e["title"] = re.sub(r"<[^>]+>", "", res[0].get("title") or "")
                e["abstract"] = re.sub(r"<[^>]+>", "", res[0].get("abstractText") or "")
                if e["abstract"]:
                    e["source"].append("europepmc")
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            e["error"] = str(exc)[:100]
        if d in idx:
            fp = json.loads(idx[d].read_text(encoding="utf-8"))
            pm = fp.get("paper_metadata") or {}
            e["title"] = e["title"] or pm.get("title", "")
            kf = "; ".join(k.get("claim", "") for k in fp.get("key_findings", []))
            e["fingerprint"] = (pm.get("situational_context_hook", "") + " Findings: " + kf)[:2500]
            e["source"].append("fingerprint")
        cache[d] = e
        time.sleep(0.1)
    if todo:
        EVIDENCE.write_text(json.dumps(cache, indent=1))
    return cache


# --------------------------------------------------------------------------
# judge clients
# --------------------------------------------------------------------------

class Refused(Exception):
    """The judge declined. Recorded as missing; NOT retried on another model."""


def _usd(model: str, inp: int, out: int, cached: int = 0) -> float:
    from src.token_budget import Usage, load_pricing, price
    import yaml
    load_pricing(yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")))
    return price(model, Usage(input_tokens=max(inp - cached, 0), output_tokens=out, cache_read_tokens=cached))


def call_judge(judge: str, system: str, user: str, max_tokens: int = 1200, think: bool = True) -> dict:
    """One judge call -> {text, usd, input_tokens, output_tokens}. Raises Refused."""
    from src.env_config import load_env
    load_env()
    provider, model = JUDGES[judge]
    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic()
        # Headroom for adaptive thinking: at 500 tokens a long rubric prompt spent the whole budget
        # thinking and returned no text block (found in the smoke test).
        kw = {} if think else {"thinking": {"type": "disabled"}}
        msg = client.messages.create(model=model, max_tokens=max_tokens + (3000 if think else 0), system=system,
                                     messages=[{"role": "user", "content": user}], **kw)
        if msg.stop_reason == "refusal":
            raise Refused(f"{model} stop_reason=refusal")
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        u = msg.usage
        inp = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        return {"text": text, "input_tokens": inp, "output_tokens": u.output_tokens,
                "usd": _usd(model, inp, u.output_tokens, getattr(u, "cache_read_input_tokens", 0) or 0)}
    import requests
    payload = {"model": model, "instructions": system, "input": user,
               "max_output_tokens": max_tokens + 3000, "reasoning": {"effort": "low"}}
    for attempt in range(3):
        r = requests.post("https://api.openai.com/v1/responses", json=payload, timeout=180,
                          headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
        if r.status_code in (429, 500, 502, 503):
            time.sleep(5 * (attempt + 1))
            continue
        break
    r.raise_for_status()
    body = r.json()
    if (body.get("incomplete_details") or {}).get("reason") == "content_filter":
        raise Refused(f"{model} content_filter")
    parts = [c for o in body.get("output", []) if o.get("type") == "message" for c in o.get("content", [])]
    if any(c.get("type") == "refusal" for c in parts):
        raise Refused(f"{model} refusal part")
    text = "".join(c.get("text", "") for c in parts if c.get("type") == "output_text")
    u = body.get("usage") or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
    return {"text": text, "input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
            "usd": _usd(model, u.get("input_tokens", 0), u.get("output_tokens", 0), cached)}


def parse_json(text: str) -> dict | None:
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
# resumable JSONL store
# --------------------------------------------------------------------------

class Store:
    def __init__(self, task: str, judge: str):
        JUDGE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = JUDGE_DIR / f"{task}__{judge}.jsonl"
        self.rows = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    self.rows[r["key"]] = r

    def has(self, key: str) -> bool:
        return key in self.rows

    def add(self, row: dict) -> None:
        self.rows[row["key"]] = row
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def spent(self) -> float:
        return sum(r.get("usd", 0) for r in self.rows.values())
