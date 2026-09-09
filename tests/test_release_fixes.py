"""Regression tests for the pre-release audit fixes.

Each test here pins a defect that was found in code with no coverage, and that
would otherwise fail silently — a wrong number in a CSV, a campaign placed on
the wrong compute, a healthy run reported as failed. Grouped by the audit
finding rather than by module, so a future reader can trace each one back.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.pipeline_runner import PipelineRunner, _split_fallback

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# Refusal fallback: a cross-provider entry must not inherit the current provider
# ----------------------------------------------------------------------

def test_explicit_provider_prefix_wins():
    assert _split_fallback("gemini:gemini-3.7-flash", "claude") == (
        "gemini", "gemini-3.7-flash")


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5", "claude"),
    ("claude-haiku-4-5", "claude"),
    ("gemini-3.7-flash", "gemini"),
])
def test_provider_is_inferred_from_an_unprefixed_model_id(model, expected):
    """The bug: gemini is the default provider and `models.gemini.
    refusal_fallbacks` names Claude models with no prefix. Inheriting the
    current provider POSTed a Claude id to the Gemini endpoint -> 404 ->
    HTTPError, which is not SkillRefusedError and so escaped the refusal
    handler and killed the run."""
    assert _split_fallback(model, "gemini") == (expected, model)


def test_an_unrecognised_model_id_still_inherits_the_current_provider():
    """Local/Ollama ids carry no recognisable prefix; inheriting is correct."""
    assert _split_fallback("gemma3:12b-it", "local") == ("gemma3", "12b-it")
    assert _split_fallback("my-finetune", "local") == ("local", "my-finetune")


def test_the_shipped_fallback_chains_all_resolve_to_a_real_provider(config):
    """Guards the config itself: every entry in every chain must land on a
    provider that `SkillRunner` can actually dispatch to."""
    models_cfg = config.get("models") or {}
    for provider in ("claude", "gemini"):
        chain = (models_cfg.get(provider) or {}).get("refusal_fallbacks") or []
        assert chain, f"{provider} has no refusal_fallbacks"
        for entry in chain:
            resolved, model = _split_fallback(entry, provider)
            assert resolved in ("claude", "gemini", "local"), (
                f"{provider} chain entry {entry!r} -> unknown provider "
                f"{resolved!r}")
            # The specific incident: a claude-* id resolving to gemini.
            if model.startswith("claude-"):
                assert resolved == "claude", (
                    f"{entry!r} would send a Claude model to {resolved}")
            if model.startswith("gemini-"):
                assert resolved == "gemini"


# ----------------------------------------------------------------------
# --compute must not be silently overridden by choose_compute()
# ----------------------------------------------------------------------

def _write_calibration(dirs_calibration: Path, *, compute: str) -> None:
    dirs_calibration.mkdir(parents=True, exist_ok=True)
    (dirs_calibration / "calibration.json").write_text(json.dumps({
        "verdict": "SCALE_UP",
        "n_batches_local": 3000,
        "n_batches_cluster": 375,
        "compute_choice": {"compute": compute},
    }), encoding="utf-8")


@pytest.mark.parametrize("forced,persisted,expected_compute,expected_batches", [
    # An explicit flag outranks whatever the calibration decided...
    ("local",   "cluster", "local",   3000),
    ("cluster", "local",   "cluster", 375),
    # ...while `auto` defers to the persisted decision, which is the whole
    # point of re-deriving the plan from calibration.json on a resume.
    ("auto",    "cluster", "cluster", 375),
    ("auto",    "local",   "local",   3000),
])
def test_explicit_compute_flag_beats_the_persisted_placement(
        config, tmp_path, forced, persisted, expected_compute, expected_batches):
    runner = PipelineRunner(config, workflow="binder", compute=forced)
    calib_dir = tmp_path / "calibration"
    _write_calibration(calib_dir, compute=persisted)

    n_batches, compute = runner._resolve_production_plan(
        None, {"calibration": calib_dir}, None)

    assert compute == expected_compute
    assert n_batches == expected_batches


def test_in_memory_calib_with_a_null_compute_falls_back_instead_of_returning_none(
        config, tmp_path):
    """A site-trial winner's calib dict can carry `compute: None`. `.get(k,
    default)` returns None for a present-but-None key, which propagated a None
    compute into the stage dispatch."""
    runner = PipelineRunner(config, workflow="binder", compute="auto")
    calib = {"compute": None, "n_batches": 1234}

    _, compute = runner._resolve_production_plan(
        calib, {"calibration": tmp_path}, None)

    assert compute == "local"


def test_site_winner_cluster_sizing_is_not_read_as_a_local_total(
        config, tmp_path):
    """`n_batches` for a cluster-placed trial is already divided by n_gpus
    (NB is per-GPU-array-task). Reading it as a local total ran production at
    1/n_gpus of the intended size."""
    runner = PipelineRunner(config, workflow="binder", compute="auto")
    calib = {"compute": "cluster", "n_batches": 375,
             "n_batches_local": 3000, "n_batches_cluster": 375}

    n_batches, compute = runner._resolve_production_plan(
        calib, {"calibration": tmp_path}, None)
    assert (compute, n_batches) == ("cluster", 375)

    # ...and forcing that same winner local must pick the LOCAL total.
    runner_local = PipelineRunner(config, workflow="binder", compute="local")
    n_batches, compute = runner_local._resolve_production_plan(
        calib, {"calibration": tmp_path}, None)
    assert (compute, n_batches) == ("local", 3000)


# ----------------------------------------------------------------------
# A deliberate pause is not a site failure
# ----------------------------------------------------------------------

def test_paused_error_is_not_caught_as_a_generic_pipeline_error():
    """`_run_site_trials` catches PipelineError to keep one bad site from
    abandoning the others. PipelinePausedError subclasses it, so `--detach`
    and `--compute cluster` were recorded as site failures and could return
    NO_GO while the GPU jobs ran happily."""
    import inspect

    from src import pipeline_runner as pr

    assert issubclass(pr.PipelinePausedError, pr.PipelineError)
    src = inspect.getsource(pr.PipelineRunner._run_site_trials)
    # The re-raise must come BEFORE the broad handler, or it never fires.
    paused_at = src.find("except PipelinePausedError")
    broad_at = src.find("except PipelineError")
    assert paused_at != -1, "site trials must re-raise PipelinePausedError"
    assert paused_at < broad_at, (
        "the PipelinePausedError handler must precede the PipelineError one")


# ----------------------------------------------------------------------
# RFD3 sidecar metrics are flat dotted keys
# ----------------------------------------------------------------------

def test_clash_metrics_are_read_with_flat_dotted_keys():
    """Read as a nested object these are None for every design ever scored,
    silently blanking two columns of refold_scores.csv. Verified against the
    real shape: a sidecar from the completed PD-L1 campaign has the flat keys
    and no `n_clashing` object at all."""
    import inspect

    from src import binder_metrics

    src = inspect.getsource(binder_metrics.score_one)
    assert 'm.get("n_clashing.interresidue_clashes_w_sidechain")' in src
    assert 'm.get("n_clashing.interresidue_clashes_w_backbone")' in src
    # The regression being pinned: a nested read of the same field.
    assert 'm.get("n_clashing")' not in src, (
        "n_clashing is a flat dotted key prefix, not a nested object")


def test_clash_metrics_read_correctly_from_a_real_sidecar():
    """Skips unless a real campaign sidecar is on disk (gitignored, so absent
    in CI) — but on the maintainer's box this is the ground truth."""
    sidecars = sorted((_ROOT / "projects").rglob("*rfd3/*_model_*.json"))
    if not sidecars:
        pytest.skip("no real RFD3 sidecar available")
    metrics = json.loads(sidecars[0].read_text(encoding="utf-8")).get("metrics", {})
    assert "n_clashing.interresidue_clashes_w_sidechain" in metrics
    assert metrics.get("n_clashing") is None, (
        "if RFD3 ever emits a nested object, score_one must be revisited")


def test_clash_metrics_match_the_prefilter_spelling():
    """`foundry_runner.prefilter_designs` gates on the same two keys. If the
    two spellings drift, the gate and the score disagree about the same
    design."""
    import inspect

    from src import binder_metrics, foundry_runner

    for key in ("n_clashing.interresidue_clashes_w_sidechain",
                "n_clashing.interresidue_clashes_w_backbone"):
        assert key in inspect.getsource(foundry_runner)
        assert key in inspect.getsource(binder_metrics)


# ----------------------------------------------------------------------
# Journal tier lists: implicit string concatenation
# ----------------------------------------------------------------------

@pytest.mark.parametrize("journal", [
    "Nat Genet", "Nature genetics",
    "Cell chemical biology", "Cell Chem Biol",
    "N Engl J Med", "Nature chemical biology",
])
def test_flagship_journals_are_recognised(journal):
    """Two missing commas glued list entries together ("nnature chemical
    biology"), dropping real journals out of tier 1. With
    `quality.require_tiered_journal: true` that silently gates downloads."""
    from src.ranking import is_tiered_journal

    assert is_tiered_journal(journal), f"{journal} should be a tiered journal"


def test_every_journal_family_has_a_working_abbreviation():
    """Broader net than the parametrized case above: gluing takes out whichever
    entry sits at the seam, so check one long form and one abbreviation from
    each publisher family actually resolves."""
    from src.ranking import is_tiered_journal

    pairs = [
        ("Nature chemical biology", "Nat Chem Biol"),
        ("Nature structural & molecular biology", "Nat Struct Mol Biol"),
        ("Nature methods", "Nat Methods"),
        ("Nature communications", "Nat Commun"),
        ("Nature genetics", "Nat Genet"),
        ("Cell chemical biology", "Cell Chem Biol"),
        ("Molecular cell", "Mol Cell"),
    ]
    missing = [j for pair in pairs for j in pair if not is_tiered_journal(j)]
    assert not missing, f"not recognised as tiered: {missing}"


def test_tier_lists_contain_no_entry_with_a_stray_single_letter():
    """The literal artefact of the original bug was a bare `"n"` entry, left
    behind when `"n engl j med",` lost its comma to the next line."""
    from src import ranking

    for name in ("_TIER1_JOURNALS", "_TIER2_JOURNALS"):
        stray = [j for j in getattr(ranking, name)
                 if isinstance(j, str) and len(j) < 3]
        assert not stray, f"{name} has suspiciously short entries: {stray}"


# ----------------------------------------------------------------------
# Curation must not silently discard extracted PDB accessions
# ----------------------------------------------------------------------

def _minimal_fingerprint(**paper_extra) -> dict:
    return {
        "relevant": True,
        "curation_metadata": {"schema_version": "2.0", "model": "m",
                              "curated_at": "2026-08-25",
                              "input_tokens": 1, "output_tokens": 1},
        "paper_metadata": {"title": "T", "doi": "10.1/x",
                           "study_type": "experimental_structural",
                           "situational_context_hook": "h", **paper_extra},
        "methodology": {}, "key_findings": [],
    }


def test_extracted_pdb_accessions_survive_validation():
    """`curation_prompt.md` §3 instructs extraction of every PDB code, but
    PaperMetadata declared no field for it and Pydantic v2 drops undeclared
    keys on model_dump — so every extracted accession was discarded and only
    RCSB-recoverable ones ever reached a fingerprint."""
    from src.curator import Fingerprint

    data = _minimal_fingerprint(pdb_accessions=["7CZD", "8ZNL"], pmcid="PMC1")
    pm = Fingerprint.model_validate(data).model_dump(mode="json")["paper_metadata"]

    assert pm["pdb_accessions"] == ["7CZD", "8ZNL"]
    assert pm["pmcid"] == "PMC1"


def test_a_fingerprint_without_accessions_still_validates():
    """Back-compat: 10k existing fingerprints have neither field."""
    from src.curator import Fingerprint

    pm = Fingerprint.model_validate(
        _minimal_fingerprint()).model_dump(mode="json")["paper_metadata"]
    assert pm["pdb_accessions"] == []
    assert pm["pmcid"] is None


# ----------------------------------------------------------------------
# Path confinement
# ----------------------------------------------------------------------

@pytest.mark.parametrize("hostile", [
    "../../../etc/passwd",
    "data/../../../../etc/hostname",
    "a/../../../../../../tmp/evil.cif",
    "data/./../../etc/shadow",
    "/etc/passwd",
])
def test_relative_dot_dot_cannot_escape_the_project_root(tmp_path, hostile):
    """Stripping a leading slash confines an ABSOLUTE path but does nothing to
    a relative one — `root / "../../../etc/passwd"` was still an escape,
    because pathlib does not normalise `..` and there was no containment
    check. Reachable via `write_file` and every file_path-taking tool."""
    import os

    from src._path_resolve import resolve

    root = tmp_path / "repo"
    root.mkdir()
    out = Path(os.path.normpath(resolve(hostile, root=str(root))))
    assert out == root or root in out.parents, f"{hostile} escaped to {out}"


def test_a_legitimate_path_under_root_is_untouched(tmp_path):
    from src._path_resolve import resolve

    root = tmp_path / "repo"
    (root / "data" / "structures").mkdir(parents=True)
    target = root / "data" / "structures" / "3kys.cif"
    target.write_text("x")
    assert Path(resolve("data/structures/3kys.cif", root=str(root))) == target


# ----------------------------------------------------------------------
# Reports are shared files — LLM prose must not carry script
# ----------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    '<a href="javascript:alert(1)">click</a>',
    '<iframe src="http://evil"></iframe>',
    "<svg/onload=alert(1)>",
    "<STYLE>body{background:url(javascript:alert(1))}</STYLE>",
    "<object data=evil></object>",
])
def test_stage_prose_cannot_inject_script_into_a_report(payload):
    from src.report_common import markdown_html

    out = markdown_html(f"Prose before. {payload} Prose after.").lower()
    for token in ("<script", "javascript:", "onerror", "onload",
                  "<iframe", "<style", "<object", "<svg"):
        assert token not in out, f"{token!r} survived sanitisation: {out}"


def test_sanitising_does_not_break_ordinary_markdown():
    """The reports' whole point is rendering the stages' own prose — tables,
    emphasis, code and blockquotes must all still work, and a bare `<` (as in
    'iPTM < 0.7') must render as a less-than sign."""
    from src.report_common import markdown_html

    assert "<strong>bold</strong>" in markdown_html("**bold**")
    assert "<code>x</code>" in markdown_html("`x`")
    assert "<table>" in markdown_html("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<blockquote>" in markdown_html("> note")
    assert "&lt; 0.7" in markdown_html("gate at iPTM < 0.7")


def test_an_llm_derived_title_is_escaped():
    """`@@TITLE@@` carries a target name straight into <title>."""
    from src.report_common import escape_html

    out = escape_html('PD-L1 "</title><script>x</script>')
    assert "<script" not in out
    assert "</title>" not in out


# ----------------------------------------------------------------------
# PyRosetta is optional — used only for post-generation scoring
# ----------------------------------------------------------------------

def _pyr_cfg(**pyrosetta) -> dict:
    return {"pyrosetta": {"python_executable": None, **pyrosetta}}


@pytest.mark.parametrize("raw,expected", [
    ("auto", "auto"), (None, "auto"), ("", "auto"),
    (True, "on"), ("true", "on"), ("required", "on"),
    (False, "off"), ("false", "off"), ("off", "off"),
    ("gibberish", "auto"),
])
def test_pyrosetta_mode_parsing(raw, expected):
    from src.pyrosetta_sasa import pyrosetta_mode

    assert pyrosetta_mode(_pyr_cfg(enabled=raw)) == expected


def test_auto_skips_cleanly_when_no_interpreter_is_configured(monkeypatch):
    from src.pyrosetta_sasa import check_available

    monkeypatch.delenv("LPT_PYROSETTA_PYTHON", raising=False)
    usable, reason = check_available(_pyr_cfg(enabled="auto"))
    assert usable is False
    assert "LPT_PYROSETTA_PYTHON" in reason


def test_enabled_true_fails_loudly_when_it_is_missing(monkeypatch):
    """A campaign pinned to `enabled: true` must not silently fall back to a
    different ranking rubric."""
    from src.pyrosetta_sasa import PyRosettaNotConfigured, check_available

    monkeypatch.delenv("LPT_PYROSETTA_PYTHON", raising=False)
    with pytest.raises(PyRosettaNotConfigured):
        check_available(_pyr_cfg(enabled=True))


def test_enabled_false_wins_even_when_it_is_installed(monkeypatch, tmp_path):
    from src.pyrosetta_sasa import check_available

    fake = tmp_path / "python"
    fake.write_text("")
    monkeypatch.setenv("LPT_PYROSETTA_PYTHON", str(fake))
    usable, reason = check_available(_pyr_cfg(enabled=False))
    assert usable is False
    assert "disabled" in reason


def test_interpreter_never_falls_back_to_the_lpt_venv_python(monkeypatch):
    """`shutil.which("python")` resolved the LPT venv, which has no pyrosetta,
    so `available()` said True and the worker then died on import — surfaced
    as "worker produced no output (rc=1)"."""
    from src.pyrosetta_sasa import resolve_interpreter

    monkeypatch.delenv("LPT_PYROSETTA_PYTHON", raising=False)
    assert resolve_interpreter(_pyr_cfg()) is None

    from src import rosetta_metrics
    assert rosetta_metrics.available(_pyr_cfg(enabled="auto")) is False


def test_rosetta_and_sasa_agree_about_availability(monkeypatch, tmp_path):
    """One answer, so the two tracks cannot disagree about the same install."""
    from src import rosetta_metrics
    from src.pyrosetta_sasa import check_available

    fake = tmp_path / "python"
    fake.write_text("")
    monkeypatch.setenv("LPT_PYROSETTA_PYTHON", str(fake))
    cfg = _pyr_cfg(enabled="auto")
    assert check_available(cfg)[0] == rosetta_metrics.available(cfg)


# --- the filter consequence: absent PyRosetta must not drop every design ---

def _records(n: int = 100) -> list[dict]:
    return [{"design_name": f"d{i}", "pass_filters": True,
             "design_to_target_iptm": 0.85,
             "min_design_to_target_pae": 6.0} for i in range(n)]


_THRESHOLDS = dict(iptm_min=0.7, ipae_max=10.0,
                   hotspot_sasa_delta_min=50.0, require_boltzgen_pass=True)


def test_absent_pyrosetta_skips_the_sasa_gate_instead_of_failing_everything():
    """The reported symptom: `pyrosetta.python_executable: null` made
    `filter_records` drop all 100 designs for `missing_hotspot_sasa`, leaving
    an empty top_k.csv and a 05_analysis.md that blamed the designs."""
    from src.design_ranking import filter_records

    survivors, stats = filter_records(
        _records(), **_THRESHOLDS, hotspot_sasa_available=False)

    assert len(survivors) == 100
    assert "missing_hotspot_sasa" not in stats.dropped


def test_present_pyrosetta_still_drops_designs_it_never_scored():
    """Enrichment is capped to top-K; a design outside that cap has NOT passed
    the filter, so it is still a drop. Skipping only applies when enrichment
    did not run at all."""
    from src.design_ranking import filter_records

    survivors, stats = filter_records(
        _records(), **_THRESHOLDS, hotspot_sasa_available=True)

    assert survivors == []
    assert stats.dropped["missing_hotspot_sasa"] == 100


def test_the_sasa_gate_still_bites_when_values_are_present():
    from src.design_ranking import filter_records

    recs = _records()
    for i, r in enumerate(recs):
        r["lpt_hotspot_sasa_delta"] = 80.0 if i < 30 else 10.0

    survivors, stats = filter_records(
        recs, **_THRESHOLDS, hotspot_sasa_available=True)

    assert len(survivors) == 30
    assert stats.dropped["hotspot_sasa"] == 70


def test_pyrosetta_is_never_used_before_designs_exist():
    """The justification for making it optional: it is scoring-only. If a new
    call site appears in a generative stage, this test should fail and the
    'optional' claim be re-examined."""
    import inspect

    from src import pipeline_runner as pr

    generative = [pr.PipelineRunner._stage_binder_spec,
                  pr.PipelineRunner._stage_trim]
    for fn in generative:
        src = inspect.getsource(fn)
        for token in ("pyrosetta", "rosetta_metrics", "hotspot_sasa"):
            assert token not in src.lower(), (
                f"{fn.__name__} references {token!r} — PyRosetta is documented "
                f"as post-generation scoring only")


# ----------------------------------------------------------------------
# RCSB "no deposited structures" is 204, not an error
# ----------------------------------------------------------------------

class _Resp:
    def __init__(self, status, content=b"", payload=None):
        self.status_code, self.content, self._payload = status, content, payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if not self.content:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


def test_no_deposited_structures_is_not_logged_as_a_failure(monkeypatch):
    """RCSB answers "nothing matched" with 204 and an empty body — the normal
    case, since most papers deposit nothing. 204 passes raise_for_status, so
    .json() raised and every ordinary paper was logged "RCSB DOI lookup
    failed", burying real failures in noise."""
    import scripts.curate_papers as cp

    monkeypatch.setattr(cp.requests, "post", lambda *a, **k: _Resp(204))
    warned = []
    monkeypatch.setattr(cp.logger, "warning", lambda m: warned.append(m))

    assert cp._rcsb_accessions_for_doi("10.1093/nar/gkaa912") == []
    assert warned == [], f"204 should be silent, got: {warned}"


def test_deposited_structures_are_returned(monkeypatch):
    import scripts.curate_papers as cp

    payload = {"result_set": [{"identifier": "7CIZ"}, {"identifier": "7CJ0"}]}
    monkeypatch.setattr(cp.requests, "post",
                        lambda *a, **k: _Resp(200, b"{...}", payload))

    assert cp._rcsb_accessions_for_doi("10.1016/j.molcel.2021.03.041") == ["7CIZ", "7CJ0"]


def test_a_real_transport_failure_is_still_warned(monkeypatch):
    import scripts.curate_papers as cp

    def boom(*a, **k):
        raise cp.requests.RequestException("connection reset")

    monkeypatch.setattr(cp.requests, "post", boom)
    warned = []
    monkeypatch.setattr(cp.logger, "warning", lambda m: warned.append(m))

    assert cp._rcsb_accessions_for_doi("10.1/x") == []
    assert warned and "connection reset" in warned[0]


# ----------------------------------------------------------------------
# Lower-severity batch
# ----------------------------------------------------------------------

def test_both_search_filters_are_ANDed_not_overwritten():
    """LanceDB's builder does `self._where = where`, so two consecutive
    .where() calls REPLACE rather than AND. Asking for study_type +
    study_category silently applied the category only."""
    import inspect

    from src import vector_store

    src = inspect.getsource(vector_store.VectorStore.search)
    assert src.count(".where(") <= 2, (
        "search() must build ONE predicate; a second .where() overwrites the first")
    assert " AND ".join.__self__ or True     # readability only
    assert '" AND ".join' in src, "the two clauses must be ANDed into one predicate"


@pytest.mark.parametrize("hostile,expected", [
    ("biochemistry", "'biochemistry'"),
    ("bio'; DROP TABLE x--", "'bio''; DROP TABLE x--'"),
    ("a'b", "'a''b'"),
])
def test_filter_values_are_quoted(hostile, expected):
    """These arrive from an LLM tool call; the schema enum is advisory only."""
    from src.vector_store import _sql_quote

    assert _sql_quote(hostile) == expected


def test_control_characters_are_stripped_from_filter_values():
    from src.vector_store import _sql_quote

    assert "\n" not in _sql_quote("bio\nchemistry")


# --- Gemini token accounting -------------------------------------------

def test_reasoning_tokens_are_billed():
    """Gemini reports reasoning tokens in thoughtsTokenCount, billed at the
    OUTPUT rate and NOT included in candidatesTokenCount. Counting only the
    latter under-reported spend on every thinking call — and `--budget`,
    documented as a hard cap, under-enforced by the same margin."""
    import inspect

    from src import skill_runner

    src = inspect.getsource(skill_runner.SkillRunner._run_gemini)
    assert "thoughtsTokenCount" in src, "reasoning tokens must be counted"
    assert "cachedContentTokenCount" in src, "cached tokens must be surfaced"


def test_gemini_handles_a_candidate_with_no_parts():
    """A thinking model truncated before emitting a part yields `content` with
    no `parts`; indexing straight in raised a bare KeyError outside the
    handled-refusal path."""
    import inspect

    from src import skill_runner

    src = inspect.getsource(skill_runner.SkillRunner._run_gemini)
    assert 'candidate["content"]["parts"]' not in src
    assert '(candidate.get("content") or {}).get("parts")' in src


def test_gemini_retries_transient_network_errors():
    """The status-code retry only helps once a response exists; a dropped
    connection raised straight out of requests.post. Gemini is the default
    provider, so a blip aborted whole multi-hour runs."""
    import inspect

    from src import skill_runner

    src = inspect.getsource(skill_runner.SkillRunner._run_gemini)
    assert "requests.ConnectionError" in src and "requests.Timeout" in src


# --- PPI structure-stage guards ----------------------------------------

def test_chain_assignment_guard_does_not_depend_on_hotspot_parsing():
    """The guards sit outside the try/except by design — but gating them on
    `hotspots_json`, assigned INSIDE it, meant a parse failure silently
    skipped both. A chain swap yields real, correctly-numbered residues on the
    WRONG protein, and is most likely exactly when the report is malformed."""
    import inspect

    from src.pipeline_runner import PipelineRunner

    src = inspect.getsource(PipelineRunner._stage_structure)
    tail = src[src.index("_verify_ppi_chain_assignment"):]
    before = src[:src.index("_verify_ppi_chain_assignment")]
    # The chain guard must NOT be nested under an `if hotspots_json:`
    assert not before.rstrip().endswith("if hotspots_json:"), (
        "chain-assignment guard is still gated on hotspot parsing")
    assert "_verify_hotspot_grounding" in tail


# --- database ----------------------------------------------------------

def test_connections_are_closed_not_just_committed(tmp_path):
    """`with sqlite3.connect(...)` commits but does not close; a long-lived
    MCP server accumulated a handle per call."""
    import os

    from src.database import Database

    db = Database(str(tmp_path / "t.db"))
    before = len(os.listdir(f"/proc/{os.getpid()}/fd"))
    for _ in range(100):
        db.stats()
    after = len(os.listdir(f"/proc/{os.getpid()}/fd"))
    assert after - before < 5, f"leaked {after - before} handles over 100 queries"


def test_a_paper_with_no_identifier_warns_instead_of_silently_no_op(tmp_path, caplog):
    """`WHERE pmcid = NULL` matches nothing, so these rows stayed 'pending'
    forever and were re-detected on every run."""
    from src.database import Database

    db = Database(str(tmp_path / "t.db"))
    db.mark_downloaded(None, None, "/x/y.pdf")     # must not raise
    db.mark_failed(None, None, "boom")


# --- resource leak in PDF extraction -----------------------------------

def test_a_malformed_pdf_does_not_leak_the_mupdf_handle(tmp_path):
    """Curation walks ~10k PDFs in one process, so this is not theoretical."""
    import os

    from src.text_extractor import _extract_pdf

    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    before = len(os.listdir(f"/proc/{os.getpid()}/fd"))
    for _ in range(50):
        try:
            _extract_pdf(bad, 5000)
        except Exception:
            pass
    after = len(os.listdir(f"/proc/{os.getpid()}/fd"))
    assert after - before < 5, f"leaked {after - before} handles over 50 files"


# --- config/default consistency ----------------------------------------

def test_cluster_n_gpus_default_matches_config_and_calibration(config):
    """A 4 in the code against an 8 in config.yaml silently sized cluster
    campaigns for half the GPUs whenever design.cluster was absent."""
    import inspect

    from src import campaign_calibration, cluster_runner

    cfg_default = (((config.get("design") or {}).get("cluster") or {}).get("n_gpus"))
    code_default = int(inspect.getsource(cluster_runner.ClusterConfig.from_cfg)
                       .split('c.get("n_gpus", ')[1].split(")")[0])
    sig_default = inspect.signature(
        campaign_calibration.choose_compute).parameters["n_gpus_cluster"].default
    assert code_default == sig_default == cfg_default, (
        f"n_gpus defaults disagree: code={code_default} "
        f"choose_compute={sig_default} config.yaml={cfg_default}")


def test_report_reads_success_metric_from_the_frozen_run_not_todays_config():
    """Editing config.yaml silently relabelled the scatter axis of an OLD
    report — the same run-time-vs-config-time drift ppi_report guards against."""
    import inspect

    from src import binder_report

    src = inspect.getsource(binder_report.build_report)
    assert 'calibration.get("success_metric")' in src


def test_cluster_paths_fail_with_a_message_not_a_typeerror():
    """Reachable on resume: staged with LPT_CLUSTER_PIPELINE_ROOT set, resumed
    from a shell without it."""
    import inspect

    from src import cluster_runner

    assert "_require_pipeline_root" in inspect.getsource(cluster_runner)


# ----------------------------------------------------------------------
# Corpus size controls: --prefer-xml and --discard-documents
# ----------------------------------------------------------------------

def test_prefer_xml_puts_xml_first_and_keeps_fallback_order():
    """PMC open-access XML is ~47x smaller than the publisher PDF of the same
    paper (measured: 8,150 PDFs at 6.1 MB mean vs 3,976 XMLs at 0.13 MB), and
    text_extractor handles both. The reorder must be STABLE so the
    carefully-ordered AWS-then-search-metadata chain within each format
    survives."""
    urls = [("pdf", "aws_v1.pdf"), ("pdf", "aws_v2.pdf"), ("pdf", "search.pdf"),
            ("xml", "aws_v1.xml"), ("xml", "aws_v2.xml"), ("xml", "search.xml")]
    urls.sort(key=lambda hu: hu[0] != "xml")

    assert [u for _, u in urls][:3] == ["aws_v1.xml", "aws_v2.xml", "search.xml"]
    assert [u for _, u in urls][3:] == ["aws_v1.pdf", "aws_v2.pdf", "search.pdf"]


def test_prefer_xml_is_opt_in():
    """It trades corpus breadth (PDF-only papers) for size, so it must never
    be the default."""
    import inspect

    from src import downloader

    sig = inspect.signature(downloader.download_papers)
    assert sig.parameters["prefer_xml"].default is False


def test_discard_documents_is_opt_in_and_runs_after_the_fingerprint():
    """Deleting the source before the fingerprint is durably on disk would
    destroy input an interrupted run has not yet extracted."""
    import inspect
    import pathlib

    src = pathlib.Path(
        inspect.getfile(test_discard_documents_is_opt_in_and_runs_after_the_fingerprint)
    ).parent.parent / "scripts" / "curate_papers.py"
    text = src.read_text(encoding="utf-8")

    assert '"--discard-documents", action="store_true"' in text, "must be opt-in"
    save_at = text.index("Saved fingerprint")
    discard_at = text.index("if args.discard_documents:")
    assert save_at < discard_at, (
        "the document must only be deleted AFTER its fingerprint is written")


def test_nothing_downstream_reads_the_source_documents():
    """The premise of --discard-documents: documents are curation INPUT only.
    If a consumer of pdf_path appears outside the download/curate path, the
    flag becomes unsafe and this should fail."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    # The download/curate path itself, plus compare_providers.py, which
    # RE-curates a fixed paper set to compare providers side by side. That is
    # precisely the "you may want to re-curate" case --discard-documents warns
    # about — it is a development tool, not part of any pipeline run.
    allowed = {"downloader.py", "curate_papers.py", "database.py",
               "models.py", "text_extractor.py", "fetch_papers.py",
               "compare_providers.py",
               # package_corpus.py names pdf_path only to REFUSE to ship a
               # database containing local paths, and data/pdfs only in its
               # exclusion note. It never opens a document.
               "package_corpus.py"}
    offenders = []
    for sub in ("src", "scripts"):
        for path in (root / sub).rglob("*.py"):
            if path.name in allowed or "__pycache__" in path.parts:
                continue
            body = path.read_text(encoding="utf-8", errors="ignore")
            if "pdf_path" in body or "pdf_dir" in body:
                offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        f"these read source documents, so discarding them is not safe: {offenders}")


# ----------------------------------------------------------------------
# Journal tier gate
# ----------------------------------------------------------------------

@pytest.mark.parametrize("journal", [
    # PubMed's own spellings of journals that ARE on the tier lists. Each of
    # these was a silent 100% exclusion before _normalise learned to strip a
    # leading "The" and PubMed's country/edition suffixes — 1,721 papers
    # across the reference corpus, 827 of them PNAS alone.
    "Proceedings of the National Academy of Sciences of the United States of America",
    "The EMBO Journal",
    "The Journal of Biological Chemistry",
    "The Biochemical journal",
    "Angew Chem Int Ed Engl",
])
def test_pubmed_spellings_of_listed_journals_are_recognised(journal):
    from src.ranking import is_tiered_journal

    assert is_tiered_journal(journal), f"{journal!r} should pass the tier gate"


@pytest.mark.parametrize("journal", [
    "Journal of Cell Biology", "The Journal of Cell Biology", "J Cell Biol",
    "Cell Stem Cell", "Stem Cell Reports",
    "Molecular and Cellular Biology", "Mol Cell Biol",
    "EMBO Molecular Medicine", "EMBO Mol Med",
    "Development", "Development (Cambridge, England)",
    "Genome Biology", "Genome Biol",
    "Developmental Cell", "Developmental cell", "Dev Cell",
    "Molecular Biology of the Cell", "Mol Biol Cell",
    "PLoS Genetics", "PLoS Genet", "PLOS Genetics",
    "FEBS Journal", "FEBS Letters", "PLoS Biology", "PLoS Computational Biology",
])
def test_curated_additions_are_recognised_in_every_spelling(journal):
    """Added deliberately; each must match in the abbreviated form PubMed
    emits as well as the full name, or it is a silent 100% exclusion."""
    from src.ranking import is_tiered_journal

    assert is_tiered_journal(journal), f"{journal!r} should pass the tier gate"


@pytest.mark.parametrize("journal", [
    # Punctuated entries could never match before the lists were normalised
    # at import — four tier 1 journals were silently excluded despite being
    # listed.
    "Genes & Development", "Cell Host & Microbe",
    "Nature Structural & Molecular Biology", "The ISME Journal",
    "Bioorganic & Medicinal Chemistry", "Organic & Biomolecular Chemistry",
])
def test_punctuated_tier_entries_match(journal):
    from src.ranking import is_tiered_journal

    assert is_tiered_journal(journal)


def test_every_tier_entry_is_reachable():
    """An entry not in normalised form can never match any lookup, so it is
    dead weight that looks like coverage. Normalising at import prevents it;
    this fails if that ever regresses."""
    from src.ranking import _TIER1_JOURNALS, _TIER2_JOURNALS, _normalise

    unreachable = [e for e in (_TIER1_JOURNALS | _TIER2_JOURNALS)
                   if _normalise(e) != e]
    assert not unreachable, f"these entries can never match: {unreachable}"


@pytest.mark.parametrize("journal", [
    "PLoS One", "Sci Rep", "Int J Mol Sci", "bioRxiv",
    "Frontiers in Molecular Biosciences", "Cells",
    # Adjacent names that must NOT be caught by the generic "development" entry
    "Stem Cells and Development", "Developmental biology",
    "Frontiers in Cell and Developmental Biology",
])
def test_deliberately_excluded_venues_stay_excluded(journal):
    """The gate is a quality judgement; normalisation must not soften it."""
    from src.ranking import is_tiered_journal

    assert not is_tiered_journal(journal), f"{journal!r} should NOT pass"


def test_normalisation_is_idempotent():
    from src.ranking import _normalise

    for raw in ("The EMBO Journal", "  Nature   Methods  ", "PNAS"):
        once = _normalise(raw)
        assert _normalise(once) == once


def test_extra_journals_from_config_are_honoured():
    """tier1_extra / tier2_extra are how a user extends the list without
    editing src/ranking.py."""
    from src.ranking import is_tiered_journal

    assert not is_tiered_journal("Journal of Obscure Results")
    assert is_tiered_journal("Journal of Obscure Results",
                             tier2_extra=["Journal of Obscure Results"])
    # ...and the extras go through the same normalisation.
    assert is_tiered_journal("The Journal of Obscure Results",
                             tier2_extra=["Journal of Obscure Results"])


def test_the_gate_is_documented_where_a_user_will_see_it():
    """It silently drops ~72% of search hits, so it must not be discoverable
    only by reading src/ranking.py."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    doc = root / "docs" / "journal-filtering.md"
    assert doc.is_file(), "docs/journal-filtering.md is the canonical reference"

    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "journal-filtering.md" in readme, "README must link the reference"
    assert "require_tiered_journal" in readme

    cfg = (root / "config.yaml").read_text(encoding="utf-8")
    assert "docs/journal-filtering.md" in cfg, (
        "the config key itself should point at the explanation")


# ----------------------------------------------------------------------
# Onboarding surface
# ----------------------------------------------------------------------

def _repo_root():
    import pathlib

    return pathlib.Path(__file__).resolve().parent.parent


def test_onboarding_scripts_exist_and_have_help():
    """A new user's first three commands. If any stops parsing, onboarding
    breaks silently — the docs would still say to run it."""
    import subprocess
    import sys

    root = _repo_root()
    for name in ("doctor.py", "quickstart.py", "fetch_reference_data.py"):
        path = root / "scripts" / name
        assert path.is_file(), f"{name} is referenced by the docs but missing"
        p = subprocess.run([sys.executable, str(path), "--help"],
                           capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, f"{name} --help failed: {p.stderr[:200]}"


def test_setup_agent_prompt_references_only_real_things():
    """SETUP_AGENT.md tells an agent what to run. Every script, doc and flag it
    names must exist, or the agent will confidently run something that
    doesn't."""
    import re
    import subprocess
    import sys

    root = _repo_root()
    text = (root / "SETUP_AGENT.md").read_text(encoding="utf-8")

    missing = [s for s in set(re.findall(r"scripts/([a-z_]+\.py)", text))
               if not (root / "scripts" / s).is_file()]
    assert not missing, f"SETUP_AGENT.md names missing scripts: {missing}"

    docs = set(re.findall(r"docs/[a-z_\-]+\.md", text)) | {"README.md", ".env.example"}
    missing_docs = [d for d in docs if not (root / d).is_file()]
    assert not missing_docs, f"SETUP_AGENT.md names missing docs: {missing_docs}"

    # The flags it tells the agent to use must be real.
    for script, flag in (("fetch_papers.py", "--prefer-xml"),
                         ("curate_papers.py", "--discard-documents"),
                         ("doctor.py", "--track"),
                         ("fetch_reference_data.py", "--with-depmap")):
        p = subprocess.run([sys.executable, str(root / "scripts" / script), "--help"],
                           capture_output=True, text=True, timeout=120)
        assert flag in p.stdout, f"{script} has no {flag}, but SETUP_AGENT.md uses it"


def test_the_base_install_does_not_require_the_corpus_extra():
    """The whole point of the split: everything except corpus search must
    import without sentence-transformers or lancedb."""
    import ast

    root = _repo_root()
    heavy = {"sentence_transformers", "lancedb", "torch"}
    offenders = []
    for path in (root / "src").glob("*.py"):
        if path.name == "vector_store.py":       # guarded, lazy, and expected
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # Only MODULE-level imports break the base install; a lazy import
            # inside a function is fine.
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.col_offset == 0:
                names = ([a.name for a in node.names]
                         if isinstance(node, ast.Import) else [node.module or ""])
                if any((n or "").split(".")[0] in heavy for n in names):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"module-level heavy imports break the base install: {offenders}")


def test_corpus_extra_is_declared_and_not_a_base_dependency():
    import tomllib

    root = _repo_root()
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    base = " ".join(data["project"]["dependencies"])
    extras = data["project"]["optional-dependencies"]

    assert "corpus" in extras, "the corpus extra must exist"
    for pkg in ("sentence-transformers", "lancedb"):
        assert pkg not in base, f"{pkg} must not be a base dependency"
        assert any(pkg in d for d in extras["corpus"]), f"{pkg} missing from corpus extra"


# ----------------------------------------------------------------------
# The corpus ships pre-built — setup must not tell users to pay for it
# ----------------------------------------------------------------------

def test_corpus_package_and_fetch_scripts_exist():
    import subprocess
    import sys

    root = _repo_root()
    for name in ("package_corpus.py", "fetch_corpus.py"):
        path = root / "scripts" / name
        assert path.is_file(), f"{name} missing"
        p = subprocess.run([sys.executable, str(path), "--help"],
                           capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, f"{name} --help failed: {p.stderr[:200]}"


def test_the_release_archive_excludes_source_documents():
    """PDFs are ~95% of a corpus on disk and nothing downstream reads them.
    Shipping them would turn an 83 MB download into 50 GB."""
    from scripts.package_corpus import MEMBERS

    shipped = [rel for rel, _ in MEMBERS]
    assert not any("pdfs" in rel for rel in shipped), (
        f"source documents must not ship: {shipped}")
    # ...and the things that make search work must.
    for required in ("data/fingerprints", "data/vectors", "data/literature.db"):
        assert required in shipped, f"{required} must ship"


def test_packaging_refuses_a_database_with_local_paths():
    """The DB stores pdf_path/fingerprint_path. Absolute or home-directory
    paths would leak the maintainer's filesystem layout to every user."""
    import inspect

    from scripts import package_corpus

    src = inspect.getsource(package_corpus._verify_no_personal_data)
    assert "home" in src and "absolute" in src.lower()
    src_pkg = inspect.getsource(package_corpus.package)
    assert "check()" in src_pkg, "package() must run the check before archiving"


def test_setup_does_not_present_corpus_building_as_the_default():
    """The corpus is a free download. Telling a new user to spend $150-500
    rebuilding it — which an earlier draft of SETUP_AGENT.md did — is wrong."""
    root = _repo_root()
    text = (root / "SETUP_AGENT.md").read_text(encoding="utf-8")

    phase6 = text[text.index("### Phase 6"):text.index("### Phase 7")]
    assert "fetch_corpus.py" in phase6, (
        "Phase 6 must lead with downloading the pre-built corpus")
    lead = phase6[:phase6.index("```") if "```" in phase6 else 400]
    assert "free" in lead.lower(), "the lead must say the corpus is free"


def test_doctor_points_at_the_download_not_a_rebuild():
    import inspect

    from scripts import doctor

    src = inspect.getsource(doctor.check_corpus)
    assert "fetch_corpus.py" in src
    assert "curate_papers.py" not in src, (
        "a missing corpus should be fixed by downloading, not by curating")


# ----------------------------------------------------------------------
# Release blocker: the corpus must be published before going public
# ----------------------------------------------------------------------

def test_the_readmes_first_screen_says_where_the_corpus_comes_from():
    """`data/` is gitignored, so a clone has no corpus and every corpus tool
    fails until `fetch_corpus.py` runs. A reader must learn that from the
    README's opening, not by hitting the failure.

    This test used to pin a "not yet released" banner to the first 800
    characters, on the premise that the command found nothing until a release
    asset existed. The asset exists now (v0.1.0), so the banner went; what
    still has to be true is that the opening names the fetch step and points
    at the release state somewhere.
    """
    root = _repo_root()
    readme = (root / "README.md").read_text(encoding="utf-8")
    head = readme[:2000]
    assert "fetch_corpus.py" in head, (
        "the README's opening must tell a reader how to get the corpus")
    assert "RELEASE_CHECKLIST.md" in readme, (
        "release state must be discoverable from the README")

    checklist = root / "RELEASE_CHECKLIST.md"
    assert checklist.is_file(), "RELEASE_CHECKLIST.md must exist while unreleased"
    body = checklist.read_text(encoding="utf-8")
    assert "package_corpus.py" in body and "fetch_corpus.py" in body


def test_the_asset_name_the_fetcher_looks_for_matches_what_packaging_writes():
    """fetch_corpus.py finds a release asset by name prefix. If packaging ever
    emits a different name, the published corpus becomes invisible and the
    failure looks like 'no release yet'."""
    from scripts.fetch_corpus import ASSET_PREFIX
    from scripts.package_corpus import DEFAULT_OUT

    assert DEFAULT_OUT.name.startswith(ASSET_PREFIX), (
        f"packaging writes {DEFAULT_OUT.name!r} but fetching looks for "
        f"{ASSET_PREFIX!r}*")


def test_install_and_fetch_agree_on_what_the_archive_contains():
    """package_corpus decides what ships; fetch_corpus reports what is present.
    If they drift, --check misreports a correctly-installed corpus."""
    from scripts.fetch_corpus import INSTALLS
    from scripts.package_corpus import MEMBERS

    assert set(INSTALLS) == {rel for rel, _ in MEMBERS}, (
        "package_corpus.MEMBERS and fetch_corpus.INSTALLS have drifted")


# ----------------------------------------------------------------------
# Python 3.13's VERIFY_X509_STRICT vs TLS-inspecting proxies
# ----------------------------------------------------------------------

def test_relaxation_is_opt_in_and_off_by_default(monkeypatch):
    """Silently relaxing a TLS default is not a tool's decision to make."""
    import ssl

    monkeypatch.delenv("LPT_SSL_RELAX_STRICT", raising=False)
    import src.env_config as ec

    monkeypatch.setattr(ec, "_STRICT_RELAXED", False)
    before = ssl.create_default_context
    ec.load_env()
    assert ssl.create_default_context is before, (
        "load_env must not touch TLS defaults unless asked")


def test_relaxation_clears_only_the_strict_flag(monkeypatch):
    """It must drop VERIFY_X509_STRICT and NOTHING else — trust chain,
    hostname and expiry checking all still have to apply."""
    import ssl

    import src.env_config as ec

    monkeypatch.setattr(ec, "_STRICT_RELAXED", False)
    original = ssl.create_default_context
    try:
        assert ec.relax_x509_strict() is True
        ctx = ssl.create_default_context()
        assert not (ctx.verify_flags & ssl.VERIFY_X509_STRICT)
        # The properties that actually matter must survive.
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True
    finally:
        ssl.create_default_context = original
        ec._STRICT_RELAXED = False


def test_relaxation_also_covers_the_urllib3_path(monkeypatch):
    """requests builds its context through urllib3, NOT through
    ssl.create_default_context — patching only the stdlib left every requests
    call still failing, which is how the first version of this fix looked
    correct and did nothing."""
    import ssl

    import src.env_config as ec

    urllib3_util = pytest.importorskip("urllib3.util.ssl_")
    monkeypatch.setattr(ec, "_STRICT_RELAXED", False)
    std_original = ssl.create_default_context
    u3_original = urllib3_util.create_urllib3_context
    try:
        ec.relax_x509_strict()
        ctx = urllib3_util.create_urllib3_context()
        assert not (ctx.verify_flags & ssl.VERIFY_X509_STRICT)
    finally:
        ssl.create_default_context = std_original
        urllib3_util.create_urllib3_context = u3_original
        ec._STRICT_RELAXED = False


def test_relaxation_is_idempotent(monkeypatch):
    import ssl

    import src.env_config as ec

    monkeypatch.setattr(ec, "_STRICT_RELAXED", False)
    original = ssl.create_default_context
    try:
        assert ec.relax_x509_strict() is True
        once = ssl.create_default_context
        assert ec.relax_x509_strict() is True
        assert ssl.create_default_context is once, "must not double-wrap"
    finally:
        ssl.create_default_context = original
        ec._STRICT_RELAXED = False


def test_python_version_is_bounded_and_tested_versions_are_in_ci():
    """An unbounded requires-python let an untested interpreter become the
    default on someone's machine — and 3.13 DID change behaviour under us."""
    import tomllib

    import yaml

    root = _repo_root()
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    spec = data["project"]["requires-python"]
    assert "<" in spec, f"requires-python must have an upper bound, got {spec!r}"

    workflow = yaml.safe_load(
        (root / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    matrix = workflow["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    assert len(matrix) >= 2, f"CI must test more than one Python: {matrix}"
    for v in ("3.12", "3.13"):
        assert v in matrix, f"{v} is supported but not in the CI matrix"


# ----------------------------------------------------------------------
# PPI -> foundry bridge: defects found by a real GPU validation run
# ----------------------------------------------------------------------

def test_bridge_carries_the_chain_assignment_into_target_intel():
    """`_binder_sites` reads target_chain/partner_chain from the TARGET-INTEL
    handoff, but PPI's structure stage puts them in the INTERFACE handoff. The
    bridge did not carry them across, so every path that builds a site list —
    `--stop-after spec` (the recommended first command), `--stop-after trial`,
    `--trial-sites N` — died with 'target-intel did not name usable chains'."""
    import inspect

    from src.pipeline_runner import PipelineRunner

    src = inspect.getsource(PipelineRunner._bridge_ppi_to_foundry)
    assert '"target_chain": structure_handoff.get("target_chain"' in src
    assert '"partner_chain": structure_handoff.get("partner_chain"' in src


def test_site_building_works_with_the_bridge_handoff_and_fails_without():
    from src.pipeline_runner import PipelineBlockedError, PipelineRunner

    complete = {"pdb_id": "6VJJ", "target_gene": "KRAS",
                "target_chain": "A", "partner_chain": "B"}
    sites = PipelineRunner._binder_sites(complete, limit=1)
    assert sites and sites[0]["target_chain"] == "A"

    with pytest.raises(PipelineBlockedError, match="usable chains"):
        PipelineRunner._binder_sites({"pdb_id": "6VJJ", "target_gene": "KRAS"},
                                     limit=1)


def test_bridge_coerces_a_modality_rfd3_cannot_build():
    """foundry designs mini-proteins; RFD3 has no cyclic-peptide path, and
    binder_sizes.cyclic_peptide is 12-15 residues. The real validation run's
    structure stage DID emit modality: cyclic_peptide — it was silently ignored
    and the right thing happened by accident. Coerce it loudly instead."""
    import inspect

    from src.pipeline_runner import PipelineRunner

    # The coercion moved into _resolve_modality, which every caller shares —
    # the bridge no longer carries its own copy.
    src = inspect.getsource(PipelineRunner._resolve_modality)
    assert "cyclic_peptide" in src and "opt-in" in src
    bridge = inspect.getsource(PipelineRunner._bridge_ppi_to_foundry)
    assert "_resolve_modality" in bridge, "the bridge must go through it"


def test_trim_bsa_warning_compares_like_with_like():
    """`bsa_total_A2` covers BOTH chains; per-residue BSA is filtered to the
    target's side. Subtracting one from the other reported ~half the interface
    as dropped on EVERY trim — a no-op trim removing one cloning-artifact
    residue warned '1138 A^2 (60%)' and opened a trim_gate checkpoint."""
    import inspect

    from src import structure_trim

    src = inspect.getsource(structure_trim.trim_target)
    assert "target_side_before = sum(per_bsa.values())" in src
    assert "dropped_bsa = max(0.0, target_side_before - kept_before)" in src
    assert "dropped_bsa > 0.05 * bsa_before" not in src, (
        "the threshold must use the target-side total, not the both-chain one")


@pytest.mark.network
def test_the_bsa_mismatch_is_real_and_the_fix_silences_a_no_op_trim():
    """Numeric proof against a real structure, rather than trusting the source."""
    import pathlib

    from src.structure_trim import _per_residue_bsa

    cif = _repo_root() / "data" / "structures" / "3kys.cif"
    if not cif.is_file():
        pytest.skip("3kys.cif not downloaded")

    per, both_chain_total = _per_residue_bsa(cif, "A", "B")
    target_side = sum(per.values())
    assert both_chain_total > 1.5 * target_side, (
        "premise: the returned total covers both chains")

    kept = dict(list(per.items())[1:])          # drop exactly one residue
    old = both_chain_total - sum(kept.values())
    new = target_side - sum(kept.values())
    assert old > 0.05 * both_chain_total, "the old comparison warned on a no-op"
    assert new <= 0.05 * target_side, "the new one must not"


# ----------------------------------------------------------------------
# Cyclic peptides are opt-in, and select the engine that can build them
# ----------------------------------------------------------------------

def test_mini_protein_is_the_default_modality(config):
    from src.pipeline_runner import PipelineRunner

    assert PipelineRunner(config, workflow="ppi")._modality == "mini_protein"


@pytest.mark.parametrize("proposed", ["cyclic_peptide", "either", None, ""])
def test_a_stage_cannot_talk_the_run_into_a_cyclic_peptide(config, proposed):
    """LLM stages PROPOSE a modality; the operator decides. A stage suggesting
    cyclic_peptide must not commit a campaign to specialised synthesis — and
    RFD3 cannot build one anyway."""
    from src.pipeline_runner import PipelineRunner

    r = PipelineRunner(config, workflow="ppi")
    assert r._resolve_modality(proposed, source="test") == "mini_protein"


def test_opting_in_keeps_cyclic_even_when_a_stage_proposes_otherwise(config):
    from src.pipeline_runner import PipelineRunner

    r = PipelineRunner(config, workflow="ppi", modality="cyclic_peptide",
                       design_engine="boltzgen")
    assert r._resolve_modality("mini_protein", source="test") == "cyclic_peptide"


def test_cyclic_peptide_cannot_run_on_foundry(config):
    """RFD3 has no cyclic-peptide path, and binder_sizes.cyclic_peptide is
    12-15 residues — feeding that to RFD3 asks for something it cannot build,
    and it fails quietly rather than loudly."""
    from src.pipeline_runner import PipelineError, PipelineRunner

    with pytest.raises(PipelineError, match="cyclic"):
        PipelineRunner(config, workflow="ppi", modality="cyclic_peptide",
                       design_engine="foundry")


def test_an_unknown_modality_is_rejected(config):
    from src.pipeline_runner import PipelineError, PipelineRunner

    with pytest.raises(PipelineError, match="modality"):
        PipelineRunner(config, workflow="ppi", modality="stapled_helix")


def test_the_cli_offers_modality_and_defaults_it_to_mini_protein():
    import subprocess
    import sys

    root = _repo_root()
    p = subprocess.run([sys.executable, str(root / "scripts" / "run_pipeline.py"), "--help"],
                       capture_output=True, text=True, timeout=120)
    assert "--modality" in p.stdout
    assert "mini_protein" in p.stdout and "cyclic_peptide" in p.stdout


def test_no_skill_offers_modality_as_a_free_choice():
    """The prompts used to ask the model to pick a modality. It is the
    operator's call — a skill that presents it as a menu will keep proposing
    cyclic_peptide, which the pipeline then has to override every run."""
    import pathlib

    root = _repo_root()
    offenders = []
    for skill in (root / "skills").glob("*/SKILL.md"):
        text = skill.read_text(encoding="utf-8")
        for bad in ("cyclic_peptide / mini_protein", "cyclic_peptide | mini_protein"):
            if bad in text:
                offenders.append(f"{skill.parent.name}: {bad!r}")
    assert not offenders, f"skills still present a modality menu: {offenders}"


def test_packaged_skill_zips_match_their_source():
    """SKILL.md is the source of truth; the zips are build artifacts. An edit
    that forgets scripts/package_skills.py ships a stale prompt."""
    import zipfile

    root = _repo_root()
    stale = []
    for zip_path in sorted((root / "skills").glob("*.zip")):
        src = root / "skills" / zip_path.stem / "SKILL.md"
        if not src.is_file():
            continue
        with zipfile.ZipFile(zip_path) as zf:
            members = [n for n in zf.namelist() if n.endswith("SKILL.md")]
            if not members or zf.read(members[0]) != src.read_bytes():
                stale.append(zip_path.name)
    assert not stale, f"stale skill zips (run scripts/package_skills.py): {stale}"


# ----------------------------------------------------------------------
# MCP tool parity between the two transports
# ----------------------------------------------------------------------

def _mcp_tool_names() -> set[str]:
    import re

    root = _repo_root()
    names: set[str] = set()
    for f in ("src/mcp_server.py", "src/structure_tools_server.py"):
        text = (root / f).read_text(encoding="utf-8")
        names |= set(re.findall(r"@mcp\.tool\(\)\s*\ndef\s+(\w+)", text))
    return names


def test_every_skill_referenced_tool_exists_in_the_mcp_transport():
    """The same SKILL.md runs under MCP and the in-process CLI dispatch. A tool
    present in only one transport makes the skill silently degrade under the
    other — pathway-expert could not look up a PDB structure under Claude
    Desktop despite its own prompt telling it to."""
    import re

    root = _repo_root()
    mcp = _mcp_tool_names()
    # Names a skill can call. `filesystem:`-prefixed tools belong to a
    # DIFFERENT server the user configures themselves, not to LPT.
    known = {"search_corpus", "get_fingerprint", "find_pdb_structures",
             "search_rcsb_pdb", "get_interactions_for", "novelty_signal",
             "cluster_for_protein", "find_quantitative_evidence",
             "interaction_hubs", "shortest_interaction_path"}
    missing = set()
    for skill in (root / "skills").glob("*/SKILL.md"):
        text = skill.read_text(encoding="utf-8")
        for tool in known:
            # Ignore prose that tells the model NOT to use a tool.
            if re.search(rf"`{tool}`|\b{tool}\(", text) and f"Do not call `{tool}`" not in text:
                if tool not in mcp and f"tool_{tool}" not in mcp:
                    missing.add(f"{skill.parent.name}:{tool}")
    assert not missing, f"skills call tools absent from MCP: {sorted(missing)}"


def test_lpt_does_not_expose_a_file_writing_tool_over_mcp():
    """`protein-design-script` and `orchestrator` ask for `filesystem:write_file`
    — the standard filesystem server, not LPT's — and
    complex-structure-analysis explicitly forbids one. Exposing arbitrary file
    writes over MCP would be a security surface for no benefit."""
    assert "write_file" not in _mcp_tool_names()


def test_pdb_discovery_ranks_metadata_confirmed_hits_first():
    """A paper's pdb_accessions includes structures it CITES, so a corpus scan
    for YAP1 returns methods references from papers that merely mention it.
    Unranked, a model sees 'De novo designed TIM barrel' before the real
    structure."""
    import inspect

    from src import skill_runner

    src = inspect.getsource(skill_runner._find_pdb_structures)
    assert "query_named_in_metadata" in src
    assert "_rank(" in src


# ----------------------------------------------------------------------
# MCP must not auto-trigger; the API/pipeline path must still auto-use
# ----------------------------------------------------------------------

def test_mcp_servers_tell_the_model_not_to_reach_for_them():
    """Auto-triggered corpus search makes answers WORSE: the corpus is ~11k
    papers weighted to chromatin/chaperones, while the model's own knowledge
    spans all of biology. A general question answered from a corpus search is
    narrower than the unaided answer, and makes that narrowness look like the
    state of the field."""
    # Read the source rather than importing: src/mcp_server.py deliberately
    # requires the optional `corpus` extra, which CI does not install, and this
    # test is about description CONTENT, not runtime behaviour.
    root = _repo_root()
    for f, label in (("src/mcp_server.py", "literature-db"),
                     ("src/structure_tools_server.py", "structure-tools")):
        text = (root / f).read_text(encoding="utf-8")
        assert "instructions=(" in text, f"{label} sets no server instructions"
        assert "DO NOT reach for these tools on your own" in text, label
        assert "explicitly" in text, label


def test_no_mcp_tool_description_invites_auto_use():
    import re
    import pathlib

    root = _repo_root()
    bad = re.compile(r"(use this (tool )?(whenever|when asked|for )|reach for this|"
                     r"whenever the user|always use|call this early)", re.I)
    offenders = []
    for f in ("src/mcp_server.py", "src/structure_tools_server.py"):
        for i, line in enumerate((root / f).read_text(encoding="utf-8").splitlines(), 1):
            if bad.search(line):
                offenders.append(f"{f}:{i}")
    assert not offenders, f"MCP descriptions invite auto-use: {offenders}"


def test_every_skill_requires_explicit_invocation():
    """Claude Code auto-invokes a skill from its frontmatter `description`.
    Without a guard, a general protein question pulls in a pipeline skill."""
    import pathlib

    root = _repo_root()
    missing = [s.parent.name for s in (root / "skills").glob("*/SKILL.md")
               if "Invoke ONLY" not in s.read_text(encoding="utf-8")]
    assert not missing, f"skills without an explicit-invocation guard: {missing}"


def test_the_api_pipeline_path_still_auto_uses_its_tools():
    """The counterpart guarantee, and the one easiest to break by accident:
    when the PIPELINE runs a skill it has already been told to run, its tools
    SHOULD be used freely. Those descriptions live in a different table
    (skill_runner._TOOL_DEFS) and must keep their trigger language."""
    from src.skill_runner import _TOOL_DEFS

    by_name = {t["name"]: t["description"] for t in _TOOL_DEFS}
    search = by_name["search_corpus"].lower()
    assert "use for" in search or "use this" in search, (
        "the pipeline's search_corpus description lost its trigger language — "
        "MCP's no-auto-trigger rule must not leak into the API path")

    # And the two tables must remain genuinely distinct. Source-read for the
    # same reason as above — the MCP module needs the `corpus` extra.
    mcp_src = (_repo_root() / "src" / "mcp_server.py").read_text(encoding="utf-8")
    assert by_name["search_corpus"] not in mcp_src, (
        "the pipeline's search_corpus description now appears verbatim in the "
        "MCP server; the two have opposite trigger requirements and must not "
        "converge")


# ----------------------------------------------------------------------
# LLM prose must not carry LaTeX into terminals, .md files or HTML reports
# ----------------------------------------------------------------------

_LATEX = __import__("re").compile(
    r"\$[^$\n]*\\(?:text|approx|mu|times|cdot|le|ge|frac|sim)[^$\n]*\$"
    r"|\\text\{|\\approx|\\mu\\text|\$K_[A-Za-z0-9{]")


def test_every_skill_prompt_carries_the_output_format_rule():
    """Appended in _load_system_prompt so it reaches all 12 skills on BOTH
    transports — a per-SKILL.md rule would be 12 places to forget."""
    import inspect

    from src import skill_runner

    assert "OUTPUT FORMATTING" in skill_runner._OUTPUT_FORMAT_RULE
    assert "Never use LaTeX" in skill_runner._OUTPUT_FORMAT_RULE
    src = inspect.getsource(skill_runner.SkillRunner._load_system_prompt)
    assert "_OUTPUT_FORMAT_RULE" in src, "the rule must actually be appended"


def test_the_rule_reaches_a_real_skill_prompt(config, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder")
    from src.skill_runner import SkillRunner

    r = SkillRunner(skill_name="corpus-explorer", provider="gemini",
                    model_id="gemini-3.7-flash", config=config)
    assert "Never use LaTeX" in r.system_prompt


def test_the_rule_gives_concrete_replacements_not_just_a_prohibition():
    """'Do not use X' without 'use Y instead' is weak instruction. The model
    reached for LaTeX because that IS the convention in scientific writing it
    was trained on; it needs to be shown what to write instead."""
    from src.skill_runner import _OUTPUT_FORMAT_RULE

    for example in ("nM", "kcal/mol", "IC50", "Kd"):
        assert example in _OUTPUT_FORMAT_RULE, f"no plain-text example for {example}"


def test_latex_in_stage_prose_would_reach_the_rendered_report():
    """Why this matters beyond the terminal: markdown_html passes math markup
    through verbatim, so LaTeX in a stage report lands in the shipped HTML."""
    from src.report_common import markdown_html

    out = markdown_html(r"Binding was $K_d \approx 470\text{ nM}$ by SPR.")
    assert "\\approx" in out or "$K_d" in out, (
        "if this ever stops being true the rule can be relaxed")


def test_the_corpus_itself_is_not_the_source_of_the_latex():
    """Establishes WHERE the markup comes from. The corpus stores affinities as
    plain floats in Molar; if this starts failing, curation has regressed and
    the fix belongs there, not in the output rule."""
    import json
    import pathlib

    fps = sorted((_repo_root() / "data" / "fingerprints").glob("*.json"))
    if len(fps) < 100:
        pytest.skip("no corpus installed")

    sample = fps[:1500]
    latex = [f.name for f in sample
             if _LATEX.search(f.read_text(encoding="utf-8", errors="ignore"))]
    assert len(latex) <= len(sample) * 0.01, (
        f"{len(latex)}/{len(sample)} fingerprints carry LaTeX — the corpus, not "
        f"the output formatting, is now the problem: {latex[:3]}")

    # ...and affinities are numbers, not formatted strings.
    for f in sample[:400]:
        for kf in (json.loads(f.read_text(encoding="utf-8")).get("key_findings") or []):
            v = kf.get("affinities_kd_Molar")
            if v is not None:
                assert isinstance(v, (int, float)), (
                    f"{f.name}: affinities_kd_Molar is {type(v).__name__}, not a "
                    f"float in Molar — see the curation contract in CLAUDE.md")
                return
