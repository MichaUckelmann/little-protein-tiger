"""The trim's fresh hydrophobic patch is a per-design SCORED liability.

Phase B's pre-registered branch 3 (`GLUE_PIPELINE_SCOPE.md`, "Phase B —
COMPLETE"): 1,784 refolds over 9 rungs and 143 matched pairs found **no
detectable quality cost** to patch-heavy designs at matched total contacts —
gate-pass ratios 1.125 (95% CI 0.711-1.781) on 6VJJ and 0.882 (0.488-1.597)
on 3KYS, every per-rung U test null — but the patch contacts SURVIVE
refolding (patch survival 1.000 in five of seven heavy arms). Real binding to
an artificial surface, of unmeasured magnitude. So: score it, do not gate on
it, and weight it as a tie-breaker.

Two things this file pins hardest, because both are easy to get wrong later:

1. **`patch_enrichment`, not `patch_contact_fraction`, is the rankable
   statistic.** The raw fraction scales with the patch's SIZE, which is a
   property of the trim and identical for every design in a campaign — so
   ranking on it would shift every design by the same amount and reorder
   nothing while appearing to work.
2. **The trim's exposure refusals are NOT removed.** Phase B held total
   contacts fixed, so it tested "does having contacts on the patch cost
   anything" and never "does exposing a patch cost anything" — which is the
   question the guards answer. 3KYS rung 90, the highest near-epitope dose,
   returned 0 of 40 designs through the gates against a control's 23 of 60.
"""

from __future__ import annotations

import contextlib
import json
import pathlib

import pytest

from src import binder_metrics as bm

_ROOT = pathlib.Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def captured_logs(level: str = "WARNING"):
    from loguru import logger

    out: list[str] = []
    sink = logger.add(lambda m: out.append(m.record["message"]), level=level)
    try:
        yield out
    finally:
        logger.remove(sink)


def _sidecar(tmp_path, imap: dict) -> pathlib.Path:
    p = tmp_path / "d_model_0.json"
    p.write_text(json.dumps({"specification": {"select_hotspots": {}},
                             "diffused_index_map": imap}), encoding="utf-8")
    return p


# ── the remap ──────────────────────────────────────────────────────────────

def test_author_ids_are_remapped_through_the_sidecar(tmp_path):
    """RFD3 renumbers its target to B1..N and the relabelling exists only in a
    design sidecar's `diffused_index_map`. Measured on a real 3KYS campaign
    sidecar: author A195-A401 maps to B1-B207."""
    sc = _sidecar(tmp_path, {f"A{195 + i}": f"B{1 + i}" for i in range(207)})
    assert bm.patch_from_rfd3(sc, [195, 196, 197], "B") == [1, 2, 3]
    assert bm.patch_from_rfd3(sc, [401], "B") == [207]


def test_an_empty_patch_remaps_to_nothing_without_complaint(tmp_path):
    """The usual case — 22 of the 25 trims in `projects/` are no-ops that
    expose nothing."""
    sc = _sidecar(tmp_path, {"A195": "B1"})
    with captured_logs() as warned:
        assert bm.patch_from_rfd3(sc, [], "B") == []
    assert not warned


def test_residues_mapping_into_another_chain_are_dropped(tmp_path):
    """Only the target chain's residues are the patch. A two-chain (glue)
    contig maps two input chains into one output chain, so the filter is on
    the DESTINATION letter."""
    sc = _sidecar(tmp_path, {"A10": "B1", "B20": "B2", "A30": "A5"})
    assert bm.patch_from_rfd3(sc, [10, 20, 30], "B") == [1, 2]


def test_a_sidecar_with_no_map_reports_absent_rather_than_guessing(tmp_path):
    """Taking author ids as-is would silently score the wrong residues — the
    same hazard `hotspots_from_rfd3` warns about for an input spec."""
    sc = _sidecar(tmp_path, {})
    with captured_logs() as warned:
        assert bm.patch_from_rfd3(sc, [195], "B") == []
    assert warned and "diffused_index_map" in warned[0]


def test_unmatched_author_ids_contribute_nothing(tmp_path):
    sc = _sidecar(tmp_path, {"A195": "B1"})
    assert bm.patch_from_rfd3(sc, [99991], "B") == []


# ── the columns ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("col", ["n_patch", "patch_contacts",
                                 "patch_contact_fraction", "patch_enrichment"])
def test_the_patch_columns_are_written(col):
    """`binder_metrics.FIELDS` is the contract `binder_ranking` and
    `refold_scores.csv` both read — a column missing here drops out of the
    composite with only a warning."""
    assert col in bm.FIELDS


def test_enrichment_is_normalised_by_the_patch_share_not_raw():
    """The heart of it. Two designs contacting the patch equally often must
    score equally, even when one campaign's patch is four times the size —
    otherwise the column carries the trim's size, not the design's behaviour.

    Checked arithmetically against `score_one`'s own definition, since
    building two real refolds with different patch sizes needs a GPU."""
    # frac / share, where share = |patch| / |accessible|.
    small = (4 / 40) / (10 / 200)      # 10% of contacts on 5% of the target
    large = (16 / 40) / (40 / 200)     # 40% of contacts on 20% of the target
    assert small == pytest.approx(large) == pytest.approx(2.0)


def test_a_design_that_ignores_the_patch_scores_zero_not_one():
    """0.0 rather than None or blank: `binder_ranking` warns about a MISSING
    column and z-scores a zero-variance one to zeros, so zero is what makes a
    no-patch campaign contribute nothing instead of being noisy."""
    import inspect

    src = inspect.getsource(bm.score_one)
    i = src.index('row["n_patch"]')
    block = src[i:i + 900]
    assert 'row["patch_enrichment"] = 0.0' in block


def test_the_patch_is_measured_all_atom_on_the_binder_side():
    """A patch contact asks whether the design PACKS against the patch, which
    is a single-structure question where the sidechains do the packing — the
    same reason `hotspot_engagement` uses the all-atom epitope. Backbone-only
    scored 70% engaging vs 99% all-atom on designs built on their hotspots."""
    import inspect

    src = inspect.getsource(bm.score_one)
    i = src.index('row["n_patch"]')
    block = src[i - 400:i + 900]
    assert "ep_hot" in block, "must use the all-atom epitope"


# ── the weight ─────────────────────────────────────────────────────────────

def test_the_liability_is_weighted_and_negative():
    from src.binder_ranking import DEFAULT_WEIGHTS

    assert DEFAULT_WEIGHTS["neg_patch_enrichment"] == 0.5


def test_it_is_a_tie_breaker_not_a_driver():
    """Phase B found no detectable effect, so the weight must not be able to
    outrank the metrics that ARE measured. Below the lightest geometry term
    and far below the docking term."""
    from src.binder_ranking import DEFAULT_WEIGHTS as W

    assert W["neg_patch_enrichment"] <= W["hotspot_engagement"]
    assert W["neg_patch_enrichment"] < W["neg_binder_rmsd_dock"] / 2


def test_config_and_defaults_agree():
    """The documented pairing: `binder_metrics.FIELDS` <-> `binder_ranking`
    weights <-> `config.yaml design.binder_ranking`."""
    import yaml

    from src.binder_ranking import DEFAULT_WEIGHTS

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    weights = cfg["design"]["binder_ranking"]["weights"]
    assert weights["neg_patch_enrichment"] == (
        DEFAULT_WEIGHTS["neg_patch_enrichment"])
    for col in weights:
        src = col[len("neg_"):] if col.startswith("neg_") else col
        assert src in bm.FIELDS, f"{col} refers to a column nothing writes"


def test_an_all_zero_column_contributes_nothing_to_the_composite():
    """Most campaigns have no patch at all. A zero-variance column must not
    shift anything — `composite_score` guards on std < 1e-12."""
    import inspect

    from src import binder_ranking

    src = inspect.getsource(binder_ranking.composite_score)
    assert "1e-12" in src and "zeros_like" in src


# ── what was deliberately NOT done ─────────────────────────────────────────

def test_the_trim_exposure_refusals_still_raise():
    """Branch 3 said the guards become "a ranking input rather than a
    refusal". Only the first half is implemented, deliberately: Phase B's own
    caveat 3 says it held total contacts FIXED, so it tested "does having
    contacts on the patch cost anything" and never "does exposing a patch cost
    anything" — the question the guards answer. Its caveat 2 is 3KYS rung 90,
    the highest near-epitope dose, where 0 of 40 designs cleared the gates
    against a control's 23 of 60.

    So a scored liability was ADDED and no refusal was removed. If a future
    change wants to remove them it needs the experiment that holds exposure
    itself as the variable, not this one.
    """
    import inspect

    from src import structure_trim

    src = inspect.getsource(structure_trim.trim_target)
    i = src.index("if near:")
    assert "raise TrimError" in src[i:i + 700], (
        "the near-epitope exposure refusal must remain a refusal")
    j = src.index('if verdict == "over_fraction":')
    assert "raise TrimError" in src[j:j + 900], (
        "the away-from-epitope exposure refusal must remain a refusal")
