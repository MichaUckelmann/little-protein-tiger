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
