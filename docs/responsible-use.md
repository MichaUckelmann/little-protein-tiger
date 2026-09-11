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
is an expected operational fact of running it.

**A refusal is final. LPT does not retry the stage on another model.** The run
records the refusal and stops.

You can still choose a different model — nothing stops you editing a config —
but that is an override you are making and answerable for, not a fallback the
pipeline performs. "If you override a refusal" below says what it commits you
to and what this project will not accept.

### Why there is no automatic fallback

There used to be one: `models.<provider>.refusal_fallbacks` named a chain, and
a declined stage was retried down it. That chain is gone, and the key is no
longer read (a config that still sets it gets a warning at startup).

The boundary moved twice, for the same reason each time. The chain first ended
in `claude-haiku-4-5`, which did answer a PD-L1 interface stage that
`claude-sonnet-5` and `claude-opus-5` had both declined — so the last rung was
cut, on the grounds that reaching it meant the pipeline had obtained content
two better models refused to produce. But that argument does not stop at the
last rung. **Any** automatic retry is the pipeline deciding, by itself, to go
looking for a model that will produce what the operator's chosen model
declined to produce, and no reader of the output can distinguish that from a
legitimate workaround for a miscalibrated classifier.

These refusals often *are* miscalibrated for structural-biology analysis. That
is an argument for a person overriding them, not a `for` loop: a person can
say why this target is legitimate, having read the refusal, and is accountable
for the answer. A retry loop can do neither.

### If you override a refusal

The controls below are not a way past a safety decision. They are how the
pipeline is steered, and a refusal does not make them stop existing — LPT
cannot prevent you from changing a model, and pretending otherwise would be
theatre. What it can do is be clear about what you are taking on when you use
them for this.

**Overriding a refusal means asserting that the classifier is wrong about this
specific target, and you are accountable for that assertion.** Before you do
it, you should be able to say why the work is legitimate *to someone else* —
and if that answer is not obvious enough to write down, the right venue is
your institution's biosafety or dual-use research review, not a config file.
The refusal itself is a poor guide either way: these classifiers are
demonstrably miscalibrated for structural-biology analysis in both directions,
so neither a refusal nor an answer tells you anything about whether your
target is appropriate. That judgement was always yours.

What is **not** acceptable, and will be declined as a contribution:

- **Rewording a prompt** so a classifier stops objecting.
- **Walking model to model until one answers.** Doing this by hand is the
  deleted fallback chain with a person in the loop instead of a `for`
  statement; it produces the same artifact and the same unanswerable question
  about where the content came from. If two independent frontier models decline
  a stage, treat that as a result. It was the right heuristic when the code
  applied it and it is still the right one now that you do.
- **Treating a refusal as a defect to route around.** If a stage is
  consistently declined for a target you believe is legitimate, raise it as an
  issue — that is a signal worth collecting, and it is how a miscalibration
  gets fixed for everyone rather than worked around once.

The controls, for completeness:

```bash
--provider claude|gemini|openai    # which provider runs the LLM stages
--start-from <stage>               # resume, rather than re-paying for earlier stages
```

```yaml
# one stage only, in config.yaml
models:
  gemini:
    stages:
      <stage>: "<provider>:<model>"
```

A note on what the evidence actually shows, since it is easy to read the wrong
way round: across three separate interface-stage refusals on one target,
`claude-sonnet-5` refused and `claude-opus-5` then refused too — same
category, every time. Within a provider, a categorised refusal is *consistent*
rather than arbitrary. That is a reason to take the verdict seriously, not a
map of which model to try next.

**LPT ships no per-stage model default**, for any provider. `summary` and
`binder_summary` used to be pinned to `claude-haiku-4-5`, because Sonnet-class
models decline the "review of designed binders" task and Haiku answers it —
a smaller model producing content a larger one refused, which is the same thing
the deleted chain did, merely pre-declared instead of reached at runtime. It
went with the chain.

So under `--provider claude` those two stages run on `claude-sonnet-5` and may
be refused, which ends the run at the final summary. What is lost is the
write-up: the designs, the scores and the gate decisions are already on disk
and are readable without it. The default provider answers those stages
cleanly, which is the reason it is the default.

### A refusal is recorded where you can see it

Every stage report ends with a `## MODEL PROVENANCE` block naming the model
that wrote it, and both HTML reports render a "which model wrote which stage"
table from it. Since a declined stage writes no report at all, the refusal
itself goes in the project manifest — a `refusal:<stage>` checkpoint carrying
the model, the category, and the call number. The manifest is the only account
of why a run ended once the process is gone, so a later `--start-from <stage>`
on a model you chose can be read against the record of why the first attempt
stopped.

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
the campaign settled on, which model wrote each stage, the select-agent
screen result, the calibration verdict, and the API spend. (Its `declined`
field stays in the schema for campaigns that ran under the old
automatic-fallback behaviour; a run made since then cannot populate it.)

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
