# ⚠️ BEFORE MAKING THIS REPOSITORY PUBLIC

**Status: NOT READY TO GO PUBLIC.** One blocking item, listed first.

---

## 🔴 BLOCKER — the corpus is not published yet

`scripts/fetch_corpus.py` is the documented way a new user gets the literature
corpus. **It currently finds nothing**, because no release asset exists. Until
this is done:

- `docs/journal-filtering.md`, `SETUP_AGENT.md`, `README.md` and
  `scripts/doctor.py` all tell users to run `fetch_corpus.py`.
- They will get "no `lpt-corpus*` asset found", and the literature track will be
  unusable for everyone who is not the maintainer.

### Do this

```bash
# 1. Build the archive (~83 MB from a 356 MB corpus). Refuses to run if the
#    database contains absolute or home-directory paths.
python scripts/package_corpus.py

# 2. Publish it as a release asset. The asset name must start with
#    "lpt-corpus" — that is the prefix fetch_corpus.py looks for.
gh release create v0.1.0 dist/lpt-corpus.tar.zst \
    --title "v0.1.0" \
    --notes "Initial release. Includes the curated literature corpus
(10,981 papers) as lpt-corpus.tar.zst — source documents excluded."

# 3. Verify from a clean checkout, NOT from this one.
git clone https://github.com/MichaUckelmann/little-protein-tiger /tmp/verify
cd /tmp/verify && python scripts/fetch_corpus.py --check
```

A GitHub release asset on a **private** repo is not anonymously downloadable,
so step 3 is only meaningful once the repo is public — or verify with an
authenticated client and re-check immediately after flipping visibility.

---

## Also unresolved

### `web/web.db` is in git history

Reachable at blob `5f9d65b6d745cfbaf279f79d1dcc9badd51ecd43`, containing one
user row: GitHub id, login, email, and a Fernet-encrypted Anthropic key.

**Assessed as low risk, deliberately not fixed:** no `FERNET_KEY` value has
ever been committed (verified across every blob in history), the underlying key
was rotated 2026-04-06, and the email is already the author address on every
commit. Removing it needs `git filter-repo` plus a force-push across four
branches, which rewrites every SHA.

Decide before going public — it is cheap now and impossible later.

### Third-party documentation redistributed without attribution

`skills/protein-design-script/` contains four verbatim upstream documents:
RFD3's README, the RFD3 input spec, the binder-design doc, and BoltzGen's
README (664 lines). No copyright or licence line in any of them.

RosettaCommons terms are frequently academic-only. **Verify each upstream
licence and add attribution, or replace them with links**, before publishing
them under this repo's MIT licence.

---

## Verified clean — no action needed

These were checked adversarially and are fine:

| Checked | Result |
|---|---|
| Secrets in history | No API key, token or private key in any commit. `.env` untracked; `.env.example` placeholders only |
| Personal paths in tracked files | None (`diary.md` included) |
| `literature.db` paths | All repo-relative; zero absolute, zero home-directory |
| Repo weight | 11 MB, 212 tracked files, no accidental blobs |
| Dangerous constructs | No `eval`/`exec`/`pickle.load`/`yaml.load`/`shell=True` |
| Vendored Mol* | Licence tracked, version pinned, upstream credited |
| PyRosetta licence | Flagged as academic/non-commercial in README and `docs/pyrosetta_setup.md` |
| Responsible use | `docs/responsible-use.md`, linked from the README |
| CI | Green: test + wheel-install + lint |
| Wheel | All four `lpt-*` scripts and both asset lookups verified from a clean venv |

---

## Release order

1. **Merge PR #1** (`pre-release-hardening`).
2. **Package and publish the corpus** — the blocker above.
3. Decide on `web/web.db` and the skills-directory attribution.
4. Flip visibility to public.
5. Re-run `python scripts/fetch_corpus.py --check` from a fresh clone and
   confirm the asset is found anonymously.

---

*Delete this file once the release is out — it is a pre-flight checklist, not
documentation.*
