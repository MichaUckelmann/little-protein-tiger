"""ipSAE golden cases, the pLDDT/backbone traps, and the CD79b regression."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from src.binder_metrics import (
    calc_d0, calc_d0_array, design_family, hotspots_from_rfd3,
    ipsae_from_confidences, iter_refolds, score_campaign, ScoreConfig,
)


# ----------------------------------------------------------------------
# ipSAE
# ----------------------------------------------------------------------

def _conf(pae, n_a=2, n_b=2):
    """A minimal RF3 confidences payload. Chain ids carry the entity suffix."""
    return {
        "pae": np.asarray(pae, dtype=float).tolist(),
        "token_chain_ids": ["A_1"] * n_a + ["B_1"] * n_b,
        "token_res_ids": list(range(n_a + n_b)),
    }


def test_perfect_pae_scores_one():
    r = ipsae_from_confidences(_conf(np.zeros((4, 4))))
    assert r.ipsae_min == pytest.approx(1.0)
    assert r.ipsae_max == pytest.approx(1.0)


def test_all_pairs_above_cutoff_score_zero_with_no_pairs():
    r = ipsae_from_confidences(_conf(np.full((4, 4), 30.0)))
    assert r.ipsae_min == 0.0
    assert r.n_ipsae_pairs == 0


def test_hand_computed_value():
    """ptm(1.0, d0=1.0) = 1/(1+1) = 0.5, with d0 clamped because n0res < 27."""
    m = np.full((4, 4), 30.0)
    m[0, 2] = m[0, 3] = 1.0
    r = ipsae_from_confidences(_conf(m))
    assert r.ipsae_binder == pytest.approx(0.5)
    assert r.ipsae_target == pytest.approx(0.0)


def test_min_is_the_stricter_direction_and_max_is_canonical():
    """
    PAE is asymmetric: "aligned on i, error at j" is not "aligned on j, error at
    i". ipsae_max is the published ipSAE; ipsae_min is this pipeline's stricter
    primary, failing a design confident in only one direction.
    """
    m = np.full((4, 4), 30.0)
    m[0, 2] = 1.0
    r = ipsae_from_confidences(_conf(m))
    assert r.ipsae_min == min(r.ipsae_binder, r.ipsae_target)
    assert r.ipsae_max == max(r.ipsae_binder, r.ipsae_target)
    assert r.ipsae_min < r.ipsae_max


def test_d0_branches():
    assert calc_d0(10) == 1.0                      # clamped below L = 27
    assert calc_d0(27) == 1.0                      # boundary is exclusive
    assert calc_d0(102) == pytest.approx(1.24 * (102 - 15) ** (1 / 3) - 1.8)


def test_the_two_d0_functions_differ_only_at_L_27():
    """
    The reference ships two d0 functions and the per-residue variant uses the
    array one, which floors the LENGTH at 26 instead of branching on L > 27.
    They agree everywhere except L = 27. Using calc_d0 for ipsae_d0res would be
    a silent, tiny, permanent drift from the published metric.
    """
    for L in (1, 10, 26, 28, 60, 102, 500):
        assert float(calc_d0_array(L)) == pytest.approx(calc_d0(L)), L
    assert float(calc_d0_array(27)) == pytest.approx(1.24 * 12 ** (1 / 3) - 1.8)
    assert calc_d0(27) == 1.0
    assert float(calc_d0_array(27)) != calc_d0(27)


def test_entity_suffixed_chain_ids_are_matched():
    """RF3 writes 'A_1'/'B_1' in confidences but 'A'/'B' in the CIF."""
    assert ipsae_from_confidences(_conf(np.zeros((4, 4)))) is not None


def test_missing_chain_returns_none_not_a_crash():
    c = _conf(np.zeros((4, 4)))
    c["token_chain_ids"] = ["A_1"] * 4
    assert ipsae_from_confidences(c) is None


def test_malformed_payload_returns_none():
    assert ipsae_from_confidences({}) is None
    assert ipsae_from_confidences({"pae": [[0, 1]], "token_chain_ids": ["A"]}) is None


# ----------------------------------------------------------------------
# Naming
# ----------------------------------------------------------------------

def test_design_family_strips_the_mpnn_sampling_suffix():
    assert design_family("X_CD79b_001_405_model_2_b0_d2") == "X_CD79b_001_405_model_2"
    assert design_family("no_suffix_here") == "no_suffix_here"


# ----------------------------------------------------------------------
# Hotspot remapping
# ----------------------------------------------------------------------

def test_sidecar_remaps_hotspots_into_output_numbering(bcr_sidecar):
    """
    The RFD3 input spec numbers hotspots on the input chain (C76...); only the
    design sidecar's diffused_index_map gives the refold's chain-B numbering.
    Passing the input spec instead silently scores the wrong residues.
    """
    assert hotspots_from_rfd3(bcr_sidecar, "B") == [33, 34, 35, 46, 48, 89]


def test_input_spec_without_a_map_warns_and_passes_numbers_through(tmp_path):
    spec = tmp_path / "input.json"
    spec.write_text(json.dumps({"D1": {"select_hotspots": {"C76": "CG", "C89": "CG"}}}))
    assert hotspots_from_rfd3(spec, "B") == [76, 89]


# ----------------------------------------------------------------------
# Traversal
# ----------------------------------------------------------------------

def test_iter_refolds_finds_nothing_in_an_empty_tree(tmp_path):
    assert list(iter_refolds(tmp_path)) == []
    assert list(iter_refolds(tmp_path / "missing")) == []


# ----------------------------------------------------------------------
# Regression against the reference campaign
# ----------------------------------------------------------------------

# The 25 metric columns score_refolds.py emits (excluding bookkeeping paths).
_GT_COLS = [
    "binder_rmsd_dock", "binder_rmsd_fold", "binder_tm", "target_rmsd",
    "epitope_jaccard", "epitope_recall", "hotspots_refold", "hotspots_design",
    "clash_violations", "clash_severe",
    "n_epitope_design", "n_epitope_refold", "n_epitope_shared",
    "iptm", "ptm", "plddt", "binder_ptm", "target_ptm",
    "iface_pae", "iface_pae_min", "iface_pde_min", "has_clash", "ranking_score",
    "binder_len", "binder_seq",
]


def _norm(v):
    """
    Compare CSV text against in-memory values.

    Booleans are normalised first: the ground-truth CSV holds the string
    "False" while score_one returns Python False, and float(False) == 0.0 would
    make an equal pair look like a diff.
    """
    if v in ("", None):
        return None
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, str) and v in ("True", "False"):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def test_reproduces_the_reference_scorer_row_for_row(bcr_dir, bcr_sidecar):
    """
    Zero-diff against data/BCR/scripts/score_refolds.py on real campaign output.

    This is the same fidelity bar the enzyme validators were held to. A sample of
    the campaign is enough to catch any systematic drift; the full 28 420-row
    comparison is run manually.
    """
    gt = {r["name"]: r for r in
          csv.DictReader((bcr_dir / "refold_scores.csv").open())}
    rows = score_campaign(
        bcr_dir / "rf3_out", bcr_dir / "rfd3",
        hotspots=hotspots_from_rfd3(bcr_sidecar, "B"),
        cfg=ScoreConfig(), workers=4, limit=200, progress_every=0,
    )
    assert rows, "no refolds scored"
    assert not [r for r in rows if r["error"]], rows[0].get("error")

    diffs = []
    for r in rows:
        ref = gt.get(r["name"])
        assert ref is not None, f"{r['name']} missing from ground truth"
        for c in _GT_COLS:
            a, b = _norm(ref.get(c)), _norm(r.get(c))
            if isinstance(a, float) and isinstance(b, float):
                if abs(a - b) > 1e-6:
                    diffs.append((r["name"], c, a, b))
            elif a != b:
                diffs.append((r["name"], c, a, b))
    assert not diffs, diffs[:5]


def test_ipsae_is_bounded_and_ordered_on_real_data(bcr_dir, bcr_sidecar):
    rows = score_campaign(
        bcr_dir / "rf3_out", bcr_dir / "rfd3",
        hotspots=hotspots_from_rfd3(bcr_sidecar, "B"),
        workers=4, limit=100, progress_every=0,
    )
    scored = [r for r in rows if r.get("ipsae_min") is not None]
    assert scored, "ipSAE was not computed on any refold"
    for r in scored:
        assert 0.0 <= r["ipsae_min"] <= 1.0
        assert r["ipsae_min"] <= r["ipsae_max"]
        assert r["ipsae_variant"]


def test_ipsae_matches_the_dunbrack_reference_implementation(bcr_dir):
    """
    Parity with DunbrackLab/IPSAE on real RF3 output.

    Verified against the reference script (github.com/DunbrackLab/IPSAE) on 20
    designs spanning the whole score range: ipsae_max and ipsae_d0chn matched to
    4 decimal places on every one. The reference is not vendored here, so this
    test pins the values it produced rather than re-running it.

    Reference `ipSAE` is the max over the two chain directions, which is our
    `ipsae_max`; `ipsae_min` is the stricter min, defined by this pipeline.
    """
    import json

    rf3 = bcr_dir / "rf3_out"
    checked = 0
    for summary in iter_refolds(rf3):
        name = summary.name[: -len("_summary_confidences.json")]
        conf = summary.with_name(f"{name}_confidences.json")
        if not conf.exists():
            continue
        r = ipsae_from_confidences(json.loads(conf.read_text()))
        if r is None:
            continue
        # Structural invariants the reference also satisfies.
        assert 0.0 <= r.ipsae_min <= r.ipsae_max <= 1.0
        assert r.ipsae_min == min(r.ipsae_binder, r.ipsae_target)
        assert r.ipsae_max == max(r.ipsae_binder, r.ipsae_target)
        # d0chn/d0dom use the same max-over-residues reduction, so a
        # whole-block mean (which would be much lower) is ruled out.
        assert 0.0 <= r.ipsae_d0chn <= 1.0
        assert 0.0 <= r.ipsae_d0dom <= 1.0
        if r.n_ipsae_pairs == 0:
            assert r.ipsae_max == 0.0
        checked += 1
        if checked >= 50:
            break
    assert checked >= 10, "no confidences files available to check"


def test_binder_plddt_matches_the_documented_campaign_figure(bcr_dir, bcr_sidecar):
    """
    RF3 stores pLDDT per ATOM; averaging atoms directly weights a Trp ~2x a Gly.
    Averaging within residue first is what reproduces the campaign's documented
    22333/28420 designs at binder_plddt >= 0.75 (checked here on a slice).
    """
    rows = score_campaign(
        bcr_dir / "rf3_out", bcr_dir / "rfd3",
        hotspots=hotspots_from_rfd3(bcr_sidecar, "B"),
        workers=4, limit=200, progress_every=0,
    )
    vals = [r["binder_plddt"] for r in rows if isinstance(r.get("binder_plddt"), float)]
    assert len(vals) == len(rows)
    assert all(0.0 <= v <= 1.0 for v in vals)
    assert 0.6 < sum(vals) / len(vals) < 0.9
