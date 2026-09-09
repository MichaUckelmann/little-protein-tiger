#!/usr/bin/env python3
"""Does the corpus change which target the pathway stage picks, or only confirm it?

The question this answers: when `pathway-expert` chooses a target, is it
retrieving from the corpus and reasoning from what came back, or is it recalling
a target from training and using corpus lookups to decorate a decision it had
already made? Both look identical in the finished report — the citations are
real and in the corpus either way — so the only way to tell is to change what
the corpus returns and see whether the answer moves.

Three arms, same prompt, same tool surface, same model. Only the CONTENT the
corpus tools return differs:

  live    unmodified.
  blank   every corpus-derived tool reports nothing found. Isolates "does
          corpus content change the answer at all". If the targets are
          identical to `live`, discovery is not corpus-driven.
  decoy   every corpus-derived tool returns REAL fingerprints for a fixed,
          deliberately off-topic query (chromatin), whatever was asked. This is
          the sharper arm: it separates reading from reassurance. A model that
          reasons from retrieved content should notice it has been handed
          irrelevant papers and fall back, visibly, to inference; one that only
          needs lookups to have happened will cite them and carry on.

`decoy` is not a contrived condition. `search_corpus` is a vector store and
NEVER returns "no hits" — asked for "zzzqqq nonexistent topic" it returns its
three nearest neighbours, best score 0.245, all chromatin. So on any query
outside the corpus's dense regions, low-relevance chromatin papers are what the
stage really receives. The decoy arm is that situation, made deterministic.

Queries span a ~60x range of measured corpus density (shares of the curated set
by title, from `docs/showcase/facts/corpus_explorer.json`), so divergence can be
correlated with coverage rather than eyeballed on one topic. If the corpus is
doing discovery work, `live` should diverge from `blank` MORE where coverage is
dense.

Traces are written per cell. The pipeline never passed `trace_path`, which is
why no run in `projects/` can answer this question — the tool-call sequence was
not kept. Here it is, so "searched then decided" and "decided then searched"
are distinguishable after the fact.

    python scripts/ablate_corpus.py --dry-run      # matrix + cost estimate
    python scripts/ablate_corpus.py --only pain    # one query, all three arms
    python scripts/ablate_corpus.py                # everything, resumable
    python scripts/ablate_corpus.py --report       # re-print the summary

Cells are skipped if their report already exists, so an interrupted matrix
resumes for free. Nothing here touches a GPU or writes into `projects/`.
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

OUT = _ROOT / "outputs" / "ablation"
SKILL = "pathway-expert"

# (slug, field label, query). Coverage is read from the corpus facts at runtime
# so this table cannot drift from what was measured.
QUERIES = [
    ("chromatin", "chromatin and epigenetics",
     "Design novel protein therapeutics targeting chromatin regulators that drive "
     "treatment resistance in acute myeloid leukaemia."),
    ("cancer", "cancer biology",
     "Design cancer therapeutics to target key nodes in pancreatic ductal "
     "adenocarcinoma."),
    ("immunology", "immunology",
     "Design novel inhibitors for immune checkpoint interactions that are "
     "promising drug targets."),
    ("cardiac", "cardiac",
     "Design novel protein therapeutics for heart failure targets."),
    ("pain", "neuroscience and pain",
     "design novel inhibitors for pain receptors that are promising drug targets"),
    ("fibrosis", "fibrosis",
     "Design novel inhibitors for idiopathic pulmonary fibrosis targets."),
]

ARMS = ("live", "blank", "decoy")

# Tools whose answers come from the LITERATURE corpus — the fingerprint content
# channel plus the co-mention graph and clusters built from it. DepMap tools
# (`get_genetic_codependency`, `find_cocorrelated_genes`) are NOT here: they read
# CRISPR essentiality, not papers, and leaving them live in every arm keeps the
# ablation to one variable. RCSB and the structure tools are likewise untouched.
CORPUS_TOOLS = frozenset({
    "search_corpus", "get_fingerprint", "find_quantitative_evidence",
    "get_interactions_for", "shortest_interaction_path", "interaction_hubs",
    "novelty_signal", "export_subgraph", "cluster_for_protein",
    "cluster_members", "find_clusters_by_keyword",
})

# What the decoy arm pretends every question was about.
DECOY_QUERY = "histone chaperone nucleosome assembly mechanism"

NOTHING = json.dumps({"result_text":
                      "No results found in the corpus for this query."})


def coverage() -> dict[str, float]:
    facts = json.loads((_ROOT / "docs/showcase/facts/corpus_explorer.json")
                       .read_text(encoding="utf-8"))
    return {label: pct for label, _n, pct in facts["coverage"]}


def make_runner(config: dict):
    from src.skill_runner import SkillRunner
    models = config.get("models", {}).get("gemini", {})
    return SkillRunner(
        skill_name=SKILL, provider="gemini",
        model_id=models.get("default") or "gemini-3.7-flash",
        config=config)


def patch(runner, arm: str, decoy_cache: dict) -> None:
    """Replace the corpus channel for this arm. `live` is left alone."""
    if arm == "live":
        return
    inner = runner._execute_tool

    def wrapped(name: str, input_dict: dict) -> str:
        if name not in CORPUS_TOOLS:
            return inner(name, input_dict)
        if arm == "blank":
            return NOTHING
        # decoy: real corpus content, for the wrong question.
        if name not in decoy_cache:
            if name == "search_corpus":
                decoy_cache[name] = inner(name, {"query": DECOY_QUERY,
                                                 "top_k": input_dict.get("top_k", 8)})
            else:
                # Anything else in the channel returns nothing, so the decoy is
                # specifically "you were handed off-topic PAPERS", not "the
                # whole corpus layer misbehaved".
                decoy_cache[name] = NOTHING
        return decoy_cache[name]

    runner._execute_tool = wrapped


def tiers(report: str) -> list[list[str]]:
    """The stage's ranked candidates. Same parse as docs/showcase/build_pain.py."""
    if "TARGET OPPORTUNITY LANDSCAPE" not in report:
        return []
    body = report.split("TARGET OPPORTUNITY LANDSCAPE")[1].split("### PRIMARY")[0]
    out = []
    for block in re.split(r"\n#### ", body)[1:]:
        head = block.splitlines()[0].strip()
        m = re.match(r"\[([A-Z_ ]+)\]\s*(.+)", head)
        if m:
            out.append([m.group(1).strip(), m.group(2).strip()])
    return out


def genes(report: str) -> list[str]:
    """Gene symbols named in the tier table, order preserved, deduplicated."""
    seen, out = set(), []
    for _tier, name in tiers(report):
        for g in re.split(r"\s*/\s*", name):
            g = g.strip()
            if g and g not in seen:
                seen.add(g)
                out.append(g)
    return out


def primary(report: str) -> str:
    m = re.search(r"- target_complex:\s*(.+)", report)
    return m.group(1).strip() if m else ""


def tool_calls(trace: pathlib.Path) -> dict:
    """Tool-call counts, and where the first corpus call sits in the sequence.

    The position is the point of keeping traces at all: a stage that searches
    before it has written anything looks different from one that drafts a
    candidate list and then looks it up, and the finished report cannot tell
    you which happened.

    `trace_path` is a DIRECTORY (`trace_raw.json` + `trace_rendered.md`), and
    the raw messages are in provider-native shape — Gemini nests
    `parts[].functionCall`, Claude `content[].tool_use`. Both are read here so
    the harness keeps working if `--provider claude` is ever used for an arm.
    """
    raw = trace / "trace_raw.json"
    if not raw.is_file():
        return {}
    msgs = json.loads(raw.read_text(encoding="utf-8"))
    names: list[str] = []
    first_corpus = None
    prose_before = 0
    for m in msgs:
        for part in (m.get("parts") or m.get("content") or []):
            if not isinstance(part, dict):
                continue
            call = part.get("functionCall")
            name = (call or {}).get("name") if call else (
                part.get("name") if part.get("type") == "tool_use" else None)
            if name:
                names.append(name)
                if first_corpus is None and name in CORPUS_TOOLS:
                    first_corpus = len(names)
                continue
            text = part.get("text")
            if text and first_corpus is None and len(text.strip()) > 200:
                prose_before += 1
    from collections import Counter
    return {"n_tool_calls": len(names),
            "n_corpus_calls": sum(1 for x in names if x in CORPUS_TOOLS),
            "by_tool": dict(Counter(names)),
            "first_corpus_call_at": first_corpus,
            "prose_blocks_before_first_corpus_call": prose_before}


# Words that could only have arrived from the decoy payload. The decoy feeds
# chromatin fingerprints to a question about something else, so any of these in
# the report is retrieved content leaking into an answer where it does not
# belong — the sharpest available signal that lookups are being used as
# reassurance rather than read.
# "chaperone" was in this list for one run and produced a false positive on the
# blank arm: RAMP1 legitimately IS a GPCR accessory chaperone. A leak word has
# to be one that cannot appear innocently in a report about pain, heart failure
# or fibrosis, or the metric measures vocabulary instead of contamination.
LEAK_WORDS = ("histone", "chromatin", "nucleosome", "H3K", "H2A", "H2B",
              "heterochromatin", "epigenetic")


def outcomes(report: str) -> dict:
    """The measures that actually separate the arms.

    Added after the first cell: gene picks alone understate the difference,
    because the top pick can be identical across arms while the evidence under
    it is not. Citation count is the clearest of these — the corpus's
    contribution shows up as how well-evidenced the same answer is.
    """
    dois = sorted(set(re.findall(r"10\.[0-9]{4,9}/[^\s)\"',;]+", report)))
    low = report.lower()
    return {
        "n_dois": len(dois), "dois": dois,
        "tier_labels": [t for t, _n in tiers(report)],
        "offtopic_leak": sum(low.count(w) for w in LEAK_WORDS),
        "flags_corpus_gap": sum(low.count(p) for p in
                                ("not found in corpus", "corpus gap",
                                 "absent from the corpus")),
        "report_chars": len(report),
    }


def cell(slug: str, arm: str, query: str, config: dict, force: bool) -> dict:
    md = OUT / f"{slug}__{arm}.md"
    meta_path = OUT / f"{slug}__{arm}.json"
    if md.is_file() and meta_path.is_file() and not force:
        print(f"  {slug}/{arm}: cached")
        return json.loads(meta_path.read_text(encoding="utf-8"))

    from src.token_budget import load_pricing, price
    load_pricing(config)
    runner = make_runner(config)
    patch(runner, arm, {})
    trace = OUT / f"{slug}__{arm}.trace"

    t0 = time.time()
    err = None
    report = ""
    try:
        report = runner.run(query, trace_path=trace)
    except Exception as exc:                       # noqa: BLE001 — recorded, not raised
        err = f"{type(exc).__name__}: {exc}"
        print(f"  {slug}/{arm}: FAILED {err}")
    usage = runner.usage()
    meta = {
        "slug": slug, "arm": arm, "query": query, "error": err,
        "seconds": round(time.time() - t0, 1),
        "usd": round(price(runner.model_id, usage) or 0.0, 4),
        "usage": usage.as_dict(),
        "tiers": tiers(report), "genes": genes(report),
        "primary": primary(report),
        **outcomes(report),
        **tool_calls(trace),
    }
    md.write_text(report, encoding="utf-8")
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"  {slug}/{arm}: ${meta['usd']:.4f}  {meta['seconds']:.0f}s  "
          f"{meta.get('n_tool_calls', 0)} calls "
          f"({meta.get('n_corpus_calls', 0)} corpus, first at "
          f"#{meta.get('first_corpus_call_at')})  -> {meta['genes'][:3]}")
    return meta


def summarise() -> None:
    cov = coverage()
    rows = []
    for slug, label, _q in QUERIES:
        by = {}
        for arm in ARMS:
            p = OUT / f"{slug}__{arm}.json"
            if p.is_file():
                by[arm] = json.loads(p.read_text(encoding="utf-8"))
        if by:
            rows.append((slug, label, by))

    def jac(a: list[str], b: list[str]) -> float | None:
        if not a or not b:
            return None
        sa, sb = set(a), set(b)
        return round(len(sa & sb) / len(sa | sb), 2)

    print(f"\n  {'query':11s} {'cov%':>5s}  {'live top target':24s} "
          f"{'top pick':>14s}   {'DOIs cited':>18s}  {'leak':>10s}")
    print("  " + "-" * 96)
    out = []
    for slug, label, by in rows:
        live = by.get("live", {})
        jb = jac(live.get("genes", []), by.get("blank", {}).get("genes", []))
        jd = jac(live.get("genes", []), by.get("decoy", {}).get("genes", []))
        same_b = (live.get("primary") == by.get("blank", {}).get("primary")
                  if "blank" in by else None)
        same_d = (live.get("primary") == by.get("decoy", {}).get("primary")
                  if "decoy" in by else None)
        mark = lambda v: {True: "same", False: "DIFF", None: "-"}[v]
        d = lambda a: by.get(a, {}).get("n_dois")
        lk = lambda a: by.get(a, {}).get("offtopic_leak")
        print(f"  {slug:11s} {cov.get(label, float('nan')):5.1f}  "
              f"{(live.get('primary') or '-')[:24]:24s} "
              f"b:{mark(same_b)} d:{mark(same_d)}   "
              f"{d('live')} / {d('blank')} / {d('decoy'):<10}  "
              f"{lk('live')}/{lk('blank')}/{lk('decoy')}")
        out.append({"slug": slug, "field": label, "coverage_pct": cov.get(label),
                    "primary": {a: by[a].get("primary") for a in by},
                    "genes": {a: by[a].get("genes") for a in by},
                    "primary_same_as_live": {"blank": same_b, "decoy": same_d},
                    "gene_overlap_with_live": {"blank": jb, "decoy": jd},
                    "n_dois": {a: by[a].get("n_dois") for a in by},
                    "tier_labels": {a: by[a].get("tier_labels") for a in by},
                    "offtopic_leak": {a: by[a].get("offtopic_leak") for a in by},
                    "flags_corpus_gap": {a: by[a].get("flags_corpus_gap") for a in by},
                    "n_corpus_calls": {a: by[a].get("n_corpus_calls") for a in by},
                    "first_corpus_call_at": {
                        a: by[a].get("first_corpus_call_at") for a in by},
                    "n_tool_calls": {a: by[a].get("n_tool_calls") for a in by},
                    "usd": {a: by[a].get("usd") for a in by}})
    total = sum(v or 0 for r in out for v in r["usd"].values())
    print(f"\n  spend so far: ${total:.2f}")
    (OUT / "results.json").write_text(json.dumps(out, indent=2) + "\n",
                                      encoding="utf-8")
    print(f"  wrote {(OUT / 'results.json').relative_to(_ROOT)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", action="append", metavar="SLUG",
                    help="run just these queries (repeatable)")
    ap.add_argument("--arm", action="append", choices=ARMS,
                    help="run just these arms (repeatable)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the matrix and a cost estimate, run nothing")
    ap.add_argument("--report", action="store_true",
                    help="re-print the summary from cells already on disk")
    ap.add_argument("--force", action="store_true", help="re-run cached cells")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    if args.report:
        summarise()
        return 0

    queries = [q for q in QUERIES if not args.only or q[0] in args.only]
    arms = [a for a in ARMS if not args.arm or a in args.arm]
    if not queries:
        raise SystemExit(f"no query matched {args.only}; have "
                         f"{[q[0] for q in QUERIES]}")

    cov = coverage()
    print(f"\n  {len(queries)} queries x {len(arms)} arms = "
          f"{len(queries) * len(arms)} pathway runs")
    for slug, label, _q in queries:
        print(f"    {slug:11s} {cov.get(label, float('nan')):5.1f}% of the "
              f"curated corpus  ({label})")
    # Measured on the `pain` cell rather than taken from the reference
    # campaign, whose $0.1678 turned out to be a third of the live arm's real
    # cost: retrieved fingerprints are what fills the context, so the arm with
    # a working corpus is the expensive one.
    per_arm = {"live": 0.57, "blank": 0.11, "decoy": 0.24}
    est = len(queries) * sum(per_arm[a] for a in arms)
    print(f"\n  estimated cost: ~${est:.2f}  ("
          + ", ".join(f"{a} ${per_arm[a]:.2f}" for a in arms)
          + " per query, measured on the pain cell)")
    if args.dry_run:
        print("  --dry-run: nothing executed")
        return 0

    import yaml
    config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    print()
    for slug, _label, query in queries:
        for arm in arms:
            cell(slug, arm, query, config, args.force)
    summarise()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
