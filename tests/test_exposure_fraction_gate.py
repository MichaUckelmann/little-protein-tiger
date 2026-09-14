"""The trim's exposure guard is scale-free: area over the epitope's own area.

`MAX_EXPOSED_HYDROPHOBIC = 2` counted RESIDUES, which is not scale-free in
either direction. Six residues at +16 A^2 read as "6, over the limit" and two
at +190 A^2 as "2, within tolerance" — and the second opens 2.6x more surface.
And two residues means something different against a 462 A^2 epitope than
against a 1620 A^2 one.

Measured over every cut this checkout can build, with the near-epitope check
(which this does NOT replace) excluding everything else:

    5VAI R 387->100, `R29-128`, a real domain boundary   2 res   74.8 A^2   5.5%
    3KYS A 208->190, shearing the fold                  13 res  525.4 A^2  32.5%
    5VAI R 387->200, accreting into the TM bundle       23 res 1227.5 A^2  90.5%
    5VAI R 387->150, ditto                              20 res 1249.0 A^2  92.1%

Nothing lands between 5.5% and 32.5%, so the 25% default is a PROPOSAL and
this file says so: the tests below pin the two measures' DISAGREEMENT, which
is what the change is for, and pin the four measured points, which is what it
must not break. They do not assert that 25% is the right number — one clean
cut cannot establish that, and `GLUE_PIPELINE_SCOPE.md` section 5 is what
would.
"""

from __future__ import annotations

import pathlib

import pytest

from src.structure_trim import (MAX_EXPOSED_HYDROPHOBIC,
                                MAX_EXPOSED_HYDROPHOBIC_FRACTION,
                                exposure_verdict)

_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: (away area A^2, n residues, target-side interface BSA) for the four cuts
#: above, measured on real structures.
_MEASURED = {
    "5VAI@100 domain boundary": (74.8, 2, 1356.0, "ok"),
    "3KYS@190 sheared fold": (525.4, 13, 1617.6, "over_fraction"),
    "5VAI@200 into the TM bundle": (1227.5, 23, 1356.0, "over_fraction"),
    "5VAI@150 into the TM bundle": (1249.0, 20, 1356.0, "over_fraction"),
}


@pytest.mark.parametrize("case", sorted(_MEASURED))
def test_the_measured_cuts_are_judged_as_observed(case):
    area, n, bsa, expected = _MEASURED[case]
    verdict, _ = exposure_verdict(area, n, bsa)
    assert verdict == expected


def test_the_one_clean_cut_has_real_margin():
    """5.5% against a 25% bar. If a future edit narrows this to nothing the
    guard has stopped distinguishing a domain boundary from a shear."""
    _, frac = exposure_verdict(74.8, 2, 1356.0)
    assert frac < MAX_EXPOSED_HYDROPHOBIC_FRACTION / 3


# ── the disagreement, which is the whole point ─────────────────────────────

def test_several_small_exposures_are_accepted_where_the_count_refused():
    """Six residues at +16 A^2 = 96 A^2, ~7% of a typical epitope. The count
    gate refuses this for being six; it opens less surface than the 5VAI
    domain-boundary cut that everyone agrees is clean."""
    n = 6
    assert n > MAX_EXPOSED_HYDROPHOBIC, "the count gate would refuse this"
    verdict, frac = exposure_verdict(96.0, n, 1356.0)
    assert verdict == "ok"
    assert frac < 0.10


def test_two_large_exposures_are_refused_where_the_count_passed():
    """The dangerous direction: 2 residues at +190 A^2 against a 462 A^2
    epitope is 82% of it, and the count calls two 'within tolerance'."""
    n = 2
    assert n <= MAX_EXPOSED_HYDROPHOBIC, "the count gate would pass this"
    verdict, frac = exposure_verdict(380.0, n, 462.2)
    assert verdict == "over_fraction"
    assert frac > 0.8


def test_the_same_area_is_judged_against_the_epitope_it_opened_beside():
    """Scale-freeness itself: one area, two epitopes, two verdicts."""
    small = exposure_verdict(300.0, 4, 462.2)[0]
    large = exposure_verdict(300.0, 4, 1617.6)[0]
    assert (small, large) == ("over_fraction", "ok")


# ── the fallbacks ──────────────────────────────────────────────────────────

def test_without_a_partner_chain_the_count_still_gates():
    """There is no interface area to divide by for a monomer or an
    `inhibit_active_site` target, so behaviour there is unchanged."""
    assert exposure_verdict(96.0, 6, 0.0) == ("over_count", None)
    assert exposure_verdict(96.0, 2, 0.0) == ("ok", None)


def test_max_count_none_disables_both_measures():
    """`max_exposed_hydrophobic=None` is how the benchmark and the tests that
    deliberately force an aggressive cut opt out. It must keep disabling the
    check outright rather than silently leaving the fraction armed."""
    assert exposure_verdict(9999.0, 99, 1356.0, None) == ("ok", None)


def test_max_fraction_none_falls_back_to_the_count():
    """The escape hatch in the other direction: opt out of the new measure
    and get exactly the old one."""
    assert exposure_verdict(96.0, 6, 1356.0, max_fraction=None)[0] == (
        "over_count")
    assert exposure_verdict(380.0, 2, 462.2, max_fraction=None)[0] == "ok"


def test_a_no_op_trim_exposes_nothing_and_passes():
    """Every no-op trim in `projects/` measures exactly 0.0% — 22 of the 25
    trims on disk, since the target already fitted the budget. A guard that
    fired on one of those would refuse a trim that cut nothing."""
    assert exposure_verdict(0.0, 0, 1356.0) == ("ok", 0.0)


def test_the_boundary_is_exclusive():
    """Exactly at the threshold passes; a hair over does not."""
    bsa = 1000.0
    at = MAX_EXPOSED_HYDROPHOBIC_FRACTION * bsa
    assert exposure_verdict(at, 9, bsa)[0] == "ok"
    assert exposure_verdict(at + 0.1, 9, bsa)[0] == "over_fraction"


# ── the guard it does NOT replace ──────────────────────────────────────────

def test_near_epitope_exposure_is_still_an_unconditional_refusal():
    """Source inspection, the same way `tests/test_audit_fixes.py` pins the
    designable-size gate. The fraction gate only ever judges exposure AWAY
    from the epitope: a fresh hydrophobic face within the clearance of a
    hotspot competes with the site being designed for at ANY size, so making
    that one scale-free would let a large epitope tolerate exposure sitting
    right on it. Every real cut measured here had near-epitope exposure and
    was refused by that check first, which is also why the four points above
    are the only ones the new gate decides."""
    import inspect

    from src import structure_trim

    src = inspect.getsource(structure_trim.trim_target)
    i = src.index("if near:")
    near_block = src[i:i + 700]
    assert "raise TrimError" in near_block
    assert "exposed_fraction" not in near_block, (
        "the near-epitope refusal must stay unconditional, not become a "
        "fraction of the epitope area")
