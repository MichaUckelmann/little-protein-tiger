"""Does a paper's licence permit redistributing a derivative of it?

A fingerprint is extracted from a paper's content, so the paper's reuse licence
is what decides whether that fingerprint can be shipped. This module is the ONE
place that decision is made, because four callers make it and they must not
drift: `scripts/audit_paper_licences.py` resolves licences, `fetch_papers.py`
gates downloads on them, `curate_papers.py` refuses to extract from a
restricted paper, and `package_corpus.py` excludes the fingerprints from the
release archive.

Measured on the shipped corpus, 2026-09-10 (all 14,517 curated papers):

    cc by          5,708   39.3%     permits derivatives   7,327   50.5%
    none recorded  5,684   39.2%     NO DERIVATIVES        1,506   10.4%
    cc by-nc-nd    1,501   10.3%     unknown / unlicensed  5,684   39.2%
    cc by-nc       1,028    7.1%
    cc by-nc-sa      546    3.8%
    cc0               45    0.3%
    cc by-nd           5   <0.1%

## Absence of a licence is not permission

The single most important rule here. A paper Europe PMC records no licence for
is normally a publisher deposit: free to read in PMC, not licensed for reuse —
i.e. all rights reserved. `UNKNOWN` is therefore treated as restrictive by
`permits_derivatives`, not as a default-allow. It is kept as a *separate*
verdict from `NO_DERIVATIVES` so a report can distinguish "we know this is
forbidden" from "we do not know", which are different problems: the first needs
a policy, the second needs a lookup.

## The project's position

A fingerprint is TREATED as a derivative work of its paper. This module exists
so that treating it that way is automatic: the published archive carries a
fingerprint only where the paper's licence permits redistributing derivatives,
and the conservative choice is the default rather than something a maintainer
has to remember. See `docs/licensing.md`.
"""

from __future__ import annotations

import time

#: Verdicts. Deliberately three, not two — see the module docstring.
DERIVATIVES_OK = "derivatives_ok"
NO_DERIVATIVES = "no_derivatives"
UNKNOWN = "unknown"

#: Licence tokens that forbid derivative works.
_ND_TOKENS = frozenset({"nd"})

#: Prefixes that permit them. `nc` restricts COMMERCIAL use, which is a
#: different question from derivation, and LPT is noncommercially licensed
#: anyway — so `cc by-nc` and `cc by-nc-sa` are fine here.
_OK_PREFIXES = ("cc by", "cc-by", "cc0", "cc 0", "public domain", "publicdomain",
                "pd", "gpl", "mit", "apache")


def classify(licence: str | None) -> str:
    """`DERIVATIVES_OK`, `NO_DERIVATIVES` or `UNKNOWN` for a licence string.

    Token-based, never substring: `"cc by-nd"` is ND and `"cc by"` is not, and
    a licence must never be read as ND because some unrelated word in it
    happens to contain the letters "nd" ("Attribution-NoDerivs", "and", a
    journal name in a free-text field).
    """
    if licence is None:
        return UNKNOWN
    lic = str(licence).strip().lower()
    if not lic or lic in ("none", "unknown", "n/a", "-", "—"):
        return UNKNOWN

    # "cc by-nc-nd 4.0" -> {"by", "nc", "nd", "4.0"}
    tokens = set(lic.replace("cc", " ").replace("-", " ").replace("/", " ").split())
    if tokens & _ND_TOKENS or "noderiv" in lic.replace("-", "").replace(" ", ""):
        return NO_DERIVATIVES
    if any(lic.startswith(p) for p in _OK_PREFIXES):
        return DERIVATIVES_OK
    return UNKNOWN


def permits_derivatives(licence: str | None) -> bool:
    """True only when the licence AFFIRMATIVELY allows derivative works.

    `UNKNOWN` returns False. That is the whole point: the conservative reading
    has to be what you get by writing no extra code, because the permissive
    reading is the one that ships something you had no right to ship.
    """
    return classify(licence) == DERIVATIVES_OK


def describe(licence: str | None) -> str:
    """One short phrase for a log line or a report row."""
    verdict = classify(licence)
    if verdict == DERIVATIVES_OK:
        return f"{licence} — derivatives permitted"
    if verdict == NO_DERIVATIVES:
        return f"{licence} — NO DERIVATIVES"
    return ("no licence recorded — absence of permission, not permission"
            if not licence else f"{licence} — unrecognised, treated as restricted")


# ── resolution ───────────────────────────────────────────────────────────────
#
# Europe PMC rather than PMC's own OA service: it accepts DOIs as well as
# PMCIDs (so it covers papers with no PMCID and the bioRxiv/medRxiv preprints),
# and one request answers for a batch. `scripts/audit_paper_licences.py` and
# `scripts/fetch_papers.py` both call this, which is why it lives here.

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
#: 40 OR-ed identifiers resolve in well under a second; larger batches start
#: tripping Europe PMC's query-length limit.
BATCH = 40


def resolve_batch(session, ids: list[tuple[str, str]], timeout: int = 60) -> dict:
    """`{identifier: europepmc record}` for `(kind, value)` ids.

    `kind` is `"pmcid"` or `"doi"`. The returned dict is keyed by BOTH the
    record's pmcid and its lowercased doi, so a caller can look up by whichever
    identifier it happened to query with.
    """
    clause = " OR ".join(
        f'{"PMCID" if kind == "pmcid" else "DOI"}:"{val}"' for kind, val in ids)
    r = session.get(SEARCH_URL,
                    params={"query": clause, "format": "json",
                            "resultType": "core", "pageSize": len(ids) + 10},
                    timeout=timeout)
    r.raise_for_status()
    out: dict[str, dict] = {}
    for rec in (r.json().get("resultList", {}).get("result") or []):
        for key in (rec.get("pmcid"), (rec.get("doi") or "").lower()):
            if key:
                out[key] = rec
    return out


def resolve_many(items: list[tuple[str, str | None, str | None]],
                 *, sleep: float = 0.2, log=None) -> dict[str, str]:
    """`{ref: licence}` for `(ref, pmcid, doi)` triples. `""` means none found.

    `ref` is whatever the caller wants back as the key — a paper_key, a DOI,
    anything. An entry Europe PMC has no record for maps to `""`, which
    `classify` reads as UNKNOWN and `permits_derivatives` as restricted: an
    unanswered lookup must never come back as permission.
    """
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = "little-protein-tiger licence lookup"
    out: dict[str, str] = {}
    todo = [it for it in items if (it[1] or it[2])]
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        ids = [("pmcid", str(p).strip()) if p and str(p).strip()
               else ("doi", str(d).strip())
               for _ref, p, d in chunk]
        try:
            found = resolve_batch(session, ids)
        except Exception as exc:                              # noqa: BLE001
            if log:
                log(f"licence lookup failed for a batch of {len(chunk)} "
                    f"({type(exc).__name__}) — treating them as unlicensed")
            for ref, _p, _d in chunk:
                out.setdefault(ref, "")
            continue
        for ref, pmcid, doi in chunk:
            rec = (found.get(str(pmcid or "").strip())
                   or found.get(str(doi or "").lower().strip()))
            out[ref] = (rec or {}).get("license") or ""
        if sleep:
            time.sleep(sleep)
    return out
