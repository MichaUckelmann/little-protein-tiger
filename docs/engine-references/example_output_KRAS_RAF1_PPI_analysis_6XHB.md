# PPI ANALYSIS REPORT

## COMPLEX OVERVIEW
- **PDB ID:** 6XHB
- **Complex:** KRAS (GTPase KRas, GMPPNP-bound) / RAF1 (RBD + CRD of CRAF/RAF1)
- **Target chain:** A (KRAS, residues 1–169)
- **Partner chain:** B (RAF1 RBDCRD, residues 52–188)
- **Buried surface area:** ~1397 Å²
- **Interface residues:** 55 total (21 protein residues on KRAS target, 20 protein residues on RAF1 partner; remainder are waters/ions)
- **Hydrogen bonds across interface:** 54
- **VdW contacts across interface:** 125

## TARGET CHAIN INTERFACE RESIDUES
Chain A (KRAS):
- **All:** ILE21, ILE24, GLN25, ASN26, GLU31, ASP33, ILE36, GLU37, ASP38, SER39, TYR40, ARG41, LYS42, GLN43, VAL44, VAL45, GLY48, THR50, GLU63, ARG149, TYR157
- **Hydrophobic:** ILE21, ILE24, ILE36, VAL44, VAL45
- **Aromatic:** TYR40, TYR157
- **Charged:** GLU31, ASP33, GLU37, ASP38, ARG41, LYS42, GLU63, ARG149
- **Polar:** GLN25, ASN26, SER39, TYR40, GLN43, THR50, TYR157

## PARTNER CHAIN INTERFACE RESIDUES
Chain B (RAF1 RBDCRD):
- **All:** ARG59, ASN64, LYS65, GLN66, ARG67, VAL69, ASN71, LYS84, VAL88, ARG89, THR138, HIS139, PHE141, ARG143, PHE163, GLU174, SER177, THR178, LYS179, THR182
- **Hydrophobic:** VAL69, VAL88, PHE141, PHE163
- **Aromatic:** HIS139, PHE141, PHE163
- **Charged:** ARG59, LYS65, ARG67, LYS84, ARG89, ARG143, GLU174, LYS179
- **Polar:** ASN64, GLN66, ASN71, THR138, HIS139, SER177, THR178, THR182

## HOTSPOT REGIONS (ranked by suitability for cyclic peptide / mini-protein)

### Region 1: Switch-I / RBD Interface — Excellent

- **Target residues:** A:ILE21, A:ILE24, A:GLN25, A:ASN26, A:GLU31, A:ASP33, A:ILE36, A:GLU37, A:ASP38, A:SER39, A:TYR40, A:ARG41
- **Partner residues engaging this region:** B:ARG59, B:ASN64, B:LYS65, B:GLN66, B:ARG67, B:VAL69, B:ASN71, B:LYS84, B:VAL88, B:ARG89
- **Surface character:** broad_shallow — the switch-I region presents a broad, open surface on KRAS where the RBD β-sheet docks via an antiparallel β-β interaction
- **Hydrophobic patch:** ILE21, ILE24, ILE36 form a hydrophobic cluster flanked by the aromatic TYR40. These residues are surface-exposed when RAF is removed and provide the main hydrophobic driving force for the RBD interaction.
- **Polar framing residues:** GLU31, ASP33, GLU37, ASP38 (acidic ring on one flank); ARG41 (basic, on opposite flank); GLN25, ASN26, SER39 (neutral polar, inter-switch edge)
- **Literature evidence:**
  - Mutational heatmap studies identified **I36, S39, Y40, and R41** as key binding hotspots for the RBD interaction. I36 and Y40 contribute major hydrophobic/aromatic binding energy.
  - The switch-I region (residues ~30–40) is the classical RAS effector-binding surface, validated across decades of HRAS, KRAS, and Rap1 structural studies.
  - Peptidomimetic inhibitors (P1 series) and macrocyclic peptides (KRpep-2d family / MP-6483) have been designed to occupy this effector-binding region and successfully block both RAF and PI3K interactions.
  - Stapled peptides derived from RAF1 α-helix (Sraf-2-1, Sraf-7-1) bind KRAS with low-micromolar Kd at this interface.
- **Design notes:** This is the primary target region. The designed binder should mimic the RBD β2-strand and flanking loops, presenting hydrophobic residues to engage ILE21/ILE24/ILE36/TYR40 while making salt bridges with the acidic rim (GLU31/ASP33/GLU37/ASP38). The RAF1 residues ARG59, ARG67, ARG89 make critical salt bridges with the KRAS acidic residues — the binder should replicate these electrostatic contacts in reverse (presenting anionic groups where ARG59/67/89 sit). The surface is broad and open, ideal for either cyclic peptide or mini-protein engagement.

### Region 2: Interswitch / CRD Interface — Good

- **Target residues:** A:LYS42, A:GLN43, A:VAL44, A:VAL45, A:GLY48, A:THR50, A:ARG149, A:TYR157
- **Partner residues engaging this region:** B:THR138, B:HIS139, B:PHE141, B:ARG143, B:PHE163, B:GLU174, B:SER177, B:THR178, B:LYS179, B:THR182
- **Surface character:** gently_concave — the interswitch region and α5 helix form a shallow groove where the CRD docks
- **Hydrophobic patch:** VAL44, VAL45 form the hydrophobic core of this sub-interface, contacted by CRD PHE141 and PHE163 (aromatic stacking). These are smaller (2 residues) than Region 1.
- **Polar framing residues:** LYS42, GLN43 (inter-switch, polar); ARG149, TYR157 (α5 helix edge, polar/aromatic)
- **Literature evidence:**
  - SPR mutagenesis from the 6XHB study confirmed that KRAS **V45E** causes a significant increase in Kd for RBDCRD binding, and **N26** and **V45** are validated as contributing to RAF activation.
  - KRAS mutations **K42A**, **Q43A**, **V45E**, and **Y157A** at this CRD interface were tested by SPR — V45E showed the largest effect on binding affinity.
  - CRD mutations T178A and S177N at the partner side reduced RAF1 kinase activity by ~50%, confirming this as a functionally important interface for signaling even though it contributes modestly to binding affinity.
  - This region is critical for RAF *activation* rather than just binding — mutations here reduce kinase output even when overall complex affinity is only modestly affected.
- **Design notes:** This region is secondary but functionally important. A binder targeting only Region 2 may not achieve sufficient affinity due to the smaller hydrophobic patch. However, a mini-protein large enough to span both Region 1 and Region 2 simultaneously would be optimal — it would block both the RBD and CRD contact surfaces, fully abrogating RAF1 recruitment. The binder should present aromatic residues to mimic PHE141/PHE163 contacts with VAL44/VAL45, and a polar network to engage LYS42/GLN43/ARG149.

### Region 3: α5 Helix Extension — Marginal

- **Target residues:** A:ARG149, A:TYR157 (also part of Region 2)
- **Partner residues engaging this region:** B:GLU174, B:THR178, B:LYS179
- **Surface character:** flat — the α5 helix presents a relatively flat surface
- **Hydrophobic patch:** None. This sub-region is predominantly polar/charged (ARG149, TYR157 hydroxyl).
- **Polar framing residues:** ARG149, TYR157 (both polar/charged)
- **Literature evidence:**
  - R149 in KRAS interacts with T178 in CRD; this contact was validated by mutagenesis in the context of KRAS dimerization on the membrane.
  - Y157A reduces binding affinity for RBDCRD.
  - This region overlaps with the KRAS α4-α5 dimerization surface.
- **Design notes:** Not recommended as a standalone target due to lack of hydrophobic character. Better exploited as an extension of Region 2 in a larger mini-protein binder that spans Regions 1+2+3.

## DESIGN RECOMMENDATIONS

- **Recommended modality:** either (cyclic peptide for Region 1 alone; mini-protein to span Regions 1+2)
  - BSA ~1400 Å² suggests a mini-protein would be optimal for full coverage
  - A cyclic peptide (~800–1000 Å² footprint) could effectively target Region 1 alone, which contains the most validated hotspots
- **Primary target region:** Region 1 (Switch-I / RBD Interface) — Excellent
- **Key target residues the binder must engage:** ILE21, ILE24, ILE36, TYR40 (hydrophobic core); GLU31, ASP33, GLU37, ASP38 (acidic rim for electrostatic complementarity); ARG41 (specificity determinant)
- **Secondary target residues (for mini-protein):** VAL44, VAL45 (CRD interface hydrophobic core); LYS42, GLN43 (polar anchors)
- **Partner residues to mimic:**
  - RAF1 ARG59, ARG67, ARG89 → binder should present cationic groups to engage KRAS acidic rim (GLU31/ASP33/GLU37/ASP38)
  - RAF1 VAL69, VAL88 → binder should present hydrophobic side chains to engage KRAS ILE21/ILE24/ILE36
  - RAF1 PHE141, PHE163 → binder should present aromatic residues to stack with VAL44/VAL45 (for mini-protein spanning Region 2)
- **Estimated contact surface needed:** ~800 Å² for cyclic peptide (Region 1); ~1200–1400 Å² for mini-protein (Regions 1+2)
- **Approach vector:** The binder should approach KRAS from the effector lobe face, docking across the switch-I loop (β2 strand and flanking loops). The primary approach is perpendicular to the central β-sheet of KRAS, engaging the exposed face of the switch-I/interswitch surface. For a mini-protein, extend toward the α5 helix to also occlude the CRD-binding surface.
- **Caveats:**
  - The KRAS effector-binding surface is highly conserved across HRAS/NRAS/KRAS — a binder targeting this surface will likely be pan-RAS and lack isoform selectivity. The interswitch region (Region 2) differs among RAS subfamily members and may offer selectivity for KRAS over RRAS, RIT, RAP family members.
  - The interface is predominantly electrostatic (8 charged residues out of 21 on KRAS, 8/20 on RAF1), with 54 H-bonds. The binder must replicate extensive polar contacts in addition to hydrophobic engagement.
  - KRAS is GTP-loaded in the active signaling state. Switch-I conformation is nucleotide-dependent — the binder should be designed against the GTP/GMPPNP-bound conformation (as in 6XHB).
  - Existing macrocyclic peptide inhibitors (KRpep-2d series, MP-6483) bind a nearby but distinct epitope and act as allosteric/SOS-competitive inhibitors. The direct effector-blocking approach at the switch-I surface is validated but requires high affinity to compete with the nanomolar RBD interaction.
  - The α5 helix region overlaps with the proposed KRAS dimerization interface on membranes — a binder engaging this region may also disrupt KRAS nanoclustering, an additional therapeutic benefit.