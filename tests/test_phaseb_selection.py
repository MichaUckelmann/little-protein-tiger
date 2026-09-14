"""Phase B's arm selection: the parts that fail SILENTLY if they regress.

Phase B refolds ~1,800 designs (~5.4 GPU-h) to compare patch-heavy against
patch-light designs from the SAME trim rung. Every defect this file pins was
found by running the selector on the real Phase A ladders and reading what it
chose, and every one of them produces a plausible-looking result rather than
an error:

  * matching on covariates alone paired a 3-contact "heavy" design with a
    2-contact "light" one — a one-contact gap, i.e. no contrast at all;
  * without a caliper on TOTAL target contacts, 3KYS rung 120's heavy arm
    (52 contacts) was matched against designs making 36 — so the "effect" of
    patch contact would have been measured against designs making a third
    fewer contacts of every kind;
  * matching on design-stage hotspot engagement conditions on a MEDIATOR:
    the engagement drop is the hypothesised effect, so balancing it biases
    every result toward null.

`PHASEB_MIN_SEPARATION` and `PHASEB_MIN_PAIRS` exist so a rung that cannot
answer the question says so BEFORE the GPU time is spent, rather than
returning a null that reads like evidence of no effect.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bench():
    """Import the script by path — `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "benchmark_trim", _ROOT / "scripts" / "benchmark_trim.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cand(name, *, patch, contacts=40, length=76, eng=1.0, clashes=0, cb=0):
    return {"design": name, "patch_contacts": patch, "n_contacts": contacts,
            "binder_len": length, "engagement": eng, "sc_clashes": clashes,
            "n_chainbreaks": cb, "patch_fraction": patch / contacts}


# ── the light arm is defined by an absolute patch cap, not by matching ──────

def test_the_light_pool_prefers_zero_contact_designs(bench):
    """The cleanest control a ladder can offer is same trim, same epitope, no
    patch contact. If enough such designs exist they must be used."""
    cands = ([_cand(f"z{i}", patch=0) for i in range(20)]
             + [_cand(f"m{i}", patch=2) for i in range(20)]
             + [_cand(f"h{i}", patch=5) for i in range(20)])
    pool, cap = bench._light_pool(cands, n_arm=15, floor=5)
    assert cap == 0, "a cap above 0 was chosen while 20 clean designs existed"
    assert {c["design"] for c in pool} == {f"z{i}" for i in range(20)}


def test_the_cap_rises_only_as_far_as_it_must(bench):
    """With too few clean designs the cap grows — but to the SMALLEST value
    that fills the arm, so the gap stays as wide as the data allows."""
    cands = ([_cand(f"z{i}", patch=0) for i in range(3)]
             + [_cand(f"o{i}", patch=1) for i in range(5)]
             + [_cand(f"t{i}", patch=2) for i in range(30)]
             + [_cand(f"h{i}", patch=9) for i in range(20)])
    pool, cap = bench._light_pool(cands, n_arm=10, floor=9)
    assert cap == 2, f"cap {cap} is not the smallest that yields 10 candidates"
    assert len(pool) == 38


# ── the calipers ────────────────────────────────────────────────────────────

def test_an_unmatchable_heavy_design_is_dropped_not_paired_badly(bench):
    """The real 3KYS failure: nearest-neighbour always returns SOMETHING.

    Here the only light candidates make 30 contacts against the heavy arm's
    50. Pairing them would measure stickiness, not patch diversion, so the
    heavy design must come back unmatched instead.
    """
    heavy = [_cand("h1", patch=8, contacts=50)]
    pool = [_cand(f"l{i}", patch=0, contacts=30) for i in range(10)]
    scale = bench._zscale(heavy + pool, ("binder_len", "n_contacts",
                                         "engagement", "sc_clashes",
                                         "n_chainbreaks"))
    matched, unmatched = bench._match_arms(heavy, pool, scale)
    assert matched == []
    assert [u["design"] for u in unmatched] == ["h1"]


def test_a_partner_inside_the_caliper_is_accepted(bench):
    heavy = [_cand("h1", patch=8, contacts=50)]
    pool = [_cand("far", patch=0, contacts=30),
            _cand("near", patch=0, contacts=48)]
    scale = bench._zscale(heavy + pool, ("binder_len", "n_contacts",
                                         "engagement", "sc_clashes",
                                         "n_chainbreaks"))
    matched, unmatched = bench._match_arms(heavy, pool, scale)
    assert [m["design"] for m in matched] == ["near"]
    assert unmatched == []
    assert matched[0]["matched_to"] == "h1"


def test_the_binder_length_caliper_is_enforced_too(bench):
    heavy = [_cand("h1", patch=8, contacts=40, length=86)]
    pool = [_cand("l1", patch=0, contacts=40, length=70)]
    scale = bench._zscale(heavy + pool, ("binder_len", "n_contacts",
                                         "engagement", "sc_clashes",
                                         "n_chainbreaks"))
    matched, _ = bench._match_arms(heavy, pool, scale)
    assert matched == [], "a 16-residue length difference was accepted"


def test_no_light_design_is_used_twice(bench):
    """Matching is without replacement: reusing one control would fake n."""
    heavy = [_cand(f"h{i}", patch=8, contacts=40) for i in range(3)]
    pool = [_cand("l1", patch=0, contacts=40)]
    scale = bench._zscale(heavy + pool, ("binder_len", "n_contacts",
                                         "engagement", "sc_clashes",
                                         "n_chainbreaks"))
    matched, unmatched = bench._match_arms(heavy, pool, scale)
    assert len(matched) == 1 and len(unmatched) == 2


# ── engagement is a mediator, and must stay out of the default weights ──────

def test_engagement_is_not_a_default_matching_covariate(bench):
    """Pins the decision, not the wording. On 3KYS the engagement drop IS the
    effect under test; balancing it would bias the comparison toward null."""
    assert "engagement" not in bench.PHASEB_MATCH_WEIGHTS
    assert "n_contacts" in bench.PHASEB_MATCH_WEIGHTS, (
        "total contacts must be matched — without it the heavy arm is just "
        "the stickier designs")
    # The scope's original set stays available, deliberately.
    assert "engagement" in bench.PHASEB_MATCH_WEIGHTS_WITH_ENGAGEMENT


# ── the prefilter gate, mirrored from production ────────────────────────────

@pytest.mark.parametrize("cov,ok", [
    ({"n_chainbreaks": 0, "sc_clashes": 0, "bb_clashes": 0,
      "non_loop_fraction": 0.72}, True),
    ({"n_chainbreaks": 2, "sc_clashes": 0, "bb_clashes": 0,
      "non_loop_fraction": 0.72}, False),
    ({"n_chainbreaks": 0, "sc_clashes": 1, "bb_clashes": 0,
      "non_loop_fraction": 0.72}, False),
    ({"n_chainbreaks": 0, "sc_clashes": 0, "bb_clashes": 1,
      "non_loop_fraction": 0.72}, False),
    ({"n_chainbreaks": 0, "sc_clashes": 0, "bb_clashes": 0,
      "non_loop_fraction": 0.41}, False),
])
def test_prefilter_matches_productions_four_criteria(bench, cov, ok):
    """Both arms are restricted to prefilter survivors: a real campaign never
    refolds a design the prefilter rejected, and letting it drop designs
    AFTER matching would undo the clash/chainbreak balance."""
    assert bench._prefilter_ok(cov, max_chainbreaks=1) is ok


def test_binder_length_comes_from_the_difference_not_a_key(bench, tmp_path):
    """There is no binder-length key in an RFD3 sidecar: `num_residues` counts
    the whole complex and `diffused_index_map` has one entry per TARGET
    residue. Reading `num_residues` as the binder length would make every
    design look ~2x longer and the length caliper meaningless."""
    car = tmp_path / "x_model_0.json"
    car.write_text(json.dumps({
        "metrics": {"num_residues": 159, "n_chainbreaks": 0,
                    "n_clashing.interresidue_clashes_w_sidechain": 0,
                    "n_clashing.interresidue_clashes_w_backbone": 0,
                    "non_loop_fraction": 0.72},
        "diffused_index_map": {f"A{i}": f"B{i + 1}" for i in range(90)},
    }), encoding="utf-8")
    cov = bench._design_covariates(car)
    assert cov["binder_len"] == 69


def test_the_dotted_clash_keys_are_read_as_flat_strings(bench, tmp_path):
    """`n_clashing.interresidue_clashes_w_sidechain` is ONE key. Read as a
    nested object it returns 0 for every design, which silently turns the
    clash covariate into a constant and the prefilter into a no-op."""
    car = tmp_path / "y_model_0.json"
    car.write_text(json.dumps({
        "metrics": {"num_residues": 100, "n_chainbreaks": 0,
                    "n_clashing.interresidue_clashes_w_sidechain": 7,
                    "n_clashing.interresidue_clashes_w_backbone": 3,
                    "non_loop_fraction": 0.9},
        "diffused_index_map": {"A1": "B1"},
    }), encoding="utf-8")
    cov = bench._design_covariates(car)
    assert (cov["sc_clashes"], cov["bb_clashes"]) == (7, 3)
    assert bench._prefilter_ok(cov, max_chainbreaks=1) is False


# ── the readout ─────────────────────────────────────────────────────────────

def test_medians_are_reported_for_an_arm_with_no_comparison(bench):
    """A control or single arm has no Mann-Whitney block, and the first
    version of the printer read its medians out of exactly that block — so
    the baseline the paired arms are read against printed as '-'."""
    rows = [{"iptm": 0.70, "binder_rmsd_dock": 42.5, "binder_plddt": 0.76,
             "ipsae_min": 0.15, "hotspot_engagement": 0.5,
             "patch_survival": 0.89},
            {"iptm": 0.54, "binder_rmsd_dock": 19.2, "binder_plddt": 0.76,
             "ipsae_min": 0.06, "hotspot_engagement": 0.667,
             "patch_survival": 0.95}]
    med = bench._medians(rows)
    assert med["iptm"] == 0.62
    assert med["binder_rmsd_dock"] == 30.85
    assert med["patch_survival"] == 0.92


def test_medians_skip_missing_values_rather_than_crashing(bench):
    """A scoring error leaves columns absent, and one bad refold must not
    take the whole rung's readout with it."""
    med = bench._medians([{"iptm": 0.5}, {"error": "boom"}])
    assert med["iptm"] == 0.5
    assert med["binder_rmsd_dock"] is None


def test_mwu_refuses_a_sample_too_small_to_test(bench):
    """Below three per arm there is no test to run, and returning a p-value
    anyway would invite reading one."""
    out = bench._mwu([0.5, 0.6], [0.4, 0.3])
    assert out["p"] is None


def test_mwu_reports_a_p_value_and_both_medians(bench):
    a, b = [0.8, 0.82, 0.85, 0.9], [0.4, 0.42, 0.45, 0.5]
    out = bench._mwu(a, b)
    assert out["p"] is not None and out["p"] < 0.05
    assert out["median_a"] > out["median_b"]


# ── the dock readout: a bimodal median is not an effect size ────────────────

def test_the_dock_summary_is_a_fraction_not_a_median(bench):
    """Measured on 3KYS rung 173: refolds dock under 5 A or are lost beyond
    30 A, with 2 of 104 in between. The design-level medians came out 20.94 A
    (heavy) against 2.26 A (light) — a 9x gap that a rank test put at p=0.21,
    because both refold distributions sat at ~31-33 A and the real contrast
    was 28 % vs 37 % of refolds docking at all."""
    rows = ([{"binder_rmsd_dock": 1.5}] * 3 + [{"binder_rmsd_dock": 40.0}] * 7)
    out = bench._dock_fraction(rows, 5.0)
    assert out["k"] == 3 and out["n"] == 10
    assert out["frac"] == 0.3
    assert out["ci95"][0] < 0.3 < out["ci95"][1]
    assert out["threshold_A"] == 5.0


def test_the_dock_threshold_comes_from_the_gate(bench):
    """The reported fraction and the gate-pass rate must not disagree about
    what "docked" means, so both read `binder_rmsd_dock_max`."""
    import inspect

    src = inspect.getsource(bench.phaseb_analyze)
    assert "binder_rmsd_dock_max" in src
    assert "DEFAULT_THRESHOLDS" in src


def test_bimodality_is_detected_when_the_middle_is_empty(bench):
    rows = ([{"binder_rmsd_dock": 1.2}] * 20 + [{"binder_rmsd_dock": 38.0}] * 20)
    assert bench._is_bimodal(rows) is True


def test_a_unimodal_distribution_is_not_flagged(bench):
    rows = [{"binder_rmsd_dock": v} for v in
            (6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 6.5, 7.5, 8.5)]
    assert bench._is_bimodal(rows) is False


def test_too_few_records_are_never_called_bimodal(bench):
    """The check exists to stop a median being quoted as an effect, and on
    nine records there is nothing to characterise either way."""
    assert bench._is_bimodal([{"binder_rmsd_dock": 1.0},
                              {"binder_rmsd_dock": 40.0}]) is False


def test_no_dock_values_reports_nothing_rather_than_zero(bench):
    """A rung whose refolds all failed scoring must not read as 0 % docked."""
    out = bench._dock_fraction([{"error": "boom"}], 5.0)
    assert out["frac"] is None and out["n"] == 0
