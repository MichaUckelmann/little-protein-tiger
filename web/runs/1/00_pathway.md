I now have a comprehensive picture across all relevant MASLD/MASH pathways. Let me synthesise the full report.

---

## PATHWAY BIOLOGY REPORT

### DISEASE CONTEXT
- **Disease / indication:** MASLD (Metabolic dysfunction-associated steatotic liver disease) / MASH (Metabolic dysfunction-associated steatohepatitis) — the modern nomenclature for NAFLD/NASH
- **Pathway(s) implicated:** SCAP/SREBP lipogenesis, Hedgehog (Hh)/GLI, cGAS–STING innate immunity, TGF-β/SMAD3 (via SPTBN1), Insulin/PTP1B/FOXO1, Hippo/YAP-TAZ, Ubiquitin-Proteasome System (UPS), MAPK/NF-κB
- **Primary disease mechanism:** Hepatic insulin resistance driven by plasma membrane sn-1,2-DAG accumulation activates PKCε, which phosphorylates IRTK at Thr1160. Simultaneously, the SCAP/SREBP axis is constitutively activated to drive de novo lipogenesis, establishing hepatic steatosis. Steatotic hepatocytes leak mitochondrial DNA, activating the cGAS-STING innate immune axis to perpetuate inflammation. In parallel, TAZ (WWTR1) is elevated in NASH and drives fibrosis via paracrine Ihh signalling. ALDH2-deficient or lipotoxic states accumulate aldehydes (4-HNE) that cleave the TGF-β co-adaptor SPTBN1, aberrantly rewiring SMAD3 signalling and promoting fibrogenesis. (Natural history review, DOI: N/A, Page 1; SPTBN1 paper DOI: 10.1016/j.celrep.2024.114676, Page 5; cGAS-STING review DOI: 10.2147/DDDT.S521397, Page 8)
- **Frequency of pathway dysregulation:** MASLD prevalence ~30% of adults; 70–80% of obese/diabetic individuals. ~40% of MASH/HCC cases carry TGF-β pathway genetic alterations. PNPLA3 I148M is the most common risk allele (10.1016/j.isci.2022.104949).

---

### PATHWAY MAP

**Upstream suppressors / regulators:**
| Node | Role in MASLD | Evidence type |
|---|---|---|
| INSIG1/2 | ER retention of SCAP-SREBP complex; dissolved under insulin resistance → constitutive SREBP activation | Animal model (10.3803/EnM.2017.32.1.6) |
| SAMHD1 | Elevated by IFN-γ; upregulates SCAP, S1P, S2P → amplifies SREBP processing | Hepatocyte-specific KO (10.7150/ijbs.125688) |
| FOXO1 | Transcriptionally activates ASPG under low-insulin states → PTP1B activation → blocks insulin receptor | ChIP assay (10.1038/s44318-025-00525-x) |
| ALDH2 | Loss → 4-HNE accumulation → SPTBN1 cleavage → aberrant TGF-β/SMAD3 | Mouse model (10.1016/j.celrep.2024.114676) |
| SMO | Loss of Hh signalling → Gli-code collapse → SREBP1/PPARγ de-repression → steatosis | Hepatocyte-specific KO (10.7554/eLife.13308) |

**Core cascade logic:**
- **Lipogenesis arm:** Insulin resistance → INSIG dissociation from SCAP → SCAP escorts SREBP to Golgi → S1P/S2P cleavage → nuclear SREBP activates FASN, ACC, LDLR, HMGCR. Cideb promotes this ER-to-Golgi trafficking via Sec12 recruitment at ER exit sites. Cholesterol acts as a "molecular glue" at the SCAP–INSIG transmembrane interface to create a negative feedback block — a mechanism now resolved to 3.2 Å (10.1073/pnas.2525043123).
- **Inflammatory arm:** Steatosis-induced mitochondrial stress → mtDNA leakage → cGAS catalyses cGAMP → STING translocates ER-to-Golgi → TBK1/IRF3/NF-κB → TNF-α/IL-6/NLRP3 → macrophage M1 polarisation → paracrine hepatocyte injury (10.2147/DDDT.S521397).
- **Fibrotic arm:** TAZ elevation in NASH → Ihh secretion → HSC activation (10.1016/j.celrep.2024.114676); SPTBN1 fragment–4-HNE adducts → aberrant SMAD3 → fibrogenesis + lipogenesis.

**Effectors / transcription factors:** SREBP1c, SREBP2 (lipogenesis), GLI1/GLI3 (lipid balance), IRF3, NF-κB (inflammation), SMAD3 (fibrosis), TAZ/TEAD (fibrosis, paracrine signalling)

**Known downstream targets:** FASN, ACC1, ACLY, SCD1, ELOVL6, GPAT (lipogenesis); LDLR, HMGCR (cholesterol); COL1A1, α-SMA (fibrosis); TNF-α, IL-6, IFN-I (inflammation)

---

### DYSREGULATED NODES ASSESSMENT

#### SCAP (SREBP Cleavage-Activating Protein)
- **Pathway position:** Master adaptor / gatekeeper of SREBP maturation
- **Dysregulation in disease:** Required for SREBP1c/2 proteolytic activation; constitutively active in insulin-resistant hepatocytes (10.3803/EnM.2017.32.1.6, Page 3)
- **Genetic dependency evidence:** Liver-specific SCAP deletion reduces hepatic fatty acid synthesis by **90%** in ob/ob mice (10.3803/EnM.2017.32.1.6). CRISPR-mediated SCAP KO blocks disease progression in PDAC KPC model (cross-disease dependency; 10.1158/2767-9764.CRC-24-0120). Note: complete SCAP ablation in PTEN-null background worsens NASH-HCC via LPCAT3 loss and ER stress (10.1172/JCI151895) — **partial/modulated inhibition is the safer strategy**.
- **Prior therapeutic strategies:** Fatostatin (blocks SCAP-SREBP ER-to-Golgi transport); PF-429242 (S1P inhibitor; DOI 10.7150/ijbs.125688). No approved SCAP-specific PPI inhibitor.
- **Suggested PDB structures:** 4YHC (SCAP WD40 / fission yeast Scp1 – Sre1, 2.1 Å crystal; 10.1038/cr.2015.32); cryo-EM SCAP/Insig-2 complex (3.2 Å; 10.1073/pnas.2525043123 — accession not named in fingerprint)
- **Targetability note:** The SCAP–INSIG transmembrane interface is an intracellular PPI within the ER membrane. The SCAP WD40 – SREBP C-terminal domain interaction is cytosolic-face and structurally characterised (4YHC). The SCAP/INSIG PPI is particularly attractive because cholesterol itself acts as a molecular glue at the TM interface — a well-precedented small-molecule mimicry strategy. However, transmembrane interfaces are challenging for peptides.
- **Inferred PPI opportunity:** SCAP physically escorts SREBP from the ER to the Golgi; SCAP's WD40 domain directly contacts the SREBP C-terminal regulatory domain to retain/release it. Blocking the WD40–SREBP recognition interface (RK patch at R617, K635, R640, K643 in yeast) would prevent SREBP from being chaperoned to S1P/S2P, thereby blocking nuclear accumulation and lipogenic gene expression. This is the disease-relevant PPI — named partners are SCAP and SREBP1/2. Condition 1 (interaction necessity): ✅ Met — SCAP escort is obligatory for SREBP activation. Condition 2 (consequence of disruption): ✅ Met — 90% reduction in fatty acid synthesis. Condition 3 (interaction knowability): ✅ Met — crystal structure 4YHC defines the binding interface. → **[BIOLOGICALLY JUSTIFIED]**

---

#### SAMHD1 / Cohesin (SMC3–RAD21) – SCAP/S1P/S2P axis
- **Pathway position:** Upstream amplifier of SREBP processing machinery
- **Dysregulation in disease:** SAMHD1 upregulated in MASLD hepatocytes by IFN-γ/dyslipidemia; promotes SCAP, S1P, S2P expression via cohesin complex (10.7150/ijbs.125688, Page 11)
- **Genetic dependency evidence:** Hepatocyte-specific SAMHD1 KO reduces steatosis and ALT/AST in GAN diet mice (10.7150/ijbs.125688, Page 8)
- **Prior therapeutic strategies:** None found in corpus
- **Suggested PDB structures:** None mentioned in corpus
- **Targetability note:** SAMHD1 acts via the cohesin complex (SMC3/RAD21) to transactivate SCAP/S1P/S2P; SMC3 knockdown reverses SAMHD1-induced effects. The SAMHD1–SMC3/RAD21 interaction is an intranuclear PPI. SAMHD1's dNTPase catalytic activity is **not** required for its lipogenic function (R451E/T592 mutants confirm this). Thus the therapeutic opportunity is specifically blocking its non-canonical cohesin engagement.
- **Inferred PPI opportunity:** SAMHD1 requires the cohesin complex (SMC3/RAD21) to upregulate SCAP, S1P, S2P. Disrupting SAMHD1–SMC3 engagement would disconnect SAMHD1's upregulation of the SREBP processing axis. Partners are named (SAMHD1, SMC3/RAD21); corpus confirms cohesin knockdown reverses the effect. All three conditions met. → **[BIOLOGICALLY JUSTIFIED]** (genetic dependency via in vivo KO; interaction necessity and consequence are corpus-supported; no prior therapeutic precedent)

---

#### cGAS / STING
- **Pathway position:** cGAS = upstream DNA sensor; STING = adaptor kinase scaffold
- **Dysregulation in disease:** Macrophage STING hyperactivation in NAFLD drives M1 polarisation and paracrine cytokine secretion → steatosis and fibrosis (10.2147/DDDT.S521397, Page 8). STING KO reduces steatosis and fibrosis in HFD/MCD models.
- **Genetic dependency evidence:** STING KO and cGAS deletion attenuate liver injury across multiple models (10.2147/DDDT.S521397). STING required for NRasV12 hepatocyte immune surveillance (10.1038/nature24050).
- **Prior therapeutic strategies:** cGAS inhibitor RU.521; STING inhibitors H-151 and C-176 (both improve liver outcomes in preclinical models); TBK1 inhibitor BX795 (10.2147/DDDT.S521397)
- **Suggested PDB structures:** None stated in corpus for the cGAS–STING PPI interface specifically
- **Targetability note:** The cGAS–dsDNA interaction and STING dimerisation/palmitoylation site are established small-molecule targets. The cGAS–STING PPI itself is validated pharmacologically in liver disease.
- **Inferred PPI opportunity:** cGAS directly binds dsDNA to produce cGAMP, which engages STING to initiate ER-to-Golgi translocation. The cGAS–STING axis is a PPI: cGAMP produced by cGAS non-covalently binds the STING dimer interface to allosterically activate it. Blocking this ligand-receptor PPI prevents TBK1 recruitment and downstream NF-κB/IRF3. Partners named. Consequence predictable (attenuation of hepatic inflammation/fibrosis). Pharmacological precedent exists in liver (H-151, C-176). → **[VALIDATED]**

---

#### SPTBN1 (β2-spectrin) / SMAD3
- **Pathway position:** SPTBN1 = TGF-β pathway adaptor; SMAD3 = downstream effector
- **Dysregulation in disease:** 4-HNE-cleaved SPTBN1 fragments form toxic adducts → aberrant SMAD3 signalling → fibrosis + lipogenesis. ALDH2 deficiency + heterozygous Sptbn1 loss causes spontaneous MASH (10.1016/j.celrep.2024.114676, Page 5)
- **Genetic dependency evidence:** SPTBN1 siRNA and LSKO block MASH and fibrosis (p < 0.05; 10.1016/j.celrep.2024.114676, Page 8)
- **Prior therapeutic strategies:** siRNA-mediated knockdown (proof-of-concept demonstrated in vivo; 10.1016/j.celrep.2024.114676). No PPI inhibitor found in corpus.
- **Suggested PDB structures:** Q01082 (UniProt accession; not a PDB ID — note from corpus fingerprint)
- **Targetability note:** The SPTBN1–SMAD3 interaction transmits TGF-β signals; disrupting it specifically in the toxic (4-HNE-adducted) SPTBN1 context could be selective for the pathological signal, sparing normal TGF-β homeostasis.
- **Inferred PPI opportunity:** Normal SPTBN1 serves as a scaffold that anchors SMAD3 to enable TGF-β signal relay. In MASH, cleaved+modified SPTBN1 aberrantly engages SMAD3, producing a pathological signal distinct from the homeostatic one. Disrupting the SPTBN1(cleaved)–SMAD3 interaction would block fibrotic output without completely abolishing TGF-β signalling. Partners are named; corpus confirms genetic dependency. → **[BIOLOGICALLY JUSTIFIED]**

---

#### TAZ (WWTR1) / TEAD – Ihh axis
- **Pathway position:** TAZ = Hippo pathway effector; Ihh = paracrine ligand for HSC activation
- **Dysregulation in disease:** TAZ elevated in NASH hepatocytes; drives paracrine Ihh secretion → HSC activation → fibrosis and inflammation (Natural history review, DOI: N/A, Page 1). TAZ/TEAD transcription is well-documented as a fibrotic driver.
- **Genetic dependency evidence:** Animal model observations of fibrosis/inflammatory markers (Natural history review). YAP/TEAD interaction prevention completely inhibits liver tumour formation in iCCA model (0 tumors; β-Catenin paper, DOI: N/A).
- **Prior therapeutic strategies:** YAP-TEAD inhibitor CA3 at IC50 438–511 nM in liver cancer lines (10.1002/2211-5463.13901). TEAD-mediated transcription validated as necessary for tumorigenesis. Resistance via androgen receptor escape documented.
- **Suggested PDB structures:** None stated in corpus for NASH-specific TAZ/TEAD complex
- **Targetability note:** YAP/TAZ–TEAD PPI is a well-established and druggable interface (lipid pocket on TEAD). This is an intracellular nuclear interaction. Relevant in both MASH fibrosis and progression to HCC.
- **Inferred PPI opportunity:** TAZ requires TEAD co-factor binding to transcriptionally activate Ihh and other fibrotic genes. This is directly corpus-supported (TEAD interaction necessary for fibrosis/tumour formation). Partners named. → **[VALIDATED]** (pharmacological targeting precedent exists; CA3 and related compounds documented)

---

#### PTP1B / INSR (ASPG–LPI–PTP1B axis)
- **Pathway position:** PTP1B = phosphatase that dephosphorylates and inactivates insulin receptor; negative regulator of insulin signalling
- **Dysregulation in disease:** ASPG upregulation in MASLD depletes intracellular LPI → PTP1B activation → INSR/AKT dephosphorylation → insulin resistance. Human MASLD patient cohort (n=69) confirms ASPG-HOMA-IR correlation (10.1038/s44318-025-00525-x, Page 2)
- **Genetic dependency evidence:** ASPG hepatocyte KO improves insulin sensitivity in HFD mice; PTP1B knockdown improves insulin signalling (10.1038/s44318-025-00525-x)
- **Prior therapeutic strategies:** LPI 18:0 naturally inhibits PTP1B (Kd = 10.47 µM by MST); ML-193 (not MASH-specific). No PPI inhibitor for the ASPG–PTP1B axis in corpus.
- **Suggested PDB structures:** None mentioned in corpus for this axis
- **Targetability note:** PTP1B–INSR PPI is an intracellular substrate-enzyme interaction; PTP1B has a well-characterised active site. The ASPG upstream node is more upstream and liver-specific.
- **Inferred PPI opportunity:** ASPG–PTP1B is an enzymatic interaction (LPI substrate bridge), not a classical PPI; therapeutic angle here is restoring LPI levels or blocking PTP1B's substrate-binding cleft. Insufficient direct evidence for a disruptable PPI interface. **→ Insufficient evidence to infer specific classical PPI target.**

---

### TARGET OPPORTUNITY LANDSCAPE

#### [VALIDATED] cGAS / STING
- **Evidence basis:** STING and cGAS KO attenuate NAFLD steatosis and fibrosis in macrophage-specific models (10.2147/DDDT.S521397). Small-molecule STING inhibitors H-151 and C-176 demonstrate in vivo efficacy in liver injury models, validating pharmacological inhibition of this axis.
- **What makes it attractive:** cGAS–STING operates in both hepatocytes and Kupffer cells (macrophages), placing it as a convergence node for the steatosis–inflammation–fibrosis progression. The cGAMP–STING binding interface is well-defined and already targeted by clinical-stage compounds (H-151 class). Hepatic delivery strategies (LNP/GalNAc siRNA) can restrict activity to liver.
- **Key uncertainty:** No PDB structure named in corpus for the cGAS–STING PPI interface in hepatic disease context; STING inhibitors primarily target the cGAMP binding pocket (not a classical PPI surface); need to define which component to target for a PPI-type inhibitor vs. small molecule. Resistance via compensatory TAZ pathway noted in corpus.
- **Suggested PDB ID(s):** Not found in corpus for MASH-specific cGAS–STING complex

---

#### [VALIDATED] YAP/TAZ / TEAD
- **Evidence basis:** TAZ is elevated in NASH and drives fibrosis via Ihh (Natural history review, DOI: N/A). Prevention of YAP/TEAD interaction abolishes liver tumour formation in iCCA (β-Catenin paper, DOI: N/A). CA3 (IC50 ~450 nM) validates the TEAD interface as pharmacologically accessible (10.1002/2211-5463.13901).
- **What makes it attractive:** YAP/TAZ–TEAD is an established, structurally druggable PPI with a lipid-binding pocket on TEAD. NASH fibrosis is a major unmet medical need with no approved anti-fibrotic therapy. TAZ is elevated specifically in the steatohepatitis stage, offering a therapeutic window. YAP/TAZ–TEAD inhibitors (e.g., CA3, IAG933, VT3989) are in clinical trials for cancer — disease biology directly applicable to MASH fibrosis.
- **Key uncertainty:** Androgen receptor-mediated transcriptional escape documented as a resistance mechanism in cancer lines (10.1002/2211-5463.13901). Corpus does not contain a NASH/MASH-specific TEAD structure. Dual SMAD3 and TAZ inhibition may be required for complete anti-fibrotic effect.
- **Suggested PDB ID(s):** Not found in corpus for MASH-specific YAP/TAZ–TEAD complex

---

#### [BIOLOGICALLY JUSTIFIED] SCAP / SREBP (WD40–C-terminal domain interface)
- **Evidence basis:** Liver-specific SCAP deletion reduces fatty acid synthesis by 90% and resolves steatosis (10.3803/EnM.2017.32.1.6). Crystal structure of SCAP WD40 with SREBP recognition interface determined to 2.1 Å; RK-patch residues (R617, K635, R640, K643) are essential binding determinants (10.1038/cr.2015.32, PDB: 4YHC). SAMHD1 KO in MASLD mice also validates the SCAP-upregulation axis (10.7150/ijbs.125688).
- **What makes it attractive:** SCAP–SREBP is the master gating PPI for hepatic de novo lipogenesis — the earliest pathological event in MASLD steatosis. Crystal structure 4YHC defines the peptide-accessible WD40 surface. Modulating (rather than abolishing) SCAP–SREBP engagement avoids the LPCAT3/ER stress liability identified with complete SCAP deletion (10.1172/JCI151895). The WD40–peptide interface is amenable to cyclic peptide and stapled peptide design.
- **Key uncertainty:** Complete SCAP deletion paradoxically worsens NASH in PTEN-null background (10.1172/JCI151895) — partial inhibition strategy required; no safety window yet defined. Structure in corpus is from fission yeast (Scp1); mammalian SCAP WD40–SREBP complex structure not yet named in corpus. ChREBP as a redundant lipogenic TF (from corpus: SCAP/SREBP review).
- **Suggested PDB ID(s):** **4YHC** (SCAP/Scp1 WD40 domain, 2.1 Å crystal structure; 10.1038/cr.2015.32)

---

#### [BIOLOGICALLY JUSTIFIED] SPTBN1 / SMAD3 (fibrotic arm)
- **Evidence basis:** SPTBN1 siRNA and LSKO block MASH fibrosis (p < 0.05; 10.1016/j.celrep.2024.114676). SPTBN1–SMAD3 interaction is corpus-named as the pathologically rewired signalling unit in 4-HNE-exposed liver. 40% of MASH/HCC cases show TGF-β pathway genetic alterations.
- **What makes it attractive:** SPTBN1 is a non-enzymatic scaffold — its disease-relevant function is entirely PPI-dependent. Disrupting the cleaved SPTBN1–SMAD3 interaction could be achieved with a modified-selective peptide that distinguishes full-length (normal) from cleaved (pathological) SPTBN1. Directly relevant to the ALDH2-deficient population (common East Asian variant).
- **Key uncertainty:** No PDB structure found in corpus for SPTBN1–SMAD3 complex. Q01082 is a UniProt accession, not a PDB ID. No prior therapeutic precedent for this PPI.
- **Suggested PDB ID(s):** Not found in corpus

---

### PRIMARY RECOMMENDATION

**The SCAP / SREBP (WD40–C-terminal domain) PPI is recommended as the primary target** for the downstream structural pipeline. It is the only MASLD-relevant target node in the corpus with:
1. Quantitative genetic dependency evidence (90% reduction in fatty acid synthesis upon SCAP loss)
2. A crystal structure at 2.1 Å resolution explicitly defining the binding interface (PDB: 4YHC)
3. Key contact residues mapped by mutagenesis (RK patch: R617, K635, R640, K643)
4. A biologically justified partial-inhibition window to avoid the LPCAT3/ER stress liability of complete ablation
5. A cytosol-facing β-propeller surface ideal for cyclic peptide design

The cGAS–STING [VALIDATED] axis has stronger pharmacological precedent but lacks a corpus-confirmed PDB for its PPI interface, and its primary modality is small-molecule cGAMP-mimicry rather than classical PPI disruption. YAP/TAZ–TEAD is also [VALIDATED] and highly attractive for the fibrotic component but lacks a corpus-named structure in the MASH disease context.

- **Target complex:** SCAP / SREBP (WD40 domain interaction)
- **Evidence tier:** [BIOLOGICALLY JUSTIFIED]
- **Suggested PDB ID(s):** **4YHC** (SCAP WD40 domain / Scp1 from *S. pombe*, 2.1 Å; from 10.1038/cr.2015.32)
- **Proposed next step:** Run complex-structure-analysis on PDB 4YHC at `data/structures/4YHC.cif`. Target chain: SCAP WD40 domain (chain A). Partner chain: SREBP C-terminal domain (chain B). Identify hotspot residues — specifically the RK-patch basic face and SREBP C-terminal peptide contacts — for cyclic peptide inhibitor design targeting the MASLD lipogenesis axis.

---

### REDUNDANCY AND RESISTANCE RISKS

1. **SREBP isoform redundancy (SCAP/SREBP target):** SREBP-1 and SREBP-2 are both processed by SCAP; corpus confirms SREBF1/2 dual-KO required to phenocopy SCAP loss (10.1158/2767-9764.CRC-24-0120). A SCAP WD40 inhibitor that blocks the shared binding interface should suppress both isoforms simultaneously.
2. **ChREBP as parallel lipogenic driver:** ChREBP (carbohydrate-responsive element-binding protein) drives de novo lipogenesis independently of SCAP/SREBP, particularly under high-glucose conditions (10.3803/EnM.2017.32.1.6, redundancy_risks field). Dual SCAP-WD40/ChREBP targeting may be required in patients with both hyperinsulinemia and hyperglycemia.
3. **TAZ compensates for YAP loss** in the Hippo pathway (UPS review; 10.1016/j.apsb.2025.01.010). For the fibrotic component, targeting the shared TAZ/YAP–TEAD interface is preferable to isoform-selective approaches.
4. **LPCAT3/ER stress liability:** Complete SCAP blockade in PTEN-loss background worsens liver injury via LPCAT3 downregulation and phospholipid remodelling failure (10.1172/JCI151895). Modulated/partial inhibition of the SCAP–SREBP PPI (not complete ablation) is the therapeutic strategy.
5. **cGAS–STING resistance via downstream NF-κB:** Even with STING inhibition, NF-κB can be activated through alternative DAMPs (NLRP3 inflammasome). Combination with NLRP3 inhibition may be required for complete anti-inflammatory effect.
6. **Androgen receptor transcriptional escape (YAP/TEAD axis):** CA3 treatment induces AR-mediated survival signalling — dual TAZ/YAP-TEAD + AR inhibition may be required if pursuing MASH-to-HCC prevention (10.1002/2211-5463.13901).
7. **[PATHWAY INFERRED] resistance note:** For SAMHD1–SMC3 and SPTBN1–SMAD3, the absence of any therapeutic precedent means resistance liabilities are entirely uncharacterised. SMAD3 has broad homeostatic roles in hepatocytes — off-target toxicity risk requires careful mechanistic delineation before clinical translation.

---

### CORPUS COVERAGE ASSESSMENT

| Metric | Value |
|---|---|
| Papers with study_category=pathway_biology found | 12 unique papers |
| Papers with pathway_context populated | 8 of 12 |
| Pathways covered in corpus | SCAP/SREBP, Hedgehog/GLI, cGAS-STING, TGF-β/SPTBN1, Insulin/PTP1B, Hippo/YAP-TAZ, UPS/MAPK |
| Coverage gaps | Hippo/LATS1-2 kinase in MASH-specific context; FXR/bile acid axis in fibrosis; gut-liver axis/TLR4; no GWAS-prioritised targets from direct MASLD genetics studies |
| Fallback used | **No** — all top queries returned scores ≥ 0.52 with pathway_biology category |
| Expansion round run | **Yes** — triggered by new gene symbols from fingerprints: TAZ/Ihh (from natural history review), SCAP–INSIG structural interface (from SCAP pathway review) |
| Expansion terms used | `TAZ TEAD Ihh hepatic stellate cell`; `SCAP SREBP interaction WD40 INSIG binding interface structure` |
| New papers added by expansion | **4 unique DOIs** not seen in Phase 2: 10.1073/pnas.2525043123 (SCAP/Insig cryo-EM 3.2 Å), 10.1038/cr.2015.32 (SCAP WD40 crystal 4YHC), 10.15252/embj.2018100156 (Cideb-SCAP), 10.1007/s00249-022-01606-z (SCAP-Insig cholesterol docking) |
| Recommended additional keywords | `"MASLD[MeSH] AND LATS1[Gene Name]"`, `"NASH[MeSH] AND FXR[Gene Name] AND fibrosis"`, `"MASH[tiab] AND TEAD[Gene Name] AND clinical"`, `"PNPLA3 I148M[tiab] AND protein interaction"` |
| Run | `python scripts/fetch_papers.py --keywords "MASLD LATS1 LATS2 Hippo fibrosis"` then re-curate and re-ingest |

---

### PATHWAY SOURCES

| # | Title (truncated) | DOI | Study type | Category | Key node |
|---|---|---|---|---|---|
| 1 | Hepatocyte SAMHD1 Deficiency Attenuates Hepatic Steatosis… | 10.7150/ijbs.125688 | experimental_in_vitro | pathway_biology | SAMHD1 → SCAP → SREBP |
| 2 | Natural history and metabolic mechanisms of NAFLD and NASH | N/A (review) | review | pathway_biology | PKCε, TAZ, PNPLA3 |
| 3 | The SCAP/SREBP Pathway: A Mediator of Hepatic Steatosis | 10.3803/EnM.2017.32.1.6 | review | pathway_biology | SCAP/SREBP/INSIG |
| 4 | Inhibiting SCAP/SREBP exacerbates liver injury in NASH | 10.1172/JCI151895 | experimental_in_vivo | pathway_biology | SCAP/LPCAT3/ER stress |
| 5 | Hedgehog signaling regulates liver lipid metabolism (GLI-code) | 10.7554/eLife.13308 | experimental_in_vitro | pathway_biology | SMO/GLI3/SREBP1 |
| 6 | Aldehydes alter TGF-β signaling (SPTBN1/MASH) | 10.1016/j.celrep.2024.114676 | experimental_in_vitro | pathway_biology | SPTBN1/SMAD3/ALDH2 |
| 7 | cGAS-STING Targeting in Liver Diseases | 10.2147/DDDT.S521397 | review | pathway_biology | cGAS/STING/TBK1 |
| 8 | Hepatic ASPG-mediated LPI catabolism impairs insulin signaling | 10.1038/s44318-025-00525-x | experimental_in_vitro | pathway_biology | ASPG/PTP1B/FOXO1 |
| 9 | UPS: A potential target for MASLD | 10.1016/j.apsb.2025.01.010 | review | pathway_biology | ASK1/TAK1/SREBP1 |
| 10 | SCAP WD40 structure reveals SREBP recognition | 10.1038/cr.2015.32 | experimental_structural | biochemistry | SCAP WD40/SREBP (PDB: 4YHC) |
| 11 | SCAP/Insig-2 cryo-EM, cholesterol switch mechanism | 10.1073/pnas.2525043123 | experimental_structural | biochemistry | SCAP TM / Insig-2 |
| 12 | Cideb controls SCAP/SREBP ER export via Sec12 | 10.15252/embj.2018100156 | experimental_in_vitro | biochemistry | SCAP/Cideb/Sec12 |

---

### PIPELINE HANDOFF

- pdb_id: 4YHC
- target_complex: SCAP / SREBP
- structure_query: Analyze PDB 4YHC at data/structures/4YHC.cif. Target chain A (SCAP WD40 domain / Scp1 fission yeast ortholog). Partner chain B (SREBP C-terminal regulatory domain / Sre1). Identify hotspot residues on the SCAP WD40 RK-patch surface (key residues R617, K635, R640, K643) and complementary SREBP contact residues for cyclic peptide inhibitor design targeting the MASLD de novo lipogenesis axis.

---

## Summary for Downstream Pipeline

The analysis across **12 unique papers** across 4 parallel query angles + 2 expansion rounds establishes a clear **4-tier target landscape** for MASLD/MASH PPI inhibitor design:

| Priority | Complex | Tier | PDB | Best modality |
|---|---|---|---|---|
| 🥇 | **SCAP / SREBP (WD40–CTD)** | [BIOLOGICALLY JUSTIFIED] | **4YHC** ✅ | Cyclic peptide / stapled peptide |
| 🥈 | **YAP/TAZ / TEAD** | [VALIDATED] | Not in corpus | Cyclic peptide (lipid pocket) |
| 🥉 | **cGAS / STING** | [VALIDATED] | Not in corpus | cGAMP-mimetic / allosteric |
| 4th | **SPTBN1 / SMAD3** | [BIOLOGICALLY JUSTIFIED] | Not in corpus | Peptide (cleaved-form selective) |

**→ Proceed to `complex-structure-analysis` with PDB 4YHC** to characterise the SCAP WD40 – SREBP interface, map buried surface area, identify H-bond anchors within the RK-patch, and generate a hotspot map for cyclic peptide inhibitor design.