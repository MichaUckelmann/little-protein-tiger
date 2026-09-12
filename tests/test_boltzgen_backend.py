"""B4: BoltzGen dispatched as the binder track's generator.

The binder track was foundry-only by construction — `config.yaml` said so
outright — so this is the seam that did not exist. These tests cover the two
things that can go wrong without a GPU: the dispatch picking the wrong branch,
and production being sized from the wrong place after a restart.

The GPU stages themselves are validated by running them; what is pinned here is
everything around them.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml

from src.pipeline_runner import PipelineRunner

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_3N7S = _ROOT / "data" / "structures" / "3N7S_ba1.cif"


def _runner(**kw) -> PipelineRunner:
    """A runner with only the attributes these tests touch.

    Built without `__init__` on purpose: constructing one reaches for a project
    directory, a provider and a config it does not need here, and the point is
    to test the dispatch rather than the constructor.
    """
    obj = object.__new__(PipelineRunner)
    obj.config = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    obj._modality = kw.get("modality", "mini_protein")
    obj._design_engine = kw.get("design_engine", "foundry")
    obj._workflow = kw.get("workflow", "binder")
    return obj


# ── which branch runs ───────────────────────────────────────────────────────

def test_foundry_is_the_default_backend():
    assert _runner(design_engine="foundry")._boltzgen_backend is False


def test_boltzgen_is_selected_by_the_design_engine():
    assert _runner(design_engine="boltzgen")._boltzgen_backend is True


def test_the_stage_sizes_come_from_config():
    r = _runner(design_engine="boltzgen")
    assert r._boltzgen_stage_sizes("pilot") == (24, 8)
    assert r._boltzgen_stage_sizes("calibration")[0] == 1000
    # Production's config value is a CEILING; the calibration sizes the run.
    assert r._boltzgen_stage_sizes("production")[0] >= 1000


def test_the_calibration_size_can_clear_the_minimum_hit_count():
    """At the measured 1.21% gated rate a calibration must still produce >= 5
    designs above the bar, or every verdict is "enlarge the sample"."""
    from src.campaign_calibration import MIN_HITS_FOR_ESTIMATE

    n, _ = _runner(design_engine="boltzgen")._boltzgen_stage_sizes("calibration")
    assert n * 0.0121 >= MIN_HITS_FOR_ESTIMATE


# ── complex size, which both cost laws key on ───────────────────────────────

class _Trim:
    def __init__(self, n=84, contig="12-15,/0,D27-110"):
        self.n_residues_after = n
        self.contig = contig


def test_tokens_are_target_plus_binder_midpoint():
    r = _runner(design_engine="boltzgen", modality="cyclic_peptide")
    assert r._boltzgen_tokens(_Trim(84, "12-15,/0,D27-110")) == 84 + 13


def test_a_malformed_contig_falls_back_per_modality_not_to_a_mini_protein():
    """`_binder_midpoint`'s own fallback is 78 — a mini-protein midpoint — and
    the cost laws are superlinear in this number, so using it for a macrocycle
    would over-state the complex by ~65 residues."""
    cyc = _runner(design_engine="boltzgen", modality="cyclic_peptide")
    mini = _runner(design_engine="boltzgen", modality="mini_protein")
    assert cyc._boltzgen_tokens(_Trim(84, "")) == 84 + 13
    assert mini._boltzgen_tokens(_Trim(84, "")) == 84 + 78


def test_no_trim_size_means_no_estimate_rather_than_a_wrong_one():
    r = _runner(design_engine="boltzgen")
    assert r._boltzgen_tokens(_Trim(0, "")) is None


# ── production sizing must survive a fresh process ──────────────────────────

def _dirs(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    d = {k: tmp_path / k for k in ("binder", "calibration", "campaign",
                                   "scoring", "spec", "trim")}
    for v in d.values():
        v.mkdir(parents=True, exist_ok=True)
    return d


def test_an_in_memory_calibration_is_used_directly(tmp_path):
    r = _runner(design_engine="boltzgen")
    n = r._resolve_boltzgen_production({"n_designs": 4242}, _dirs(tmp_path))
    assert n == 4242


def test_the_size_is_recovered_from_disk_on_a_fresh_process(tmp_path):
    """The hazard `_resolve_production_plan` exists for, and which bit the
    foundry track once: the recommendation only lived in the process that
    measured it, so a `--start-from production` resume silently ran at the
    config default instead."""
    dirs = _dirs(tmp_path)
    (dirs["calibration"] / "calibration.json").write_text(
        json.dumps({"verdict": "SCALE_UP", "n_production": 7226}),
        encoding="utf-8")
    r = _runner(design_engine="boltzgen")
    assert r._resolve_boltzgen_production(None, dirs) == 7226


def test_scaling_a_campaign_the_measurement_said_to_stop_is_refused(tmp_path):
    """The one mistake this stage can make that costs GPU-days."""
    from src.pipeline_runner import PipelineError

    dirs = _dirs(tmp_path)
    for verdict in ("ITERATE", "STOP"):
        (dirs["calibration"] / "calibration.json").write_text(
            json.dumps({"verdict": verdict, "n_production": 9999}),
            encoding="utf-8")
        with pytest.raises(PipelineError, match=verdict):
            _runner(design_engine="boltzgen")._resolve_boltzgen_production(
                None, dirs)


def test_a_partial_scale_up_is_still_allowed(tmp_path):
    dirs = _dirs(tmp_path)
    (dirs["calibration"] / "calibration.json").write_text(
        json.dumps({"verdict": "SCALE_UP_PARTIAL", "n_production": 500}),
        encoding="utf-8")
    assert _runner(design_engine="boltzgen")._resolve_boltzgen_production(
        None, dirs) == 500


def test_a_missing_calibration_warns_and_uses_the_ceiling(tmp_path):
    """Not an error: an operator may deliberately run production alone. But it
    must say that the size is a config default rather than a measurement."""
    from loguru import logger

    msgs: list[str] = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        r = _runner(design_engine="boltzgen")
        n = r._resolve_boltzgen_production(None, _dirs(tmp_path))
    finally:
        logger.remove(hid)
    assert n == r._boltzgen_stage_sizes("production")[0]
    assert any("rather than" in m for m in msgs), msgs


def test_an_unreadable_calibration_is_an_error_not_a_silent_default(tmp_path):
    from src.pipeline_runner import PipelineError

    dirs = _dirs(tmp_path)
    (dirs["calibration"] / "calibration.json").write_text("{not json",
                                                          encoding="utf-8")
    with pytest.raises(PipelineError, match="could not read"):
        _runner(design_engine="boltzgen")._resolve_boltzgen_production(None, dirs)


# ── the spec stage, which needs no GPU ──────────────────────────────────────

@pytest.mark.skipif(not _3N7S.exists(), reason="3N7S_ba1.cif not in this checkout")
def test_the_spec_stage_writes_a_yaml_and_a_report(tmp_path, monkeypatch):
    from src.pipeline_runner import PipelineResult

    r = _runner(design_engine="boltzgen", modality="cyclic_peptide")
    monkeypatch.setattr(type(r), "_binder_structure_path",
                        lambda self, pdb: _3N7S, raising=False)
    monkeypatch.setattr(type(r), "_record_stage",
                        lambda self, *a, **k: None, raising=False)
    dirs = _dirs(tmp_path)
    hotspots = json.dumps({"target_chain": "D", "partner_chain": "A",
                           "residues": [{"residue": "TRP", "auth_seq_id": 74},
                                        {"residue": "PHE", "auth_seq_id": 83},
                                        {"residue": "TRP", "auth_seq_id": 84}]})
    result = PipelineResult(run_dir=tmp_path, pdb_id="3N7S")
    path = r._stage_boltzgen_spec({"target_gene": "RAMP1"}, hotspots,
                                  _Trim(84, "12-15,/0,D27-110"), dirs, result)
    assert path.suffix == ".yaml" and path.is_file()
    text = path.read_text(encoding="utf-8")
    # The binding list must be the deposited label_seq, verified by name.
    assert "binding: 53,62,63" in text
    assert "cyclic: True" in text
    report = dirs["binder"] / r._BINDER_STAGE_FILES["binder_spec"]
    assert report.is_file() and "BoltzGen design specification" in report.read_text()


@pytest.mark.skipif(not _3N7S.exists(), reason="3N7S_ba1.cif not in this checkout")
def test_a_spec_the_builder_refuses_becomes_a_pipeline_error(tmp_path, monkeypatch):
    """Every SpecError corresponds to a downstream failure that is SILENT, so
    none of them may degrade to a warning here."""
    from src.pipeline_runner import PipelineError, PipelineResult

    r = _runner(design_engine="boltzgen", modality="cyclic_peptide")
    monkeypatch.setattr(type(r), "_binder_structure_path",
                        lambda self, pdb: _3N7S, raising=False)
    monkeypatch.setattr(type(r), "_record_stage",
                        lambda self, *a, **k: None, raising=False)
    hotspots = json.dumps({"target_chain": "D", "residues": [
        {"residue": "VAL", "auth_seq_id": 74}]})      # the file has TRP74
    with pytest.raises(PipelineError, match="refused"):
        r._stage_boltzgen_spec({}, hotspots, _Trim(), _dirs(tmp_path),
                               PipelineResult(run_dir=tmp_path, pdb_id="3N7S"))


def test_an_empty_hotspot_table_is_refused(tmp_path):
    from src.pipeline_runner import PipelineError, PipelineResult

    r = _runner(design_engine="boltzgen")
    with pytest.raises(PipelineError, match="no target chain or no residues"):
        r._stage_boltzgen_spec(
            {}, json.dumps({"target_chain": "", "residues": []}), _Trim(),
            _dirs(tmp_path), PipelineResult(run_dir=tmp_path, pdb_id="3N7S"))
