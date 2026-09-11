"""Does this campaign name a Federal Select Agent or Toxin?

A binder campaign against a select agent is not forbidden — plenty of
legitimate countermeasure work targets exactly these molecules — but in the US
it is regulated, and the operator needs institutional approval before it
starts rather than after a multi-day GPU run. So this screens the names a
campaign already knows and, on a hit, raises a manifest checkpoint and says so
loudly. **It never blocks a run.**

## Why a select-agent list, and not a "viral targets" list

The obvious-sounding filter — refuse viral targets — is inverted relative to
the risk and unenforceable besides:

* A binder against a viral protein IS an antiviral. Anti-spike nanobodies and
  nirsevimab (RSV F) are the beneficial application class; excluding viral
  targets removes them and closes no misuse path.
* "Viral protein" does not partition cleanly — host/virus complexes, viral
  mimicry of host folds, endogenous retroviral proteins.
* `--workflow structure` accepts any local file, so an input-side *block* is
  bypassed by renaming one. A control that can be sidestepped that easily is
  worse than none, because it invites reliance.

The select-agent list, by contrast, is short, public, stable, and legally
meaningful: a hit means "this needs review", which is a true statement.

## What this screen can and cannot do

It reads TEXT — the user's query, the target complex, the RCSB entry title and
chain descriptions. It is a **prompt for review, not a containment control**:
a name that is not written down is not screened, and nothing here inspects a
sequence or a structure. Treat a clean result as "no named select agent was
mentioned", never as "this target is cleared".

Toxins are matched by MOLECULE name, not by organism, which is why the screen
takes text rather than an organism field: a ricin structure's source organism
is *Ricinus communis* (castor bean), which is not on the list — the toxin is.

## Keeping the list current

Source: https://www.selectagents.gov/sat/list.htm — HHS/USDA, last reviewed
2025-01-14, transcribed 2026-09-11. **Verify it against that page before
relying on it**; the list is amended by rule and this copy will go stale.
`SOURCE_REVIEWED` is reported in the checkpoint payload so a run records which
vintage it was screened against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

SOURCE_URL = "https://www.selectagents.gov/sat/list.htm"
SOURCE_REVIEWED = "2025-01-14"
TRANSCRIBED = "2026-09-11"

HHS = "hhs"
OVERLAP = "overlap"
USDA_VS = "usda_veterinary"
USDA_PPQ = "usda_plant"


@dataclass(frozen=True)
class Agent:
    """One list entry.

    `patterns` is ANY-of; each inner tuple is ALL-of, so a binomial can demand
    both genus and species while a distinctive single word stands alone.
    `excluded_if` suppresses a match — the one case that genuinely needs it is
    SARS-CoV-2, which is NOT a select agent even though SARS-CoV is.
    """

    name: str
    category: str
    patterns: tuple[tuple[str, ...], ...]
    excluded_if: tuple[str, ...] = ()


#: Transcribed from SOURCE_URL. Patterns are hand-chosen to be distinctive:
#: the cost of a false positive is one operator confirmation, the cost of
#: noise is that the check gets ignored, so a pattern that would fire on
#: routine work (bare "influenza", bare "toxin") is deliberately narrowed.
AGENTS: tuple[Agent, ...] = (
    # HHS select agents and toxins
    Agent("Abrin", HHS, (("abrin",),)),
    Agent("Bacillus cereus Biovar anthracis", HHS,
          (("bacillus cereus", "anthracis"),)),
    Agent("Botulinum neurotoxins", HHS, (("botulinum",),)),
    Agent("Botulinum neurotoxin producing species of Clostridium", HHS,
          (("clostridium botulinum",),)),
    Agent("Short, paralytic alpha conotoxins", HHS, (("conotoxin",),)),
    Agent("Coxiella burnetii", HHS, (("coxiella",),)),
    Agent("Crimean-Congo haemorrhagic fever virus", HHS,
          (("crimean congo",), ("cchfv",))),
    Agent("Diacetoxyscirpenol", HHS, (("diacetoxyscirpenol",),)),
    Agent("Eastern equine encephalitis virus", HHS,
          (("eastern equine encephalitis",), ("eeev",))),
    Agent("Ebolavirus", HHS, (("ebolavirus",), ("ebola",))),
    Agent("Francisella tularensis", HHS, (("francisella",), ("tularensis",))),
    Agent("Lassa fever virus", HHS, (("lassa",),)),
    Agent("Lujo virus", HHS, (("lujo virus",),)),
    Agent("Marburg virus", HHS, (("marburg",),)),
    Agent("Monkeypox virus", HHS, (("monkeypox",), ("mpox",))),
    Agent("Reconstructed 1918 pandemic influenza virus", HHS,
          (("1918", "influenza"),)),
    Agent("Ricin", HHS, (("ricin",),)),
    Agent("Rickettsia prowazekii", HHS, (("prowazekii",),)),
    Agent("SARS-CoV", HHS,
          (("sars cov",), ("sars coronavirus",),
           ("severe acute respiratory syndrome coronavirus",)),
          # SARS-CoV-2 is NOT a select agent; only SARS-CoV/SARS-CoV-2
          # CHIMERAS are. Without this every COVID structure would fire, and a
          # check that fires on routine work is a check nobody reads.
          excluded_if=("sars cov 2", "sars cov2", "coronavirus 2", "covid",
                       "2019 ncov", "ncov 2019")),
    Agent("SARS-CoV/SARS-CoV-2 chimeric viruses", HHS,
          (("sars", "chimeric"), ("sars", "chimera"))),
    Agent("Saxitoxin", HHS, (("saxitoxin",),)),
    Agent("Chapare virus", HHS, (("chapare",),)),
    Agent("Guanarito virus", HHS, (("guanarito",),)),
    Agent("Junin virus", HHS, (("junin",), ("junín",))),
    Agent("Machupo virus", HHS, (("machupo",),)),
    Agent("Sabia virus", HHS, (("sabia virus",),)),
    Agent("Staphylococcal enterotoxins (A, B, C, D, E)", HHS,
          (("staphylococcal enterotoxin",),)),
    Agent("T-2 toxin", HHS, (("t 2 toxin",),)),
    Agent("Tetrodotoxin", HHS, (("tetrodotoxin",),)),
    Agent("Tick-borne encephalitis virus (Far Eastern / Siberian subtype)", HHS,
          (("tick borne encephalitis",),)),
    Agent("Kyasanur Forest disease virus", HHS, (("kyasanur",),)),
    Agent("Omsk hemorrhagic fever virus", HHS, (("omsk",),)),
    Agent("Variola virus (smallpox)", HHS, (("variola",), ("smallpox",))),
    Agent("Yersinia pestis", HHS, (("yersinia pestis",), ("pestis",))),
    # Overlap select agents and toxins (HHS + USDA)
    Agent("Bacillus anthracis", OVERLAP,
          (("bacillus anthracis",), ("anthracis",), ("anthrax",))),
    Agent("Burkholderia mallei", OVERLAP, (("burkholderia mallei",), ("mallei",))),
    Agent("Burkholderia pseudomallei", OVERLAP, (("pseudomallei",),)),
    Agent("Hendra virus", OVERLAP, (("hendra",),)),
    Agent("Nipah virus", OVERLAP, (("nipah",),)),
    Agent("Rift Valley fever virus", OVERLAP, (("rift valley",),)),
    Agent("Venezuelan equine encephalitis virus", OVERLAP,
          (("venezuelan equine encephalitis",), ("veev",))),
    # USDA Veterinary Services select agents and toxins
    Agent("African swine fever virus", USDA_VS, (("african swine fever",),)),
    Agent("Avian influenza virus", USDA_VS, (("avian influenza",),)),
    Agent("Classical swine fever virus", USDA_VS, (("classical swine fever",),)),
    Agent("Foot-and-mouth disease virus", USDA_VS,
          (("foot and mouth disease",), ("fmdv",))),
    Agent("Goat pox virus", USDA_VS, (("goat pox",), ("goatpox",))),
    Agent("Lumpy skin disease virus", USDA_VS, (("lumpy skin disease",),)),
    Agent("Mycoplasma capricolum", USDA_VS, (("capricolum",),)),
    Agent("Mycoplasma mycoides", USDA_VS, (("mycoides",),)),
    Agent("Newcastle disease virus", USDA_VS, (("newcastle",),)),
    Agent("Peste des petits ruminants virus", USDA_VS, (("peste des petits",),)),
    Agent("Rinderpest virus", USDA_VS, (("rinderpest",),)),
    Agent("Sheep pox virus", USDA_VS, (("sheep pox",), ("sheeppox",))),
    Agent("Swine vesicular disease virus", USDA_VS, (("swine vesicular",),)),
    # USDA Plant Protection and Quarantine select agents and toxins
    Agent("Coniothyrium glycines", USDA_PPQ, (("coniothyrium",),)),
    Agent("Ralstonia solanacearum", USDA_PPQ, (("ralstonia solanacearum",),)),
    Agent("Rathayibacter toxicus", USDA_PPQ, (("rathayibacter",),)),
    Agent("Sclerophthora rayssiae", USDA_PPQ, (("sclerophthora",),)),
    Agent("Synchytrium endobioticum", USDA_PPQ, (("synchytrium",),)),
    Agent("Xanthomonas oryzae", USDA_PPQ, (("xanthomonas oryzae",),)),
)


def _norm(text: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed.

    Hyphens and slashes become spaces so "SARS-CoV-2", "foot-and-mouth" and
    "T-2 toxin" all normalise to the spaced forms the patterns are written in.
    """
    return re.sub(r"\s+", " ",
                  re.sub(r"[^a-z0-9à-ÿ]+", " ",
                         str(text or "").lower())).strip()


def _contains(norm: str, phrase: str) -> bool:
    """Phrase match on word boundaries, tolerating a plural on the last word.

    Boundaries are what keep "ricin" out of "ricinus" (castor bean, which is
    not a select agent) and "mallei" out of "pseudomallei" — a substring test
    would conflate both pairs.
    """
    return re.search(
        rf"(?<![a-z0-9]){re.escape(phrase)}(?:es|s)?(?![a-z0-9])", norm
    ) is not None


def screen(texts: Iterable[str]) -> list[dict]:
    """Select agents named anywhere in `texts`.

    Returns `[{"name", "category", "matched", "where"}]`, empty when nothing
    matched. `where` is the first text that hit, truncated — enough for a log
    line to say why it fired without reproducing a whole entry title.
    """
    fields = [(str(t), _norm(t)) for t in texts if t and str(t).strip()]
    if not fields:
        return []

    hits: list[dict] = []
    for agent in AGENTS:
        for raw, norm in fields:
            if any(_contains(norm, ex) for ex in agent.excluded_if):
                continue
            matched = next(
                (pattern for pattern in agent.patterns
                 if all(_contains(norm, phrase) for phrase in pattern)),
                None)
            if matched:
                hits.append({
                    "name": agent.name,
                    "category": agent.category,
                    "matched": " + ".join(matched),
                    "where": raw[:160],
                })
                break
    return hits


def describe(hits: list[dict]) -> str:
    """Multi-line operator-facing summary. Empty string when nothing matched."""
    if not hits:
        return ""
    lines = [
        f"{len(hits)} Federal Select Agent/Toxin name(s) appear in this "
        f"campaign's target description:",
        "",
    ]
    for h in hits:
        lines.append(f"  * {h['name']}  [{h['category']}]  "
                     f"— matched {h['matched']!r} in {h['where']!r}")
    lines += [
        "",
        "This does NOT stop the run, and such work is often legitimate "
        "countermeasure research.",
        "It does mean US possession/use/transfer of the agent is regulated "
        "(42 CFR 73 / 9 CFR 121 / 7 CFR 331), so confirm you have "
        "institutional approval BEFORE synthesising anything.",
        f"List source: {SOURCE_URL} (last reviewed {SOURCE_REVIEWED}); "
        f"verify it — this copy was transcribed {TRANSCRIBED}.",
        "Name screening only: a target that does not name itself is not "
        "screened. See docs/responsible-use.md.",
    ]
    return "\n".join(lines)
