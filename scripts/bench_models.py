#!/usr/bin/env python3
"""Is gemini-3.8-flash worth switching the pipeline default to?

Per-token pricing is identical to gemini-3.7-flash, so the two questions are
"how many tokens does each spend on the same work" and "is the work any good".
Neither is answerable from a changelog, and the second is the one that costs
real money if it is wrong: an interface stage that emits an ungrounded hotspot
table does not fail cheaply, it fails after a multi-day GPU campaign has been
sized against the wrong residues.

So both suites here run the PRODUCTION code path — `PipelineRunner`'s own
stage methods, guards included — and score the output against things that are
objectively checkable rather than against a rubric:

  interface   `--workflow structure`'s single LLM stage
              (`complex-structure-analysis`) on four structures already in the
              checkout. Stage 0 of that track is deterministic, so the model's
              whole contribution is the epitope, and every claim it makes about
              the epitope can be checked against the coordinates: does the
              residue NAME at each auth_seq_id match the file, do the stated
              `rfd3_atoms` exist on that residue, is the label_seq_id the one
              gemmi computes, did it keep the chain assignment that was
              measured for it. The headline is blunter than any of those:
              would `_stage_binder_interface` have let the campaign proceed.

  pathway     `pathway-expert` on three queries spanning a ~80x range of
              corpus density. Checkable: every DOI it cites either is or is not
              in the fingerprint store (the pipeline's own
              `_verify_citations`), every gene symbol it names either does or
              does not resolve through `identifier_normalizer`, and the
              handoff either does or does not carry the fields the next stage
              parses.

The structures are the ones the repo has documented failures on, not a random
sample — 8ZNL is where a model returned another PD-L1 structure's textbook
numbering, 6E3Y is where one analysed the agonist peptide instead of the
receptor, 3KYS carries a palmitoylated cysteine that residue-name filtering
deletes. If a model is going to be wrong about a structure, it is wrong about
one of these.

    python scripts/bench_models.py --dry-run     # matrix + cost estimate
    python scripts/bench_models.py --suite interface
    python scripts/bench_models.py               # everything, resumable
    python scripts/bench_models.py --report      # re-print from disk

Cells are cached on their artifacts, so an interrupted matrix resumes for free.
No GPU, nothing written into `projects/`. Each cell carries its own hard
`--cell-cap` so one runaway agentic loop cannot eat the whole budget.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

OUT = _ROOT / "outputs" / "model_bench"

MODELS = ("gemini-3.7-flash", "gemini-3.8-flash")

# (slug, pdb_id, uniprot, brief). `uniprot` re-arms `_verify_target_chain_
# assignment`; it is only supplied where the accession has been CONFIRMED
# against the file (7CZD chain B aligns to Q9NZQ7 at 100% identity, chain A at
# 20%). Elsewhere it is left blank and that guard fails open, exactly as it
# does for an operator's own construct — asserting an accession we have not
# verified would manufacture failures instead of measuring them.
INTERFACE_CASES = [
    ("pdl1_7czd", "7CZD", "Q9NZQ7",
     "Disrupt this interface: select model-ready hotspots on the target chain "
     "for a mini-protein binder."),
    # The canonical-numbering trap: asked for 8ZNL, a model once returned
    # 7CZD's textbook PD-L1 hotspots (Tyr56, Gln66, ...) verbatim, and chain B
    # residue 56 here is not TYR.
    ("pdl1_8znl", "8ZNL", "",
     "Disrupt this interface: select model-ready hotspots on the target chain "
     "for a mini-protein binder."),
    # YAP1/TEAD1. Carries P1L (S-palmitoyl-cysteine) at A344, and the whole
    # TEAD-inhibitor literature is about that lipid pocket.
    ("yap_tead_3kys", "3KYS", "",
     "Disrupt this interface: select model-ready hotspots on the target chain "
     "for a mini-protein binder."),
    # CALCRL/RAMP1 + CGRP + Gs + Nb35. Seven chains, a membrane target, and
    # the entry where a stage analysed the 38-residue agonist peptide and
    # renamed the complex to match.
    ("calcrl_6e3y", "6E3Y", "",
     "Disrupt this interface: select model-ready hotspots on the target chain "
     "for a mini-protein binder."),
]

# Same three queries as `scripts/ablate_corpus.py`'s live arm, chosen there to
# span measured corpus density (chromatin 15.8% of the curated set, pain 3.9%,
# fibrosis 0.2%). Reused deliberately: that experiment's cells are on disk for
# 3.7, so an unexpected result here can be read against them.
PATHWAY_CASES = [
    ("chromatin", "Design novel protein therapeutics targeting chromatin "
                  "regulators that drive treatment resistance in acute myeloid "
                  "leukaemia."),
    ("pain", "design novel inhibitors for pain receptors that are promising "
             "drug targets"),
    ("fibrosis", "Design novel inhibitors for idiopathic pulmonary fibrosis "
                 "targets."),
]

# Fields the NEXT stage parses off each handoff. A missing one is not a style
# problem: `_stage_trim` reads target_chain, the RFD3 spec reads bsa_A2, and
# `_select_designable_structure` reads pdb_id.
INTERFACE_REQUIRED = ("pdb_id", "target_chain", "partner_chain", "design_intent",
                      "bsa_A2", "tractability")
PATHWAY_REQUIRED = ("pdb_id", "target_complex", "design_intent", "structure_query")

# Measured medians from `projects/*/ledger.jsonl` on gemini-3.7-flash:
# interface $0.18 (n=6), pathway $0.22 (n=9). Used only for the estimate.
EST_USD = {"interface": 0.22, "pathway": 0.25}


# ── objective checks ─────────────────────────────────────────────────────────

def atom_errors(residues: list[dict], cif_path: str, chain: str) -> list[str]:
    """Hotspots whose stated `rfd3_atoms` do not exist on that residue.

    `validate_spec` applies exactly this rule downstream — atoms are checked
    against the atoms gemmi actually finds at that (chain, auth_seq_id), not
    against a canonical table — and it is how the 8ZNL incident was caught at
    all, though only by luck: the memorised atoms happened not to exist on the
    valine really there. Scoring it directly means the next such case does not
    depend on luck.

    Checking against a preferred-pair table instead was tried and is wrong: the
    interface skill is asked to PREFER two atoms per residue, not restricted to
    them, so `HIS69: ND1,NE2` scored as an error while being a perfectly real
    histidine sidechain.
    """
    if not residues or not cif_path:
        return []
    try:
        import gemmi
        st = gemmi.read_structure(cif_path)
        st.setup_entities()
        present = {(ch.name, int(res.seqid.num)): {a.name for a in res}
                   for ch in st[0] for res in ch}
    except Exception:                               # noqa: BLE001
        return []
    bad = []
    for h in residues:
        auth = h.get("auth_seq_id")
        have = present.get((chain, auth))
        if have is None:
            bad.append(f"{h.get('residue')}{auth}:no-such-residue")
            continue
        stated = [a.strip().upper() for a in
                  str(h.get("rfd3_atoms", "")).replace(";", ",").split(",")
                  if a.strip()]
        absent = [a for a in stated if a not in have]
        if absent:
            bad.append(f"{h.get('residue')}{auth}:{'+'.join(absent)}")
    return bad


def unresolvable_genes(names: list[str]) -> list[str]:
    """Gene symbols the pipeline's own resolver cannot place.

    A pathway report whose targets do not resolve is not a cheaper answer, it
    is an answer the identifier layer will drop on the floor — and the resolver
    deliberately REFUSES an ambiguous synonym rather than guessing, so this
    measures naming discipline, not vocabulary size.
    """
    try:
        from src.identifier_normalizer import get_normalizer
        norm = get_normalizer()
    except Exception:
        return []       # reference data absent in this checkout; not scored
    out = []
    for n in names:
        try:
            if not norm.resolve(n).human_gene_symbol:
                out.append(n)
        except Exception:               # noqa: BLE001
            out.append(n)
    return out


def citation_check(text: str) -> dict:
    """Read back the `## CITATION VERIFICATION` block `_run_stage` appended.

    The pipeline already resolves every cited DOI against the fingerprint
    store, so the hallucinated-citation rate is a number it computes rather
    than one this script has to re-derive — same reason the report generators
    read `report_common` instead of re-parsing stage output their own way.
    """
    m = re.search(r"##\s*CITATION VERIFICATION(.*)", text, re.DOTALL)
    if not m:
        return {}
    block = m.group(1)
    def num(pat):
        g = re.search(pat, block)
        return int(g.group(1)) if g else None
    return {"cited": num(r"Citations checked:\s*(\d+)"),
            "in_corpus": num(r"Verified in corpus:\s*(\d+)"),
            "not_in_corpus": num(r"NOT IN CORPUS \((\d+)\)") or 0}


def tier_genes(text: str) -> list[str]:
    """Gene symbols from the tier table. Same parse as `scripts/ablate_corpus.py`,
    plus one repair that experiment did not need.

    Parentheticals are stripped BEFORE splitting on "/". A heading written as
    `MLL1 (MEN1/KMT2A)` otherwise splits into the fragments `MLL1 (MEN1` and
    `KMT2A)`, neither of which resolves — so the resolvability check scored a
    model's choice of PUNCTUATION as a naming failure. It was measuring this
    parser, not the report: `Menin`, `MLL1`, `TrkA` and `Nav1.7` all resolve
    through `identifier_normalizer`'s alias tiers once the parenthesis is gone.
    """
    if "TARGET OPPORTUNITY LANDSCAPE" not in text:
        return []
    body = text.split("TARGET OPPORTUNITY LANDSCAPE")[1].split("### PRIMARY")[0]
    seen, out = set(), []
    for block in re.split(r"\n#### ", body)[1:]:
        head = block.splitlines()[0].strip()
        m = re.match(r"\[([A-Z_ ]+)\]\s*(.+)", head)
        if not m:
            continue
        name = re.sub(r"\([^)]*\)", " ", m.group(2))
        for g in re.split(r"\s*/\s*", name):
            g = g.strip(" -,;")
            if g and g not in seen:
                seen.add(g)
                out.append(g)
    return out


def trace_calls(run_dir: pathlib.Path) -> dict:
    """Tool-call counts from the trace dump `_run_stage` writes.

    Token spend and tool-call count are different questions: a model can be
    cheap because it is efficient or because it stopped looking. Gemini nests
    calls under `parts[].functionCall`.
    """
    from collections import Counter
    names: list[str] = []
    for raw in run_dir.rglob("trace_raw.json"):
        try:
            msgs = json.loads(raw.read_text(encoding="utf-8"))
        except Exception:
            continue
        for msg in msgs:
            for part in (msg.get("parts") or msg.get("content") or []):
                if not isinstance(part, dict):
                    continue
                call = part.get("functionCall")
                name = ((call or {}).get("name") if call else
                        (part.get("name") if part.get("type") == "tool_use" else None))
                if name:
                    names.append(name)
    return {"n_tool_calls": len(names), "by_tool": dict(Counter(names))}


# ── the two suites ───────────────────────────────────────────────────────────

def _ledger_for(cell_dir: pathlib.Path, cap: float):
    from src.token_budget import TokenLedger
    path = cell_dir / "ledger.jsonl"
    path.unlink(missing_ok=True)   # a re-run must not inherit the last try's spend
    return TokenLedger(path, cap_usd=cap, mode="hard")


def _spend(ledger) -> dict:
    """Cost and the four token buckets, plus which model actually served.

    `served_model` is not decoration: a safety refusal walks
    `models.gemini.refusal_fallbacks` into claude-opus-5, and a cell billed to
    Opus while the table says "3.8" would put a $5 benchmark $20 over and read
    as a token-efficiency regression.
    """
    total = ledger.totals()
    return {
        "usd": round(sum(e.usd for e in ledger.entries), 4),
        "usage": total.as_dict(),
        "served_models": sorted({e.model for e in ledger.entries}),
        "n_calls_recorded": len(ledger.entries),
    }


def run_interface(case, model, config, cap) -> dict:
    """One structure, one model, through the real stage and all its guards."""
    from src.pipeline_runner import PipelineResult, PipelineRunner

    slug, pdb_id, uniprot, brief = case
    cell = OUT / f"interface__{slug}__{model}"
    cell.mkdir(parents=True, exist_ok=True)

    runner = PipelineRunner(config=config, provider="gemini", model_id=model,
                            workflow="structure", uniprot=uniprot or None,
                            capture_traces=True)
    runner._ledger = _ledger_for(cell, cap)
    dirs = runner._binder_dirs(cell)
    result = PipelineResult(run_dir=cell)

    # The pre-correction text, captured on the way past. `_correct_label_seq_ids`
    # rewrites the label_seq_id column in place, so scoring the artifact on disk
    # would score the pipeline's repair of the model's answer instead of the
    # answer — and label_seq_id is the column models most reliably get wrong
    # (23/23 on 5GRS, all off by the same amount).
    raw: dict[str, str] = {}
    inner = runner._correct_label_seq_ids

    def spy(text, report_path, cif_path, target_chain):
        raw["text"], raw["cif"], raw["chain"] = text, str(cif_path), target_chain
        return inner(text, report_path, cif_path, target_chain)

    runner._correct_label_seq_ids = spy

    t0 = time.time()
    err = None
    intel: dict = {}
    try:
        intel = runner._stage_structure_intel(pdb_id, brief, dirs, result,
                                              uniprot=uniprot, chains="")
        runner._stage_binder_interface(intel, dirs, result)
    except Exception as exc:                    # noqa: BLE001 — recorded, not raised
        err = f"{type(exc).__name__}: {exc}"

    meta = {"suite": "interface", "slug": slug, "pdb_id": pdb_id, "model": model,
            "uniprot": uniprot, "seconds": round(time.time() - t0, 1),
            "production_accepted": err is None, "error": err,
            **_spend(runner._ledger), **trace_calls(cell)}

    text = raw.get("text", "")
    if text:
        # The scorers below run on the model's OWN table, and one of them was
        # already found wrong once mid-benchmark. Keeping the pre-correction
        # text means the next scorer fix is a `--rescore`, not another $2.
        (cell / "raw_interface.md").write_text(text, encoding="utf-8")
    if not text:
        report = dirs["binder"] / runner._BINDER_STAGE_FILES["interface"]
        text = report.read_text(encoding="utf-8") if report.is_file() else ""
    handoff = runner._parse_handoff(text)
    hotspots_json = runner._parse_hotspot_residues(text, handoff) if text else None
    residues = json.loads(hotspots_json)["residues"] if hotspots_json else []

    # `_resolve_unverified_label_seq_ids` is pure — it returns the corrected
    # text and a warning per row it disagreed with — so it doubles as the
    # scorer for the two columns it exists to police.
    warns: list[str] = []
    if text and raw.get("cif"):
        try:
            _fixed, warns = runner._resolve_unverified_label_seq_ids(
                text, pathlib.Path(raw["cif"]), raw.get("chain") or "A")
        except Exception as exc:                # noqa: BLE001
            warns = [f"label_seq resolution failed: {exc}"]

    meta.update({
        "n_hotspots": len(residues),
        "handoff_missing": [f for f in INTERFACE_REQUIRED if not handoff.get(f)],
        "kept_measured_target_chain": bool(
            handoff.get("target_chain")
            and handoff["target_chain"] == intel.get("target_chain")),
        "measured_target_chain": intel.get("target_chain"),
        "reported_target_chain": handoff.get("target_chain"),
        "n_name_mismatch": sum(1 for w in warns if "NAME mismatch" in w
                               or "not present in structure" in w),
        "n_label_seq_disagreed": sum(1 for w in warns if "label_seq" in w.lower()
                                     and "NAME mismatch" not in w),
        "warnings": warns,
        "bad_atoms": atom_errors(residues, raw.get("cif", ""),
                                 raw.get("chain") or "A"),
    })
    (cell / "cell.json").write_text(json.dumps(meta, indent=2) + "\n",
                                    encoding="utf-8")
    return meta


def run_pathway(case, model, config, cap) -> dict:
    """One query, one model, through `pathway-expert` and the citation check."""
    from src.pipeline_runner import PipelineRunner

    slug, query = case
    cell = OUT / f"pathway__{slug}__{model}"
    cell.mkdir(parents=True, exist_ok=True)

    runner = PipelineRunner(config=config, provider="gemini", model_id=model,
                            capture_traces=True)
    runner._ledger = _ledger_for(cell, cap)
    out = cell / "00_pathway.md"

    t0 = time.time()
    err, handoff = None, {}
    try:
        handoff = runner._run_stage("pathway-expert", query, [], out,
                                    stage="pathway")
    except Exception as exc:                    # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"

    text = out.read_text(encoding="utf-8") if out.is_file() else ""
    genes = tier_genes(text)
    meta = {"suite": "pathway", "slug": slug, "model": model,
            "seconds": round(time.time() - t0, 1),
            "production_accepted": err is None, "error": err,
            **_spend(runner._ledger), **trace_calls(cell),
            "primary": (handoff.get("target_complex") or ""),
            "primary_pair": sorted(g.strip().upper() for g in
                                   re.split(r"\s*/\s*",
                                            handoff.get("target_complex") or "")
                                   if g.strip()),
            "genes": genes,
            "unresolvable_genes": unresolvable_genes(genes),
            "handoff_missing": [f for f in PATHWAY_REQUIRED if not handoff.get(f)],
            "citations": citation_check(text),
            "report_chars": len(text)}
    (cell / "cell.json").write_text(json.dumps(meta, indent=2) + "\n",
                                    encoding="utf-8")
    return meta


SUITES = {
    "interface": (INTERFACE_CASES, run_interface),
    "pathway": (PATHWAY_CASES, run_pathway),
}


def cell_path(suite: str, slug: str, model: str) -> pathlib.Path:
    return OUT / f"{suite}__{slug}__{model}" / "cell.json"


def cell(suite, case, model, config, cap, force) -> dict:
    slug = case[0]
    path = cell_path(suite, slug, model)
    if path.is_file() and not force:
        print(f"  {suite}/{slug}/{model}: cached")
        return json.loads(path.read_text(encoding="utf-8"))
    _cases, fn = SUITES[suite]
    meta = fn(case, model, config, cap)
    flag = "ok " if meta["production_accepted"] else "REJ"
    print(f"  {suite}/{slug}/{model}: {flag} ${meta['usd']:.4f}  "
          f"{meta['seconds']:.0f}s  {meta.get('n_tool_calls', 0)} calls  "
          f"in={meta['usage']['input_tokens']:,} out={meta['usage']['output_tokens']:,}"
          + (f"  [{meta['error'][:70]}]" if meta.get("error") else ""))
    return meta


def rescore() -> int:
    """Re-derive every artifact-based metric from the reports already on disk.

    A benchmark's scorers get corrected while it runs — two of these did — and
    without this the choice is between re-paying for the calls and shipping a
    table scored two different ways. Cost, timing, token buckets and
    `served_models` are measurements of the call and are preserved untouched;
    everything downstream of the artifact is recomputed.

    Interface cells are rescored only where `raw_interface.md` exists: the
    report on disk has had its label_seq_id column repaired in place by
    `_correct_label_seq_ids`, so scoring that file would score the pipeline's
    repair rather than the model's answer.
    """
    import yaml

    from src.pipeline_runner import PipelineResult, PipelineRunner

    config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    n = 0
    for suite, (cases, _fn) in SUITES.items():
        for case in cases:
            for model in MODELS:
                path = cell_path(suite, case[0], model)
                if not path.is_file():
                    continue
                meta = json.loads(path.read_text(encoding="utf-8"))
                cell_dir = path.parent
                if suite == "pathway":
                    md = cell_dir / "00_pathway.md"
                    if not md.is_file():
                        continue
                    text = md.read_text(encoding="utf-8")
                    genes = tier_genes(text)
                    meta.update(genes=genes,
                                unresolvable_genes=unresolvable_genes(genes),
                                citations=citation_check(text),
                                report_chars=len(text))
                else:
                    rawmd = cell_dir / "raw_interface.md"
                    if not rawmd.is_file():
                        print(f"  {path.parent.name}: no raw_interface.md "
                              f"(run predates it) — left as scored")
                        continue
                    text = rawmd.read_text(encoding="utf-8")
                    runner = PipelineRunner(config=config, provider="gemini",
                                            model_id=model, workflow="structure")
                    handoff = runner._parse_handoff(text)
                    hs = runner._parse_hotspot_residues(text, handoff)
                    residues = json.loads(hs)["residues"] if hs else []
                    cif = runner._binder_structure_path(meta["pdb_id"])
                    chain = meta.get("reported_target_chain") or "A"
                    meta.update(n_hotspots=len(residues),
                                bad_atoms=atom_errors(residues, str(cif), chain))
                path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
                n += 1
    print(f"  rescored {n} cells from artifacts on disk (no API calls)")
    return n


# ── reporting ────────────────────────────────────────────────────────────────

def _load_all() -> dict:
    got: dict = {}
    for suite, (cases, _fn) in SUITES.items():
        for case in cases:
            for model in MODELS:
                p = cell_path(suite, case[0], model)
                if p.is_file():
                    got[(suite, case[0], model)] = json.loads(
                        p.read_text(encoding="utf-8"))
    return got


def summarise() -> None:
    got = _load_all()
    if not got:
        print("  nothing on disk yet")
        return

    for suite, (cases, _fn) in SUITES.items():
        rows = [(c[0], {m: got[(suite, c[0], m)] for m in MODELS
                        if (suite, c[0], m) in got}) for c in cases]
        rows = [(s, by) for s, by in rows if by]
        if not rows:
            continue
        print(f"\n  ── {suite} ─────────────────────────────────────────────")
        head = (f"  {'case':16s}" + "".join(f"{m.split('-')[1]:>26s}" for m in MODELS))
        print(head)
        print("  " + "-" * (16 + 26 * len(MODELS)))
        for slug, by in rows:
            def col(key, fmt="{}"):
                out = ""
                for m in MODELS:
                    c = by.get(m)
                    out += f"{'-' if c is None else fmt.format(key(c)):>26s}"
                return out
            print(f"  {slug:16s}" + col(lambda c: f"${c['usd']:.4f}"))
            print(f"  {'':16s}" + col(lambda c: f"{c['usage']['input_tokens']:,} in / "
                                                f"{c['usage']['output_tokens']:,} out"))
            print(f"  {'':16s}" + col(lambda c: f"{c.get('n_tool_calls', 0)} calls, "
                                                f"{c['seconds']:.0f}s"))
            if suite == "interface":
                print(f"  {'':16s}" + col(lambda c: (
                    "ACCEPTED" if c["production_accepted"] else "REJECTED")))
                print(f"  {'':16s}" + col(lambda c: (
                    f"{c['n_hotspots']} hotspots, "
                    f"{c['n_name_mismatch']} misnamed")))
                print(f"  {'':16s}" + col(lambda c: (
                    f"{c['n_label_seq_disagreed']} label_seq, "
                    f"{len(c['bad_atoms'])} bad atoms")))
                print(f"  {'':16s}" + col(lambda c: (
                    f"chain {c.get('reported_target_chain') or '?'}"
                    + ("" if c["kept_measured_target_chain"] else " (SWAPPED)"))))
            else:
                print(f"  {'':16s}" + col(lambda c: (c.get("primary") or "-")[:24]))
                cit = lambda c: c.get("citations") or {}
                print(f"  {'':16s}" + col(lambda c: (
                    f"{cit(c).get('in_corpus', '-')}/{cit(c).get('cited', '-')} "
                    f"DOIs in corpus")))
                print(f"  {'':16s}" + col(lambda c: (
                    f"{len(c.get('unresolvable_genes') or [])} unresolvable "
                    f"of {len(c.get('genes') or [])}")))
            print()

    # Totals, per model, with the comparison stated as a ratio rather than
    # left to the reader — the whole decision is "does 3.8 cost more".
    print("  ── totals ──────────────────────────────────────────────")
    tot = {}
    for m in MODELS:
        cells = [c for (s, _sl, mm), c in got.items() if mm == m]
        paired = [c for c in cells]
        tot[m] = {
            "n_cells": len(paired),
            "usd": sum(c["usd"] for c in paired),
            "in": sum(c["usage"]["input_tokens"] for c in paired),
            "out": sum(c["usage"]["output_tokens"] for c in paired),
            "cache_read": sum(c["usage"].get("cache_read_tokens", 0) for c in paired),
            "seconds": sum(c["seconds"] for c in paired),
            "accepted": sum(1 for c in paired if c["production_accepted"]),
            "calls": sum(c.get("n_tool_calls", 0) for c in paired),
        }
        print(f"  {m:20s} n={tot[m]['n_cells']:2d}  ${tot[m]['usd']:.4f}  "
              f"{tot[m]['in']:,} in / {tot[m]['out']:,} out  "
              f"{tot[m]['calls']} tool calls  {tot[m]['seconds']:.0f}s  "
              f"accepted {tot[m]['accepted']}/{tot[m]['n_cells']}")
    a, b = MODELS
    if tot[a]["n_cells"] == tot[b]["n_cells"] and tot[a]["usd"]:
        for k, label in (("usd", "cost"), ("in", "input tokens"),
                         ("out", "output tokens"), ("seconds", "wall clock")):
            print(f"    {label:14s} {b.split('-')[1]} / {a.split('-')[1]} = "
                  f"{tot[b][k] / max(tot[a][k], 1e-9):.2f}x")
    else:
        print("    (ratios withheld — the matrix is not complete/paired yet)")

    unexpected = {(s, sl, m): c["served_models"] for (s, sl, m), c in got.items()
                  if c["served_models"] not in ([m], [])}
    if unexpected:
        print("\n  ⚠ cells NOT served by the model under test (refusal fallback):")
        for key, served in sorted(unexpected.items()):
            print(f"    {key} -> {served}")

    res = OUT / "results.json"
    res.write_text(json.dumps({"models": list(MODELS), "totals": tot,
                               "cells": [{"suite": s, "slug": sl, "model": m, **c}
                                         for (s, sl, m), c in sorted(got.items())]},
                              indent=2) + "\n", encoding="utf-8")
    print(f"\n  wrote {res.relative_to(_ROOT)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--suite", action="append", choices=sorted(SUITES),
                    help="run just these suites (repeatable)")
    ap.add_argument("--only", action="append", metavar="SLUG",
                    help="run just these cases (repeatable)")
    ap.add_argument("--model", action="append", choices=MODELS,
                    help="run just these models (repeatable)")
    ap.add_argument("--cell-cap", type=float, default=1.00, metavar="USD",
                    help="hard per-cell budget cap (default 1.00)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--rescore", action="store_true",
                    help="re-derive the artifact-based metrics from reports "
                         "already on disk, then re-print; makes no API calls")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    if args.rescore:
        rescore()
        summarise()
        return 0
    if args.report:
        summarise()
        return 0

    suites = args.suite or list(SUITES)
    models = args.model or list(MODELS)
    plan = [(s, c, m) for s in suites for c in SUITES[s][0]
            for m in models if not args.only or c[0] in args.only]
    if not plan:
        raise SystemExit(f"nothing matched --only {args.only}")

    todo = [p for p in plan if args.force
            or not cell_path(p[0], p[1][0], p[2]).is_file()]
    est = sum(EST_USD[s] for s, _c, _m in todo)
    print(f"\n  {len(plan)} cells ({len(todo)} to run, "
          f"{len(plan) - len(todo)} cached)")
    for s, c, m in plan:
        mark = " " if (s, c[0], m) in {(x[0], x[1][0], x[2]) for x in todo} else "."
        print(f"   {mark} {s:10s} {c[0]:16s} {m}")
    print(f"\n  estimated cost of the cells still to run: ~${est:.2f} "
          f"(medians from projects/*/ledger.jsonl on 3.7)")
    print(f"  hard cap per cell: ${args.cell_cap:.2f}")
    if args.dry_run:
        print("  --dry-run: nothing executed")
        return 0

    import yaml
    from src.token_budget import load_pricing
    config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    load_pricing(config)
    print()
    for suite, case, model in plan:
        cell(suite, case, model, config, args.cell_cap, args.force)
    summarise()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
