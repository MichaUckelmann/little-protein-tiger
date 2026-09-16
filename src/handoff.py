"""
Parse the '### PIPELINE HANDOFF' key:value blocks and 'MODEL-READY HOTSPOTS'
tables that every LLM skill stage emits at the end of its markdown report.

Extracted from ``PipelineRunner`` (which still exposes ``_parse_handoff`` /
``_parse_hotspot_residues`` as thin wrappers around these) so that
``src/binder_report.py`` — a deterministic, standalone report generator with
no reason to depend on the whole pipeline-runner state machine — can read the
same stage output without duplicating the parsing logic.
"""

from __future__ import annotations

import json
import re

from loguru import logger


def parse_handoff(text: str) -> dict[str, str]:
    """
    Extract key:value fields from a '### PIPELINE HANDOFF' block.

    Accepts both canonical format ('- key: value') and bare format
    ('key: value'), and strips markdown code fences that models
    sometimes wrap the block in.

    Stops at the next markdown heading or end of string.
    Returns {} if no block is found (non-fatal).
    """
    match = re.search(
        r"###\s+PIPELINE HANDOFF\s*\n(.*?)(?=\n##|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        # Strip code-fence lines (``` or ~~~)
        if re.match(r"^\s*```", line) or re.match(r"^\s*~~~", line):
            continue
        # Accept '- key: value' (canonical) or 'key: value' (bare)
        m = re.match(r"^\s*(?:-\s+)?(\w+):\s*(.+)$", line)
        if m:
            fields[m.group(1).strip()] = m.group(2).strip()
    return fields


def _clean_atom_list(raw: str) -> str:
    """
    Strip explanatory prose from a "RFD3 sidechain atoms" table cell.

    A backbone-only hotspot (GLY, or a residue contacted only via CA/CB) is
    real and legitimate, and the model sometimes annotates it inline —
    "CA (no sidechain — backbone contact only)" — rather than leaving the
    cell as a bare atom list. RFD3's select_hotspots takes that string
    verbatim as an atom name, so "CA (no sidechain..." reaches validate_spec
    as a single malformed atom and fails a real design (observed on a PD-L1
    GLY119/ALA121 hotspot). Keep only the leading comma-separated token(s)
    that look like atom names; drop everything from the first non-atom-name
    character on.
    """
    head = raw.split("(")[0]
    tokens = [t.strip() for t in head.split(",")]
    atoms = [t for t in tokens if re.fullmatch(r"[A-Z][A-Z0-9]{0,3}", t)]
    return ",".join(atoms) if atoms else raw.strip()


#: A region heading looks like
#: "Target chain A — Region 1: Central Hydrophobic Core — selected 9 of 18 ..."
#: A glue pocket is a region under another name, and the STABILIZE template
#: writes it inside brackets ("[STABILIZE — Glue Pocket 1]"), so `]` has to
#: terminate the label or it is captured as part of it.
_REGION_LABEL = re.compile(r"((?:Region|Glue\s+Pocket)\s*\d+[^\n\]—-]*)",
                           re.IGNORECASE)

#: "Primary target: Region 1 (Central Hydrophobic Core) — Excellent"
_PRIMARY_REGION = re.compile(
    r"Primary target:\s*(?:Region|Glue\s+Pocket)\s*(\d+)", re.IGNORECASE)

#: A per-chain sub-heading inside ONE hotspot section. Seen on disk as
#: "Chain A (GLP-1R) periinterface patch", "Chain B / U (PTPN14) ..." and
#: "Target chain A — Region 1: operator-specified". Deliberately matches the
#: bare "Chain <id>" form with no colon, because that is what every report
#: already on disk writes and `_correct_label_seq_ids` re-parses those on
#: every `--start-from trim`.
_CHAIN_HEADING = re.compile(
    r"^[ \t]*(?:Target\s+chain|Chain)\s+([A-Za-z0-9]{1,4})\b",
    re.MULTILINE | re.IGNORECASE,
)

#: `| PHE | 314 | 122 | CD2,CZ |` and `| PHE314 | 314 | 122 | CD2,CZ |`
_ROW = re.compile(r"^\|\s*([A-Z]{3})\d*\s*\|\s*(\d+)\s*\|",
                  re.MULTILINE)


def _declared_chains(handoff: dict) -> tuple[str, str]:
    """The (target, partner) chain ids the handoff declares, as written."""
    target = handoff.get("target_chain", "") or handoff.get("chain_a", "")
    partner = handoff.get("partner_chain", "") or handoff.get("chain_b", "")
    return target.strip(), partner.strip()


def _resolve_heading_chain(token: str, handoff: dict, default: str) -> str:
    """Map a chain-heading token onto a real chain id from the handoff.

    A STABILIZE report is routinely internally inconsistent about chain
    naming: `div_standard_diabetes` heads its two sub-tables "Chain A" and
    "Chain B" while the structure's real ids — and its own `select_hotspots`
    block — are R and P. So a token is read LITERALLY when it names a chain
    the handoff declared, and POSITIONALLY otherwise (A = target, B = partner),
    which is what those reports mean by it.

    The literal-before-positional order is only reachable from
    `chain_blocks`, and only for a section carrying two or more distinct
    headings — see that function for why that matters.
    """
    tok = token.strip().upper()
    target, partner = _declared_chains(handoff)
    if target and tok == target.upper():
        return target
    if partner and tok == partner.upper():
        return partner
    if tok == "A" and target:
        return target
    if tok == "B" and partner:
        return partner
    return token.strip() or default


def chain_blocks(section: str, handoff: dict,
                 default: str) -> list[tuple[str, str]]:
    """Split ONE hotspot section into (chain, text) blocks.

    A molecular-glue pocket spans both proteins, so its table is written as
    two chain-headed sub-tables inside a single `### MODEL-READY HOTSPOTS`
    section. Every row still has to be attributed to the chain it is
    actually on: on 5VAI the four chain-P rows are real residues whose
    numbers ALSO exist on chain R under different names, so mis-attributing
    them produces a table that grounds, trims and validates against the
    wrong molecule.

    **Splits only when the section carries two or more DISTINCT chain
    headings.** One heading, or none, returns a single block under `default`
    and never consults `_resolve_heading_chain` at all. That is what keeps
    the single-chain path byte-identical by construction rather than by
    survey: 65 of 108 shipped reports declare a `partner_chain` of literally
    "A" or "B" that differs from their `target_chain`, so a positional
    reading of a lone legacy "Chain A" heading would resolve to the PARTNER.
    Measured over every stage report on disk: 4 of 162 hotspot sections carry
    two or more headings, and all four are the two glue runs.

    Public because `pipeline_runner._correct_label_seq_ids` must split on the
    identical rule — the `handoff.py` pairing in CLAUDE.md's "Common file
    pairs" is exactly this: the parse lives once.
    """
    marks = list(_CHAIN_HEADING.finditer(section))
    if len({m.group(1).upper() for m in marks}) < 2:
        return [(default, section)]

    blocks: list[tuple[str, str]] = []
    head = section[:marks[0].start()]
    if _ROW.search(head):
        blocks.append((default, head))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(section)
        blocks.append((_resolve_heading_chain(m.group(1), handoff, default),
                       section[m.start():end]))
    return blocks


def _region_label(section: str) -> str:
    m = _REGION_LABEL.search(section)
    return m.group(1).strip() if m else ""


def _section_residue_ids(section: str) -> list[str]:
    return [f"{m.group(1)}{m.group(2)}" for m in _ROW.finditer(section)]


def _primary_section(sections: list[str], text: str) -> str:
    """The region the report itself calls primary, else the first.

    The explicit statement is preferred over position because a model that
    lists regions out of rank order would otherwise hand the campaign its
    second choice — and position is only a proxy for ranking.
    """
    m = _PRIMARY_REGION.search(text)
    if m:
        wanted = re.compile(
            rf"(?:Region|Glue\s+Pocket)\s*{int(m.group(1))}\b", re.IGNORECASE)
        for sec in sections:
            if wanted.search(sec):
                return sec
    return sections[0]


def parse_hotspot_residues(text: str, handoff: dict) -> str | None:
    """
    Parse the MODEL-READY HOTSPOTS table(s) from structure stage output.

    Returns a JSON string:
        {"target_chain": "A", "partner_chain": "B",
         "region": "Region 1: Central Hydrophobic Core",
         "regions_declared": 2,
         "residues": [{"residue": "LEU", "auth_seq_id": 245,
                       "label_seq_id": 245, "rfd3_atoms": "CD1,CG2"}, ...]}

    Returns None if the section is absent (non-fatal).

    **ONE region reaches the spec, not all of them.** The structure skill
    emits a `### MODEL-READY HOTSPOTS` section PER REGION, ranked, each
    capped at `foundry_spec.MAX_HOTSPOTS` (12) and each labelled
    "Separability: Independent — separate design submission required". This
    used to concatenate every section, which is how one 3KYS run reached
    `build_rfd3_spec` with **16 hotspots against the 12 cap**: Region 1's 9
    plus Region 2's 7, two patches ~20 A apart fused into a single declared
    epitope. The consequences are not just a warning:

    * RFD3 conditions on the union, so it is steered at no one site.
    * `hotspot_engagement` is a FRACTION of the declared set with a 0.75 gate,
      so a binder docked perfectly on the primary region scores 9/16 = 0.56
      and is REJECTED for missing residues it was never meant to touch.
    * More hotspots is not stricter — RFD3's hit rate falls as the declared
      set grows (see CLAUDE.md), so merging weakens the gate twice over.

    The region used is the one the report names as primary ("Primary target:
    Region N" in DESIGN RECOMMENDATIONS) when that resolves to a section, and
    otherwise the FIRST section, because the skill emits them ranked by
    suitability. Dropped regions are logged with their residues — they stay
    in the report for a human, and designing against one is a separate
    campaign (`--hotspots`, or a second run).
    """
    # target_chain / partner_chain were added to the PIPELINE HANDOFF
    # template after some runs were created.  Fall back to chain_a / chain_b
    # for older runs that only emitted those fields.
    target_chain, partner_chain = _declared_chains(handoff)

    sections = re.findall(
        r"###\s+MODEL.READY HOTSPOTS.*?(?=\n###|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not sections:
        return None

    regions_declared = len(sections)
    chosen = _primary_section(sections, text)
    if regions_declared > 1:
        logger.warning(
            f"the interface stage declared {regions_declared} independent "
            f"hotspot regions; designing against "
            f"{_region_label(chosen) or 'the primary one'} and DROPPING the "
            f"rest — "
            + "; ".join(
                f"{_region_label(sec) or f'section {i + 1}'}: "
                f"{', '.join(_section_residue_ids(sec)) or 'no parsable rows'}"
                for i, sec in enumerate(sections) if sec is not chosen)
            + ". They are separate campaigns: each region is independently "
              "separable, and merging them would steer RFD3 at no one site "
              "and make hotspot_engagement unsatisfiable. Re-run with "
              "--hotspots to design against another region.")
    sections = [chosen]

    # label_seq_id is allowed to be non-integer (e.g. "UNVERIFIED" or
    # similar when tool_get_sequence_map could not be called). Match any
    # non-pipe content and try to parse as int; fall back to auth_seq_id
    # if it isn't a number. Only auth_seq_id is required to be an int.
    row_pat = re.compile(
        r"^\|\s*([A-Z]+)\d*\s*\|\s*(\d+)\s*\|\s*([^|]*?)\s*\|\s*([^|]+?)\s*\|",
        re.MULTILINE,
    )
    residues: list[dict] = []
    seen: set[tuple] = set()
    for chain, section in chain_blocks(chosen, handoff, target_chain or "A"):
        for m in row_pat.finditer(section):
            residue, auth_id, label_raw, atoms = m.groups()
            auth_id_int = int(auth_id)
            try:
                label_id_int = int(label_raw.strip())
            except (ValueError, AttributeError):
                # Non-numeric label (e.g. "**UNVERIFIED**") — fall back to
                # auth_seq_id. Downstream SASA enrichment and every RFD3
                # spec use auth_seq_id anyway (RFD3 resolves against its
                # loader's `res_id`, which for the `trimmed.pdb` it is given
                # IS the author numbering — see CLAUDE.md).
                #
                # Two things still read the label column, so the fallback is
                # not free: `ppi_report` highlights a legacy-BoltzGen design
                # refold by it (that engine's output carries the input
                # label_seq as its auth_seq_id), and the legacy design-script
                # skill writes `binding:` from its own copy of the table. The
                # DETERMINISTIC BoltzGen builder does not — `boltzgen_spec.
                # resolve_binding` recomputes the index from auth_seq_id
                # against the file that will appear in `path:` and grounds it
                # by residue name. Normally moot, because
                # `_resolve_unverified_label_seq_ids` overwrites the column
                # from gemmi and hard-fails if it cannot.
                label_id_int = auth_id_int
            # Keyed on the chain too: a glue table legitimately carries the
            # same residue name at the same author number on both chains, and
            # a chain-less key silently collapses them into one hotspot.
            key = (chain, residue, auth_id_int)
            if key not in seen:
                seen.add(key)
                residues.append({
                    "residue": residue,
                    "auth_seq_id": auth_id_int,
                    "label_seq_id": label_id_int,
                    "rfd3_atoms": _clean_atom_list(atoms),
                    "chain": chain,
                })

    if not residues:
        return None

    # First-appearance order of the chains that actually carry rows. For a
    # single-chain table this is exactly [target_chain]; for a glue pocket it
    # is both, and it is what tells every downstream stage that this target
    # has a co-target at all.
    target_chains: list[str] = []
    for r in residues:
        if r["chain"] not in target_chains:
            target_chains.append(r["chain"])
    if target_chain and target_chain not in target_chains:
        # Not fatal here: _verify_hotspot_grounding owns the refusal, and it
        # can say which residue is on which chain. Warning rather than
        # raising also keeps a legacy report readable by the report builders.
        logger.warning(
            f"the declared target_chain {target_chain!r} carries no hotspot "
            f"rows; the table attributes its residues to "
            f"{', '.join(target_chains)}")

    return json.dumps({
        "target_chain": target_chain,
        "partner_chain": partner_chain,
        "target_chains": target_chains,
        "region": _region_label(chosen),
        "regions_declared": regions_declared,
        "residues": residues,
    })
