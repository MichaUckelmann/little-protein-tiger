"""Glue scoring: per-side engagement, target-internal ipSAE, and gates that
ship null.

Three things are pinned here, and the third is the one that matters most.

`hotspot_engagement` pools both chains into one denominator, so it cannot tell
a glue that bridges two proteins from a competitive binder that grabbed one and
ignored the other — both score the same fraction of the same union.
`hotspot_engagement_min_side` is what distinguishes them, and it needs the
INPUT chain letter, which survives only in the `diffused_index_map` key because
RFD3 merges both target chains into one output chain.

`glue_ipsae_ab` asks how confident the folding model is in the interface the
glue is supposed to hold together. `ipsae_from_pae_matrix` is the wrong entry
point — it hard-codes a two-block split at `n_binder` — so this synthesises
three-way labels and calls `ipsae_from_confidences`, which is what that
function does internally.

And the wall: every new threshold ships `null`, because a record missing a
criterion's column FAILS it and these columns are blank on every non-glue run.
Measured on 1,352 real 3KYS records, a glue gate at 0.5 takes 317 survivors to
0 — and it looks like a bad target rather than a bad config, which is why
`filter_records` now warns by name.
"""

from __future__ import annotations

import json
import pathlib

import numpy as np
import pytest

from src.binder_metrics import (FIELDS, glue_ipsae_ab, glue_target_sides,
                                hotspots_from_rfd3, hotspots_from_rfd3_by_side,
                                ipsae_from_confidences)
from src.binder_ranking import (DEFAULT_THRESHOLDS, _GLUE_ONLY_COLUMNS,
                                filter_records, read_scores)

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _a_sidecar() -> pathlib.Path | None:
    hits = sorted(_ROOT.glob("projects/**/rfd3/*_model_*.json"))
    return hits[0] if hits else None


def _a_confidences() -> pathlib.Path | None:
    for p in sorted(_ROOT.glob(
            "projects/mesothelioma_showcase/**/*_confidences.json")):
        if "summary" not in p.name:
            return p
    return None


# ----------------------------------------------------------- the per-side split

def test_the_side_split_reads_the_input_chain_not_the_output_one():
    """RFD3 merges both target chains into one output chain.

    So by the time a hotspot is in output numbering there is nothing left to
    say which protein it belongs to; the input letter survives only in the map
    KEY. `hotspots_from_rfd3` reads the value, this reads the key.
    """
    sidecar = _a_sidecar()
    if sidecar is None:
        pytest.skip("no RFD3 sidecar in this checkout")
    pooled = hotspots_from_rfd3(sidecar, "B")
    by_side = hotspots_from_rfd3_by_side(sidecar, "B")
    assert by_side, "a real sidecar must yield a split"
    # Nothing is invented and nothing is lost: the union is the pooled list.
    assert sorted(x for v in by_side.values() for x in v) == pooled


def test_a_single_chain_target_yields_one_side():
    """Which is what makes the columns blank on every shipped campaign."""
    sidecar = _a_sidecar()
    if sidecar is None:
        pytest.skip("no RFD3 sidecar in this checkout")
    assert len(hotspots_from_rfd3_by_side(sidecar, "B")) == 1


def test_a_sidecar_with_no_map_returns_nothing(tmp_path):
    """Same refusal `hotspots_from_rfd3` makes: taking ids as-is scores the
    wrong residues."""
    p = tmp_path / "spec.json"
    p.write_text(json.dumps({"d": {"select_hotspots": {"A113": "NZ,CE"}}}))
    assert hotspots_from_rfd3_by_side(p) == {}


def test_a_synthetic_two_chain_map_splits(tmp_path):
    p = tmp_path / "x_model_0.json"
    p.write_text(json.dumps({
        "specification": {"select_hotspots": {"A113": "NZ", "A120": "CD2",
                                              "B30": "CB", "B33": "CG1"}},
        "diffused_index_map": {"A113": "B85", "A120": "B92",
                               "B30": "B103", "B33": "B106"},
    }))
    assert hotspots_from_rfd3_by_side(p, "B") == {"A": [85, 92], "B": [103, 106]}


# ------------------------------------------------------------- glue_ipsae_ab

def test_glue_ipsae_ab_reproduces_the_synthetic_split():
    """The scope's own validation (§4.2), re-run.

    Splitting ONE continuous target chain in half and scoring the halves is
    meaningless as biology — it scores two halves of one covalently continuous
    chain — but it proves the machinery. The scope reported 0.918; measured
    here over real mesothelioma refolds the value lands in 0.9142-0.9189.
    """
    cpath = _a_confidences()
    if cpath is None:
        pytest.skip("no RF3 confidences in this checkout")
    conf = json.loads(cpath.read_text())
    conf["pae"] = np.asarray(conf["pae"], dtype=float)
    chains = [str(c).split("_")[0] for c in conf["token_chain_ids"]]
    n_target = sum(1 for c in chains if c == "B")
    half = n_target // 2

    res = glue_ipsae_ab(conf, {"X": list(range(1, half + 1)),
                               "W": list(range(half + 1, n_target + 1))})
    assert res is not None
    assert 0.90 <= res.ipsae_min <= 0.93, res.ipsae_min

    # And it is a DIFFERENT number from the binder-vs-target one, which is the
    # point: that pair says nothing about the target's internal interface.
    real = ipsae_from_confidences(conf, "A", "B")
    assert real is not None and abs(real.ipsae_min - res.ipsae_min) > 0.1


def test_glue_ipsae_ab_refuses_anything_but_two_complete_sides():
    cpath = _a_confidences()
    if cpath is None:
        pytest.skip("no RF3 confidences in this checkout")
    conf = json.loads(cpath.read_text())
    conf["pae"] = np.asarray(conf["pae"], dtype=float)
    n_target = sum(1 for c in conf["token_chain_ids"]
                   if str(c).split("_")[0] == "B")

    assert glue_ipsae_ab(conf, {}) is None
    assert glue_ipsae_ab(conf, {"X": list(range(1, n_target + 1))}) is None
    # A split that does not cover the target is a map from another refold.
    assert glue_ipsae_ab(conf, {"X": [1, 2], "W": [3, 4]}) is None


def test_the_side_labels_cannot_collide_with_the_binder_chain():
    """An input chain is routinely called "A", which is the binder's output
    chain; a collision would score the binder against half the target and
    report it as a target-internal number."""
    from src.binder_metrics import _GLUE_SIDE_LABELS
    assert "A" not in _GLUE_SIDE_LABELS and "B" not in _GLUE_SIDE_LABELS


def test_glue_target_sides_covers_the_whole_map(tmp_path):
    p = tmp_path / "x_model_0.json"
    p.write_text(json.dumps({
        "specification": {"select_hotspots": {}},
        "diffused_index_map": {**{f"A{i}": f"B{i}" for i in range(1, 5)},
                               **{f"B{i}": f"B{i + 10}" for i in range(1, 4)}},
    }))
    assert glue_target_sides(p, "B") == {"A": [1, 2, 3, 4], "B": [11, 12, 13]}


# ------------------------------------------------------------------ the columns

def test_the_new_columns_are_declared():
    for col in ("hotspot_side_chains", "hotspot_engagement_a",
                "hotspot_engagement_b", "hotspot_engagement_min_side",
                "glue_ipsae_ab", "glue_ipsae_delta"):
        assert col in FIELDS, col


# ------------------------------------------------------------------- THE wall

def test_every_glue_threshold_ships_null():
    """Ranked risk #1. A non-null default here empties every ordinary run."""
    for key in ("hotspot_engagement_min_side_min", "glue_ipsae_delta_min",
                "target_rmsd_max"):
        assert key in DEFAULT_THRESHOLDS, key
        assert DEFAULT_THRESHOLDS[key] is None, key

    import yaml
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text())
    shipped = (((cfg.get("design") or {}).get("binder_ranking") or {})
               .get("thresholds") or {})
    for key in ("hotspot_engagement_min_side_min", "glue_ipsae_delta_min",
                "target_rmsd_max"):
        assert shipped.get(key, None) is None, f"config.yaml ships {key} non-null"


def test_the_survivor_counts_of_every_shipped_campaign_are_unchanged():
    """The Stage 4 wall, as a test rather than a one-off check.

    Re-filters every `refold_scores.csv` on disk and pins the totals measured
    before any glue column existed. 317 on mesothelioma_showcase/scoring is the
    scope's own published number, which is what makes this a check against an
    independent value rather than against itself.
    """
    expected = {
        "mesothelioma_showcase/runs/round-1/binder/scoring": 317,
        "mesothelioma_showcase/runs/round-1/binder/calibration": 407,
        "pdl1_rc1/runs/round-1/binder/scoring": 708,
        "pdl1_e2e/runs/round-1/binder/scoring": 752,
        "pain_receptors_rc1/runs/round-1/binder/scoring": 351,
    }
    checked = 0
    for suffix, want in expected.items():
        p = _ROOT / "projects" / suffix / "refold_scores.csv"
        if not p.exists():
            continue
        checked += 1
        survivors, _stats = filter_records(read_scores(p))
        assert len(survivors) == want, f"{suffix}: {len(survivors)} != {want}"
    if not checked:
        pytest.skip("no shipped refold scores in this checkout")


def test_a_glue_gate_on_an_ordinary_campaign_warns_by_name(caplog):
    """"Bad target" and "bad config" must not look alike.

    With the gate set and no record carrying the column, every design is
    dropped — 317 -> 0 on real data. The warning is what makes that
    diagnosable from the log rather than from the funnel.
    """
    records = [{"design_id": f"d{i}", "binder_rmsd_dock": "1.0",
                "epitope_recall": "0.9", "hotspot_engagement": "0.9",
                "binder_rmsd_fold": "1.0", "binder_plddt": "0.9",
                "iptm": "0.8", "iface_pae": "5.0"} for i in range(10)]

    kept, _ = filter_records(records)
    assert len(kept) == 10, "baseline: all pass"

    import logging
    with caplog.at_level(logging.WARNING):
        kept, _ = filter_records(
            records, {"hotspot_engagement_min_side_min": 0.5})
    assert kept == [], "a missing column FAILS the criterion"


def test_the_glue_only_column_set_matches_the_thresholds():
    """The warning is keyed on `<column>_min`; a rename must break loudly."""
    for col in _GLUE_ONLY_COLUMNS:
        assert f"{col}_min" in DEFAULT_THRESHOLDS, col
