"""
The LLM-facing shape of a structure-tool result.

`src/structure_tools.py` is the pure-numeric layer, and its return shapes are
consumed by real Python callers (`structure_trim`, `cluster_runner`,
`pipeline_runner`, `binder_report`, the tests). Nothing here changes those.
What it changes is the JSON handed to a model, which has a different
constraint: every byte of a tool result is re-sent on every subsequent call in
the stage, so a field the prompt never reads is paid for many times over.

Shared by BOTH transports on purpose — `skill_runner` (CLI/API) and
`structure_tools_server` (MCP) — the same way `src/_path_resolve.py` is. They
had already drifted once: `get_fingerprint` stripped three blocks on one
transport and nothing on the other, so one skill saw different fields depending
on how it was invoked.

Two rules, learned the hard way:

1. **Never drop a field a SKILL.md names.** `binder-optimizer` reads
   `one_letter` and `hydrophobicity` off each interface residue by name. A
   model that stops receiving a field it was told to expect does not report the
   gap — it treats absence as a fact.

2. **Derivable is not the same as droppable.** `hydrophobicity` is a pure
   Kyte-Doolittle lookup on the residue name, so it is redundant in the
   information-theoretic sense. It stays anyway: the alternative is the model
   recalling the KD scale from memory while choosing hydrophobic hotspots,
   which is precisely the hallucination this pipeline guards against
   everywhere else. It costs ~4%.

So what actually goes is narrow: four constants and two intermediates whose
derived value is retained. Measured on 5GRS chains A/I (61 + 51 interface
residues), the largest real payload seen:

    as sent before (indent=2)            114,425   100%
    compact JSON, nothing dropped         68,707    60%
    + the fields below                    55,719    49%

40% of it was whitespace.
"""

from __future__ import annotations

import json
from typing import Any

#: Constant per-entry chain labels. Each is the chain the surrounding object
#: already names, repeated once per residue — and `target_chain` once per
#: CONTACT, which on a 61-residue interface is ~1,100 copies of one letter.
_CONSTANT_CHAIN_KEYS = ("chain", "target_chain")

#: The two absolute SASA values whose difference is `bsa_A2`. `bsa_A2` is kept
#: and is what every prompt reasons about; no SKILL.md names either of these.
_REDUNDANT_SASA_KEYS = ("sasa_free_A2", "sasa_complex_A2")


def llm_view_interface(result: dict) -> dict:
    """`analyze_interface` as a model should see it. Same keys, less repetition."""
    if not isinstance(result, dict) or "error" in result:
        return result
    out = dict(result)

    iface = out.get("interface")
    if isinstance(iface, dict):
        iface = dict(iface)
        per_res = iface.get("bsa_per_residue")
        if isinstance(per_res, list):
            iface["bsa_per_residue"] = [
                {k: v for k, v in e.items()
                 if k not in _REDUNDANT_SASA_KEYS and k not in _CONSTANT_CHAIN_KEYS}
                for e in per_res if isinstance(e, dict)
            ]
        out["interface"] = iface

    for key in ("chain_a_interface_residues", "chain_b_interface_residues"):
        rows = out.get(key)
        if not isinstance(rows, list):
            continue
        slim = []
        for r in rows:
            if not isinstance(r, dict):
                slim.append(r)
                continue
            r = {k: v for k, v in r.items() if k not in _CONSTANT_CHAIN_KEYS}
            contacts = r.get("contacts")
            if isinstance(contacts, list):
                r["contacts"] = [
                    {k: v for k, v in c.items() if k not in _CONSTANT_CHAIN_KEYS}
                    if isinstance(c, dict) else c
                    for c in contacts
                ]
            slim.append(r)
        out[key] = slim
    return out


def llm_view_sequence_map(result: dict) -> dict:
    """
    `get_sequence_map` unchanged for now.

    Its `residues[]` list is 81% of the payload and restates what `sequence`
    plus the two numbering maps already carry, with no Python consumer and no
    SKILL.md reference — so it looks like the obvious cut. It is not taken yet
    on purpose: models have twice produced systematically wrong `label_seq_id`
    values by COUNTING (23/23 on 5GRS, 10/10 on 5GN0) rather than reading
    `auth_to_label`, and `residues[]` is the direct per-residue lookup that
    makes the correct answer easiest to reach. Removing it plausibly makes that
    worse. Decide it with a measurement — re-run the stage both ways and diff
    the MODEL-READY HOTSPOTS tables — not with a byte count.
    """
    return result


def dumps(obj: Any) -> str:
    """
    Compact JSON for a model.

    `indent=2` costs 40% of these payloads and buys nothing: no model needs
    pretty-printing to parse JSON, and those bytes are re-sent on every
    subsequent call in the stage.
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
