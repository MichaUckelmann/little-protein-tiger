# Failure-mode catalog

Each entry: the symptom, why it happens, how it was caught, and the fix. These are
the traps that cost the most time; check against them whenever a result looks off.

## TS / frequency

**Two (or more) imaginary modes.** A second-order saddle, not a TS. Animate every
imaginary. If the extra one is dominated by all-hydrogen motion (a methyl or ester
–OCH3 rotor) or by the truncated capping groups, it is a **soft artifact** with ~0
reaction-coordinate component — it does not change the TS energy. Caught by reading
the displacement vectors (H-dominated, bond-closing component ≈ 0). Fix: stagger
the offending methyl and/or displace along that mode, then re-optimize with a finer
grid. It disappears entirely once real backbone replaces the caps. Do NOT redesign
the pocket over it.

**The imaginary frequency barely changes when you add the pocket.** Not a problem,
and not the catalysis metric. The imaginary frequency is the *curvature* of the
barrier top, not its *height*. An unchanged value means the pocket did not distort
the reaction coordinate (good). Catalysis is a barrier-height (energy) question —
compute ΔE‡, do not read it off the frequency.

**"Concerted" TS that is actually stepwise.** The saddle has one large imaginary
but animating it shows only ONE of the two expected bonds forming; the other stays
idle (>3 A). The reaction is stepwise. Caught by `classify_ts` on the lowest mode.
Consequence: redesign for the real rate-determining step (the single-bond TS).

## Fragment placement / relaxation

**An unanchored fragment relaxes away from the target geometry.** A guanidinium
placed to chelate bidentate relaxes to monodentate; His ligands placed near a metal
drift off. The truncated group has no backbone, so it finds ITS optimum, not the
protein-enforced one. Caught by measuring contacts after the relax (donor–acceptor
> 3.5 A). Fix: freeze a backbone-surrogate anchor atom on every fragment, in every
stage.

**A frozen anchor preserves a placement mistake.** A sign error put an anchored
methyl carbon 1.5 A from the atom it was meant to donate to; the relax then shoved
the substrate away and the donor "drifted off". The anchor faithfully held the bad
guess. Caught by measuring the GUESS before submitting. Rule: always measure the
guess geometry (key contacts, anchor distances) before any QM run.

**Anchor too far → fragment pivots.** Freezing only a distant atom on a rigid group
(imidazole ring, guanidinium) leaves it free to pivot about that point and swing its
donor away. Fix: anchor closer (freeze a ring atom), freeze two atoms, or restrain
the key contact distance.

## Chemistry of the contact

**Cationic donor next to a basic site → proton transfer.** A Lys/ammonium beside a
basic (deprotonated) imine N hands over a proton, making an iminium and destroying
the reactive species. Caught by finding a new N–H on the base (H within ~1.1 A) and
a missing H on the donor. Fix: use a NEUTRAL donor there (backbone amide, Asn/Gln
side chain). The cation that "works" in the metal analog does so via a dative bond
with no transferable proton — H-bond donors cannot copy that safely.

**Donor/acceptor or charge mismatch.** Putting an acceptor (carboxylate, Asp) at a
site that is itself an acceptor with no donatable H (a deprotonated imine N) makes
no H-bond, and stacking like charges is repulsive. Match polarity AND charge at
every contact. The deprotonated imine N wants a neutral donor or a non-protonating
cation, not another anion.

**Oxyanion hole on the wrong carbonyl.** In a conjugate (Michael) addition the
developing negative charge is on the ELECTROPHILE's enolate (the acceptor being
attacked), not the nucleophile's ester. Putting the hole on the nucleophile's
carbonyl stabilizes the wrong thing. Put the hole where charge develops at the TS.

## Metals

**First-shell ligands dissociate or coordinate through the wrong atom.** Cu(II)
with His models: ligands drift to 3.5–3.8 A, or the ring binds through a carbon
instead of the donor N; an added carboxylate (Asp) outcompetes a neutral ester for
the metal and half-opens the substrate chelate. Caught by measuring all M–L
distances (real M(II)–N/O ≈ 1.9–2.2 A) and checking the closest atoms to the metal.
Fixes: anchor each ligand close (ring atom, not a far methyl); restrain M–N/M–O
distances during the relax; keep competing anionic residues out of the first shell
(use neutral N-donors; let the net charge be what it is); or start from a known
pre-organized motif. If it keeps collapsing, shelve the metal route — a metal-free
stepwise alternative may be the better target.

## Energetics / interpretation

**"The energies look identical."** Total electronic energies of hundreds–thousands
of Hartree hide the barrier in the 3rd–4th decimal: 0.001 Ha = 0.63 kcal/mol. Also,
the TS energy matches the *near-TS* end of a scan by construction — compare the TS
to the *lowest* (reactant) scan point, not to the point nearest the TS.

**Treating a raw pocket-vs-bare barrier as proven catalysis.** A bare anion fully
solvated by implicit solvent vs a pocket that swaps bulk solvation for discrete
H-bonds is not a matched comparison; part of any difference is bookkeeping. And the
references may differ (bound complex vs separating fragments). Report the raw number
as encouraging, then do the matched reference (donors present but moved away) and,
for a rate, free energies. Note separately if the pocket creates a bound
pre-reaction complex where the bare reactants just separate — that is genuine
substrate pre-organization, a real and reportable result.

## Scaffolding handoff

**"Fix the N–H and relabel it as residue X."** A fixed backbone amide N–H is not
interchangeable with a side-chain donor (e.g. Asn's carboxamide): they attach to
different scaffolding and present the N–H from different geometry, so design cannot
realize the relabeled version. Decide per donor: sequence-agnostic backbone N–H
(model and fix as backbone), or a specific side chain (model THAT side chain in the
QM cluster and fix its functional atoms). Do not cross the two.

**Passing a TS "ligand" through a small-molecule parameterizer.** The TS has partial
bonds; a tool that perceives bond orders or "corrects" lengths will silently change
the geometry you are scaffolding around. Pass the TS as rigid coordinates and
disable bond re-perception.

**Expecting a winner from a few trajectories.** A thin motif (substrate + a few
donors) yields many nominal backbones; the signal is in filtering — fold
self-consistency, then donor retention in the TS pose, not fold quality alone.
Generate many, filter hard.
