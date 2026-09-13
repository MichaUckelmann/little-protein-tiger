"""Domain segmentation and the trimming refinement pipeline."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.structure_trim import (
    BRIDGE_GAP, MIN_SEGMENT, Domain, TrimBudgetError, TrimError,
    _bridge_gaps, _drop_islands, _hotspot_centred_crop, _nearest_observed,
    _prefer_contiguous, _segments, build_contig, chain_residues,
    geometric_domains, load_mapping, plan_trim, trim_target,
)

_ROOT = Path(__file__).resolve().parents[1]
_3KYS = _ROOT / "data" / "structures" / "3KYS_ba1.cif"


# ----------------------------------------------------------------------
# Span arithmetic
# ----------------------------------------------------------------------

def test_segments_collapses_runs():
    assert _segments([1, 2, 3, 7, 8, 20]) == [(1, 3), (7, 8), (20, 20)]
    assert _segments([]) == []


def test_segments_deduplicates():
    """Guards the degenerate `C92-92,C92-92` contigs a duplicated id produced."""
    assert _segments([5, 5, 6, 6, 7]) == [(5, 7)]


def test_build_contig_shape():
    assert build_contig([(42, 145)], "B", 68, 86) == "68-86,/0,B42-145"
    assert build_contig([(1, 10), (20, 30)], "A", 70, 86) == "70-86,/0,A1-10,A20-30"


def test_nearest_observed_clamps_into_the_observed_range():
    """
    Domain ranges are annotated on the full entity sequence and routinely run
    past the modelled residues. Dropping such a span discards the annotation and
    silently falls through to a worse segmentation tier.
    """
    l2a = {i: 100 + i for i in range(1, 51)}
    assert _nearest_observed(1, l2a, +1) == 101
    assert _nearest_observed(219, l2a, -1) == 150      # past the end -> clamped
    assert _nearest_observed(-5, l2a, +1) == 101       # before the start
    assert _nearest_observed(10, {}, +1) is None


# ----------------------------------------------------------------------
# Refinement steps
# ----------------------------------------------------------------------

def _res(auths):
    return [{"auth": a, "icode": "", "name": "ALA",
             "ca": np.array([float(a), 0.0, 0.0])} for a in auths]


def test_bridge_gaps_fills_short_holes_only():
    by = {r["auth"]: r for r in _res(range(1, 101))}
    kept = _bridge_gaps([1, 2, 3, 10, 11], by, budget=100)
    assert kept == list(range(1, 12))
    far = _bridge_gaps([1, 2, 3, 90, 91], by, budget=100)
    assert 50 not in far


def test_bridge_gaps_respects_the_budget():
    by = {r["auth"]: r for r in _res(range(1, 101))}
    assert _bridge_gaps([1, 2, 3, 10, 11], by, budget=5) == [1, 2, 3, 10, 11]


def test_islands_are_dropped_but_never_a_hotspot():
    warn = []
    kept = _drop_islands(list(range(1, 21)) + [50, 80], hot=[80], warnings=warn)
    assert 50 not in kept          # bare island
    assert 80 in kept              # island holding a hotspot
    assert warn


def test_prefer_contiguous_collapses_fragments_around_the_hotspots():
    residues = _res(range(1, 201))
    frag = [5, 6, 7] + list(range(100, 141))
    warn = []
    out = _prefer_contiguous(frag, residues, hot=[120], budget=60, warnings=warn)
    assert _segments(out) == [(out[0], out[-1])], "should be one span"
    assert 120 in out
    assert warn


def test_prefer_contiguous_keeps_fragments_rather_than_lose_a_hotspot():
    """A tidier contig is never worth dropping a hotspot."""
    residues = _res(range(1, 201))
    frag = [5, 6, 7] + list(range(180, 191))
    warn = []
    out = _prefer_contiguous(frag, residues, hot=[6, 185], budget=30, warnings=warn)
    assert 6 in out and 185 in out
    assert len(_segments(out)) > 1
    assert any("fragmented" in w for w in warn)


def test_hotspot_centred_crop_ranks_by_3d_not_sequence():
    """
    A membrane-proximal tail is sequence-adjacent to the epitope but far from it
    in space; ranking by distance is what lets the crop drop it.
    """
    residues = _res(range(1, 51))
    for r in residues[40:]:
        r["ca"] = np.array([0.0, 500.0, 0.0])     # a distant tail
    out = _hotspot_centred_crop(residues, hot=[5], budget=20)
    assert all(a <= 40 for a in out)
    assert 5 in out


# ----------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------

def test_hotspot_bearing_domains_over_budget_raise_rather_than_drop_one():
    residues = _res(range(1, 301))
    domains = [Domain(0, 1, 300, 300, "cath"), Domain(1, 301, 400, 100, "cath")]
    with pytest.raises(TrimBudgetError, match="over the"):
        plan_trim(residues, domains,
                  [{"auth_seq_id": 10}, {"auth_seq_id": 290}], budget=50)


def test_a_single_unsplit_domain_falls_back_to_a_crop_instead_of_erroring():
    """
    When segmentation finds no boundary, "the domain" is the whole chain and a
    budget error would be misleading — crop around the epitope instead.
    """
    residues = _res(range(1, 301))
    kept, warnings = plan_trim(residues, [Domain(0, 1, 300, 300, "geometric")],
                               [{"auth_seq_id": 150}], budget=100)
    assert 150 in kept
    assert any("no domain boundary" in w for w in warnings)


def test_geometric_partition_returns_whole_chain_when_it_already_fits():
    doms = geometric_domains(_res(range(1, 101)), budget=200)
    assert len(doms) == 1 and doms[0].n_residues == 100


# ----------------------------------------------------------------------
# End-to-end against real structures
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def tead_hotspots():
    return [{"residue": "LEU", "auth_seq_id": a, "rfd3_atoms": "CD1,CD2,CG"}
            for a in (391, 395, 398)]


@pytest.mark.skipif(not _3KYS.exists(), reason="3KYS not downloaded")
def test_trims_tead1_keeping_every_hotspot(tmp_path, tead_hotspots):
    # budget=150 on a ~208-residue chain forces a cut, which is the point —
    # these pin the trim MECHANICS. That cut does expose hydrophobic core near
    # the epitope, so the exposure guard is relaxed here; the guard's own
    # behaviour is pinned separately below.
    res = trim_target(_3KYS, target_chain="A", partner_chain="B",
                      hotspots=tead_hotspots, budget=150, out_dir=tmp_path,
                      pdb_id="3KYS", binder_min=70, binder_max=86,
                      max_exposed_hydrophobic=None)
    assert res.n_residues_after <= 150
    assert res.n_residues_after < res.n_residues_before
    assert len(res.hotspots_retained) == 3 and not res.hotspots_lost
    assert res.trimmed_path.exists() and res.mapping_path.exists()
    assert (tmp_path / "trimmed.pdb").exists()


@pytest.mark.skipif(not _3KYS.exists(), reason="3KYS not downloaded")
@pytest.mark.network
def test_rcsb_annotation_is_used_when_available(tmp_path, tead_hotspots):
    """3KYS chain A carries a CATH assignment; the geometric tier is the fallback."""
    res = trim_target(_3KYS, target_chain="A", partner_chain="B",
                      hotspots=tead_hotspots, budget=150, out_dir=tmp_path,
                      pdb_id="3KYS", max_exposed_hydrophobic=None)
    assert res.method in ("cath", "scop", "scop2", "ecod"), res.method


@pytest.mark.skipif(not _3KYS.exists(), reason="3KYS not downloaded")
def test_author_numbering_is_preserved_so_hotspot_ids_stay_valid(tmp_path,
                                                                 tead_hotspots):
    """
    The RFD3 spec addresses hotspots by author id. If the trim renumbered, every
    select_hotspots key would silently point at a different residue.
    """
    res = trim_target(_3KYS, target_chain="A", partner_chain="B",
                      hotspots=tead_hotspots, budget=150, out_dir=tmp_path,
                      pdb_id="3KYS", max_exposed_hydrophobic=None)
    mapping = load_mapping(res.mapping_path)
    assert mapping["identity_numbering"] is True
    assert mapping["auth_in_to_auth_out"] == {}
    kept = {r["auth"] for r in chain_residues(res.trimmed_path, "A")}
    for h in tead_hotspots:
        assert h["auth_seq_id"] in kept
    assert all(h["retained"] for h in mapping["hotspots"])


@pytest.mark.skipif(not _3KYS.exists(), reason="3KYS not downloaded")
def test_losing_a_hotspot_is_a_hard_failure(tmp_path):
    """A hotspot outside the structure must fail loudly, not be dropped."""
    with pytest.raises(TrimError, match="not present"):
        trim_target(_3KYS, target_chain="A", partner_chain="B",
                    hotspots=[{"residue": "LEU", "auth_seq_id": 9999}],
                    budget=150, out_dir=tmp_path, pdb_id="3KYS",
                    max_exposed_hydrophobic=None)


@pytest.mark.skipif(not _3KYS.exists(), reason="3KYS not downloaded")
def test_contig_matches_the_kept_segments(tmp_path, tead_hotspots):
    res = trim_target(_3KYS, target_chain="A", partner_chain="B",
                      hotspots=tead_hotspots, budget=150, out_dir=tmp_path,
                      pdb_id="3KYS", binder_min=70, binder_max=86,
                      max_exposed_hydrophobic=None)
    binder, _, spans = res.contig.partition(",/0,")
    assert binder == "70-86"
    assert spans == ",".join(f"A{lo}-{hi}" for lo, hi in res.kept_segments)


_7XQ8 = _ROOT / "data" / "structures" / "7XQ8_ba1.cif"


@pytest.mark.skipif(not _7XQ8.exists(), reason="7XQ8 not downloaded")
def test_reproduces_the_hand_made_cd79_extracellular_trim(tmp_path):
    """
    Ground truth: `data/BCR/inputs/CD79_A_B_extr.pdb` was trimmed by hand to the
    extracellular Ig domains, chain C 44-145. Trimming CD79B here on domain
    boundaries must land in the same place.
    """
    hotspots = [{"residue": r, "auth_seq_id": a, "rfd3_atoms": x} for r, a, x in
                [("TRP", 76, "CG,CD1,NE1"), ("ILE", 77, "CG1,CD1"),
                 ("TRP", 78, "CD2,CE3,CZ3"), ("LEU", 89, "CG,CD1"),
                 ("LEU", 91, "CD1,CD2,CG"), ("VAL", 132, "CG1,CG2")]]
    res = trim_target(_7XQ8, target_chain="B", partner_chain="A",
                      hotspots=hotspots, budget=110, out_dir=tmp_path,
                      pdb_id="7XQ8", binder_min=68, binder_max=86)
    assert res.n_segments == 1
    (lo, hi), = res.kept_segments
    assert hi == 145, f"C-terminal boundary should be the TM junction, got {hi}"
    assert abs(lo - 44) <= 4, f"N-terminal boundary {lo} is far from the hand trim's 44"
    assert 100 <= res.n_residues_after <= 110
    assert len(res.hotspots_retained) == 6


@pytest.mark.skipif(not _7XQ8.exists(), reason="7XQ8 not downloaded")
def test_dropping_a_second_native_interface_warns_but_does_not_fail(tmp_path):
    """
    CD79A/CD79B bury ~80% of their interface through the TRANSMEMBRANE helices.
    An extracellular-domain campaign discards that on purpose, so retention must
    be measured over the residues kept — not over the whole native interface,
    which would block every legitimate multi-interface trim.
    """
    hotspots = [{"residue": "X", "auth_seq_id": a, "rfd3_atoms": "CB"}
                for a in (76, 77, 78, 89, 91, 132)]
    res = trim_target(_7XQ8, target_chain="B", partner_chain="A",
                      hotspots=hotspots, budget=110, out_dir=tmp_path,
                      pdb_id="7XQ8")
    assert res.bsa_retention >= 0.9
    # Compare against the TARGET-SIDE total, not the both-chain one:
    # bsa_dropped_A2 counts only the trimmed chain's buried area, so measuring
    # it against the whole interface is the apples-to-oranges comparison that
    # made this warning fire on every trim, including no-ops.
    assert res.bsa_dropped_A2 > 0.5 * res.interface_bsa_target_side_A2
    assert any("more than one interface" in w for w in res.warnings)


@pytest.mark.skipif(not _7XQ8.exists(), reason="7XQ8 not downloaded")
def test_insertion_codes_are_refused_not_silently_mis_numbered(tmp_path):
    """
    7XQ8 chain C is a Kabat-numbered antibody chain: author ids repeat. An RFD3
    contig cannot address those residues, and emitting `C92-92,C92-92` produces a
    spec RFD3 accepts and silently mis-models.
    """
    with pytest.raises(TrimError, match="insertion codes"):
        trim_target(_7XQ8, target_chain="C", partner_chain="L",
                    hotspots=[{"residue": "TYR", "auth_seq_id": 150}],
                    budget=220, out_dir=tmp_path, pdb_id="7XQ8")


# --- solvent hygiene -------------------------------------------------------
# Ordered waters carry their target chain's id and their own numbering, so they
# reached the per-residue interface accounting but could never be in the trim's
# kept_set: every interface water was counted as a residue the trim had removed.
# On 7CZD that was 20 waters worth 435 A^2 — 35% of the target-side total, which
# tripped the 5% threshold and opened a trim_gate checkpoint on a trim that
# removed nothing at all (117 residues in, 117 out).

from src.structure_tools import is_solvent_or_additive  # noqa: E402


@pytest.mark.parametrize("name", ["HOH", "DOD", "EDO", "GOL", "MPD", "DMS", "PEG"])
def test_water_and_cryoprotectant_are_strippable(name):
    assert is_solvent_or_additive(name)


@pytest.mark.parametrize("name", [
    "ALA", "GLY", "TRP",              # ordinary residues
    "MSE", "SEP", "TPO", "PTR",       # modified residues a depositor may use
    "PCA", "CSO", "UNK",              # and the odd ones
])
def test_the_polypeptide_is_never_strippable(name):
    """The whole safety property: this must not be able to cut the chain."""
    assert not is_solvent_or_additive(name)


@pytest.mark.parametrize("name", [
    "GTP", "GMPPNP", "ATP",   # functional ligands — KRAS campaigns need these
    "ZN", "MG", "CA", "FE",   # metals, frequently structural
    "SO4", "PO4",             # ions that sit in phosphate-binding sites
    "NAG", "BMA",             # sugars, which may be a real glycan
    "DA", "DT", "A", "U",     # nucleic acid
])
def test_anything_possibly_functional_is_kept(name):
    """The list is a denylist on purpose — an unrecognised ligand survives."""
    assert not is_solvent_or_additive(name)


def test_case_and_whitespace_do_not_defeat_it():
    assert is_solvent_or_additive(" hoh ")
    assert not is_solvent_or_additive("")
    assert not is_solvent_or_additive(None)  # type: ignore[arg-type]


# --- the backbone rule ------------------------------------------------------
# Membership of the chain is decided by GEOMETRY, not by name. gemmi's
# chemical-component table does not know every modification a depositor may
# make: 3KYS residue A344 is P1L, S-palmitoyl-cysteine, reported as
# kind=UNKNOWN / is_amino_acid=False, yet it carries a full N/CA/C backbone at
# 3.86 A and 3.85 A from residues 343 and 345. Filtering on the name deleted it,
# which BOTH removed the palmitoylation the TEAD-inhibitor literature is about
# AND split the chain into a third segment that cost a chain break downstream.

from src.structure_tools import is_chain_residue  # noqa: E402


class _FakeAtom:
    pass


class _FakeRes:
    def __init__(self, name, atoms):
        self.name, self._atoms = name, set(atoms)

    def find_atom(self, name, altloc):
        return _FakeAtom() if name in self._atoms else None


def test_a_backbone_bearing_residue_is_chain_whatever_it_is_called():
    assert is_chain_residue(_FakeRes("P1L", ("N", "CA", "C", "SG")))
    assert is_chain_residue(_FakeRes("ZZZ", ("N", "CA", "C")))


def test_a_recognised_residue_is_chain_even_with_atoms_missing():
    """A poorly resolved sidechain, or a CA-only trace, is still chain."""
    assert is_chain_residue(_FakeRes("ALA", ("CA",)))
    assert is_chain_residue(_FakeRes("MSE", ()))


def test_a_free_ligand_is_not_chain():
    assert not is_chain_residue(_FakeRes("HOH", ("O",)))
    assert not is_chain_residue(_FakeRes("GTP", ("PA", "PB", "O5'", "C5'")))
    assert not is_chain_residue(_FakeRes("EDO", ("C1", "O1", "C2", "O2")))


def test_a_partial_backbone_is_not_enough():
    """N+CA without C is a fragment, not a linked residue."""
    assert not is_chain_residue(_FakeRes("XYZ", ("N", "CA")))


@pytest.mark.skipif(not _3KYS.exists(), reason="3KYS not downloaded")
def test_the_real_palmitoyl_cysteine_survives_a_trim(tmp_path):
    """It must not be DELETED — deleting it splits TEAD1 into an extra segment
    and drops the palmitoylation site the whole TEAD-inhibitor literature is
    about. It is now also CONVERTED to its parent cysteine, because RFD3's
    parser does not know P1L and it is deposited as a HETATM record between two
    ATOM records: a contig spanning it aborted a real campaign with `Residue
    A344 not found in atom array` after ten retries.

    So the requirement is sharper than "still there": still there, at the same
    author id, with an intact backbone, and parseable.
    """
    import gemmi
    from src.structure_trim import write_trimmed
    out = tmp_path / "t.cif"
    write_trimmed(_3KYS, out,
                  {"A": list(range(195, 412))})
    st = gemmi.read_structure(str(out))
    names = {r.name for c in st[0] for r in c}
    assert "HOH" not in names, "solvent should still be stripped"

    a344 = [r for c in st[0] if c.name == "A" for r in c if r.seqid.num == 344]
    assert a344, "the palmitoylated cysteine was deleted again"
    res = a344[0]
    assert res.name == "CYS", f"A344 should be written as its parent, got {res.name}"
    assert res.het_flag == "A", (
        "A344 must be an ATOM record — as a HETATM a polymer parser skips it, "
        "which is the failure this conversion exists for")
    atoms = {a.name for a in res}
    assert {"N", "CA", "C", "O", "CB", "SG"} <= atoms, (
        f"backbone + the cysteine sidechain must survive, got {sorted(atoms)}")
    assert not {a for a in atoms if a.startswith("C") and a[1:].isdigit()}, (
        f"the palmitoyl tail must be gone, got {sorted(atoms)}")


@pytest.mark.skipif(not _3KYS.exists(), reason="3KYS not downloaded")
def test_a_modified_residue_with_no_known_parent_is_left_alone(tmp_path,
                                                               monkeypatch):
    """Guessing a parent would silently change which amino acid is designed
    against, so an unknown modification is kept as deposited and reported.
    `foundry_spec.validate_spec` is what then refuses a contig spanning it —
    before the GPU, rather than after ten RFD3 retries."""
    from loguru import logger

    import src.structure_tools as stools
    from src.structure_trim import write_trimmed

    monkeypatch.setitem(stools._CURATED_PARENTS, "P1L", None)
    monkeypatch.setattr(stools, "_CURATED_PARENTS", {})
    # The CCD tier has to be disabled too, or this test no longer tests
    # anything: the CCD knows P1L perfectly well (it is where the fourth tier
    # gets 2MR, KCR and the rest from), so with only the curated table blanked
    # the parent would still resolve. `LPT_CCD_OFFLINE` is the documented way
    # to pin a run to local knowledge, and the memo is cleared so a lookup
    # cached by an earlier test cannot answer either.
    monkeypatch.setenv("LPT_CCD_OFFLINE", "1")
    monkeypatch.setattr(stools, "_CCD_MEMO", {})
    monkeypatch.setattr(stools, "_CCD_NAMES", {})
    monkeypatch.setattr(stools, "_CCD_LOADED", True)
    msgs: list[str] = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        out = write_trimmed(_3KYS,
                            tmp_path / "t.cif",
                            {"A": list(range(195, 412))})
    finally:
        logger.remove(hid)
    import gemmi
    st = gemmi.read_structure(str(out))
    a344 = [r for c in st[0] if c.name == "A" for r in c if r.seqid.num == 344]
    assert a344 and a344[0].name == "P1L", "an unknown parent must not be guessed"
    assert any("no parent amino acid known" in m for m in msgs), msgs


@pytest.mark.skipif(not _3KYS.exists(), reason="3KYS not downloaded")
def test_the_exposure_guard_compares_the_same_atoms_on_both_sides(tmp_path):
    """Converting a modified residue drops the modification's atoms from the
    TRIMMED structure only, and biotite counts P1L as an amino acid — so the
    deposited side still carried 3KYS A344's 16-carbon tail. The pocket it
    fills is lined by MET347 and PHE392, which read as +28.9 and +21.7 A^2 of
    freshly exposed hydrophobic surface and made the guard refuse a trim that
    had cut NOTHING (208 -> 208 residues). Same class of error as counting
    waters on one side only."""
    from src.structure_trim import trim_target

    hs = [{"auth_seq_id": n} for n in
          (240, 242, 246, 249, 274, 276, 314, 346, 350, 353, 354, 357, 362, 366)]
    res = trim_target(_3KYS,
                      target_chain="A", partner_chain="B", hotspots=hs,
                      out_dir=tmp_path, budget=220)
    assert res.n_residues_after == 208, "nothing should have been cut"
    assert len(res.kept_segments) == 2, (
        f"keeping A344 means 2 segments, not 3: {res.kept_segments}")


# --- the three trim policies -----------------------------------------------
# Benchmarking across 19 complexes showed the trim would happily return a
# 19-residue "target" (7XT6, 364 residues in) and expose 2,427 A^2 of buried
# hydrophobic core (1D8D) with nothing to stop either.

def test_a_target_that_fits_the_budget_is_not_cut_at_all():
    """200-residue proteins are fine to design against; domain selection was
    cropping them anyway, which buys nothing the GPU cares about."""
    from src.structure_trim import Domain, plan_trim
    residues = [{"auth": i, "icode": "", "name": "ALA",
                 "ca": np.array([float(i), 0.0, 0.0])} for i in range(1, 201)]
    domains = [Domain(index=0, start_auth=1, end_auth=100, n_residues=100, source="cath"),
               Domain(index=1, start_auth=101, end_auth=200, n_residues=100, source="cath")]
    kept, warns = plan_trim(residues, domains, [{"auth_seq_id": 20}], budget=220)
    assert len(kept) == 200, "a target inside the budget must be kept whole"


def test_a_target_below_the_floor_is_refused():
    from src.structure_trim import Domain, TrimError, plan_trim
    residues = [{"auth": i, "icode": "", "name": "ALA",
                 "ca": np.array([float(i), 0.0, 0.0])} for i in range(1, 41)]
    with pytest.raises(TrimError, match="80-residue floor"):
        plan_trim(residues,
                  [Domain(index=0, start_auth=1, end_auth=40, n_residues=40, source="cath")],
                  [{"auth_seq_id": 20}], budget=220)


def test_the_floor_is_eighty():
    from src.structure_trim import MIN_TARGET_RESIDUES
    assert MIN_TARGET_RESIDUES == 80


def test_two_exposed_hydrophobics_are_tolerated_but_three_are_not():
    from src.structure_trim import MAX_EXPOSED_HYDROPHOBIC
    assert MAX_EXPOSED_HYDROPHOBIC == 2


@pytest.mark.skipif(not _3KYS.exists(), reason="3KYS not downloaded")
def test_the_exposure_guard_fires_on_a_cut_that_opens_the_epitope(tmp_path, tead_hotspots):
    """The same aggressive budget the mechanics tests opt out of."""
    from src.structure_trim import TrimError
    with pytest.raises(TrimError, match="hydrophobic"):
        trim_target(_3KYS, target_chain="A", partner_chain="B",
                    hotspots=tead_hotspots, budget=150, out_dir=tmp_path,
                    pdb_id="3KYS", binder_min=70, binder_max=86)


# ── a no-op trim must retain 100% by construction ────────────────────────────
#
# `write_trimmed` always strips solvent, so the AFTER interface was measured on
# a desolvated structure while the BEFORE one was measured with every ordered
# water still in place. Both sides already filtered waters out of the per-
# residue LIST, but a water at the interface still occupies space and changes
# the SASA of the protein residues around it — so `retention` drifted in
# whichever direction that structure's waters pushed it, and the direction was
# not always benign:
#
#     7CZD  162 solvent entries on chain B  ->  105.2%   (passed, wrongly)
#     6VJJ   92 solvent entries on chain A  ->   84.4%   (REFUSED, wrongly)
#
# 6VJJ is the entry `quickstart.py` itself suggests, and the refusal said
# "cutting has damaged the epitope itself" one line after the planner logged
# "keeping it whole, no trim".

_WATER_RICH = [
    ("7CZD", "B", "A", [56, 66, 115]),
    ("6VJJ", "A", "B", [38, 40, 41]),
    ("3KYS", "A", "B", [276, 314, 318, 322]),
]


@pytest.mark.parametrize("pdb,target,partner,hot", _WATER_RICH)
def test_a_trim_that_removes_nothing_retains_everything(pdb, target, partner,
                                                        hot, tmp_path):
    """Not "close to 100%" — exactly 100%. Anything else is a metric fault."""
    from src.structure_trim import trim_target

    path = _ROOT / "data" / "structures" / f"{pdb}_ba1.cif"
    if not path.is_file():
        path = _ROOT / "data" / "structures" / f"{pdb}.cif"
    if not path.is_file():
        pytest.skip(f"{pdb} not in this checkout")

    hotspots = [{"residue": "UNK", "auth_seq_id": h, "label_seq_id": 0,
                 "rfd3_atoms": "CA"} for h in hot]
    res = trim_target(path, target_chain=target, partner_chain=partner,
                      hotspots=hotspots, budget=220, out_dir=tmp_path,
                      pdb_id=pdb, max_exposed_hydrophobic=None)

    assert res.n_residues_after == res.n_residues_before, "expected a no-op trim"
    assert res.bsa_retention == pytest.approx(1.0, abs=0.005), (
        f"{pdb}: nothing was removed but retention is {res.bsa_retention:.1%} "
        f"— the before/after interface is being measured on different bases")
    assert res.bsa_dropped_A2 == pytest.approx(0.0, abs=1.0)


def test_the_trim_leaves_only_its_own_outputs_in_the_run_directory(tmp_path):
    """The desolvated copy used for the BEFORE measurement is an intermediate.
    Left in `out_dir` it sits beside `trimmed.cif` inviting someone to hand the
    wrong file to RFD3."""
    from src.structure_trim import trim_target

    if not _3KYS.is_file():
        pytest.skip("3KYS not in this checkout")
    trim_target(_3KYS, target_chain="A", partner_chain="B",
                hotspots=[{"residue": "UNK", "auth_seq_id": 276,
                           "label_seq_id": 0, "rfd3_atoms": "CA"}],
                budget=220, out_dir=tmp_path, pdb_id="3KYS",
                max_exposed_hydrophobic=None)
    cifs = sorted(p.name for p in tmp_path.glob("*.cif"))
    assert cifs == ["trimmed.cif"], cifs


def test_a_cofactor_or_metal_ion_is_not_reported_as_an_unconvertible_residue(
        tmp_path):
    """The parent-conversion warning must fire only for residues a polymer
    parser might MISTAKE for chain, i.e. ones carrying N/CA/C.

    Without that gate it fired on 5HYN (EZH2/EED) for "SAHx1, ZNx8" and
    advised adding S-adenosylhomocysteine and eight zinc ions to
    `structure_tools._CURATED_PARENTS` — which would be nonsense, and which
    buries any real modified residue in noise. Found by running the pipeline
    over a chromatin target, not by a test.
    """
    from loguru import logger

    from src.structure_trim import write_trimmed

    src = Path("data/structures/5HYN_ba1.cif")
    if not src.is_file():
        pytest.skip("5HYN not in this checkout")
    msgs: list[str] = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        write_trimmed(src, tmp_path / "t.cif", {"A": None, "B": None})
    finally:
        logger.remove(hid)
    noise = [m for m in msgs if "no parent amino acid known" in m]
    assert not noise, f"ligands/ions reported as unconvertible residues: {noise}"


def test_the_common_chromatin_ptms_all_resolve_to_lysine():
    """A chromatin-weighted corpus meets methylated and acetylated lysines
    constantly, and an unresolved one would be left as a HETATM that
    `validate_spec` then refuses. gemmi tabulates most of them; M2L it does
    not, so that one is curated."""
    from src.structure_tools import parent_residue

    for code in ("M3L", "MLY", "MLZ", "ALY", "M2L"):
        assert parent_residue(code) == "LYS", code
    # And the ligands must stay unparented — converting one would invent a
    # residue that is not there.
    for code in ("SAH", "ZN", "GOL", "EDO", "ATP", "HOH"):
        assert parent_residue(code) is None, code


# ----------------------------------------------------------------------
# An external domain annotation is trusted only if it covers the epitope
# ----------------------------------------------------------------------

@pytest.mark.skipif(not (_ROOT / "data" / "structures" / "5VAI_ba1.cif").exists(),
                    reason="5VAI not downloaded")
def test_an_annotation_that_misses_the_hotspots_falls_through(tmp_path):
    """`segment_domains` preferred the RCSB tier unconditionally, so a wrong
    annotation beat a correct geometric partition.

    5VAI chain R is a class-B GPCR and RCSB returns two ECOD domains 96-204 /
    208-421 labelled "Sulfatase,SGSH_C" — neither containing hotspots
    66/67/70. They were force-kept as orphans, the trim accreted through the
    transmembrane bundle to 220 residues in two segments, and it was REFUSED
    for opening 33 exposed hydrophobics. The geometric partition returns
    exactly 29-128, which 4ZGM — an unrelated GLP-1R ectodomain entry
    depositing its ECD as auth 29-128 — corroborates independently.
    """
    from src.structure_trim import trim_target

    res = trim_target(_ROOT / "data" / "structures" / "5VAI_ba1.cif",
                      target_chain="R", partner_chain="P",
                      hotspots=[{"auth_seq_id": n} for n in (66, 67, 70)],
                      out_dir=tmp_path, budget=220, pdb_id="5VAI")
    assert res.method == "geometric", (
        f"the ECOD annotation should have been rejected, got {res.method}")
    assert res.kept_segments == [(29, 128)], res.kept_segments
    assert res.n_segments == 1


def test_an_annotation_that_covers_the_hotspots_is_still_trusted():
    """The predicate must not reject a GOOD annotation — that would silently
    demote every target to the geometric tier."""
    from src.structure_trim import Domain, _domains_cover_hotspots

    doms = [Domain(index=0, start_auth=10, end_auth=100, n_residues=91,
                   source="cath", label="Ig-like"),
            Domain(index=1, start_auth=101, end_auth=200, n_residues=100,
                   source="cath", label="Ig-like")]
    assert _domains_cover_hotspots(doms, [45, 60, 150], "A")
    # An epitope legitimately spanning a boundary is fine: ANY hit is enough.
    assert _domains_cover_hotspots(doms, [99, 101], "A")
    # No hotspots given claims nothing — the previous behaviour exactly.
    assert _domains_cover_hotspots(doms, None, "A")
    assert _domains_cover_hotspots(doms, [], "A")
    # One uncovered hotspot is enough to distrust the whole annotation.
    assert not _domains_cover_hotspots(doms, [45, 250], "A")


@pytest.mark.skipif(not (_ROOT / "data" / "structures" / "5VAI_ba1.cif").exists(),
                    reason="5VAI not downloaded")
def test_the_exposure_warning_reports_an_area_not_only_a_count(tmp_path):
    """A residue count is not scale-free: two residues at +190 A^2 read as
    "within tolerance" while six at +16 A^2 read as over the limit. The sum,
    and its ratio to the target-side interface area, are the physically
    meaningful numbers and were never computed. Nothing gates on them yet."""
    from src.structure_trim import trim_target

    res = trim_target(_ROOT / "data" / "structures" / "5VAI_ba1.cif",
                      target_chain="R", partner_chain="P",
                      hotspots=[{"auth_seq_id": n} for n in (66, 67, 70)],
                      out_dir=tmp_path, budget=220, pdb_id="5VAI")
    exposure = [w for w in res.warnings if "hydrophobic residue(s) away" in w]
    assert exposure, res.warnings
    w = exposure[0]
    # LEU32 +58.9 and VAL36 +15.9 -> 75 A^2, against a 1,356 A^2 target side.
    assert "75 A^2" in w, w
    assert "6% of the target-side interface area" in w, w


# ----------------------------------------------------------------------
# The CCD tier, and saying so in the report
# ----------------------------------------------------------------------

def _isolated_ccd(monkeypatch, responder=None):
    """Point the CCD tier at a canned response, with no cache and no network."""
    import src.structure_tools as stools

    monkeypatch.setattr(stools, "_CCD_MEMO", {})
    monkeypatch.setattr(stools, "_CCD_NAMES", {})
    monkeypatch.setattr(stools, "_CCD_LOADED", True)      # skip the disk cache
    monkeypatch.setattr(stools, "_CCD_WARNED", False)
    monkeypatch.setattr(stools, "_ccd_cache_store",
                        lambda *a, **k: None)             # never write to data/
    monkeypatch.delenv("LPT_CCD_OFFLINE", raising=False)
    if responder is not None:
        import requests
        monkeypatch.setattr(requests, "get", responder)
    return stools


def _ccd_response(status=200, payload=None):
    def go(*_a, **_k):
        return type("R", (), {"status_code": status,
                              "json": lambda self: payload or {}})()
    return go


def test_the_ccd_tier_resolves_what_gemmi_does_not(monkeypatch):
    """The gap this tier exists for, on the residues that motivated it.

    gemmi tabulates the acetyl/methyl lysines but NOT methylarginine or the
    other acyl-lysines, and a deposited entry file never states a parent —
    measured, 0 of 1,442 `_chem_comp` rows across 60 local structures. The
    CCD states one for all of them.
    """
    stools = _isolated_ccd(monkeypatch, _ccd_response(payload={"chem_comp": {
        "type": "L-peptide linking", "mon_nstd_parent_comp_id": "ARG",
        "name": "N3, N4-DIMETHYLARGININE"}}))
    assert stools.parent_residue("2MR") == "ARG"
    assert stools.component_description("2MR") == "N3, N4-DIMETHYLARGININE"


def test_a_ligand_never_acquires_a_parent_from_the_ccd(monkeypatch):
    """A component that is not peptide-linking must not be converted, even if
    the CCD names a parent for it. Converting a cofactor would invent a
    residue that is not in the structure — the same class of error as the
    SAH/zinc false positives the backbone gate fixed."""
    stools = _isolated_ccd(monkeypatch, _ccd_response(payload={"chem_comp": {
        "type": "non-polymer", "mon_nstd_parent_comp_id": "LYS",
        "name": "some ligand"}}))
    assert stools.parent_residue("XYZ") is None


def test_a_parent_outside_the_twenty_is_refused(monkeypatch):
    """Some components name another non-standard residue as their parent. One
    hop only — resolving recursively would need a cycle guard for no measured
    benefit — so an unrecognised parent is treated as unknown."""
    stools = _isolated_ccd(monkeypatch, _ccd_response(payload={"chem_comp": {
        "type": "L-peptide linking", "mon_nstd_parent_comp_id": "MLY",
        "name": "something built on methyl-lysine"}}))
    assert stools.parent_residue("XYZ") is None


@pytest.mark.parametrize("responder,label", [
    (_ccd_response(status=500), "HTTP 500"),
    (_ccd_response(status=404), "HTTP 404"),
    (_ccd_response(payload={"unexpected": True}), "200 with junk JSON"),
    (_ccd_response(payload={}), "200 with empty JSON"),
])
def test_the_ccd_tier_fails_open(monkeypatch, responder, label):
    """A trim must never depend on the network. Every failure returns None,
    which callers already treat as "unknown parent": the residue is left as
    deposited and `validate_spec` refuses a contig spanning it, so the run
    stops before the GPU instead of guessing."""
    stools = _isolated_ccd(monkeypatch, responder)
    assert stools.parent_residue("XYZ") is None, label


def test_a_connection_failure_fails_open_too(monkeypatch):
    """The case that actually happens: no route, DNS down, TLS intercepted."""
    import requests

    def boom(*_a, **_k):
        raise requests.exceptions.ConnectionError("no route to host")

    stools = _isolated_ccd(monkeypatch, boom)
    assert stools.parent_residue("XYZ") is None
    # And it must not raise on the second call either, nor re-request.
    assert stools.parent_residue("XYZ") is None


def test_offline_mode_uses_only_what_is_cached(monkeypatch):
    """`LPT_CCD_OFFLINE=1` is for a run that must be reproducible or is behind
    a firewall: cached answers still resolve, uncached ones refuse, and no
    request is attempted."""
    import src.structure_tools as stools

    called = []
    monkeypatch.setattr(stools, "_CCD_MEMO", {"KCR": "LYS"})
    monkeypatch.setattr(stools, "_CCD_NAMES", {"KCR": "N-6-crotonyl-L-lysine"})
    monkeypatch.setattr(stools, "_CCD_LOADED", True)
    monkeypatch.setenv("LPT_CCD_OFFLINE", "1")
    import requests
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: called.append(1) or _ccd_response()())
    assert stools.parent_residue("KCR") == "LYS"
    assert stools.parent_residue("2MR") is None
    assert not called, "offline mode must make no request"


def test_selenocysteine_and_pyrrolysine_resolve(monkeypatch):
    """gemmi ANSWERS for both, with UPPERCASE one-letter codes ('U', 'O') —
    its convention for a residue in its own right rather than a modification
    — so `parent_residue` found no parent and the run stopped at spec build.
    SEC is in the shipped corpus. What a generator needs is the nearest
    BUILDABLE canonical, which is the isosteric one; RFD3 has no selenium."""
    monkeypatch.setenv("LPT_CCD_OFFLINE", "1")      # local knowledge only
    from src.structure_tools import parent_residue

    assert parent_residue("SEC") == "CYS"
    assert parent_residue("PYL") == "LYS"


@pytest.mark.skipif(not _3KYS.exists(), reason="3KYS not downloaded")
def test_a_conversion_is_reported_prominently_not_just_logged(tmp_path):
    """The trim report for 3KYS said NOTHING about A344 while replacing
    S-palmitoyl-cysteine with plain cysteine and dropping a 16-carbon tail —
    and that lipid is what the whole TEAD-inhibitor literature is about. The
    conversion is necessary (RFD3 cannot parse P1L) but it changes the
    chemistry being designed against, so it has to be visible."""
    from src.pipeline_runner import PipelineRunner
    from src.structure_trim import trim_target

    res = trim_target(_3KYS, target_chain="A", partner_chain="B",
                      hotspots=[{"auth_seq_id": n} for n in
                                (240, 242, 246, 249, 274, 276, 314, 346)],
                      out_dir=tmp_path, budget=220, pdb_id="3KYS")
    mods = res.modified_residues
    assert len(mods) == 1, mods
    m = mods[0]
    assert (m["chain"], m["auth_seq_id"], m["deposited"], m["parent"]) == \
        ("A", 344, "P1L", "CYS")
    assert "palmitoyl" in m["description"].lower(), m["description"]
    # The palmitoyl tail is what was dropped, and saying how much matters.
    assert len(m["atoms_dropped"]) == 17, m["atoms_dropped"]

    note = "\n".join(PipelineRunner._modified_residue_notes(res))
    assert "## " in note, "must be a headed section, not another bullet"
    assert "A344" in note and "P1L" in note
    assert "worth checking the biology" in note.lower()

    # And it must survive a resume, which rebuilds the trim from disk.
    import json
    assert len(json.loads(res.mapping_path.read_text())["modified_residues"]) == 1


def test_a_trim_with_no_modified_residues_adds_no_section():
    """No false prominence: a target with no modified residue gets nothing."""
    from src.pipeline_runner import PipelineRunner

    class _Bare:
        modified_residues: list = []

    assert PipelineRunner._modified_residue_notes(_Bare()) == []
