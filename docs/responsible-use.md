# Responsible use

Little Protein Tiger designs de novo protein binders and mines the primary
literature to choose targets. That combination is useful for research and is
also dual-use, so this page states plainly what the project is for, what its
output is and is not, and where the responsibility sits.

## Intended use

LPT is research software for computational biology: target discovery from
curated literature, interface analysis of experimental structures, and de novo
binder design against a chosen epitope. It is intended for use by researchers
in an institutional setting who are competent to evaluate its output.

## What a design actually is

**Every design this pipeline emits is an unvalidated computational
hypothesis.** It is a sequence and a predicted structure, produced by models
that are confident about poses they have chosen, not poses that are correct.
The pipeline is built around that fact, which is worth understanding before
you trust a number:

- Folding-model confidence (iPTM, ipSAE) reports confidence in the interface
  the model *chose*. It does not report whether that interface is the one you
  asked for. `binder_rmsd_dock` is the gate that does that work — see the
  binder-track notes in `CLAUDE.md`.
- Of designs clearing iPTM > 0.7 in two complete campaigns, only 45% and 8%
  were actually docked on the intended target site.
- Rosetta terms are computed only for designs that already passed the
  geometric gates, because a mis-docked model is still a physical pose and
  will return well-defined, meaningless energies.

A calibration verdict of SCALE_UP means "this target looks designable at a
measurable rate", not "these binders work". Nothing here is a substitute for
experimental validation.

## Biosecurity

If you synthesize anything derived from this pipeline, screening it is your
responsibility, not the pipeline's.

- Follow your institution's biosafety and dual-use research policies, and any
  applicable export-control and biosecurity regulation in your jurisdiction.
- Use a synthesis provider that screens orders. The
  [International Gene Synthesis Consortium](https://genesynthesisconsortium.org/)
  maintains a Harmonized Screening Protocol; its members screen both sequences
  and customers.
- Do not use LPT to design binders intended to cause harm, to enhance the
  virulence or transmissibility of a pathogen, to defeat a medical
  countermeasure, or to target a select agent or toxin.

The literature corpus tooling is subject to the same judgement: a target list
is not neutral just because it was assembled automatically.

## On safety classifiers

The LLM stages of this pipeline are sometimes declined by provider safety
classifiers — the interface-analysis stage most often, categorised `bio`. This
is an expected operational fact and is handled by falling back to a different
model or provider (`models.<provider>.refusal_fallbacks`).

**Rewording a prompt to get around a safety classifier is not an accepted
contribution to this project.** Model fallback is a legitimate engineering
response to an inconsistent classifier; prompt engineering aimed at defeating a
safety check is not, and pull requests doing it will be declined. If a stage is
consistently refused for a target you believe is legitimate, that is worth
raising as an issue rather than routing around.

### Two models, then stop

The fallback chain stops after **two** independent frontier models decline
(`MAX_REFUSALS_BEFORE_STOP` in `src/pipeline_runner.py`, enforced in code so a
longer configured chain cannot walk past it). The run then raises
`SkillRefusedError` and fails.

That boundary is deliberate, and it moved. The chain used to end in
`claude-haiku-4-5`, which did answer a PD-L1 interface stage that
`claude-sonnet-5` and `claude-opus-5` had both declined. Reaching that rung
means the pipeline obtained content two better models refused to produce, and
nobody reading the output could distinguish it from the legitimate case.

So the line is:

- **Crossing providers once is a probe**, and a justified one — these refusals
  are demonstrably miscalibrated for structural-biology analysis. A single
  cross-provider retry asks "is this one classifier wrong?"
- **Continuing until something answers is shopping for a permissive verdict.**
  Two frontier models agreeing is treated as a result, not an obstacle.

Do not add a rung to get a stage through. Raise an issue instead.

### A refusal is recorded where you can see it

Every stage report ends with a `## MODEL PROVENANCE` block naming the model
that wrote it — on the clean path too, because "written by the first model
asked" is what makes "this one was not" meaningful. When a model declined
first, that block names it and its category, both HTML reports show it in a
"which model wrote which stage" table, and the project manifest gets a
`refusal_fallback:<stage>` checkpoint so the record survives the process.

A run that stopped because every model declined records that too — the
manifest is the only account of why a run ended once the process is gone.

## Select-agent screening

Before any GPU stage, LPT name-screens the campaign — your query, the target
complex, the RCSB entry title and chain descriptions — against the
[Federal Select Agent Program list](https://www.selectagents.gov/sat/list.htm)
(`src/select_agents.py`). A hit **warns, records a manifest checkpoint, and
lets the run continue**; it never blocks.

It is advisory on purpose. Designing a binder against a select agent is often
legitimate countermeasure work; what it is not is unregulated, and the point
of the check is to reach you *before* a multi-day campaign rather than after
it. Confirm institutional approval before synthesising anything.

**What it is not:**

- **Not a clearance.** A clean result means no listed name appeared in the
  text that was screened. It inspects no sequence and no structure, and a
  target that does not name itself is not screened.
- **Not a "viral targets" filter, and deliberately so.** That filter is
  inverted relative to the risk — a binder against a viral protein is an
  antiviral, and anti-spike nanobodies and nirsevimab are the beneficial
  application class. "Viral protein" also does not partition cleanly
  (host/virus complexes, viral mimicry of host folds), and since
  `--workflow structure` accepts any local file, an input-side *block* is
  bypassed by renaming one. A control that can be sidestepped that easily is
  worse than none, because it invites reliance on it.
- **Not current unless you check.** The list is amended by rule. LPT's copy
  was transcribed from the 2025-01-14 revision, and the vintage it screened
  against is recorded in the checkpoint payload.

## What each run records

Every report directory gets a `provenance.json` (`src/run_provenance.py`),
regenerated whenever a report is: the project and query, the target identity
the campaign settled on, which model wrote each stage and whether any model
declined first, the select-agent screen result, the calibration verdict, and
the API spend.

For a dual-use tool, auditability is the control that is actually available —
the generative models are public and `--workflow structure` takes any file, so
prevention is not on offer. What a release can reasonably provide is that
every campaign leaves a complete, mechanical account of what it targeted and
who decided what. It is a record, not a clearance.

## No warranty

LPT is released under PolyForm Noncommercial 1.0.0 and comes with no warranty
of any kind, including no warranty that its designs are safe, effective,
novel, or free of third-party rights. See `LICENSE` and `docs/licensing.md`.

The external models and tools LPT orchestrates (RFdiffusion3, solubleMPNN,
RF3, BoltzGen, PyRosetta, Protenix) carry **their own licences**, several of
which restrict commercial use. LPT does not redistribute them and grants no
rights to them — see the third-party section of the README.

## Reporting a concern

For a security vulnerability in this code, or a misuse concern about the
project itself, contact the maintainer listed in `pyproject.toml` rather than
opening a public issue.
