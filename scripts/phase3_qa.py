#!/usr/bin/env python3
"""Phase 3: closed-book vs open-book questions whose answers are only in the corpus.

    .venv/bin/python scripts/phase3_qa.py build            # questions + gold, no API calls
    .venv/bin/python scripts/phase3_qa.py run --max-usd 2  # answer with both arms (resumable)
    .venv/bin/python scripts/phase3_qa.py score            # automatic + LLM-judged scoring, then report

Plan and decision rules: docs/phase3_closed_vs_open_book.md (written before any
question existed). The gold answer is what the corpus fingerprint says, not an
independently verified fact.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import random
import re
import sqlite3
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import phase2_common as C  # noqa: E402

OUT = _ROOT / "outputs" / "phase3_qa"
SEED = "phase3-2026-10-07"
N = 16

QA_PROMPT = (
    "You answer specific questions about published biochemistry and molecular biology.\n"
    "If you cannot determine the answer, reply with exactly UNKNOWN on the first line.\n"
    "Otherwise put the answer on the first line (for a number give one value with its units; for residues give a "
    "comma-separated list like R123, Y45), then at most two sentences saying where it comes from."
)
OPEN_NOTE = "\nYou have tools that search a database of curated papers. Use them if they help."

AA3 = {"ala": "A", "arg": "R", "asn": "N", "asp": "D", "cys": "C", "gln": "Q", "glu": "E", "gly": "G", "his": "H", "ile": "I",
       "leu": "L", "lys": "K", "met": "M", "phe": "F", "pro": "P", "ser": "S", "thr": "T", "trp": "W", "tyr": "Y", "val": "V"}


# ------------------------------------------------------------------ build
def build() -> None:
    db = sqlite3.connect(_ROOT / "data" / "literature.db")
    year = {d.lower(): y for d, y in db.execute("select doi, year from papers where doi is not null")}
    pairs = collections.defaultdict(list)
    for f in (_ROOT / "data" / "fingerprints").glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        doi = (d["paper_metadata"].get("doi") or "").lower()
        for k in d.get("key_findings", []):
            pp = k.get("protein_pair") or []
            if len(pp) == 2 and all(pp):
                pairs[frozenset(x.lower() for x in pp)].append((doi, year.get(doi), k, pp))
    uniq = [v[0] for v in pairs.values() if len(v) == 1 and v[0][2].get("experimental_context")]
    for x in uniq:                      # "…cells." + "." read as a typo in the question
        x[2]["experimental_context"] = x[2]["experimental_context"].strip().rstrip(".")
    plain = re.compile(r"^[A-Za-z]{1,3}-?\d+$")      # R123 / Arg123, not a mutation such as M7Y
    rng = random.Random(SEED)
    qs = []

    def pick(pool, n):
        pool = sorted(pool, key=lambda x: x[0])
        return rng.sample(pool, min(n, len(pool)))

    for doi, y, k, pp in pick([x for x in uniq if (x[1] or 0) >= 2024 and x[2].get("affinities_kd_Molar")], N):
        qs.append({"id": f"kd{len(qs):02d}", "type": "kd", "doi": doi, "year": y,
                   "q": f"What dissociation constant (Kd) has been reported for the interaction between {pp[0]} and {pp[1]}? "
                        f"Context: {k['experimental_context']}. Give one value with units.",
                   "gold": k["affinities_kd_Molar"]})
    for doi, y, k, pp in pick([x for x in uniq if (x[1] or 0) >= 2024 and len(x[2].get("key_amino_acid_residues") or []) >= 2
                                                    and all(plain.match(str(r).strip()) for r in x[2]["key_amino_acid_residues"])], N):
        qs.append({"id": f"rs{len(qs):02d}", "type": "residues", "doi": doi, "year": y,
                   "q": f"Which amino acid residues were reported as important for the interaction between {pp[0]} and {pp[1]}? "
                        f"Context: {k['experimental_context']}. List the residues.",
                   "gold": k["key_amino_acid_residues"]})
    for doi, y, k, pp in pick([x for x in uniq if x[1] == 2026 and x[2].get("claim")], N):
        qs.append({"id": f"rc{len(qs):02d}", "type": "recent", "doi": doi, "year": y,
                   "q": f"A 2026 study examined {pp[0]} and {pp[1]}. Context: {k['experimental_context']}. What did it report?",
                   "gold": k["claim"]})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "questions.json").write_text(json.dumps(qs, indent=1))
    print(collections.Counter(q["type"] for q in qs), "questions ->", OUT / "questions.json")
    for t in ("kd", "residues", "recent"):
        x = next(q for q in qs if q["type"] == t)
        print(f"\n[{t}] {x['q']}\n   gold: {x['gold']}")


# ------------------------------------------------------------------ run
def make_runner(arm: str, config: dict):
    import src.skill_runner as sr
    models = config.get("models", {}).get("gemini", {})
    r = sr.SkillRunner(skill_name="corpus-explorer", provider="gemini", model_id=models.get("default") or "gemini-3.7-flash",
                       config=config, max_input_tokens=200_000)
    r.system_prompt = QA_PROMPT + (OPEN_NOTE if arm == "open" else "") + sr._OUTPUT_FORMAT_RULE
    if arm == "closed":
        r.skill_name = "qa-notools"
    return r


def run(max_usd: float) -> None:
    import yaml
    import src.skill_runner as sr
    from src.env_config import load_env
    from src.token_budget import load_pricing, price
    load_env()
    orig = sr._filter_tools
    sr._filter_tools = lambda defs, skill: [] if str(skill).endswith("-notools") else orig(defs, skill)
    config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    load_pricing(config)
    qs = json.loads((OUT / "questions.json").read_text())
    path = OUT / "answers.jsonl"
    done = {(r["id"], r["arm"]) for r in map(json.loads, path.read_text().splitlines())} if path.exists() else set()
    spent = sum(json.loads(l).get("usd", 0) for l in path.read_text().splitlines()) if path.exists() else 0.0
    order = [(q, arm) for q in qs for arm in ("closed", "open")]
    random.Random(SEED + ":order").shuffle(order)
    for q, arm in order:
        if (q["id"], arm) in done:
            continue
        if spent >= max_usd:
            print(f"stopping: ${spent:.2f} >= {max_usd}")
            break
        runner = make_runner(arm, config)
        err, text = None, ""
        try:
            text = runner.run(q["q"])
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {str(exc)[:160]}"
        u = runner.usage()
        usd = price(runner.model_id, u) or 0.0
        spent += usd
        tool_calls = [m for m in (getattr(runner, "_messages", None) or [])
                      for p in m.get("parts", []) if "functionCall" in p] if arm == "open" else []
        row = {"id": q["id"], "type": q["type"], "arm": arm, "answer": text, "error": err, "usd": round(usd, 5),
               "n_tool_calls": len(tool_calls)}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"  {q['id']:6}{arm:7} ${usd:.3f} tools {len(tool_calls):>2}  {text.strip().splitlines()[0][:70] if text.strip() else err}")
    print(f"spent ${spent:.2f}")


# ------------------------------------------------------------------ score
def parse_kd(text: str):
    first = text.strip().splitlines()[0] if text.strip() else ""
    first = re.sub(r"(?:±|\+/-|\+-)\s*[0-9.]+(?:\s*[x×]\s*10\^?[-−]?\d+)?\s*", " ", first)   # "3.5 ± 0.3 µM": drop the error term
    m = re.search(r"([0-9]*\.?[0-9]+(?:\s*[x×]\s*10\^?[-−]?\d+)?(?:[eE][-+]?\d+)?)\s*(pM|nM|µM|μM|uM|mM|M)\b", first)
    if not m:
        return None
    num = m.group(1)
    num = re.sub(r"\s*[x×]\s*10\^?([-−]?\d+)", lambda k: "e" + k.group(1).replace("−", "-"), num)
    try:
        v = float(num)
    except ValueError:
        return None
    return v * {"pM": 1e-12, "nM": 1e-9, "µM": 1e-6, "μM": 1e-6, "uM": 1e-6, "mM": 1e-3, "M": 1.0}[m.group(2)]


def residues(items) -> set:
    out = set()
    for s in items if isinstance(items, list) else re.split(r"[,;]", str(items)):
        for aa, n in re.findall(r"([A-Za-z]{1,3})[-\s]?(\d+)", str(s)):
            aa = AA3.get(aa.lower(), aa.upper() if len(aa) == 1 else "")
            if aa:
                out.add(f"{aa}{n}")
    return out


def is_unknown(text: str) -> bool:
    return not text.strip() or text.strip().splitlines()[0].strip().upper().startswith("UNKNOWN")


JUDGE_SYS = ("You compare a model's answer with a gold finding from a paper. Labels: correct = the answer conveys the same main "
             "finding; partly = overlaps but misses or changes something essential; incorrect = a different or contradicting "
             'finding. Return only JSON: {"label": "correct|partly|incorrect", "reason": "<=30 words"}.')


def score() -> None:
    qs = {q["id"]: q for q in json.loads((OUT / "questions.json").read_text())}
    ans = [json.loads(l) for l in (OUT / "answers.jsonl").read_text().splitlines()]
    label = {}
    for a in ans:
        q = qs[a["id"]]
        if a["error"]:
            label[(a["id"], a["arm"])] = "error"
        elif is_unknown(a["answer"]):
            label[(a["id"], a["arm"])] = "unknown"
        elif q["type"] == "kd":
            v = parse_kd(a["answer"])
            ok = v is not None and abs(math.log10(v / q["gold"])) <= math.log10(3)
            label[(a["id"], a["arm"])] = "correct" if ok else "wrong"
        elif q["type"] == "residues":
            g, got = residues(q["gold"]), residues(a["answer"].strip().splitlines()[0])
            label[(a["id"], a["arm"])] = "correct" if g and len(g & got) / len(g) >= 0.5 else "wrong"
    # recent: two blind judges
    judged = collections.defaultdict(dict)
    store = {j: C.Store("phase3_recent", j) for j in C.JUDGES}
    for a in ans:
        q = qs[a["id"]]
        if q["type"] != "recent" or label.get((a["id"], a["arm"])) in ("unknown", "error"):
            continue
        key = f"{a['id']}:{a['arm']}"
        user = f"QUESTION: {q['q']}\n\nGOLD FINDING: {q['gold']}\n\nMODEL ANSWER: {a['answer'][:1500]}"
        for j in C.JUDGES:
            if not store[j].has(key):
                try:
                    res = C.call_judge(j, JUDGE_SYS, user, 200, think=False)
                    store[j].add({"key": key, "id": a["id"], "arm": a["arm"], "parsed": C.parse_json(res["text"]), "usd": res["usd"]})
                except C.Refused as exc:
                    store[j].add({"key": key, "id": a["id"], "arm": a["arm"], "refused": True, "usd": 0.0})
            r = store[j].rows[key]
            judged[(a["id"], a["arm"])][j] = (r.get("parsed") or {}).get("label")
    for k, js in judged.items():
        labs = [x for x in js.values() if x]
        # both judges must call it correct; any "incorrect" from both is wrong; otherwise partly (counted as not correct)
        label[k] = "correct" if labs and all(x == "correct" for x in labs) else "wrong"
    # report
    print(f"\n{'type':10}{'arm':8}{'n':>3}{'correct':>9}{'wrong':>7}{'unknown':>9}{'err':>5}")
    rows = {}
    for t in ("kd", "residues", "recent"):
        for arm in ("closed", "open"):
            ids = [i for i, q in qs.items() if q["type"] == t]
            c = collections.Counter(label.get((i, arm), "missing") for i in ids)
            rows[(t, arm)] = c
            print(f"{t:10}{arm:8}{len(ids):>3}{c['correct']:>9}{c['wrong']:>7}{c['unknown']:>9}{c['error']:>5}")
    wins = 0
    print("\nopen - closed correct rate:")
    for t in ("kd", "residues", "recent"):
        n = sum(rows[(t, 'open')].values())
        d = (rows[(t, 'open')]['correct'] - rows[(t, 'closed')]['correct']) / max(n, 1)
        wins += d >= 0.25
        print(f"  {t:10}{d:+.2f}  ({rows[(t,'open')]['correct']}/{n} vs {rows[(t,'closed')]['correct']}/{n})")
    print(f"\nrule (>= 0.25 on at least two of three types): {'MET' if wins >= 2 else 'NOT MET'} ({wins} of 3)")
    cw = sum(rows[(t, 'closed')]['wrong'] for t in rows if False) if False else sum(rows[(t, 'closed')]['wrong'] for t in ('kd', 'residues', 'recent'))
    print(f"closed-book answers that were given and wrong: {cw} of 48; open-book wrong: {sum(rows[(t,'open')]['wrong'] for t in ('kd','residues','recent'))} of 48")
    spend = sum(a["usd"] for a in ans) + sum(sum(r.get("usd", 0) for r in s.rows.values()) for s in store.values())
    print(f"spend on this phase: ${spend:.2f}")
    (OUT / "scores.json").write_text(json.dumps({f"{t}/{arm}": dict(c) for (t, arm), c in rows.items()}, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["build", "run", "score"])
    ap.add_argument("--max-usd", type=float, default=2.0)
    a = ap.parse_args()
    {"build": build, "run": lambda: run(a.max_usd), "score": score}[a.cmd]() if a.cmd != "run" else run(a.max_usd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
