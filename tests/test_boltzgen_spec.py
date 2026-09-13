"""`src.boltzgen_spec` — the three refusals, and the one numbering.

Every assertion here corresponds to a failure that was observed on a real
BoltzGen run rather than imagined, because each one is silent: the campaign
completes, costs GPU hours, and answers a different question than the one asked.

* an equal-length binder aborts the run two steps later, naming neither cause
* a binding index outside `res_index` is ignored with no error at all
* a consistent-but-wrong `label_seq` selects the wrong residues

Synthetic tests always run. Real-data tests need `data/structures/3N7S_ba1.cif`
and are skipped without it, following `tests/test_ppi_report.py`'s convention.
"""

from __future__ import annotations

import pathlib

import pytest

from src.boltzgen_spec import (
    BoltzGenSpec, DEFAULT_SIZES, PROTOCOL_BY_MODALITY, SpecError,
    build_boltzgen_spec, kept_segments_to_res_index, safe_binder_range,
    validate_spec,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_3N7S = _ROOT / "data" / "structures" / "3N7S_ba1.cif"

#: RAMP1's epitope on 3N7S chain D, with the label_seq values verified against
#: the file by `boltzgen_residue_indices` AND by residue name.
_RAMP1_HOTSPOTS = [
    {"residue": "TRP", "auth_seq_id": 74}, {"residue": "PHE", "auth_seq_id": 83},
    {"residue": "TRP", "auth_seq_id": 84}, {"residue": "PRO", "auth_seq_id": 85},
    {"residue": "ASP", "auth_seq_id": 90},
]
_RAMP1_LABELS = (53, 62, 63, 64, 69)


def _spec(**kw) -> BoltzGenSpec:
    base = dict(path=pathlib.Path("/tmp/x.yaml"), name="x",
                modality="cyclic_peptide", protocol="peptide-anything",
                structure_path=pathlib.Path("/tmp/t.cif"), target_chain="A",
                binder_chain="B", binding=(10, 20), res_index=None,
                binder_min=12, binder_max=15)
    base.update(kw)
    return BoltzGenSpec(**base)


# ── the equal-length crash ───────────────────────────────────────────────────

def test_a_target_outside_the_window_needs_no_adjustment():
    lo, hi, note = safe_binder_range("cyclic_peptide", 84)
    assert (lo, hi) == DEFAULT_SIZES["cyclic_peptide"] and note == ""


@pytest.mark.parametrize("target_len", range(70, 87))
def test_the_window_never_contains_the_target_length(target_len):
    """The whole point: whatever the target size, the fatal length is excluded.

    Measured on RAMP1 (84 residues) with a 70..86 window: 2 of 24 designs
    sampled 84 and both killed the run at the `folding` step.
    """
    lo, hi, note = safe_binder_range("mini_protein", target_len)
    assert not lo <= target_len <= hi
    assert note, "a narrowed window must say why"
    assert lo <= hi


def test_narrowing_keeps_the_larger_remainder():
    # 84 in 70..86: dropping the top loses 3 lengths, the bottom loses 15.
    assert safe_binder_range("mini_protein", 84)[:2] == (70, 83)
    # 71: dropping the bottom loses 2, the top loses 16.
    assert safe_binder_range("mini_protein", 71)[:2] == (72, 86)


def test_a_window_that_cannot_exclude_the_length_is_refused():
    with pytest.raises(SpecError, match="leaves nothing"):
        safe_binder_range("cyclic_peptide", 13, sizes={"cyclic_peptide": (13, 13)})


# ── binding outside res_index is silently ignored by BoltzGen ───────────────

def test_binding_outside_the_trim_is_refused_not_shipped():
    """`boltzgen check` ACCEPTS `binding: 1` with `res_index: 53..69` and simply
    marks nothing — a campaign with no binding constraint and no error."""
    with pytest.raises(SpecError, match="outside res_index"):
        validate_spec(_spec(binding=(1, 55), res_index=((53, 69),)))


def test_binding_inside_the_trim_passes():
    validate_spec(_spec(binding=(55, 60), res_index=((53, 69),)))


def test_a_binder_chain_colliding_with_the_target_is_refused():
    with pytest.raises(SpecError, match="collides"):
        validate_spec(_spec(target_chain="B", binder_chain="B"))


def test_validate_catches_an_equal_length_window_against_the_trimmed_length():
    """The guard again, at the other end: `res_index` decides the designable
    length, so a window chosen against the FULL chain can still be fatal."""
    with pytest.raises(SpecError, match="designable target length"):
        # res_index keeps 14 residues; a 12..15 window contains 14.
        validate_spec(_spec(binding=(55,), res_index=((53, 66),)))


def test_one_based_indices_are_enforced():
    with pytest.raises(SpecError, match="1-based"):
        validate_spec(_spec(binding=(0, 5)))


def test_an_empty_binding_list_is_refused():
    with pytest.raises(SpecError, match="no binding"):
        validate_spec(_spec(binding=()))


# ── modality plumbing ───────────────────────────────────────────────────────

# `test_protocols_match_the_pipeline_runners_table` lived here. It pinned
# `PROTOCOL_BY_MODALITY` against `PipelineRunner._MODALITY_TO_PROTOCOL`; that
# second table went with the retired legacy execution stage, so there is
# nothing left to cross-check. The lesson it carried — a drift between two
# copies of this mapping silently sends a macrocycle through
# `protein-anything`, which skips the cyclic constraint — is recorded at
# `PROTOCOL_BY_MODALITY`'s own definition. See LEGACY_RETIREMENT_SCOPE.md.


def test_sizes_match_the_shipped_config():
    import yaml

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    shipped = cfg["design"]["constraints"]["binder_sizes"]
    for modality, (lo, hi) in DEFAULT_SIZES.items():
        assert (shipped[modality]["min"], shipped[modality]["max"]) == (lo, hi)


# ── real structure: the numbering ───────────────────────────────────────────

@pytest.mark.skipif(not _3N7S.exists(), reason="3N7S_ba1.cif not in this checkout")
def test_binding_indices_are_the_deposited_label_seq():
    """Verified against `boltzgen check`: with these values its visualisation
    CIF marks exactly TRP53 / PHE62 / TRP63 / PRO64 / ASP69."""
    spec = build_boltzgen_spec(
        name="t", structure_path=_3N7S, target_chain="D",
        hotspots=_RAMP1_HOTSPOTS, out_path=pathlib.Path("/tmp/lpt_t.yaml"),
        modality="cyclic_peptide")
    assert spec.binding == _RAMP1_LABELS


@pytest.mark.skipif(not _3N7S.exists(), reason="3N7S_ba1.cif not in this checkout")
def test_a_wrong_residue_name_is_a_hard_failure():
    """The `projects/il7ra_e2e` shape: a numbering off by a constant, which is
    internally consistent and selects the wrong residues."""
    with pytest.raises(SpecError, match="grounding failed"):
        build_boltzgen_spec(
            name="t", structure_path=_3N7S, target_chain="D",
            hotspots=[{"residue": "VAL", "auth_seq_id": 74}],
            out_path=pathlib.Path("/tmp/lpt_t2.yaml"), modality="cyclic_peptide")


@pytest.mark.skipif(not _3N7S.exists(), reason="3N7S_ba1.cif not in this checkout")
def test_author_trim_spans_translate_to_label_spans():
    """An author range is not the same range in label space — it has to go
    through the file's own map, not an offset."""
    spans = kept_segments_to_res_index(_3N7S, "D", [[74, 110]])
    assert spans == ((53, 89),)
    covered = set(range(spans[0][0], spans[0][1] + 1))
    assert set(_RAMP1_LABELS) <= covered


@pytest.mark.skipif(not _3N7S.exists(), reason="3N7S_ba1.cif not in this checkout")
def test_a_trim_that_drops_the_epitope_is_refused():
    with pytest.raises(SpecError, match="outside res_index"):
        build_boltzgen_spec(
            name="t", structure_path=_3N7S, target_chain="D",
            hotspots=_RAMP1_HOTSPOTS,
            out_path=pathlib.Path("/tmp/lpt_t3.yaml"),
            modality="cyclic_peptide", kept_segments=[[95, 110]])


@pytest.mark.skipif(not _3N7S.exists(), reason="3N7S_ba1.cif not in this checkout")
def test_the_binder_chain_avoids_letters_the_target_file_uses():
    """3N7S has chains A and D, so B is free and chosen."""
    spec = build_boltzgen_spec(
        name="t", structure_path=_3N7S, target_chain="D",
        hotspots=_RAMP1_HOTSPOTS, out_path=pathlib.Path("/tmp/lpt_t4.yaml"),
        modality="cyclic_peptide")
    assert spec.binder_chain == "B"
    assert spec.binder_chain != spec.target_chain
