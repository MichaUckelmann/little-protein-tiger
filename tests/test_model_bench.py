"""The model-comparison harness's scorers, which a spend decision rests on.

`scripts/bench_models.py` exists to answer "switch the default from
gemini-3.7-flash to 3.8?", and the answer is only as good as the checks it
scores each cell with. Those checks are worth pinning for the same reason the
showcase provenance tests exist: a scorer that is quietly wrong produces a
confident table, and nothing about the table looks wrong.

The specific bug this file was written after: `atom_errors` first compared each
hotspot's `rfd3_atoms` against `_RFD3_SIDECHAIN_ATOMS`, the table of the TWO
atoms the interface skill is asked to PREFER per residue. The skill is not
restricted to them, so a perfectly real `HIS69: ND1,NE2` scored as an error and
the first cell reported "1 bad atom" on output that `validate_spec` would have
accepted without complaint. The rule has to be the one validate_spec applies —
does the atom exist on that residue in that file — which is what these tests
hold it to.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PDL1 = _ROOT / "data" / "structures" / "7CZD_ba1.cif"


def _bench():
    """Import the script by path — `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "bench_models", _ROOT / "scripts" / "bench_models.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bench():
    return _bench()


# ── atom_errors: validate_spec's rule, not a preferred-pair table ────────────

def test_a_real_sidechain_atom_outside_the_preferred_pair_is_not_an_error(bench):
    """HIS ND1 is a real histidine atom. The preferred-pair table lists CD2/NE2.

    This is the regression: scoring against the table marked correct output
    wrong, and the benchmark's whole purpose is comparing two models' error
    rates — a scorer with a floor of false positives compresses exactly the
    difference it is meant to measure.
    """
    if not _PDL1.is_file():
        pytest.skip("7CZD_ba1.cif not in this checkout")
    res = [{"residue": "HIS", "auth_seq_id": 69, "rfd3_atoms": "ND1,NE2"}]
    assert bench.atom_errors(res, str(_PDL1), "B") == []


def test_an_atom_that_does_not_exist_on_the_residue_is_an_error(bench):
    if not _PDL1.is_file():
        pytest.skip("7CZD_ba1.cif not in this checkout")
    # Chain B 69 is a histidine; OH is a tyrosine hydroxyl.
    res = [{"residue": "HIS", "auth_seq_id": 69, "rfd3_atoms": "OH"}]
    assert bench.atom_errors(res, str(_PDL1), "B") == ["HIS69:OH"]


def test_a_hotspot_on_a_residue_the_file_does_not_have_is_an_error(bench):
    """The 8ZNL failure shape: a residue number from another structure."""
    if not _PDL1.is_file():
        pytest.skip("7CZD_ba1.cif not in this checkout")
    res = [{"residue": "TYR", "auth_seq_id": 99999, "rfd3_atoms": "OH"}]
    assert bench.atom_errors(res, str(_PDL1), "B") == ["TYR99999:no-such-residue"]


def test_atom_scoring_fails_open_on_an_unreadable_structure(bench):
    """A missing file must not read as a model error.

    Every check here has to distinguish "the model was wrong" from "the harness
    could not tell", because the two land in the same column otherwise.
    """
    assert bench.atom_errors(
        [{"residue": "HIS", "auth_seq_id": 69, "rfd3_atoms": "ND1"}],
        "/nonexistent/nope.cif", "B") == []


# ── the pipeline's own citation verification, read back ─────────────────────

def test_citation_counts_come_from_the_pipelines_own_verification_block(bench):
    """`_run_stage` appends this block; the harness must not re-derive it."""
    text = ("body\n\n## CITATION VERIFICATION\n"
            "- Citations checked: 12\n"
            "- Verified in corpus: 9\n"
            "- **NOT IN CORPUS (3):** 10.1/a, 10.1/b, 10.1/c\n")
    assert bench.citation_check(text) == {"cited": 12, "in_corpus": 9,
                                          "not_in_corpus": 3}


def test_a_clean_citation_block_reports_zero_not_in_corpus(bench):
    text = ("## CITATION VERIFICATION\n- Citations checked: 4\n"
            "- Verified in corpus: 4\n"
            "- All cited DOIs verified against fingerprint store.\n")
    assert bench.citation_check(text)["not_in_corpus"] == 0


def test_a_report_with_no_citations_at_all_is_distinguishable_from_a_clean_one(bench):
    """An empty dict, not zeros. A stage that cited nothing has not passed a
    hallucination check — it has skipped one, and the table must not print
    that as 0/0 verified."""
    assert bench.citation_check("no dois here") == {}


# ── gene resolvability ──────────────────────────────────────────────────────

def test_unresolvable_genes_flags_the_names_the_resolver_refuses(bench):
    """`NAP1` is deliberately left unresolved (it is a yeast name that once
    resolved to ACOT8); `KMT2A` must resolve. If the reference data is absent
    the check must return nothing rather than flag everything."""
    got = bench.unresolvable_genes(["KMT2A", "MEN1", "NAP1", "ZZQQX"])
    if not got:
        pytest.skip("reference data absent; the check correctly scores nothing")
    assert "NAP1" in got and "ZZQQX" in got
    assert "KMT2A" not in got and "MEN1" not in got


# ── the matrix itself ───────────────────────────────────────────────────────

def test_both_models_are_priced_or_a_budget_cap_silently_stops_enforcing(bench):
    """`rates_for` prefix-matches, and "gemini-3.8-flash" does not start with
    "gemini-3.7-flash". An unpriced model prices at $0.00, so the per-cell cap
    would never fire and the benchmark's own cost column would read zero."""
    import yaml

    from src.token_budget import load_pricing, rates_for

    load_pricing(yaml.safe_load(
        (_ROOT / "config.yaml").read_text(encoding="utf-8")))
    for model in bench.MODELS:
        assert rates_for(model), f"{model} has no pricing entry in config.yaml"


def test_every_interface_case_names_a_structure_that_is_in_the_checkout(bench):
    """The suite must not depend on an RCSB fetch mid-benchmark: a download
    failure on one cell would show up as that model being slower."""
    for _slug, pdb_id, _uniprot, _brief in bench.INTERFACE_CASES:
        d = _ROOT / "data" / "structures"
        if not d.is_dir():
            pytest.skip("data/structures absent in this checkout")
        assert (d / f"{pdb_id}_ba1.cif").is_file() or (d / f"{pdb_id}.cif").is_file(), \
            f"{pdb_id} is not on disk; the benchmark would fetch it mid-run"


def test_a_uniprot_accession_is_only_claimed_where_it_was_verified(bench):
    """`uniprot` re-arms `_verify_target_chain_assignment`, so a wrong one
    manufactures a failure. Only 7CZD carries one here, and only because chain
    B aligns to Q9NZQ7 at 100% identity against chain A's 20%."""
    claimed = {slug: u for slug, _p, u, _b in bench.INTERFACE_CASES if u}
    assert claimed == {"pdl1_7czd": "Q9NZQ7"}
