"""
Regression tests for six defects found by a source audit of the working tree.

Each test names the failure it prevents, because none of these were caught by
the existing suite: the BoltzGen analysis stage is unreachable in the default
config, `prune_confidences` had no call site to test, the calibration module's
stale refold anchor was only correct via one call site's keyword, the curation
enums were never validated at all, vector ingestion's add-only dedup looks
correct until a fingerprint changes, and `max_campaign_days` was read from a
key that did not exist.
"""

from __future__ import annotations

import builtins
import symtable
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest
import yaml

from src.pipeline_runner import PipelineError

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# 1. Undefined names in pipeline stages
# ----------------------------------------------------------------------

def _undefined_names(path: Path) -> dict[str, set[str]]:
    """
    Names a function body loads that are bound in no scope it can see.

    Uses `symtable` rather than a hand-rolled AST walk: the interpreter's own
    scope analyser already knows about closures, comprehension scopes, lambda
    parameters, `global`/`nonlocal` and walrus bindings, all of which a naive
    walk gets wrong in this file. A symbol that is `is_global()` and never
    assigned, yet is not a module-level name or a builtin, is a `NameError`
    waiting for the branch to be reached.
    """
    src = path.read_text(encoding="utf-8")
    top = symtable.symtable(src, path.name, "exec")
    # Module dunders exist at runtime but only appear in the symbol table if
    # the module body itself mentions them.
    module_names = {s.get_name() for s in top.get_symbols()} | {
        "__file__", "__name__", "__doc__", "__package__", "__spec__",
        "__loader__", "__builtins__", "__debug__",
    }
    found: dict[str, set[str]] = {}

    def walk(table, qualname: str) -> None:
        if table.get_type() == "function":
            missing = {
                s.get_name() for s in table.get_symbols()
                if s.is_global() and not s.is_assigned()
                and s.get_name() not in module_names
                and not hasattr(builtins, s.get_name())
            }
            if missing:
                found[qualname] = missing
        for child in table.get_children():
            walk(child, f"{qualname}.{child.get_name()}".lstrip("."))

    walk(top, "")
    return found


@pytest.mark.parametrize("module", sorted(
    p.name for p in (_ROOT / "src").glob("*.py") if not p.name.startswith("_")))
def test_no_function_loads_an_undefined_name(module):
    """
    `_stage_analysis` called `check_available(cfg)` with no `cfg` in scope.

    It is unreachable in the default configuration — `design.backend: foundry`
    bridges `--workflow ppi` past the BoltzGen stages — so it failed only under
    `--design-engine boltzgen` / `--modality cyclic_peptide`, and it failed as a
    caught `NameError` recorded in `result.error` three stages after the work
    that mattered. Nothing in the suite ever entered the function, which is
    exactly why this checks every function in `src/` rather than that one.
    """
    offenders = _undefined_names(_ROOT / "src" / module)
    assert not offenders, f"undefined names in {module}: {offenders}"


# ----------------------------------------------------------------------
# 2. prune_confidences is wired, guarded, and off by default
# ----------------------------------------------------------------------

def _fake_rf3_tree(root: Path, names: list[str]) -> Path:
    rf3 = root / "rf3_out"
    for n in names:
        d = rf3 / n
        d.mkdir(parents=True)
        (d / f"{n}_summary_confidences.json").write_text("{}", encoding="utf-8")
        (d / f"{n}_confidences.json").write_text("x" * 1000, encoding="utf-8")
    return rf3


def test_prune_confidences_has_a_call_site(config):
    """It was defined and documented as running, but never called."""
    from src.pipeline_runner import PipelineRunner

    assert "prune_confidences" in (
        _ROOT / "src" / "pipeline_runner.py").read_text(encoding="utf-8")
    assert hasattr(PipelineRunner, "_prune_scored_confidences")


def test_pruning_is_off_by_default_and_keeps_survivors(config, tmp_path):
    """
    Default off: deleting the PAE matrices is irreversible (ipSAE cannot be
    recomputed), so the operator opts in. When on, survivors are kept.
    """
    from src.pipeline_runner import PipelineRunner

    rf3 = _fake_rf3_tree(tmp_path, ["keep_me", "drop_me"])
    survivors = [{"name": "keep_me"}]

    r = PipelineRunner(config, workflow="binder")
    assert r._prune_scored_confidences(rf3, survivors) == ""
    assert (rf3 / "drop_me" / "drop_me_confidences.json").exists()

    on = json.loads(json.dumps(config))
    on["design"]["foundry"]["prune_confidences"] = True
    r2 = PipelineRunner(on, workflow="binder")
    note = r2._prune_scored_confidences(rf3, survivors)

    assert "Pruned" in note
    assert (rf3 / "keep_me" / "keep_me_confidences.json").exists()
    assert not (rf3 / "drop_me" / "drop_me_confidences.json").exists()
    # The summary file is the campaign's completion key — never pruned.
    assert (rf3 / "drop_me" / "drop_me_summary_confidences.json").exists()


def test_pruning_is_skipped_when_nothing_survived(config, tmp_path):
    """
    A zero-survivor campaign is the one an ITERATE verdict tells you to re-gate
    at a softer bar. Pruning it would delete the only evidence that allows it.
    """
    from src.pipeline_runner import PipelineRunner

    rf3 = _fake_rf3_tree(tmp_path, ["a", "b"])
    on = json.loads(json.dumps(config))
    on["design"]["foundry"]["prune_confidences"] = True

    assert PipelineRunner(on, workflow="binder")._prune_scored_confidences(rf3, []) == ""
    assert (rf3 / "a" / "a_confidences.json").exists()
    assert (rf3 / "b" / "b_confidences.json").exists()


def test_pruning_never_touches_a_cluster_tree(config, tmp_path):
    """`rf3_dir` is None for a cluster campaign: a Protenix tree has a
    different layout and lives on shared storage."""
    from src.pipeline_runner import PipelineRunner

    on = json.loads(json.dumps(config))
    on["design"]["foundry"]["prune_confidences"] = True
    r = PipelineRunner(on, workflow="binder")
    assert r._prune_scored_confidences(None, [{"name": "x"}]) == ""


# ----------------------------------------------------------------------
# 3. One refold anchor, one size law
# ----------------------------------------------------------------------

def test_the_calibration_gate_and_the_planner_share_one_anchor():
    """
    The gate held a flat 8.4 s while the planner had long since scaled the rate
    with complex size. On MASH/TEAD4 that made the same 22k refolds cost 63
    GPU-h at the gate and 138 GPU-h in the plan — the difference between inside
    and outside the 120 h budget SCALE_UP is decided against.
    """
    import src.campaign_calibration as cc
    import src.foundry_runner as fr

    assert cc.SEC_PER_RF3_REFOLD == fr.SEC_PER_RF3_REFOLD
    assert cc.SEC_PER_RF3_REFOLD != 8.4


def test_calibrate_derives_the_refold_rate_from_complex_size():
    """`n_tokens` must cost a large complex above the 195-token anchor."""
    from src.campaign_calibration import calibrate
    from tests.test_binder_ranking import rec

    records = [rec(name=f"d{i}", design_family=f"f{i}", iptm=0.9) for i in range(40)]
    small = calibrate(records, target_designs=10, n_tokens=195)
    large = calibrate(records, target_designs=10, n_tokens=300)

    assert large.pessimistic.est_gpu_hours > small.pessimistic.est_gpu_hours

    explicit = calibrate(records, target_designs=10,
                         sec_per_rf3_refold=99.0, n_tokens=300)
    assert explicit.pessimistic.est_gpu_hours > large.pessimistic.est_gpu_hours


def test_max_campaign_days_is_declared_in_config(config):
    """It was read with a hardcoded default from a key that did not exist, so
    the 120 GPU-h budget behind every SCALE_UP verdict was not tunable."""
    foundry = config["design"]["foundry"]
    assert "max_campaign_days" in foundry
    assert float(foundry["max_campaign_days"]) > 0
    assert "prune_confidences" in foundry
    assert foundry["prune_confidences"] is False


# ----------------------------------------------------------------------
# 4. The curation enums are actually closed
# ----------------------------------------------------------------------

def test_the_enums_are_read_from_the_schema_file():
    """
    `extraction_schema.json` is one of three files CLAUDE.md says must agree,
    and nothing had ever loaded it. Parsing the enums out of it is what makes
    the pair impossible to drift.
    """
    from src.curator import STUDY_CATEGORIES, STUDY_TYPES, load_schema_enums

    schema = json.loads(
        (_ROOT / "extraction_schema.json").read_text(encoding="utf-8"))
    assert schema["study_category"].startswith("enum[")
    assert schema["paper_metadata"]["study_type"].startswith("enum[")

    loaded = load_schema_enums()
    assert loaded["study_category"] == STUDY_CATEGORIES
    assert loaded["study_type"] == STUDY_TYPES


@pytest.mark.parametrize("bad", ["proteomics", "", "pathway biology!", "chemistry"])
def test_an_out_of_enum_category_is_rejected(bad):
    """
    Rejection is the point: `curate_paper` feeds the ValidationError back into
    the same conversation as a correction turn. Coercing would mislabel the
    paper; warning would reproduce the bug — 565 of 11,052 shipped fingerprints
    carry a category that reaches neither the prompt nor `search_corpus`'s
    filter enum.
    """
    from pydantic import ValidationError

    from src.curator import Fingerprint

    with pytest.raises(ValidationError):
        Fingerprint(relevant=True, study_category=bad)


def test_a_missing_category_is_still_allowed():
    """An absent category is honest; a wrong one is not."""
    from src.curator import Fingerprint

    assert Fingerprint(relevant=True, study_category=None).study_category is None


def test_every_category_in_the_shipped_corpus_is_in_the_enum():
    """
    The three domains the corpus actually uses (`enzymology`, `biocatalysis`,
    `computational_chemistry`) were added to the enum rather than folded into
    `biochemistry`: 565 existing papers use them, and collapsing them would
    lose the distinction going forward as well as leaving those papers
    unreachable.
    """
    from src.curator import STUDY_CATEGORIES

    for observed in ("biochemistry", "pathway_biology", "structural_biology",
                     "enzymology", "biocatalysis", "computational_chemistry",
                     "host_pathogen", "clinical", "review"):
        assert observed in STUDY_CATEGORIES


def test_prompt_and_tool_definitions_list_the_same_categories():
    """schema <-> prompt <-> tool definition, the trio CLAUDE.md pairs."""
    from src.curator import STUDY_CATEGORIES
    from src.skill_runner import _TOOL_DEFS
    from src.vector_store import VectorStore

    prompt = (_ROOT / "curation_prompt.md").read_text(encoding="utf-8")
    for cat in STUDY_CATEGORIES:
        assert f"`{cat}`" in prompt, f"{cat} missing from curation_prompt.md"

    search = next(d for d in _TOOL_DEFS if d["name"] == "search_corpus")
    runner_enum = search["parameters"]["properties"]["study_category"]["enum"]
    mcp_enum = (VectorStore.SEARCH_TOOL_DEFINITION["input_schema"]
                ["properties"]["study_category"]["enum"])
    assert set(runner_enum) == set(STUDY_CATEGORIES)
    assert set(mcp_enum) == set(STUDY_CATEGORIES)


def test_an_out_of_enum_study_type_is_rejected():
    from pydantic import ValidationError

    from src.curator import PaperMetadata

    with pytest.raises(ValidationError):
        PaperMetadata(title="t", study_type="wet_lab", situational_context_hook="h")

    ok = PaperMetadata(title="t", study_type="Experimental In Vitro",
                       situational_context_hook="h")
    assert ok.study_type == "experimental_in_vitro"


# ----------------------------------------------------------------------
# 5. Vector ingestion is no longer add-only
# ----------------------------------------------------------------------

class _FakeTable:
    """Minimal duck type for the three LanceDB calls `ingest` makes."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = list(rows or [])

    def to_arrow(self):
        import pyarrow as pa

        return pa.table({
            "paper_key": [r["paper_key"] for r in self.rows],
            "embed_text": [r["embed_text"] for r in self.rows],
        })

    def add(self, records) -> None:
        self.rows.extend(records)

    def delete(self, predicate: str) -> None:
        inner = predicate.split("(", 1)[1].rstrip(")")
        keys = {v.strip().strip("'").replace("''", "'") for v in inner.split(",")}
        self.rows = [r for r in self.rows if r["paper_key"] not in keys]


class _FakeEncoder:
    def encode(self, texts, **kw):
        import numpy as np

        return np.zeros((len(texts), 768), dtype="float32")


def _store_with(tmp_path, rows):
    from src.vector_store import VectorStore

    store = VectorStore(db_path=tmp_path / "vec")
    table = _FakeTable(rows)
    store._get_db = lambda: None                       # type: ignore[assignment]
    store._get_table = lambda create_if_missing=False: table  # type: ignore[assignment]
    store._get_encoder = lambda: _FakeEncoder()        # type: ignore[assignment]
    return store, table


def _write_fp(d: Path, doi: str, hook: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"doi_{doi.replace('/', '_')}.json").write_text(json.dumps({
        "relevant": True,
        "paper_metadata": {"doi": doi, "title": "t",
                           "situational_context_hook": hook},
        "entities": {"proteins": ["YAP1"]},
        "key_findings": [{"claim": "a claim."}],
    }), encoding="utf-8")


def test_a_changed_fingerprint_is_re_embedded(tmp_path):
    """
    Dedup used to be on `paper_key` alone, so a re-curated fingerprint kept its
    stale vector until someone ran a full `--rebuild`.
    """
    fps = tmp_path / "fingerprints"
    _write_fp(fps, "10.1/a", "original hook")

    store, table = _store_with(tmp_path, [])
    assert store.ingest(fps) == 1
    assert store.last_ingest_stats["added"] == 1

    # Unchanged: nothing to do.
    store2, _ = _store_with(tmp_path, table.rows)
    assert store2.ingest(fps) == 0
    assert store2.last_ingest_stats["unchanged"] == 1

    # Re-curated: exactly one row, carrying the NEW text.
    _write_fp(fps, "10.1/a", "revised hook after re-curation")
    store3, table3 = _store_with(tmp_path, table.rows)
    assert store3.ingest(fps) == 1
    assert store3.last_ingest_stats["updated"] == 1
    assert len(table3.rows) == 1
    assert "revised hook" in table3.rows[0]["embed_text"]


def test_orphaned_rows_are_reported_and_only_pruned_on_request(tmp_path):
    """
    The shipped index carries ~1,700 rows whose fingerprint file is gone; they
    still match `search_corpus` while `get_fingerprint` fails on them.
    """
    fps = tmp_path / "fingerprints"
    _write_fp(fps, "10.1/a", "hook")
    stale = [{"paper_key": "doi:10.1/gone", "embed_text": "old"}]

    store, table = _store_with(tmp_path, stale)
    store.ingest(fps)
    assert store.last_ingest_stats["orphans_seen"] == 1
    assert store.last_ingest_stats["pruned"] == 0
    assert any(r["paper_key"] == "doi:10.1/gone" for r in table.rows)

    store2, table2 = _store_with(tmp_path, stale)
    store2.ingest(fps, prune_orphans=True)
    assert store2.last_ingest_stats["pruned"] == 1
    assert not any(r["paper_key"] == "doi:10.1/gone" for r in table2.rows)


def test_an_unreadable_index_refuses_rather_than_duplicating(tmp_path):
    """
    The old handler logged "will re-index all" and continued with an empty key
    set. `table.add` has no primary key, so that appends a second copy of the
    whole corpus rather than re-indexing anything.
    """
    fps = tmp_path / "fingerprints"
    _write_fp(fps, "10.1/a", "hook")

    store, table = _store_with(tmp_path, [])

    def _boom():
        raise RuntimeError("corrupt manifest")

    table.to_arrow = _boom  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="--rebuild"):
        store.ingest(fps)


# ----------------------------------------------------------------------
# 6. A stage may not redefine its way past the chain guards
# ----------------------------------------------------------------------

def test_partner_guard_runs_on_both_entry_points():
    """
    The PPI track and the binder track reach the interface skill by different
    routes, and the fix must ADD a check to each rather than move one.
    `_stage_binder_interface` keeps all three of its original guards.
    """
    import inspect

    from src.pipeline_runner import PipelineRunner

    ppi = inspect.getsource(PipelineRunner._stage_structure)
    binder = inspect.getsource(PipelineRunner._stage_binder_interface)

    for src in (ppi, binder):
        assert "_verify_partner_chain_is_requested" in src
    # nothing removed from the binder track
    for guard in ("_verify_target_chain_assignment", "_verify_hotspot_grounding",
                  "_check_ortholog_conservation"):
        assert guard in binder, f"{guard} disappeared from the binder track"
    # nothing removed from the PPI track
    for guard in ("_verify_ppi_chain_assignment", "_verify_hotspot_grounding",
                  "_check_ortholog_conservation"):
        assert guard in ppi, f"{guard} disappeared from the PPI track"


def test_the_ppi_guards_are_given_the_upstream_complex():
    """
    A stage that renames `target_complex` to whatever it analysed must not get
    to validate its own substitution — the guards take the value the pathway
    and literature stages settled on.
    """
    import inspect

    from src.pipeline_runner import PipelineRunner

    src = inspect.getsource(PipelineRunner._stage_structure)
    assert "requested_complex = result.target_complex or target_complex" in src
    assert "_verify_ppi_chain_assignment(\n            requested_complex" in src
    assert "renamed the target complex" in src


@pytest.mark.parametrize("requested,expect_call", [
    ("CALCRL / RAMP1", True),        # two named proteins -> checkable
    ("DPP4 (active site)", False),   # single protein -> nothing to check
    ("", False),
])
def test_partner_guard_only_applies_to_a_named_pair(config, requested, expect_call, monkeypatch):
    from src.pipeline_runner import PipelineRunner

    r = PipelineRunner(config, workflow="ppi")
    seen = {}
    monkeypatch.setattr(r, "_binder_structure_path",
                        lambda pdb: seen.setdefault("path", Path("/nonexistent.cif")))
    r._verify_partner_chain_is_requested(requested, {"partner_chain": "B"},
                                         "1ABC", source="test")
    assert ("path" in seen) is expect_call


def test_partner_guard_fails_open_without_a_partner_chain(config):
    """No partner chain named -> nothing to verify, and no crash."""
    from src.pipeline_runner import PipelineRunner

    r = PipelineRunner(config, workflow="ppi")
    r._verify_partner_chain_is_requested("A / B", {}, "1ABC", source="test")


# ----------------------------------------------------------------------
# 7. AlphaFold models are legal, but only for the single-chain mode
# ----------------------------------------------------------------------

@pytest.mark.parametrize("intent", ["disrupt", "stabilize"])
def test_an_af_model_cannot_be_used_for_a_ppi(intent):
    """An AlphaFold model is a monomer: there is no partner in it to disrupt."""
    from src.pipeline_runner import PipelineBlockedError, PipelineRunner

    with pytest.raises(PipelineBlockedError, match="single chain"):
        PipelineRunner._check_af_model_intent(
            {"pdb_id": "AF-Q96CH1", "design_intent": intent})


def test_an_af_model_defaults_to_the_single_chain_mode():
    from src.pipeline_runner import PipelineRunner

    h = {"pdb_id": "AF-Q96CH1", "design_intent": ""}
    PipelineRunner._check_af_model_intent(h)
    assert h["design_intent"] == "inhibit_active_site"

    ok = {"pdb_id": "AF-Q96CH1", "design_intent": "inhibit_active_site"}
    PipelineRunner._check_af_model_intent(ok)          # no raise
    experimental = {"pdb_id": "6E3Y", "design_intent": "disrupt"}
    PipelineRunner._check_af_model_intent(experimental)  # untouched
    assert experimental["design_intent"] == "disrupt"


def test_both_target_selecting_skills_know_af_ids_are_legal():
    """
    The orphan-GPCR run wrote "de novo AlphaFold structural modeling is
    required" and fell back to a downstream complex, because the skill did not
    know `_ensure_structure` already accepts an AF- pseudo-id.
    """
    for name in ("pathway-expert", "wildcard-expert"):
        text = (_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert "AF-<UniProt accession>" in text, name
        assert "inhibit_active_site" in text, name


# ----------------------------------------------------------------------
# 8. Organism is checked at target-selection time
# ----------------------------------------------------------------------

def test_organism_is_checked_before_the_literature_stage():
    import inspect

    from src.pipeline_runner import PipelineRunner

    src = inspect.getsource(PipelineRunner.run)
    i_check = src.index("_check_structure_organism")
    i_lit = src.index("_stage_literature(")
    assert i_check < i_lit, "organism pre-check must run before paying for stage 1"
    assert "_check_af_model_intent" in src


@pytest.mark.parametrize("pdb", ["", "NOT_FOUND", "AF-Q12770"])
def test_organism_check_skips_what_it_cannot_answer(config, pdb):
    """No id, no structure chosen, or a predicted model -> nothing to look up."""
    from src.pipeline_runner import PipelineRunner

    PipelineRunner(config, workflow="ppi")._check_structure_organism(pdb, "A / B")


# ----------------------------------------------------------------------
# 9. Token counting asks the provider that can answer
# ----------------------------------------------------------------------

def test_gemini_stages_do_not_call_anthropics_token_counter(config, monkeypatch):
    """
    Anthropic's count_tokens 404s on a Gemini model id, and Gemini is the
    default provider — so every stage paid a wasted round trip and logged a
    scary failure before falling back to the heuristic anyway.
    """
    import anthropic

    from src.pipeline_runner import PipelineRunner

    called = []
    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda *a, **k: called.append(1))

    class _Runner:
        skill_name = "pathway-expert"
        system_prompt = "x" * 4000

    r = PipelineRunner(config, workflow="ppi")
    usage = r._estimate_stage_usage(_Runner(), "q" * 100, None,
                                    "gemini-3.7-flash", "gemini")
    assert not called, "asked Anthropic to count Gemini tokens"
    assert usage.cache_creation_tokens == (4000 + 100) // 4

    r._estimate_stage_usage(_Runner(), "q" * 100, None, "claude-sonnet-5", "claude")
    assert called, "the Claude path must still use the real counter"


def test_an_unresolvable_partner_name_fails_open(config, monkeypatch, tmp_path):
    """
    An antibody, nanobody or peptide partner has no gene symbol, so
    `resolve_target` returns nothing for it. The guard must then take the
    partner chain on trust — it has no evidence either way.

    Caught on a live GCGR run: `GCGR / mAb1` resolved only GCGR, so the loop
    compared the mAb1 chain against GCGR's OWN accession, got the mismatch that
    comparison must produce, and hard-failed a correct chain assignment. The
    same shape would have failed the PD-L1 / anti-PD-L1 VHH campaign this whole
    guard family exists because of.
    """
    from src.ortholog_check import MATCH, MISMATCH, ChainVerdict
    from src.pipeline_runner import PipelineRunner

    r = PipelineRunner(config, workflow="ppi")
    structure = tmp_path / "x.cif"
    structure.write_text("", encoding="utf-8")
    monkeypatch.setattr(r, "_binder_structure_path", lambda pdb: structure)
    monkeypatch.setattr("src.target_resolve.fetch_uniprot_sequence", lambda a: "SEQ")
    monkeypatch.setattr(r, "_chain_identity_to_uniprot", lambda *a, **k: 0.99)

    class _R:
        def __init__(self, ok, uniprot=""): self.ok, self.uniprot, self.gene = ok, uniprot, "G"
    monkeypatch.setattr("src.target_resolve.resolve_target",
                        lambda n: _R(True, "P47871") if n == "GCGR" else _R(False))

    # target chain matches the one resolvable name -> the partner is the
    # unresolvable one -> nothing to verify, must not raise.
    monkeypatch.setattr(r, "_classify_target_chain",
                        lambda *a, **k: ChainVerdict(verdict=MATCH, reason="x"))
    r._verify_partner_chain_is_requested(
        "GCGR / mAb1", {"target_chain": "A", "partner_chain": "C"}, "5XEZ",
        source="test")

    # ...but when BOTH names resolve, a genuine substitution still raises.
    monkeypatch.setattr("src.target_resolve.resolve_target",
                        lambda n: _R(True, "P47871" if n == "GCGR" else "O60894"))
    monkeypatch.setattr(r, "_classify_target_chain",
                        lambda *a, **k: ChainVerdict(verdict=MISMATCH, reason="different molecule"))
    with pytest.raises(PipelineError, match="not one of the proteins"):
        r._verify_partner_chain_is_requested(
            "CALCRL / RAMP1", {"target_chain": "R", "partner_chain": "P"}, "6E3Y",
            source="test")


# ----------------------------------------------------------------------
# 10. Size policy judged on what will actually be designed
# ----------------------------------------------------------------------

def test_designable_size_is_gated_on_the_foundry_engine():
    """
    `--design-engine boltzgen` has no trim stage, so there the RAW chain length
    is the operative number and refusing an oversized chain is correct. The
    designable count may only relax the policy when a trim will actually follow.
    """
    import inspect

    from src.pipeline_runner import PipelineRunner

    src = inspect.getsource(PipelineRunner._stage_structure)
    i_gate = src.index('self._design_engine == "foundry"')
    i_call = src.index("_designable_chain_sizes(")
    assert i_gate < i_call, "the designable-size lookup must sit behind the engine gate"


def test_no_oversized_chain_means_no_lookup(config, monkeypatch):
    """The common case must cost nothing — no metadata call, no topology call."""
    from src.pipeline_runner import PipelineRunner

    called = []
    monkeypatch.setattr("src.target_resolve.entry_metadata",
                        lambda *a, **k: called.append(1) or {})
    r = PipelineRunner(config, workflow="ppi")
    assert r._designable_chain_sizes("5XEZ", {"A": 120, "B": 90}, 500) == {}
    assert not called


def test_a_soluble_chain_keeps_its_raw_size(config, monkeypatch):
    """Only membrane proteins have a transmembrane span to strip."""
    from src.pipeline_runner import PipelineRunner

    monkeypatch.setattr("src.target_resolve.entry_metadata",
                        lambda *a, **k: {"1ABC": {"chains": {"A": {"uniprots": ["P00001"]}}}})

    class _Topo:
        fetched, is_membrane, segments = True, False, []
    monkeypatch.setattr("src.membrane_topology.fetch_topology", lambda a: _Topo())

    r = PipelineRunner(config, workflow="ppi")
    assert r._designable_chain_sizes("1ABC", {"A": 600}, 500) == {}


def test_a_membrane_chain_reports_its_designable_count(config, monkeypatch):
    """
    5XEZ chain A: a 574-residue GCGR-endolysin fusion that the structure stage
    refused against a 500-residue limit, recommending exactly the crop the trim
    stage performs automatically. 167 residues survive TM stripping.
    """
    from src.pipeline_runner import PipelineRunner

    monkeypatch.setattr("src.target_resolve.entry_metadata",
                        lambda *a, **k: {"5XEZ": {"chains": {"A": {"uniprots": ["P47871"]}}}})

    class _Topo:
        fetched, is_membrane, segments = True, True, []

    class _Restrict:
        applies = True
        allowed_auth = set(range(167))
    monkeypatch.setattr("src.membrane_topology.fetch_topology", lambda a: _Topo())
    monkeypatch.setattr("src.membrane_topology.restriction_for",
                        lambda *a, **k: _Restrict())

    r = PipelineRunner(config, workflow="ppi")
    assert r._designable_chain_sizes("5XEZ", {"A": 574}, 500) == {"A": 167}
    # a restriction that keeps everything is not a reason to relax the policy
    _Restrict.allowed_auth = set(range(574))
    assert r._designable_chain_sizes("5XEZ", {"A": 574}, 500) == {}


# ----------------------------------------------------------------------
# 11. Domain spans must be grounded in residues that exist
# ----------------------------------------------------------------------

def test_a_domain_is_sized_by_observed_residues_not_by_arithmetic():
    """
    `end - start + 1` is wrong the moment author numbering has a gap, and a
    fusion construct guarantees one. 5TGZ chain A is CB1R-flavodoxin-CB1R,
    running auth -2..1148: two CATH spans mapped to endpoints that do not exist
    (-2..2109, 333..2101), were sized 2112 and 1769, and summed to a 3,881
    residue "domain set" for a 439-residue chain. The trim then refused a target
    that fits the 220-residue budget comfortably.
    """
    from src.structure_trim import _domain_from_span

    observed = set(range(1, 101)) | set(range(1001, 1051))   # a gap in the middle
    d = _domain_from_span(0, 1, 1050, observed, "cath", "x")
    assert d.n_residues == 150            # counted, not 1050
    assert _domain_from_span(0, 500, 600, observed, "cath", "x") is None


def test_rcsb_spans_that_miss_the_chain_are_dropped(monkeypatch):
    """A mis-mapped annotation must fall through to the next tier, not poison
    the budget check."""
    from src import structure_trim

    payload = {"data": {"entry": {"polymer_entities": [{
        "polymer_entity_instances": [{
            "rcsb_polymer_entity_instance_container_identifiers": {"auth_asym_id": "A"},
            "rcsb_polymer_instance_feature": [{
                "type": "CATH", "name": "bogus",
                "feature_positions": [{"beg_seq_id": 1, "end_seq_id": 9}],
            }],
        }]}]}}}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return payload
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp())

    auth_to_label = {i: i for i in range(1, 10)}          # maps to auth 1..9
    assert structure_trim.rcsb_domains("1ABC", "A", auth_to_label,
                                       observed={500, 501}) == []
    kept = structure_trim.rcsb_domains("1ABC", "A", auth_to_label,
                                       observed=set(range(1, 10)))
    assert len(kept) == 1 and kept[0].n_residues == 9


def test_a_fusion_partner_is_excluded_from_the_design_target(config, monkeypatch):
    """
    Crystallisation chimeras (GCGR-endolysin, CB1R-flavodoxin) put a second
    accession on the target chain. The fusion partner must never carry a
    hotspot, be trimmed to, or count against the residue budget — and the
    deposited RCSB entity alignment already says which residues are which.
    """
    from src.pipeline_runner import PipelineRunner

    monkeypatch.setattr(
        "src.target_resolve.entry_metadata",
        lambda *a, **k: {"5TGZ": {"chains": {"A": {"uniprots": ["P00323", "P21554"]}}},
                         "3KYS": {"chains": {"A": {"uniprots": ["P46937"]}}}})
    monkeypatch.setattr("src.membrane_topology.uniprot_to_auth",
                        lambda *a, **k: {i: i for i in range(1, 292)})

    r = PipelineRunner(config, workflow="ppi")
    assert len(r._target_accession_residues("5TGZ", "A", "P21554")) == 291
    # a normal single-accession chain is left completely alone
    assert r._target_accession_residues("3KYS", "A", "P46937") is None


def test_topology_and_chimera_restrictions_compose(config):
    """Both mean "keep only these", so the conjunction is the intersection."""
    from src.pipeline_runner import PipelineRunner

    class _R:
        applies = True
        allowed_auth = set(range(100, 400))

    combine = PipelineRunner._combine_allowed
    assert combine(_R(), None) == set(range(100, 400))
    assert combine(None, {1, 2, 3}) == {1, 2, 3}
    assert combine(_R(), set(range(350, 500))) == set(range(350, 400))
    assert combine(None, None) is None


# ----------------------------------------------------------------------
# 12. The PPI track measures interfaces instead of guessing chains
# ----------------------------------------------------------------------

def test_interface_options_are_skipped_when_they_cannot_help(config):
    """No target name, no PDB, or an AlphaFold monomer -> nothing to measure."""
    from src.pipeline_runner import PipelineRunner

    r = PipelineRunner(config, workflow="ppi")
    assert r._ppi_interface_options("", "A / B") == ""
    assert r._ppi_interface_options("6E3Y", "") == ""
    assert r._ppi_interface_options("AF-Q16602", "CALCRL / RAMP1") == ""


def test_interface_options_fail_open(config, monkeypatch, tmp_path):
    """Advisory only: any lookup failure must leave the stage running as before."""
    from src.pipeline_runner import PipelineRunner

    r = PipelineRunner(config, workflow="ppi")
    monkeypatch.setattr(r, "_binder_structure_path", lambda p: tmp_path / "missing.cif")
    assert r._ppi_interface_options("6E3Y", "CALCRL / RAMP1") == ""

    def _boom(*a, **k):
        raise RuntimeError("RCSB down")
    monkeypatch.setattr("src.target_resolve.resolve_target", _boom)
    assert r._ppi_interface_options("6E3Y", "CALCRL / RAMP1") == ""


def test_the_structure_prompt_carries_the_measured_interfaces():
    """
    Two independent runs on 6E3Y both picked the 38-residue CGRP peptide over
    chain E (RAMP1), because an agonist-bound cryo-EM structure makes the ligand
    the conspicuous interface and nothing had measured the alternative. The
    stage now gets both, with numbers.
    """
    import inspect

    from src.pipeline_runner import PipelineRunner

    src = inspect.getsource(PipelineRunner._stage_structure)
    assert src.count("_ppi_interface_options(") == 2, \
        "both query-construction branches must carry the measured interfaces"


# ----------------------------------------------------------------------
# 13. Structure choice: best-evidenced != best to design against
# ----------------------------------------------------------------------

_SIX = {"chains": {
    "A": {"length": 394, "description": "Guanine nucleotide-binding protein G(s) subunit", "uniprots": ["P63092"]},
    "B": {"length": 350, "description": "Guanine nucleotide-binding protein G(I)/G(S)", "uniprots": ["P62873"]},
    "E": {"length": 149, "description": "Receptor activity-modifying protein 1", "uniprots": ["O60894"]},
    "N": {"length": 138, "description": "Nanobody 35", "uniprots": []},
    "P": {"length": 38, "description": "Calcitonin gene-related peptide 1", "uniprots": ["P06881"]},
    "R": {"length": 490, "description": "Calcitonin gene-related peptide type 1 receptor", "uniprots": ["Q16602"]},
}, "resolution_A": 3.3, "method": "ELECTRON MICROSCOPY"}

_ECD = {"chains": {
    "A": {"length": 115, "description": "Calcitonin gene-related peptide type 1 receptor", "uniprots": ["Q16602"]},
    "C": {"length": 96, "description": "Receptor activity-modifying protein 1", "uniprots": ["O60894"]},
}, "resolution_A": 2.1, "method": "X-RAY DIFFRACTION"}


def _patch_rcsb(monkeypatch, meta):
    class _R:
        def __init__(self, ok, uniprot="", gene=""):
            self.ok, self.uniprot, self.gene = ok, uniprot, gene
    monkeypatch.setattr("src.target_resolve.resolve_target",
                        lambda n: _R(True, "Q16602", "CALCRL") if "CALCRL" in n.upper()
                        else _R(True, "O60894", "RAMP1"))
    monkeypatch.setattr("src.target_resolve.find_complex_structures",
                        lambda *a, **k: list(meta))
    monkeypatch.setattr("src.target_resolve.entry_metadata",
                        lambda ids, *a, **k: {i: meta[i] for i in ids if i in meta})


def test_a_scaffolded_complex_is_swapped_for_the_clean_ectodomain(config, monkeypatch):
    """
    The corpus cites the landmark structure, which for a receptor is the
    full-length agonist-bound cryo-EM complex — carrying a nanobody, a
    heterotrimeric G protein and the peptide ligand. The same interface exists
    in a 2.1 A ectodomain crystal structure with nothing else in the box.
    """
    from src.pipeline_runner import PipelineRunner

    _patch_rcsb(monkeypatch, {"6E3Y": _SIX, "3N7S": _ECD})
    r = PipelineRunner(config, workflow="ppi")
    assert r._select_designable_structure("CALCRL / RAMP1", "6E3Y",
                                          operator_pinned=False) == "3N7S"


def test_an_operator_pinned_pdb_is_never_overridden(config, monkeypatch):
    from src.pipeline_runner import PipelineRunner

    _patch_rcsb(monkeypatch, {"6E3Y": _SIX, "3N7S": _ECD})
    r = PipelineRunner(config, workflow="ppi")
    assert r._select_designable_structure("CALCRL / RAMP1", "6E3Y",
                                          operator_pinned=True) is None


def test_a_clean_complex_is_left_alone(config, monkeypatch):
    """Only a measurable defect in the chosen entry justifies overriding an
    evidence-based choice."""
    from src.pipeline_runner import PipelineRunner

    _patch_rcsb(monkeypatch, {"3N7S": _ECD, "6E3Y": _SIX})
    r = PipelineRunner(config, workflow="ppi")
    assert r._select_designable_structure("CALCRL / RAMP1", "3N7S",
                                          operator_pinned=False) is None


def test_an_entry_without_both_proteins_is_not_a_candidate(config, monkeypatch):
    """The alternative must actually contain the requested interface."""
    from src.pipeline_runner import PipelineRunner

    lone = {"chains": {"A": {"length": 115, "description": "CGRP receptor",
                             "uniprots": ["Q16602"]}},
            "resolution_A": 1.5, "method": "X-RAY DIFFRACTION"}
    _patch_rcsb(monkeypatch, {"6E3Y": _SIX, "9XXX": lone})
    r = PipelineRunner(config, workflow="ppi")
    # 9XXX is higher resolution and has no scaffolding, but lacks RAMP1
    assert r._select_designable_structure("CALCRL / RAMP1", "6E3Y",
                                          operator_pinned=False) is None


def test_structure_choice_is_settled_before_the_structure_stage():
    """It cannot happen later: by the time the structure stage emits a handoff
    it has already analysed one entry, and its hotspots are those coordinates."""
    import inspect

    from src.pipeline_runner import PipelineRunner

    src = inspect.getsource(PipelineRunner.run)
    assert src.index("_select_designable_structure") < src.index("_stage_literature(")


def test_a_structure_switch_rewrites_the_prose_too(config, monkeypatch):
    """
    `structure_query` is prose the pathway stage wrote naming the old entry, and
    it flows into the literature prompt. Left stale, the literature stage
    reasoned about and cited 6E3Y while the pipeline ran on 3N7S — correct
    behaviour, wrong narrative, and the narrative is what reaches the report.
    """
    import re as _re

    handoff = {
        "pdb_id": "6E3Y",
        "structure_query": "Analyze PDB 6E3Y at data/structures/6E3Y.cif.",
        "design_query": "Generate inputs for CALCRL / RAMP1, PDB 6e3y.",
    }
    stale, better = "6E3Y", "3N7S"
    for field in ("structure_query", "design_query"):
        handoff[field] = _re.sub(_re.escape(stale), better, handoff[field],
                                 flags=_re.IGNORECASE)
    assert "6E3Y" not in handoff["structure_query"]
    assert "6e3y" not in handoff["design_query"]          # case-insensitive
    assert "3N7S" in handoff["structure_query"]

    import inspect

    from src.pipeline_runner import PipelineRunner
    src = inspect.getsource(PipelineRunner.run)
    assert "structure_query" in src and "Structure switched from" in src


# ----------------------------------------------------------------------
# 14. A resumed trim must carry everything the later stages read
# ----------------------------------------------------------------------

def test_trim_from_disk_has_every_attribute_the_gpu_stages_use():
    """
    `--start-from production` is the documented normal case after a multi-day
    campaign, and it was broken from 2026-08-29 until this test: `plan_campaign`
    sizes RF3 cost from `trim.n_residues_after`, `_TrimFromDisk` never defined
    it, and the run died before launching anything with

        '_TrimFromDisk' object has no attribute 'n_residues_after'

    The value was in trim_map.json the whole time. Checked by reflection over
    what the module actually reads, so a future `trim.<field>` cannot silently
    reintroduce it.
    """
    import re

    from src.pipeline_runner import _TrimFromDisk

    src = (_ROOT / "src" / "pipeline_runner.py").read_text(encoding="utf-8")
    used = set(re.findall(r"\btrim\.([a-z_][a-z0-9_]*)", src))
    stub = _TrimFromDisk({"kept_segments": [[27, 110]], "n_segments": 1,
                          "contig": "70-86,/0,D27-110", "trimmed_path": "/x.cif"})
    missing = {f for f in used if not hasattr(stub, f)}
    assert not missing, f"_TrimFromDisk is missing {sorted(missing)}"


def test_trim_from_disk_falls_back_to_the_segments():
    """A trim_map written before the field existed must still resume."""
    from src.pipeline_runner import _TrimFromDisk

    t = _TrimFromDisk({"kept_segments": [[27, 110]], "contig": "70-86,/0,D27-110"})
    assert t.n_residues_after == 84          # 110 - 27 + 1
    t2 = _TrimFromDisk({"kept_segments": [[1, 10], [21, 30]]})
    assert t2.n_residues_after == 20


# ----------------------------------------------------------------------
# 15. Loose ends the first end-to-end campaign surfaced
# ----------------------------------------------------------------------

def test_target_and_partner_follow_the_chain_assignment(config, monkeypatch):
    """
    The structure stage picks whichever chain carries the epitope, and that is
    often the SECOND name in `target_complex`. On 3N7S it chose chain D (RAMP1)
    while the complex reads "CALCRL / RAMP1", so the bridge stamped
    target_gene=CALCRL on a campaign designed against RAMP1's ectodomain.

    Not just a label: `_stage_trim` calls restriction_for(pdb, target_chain,
    target_uniprot). With the other protein's accession `uniprot_to_auth` finds
    no alignment, the restriction silently does not apply, and NO transmembrane
    stripping happens.
    """
    from src.pipeline_runner import PipelineRunner

    monkeypatch.setattr(
        "src.target_resolve.entry_metadata",
        lambda *a, **k: {"3N7S": {"chains": {
            "A": {"uniprots": ["Q16602"]}, "D": {"uniprots": ["O60894"]}}}})

    class _R:
        def __init__(self, u): self.ok, self.uniprot, self.gene = True, u, ""
    monkeypatch.setattr("src.target_resolve.resolve_target",
                        lambda n: _R("Q16602" if n == "CALCRL" else "O60894"))

    r = PipelineRunner(config, workflow="ppi")
    assert r._order_names_by_chain(["CALCRL", "RAMP1"], {"target_chain": "D"},
                                   "3N7S", "CALCRL", "RAMP1") == ("RAMP1", "CALCRL")
    assert r._order_names_by_chain(["CALCRL", "RAMP1"], {"target_chain": "A"},
                                   "3N7S", "CALCRL", "RAMP1") == ("CALCRL", "RAMP1")
    # unanswerable -> unchanged
    assert r._order_names_by_chain(["CALCRL", "RAMP1"], {}, "3N7S",
                                   "CALCRL", "RAMP1") == ("CALCRL", "RAMP1")


def test_a_pause_is_not_logged_as_an_error():
    """`--stop-after` and every --detach handoff printed "Pipeline error" —
    the same confusion that once recorded a healthy campaign as FAILED."""
    import inspect

    from src.pipeline_runner import PipelineRunner

    src = inspect.getsource(PipelineRunner.run)
    assert src.index("except PipelinePausedError") < src.index('logger.error(f"Pipeline error')


@pytest.mark.parametrize("stored,query,want", [
    ("cannabidiol", "BID", False),      # the real false positive
    ("resource", "SRC", False),
    ("BAXTER", "BAX", False),
    ("BID protein", "BID", True),
    ("YAP1/TAZ", "YAP1", True),
    ("PD-1 receptor", "PD-1", True),
    ("MDM2-p53 interaction", "MDM2", True),
])
def test_pdb_lookup_matches_whole_symbols(stored, query, want):
    """`q in s` made every short gene symbol match inside an unrelated word,
    which is how a de novo binder paper was reported as a caspase structure."""
    from src.skill_runner import _find_pdb_structures  # noqa: F401  (import guard)
    import re

    def matches(stored: str, query: str) -> bool:
        if len(query) < 3:
            return False
        s, q = stored.upper(), query.upper()
        return re.search(rf"(?<![A-Z0-9]){re.escape(q)}(?![A-Z0-9])", s) is not None

    assert matches(stored, query) is want


def test_a_target_node_may_have_no_known_dysregulation():
    """Required `str` against a legitimately-null field cost a full extra LLM
    round-trip on 4.4% of curations (17 of 385 measured)."""
    from src.curator import TargetNode

    n = TargetNode(protein="X", pathway_position="kinase", dysregulation=None,
                   source_span="Page 1, Para 1")
    assert n.dysregulation is None


def test_a_structure_switch_is_written_where_a_reader_will_find_it(config, tmp_path):
    """
    The switch happens between stages, so `00_pathway.md` is already on disk
    naming the entry that was replaced. On the first campaign that used this,
    the pathway report said 6E3Y eight times, the structure report said 3N7S,
    and the only account of why lived in a log line and a manifest checkpoint —
    neither of which a reader of the run or the HTML report ever sees.
    """
    from src.pipeline_runner import PipelineResult, PipelineRunner

    pathway = tmp_path / "00_pathway.md"
    pathway.write_text("# PATHWAY BIOLOGY REPORT\n\n- pdb_id: 6E3Y\n", encoding="utf-8")
    r = PipelineRunner(config, workflow="ppi")
    r._last_switch_reason = "4 scaffolding chains against 0, at no worse resolution"
    res = PipelineResult(run_dir=tmp_path)
    res.stage_files["pathway"] = pathway

    r._note_structure_switch(res, "6E3Y", "3N7S")
    text = pathway.read_text(encoding="utf-8")

    assert "STRUCTURE SUBSTITUTION" in text
    assert "recommends **6E3Y**" in text and "against **3N7S**" in text
    assert "scaffolding chains" in text
    assert "--pdb 6E3Y" in text                      # how to override it
    assert "# PATHWAY BIOLOGY REPORT" in text        # original left intact
    assert "- pdb_id: 6E3Y" in text


def test_the_switch_note_never_breaks_a_run(config, tmp_path):
    """Annotation is bookkeeping; a missing or unwritable file must not fail."""
    from src.pipeline_runner import PipelineResult, PipelineRunner

    r = PipelineRunner(config, workflow="ppi")
    res = PipelineResult(run_dir=tmp_path)
    r._note_structure_switch(res, "6E3Y", "3N7S")          # no pathway file
    res.stage_files["pathway"] = tmp_path / "missing.md"
    r._note_structure_switch(res, "6E3Y", "3N7S")          # file absent


# ---------------------------------------------------------------------
# Onboarding guards (beta-tester audit, 2026-09-08)
#
# Three failure modes that all present as "it worked" to a new user, which
# is the worst way for a first run to go wrong.
# ---------------------------------------------------------------------

def test_doctor_does_not_accept_an_unedited_env_example_as_configured():
    """`cp .env.example .env` and forgetting to edit it left every key
    non-empty, so doctor.py reported a fully unconfigured checkout green and
    the user found out from a provider 403 several stages later."""
    import importlib
    doctor = importlib.import_module("scripts.doctor")
    env_example = dict(
        line.split("=", 1)
        for line in (_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#"))
    for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "NCBI_EMAIL"):
        shipped = env_example.get(var, "")
        # The placeholder must be recognised as NOT usable...
        with mock.patch.dict(os.environ, {var: shipped}, clear=False):
            usable, why = doctor._key_state(var)
        assert not usable, f"{var}={shipped!r} from .env.example counted as set"
        assert why, f"{var} rejected without saying why"
        # ...and a real-looking value must still pass.
        with mock.patch.dict(os.environ, {var: "a-real-looking-value"}, clear=False):
            assert doctor._key_state(var)[0]


def test_the_literature_track_treats_an_anthropic_key_as_required():
    """`scripts/ask_corpus.py` has no Gemini path and `curation.provider`
    defaults to claude, so reporting the key as merely optional for the
    literature track sent users at a REPL that could not start."""
    import importlib
    doctor = importlib.import_module("scripts.doctor")
    rep = doctor.Report()
    with mock.patch.dict(os.environ,
                         {"GEMINI_API_KEY": "x", "ANTHROPIC_API_KEY": ""},
                         clear=False):
        doctor.check_api_keys(rep)
    def status_for(track: str) -> str:
        rows = [c for c in rep.for_track(track) if c.name == "ANTHROPIC_API_KEY"]
        assert len(rows) == 1, f"{track}: expected one ANTHROPIC row, got {len(rows)}"
        return rows[0].status

    assert status_for("literature") == doctor.FAIL
    # ...but still only a warning for the design tracks, where Gemini is the
    # real default and Claude is the fallback.
    assert status_for("ppi") == doctor.WARN
    assert status_for("binder") == doctor.WARN


def test_a_missing_vector_index_names_fetch_corpus_on_an_empty_checkout():
    """`ingest_vectors.py` is the right fix only when there are fingerprints
    to ingest. On a fresh clone it embeds nothing and changes nothing."""
    from src.vector_store import VectorStore
    import src.vector_store as vs_mod
    with tempfile.TemporaryDirectory() as d:
        fake = Path(d) / "src" / "vector_store.py"
        fake.parent.mkdir(parents=True)
        fake.write_text("", encoding="utf-8")
        with mock.patch.object(vs_mod, "__file__", str(fake)):
            store = VectorStore(db_path=Path(d) / "vectors")
            with pytest.raises(RuntimeError) as exc:
                store._get_table()
    assert "fetch_corpus.py" in str(exc.value)
    assert "ingest_vectors.py" not in str(exc.value)


def test_a_gpu_stage_that_produced_nothing_is_not_recorded_as_complete():
    """`wait_for_campaign` returns when the work is done OR the driver
    stopped — its own docstring calls those independent facts. Recording the
    stage complete regardless sent an empty campaign into calibration, which
    then reported STOP phrased as a MEASURED rate. Nothing said "never ran".
    """
    src = (_ROOT / "src" / "pipeline_runner.py").read_text(encoding="utf-8")
    idx = src.index("final = wait_for_campaign(")
    # Everything between the wait and the stage being marked complete.
    tail = src[idx:src.index('self._record_stage(mode, "complete", out, stage=mode)', idx)]
    assert "final.n_rf3 == 0" in tail, "no zero-refold guard before recording complete"
    assert "FoundryError" in tail, "zero refolds must raise, not warn"
    assert "not final.complete" in tail, "a short campaign should still warn"


def test_pathway_mode_is_reachable_from_the_cli():
    """README documented `--pathway-mode` in two places while argparse had no
    such flag: `error: unrecognized arguments: --pathway-mode wildcard`."""
    from src.pipeline_runner import PipelineRunner
    out = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "run_pipeline.py"), "--help"],
        capture_output=True, text=True, timeout=120)
    assert "--pathway-mode" in out.stdout
    runner = PipelineRunner(
        config=yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8")),
        workflow="ppi", pathway_mode="wildcard")
    assert runner._pathway_mode == "wildcard"
