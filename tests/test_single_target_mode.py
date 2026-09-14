"""A single target with no partner chain is a legitimate campaign.

An AlphaFold monomer, a structure with one designable chain, an enzyme active
site: `design_intent: inhibit_active_site` has existed for all three, and
`_stage_structure_intel` MEASURES the case (`design_intent = "disrupt" if
partner_chain else "inhibit_active_site"`). It was nevertheless unreachable —
two near-duplicate chain validators required a partner, and everything
downstream was already partner-optional.

Found by `scripts/benchmark_trimming.py`: 5 of 28 targets produced only
`20_target_intel.md` and nothing else, all of them single-chain, all refused
with "target-intel did not name usable chains (target='A', partner='')" —
and `--chains A` did not rescue it.

**Two validators, not one, and they guard different dicts.** `_binder_sites`
is reached only via `--trial-sites N` or `--stop-after trial|spec`, so a plain
single-site run never touches it; the default path is stopped by
`_stage_trim`'s copy instead. The first validates target-intel's handoff, the
second the interface stage's hotspot table, so a fix had to land in both or it
would simply move one stage later.

**The placeholder hole is the other half.** `len(text) <= 4 and
text.isalnum()` accepts `none`, `None`, `null`, `na`, `nan`, `nil`, `TBD` and
`tbd` as auth chain ids — and `_binder_sites`'s own docstring says it exists
to catch `"TBD (PD-L1)"`, where the length limit catches the parenthesised
form and the bare word walks through. Observed in production:
`projects/gpcr_metabolic_v3` ran with `partner_chain: none` past BOTH
validators, `structure_trim._per_residue_bsa` logged "interface analysis
unavailable: Chain 'none' not found in structure" and failed open, and the
trim measured a zero interface for a target it believed had a partner.

The safety property this must not break: a `disrupt` campaign whose partner
went missing has to keep refusing. Relaxing the validator unconditionally
would turn it into a single-target run against one protein's surface when the
whole objective was to disrupt an interface — and single-target mode switches
OFF the wrong-molecule and ortholog guards, so it would lose its protection
at the same moment.
"""

from __future__ import annotations

import contextlib

import pytest

from src.pipeline_runner import (MALFORMED_CHAIN, PipelineBlockedError,
                                 PipelineRunner, chain_id_or_blank)


@contextlib.contextmanager
def captured_logs(level: str = "WARNING"):
    from loguru import logger

    out: list[str] = []
    sink = logger.add(lambda m: out.append(m.record["message"]), level=level)
    try:
        yield out
    finally:
        logger.remove(sink)


def _intel(**kw):
    base = {"pdb_id": "8FYU", "target_chain": "B", "partner_chain": "",
            "design_intent": "inhibit_active_site"}
    base.update(kw)
    return base


# ── the predicate ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["A", "B", "AA0A", "0", "H"])
def test_a_real_chain_id_survives(value):
    assert chain_id_or_blank(value) == value


@pytest.mark.parametrize("value", [
    "none", "None", "NONE", "null", "NULL", "na", "NA", "nan", "nil",
    "tbd", "TBD", "n/a", "N/A", "-", "?", "", "   ", None, "unknown",
])
def test_every_placeholder_reads_as_absent(value):
    """Not as a chain id. `none`, `na`, `null`, `nan`, `nil` and `TBD` all
    pass `len <= 4 and isalnum()`, which is what let `partner_chain: none`
    through two validators and into a real campaign."""
    assert chain_id_or_blank(value) == ""


@pytest.mark.parametrize("value", ["TBD (PD-L1)", "chain A", "the partner",
                                   "A,B", "A/B"])
def test_malformed_is_distinct_from_absent(value):
    """Absent is legal in single-target mode; malformed never is. Collapsing
    them would make a garbled handoff silently start a monomer campaign."""
    assert chain_id_or_blank(value) == MALFORMED_CHAIN


def test_there_is_one_predicate_not_two():
    """Both validators must use it. Two copies of the same rule is how the
    placeholder hole came to exist in two places at once."""
    import inspect

    from src import pipeline_runner

    sites = inspect.getsource(PipelineRunner._binder_sites)
    trim = inspect.getsource(PipelineRunner._stage_trim)
    assert "chain_id_or_blank" in sites and "chain_id_or_blank" in trim
    # And the old inline predicate is gone from the trim validator.
    assert "text.isalnum()" not in trim
    assert pipeline_runner.chain_id_or_blank is chain_id_or_blank


# ── _binder_sites ──────────────────────────────────────────────────────────

def test_a_single_target_campaign_is_allowed():
    sites = PipelineRunner._binder_sites(_intel())
    assert len(sites) == 1
    assert sites[0]["target_chain"] == "B"
    assert sites[0]["partner_chain"] == ""


def test_a_placeholder_partner_becomes_a_blank_not_a_chain():
    """The gpcr_metabolic_v3 case: `partner_chain: none` must not reach the
    trim as a chain to look up."""
    sites = PipelineRunner._binder_sites(_intel(partner_chain="none"))
    assert sites[0]["partner_chain"] == ""


def test_a_disrupt_campaign_with_no_partner_still_refuses():
    """The safety property. An interface campaign that lost its partner must
    not silently become a single-target one."""
    with pytest.raises(PipelineBlockedError, match="interface campaign"):
        PipelineRunner._binder_sites(_intel(design_intent="disrupt"))


@pytest.mark.parametrize("intent", ["disrupt", "stabilize", "", "DISRUPT"])
def test_only_single_target_intent_waives_the_partner(intent):
    with pytest.raises(PipelineBlockedError):
        PipelineRunner._binder_sites(
            _intel(design_intent=intent, partner_chain="none"))


def test_intent_matching_is_case_insensitive():
    assert PipelineRunner._binder_sites(
        _intel(design_intent="INHIBIT_ACTIVE_SITE"))[0]["partner_chain"] == ""


def test_a_malformed_partner_refuses_even_in_single_target_mode():
    with pytest.raises(PipelineBlockedError, match="neither an auth chain id"):
        PipelineRunner._binder_sites(_intel(partner_chain="TBD (PD-L1)"))


def test_a_missing_target_chain_still_refuses():
    with pytest.raises(PipelineBlockedError, match="usable target chain"):
        PipelineRunner._binder_sites(_intel(target_chain=""))


def test_claiming_single_target_while_naming_a_partner_is_refused():
    """Incoherent, and it matters because single-target mode disables the
    chain-assignment and ortholog guards — so a run must not be able to get
    that waiver while still describing an interface."""
    with pytest.raises(PipelineBlockedError, match="disagree"):
        PipelineRunner._binder_sites(
            _intel(partner_chain="A", partner_name="PD-1"))


def test_a_normal_two_chain_campaign_is_untouched():
    sites = PipelineRunner._binder_sites(
        _intel(design_intent="disrupt", partner_chain="A", partner_name="PD-1"))
    assert (sites[0]["target_chain"], sites[0]["partner_chain"]) == ("B", "A")


# ── the guards that go quiet ───────────────────────────────────────────────

def test_single_target_mode_says_which_guards_it_disabled():
    """Failing open is right — there is no partner to verify and no interface
    area to retain — but going quiet about it is not, which is the posture
    `--workflow structure` already takes for the `--uniprot` guards."""
    r = PipelineRunner.__new__(PipelineRunner)
    with captured_logs() as rec:
        r._note_single_target_guards({"design_intent": "inhibit_active_site"})
    msg = "\n".join(rec)
    assert "INACTIVE" in msg
    assert "chain-assignment" in msg
    assert "ortholog" in msg, "the second-order casualty must be named"
    assert "retention" in msg
    assert "COUNT" in msg, "must say the exposure gate changes measure"


def test_it_reports_what_the_stage_actually_wrote():
    """`none` and a genuine blank are both read as absent, and a reader needs
    to know which one happened."""
    r = PipelineRunner.__new__(PipelineRunner)
    with captured_logs() as rec:
        r._note_single_target_guards({"design_intent": "inhibit_active_site"},
                                     raw="none")
    assert "'none'" in "\n".join(rec)


def test_the_ortholog_casualty_is_real_not_folklore():
    """`self._ortholog` is assigned ONLY inside
    `_verify_target_chain_assignment`, after the point it returns from when a
    partner is missing — so no partner also disables ortholog detection and
    `_check_ortholog_conservation`. Pinned because neither function says so."""
    import inspect

    src = inspect.getsource(PipelineRunner._verify_target_chain_assignment)
    early = src.index("if not target_chain or not partner_chain")
    assignments = [i for i in range(len(src))
                   if src.startswith("self._ortholog = ", i)]
    assert assignments, "the assignment moved; re-check the warning's claim"
    assert all(i > early for i in assignments), (
        "an assignment now precedes the early return, so the warning's "
        "ortholog claim is stale")


# ── the interface prompt ───────────────────────────────────────────────────

def test_the_prompt_does_not_describe_a_partner_that_does_not_exist():
    """With no partner the old line rendered ", = chain ." — the degenerate
    description this method's own docstring blames for the PD-L1/7CZD failure,
    where a stage given one decided no usable structure existed and asked for
    a different one."""
    import inspect

    src = inspect.getsource(PipelineRunner._stage_binder_interface)
    assert "SINGLE-TARGET" in src
    assert "chain_id_or_blank" in src
    # It must also tell the model what to write back, since the skill's
    # handoff template has no single-target variant and the model invented
    # `none` when left to itself.
    i = src.index("SINGLE-TARGET")
    assert "BLANK" in src[i:i + 500]
    assert "none" in src[i:i + 600]
