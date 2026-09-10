"""The documents must not describe a tool that isn't there.

Four fresh-clone trials of `SETUP_AGENT.md` found the same pattern every time:
where a document and the code disagreed, **the code was right**. The document
prescribed `LPT_CA_BUNDLE` for a failure only `LPT_SSL_RELAX_STRICT` fixes; it
verified an API key with a grep that passes on `.env.example`'s own
placeholder; it offered a `structure` track and never named its command; and
Phase 8's one-liner was measured failing for 73 seconds while the working
script existed but was uncommitted.

None of those needed a human to catch. They needed *something* to check that a
documented command resolves to a real script with real flags — which is what
this file does. It is deliberately mechanical: no judgement about whether the
prose is good, only whether it refers to things that exist.

Nothing here needs network, GPU, API keys or the corpus.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"

#: Every document a new user or an agent is pointed at.
_DOCS = [
    _ROOT / "README.md",
    _ROOT / "SETUP_AGENT.md",
    _ROOT / "CLAUDE.md",
    *sorted((_ROOT / "docs").glob("*.md")),
]


def _doc_ids() -> list[str]:
    return [d.relative_to(_ROOT).as_posix() for d in _DOCS]


def _argparse_options(script: pathlib.Path) -> set[str]:
    """Option strings the script declares, by PARSING it.

    Never by importing: several of these scripts do real work at import time
    (`load_env`, a model load, a `sys.path` mutation), and one of them used to
    write `.mcp.json` when merely probed with `--help`.
    """
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
    except SyntaxError:                                       # pragma: no cover
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.add(arg.value)
    return out


@pytest.mark.parametrize("doc", _DOCS, ids=_doc_ids())
def test_every_script_a_document_names_exists(doc):
    """`scripts/warm_embedding_cache.py` was named by a document and untracked.

    That is the exact shape of the worst finding in the trials: the fix was
    written, measured and documented, and the one artifact that would let a
    user apply it never reached git. A `git clone` user got the failing
    instruction and no script to fall back on.
    """
    if not doc.is_file():
        pytest.skip(f"{doc.name} absent")
    named = sorted(set(re.findall(r"scripts/([a-z0-9_]+\.py)",
                                  doc.read_text(encoding="utf-8"))))
    missing = [n for n in named if not (_SCRIPTS / n).is_file()]
    assert not missing, f"{doc.name} names scripts that do not exist: {missing}"


@pytest.mark.parametrize("doc", _DOCS, ids=_doc_ids())
def test_every_flag_a_document_shows_is_a_real_flag(doc):
    """A documented `--flag` that argparse rejects is worse than no example:
    the reader assumes their own environment is broken."""
    if not doc.is_file():
        pytest.skip(f"{doc.name} absent")
    bad: list[str] = []
    for line in doc.read_text(encoding="utf-8").splitlines():
        m = re.search(r"scripts/([a-z0-9_]+\.py)((?:\s+--?[a-zA-Z0-9][\w-]*)+)",
                      line)
        if not m:
            continue
        script = _SCRIPTS / m.group(1)
        if not script.is_file():
            continue                        # the test above owns that failure
        known = _argparse_options(script)
        if not known:
            continue                        # no argparse: nothing to check
        for flag in re.findall(r"--[a-zA-Z0-9][\w-]*", m.group(2)):
            if flag not in known:
                bad.append(f"{m.group(1)} {flag}  (line: {line.strip()[:60]})")
    assert not bad, f"{doc.name} shows flags that do not exist:\n  " + "\n  ".join(bad)


def test_help_never_performs_the_side_effect_it_is_being_asked_about():
    """`setup_mcp_json.py --help` used to WRITE `.mcp.json`.

    It had no argparse at all, so `--help` fell through to the body — probing
    for a dry-run flag performed the write. Checked by reflection over the
    scripts a document tells someone to run, because the failure mode is a
    script that never declares an argument parser.
    """
    documented: set[str] = set()
    for doc in _DOCS:
        if doc.is_file():
            documented |= set(re.findall(r"scripts/([a-z0-9_]+\.py)",
                                         doc.read_text(encoding="utf-8")))
    # Narrowed to scripts that WRITE. `launch_mcp.py` and
    # `launch_structure_tools.py` read `sys.argv[1:]` to forward it to a
    # re-exec and produce no artifact, so `--help` there merely starts a server
    # with an unknown argument — not the hazard this guards.
    writes = ("write_text(", "write_bytes(", 'open(', "shutil.copy",
              "os.replace", ".replace(")
    parserless = []
    for name in sorted(documented):
        script = _SCRIPTS / name
        if not script.is_file():
            continue
        src = script.read_text(encoding="utf-8")
        if "argparse" in src or "sys.argv" not in src:
            continue
        if any(w in src for w in writes):
            parserless.append(name)
    assert not parserless, (
        "documented scripts that read sys.argv without argparse AND write "
        f"files, so --help performs the side effect: {parserless}")


def test_the_embedding_model_is_named_once():
    """`warm_embedding_cache.py` and `vector_store.py` must agree, or the warm
    step caches a model the search path then re-downloads under
    HF_HUB_OFFLINE=1 — failing with an error that names neither."""
    warm = (_SCRIPTS / "warm_embedding_cache.py").read_text(encoding="utf-8")
    store = (_ROOT / "src" / "vector_store.py").read_text(encoding="utf-8")
    model = re.search(r'^MODEL = "([^"]+)"', warm, re.M)
    assert model, "warm_embedding_cache.py no longer declares MODEL"
    assert model.group(1) in store, (
        f"{model.group(1)} is not the model src/vector_store.py loads")


def test_sizes_are_reported_in_the_unit_they_claim():
    """Three figures for one file is how a reader learns to distrust the tool.

    `fetch_corpus.py` printed "100 MB" for a 104,535,812-byte asset because it
    divided by 2**20 and wrote "MB"; the documents said ~105 MB and GitHub's
    release page said 104 MB. `SETUP_AGENT.md` Phase 6 tells the reader to
    trust this tool over the prose, so the tool has to be the accurate one.
    """
    offenders = []
    for script in sorted(_SCRIPTS.glob("*.py")):
        for i, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]        # a comment may DISCUSS 2**20
            if re.search(r"2\s*\*\*\s*[23]0", code) and re.search(r"\bMB\b|\bGB\b", code):
                offenders.append(f"{script.name}:{i}  {line.strip()[:70]}")
    assert not offenders, (
        "binary divisor labelled as decimal MB/GB — either divide by 1e6/1e9 "
        "or write MiB/GiB:\n  " + "\n  ".join(offenders))


def test_claude_md_thresholds_match_the_config_they_cite():
    """CLAUDE.md opens with "Read before changing a threshold", so a number in
    it that no longer matches `config.yaml` is worse than absent."""
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    text = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    br = cfg["design"]["binder_ranking"]
    fo = cfg["design"]["foundry"]

    # Deliberately a presence check, not a phrase match: prose gets rewritten
    # and a brittle assertion on wording fails for the wrong reason. What must
    # hold is that a threshold's CURRENT value is discussed somewhere in the
    # file, so moving one forces a look at the document that opens with "read
    # before changing a threshold".
    for name, value in (
        ("design.binder_ranking.thresholds.hotspot_engagement_min",
         br["thresholds"]["hotspot_engagement_min"]),
        ("design.binder_ranking.excellence_ipsae_min", br["excellence_ipsae_min"]),
        ("design.foundry.target_residue_budget", fo["target_residue_budget"]),
        ("design.foundry.max_local_hours", fo["max_local_hours"]),
    ):
        rendered = (f"{value:g}" if isinstance(value, float) else str(value))
        assert rendered in text, (
            f"config.yaml sets {name} = {rendered}, which appears nowhere in "
            f"CLAUDE.md — update the note that explains it")


# ── the licence gate ─────────────────────────────────────────────────────────

def test_absence_of_a_licence_is_never_read_as_permission():
    """The one rule the whole gate rests on.

    A paper Europe PMC records no licence for is normally a publisher deposit:
    free to read, not licensed for reuse. If `permits_derivatives` ever
    defaults an unknown licence to True, the archive silently starts shipping
    fingerprints of 5,684 papers nobody granted rights to.
    """
    from src.paper_licence import NO_DERIVATIVES, UNKNOWN, classify, permits_derivatives

    for value in (None, "", "  ", "none", "unknown", "all rights reserved",
                  "copyright Elsevier", "—"):
        assert not permits_derivatives(value), f"{value!r} read as permission"
        assert classify(value) == UNKNOWN, value

    for value in ("cc by-nd", "cc by-nc-nd", "CC BY-NC-ND 4.0",
                  "Attribution-NoDerivs 3.0"):
        assert not permits_derivatives(value), f"{value!r} read as permission"
        assert classify(value) == NO_DERIVATIVES, value

    for value in ("cc by", "cc by-sa", "cc by-nc", "cc by-nc-sa", "cc0",
                  "public domain"):
        assert permits_derivatives(value), f"{value!r} wrongly restricted"


def test_nd_is_matched_as_a_token_not_a_substring():
    """"cc by" must never be read as ND because some word contains "nd"."""
    from src.paper_licence import permits_derivatives

    assert permits_derivatives("cc by")           # not "nd" in "by"
    assert permits_derivatives("cc by-sa")
    # a free-text field naming a journal, not a licence term
    assert permits_derivatives("cc by (Endocrinology deposit)")


def test_the_licence_gate_defaults_to_on_in_config():
    """`--allow-restricted-licence` may turn it off; nothing may turn it on by
    omission. The conservative setting has to be what you get for free."""
    import yaml

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["quality"]["require_derivative_licence"] is True


def test_every_licence_decision_goes_through_one_module():
    """Four callers make this decision and must not drift.

    A POSITIVE check — each gating script must import the shared module —
    rather than hunting for a local ND literal. The first version of this did
    hunt, and flagged two docstrings that merely *mention* `cc by-nc-nd`;
    a test that cries wolf about prose gets muted, and then it protects
    nothing.
    """
    gating = ("audit_paper_licences.py", "fetch_papers.py", "curate_papers.py",
              "package_corpus.py")
    missing = []
    for name in gating:
        src = (_SCRIPTS / name).read_text(encoding="utf-8")
        if "paper_licence import" not in src and "src.paper_licence" not in src:
            missing.append(name)
    assert not missing, (
        f"these gate on licences without importing src.paper_licence, so they "
        f"carry their own copy of the rule: {missing}")


def test_the_papers_table_can_record_a_licence():
    """A sidecar JSON cannot gate a fetch or a package — the value has to be
    somewhere a SQL query can see it, and a shipped database that carries its
    own licence provenance is auditable by whoever receives it."""
    import sqlite3
    import tempfile

    from src.database import Database

    with tempfile.TemporaryDirectory() as td:
        db_path = pathlib.Path(td) / "t.db"
        Database(str(db_path))
        cols = {r[1] for r in
                sqlite3.connect(db_path).execute("PRAGMA table_info(papers)")}
    for col in ("licence", "licence_source", "licence_checked_at"):
        assert col in cols, f"papers.{col} missing"
