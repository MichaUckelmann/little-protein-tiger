"""The BoltzGen ranking path, exercised against the SHIPPED thresholds.

This file exists because `design.thresholds` in `config.yaml` had never gated a
real run. All four e2e drivers overrode the same four values — `iptm_min 0.10,
ipae_max 25.0, hotspot_sasa_delta_min 0.0, require_boltzgen_pass False` — so
every archived `filter_stats.txt` reads "survivors: 100 / 100, drop reasons:
(none)", and the numbers in the config were inherited rather than measured.
`src/design_metrics.py` and `src/design_runner.py` had zero test imports
between them.

Two shapes, matching this repo's convention elsewhere (see
`tests/test_ppi_report.py`):

* **synthetic**, always run — the funnel's own invariants and the drop-order
  contract.
* **real-data**, skipped when a checkout has no `outputs/` — the shipped
  thresholds applied to real BoltzGen output, which is the only thing that can
  catch a threshold that is unreachable for a whole modality.

Nothing here needs PyRosetta or a GPU; the real-data tests read an archived
`05_metrics_enriched.csv` as a fixture rather than recomputing SASA.
"""

from __future__ import annotations

import csv
import pathlib

import pytest
import yaml

from src.design_ranking import filter_records, rank_designs

_ROOT = pathlib.Path(__file__).resolve().parents[1]
#: A cyclic-peptide campaign whose designs bury very little hotspot area.
_CGAS = _ROOT / "outputs" / "e2e_cgas_sting"
#: A cyclic-peptide campaign against a groove, which buries a lot.
_MESO = _ROOT / "outputs" / "e2e_mesothelioma"


def _shipped() -> dict:
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    return cfg["design"]["thresholds"]


def _enriched(run_dir: pathlib.Path) -> list[dict]:
    with (run_dir / "05_metrics_enriched.csv").open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _rec(**kw) -> dict:
    """A design that passes every shipped gate; override to fail one."""
    base = dict(design_id="d1", pass_filters="True", design_to_target_iptm=0.80,
                min_design_to_target_pae=5.0, lpt_hotspot_sasa_delta=200.0,
                complex_plddt=0.80, designed_chain_sequence="ACDEFGHIKL")
    base.update(kw)
    return base


# ── synthetic: the invariants ────────────────────────────────────────────────

def test_the_shipped_defaults_are_what_the_code_falls_back_to():
    """A driver that passes no thresholds must get config.yaml's values, not
    a second set of literals hidden in the module."""
    t = _shipped()
    assert t["require_boltzgen_pass"] is True
    assert t["iptm_min"] == 0.60
    assert t["ipae_max"] == 10.0


def test_boltzgen_pass_is_tested_first_so_drops_attribute_to_it():
    """`pass_filters` is the most informative column BoltzGen writes — it is
    dominated by a design-vs-refold RMSD check — so a design failing it should
    be reported as such rather than as an iPTM failure."""
    recs = [_rec(pass_filters="False", design_to_target_iptm=0.01)]
    _, stats = filter_records(recs, iptm_min=0.60, ipae_max=10.0,
                              hotspot_sasa_delta_min=30.0,
                              require_boltzgen_pass=True)
    assert stats.dropped == {"boltzgen_pass": 1}


def test_the_funnel_accounts_for_every_input():
    """survivors + drops == inputs. A funnel that loses designs silently is how
    an empty top-K gets read as 'the designs were bad'."""
    recs = [
        _rec(design_id="ok"),
        _rec(design_id="bad_pass", pass_filters="False"),
        _rec(design_id="bad_iptm", design_to_target_iptm=0.1),
        _rec(design_id="bad_ipae", min_design_to_target_pae=99.0),
        _rec(design_id="bad_sasa", lpt_hotspot_sasa_delta=0.0),
    ]
    surv, stats = filter_records(recs, iptm_min=0.60, ipae_max=10.0,
                                 hotspot_sasa_delta_min=30.0,
                                 require_boltzgen_pass=True)
    assert stats.n_input == len(recs)
    assert len(surv) == stats.n_survivors == 1
    assert stats.n_survivors + sum(stats.dropped.values()) == stats.n_input


def test_disabling_the_sasa_gate_is_a_skip_not_a_universal_failure():
    """When PyRosetta never ran, nothing carries the column. Failing every
    design for it would blame the designs for a missing tool."""
    recs = [_rec(lpt_hotspot_sasa_delta="")]
    surv, _ = filter_records(recs, iptm_min=0.60, ipae_max=10.0,
                             hotspot_sasa_delta_min=30.0,
                             require_boltzgen_pass=True,
                             hotspot_sasa_available=False)
    assert len(surv) == 1


def test_an_empty_survivor_set_ranks_without_raising():
    """The NO_GO path has to produce artifacts, not a traceback."""
    res = rank_designs([_rec(pass_filters="False")],
                       thresholds=_shipped(),
                       weights={"design_to_target_iptm": 1.0},
                       mmr={"lambda_": 0.5, "seq_identity_cap": 0.7}, top_k=20)
    assert res.survivors == [] and res.top_k == []


# ── real data: what the shipped thresholds actually do ──────────────────────

@pytest.mark.skipif(not _MESO.exists(),
                    reason="outputs/e2e_mesothelioma not present in this checkout")
def test_the_shipped_thresholds_are_not_vacuous_on_a_real_campaign():
    """They must admit SOMETHING on a campaign that produced good designs.

    Measured: 5 of 100 survive on this run (53 dropped on `pass_filters`, 42 on
    iPTM). A config that admits zero everywhere would hand the design-analyst an
    empty top-K and read as "the designs were bad".
    """
    t = _shipped()
    surv, stats = filter_records(
        _enriched(_MESO), iptm_min=t["iptm_min"], ipae_max=t["ipae_max"],
        hotspot_sasa_delta_min=t["hotspot_sasa_delta_min"],
        require_boltzgen_pass=t["require_boltzgen_pass"])
    assert stats.n_input == 100
    assert len(surv) > 0, (
        f"the shipped thresholds admit nothing on {_MESO.name}: "
        f"{dict(stats.dropped)}")


@pytest.mark.skipif(not _CGAS.exists(),
                    reason="outputs/e2e_cgas_sting not present in this checkout")
def test_boltzgens_own_filter_is_the_gate_that_does_the_work():
    """On this run it alone accounts for 99 of 100 drops.

    Which is why the four drivers overriding it to False is what kept the rest
    of the block unexercised — with it off, everything downstream passed.
    """
    t = _shipped()
    _, stats = filter_records(
        _enriched(_CGAS), iptm_min=t["iptm_min"], ipae_max=t["ipae_max"],
        hotspot_sasa_delta_min=t["hotspot_sasa_delta_min"],
        require_boltzgen_pass=t["require_boltzgen_pass"])
    assert stats.dropped.get("boltzgen_pass", 0) >= 90


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN, MEASURED: hotspot_sasa_delta_min = 30.0 is unreachable for cyclic "
    "peptides. Across outputs/e2e_cgas_sting the column spans 0.0-24.7 A^2, so "
    "--modality cyclic_peptide on the shipped config yields an empty top-K BY "
    "CONSTRUCTION, without crashing, and hands the design-analyst nothing. "
    "Mini-proteins on the same pipeline bury 190-430 A^2, so one scalar cannot "
    "serve both. Closing this needs per-modality thresholds; this test turns "
    "green when they land."))
@pytest.mark.skipif(not _CGAS.exists(),
                    reason="outputs/e2e_cgas_sting not present in this checkout")
def test_every_shipped_threshold_is_reachable_for_the_modality_it_gates():
    """A gate no design of a given modality can ever pass is not strictness.

    This is the check that would have caught the cyclic-peptide SASA gate: it
    compares each threshold against the observed range of its own column rather
    than against an opinion about what a good design looks like.
    """
    t = _shipped()
    rows = _enriched(_CGAS)
    sasa = [float(r["lpt_hotspot_sasa_delta"]) for r in rows
            if r.get("lpt_hotspot_sasa_delta") not in (None, "")]
    assert max(sasa) >= t["hotspot_sasa_delta_min"], (
        f"no design reaches the gate: max {max(sasa):.1f} < "
        f"{t['hotspot_sasa_delta_min']}")
