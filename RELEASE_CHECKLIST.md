# ⚠️ BEFORE MAKING THIS REPOSITORY PUBLIC

**Status: NOT READY TO GO PUBLIC.** One blocking item, listed first, and one
open decision below it.

---

## ✅ RESOLVED — the corpus is published

`v0.1.0` carries `lpt-corpus.tar.zst` (111,434,853 bytes), and
`scripts/fetch_corpus.py` finds it. Rebuild and replace with:

```bash
python scripts/package_corpus.py            # -> dist/lpt-corpus.tar.zst
gh release upload v0.1.0 dist/lpt-corpus.tar.zst --clobber
```

The asset name must start with `lpt-corpus` — that is the prefix
`fetch_corpus.py` looks for. When you replace it, update the sha256 in the
release notes; it is quoted there for verification.

## 🟡 The published asset predates the abstract strip

The live asset was built before `package_corpus.py` learned to blank
`papers.abstract`, so it carries **11,818 publisher-supplied abstracts** in
`data/literature.db`. Nothing reads them (see `_STRIPPED_COLUMNS` and
`docs/licensing.md`), and no third party has downloaded it — the repo is still
private — so replacing it costs nothing now and is awkward later.

**Rebuild and re-upload before flipping visibility.** This is the same class of
problem as the API key found in `curation_error`, caught at the same stage, and
it is only free while the repo is private.

## 🔴 Verify the asset resolves anonymously — only possible after going public

```bash
git clone https://github.com/MichaUckelmann/little-protein-tiger /tmp/verify
cd /tmp/verify && python scripts/fetch_corpus.py --check
```

A GitHub release asset on a **private** repo is not anonymously downloadable,
and that 404 is indistinguishable from "no asset published yet" — which is
exactly what a beta tester would report. Run this immediately after the flip.

---

## Also unresolved

### `web/web.db` is in git history

Reachable at blob `5f9d65b6d745cfbaf279f79d1dcc9badd51ecd43`, containing one
user row: GitHub id, login, email, and a Fernet-encrypted Anthropic key.

**DECIDED 2026-09-09: accepted, not removed.** Re-verified independently
before the call:

- `FERNET_KEY` has never been committed — the only matches in history are an
  empty `.env.example` placeholder, prose, and code reading the env var. So the
  231-char Fernet token in that row is not decryptable from anything in the
  repository.
- The maintainer confirmed the underlying Anthropic key was rotated
  (2026-04-06, the same day the row was written).
- `github_id`, `github_login` and the email are already public in the author
  metadata of every commit, so purging the blob would hide none of them.

Against that, `git filter-repo` + force-push rewrites all 74 SHAs and would
have invalidated PR #1 mid-merge. Rewriting history to remove inert content was
the larger risk. No action needed before going public.

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
| Redistributed upstream docs | **Resolved 2026-08-27.** All permissive, none academic-only: foundry (RFD3 README, input spec, binder-design doc) is **BSD-3-Clause**, © 2025 IPD/University of Washington; BoltzGen's README and the cyclic-peptide example spec are **MIT**, © 2025 Hannes Stärk. Verified against upstream `LICENSE.md`/`LICENSE` — note foundry's default branch is `production`, not `main`. Attribution headers added to all five files, full texts in `THIRD_PARTY_LICENSES.md`, README licence table corrected. `dl_binder_design` (MIT, © 2023 N. R. Bennett) is referenced nowhere in the repo — nothing redistributed |
| PyRosetta licence | Flagged as academic/non-commercial in README and `docs/pyrosetta_setup.md` |
| Responsible use | `docs/responsible-use.md`, linked from the README |
| CI | Green: test + wheel-install + lint |
| Wheel | All four `lpt-*` scripts and both asset lookups verified from a clean venv |

---

## Release order

1. ~~**Merge PR #1**~~ — merged 2026-09-09 as `ce22ce5`, CI green on 3.12/3.13.
2. **Package and publish the corpus** — the blocker above.
3. ~~Decide on `web/web.db`~~ — done, accepted (see above).
4. Flip visibility to public.
5. Re-run `python scripts/fetch_corpus.py --check` from a fresh clone and
   confirm the asset is found anonymously.

---

*Delete this file once the release is out — it is a pre-flight checklist, not
documentation.*
