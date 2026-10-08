"""Deterministic eligibility gates for corpus-derived target hypotheses.

`wildcard-expert` proposes hypotheses from graph edges, DepMap correlations and
corpus papers, and states an evidence chain for each. Nothing downstream checks
that chain, and the skill picks its own primary candidate. Observed on a real run
(2026-10-08): the report recommended SHOC2 / KRAS with a PDB entry taken from a
paper about the MRAS-SHOC2-PP1C complex, while its own risk section quoted the
corpus's measurement that MRAS binds SHOC2 more tightly than KRAS (900 nM against
7 uM); and 4 of 41 numbers in five reports appeared in no tool response.

So selection is split. The skill GENERATES candidates and states its preference.
This module decides which candidates are ELIGIBLE, in code the skill cannot argue
with, and applies an operator policy to the eligible ones:

  G1 grounded      every number and DOI in the evidence chain appears in a tool
                   response from the same run (so it was read, not recalled). A
                   corpus-derived hypothesis must state a chain; a canonical
                   candidate need not, but if it states one it is checked;
  G2 structure     a PDB entry the candidate lists contains BOTH named proteins;
  G3 novel         the pair differs from the canonical track's top pair;
  G4 supported     at least one grounded corpus DOI, or a grounded DepMap
                   correlation of at least `MIN_DEPMAP_R`.

Pure functions. Anything that needs the network (RCSB entries) or the identifier
tables is passed in, so the tests run offline. Chain-size designability is NOT
checked here: `pipeline_runner._designable_chain_sizes` already does it downstream
with the real structure, and duplicating it would create a second dispatch.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

MIN_DEPMAP_R = 0.3
ABS_TOL = 0.0006        # values are quoted at up to four decimals
_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"',;)\]]+", re.I)
NUMERIC_KINDS = {"depmap_r", "novelty_score", "quantitative", "mentions"}


def parse_choices(report_text: str) -> list[dict]:
    """The `- choices_json:` array from a report's PIPELINE HANDOFF; [] if absent or invalid."""
    m = re.search(r"^-\s*choices_json:\s*(\[.*\])\s*$", report_text, re.M)
    if not m:
        return []
    try:
        out = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    return [c for c in out if isinstance(c, dict)]


def numbers_in(text: str) -> list[float]:
    return [float(x) for x in _NUM.findall(text)]


def _first_number(value) -> float | None:
    m = _NUM.search(str(value))
    return float(m.group(0)) if m else None


def number_grounded(value, numbers: Iterable[float]) -> bool:
    """True if `value` matches some tool-returned number, sign-insensitively, to four decimals."""
    x = _first_number(value)
    if x is None:
        return False
    return any(abs(abs(n) - abs(x)) <= ABS_TOL for n in numbers)


def doi_grounded(doi: str, responses_text: str) -> bool:
    d = str(doi).strip().lower().rstrip(".")
    return bool(d) and d in responses_text.lower()


def pair_key(complex_name: str, resolve: Callable[[str], str]) -> frozenset:
    parts = [p.strip() for p in re.split(r"\s*/\s*", str(complex_name)) if p.strip()]
    return frozenset(resolve(p) for p in parts)


@dataclass
class GateResult:
    complex: str
    track: str
    checks: dict = field(default_factory=dict)      # gate -> (passed, reason)

    @property
    def eligible(self) -> bool:
        return all(ok for ok, _ in self.checks.values())

    @property
    def failed(self) -> list[str]:
        return [g for g, (ok, _) in self.checks.items() if not ok]


def check_evidence_chain(cand: dict, responses: dict[str, str]) -> tuple[bool, str, dict]:
    """G1. `responses` maps tool name -> concatenated response text for the run."""
    chain = cand.get("evidence_chain") or []
    if not chain:
        # A canonical candidate rests on consensus knowledge and need not state a chain;
        # a corpus-derived hypothesis exists only because of its evidence, so it must.
        if cand.get("track") == "canonical":
            return True, "canonical candidate: no chain required", {"grounded": 0, "total": 0}
        return False, "no evidence_chain stated", {"grounded": 0, "total": 0}
    everything = "\n".join(responses.values())
    all_numbers = numbers_in(everything)
    bad, grounded = [], 0
    for item in chain:
        if not isinstance(item, dict):
            bad.append(f"malformed item {item!r}")
            continue
        kind, value, tool = item.get("kind"), item.get("value"), item.get("tool")
        text = responses.get(tool, "") if tool in responses else everything
        nums = numbers_in(text) if tool in responses else all_numbers
        if kind in NUMERIC_KINDS:
            ok = number_grounded(value, nums)
        elif kind == "doi":
            ok = doi_grounded(value, everything)
        elif kind == "graph_path":
            # The model may write the path as a JSON list (["KRAS", "SHOC2"]) or as "A -> B -> C".
            # Stringifying a list leaves brackets and quotes on the symbols, which then match nothing
            # (observed: honest paths rejected as "not found in any tool response").
            raw = value if isinstance(value, (list, tuple)) else re.split(r"->|→|/|,|\s-\s", str(value))
            syms = [re.sub(r"[^A-Za-z0-9_.]", "", str(x)) for x in raw]
            syms = [x for x in syms if x]
            ok = bool(syms) and all(re.search(rf"\b{re.escape(x)}\b", text, re.I) for x in syms)
        else:
            ok = False
        grounded += ok
        if not ok:
            bad.append(f"{kind}={value!s:.40}")
    detail = {"grounded": grounded, "total": len(chain)}
    return (not bad), ("all grounded" if not bad else "not found in any tool response: " + "; ".join(bad)), detail


def entry_names_both(entry: dict, a: str, b: str, resolve: Callable[[str], str]) -> bool:
    """G2 core: does an RCSB entry (`genes`, `descriptions`) contain both proteins?"""
    genes = {resolve(g) for g in entry.get("genes", [])}
    desc = " ".join(entry.get("descriptions", [])).lower()

    def has(name: str) -> bool:
        return resolve(name) in genes or (len(name) >= 4 and name.lower() in desc)
    return has(a) and has(b)


def check_structure(cand: dict, entries: dict[str, dict], resolve: Callable[[str], str]) -> tuple[bool, str]:
    parts = [p.strip() for p in re.split(r"\s*/\s*", str(cand.get("complex", ""))) if p.strip()]
    if len(parts) != 2:
        return False, "complex is not a two-protein pair"
    ids = [str(i).upper() for i in cand.get("pdb_ids") or []]
    if not ids:
        return False, "no PDB entry listed"
    known = [i for i in ids if entries.get(i, {}).get("exists", True) and i in entries]
    if not known:
        return False, f"none of {ids} could be looked up"
    for i in known:
        if entry_names_both(entries[i], parts[0], parts[1], resolve):
            return True, f"{i} contains both"
    return False, f"no listed entry ({', '.join(known)}) contains both {parts[0]} and {parts[1]}"


def evaluate(cands: list[dict], responses: dict[str, str], entries: dict[str, dict],
             resolve: Callable[[str], str]) -> list[GateResult]:
    canon = [c for c in cands if c.get("track") == "canonical"]
    canon_top = pair_key(canon[0]["complex"], resolve) if canon else None
    out = []
    for c in cands:
        r = GateResult(complex=str(c.get("complex", "")), track=str(c.get("track", "")))
        g1 = check_evidence_chain(c, responses)
        r.checks["grounded"] = (g1[0], g1[1])
        r.checks["structure"] = check_structure(c, entries, resolve)
        if c.get("track") == "corpus_derived":
            same = canon_top is not None and pair_key(c.get("complex", ""), resolve) == canon_top
            r.checks["novel"] = (not same, "same pair as the canonical top pick" if same else "differs from canonical top pick")
            chain = c.get("evidence_chain") or []
            everything = "\n".join(responses.values())
            doi_ok = any(i.get("kind") == "doi" and doi_grounded(i.get("value", ""), everything) for i in chain if isinstance(i, dict))
            nums = numbers_in(everything)
            dep_ok = any(i.get("kind") == "depmap_r" and (_first_number(i.get("value")) or 0) >= MIN_DEPMAP_R
                         and number_grounded(i.get("value"), nums) for i in chain if isinstance(i, dict))
            r.checks["supported"] = (doi_ok or dep_ok,
                                     "grounded corpus DOI" if doi_ok else f"grounded DepMap r >= {MIN_DEPMAP_R}" if dep_ok
                                     else f"no grounded DOI and no grounded DepMap r >= {MIN_DEPMAP_R}")
        out.append(r)
    return out


POLICIES = ("novel_if_eligible", "canonical", "skill_preference")


def select_forward(cands: list[dict], results: list[GateResult], policy: str = "novel_if_eligible",
                   skill_preference: str | None = None) -> dict:
    """Pick the candidate to hand to the next stage. Always explains itself.

    novel_if_eligible  best eligible corpus-derived candidate; else the first eligible
                       canonical one; else the skill's own preference, flagged as ungated.
    canonical          first eligible canonical candidate; else as above.
    skill_preference   the skill's choice if it is eligible; else novel_if_eligible.
    """
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}")
    by = {id(c): r for c, r in zip(cands, results)}
    eligible = [c for c in cands if by[id(c)].eligible]
    novel = [c for c in eligible if c.get("track") == "corpus_derived"]
    canon = [c for c in eligible if c.get("track") == "canonical"]

    def rank(c):
        chain = c.get("evidence_chain") or []
        return (-len(chain), -(c.get("novelty_score") or 0.0), cands.index(c))

    pref = next((c for c in cands if skill_preference and c.get("complex") == skill_preference), None)
    if policy == "skill_preference" and pref is not None and by[id(pref)].eligible:
        chosen, why = pref, "the skill's preference passed every gate"
    elif policy == "canonical" and canon:
        chosen, why = canon[0], "policy=canonical: first eligible canonical candidate"
    elif novel:
        chosen, why = sorted(novel, key=rank)[0], "best eligible corpus-derived hypothesis (most grounded evidence, then novelty)"
    elif canon:
        chosen, why = canon[0], "no corpus-derived hypothesis passed the gates; fell back to the first eligible canonical candidate"
    else:
        chosen, why = pref or (cands[0] if cands else None), "NO candidate passed the gates; the skill's own preference is forwarded UNGATED"
    return {
        "chosen": chosen.get("complex") if chosen else None,
        "track": chosen.get("track") if chosen else None,
        "gated": bool(chosen and by[id(chosen)].eligible),
        "reason": why,
        "eligible": [c.get("complex") for c in eligible],
        "rejected": {r.complex: r.failed for r in results if not r.eligible},
        "policy": policy,
    }
