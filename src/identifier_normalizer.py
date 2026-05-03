"""
Resolve curated protein names to canonical UniProt accessions / human gene symbols.

The corpus is loose with naming. This module applies a tiered resolver:

  exact_gene      → HGNC-approved symbol exact match
  hgnc_alias      → HGNC alias_symbol (pipe-separated synonyms)
  hgnc_prev       → HGNC prev_symbol (deprecated symbols)
  kb_id           → UniProtKB-ID exact (e.g. ``YAP1_HUMAN``)
  stripped_mutant → strip ``-G12C`` / ``-V600E`` suffix and retry
  uniprot_synonym → UniProt Gene_Synonym row from idmapping.dat
  family_head     → multi-hit (e.g. ``AKT`` → AKT1/2/3); marked ``is_family_head``
  fuzzy           → prefix match (last resort, low confidence)

A separate filter drops research tools, generic molecules, and curator
placeholders before resolution is attempted (``Cas9``, ``DNA``, ``N/A``,
``Nucleosome``, etc.) — these are returned via ``filtered_out`` so the
audit list captures them.

Lookup is HUMAN-ONLY. Non-human raw names (``Yap1`` from a mouse paper)
are mapped to the human ortholog via gene-symbol identity, since orthologs
share gene symbols across mammals in the vast majority of cases. The
``native_taxon`` is preserved on the entry; consumers that care about the
original organism still have it.
"""
from __future__ import annotations

import csv
import gzip
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Greek letter transliteration (curators use Unicode for κ, α, β, γ)
# ---------------------------------------------------------------------------

# Spelled-out forms match how HGNC/UniProt write aliases ("ER-alpha", "beta-catenin",
# "IL-1beta"). Single-letter forms (α→A) are common in formulas but rare in alias tables.
_GREEK_MAP = {
    "α": "ALPHA", "Α": "ALPHA",
    "β": "BETA",  "Β": "BETA",
    "γ": "GAMMA", "Γ": "GAMMA",
    "δ": "DELTA", "Δ": "DELTA",
    "ε": "EPSILON", "Ε": "EPSILON",
    "ζ": "ZETA", "Ζ": "ZETA",
    "η": "ETA", "Η": "ETA",
    "θ": "THETA", "Θ": "THETA",
    "ι": "IOTA", "Ι": "IOTA",
    "κ": "KAPPA", "Κ": "KAPPA",
    "λ": "LAMBDA", "Λ": "LAMBDA",
    "μ": "MU", "Μ": "MU",
    "ν": "NU", "Ν": "NU",
    "ξ": "XI", "Ξ": "XI",
    "π": "PI", "Π": "PI",
    "ρ": "RHO", "Ρ": "RHO",
    "σ": "SIGMA", "ς": "SIGMA", "Σ": "SIGMA",
    "τ": "TAU", "Τ": "TAU",
    "φ": "PHI", "Φ": "PHI",
    "χ": "CHI", "Χ": "CHI",
    "ψ": "PSI", "Ψ": "PSI",
    "ω": "OMEGA", "Ω": "OMEGA",
}


def _translit(name: str) -> str:
    return "".join(_GREEK_MAP.get(c, c) for c in name)


# ---------------------------------------------------------------------------
# Filtering: placeholders + non-protein research tools / concepts
# ---------------------------------------------------------------------------

# Curator placeholders observed in the corpus (case-insensitive, post-normalisation).
_PLACEHOLDER_TERMS = frozenset({
    "", "N/A", "NA", "NONE", "NULL", "UNKNOWN", "UNSPECIFIED",
    "?", "-", "--", "TBD", "NOTAPPLICABLE", "NOT APPLICABLE", "NOT SPECIFIED",
})

# Generic biomolecules and research tools that are not gene products and
# should never resolve to a UniProt accession in this pipeline.
_NON_PROTEIN_TERMS = frozenset({
    # Nucleic acids / generic
    "DNA", "RNA", "SSDNA", "DSDNA", "MRNA", "SIRNA", "MIRNA", "TRNA", "SHRNA",
    "GUIDE RNA", "GRNA", "CRRNA", "CHROMATIN", "NUCLEOSOME",
    # CRISPR / genome-editing tools
    "CAS9", "DCAS9", "SPCAS9", "SACAS9", "CAS12", "CAS12A", "CAS13",
    "CAS-9", "DEAD CAS9", "CRISPR",
    # Protein tags / fluorescent reporters
    "GFP", "EGFP", "MCHERRY", "RFP", "YFP", "BFP", "MNEONGREEN", "TAGRFP",
    "HALO TAG", "HALOTAG", "SNAP TAG", "SNAPTAG", "FLAG TAG",
    "HA TAG", "MYC TAG", "GST TAG", "MBP TAG", "HIS TAG", "STREP TAG",
    "HALO", "SNAP",
    # Lab proteases / commodity enzymes used as tools
    "TEV PROTEASE", "TEV", "PRESCISSION", "PRESCISSION PROTEASE",
    "THROMBIN", "TRYPSIN",
    # In silico tools / methods (often appear as 'protein' entities)
    "ALPHAFOLD", "ALPHAFOLD2", "ALPHAFOLD3", "PROTEINMPNN", "ESMFOLD",
    "ROSETTA", "ROSETTAFOLD", "RFDIFFUSION", "RFD3", "BOLTZ",
    # Common reagents that aren't human gene products in the usual sense
    "BIRA", "TETR", "ARAC",  # bacterial regulators used in expression systems
    "STREPTAVIDIN", "AVIDIN",
    "T7 RNA POLYMERASE", "T7", "T7RNAP", "T7 POLYMERASE",
    "CRE RECOMBINASE", "CRE", "FLP RECOMBINASE", "FLP", "FLPE",
    "LUCIFERASE", "RENILLA LUCIFERASE", "FIREFLY LUCIFERASE",
    "BETA-GALACTOSIDASE", "LACZ",
    "IGG", "IGM", "IGA", "FAB", "SCFV", "SCFV16", "SCFV-FC",
    "GB1", "PROTEIN G", "PROTEIN A",  # purification handles
    "KRAB",  # repressor domain used as tool
    "DCAS9-KRAB", "CAS9-KRAB",
    # Generic chemical / biological-but-not-gene
    "INSULIN",  # often used as positive-control / model substrate
    "CHOLESTEROL", "PIP2", "PIP3", "ATP", "GTP", "ADP", "GDP", "AMP",
    "CGAMP", "CAMP", "CGMP", "NAD", "NADH", "FAD", "FADH",
    "CA2+", "CA(2+)", "CALCIUM", "MAGNESIUM", "MG2+", "ZINC", "ZN2+",
    "NA+", "K+",
    # Mitochondrial / organelle DNA — not a protein
    "MTDNA", "CHLOROPLAST DNA", "PLASMID", "PHAGE DNA",
    # Generic phospho-histones (PTMs, not proteins)
    "GAMMA-H2AX", "GAMMAH2AX", "PHOSPHO-H3",
    # Generic placeholders curators sometimes write
    "TARGET RNA", "TARGET DNA", "TARGETRNA", "TARGETDNA",
    "TARGET GENES", "TARGETGENES", "SUBSTRATE", "LIGAND",
    "COMPOUND 1", "COMPOUND 2", "COMPOUND 3", "COMPOUND",
    # Cellular structures / regions / generic biology nouns the curators
    # sometimes write into protein_pair when the actual partner is a
    # location rather than a protein.
    "MEMBRANE", "LIPID MEMBRANE", "LIPIDMEMBRANE", "PLASMA MEMBRANE",
    "MITOCHONDRIA", "MITOCHONDRION", "NUCLEUS", "CYTOSOL", "ER",
    "ENDOPLASMIC RETICULUM", "GOLGI", "LYSOSOME", "CHROMOSOME",
    "PROMOTER", "ENHANCER", "TERMINATOR", "OPERATOR",
    "ANTIGEN", "ANTIBODY", "EPITOPE", "PEPTIDE",
    "NCP", "NUCLEOSOME CORE PARTICLE",
    # Cell-type labels that curators occasionally write as "interactors"
    "T CELL", "B CELL", "CD4+ T CELL", "CD8+ T CELL",
    "CD4+TCELL", "CD8+TCELL", "T CELL RECEPTOR", "TCR",
    # Long-RNA / ncRNA placeholders that aren't protein-coding
    "XIST", "MALAT1", "HOTAIR", "NEAT1",
    # PTMs / chemical groups (not proteins)
    "H3K4ME3", "H3K9ME3", "H3K27ME3", "H3K27AC", "H3K36ME3", "H3K9AC",
    "H4K20ME3", "H3K9ME2", "H3K4ME1", "H3K4ME2",
    "UBIQUITIN", "SUMO", "SUMO1", "NEDD8",  # generic-mention only; specific
    # ubiquitin-genes UBA52/UBB/UBC are not affected (different strings).
    # MHC nomenclature when written generically (specific HLAs still resolve)
    "MHC", "MHC-I", "MHC-II", "MHCI", "MHCII", "HLA",
    # Viral / non-host proteins (DepMap is human cell lines anyway)
    "SARS-COV-2 SPIKE", "SPIKE PROTEIN", "SPIKE", "ACE2 RECEPTOR",
    "INFLUENZA HA", "HEMAGGLUTININ",
})


# Curated alias overrides for cases HGNC's alias_symbol field misses.
# Maps a *normalised* query form to an APPROVED HGNC gene symbol.
# Only for high-frequency, unambiguous mappings — do not let this grow.
_BIOLOGY_ALIASES: dict[str, str] = {
    # RNA polymerase II catalytic subunit
    "POLII": "POLR2A", "POL2": "POLR2A", "POLYMERASE2": "POLR2A",
    "RNAPII": "POLR2A", "RNAPOL2": "POLR2A", "RNAPOLII": "POLR2A",
    # Cadherins
    "ECADHERIN": "CDH1", "ECAD": "CDH1",
    "NCADHERIN": "CDH2", "NCAD": "CDH2",
    "VECADHERIN": "CDH5", "VECAD": "CDH5",
    "PCADHERIN": "CDH3",
    # IFN gamma after greek translit -> "IFN-GAMMA" -> "IFNGAMMA"
    "IFNGAMMA": "IFNG",
    "IFNALPHA": "IFNA1", "IFNBETA": "IFNB1",
    # MEK family — HGNC has MAP2K1 alias 'MEK1' but bare 'MEK' isn't listed.
    # Treat 'MEK' as MAP2K1 (the dominant member referred to in literature).
    "MEK": "MAP2K1",
    # Caspases — HGNC alias does not list "Caspase-3", "Caspase3", "CASPASE3"
    "CASPASE1": "CASP1", "CASPASE2": "CASP2", "CASPASE3": "CASP3",
    "CASPASE4": "CASP4", "CASPASE5": "CASP5", "CASPASE6": "CASP6",
    "CASPASE7": "CASP7", "CASPASE8": "CASP8", "CASPASE9": "CASP9",
    "CASPASE10": "CASP10", "CASPASE11": "CASP4",  # mouse Casp11 = human CASP4
    "CASPASE12": "CASP12", "CASPASE14": "CASP14",
    # Cyclins — verbose form
    "CYCLIND1": "CCND1", "CYCLIND2": "CCND2", "CYCLIND3": "CCND3",
    "CYCLINE1": "CCNE1", "CYCLINE2": "CCNE2",
    "CYCLINA1": "CCNA1", "CYCLINA2": "CCNA2",
    "CYCLINB1": "CCNB1", "CYCLINB2": "CCNB2",
    # Cytoskeletal default isoforms
    "ACTIN": "ACTB",       # default: cytoplasmic beta-actin
    "FACTIN": "ACTB",      # F-actin = polymerised beta-actin
    "GACTIN": "ACTB",
    "ALPHASMA": "ACTA2",   # smooth-muscle alpha actin
    "TUBULIN": "TUBB",     # default beta-tubulin (TUBB)
    "ALPHATUBULIN": "TUBA1A", "BETATUBULIN": "TUBB",
    "VIMENTIN": "VIM",
    # Lamins
    "LAMINA": "LMNA", "LAMINB1": "LMNB1", "LAMINB2": "LMNB2",
    "LAMINC": "LMNA",  # lamin C is a splice variant of LMNA
    # Heat shock + stress response
    "HSP70": "HSPA1A", "HSP72": "HSPA1A",
    "HSP60": "HSPD1", "HSP40": "DNAJB1",
    "HSC70": "HSPA8",
    # Receptors & signalling (Greek-letter Spell-outs)
    "BETA2AR": "ADRB2", "BETA1AR": "ADRB1", "BETA3AR": "ADRB3",
    "M2R": "CHRM2", "M1R": "CHRM1", "M3R": "CHRM3",
    "ALPHASYNUCLEIN": "SNCA", "BETASYNUCLEIN": "SNCB", "GAMMASYNUCLEIN": "SNCG",
    "AT1R": "AGTR1", "AT2R": "AGTR2",
    "GABAARECEPTOR": "GABRA1",  # default GABA-A alpha1 subunit
    # Amyloid / neurodegeneration
    "ABETA": "APP", "ABETA40": "APP", "ABETA42": "APP", "ABETA43": "APP",
    "AMYLOIDBETA": "APP",
    "HUNTINGTIN": "HTT",
    "TAUOPATHY": "MAPT",  # study term but commonly used
    # G-protein subunits
    "GALPHAS": "GNAS", "GALPHAI": "GNAI1", "GALPHAQ": "GNAQ",
    "GALPHAI1": "GNAI1", "GALPHAI2": "GNAI2", "GALPHAI3": "GNAI3",
    "GALPHA12": "GNA12", "GALPHA13": "GNA13", "GALPHAO": "GNAO1",
    "GS": "GNAS", "GI": "GNAI1", "GQ": "GNAQ",  # very lossy but standard shorthand
    # Cyclin-dependent kinases / kinase shorthand
    "CK2": "CSNK2A1",
    "CAMKII": "CAMK2A", "CAMKIV": "CAMK4", "CAMKI": "CAMK1",
    "PKA": "PRKACA",  # catalytic subunit α
    "PKC": "PRKCA",   # default α isozyme
    "GSK3BETA": "GSK3B", "GSK3ALPHA": "GSK3A",
    "GSK3": "GSK3B",
    "AURORAA": "AURKA", "AURORAB": "AURKB", "AURORAC": "AURKC",
    # Apoptosis / inflammasome / immune
    "GRANZYMEB": "GZMB", "GRANZYMEA": "GZMA", "GRANZYMEK": "GZMK",
    "PERFORIN": "PRF1",
    # Nuclear receptors
    "PPARALPHA": "PPARA", "PPARGAMMA": "PPARG", "PPARDELTA": "PPARD",
    "PPARBETA": "PPARD",
    "RORALPHA": "RORA", "RORBETA": "RORB", "RORGAMMA": "RORC",
    "RORGAMMAT": "RORC",
    "RXRALPHA": "RXRA", "RXRBETA": "RXRB", "RXRGAMMA": "RXRG",
    "LXR": "NR1H3",  # default LXRα
    "LXRALPHA": "NR1H3", "LXRBETA": "NR1H2",
    # Stress / unfolded protein response
    "IRE1ALPHA": "ERN1", "IRE1": "ERN1",
    "PERK": "EIF2AK3",  # PKR-like ER kinase
    "ATF6ALPHA": "ATF6", "ATF6BETA": "ATF6B",
    # Cytoskeletal + adhesion
    "EMERIN": "EMD",
    "TITIN": "TTN",
    "BETACARDIACMYOSIN": "MYH7",
    "ALPHACARDIACMYOSIN": "MYH6",
    "BETAARRESTIN1": "ARRB1", "BETAARRESTIN2": "ARRB2",
    "BETAARR1": "ARRB1", "BETAARR2": "ARRB2",
    "BARR1": "ARRB1", "BARR2": "ARRB2",
    "CALMODULIN": "CALM1",
    "CALCINEURIN": "PPP3CA",  # catalytic A subunit α
    # Cytokines / interferons
    "TGFBETA1": "TGFB1", "TGFBETA2": "TGFB2", "TGFBETA3": "TGFB3",
    "TGFBETA": "TGFB1",
    "IFN1": "IFNA1", "TYPE1IFN": "IFNA1", "TYPEIIFN": "IFNA1",
    "IFNI": "IFNA1",
    "INTERFERONGAMMA": "IFNG",
    # Misc common shorthand
    "ALPHASMA": "ACTA2",
    "RNAPOLYMERASEII": "POLR2A", "RNAPOLYMERASE2": "POLR2A",
    "RNAPOLYMERASE": "POLR2A",  # default to POLR2A
    "RNAP": "POLR2A",
    "EBPALPHA": "CEBPA", "EBPBETA": "CEBPB", "EBPDELTA": "CEBPD",
    "CEBPALPHA": "CEBPA", "CEBPBETA": "CEBPB",
    "GAPHL": "GAPDH",
    "GADPH": "GAPDH",  # frequent typo
    # Autophagy / LC3 family
    "LC3B": "MAP1LC3B", "LC3A": "MAP1LC3A", "LC3C": "MAP1LC3C",
    "GABARAP": "GABARAP", "GABARAPL1": "GABARAPL1", "GABARAPL2": "GABARAPL2",
    # SREBP family
    "SREBP": "SREBF1", "SREBP1": "SREBF1", "SREBP2": "SREBF2",
    "SREBP1A": "SREBF1", "SREBP1C": "SREBF1",
    # Phosphatases
    "PP2A": "PPP2CA", "PP1": "PPP1CA", "PP2B": "PPP3CA",
    # Lysosomal / autophagy
    "LAMP2A": "LAMP2", "LAMP2B": "LAMP2", "LAMP2C": "LAMP2",
    # MHC nomenclature where a single chain is reasonable default
    "HLADR": "HLA-DRA", "HLADRB": "HLA-DRB1",
    # NKX hyphen variants
    "NKX21": "NKX2-1", "NKX22": "NKX2-2", "NKX25": "NKX2-5", "NKX61": "NKX6-1",
    # Hippo / fly orthologs commonly referenced as human equivalent
    # Yki is the Drosophila ortholog of YAP1 — papers often discuss it as
    # a stand-in for human Hippo signalling, so map to human ortholog.
    "YKI": "YAP1",
    # Mouse-named genes that share human gene symbol after upper-casing
    # (Trp53 is mouse Tp53; the human ortholog is TP53).
    "TRP53": "TP53",
    # Specific "h<gene>" no-hyphen species prefixes that recur in literature
    "HACE2": "ACE2", "HTERT": "TERT", "HTAU": "MAPT",
    # Common gene-name shorthand HGNC misses
    "MENIN": "MEN1",
    "MYOSIN": "MYH7",            # default to cardiac beta-myosin heavy chain
    "CARDIACMYOSIN": "MYH7",
    "ALPHACARDIACMYOSIN": "MYH6", "BETACARDIACMYOSIN": "MYH7",
    "NONMUSCLEMYOSIN": "MYH9",
    "SKELETALMYOSIN": "MYH1",
    "MYOSINHEAVYCHAIN": "MYH7",
}


# ---------------------------------------------------------------------------
# Light name normalisation (mirrors src._corpus_graph but with greek + diacritic)
# ---------------------------------------------------------------------------

_SPECIES_PREFIX = re.compile(r"^(?:H|M|R|HSA|MMU|RNO)-(?=[A-Z0-9])", re.IGNORECASE)
_MUTANT_RE = re.compile(r"[-\s]?[A-Z]\d{1,4}[A-Z]$", re.IGNORECASE)


def normalize(name: str) -> str:
    """Uppercase, transliterate Greek, strip species prefix, drop hyphens and whitespace.

    Hyphen handling is more aggressive than ``src._corpus_graph._normalize_protein``
    on purpose: HGNC aliases come in inconsistent hyphenation
    (``"PD-L1"`` vs ``"PDL1"``, ``"E-cadherin"`` vs ``"Ecadherin"``) and we
    want both forms to collapse to a single dict key.

    Returns "" for placeholders so callers can treat them uniformly.
    """
    if not name:
        return ""
    s = _translit(name).strip().upper()
    s = _SPECIES_PREFIX.sub("", s)
    s = s.replace("-", "").replace(" ", "")
    if s in _PLACEHOLDER_TERMS:
        return ""
    return s


def _strip_mutant(name: str) -> str:
    """Strip a single trailing point-mutation suffix (KRAS-G12C → KRAS)."""
    s = name.strip()
    m = _MUTANT_RE.search(s)
    if m and m.start() > 1:
        return s[: m.start()].rstrip("-").rstrip()
    return s


_HISTONE_GENERIC = frozenset({
    "H1", "H2A", "H2B", "H3", "H4", "H3.3", "H2A.X", "H2A.Z",
    "HISTONE H1", "HISTONE H2A", "HISTONE H2B", "HISTONE H3", "HISTONE H4",
})

_COMPLEX_TERMS = frozenset({
    "PROTEASOME", "RIBOSOME", "SPLICEOSOME", "EXOSOME",
    "PRC1", "PRC2", "MTORC1", "MTORC2", "TORC1", "TORC2",
    "BAF COMPLEX", "PBAF", "NURF", "INO80", "SWI/SNF", "SWISNF",
    "COP9", "COP9 SIGNALOSOME", "ANAPHASE PROMOTING COMPLEX", "APC/C",
    "26S PROTEASOME", "20S PROTEASOME",
    "COHESIN", "CONDENSIN", "MEDIATOR", "MRN", "MRN COMPLEX",
    "V-ATPASE", "VATPASE", "F-ATPASE", "FATPASE",
    "MCM2-7", "MCM27", "MCM COMPLEX",
    "GATOR1", "GATOR2", "RAGULATOR", "TSC", "TSC COMPLEX",
    "CMG", "CMG COMPLEX",
    "HUSH", "HUSH COMPLEX",
    "DSIF", "DSIF COMPLEX",
})


_VIRAL_PREFIXES = (
    "SARS-COV-2", "SARS-COV", "SARSCOV2", "SARSCOV",
    "HIV-1", "HIV-2", "HIV1", "HIV2",
    "HCV", "HBV", "HPV", "EBV", "HSV-1", "HSV-2", "CMV", "VZV",
    "INFLUENZA", "DENGUE", "ZIKA", "EBOLA", "RSV",
)


def is_filtered(raw_name: str) -> str | None:
    """If the name should be filtered out, return a reason tag; else None.

    Reasons:
      - ``"placeholder"`` — curator placeholder ("N/A", "null", etc.)
      - ``"non_specific_histone"`` — generic "H3" / "Histone H3" without paralog
      - ``"complex"`` — multi-subunit complex, not a single gene product
      - ``"viral"`` — viral-prefixed protein (SARS-CoV-2 Spike, HIV-1 Env, ...)
      - ``"research_tool"`` — reagent / tag / generic biomolecule (Cas9, GFP, DNA)
    """
    if not raw_name:
        return "placeholder"
    upper = _translit(raw_name).strip().upper()
    compact = upper.replace(" ", "")
    dehyphen = compact.replace("-", "")

    def in_set(s: frozenset[str]) -> bool:
        return upper in s or compact in s or dehyphen in s

    if in_set(_PLACEHOLDER_TERMS):
        return "placeholder"
    if in_set(_HISTONE_GENERIC):
        return "non_specific_histone"
    if in_set(_COMPLEX_TERMS):
        return "complex"
    # Viral proteins are detected by prefix — anything starting with a
    # known virus identifier is non-human and should not anchor DepMap edges.
    for vp in _VIRAL_PREFIXES:
        if upper.startswith(vp + " ") or upper.startswith(vp + "-") or compact.startswith(vp):
            return "viral"
    if in_set(_NON_PROTEIN_TERMS):
        return "research_tool"
    return None


# ---------------------------------------------------------------------------
# Resolution result
# ---------------------------------------------------------------------------


@dataclass
class Resolution:
    raw_name: str
    normalized: str = ""
    human_uniprot: str | None = None
    human_gene_symbol: str | None = None
    candidate_uniprots: list[str] = field(default_factory=list)
    match_confidence: str | None = None  # tier name, see module docstring
    is_family_head: bool = False
    is_human_ortholog_mapping: bool = False
    native_taxon: int | None = None
    filtered_reason: str | None = None  # set when not resolvable & was filtered

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_name": self.raw_name,
            "normalized": self.normalized,
            "human_uniprot": self.human_uniprot,
            "human_gene_symbol": self.human_gene_symbol,
            "candidate_uniprots": self.candidate_uniprots,
            "match_confidence": self.match_confidence,
            "is_family_head": self.is_family_head,
            "is_human_ortholog_mapping": self.is_human_ortholog_mapping,
            "native_taxon": self.native_taxon,
        }


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class IdentifierNormalizer:
    """Resolver loaded once at startup from UniProt + HGNC tables.

    Construction is the only expensive step (~2 s for UniProt, ~1 s for HGNC).
    ``resolve()`` is dict-lookup fast.
    """

    def __init__(self, uniprot_idmapping: Path, hgnc_tsv: Path):
        self.uniprot_path = Path(uniprot_idmapping)
        self.hgnc_path = Path(hgnc_tsv)

        # UniProt indexes
        self._by_gene: dict[str, list[str]] = {}        # gene_symbol → [acc]
        self._by_synonym: dict[str, list[str]] = {}     # uniprot synonym → [acc]
        self._by_kb_id: dict[str, str] = {}             # YAP1_HUMAN → P46937
        self._uniprot_to_gene: dict[str, str] = {}      # acc → primary gene symbol (UniProt)

        # HGNC indexes (human only, by definition)
        self._hgnc_symbol_to_uniprot: dict[str, list[str]] = {}     # APPROVED symbol → [uniprot]
        self._hgnc_alias_to_symbol: dict[str, list[str]] = {}       # alias → [APPROVED symbol]
        self._hgnc_prev_to_symbol: dict[str, list[str]] = {}        # prev_symbol → [APPROVED]

        self._load_uniprot()
        self._load_hgnc()

    # ------------------------------------------------------------------
    def _load_uniprot(self) -> None:
        opener = gzip.open if self.uniprot_path.suffix == ".gz" else open
        with opener(self.uniprot_path, "rt", encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue
                acc, key, value = parts
                if key == "Gene_Name":
                    self._by_gene.setdefault(value.upper(), []).append(acc)
                    # Keep first-seen as the primary mapping for acc → gene.
                    self._uniprot_to_gene.setdefault(acc, value.upper())
                elif key == "Gene_Synonym":
                    self._by_synonym.setdefault(value.upper(), []).append(acc)
                elif key == "UniProtKB-ID":
                    self._by_kb_id[value.upper()] = acc

    # ------------------------------------------------------------------
    def _load_hgnc(self) -> None:
        with self.hgnc_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh, delimiter="\t", quotechar='"')
            header = next(reader)
            try:
                i_symbol = header.index("symbol")
                i_alias = header.index("alias_symbol")
                i_prev = header.index("prev_symbol")
                i_uniprot = header.index("uniprot_ids")
                i_status = header.index("status")
            except ValueError as exc:
                raise RuntimeError(f"HGNC TSV missing expected column: {exc}") from None

            for row in reader:
                if len(row) <= max(i_symbol, i_alias, i_prev, i_uniprot, i_status):
                    continue
                if row[i_status] != "Approved":
                    continue
                symbol = row[i_symbol].strip().upper()
                if not symbol:
                    continue
                # uniprot_ids can be pipe-separated for genes coding multiple
                # isoforms with separate accessions; keep them all.
                uniprots = [u for u in row[i_uniprot].split("|") if u]
                if uniprots:
                    self._hgnc_symbol_to_uniprot[symbol] = uniprots
                    # Also index a hyphen-stripped form so `NKX21` finds `NKX2-1`.
                    sym_norm = normalize(row[i_symbol])
                    if sym_norm and sym_norm != symbol:
                        self._hgnc_symbol_to_uniprot.setdefault(sym_norm, uniprots)
                for alias in row[i_alias].split("|"):
                    a = alias.strip().upper()
                    if a:
                        self._hgnc_alias_to_symbol.setdefault(a, []).append(symbol)
                        # Also normalised form (greek + dehyphen) so "PD-L1" hits.
                        an = normalize(alias)
                        if an and an != a:
                            self._hgnc_alias_to_symbol.setdefault(an, []).append(symbol)
                for prev in row[i_prev].split("|"):
                    p = prev.strip().upper()
                    if p:
                        self._hgnc_prev_to_symbol.setdefault(p, []).append(symbol)
                        pn = normalize(prev)
                        if pn and pn != p:
                            self._hgnc_prev_to_symbol.setdefault(pn, []).append(symbol)

    # ------------------------------------------------------------------
    def _hgnc_symbol_lookup(self, symbol: str) -> tuple[str | None, list[str]]:
        """Given a candidate HGNC-approved symbol, return (symbol, [uniprots]) or (None, []).

        A symbol is considered HGNC-approved only if it appears in the
        approved-symbol index. Empty ``uniprot_ids`` is then patched from
        the UniProt index, but unknown symbols never silently fall through
        to UniProt — that prevents TrEMBL TrEMBL-only oddities (e.g.
        ``IL1BETA`` from a parasite proteome) from leaking into HGNC tiers.
        """
        sym = symbol.upper()
        if sym not in self._hgnc_symbol_to_uniprot:
            return None, []
        accs = list(self._hgnc_symbol_to_uniprot[sym])
        if not accs:
            accs = list(self._by_gene.get(sym) or [])
        return sym, accs

    # ------------------------------------------------------------------
    def resolve(self, raw_name: str, native_taxon: int | None = None) -> Resolution:
        """Resolve ``raw_name`` to a Resolution.

        ``native_taxon`` is the NCBI taxon ID of the species the protein
        was studied in (from ``methodology.protein_origin_organism``). It
        is *currently informational only* — every resolution maps to the
        human ortholog via gene-symbol identity. ``is_human_ortholog_mapping``
        flags entries where the native taxon != 9606.
        """
        res = Resolution(raw_name=raw_name, native_taxon=native_taxon)
        if not raw_name:
            res.filtered_reason = "placeholder"
            return res

        # 1. Filter junk first (placeholders + research tools).
        reason = is_filtered(raw_name)
        if reason:
            res.filtered_reason = reason
            return res

        norm = normalize(raw_name)
        res.normalized = norm
        if not norm:
            res.filtered_reason = "placeholder"
            return res

        # is_human_ortholog_mapping flag — set when native is non-human and
        # we ended up resolving against the human index. Lookup is human-only,
        # so this is true iff native_taxon is set and != 9606.
        non_human = native_taxon is not None and native_taxon != 9606

        # 2. HGNC approved symbol exact (best signal — human-curated)
        sym, accs = self._hgnc_symbol_lookup(norm)
        if sym:
            self._fill(res, sym, accs, "exact_gene", is_family=False, non_human=non_human)
            return res

        # 2a. Curated biology aliases (HGNC misses these specific patterns)
        curated = _BIOLOGY_ALIASES.get(norm)
        if curated:
            sym, accs = self._hgnc_symbol_lookup(curated)
            if sym:
                self._fill(res, sym, accs, "curated_alias", is_family=False, non_human=non_human)
                return res

        # 2b. Paralog-default: if `<NAME>1` is approved (e.g. YAP→YAP1, AKT→AKT1),
        # prefer that — and if AKT2/AKT3 also exist, return all as a family.
        # This intentionally fires BEFORE HGNC alias because alias tables
        # sometimes incorrectly point shorthand at unrelated genes
        # (HGNC has YAP→YY1AP1 even though YAP is overwhelmingly used for YAP1).
        if len(norm) >= 3 and not norm[-1].isdigit():
            paralogs: list[str] = []
            for suffix in ("1", "2", "3", "4", "5"):
                candidate = norm + suffix
                if candidate in self._hgnc_symbol_to_uniprot:
                    paralogs.append(candidate)
            for letter_suffix in ("A", "B", "C", "D"):
                candidate = norm + letter_suffix
                if candidate in self._hgnc_symbol_to_uniprot:
                    paralogs.append(candidate)
            if paralogs:
                paralogs = sorted(set(paralogs))
                accs: list[str] = []
                for p in paralogs:
                    _, a = self._hgnc_symbol_lookup(p)
                    accs.extend(a)
                accs = sorted(set(accs))
                if accs:
                    res.human_gene_symbol = paralogs[0]
                    res.candidate_uniprots = accs
                    res.human_uniprot = accs[0]
                    res.match_confidence = "paralog_default" if len(paralogs) == 1 else "family_head"
                    res.is_family_head = len(paralogs) > 1
                    res.is_human_ortholog_mapping = non_human
                    return res

        # 3. HGNC alias
        alias_hits = self._hgnc_alias_to_symbol.get(norm) or []
        if alias_hits:
            # Deduplicate — alias may map to multiple approved symbols.
            unique = sorted(set(alias_hits))
            if len(unique) == 1:
                sym, accs = self._hgnc_symbol_lookup(unique[0])
                if sym:
                    self._fill(res, sym, accs, "hgnc_alias", is_family=False, non_human=non_human)
                    return res
            else:
                # Multiple HGNC symbols claim this alias — treat as family.
                all_accs: list[str] = []
                for s in unique:
                    _, a = self._hgnc_symbol_lookup(s)
                    all_accs.extend(a)
                if all_accs:
                    res.human_gene_symbol = unique[0]  # representative
                    res.candidate_uniprots = sorted(set(all_accs))
                    res.human_uniprot = res.candidate_uniprots[0]
                    res.match_confidence = "hgnc_alias"
                    res.is_family_head = True
                    res.is_human_ortholog_mapping = non_human
                    return res

        # 4. HGNC prev_symbol (deprecated approved symbol)
        prev_hits = self._hgnc_prev_to_symbol.get(norm) or []
        if prev_hits:
            unique = sorted(set(prev_hits))
            sym, accs = self._hgnc_symbol_lookup(unique[0])
            if sym:
                self._fill(res, sym, accs, "hgnc_prev", is_family=False, non_human=non_human)
                return res

        # 5. UniProt gene_symbol exact (fallback: covers genes HGNC has
        # withdrawn or never approved — generally lower confidence than HGNC).
        accs = self._by_gene.get(norm) or []
        if accs:
            primary = self._uniprot_to_gene.get(accs[0], norm)
            res.human_gene_symbol = primary
            res.candidate_uniprots = list(accs)
            res.human_uniprot = accs[0]
            res.match_confidence = "uniprot_gene"
            res.is_family_head = len(accs) > 1
            res.is_human_ortholog_mapping = non_human
            return res

        # 6. UniProtKB-ID (e.g. YAP1_HUMAN)
        acc = self._by_kb_id.get(norm)
        if acc:
            sym = self._uniprot_to_gene.get(acc) or norm.split("_", 1)[0]
            res.human_gene_symbol = sym
            res.candidate_uniprots = [acc]
            res.human_uniprot = acc
            res.match_confidence = "kb_id"
            res.is_human_ortholog_mapping = non_human
            return res

        # 7. Strip mutant suffix and retry exact (HGNC then UniProt)
        stripped = normalize(_strip_mutant(raw_name))
        if stripped and stripped != norm:
            sym, accs = self._hgnc_symbol_lookup(stripped)
            if sym:
                self._fill(res, sym, accs, "stripped_mutant", is_family=False, non_human=non_human)
                return res
            accs = self._by_gene.get(stripped) or []
            if accs:
                primary = self._uniprot_to_gene.get(accs[0], stripped)
                res.human_gene_symbol = primary
                res.candidate_uniprots = list(accs)
                res.human_uniprot = accs[0]
                res.match_confidence = "stripped_mutant"
                res.is_family_head = len(accs) > 1
                res.is_human_ortholog_mapping = non_human
                return res

        # 8. UniProt Gene_Synonym (last reliable tier)
        accs = self._by_synonym.get(norm) or []
        if accs:
            unique_accs = sorted(set(accs))
            primary = self._uniprot_to_gene.get(unique_accs[0])
            res.human_gene_symbol = primary
            res.candidate_uniprots = unique_accs
            res.human_uniprot = unique_accs[0]
            res.match_confidence = "uniprot_synonym"
            res.is_family_head = len(unique_accs) > 1
            res.is_human_ortholog_mapping = non_human
            return res

        # 9. Family-head fuzzy: prefix match on HGNC symbols, length-bounded.
        # Catches "AKT" → AKT1/2/3, "TEAD" → TEAD1-4. Requires len(norm)>=3 to avoid noise.
        if len(norm) >= 3:
            prefix_syms: list[str] = []
            for sym in self._hgnc_symbol_to_uniprot:
                if sym.startswith(norm) and len(sym) <= len(norm) + 2:
                    suffix = sym[len(norm):]
                    # Only treat as paralog if suffix is a small integer (1-9, 10-19) or
                    # a single capital letter (A, B, C). Avoids "RPA" matching "RPAP3".
                    if re.fullmatch(r"\d{1,2}|[A-Z]", suffix):
                        prefix_syms.append(sym)
            if prefix_syms:
                prefix_syms.sort()
                accs: list[str] = []
                for s in prefix_syms:
                    _, a = self._hgnc_symbol_lookup(s)
                    accs.extend(a)
                accs = sorted(set(accs))
                if accs:
                    res.human_gene_symbol = prefix_syms[0]
                    res.candidate_uniprots = accs
                    res.human_uniprot = accs[0]
                    res.match_confidence = "family_head"
                    res.is_family_head = True
                    res.is_human_ortholog_mapping = non_human
                    return res

        # Unresolved.
        return res

    # ------------------------------------------------------------------
    def _fill(
        self,
        res: Resolution,
        symbol: str,
        accs: list[str],
        tier: str,
        is_family: bool,
        non_human: bool,
    ) -> None:
        """Helper for the common single-symbol resolution path."""
        unique_accs = sorted(set(accs))
        res.human_gene_symbol = symbol
        res.candidate_uniprots = unique_accs
        res.human_uniprot = unique_accs[0] if unique_accs else None
        res.match_confidence = tier
        res.is_family_head = is_family or len(unique_accs) > 1
        res.is_human_ortholog_mapping = non_human


# ---------------------------------------------------------------------------
# Module-level lazy singleton (shared by graph tools, DepMap tools, etc.)
# ---------------------------------------------------------------------------

_DEFAULT_IDMAPPING = (
    Path(__file__).resolve().parent.parent / "data" / "depmap" / "HUMAN_9606_idmapping.dat.gz"
)
_DEFAULT_HGNC = (
    Path(__file__).resolve().parent.parent / "data" / "depmap" / "hgnc_complete_set.tsv"
)
_NORMALIZER: IdentifierNormalizer | None = None


def get_normalizer() -> IdentifierNormalizer:
    """Return a process-wide :class:`IdentifierNormalizer` (built on first call)."""
    global _NORMALIZER
    if _NORMALIZER is None:
        _NORMALIZER = IdentifierNormalizer(_DEFAULT_IDMAPPING, _DEFAULT_HGNC)
    return _NORMALIZER


# ---------------------------------------------------------------------------
# Fingerprint walking — extract every (raw_name, source_path) tuple
# ---------------------------------------------------------------------------


def extract_protein_occurrences(fp: dict) -> list[tuple[str, str]]:
    """Return [(raw_name, source_path), ...] for every protein-name slot in a fingerprint.

    Sites:
      - key_findings[i].protein_pair[j]
      - pathway_context.target_nodes[i].protein
      - pathway_context.upstream_regulators[i]
      - pathway_context.downstream_effectors[i]
      - entities.proteins[i]

    Compound names like "PD-1/PD-L1" are split on '/' into separate tuples
    (each carrying the same source_path).
    """
    out: list[tuple[str, str]] = []
    for i, kf in enumerate(fp.get("key_findings") or []):
        pair = kf.get("protein_pair") or []
        for j, name in enumerate(pair):
            for piece in split_compound(name):
                if piece:
                    out.append((piece, f"key_findings[{i}].protein_pair[{j}]"))
    pc = fp.get("pathway_context") or {}
    for i, tn in enumerate(pc.get("target_nodes") or []):
        name = tn.get("protein") if isinstance(tn, dict) else None
        for piece in split_compound(name):
            if piece:
                out.append((piece, f"pathway_context.target_nodes[{i}].protein"))
    for i, name in enumerate(pc.get("upstream_regulators") or []):
        for piece in split_compound(name):
            if piece:
                out.append((piece, f"pathway_context.upstream_regulators[{i}]"))
    for i, name in enumerate(pc.get("downstream_effectors") or []):
        for piece in split_compound(name):
            if piece:
                out.append((piece, f"pathway_context.downstream_effectors[{i}]"))
    ents = fp.get("entities") or {}
    for i, name in enumerate(ents.get("proteins") or []):
        for piece in split_compound(name):
            if piece:
                out.append((piece, f"entities.proteins[{i}]"))
    return out


_PARALOG_DIGIT_RE = re.compile(r"^(.+?\D)(\d+)$")


def split_compound(name: str | None) -> list[str]:
    """Split compound protein names into individual components.

    Handles three patterns:
      - Full names separated by ``/``: ``"PD-1/PD-L1"`` -> ``["PD-1", "PD-L1"]``
      - Paralog shorthand with shared stem: ``"MEK1/2"`` -> ``["MEK1", "MEK2"]``,
        ``"Erk 1/2"`` -> ``["Erk 1", "Erk 2"]``. The stem is taken from the
        first component up to (but not including) its trailing digit.
      - Single bare names pass through unchanged.

    Returned components are not normalised — that's the resolver's job. The
    splitter only handles tokenisation.
    """
    if not name or "/" not in name:
        return [name] if name else []
    parts = [p.strip() for p in re.split(r"\s*/\s*", name) if p.strip()]

    # Detect paralog shorthand: at least one short numeric tail.
    # Example shapes that should expand:
    #   ["MEK1", "2"] -> ["MEK1", "MEK2"]
    #   ["LATS1", "2"] -> ["LATS1", "LATS2"]
    #   ["Erk 1", "2"] -> ["Erk 1", "Erk 2"]
    if len(parts) >= 2 and parts[0]:
        first = parts[0]
        m = _PARALOG_DIGIT_RE.match(first)
        if m:
            stem, _ = m.group(1), m.group(2)
            expanded = [first]
            ok = True
            for p in parts[1:]:
                if p.isdigit():
                    expanded.append(stem + p)
                elif len(p) >= 2:
                    # Mixed compound (e.g. "PD-1/PD-L1") — fall through to
                    # the standard length filter below.
                    ok = False
                    break
                else:
                    ok = False
                    break
            if ok:
                return expanded

    out = [p for p in parts if len(p) >= 2]
    return out or ([name] if name else [])
