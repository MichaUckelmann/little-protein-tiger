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
