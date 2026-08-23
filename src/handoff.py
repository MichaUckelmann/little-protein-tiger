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


def parse_hotspot_residues(text: str, handoff: dict) -> str | None:
    """
    Parse the MODEL-READY HOTSPOTS table(s) from structure stage output.

    Returns a JSON string:
        {"target_chain": "A", "partner_chain": "B",
         "residues": [{"residue": "LEU", "auth_seq_id": 245,
                       "label_seq_id": 245, "rfd3_atoms": "CD1,CG2"}, ...]}

    Returns None if the section is absent (non-fatal).
    """
    # target_chain / partner_chain were added to the PIPELINE HANDOFF
    # template after some runs were created.  Fall back to chain_a / chain_b
    # for older runs that only emitted those fields.
    target_chain = handoff.get("target_chain", "") or handoff.get("chain_a", "")
    partner_chain = handoff.get("partner_chain", "") or handoff.get("chain_b", "")

    sections = re.findall(
        r"###\s+MODEL.READY HOTSPOTS.*?(?=\n###|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not sections:
        return None

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
    for section in sections:
        for m in row_pat.finditer(section):
            residue, auth_id, label_raw, atoms = m.groups()
            auth_id_int = int(auth_id)
            try:
                label_id_int = int(label_raw.strip())
            except (ValueError, AttributeError):
                # Non-numeric label (e.g. "**UNVERIFIED**") — fall back to
                # auth_seq_id. Downstream SASA enrichment uses auth_seq_id
                # anyway; label_seq_id is only needed for BoltzGen YAML
                # `binding:` lines, and those are written by the
                # design-script skill from its own copy of the table.
                label_id_int = auth_id_int
            key = (residue, auth_id_int)
            if key not in seen:
                seen.add(key)
                residues.append({
                    "residue": residue,
                    "auth_seq_id": auth_id_int,
                    "label_seq_id": label_id_int,
                    "rfd3_atoms": _clean_atom_list(atoms),
                })

    if not residues:
        return None

    return json.dumps({
        "target_chain": target_chain,
        "partner_chain": partner_chain,
        "residues": residues,
    })
