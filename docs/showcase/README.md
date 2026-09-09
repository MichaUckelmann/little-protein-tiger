# Showcase pages

Four self-contained HTML pages that walk through what LPT actually does, built
from real runs in this repository.

`index.html` is the landing page that links them. `_common.py` holds the public
base URL and the OpenGraph/Twitter block every page shares — **change `SITE`
there if the Pages URL ever changes**, then rebuild everything, or the link
previews will point at the old host.

| Page | Built from | What it shows |
|---|---|---|
| `pain_receptors.html` | `projects/pain_receptors_v3` | The most recent end-to-end run, and the fullest arc: one general prompt about pain, three candidate targets ranked by evidence, the CGRP receptor chosen, a designable structure measured out, a reachable epitope on a membrane protein, a trial that sized the campaign, and 365 gated designs |
| `ppi_discovery.html` | `projects/mesothelioma_showcase` | The PPI track end to end — an unnamed target chosen, argued, sized from a measured hit rate, and designed against on GPU; with the archived BoltzGen run as the before |
| `campaign_pdl1.html` | `projects/pdl1_e2e` | A complete binder campaign against PD-L1 — target choice, epitope, calibration gate, production funnel, ranked designs |
| `corpus_explorer.html` | `outputs/mesothelioma_showcase.txt`, `data/` | One corpus-explorer session: tool trace, fingerprint schema, interaction + DepMap graphs |

## Where the numbers come from

**Every figure is extracted at build time. None is typed in.** This was not
always true, and the drift it caused is why the arrangement below exists: the
builders used to carry their numbers as literals while the pages claimed the
figures "were read from" the run. Nothing compared the two. What shipped
included four of twelve hotspot residue names wrong on `ppi_discovery.html`
(in a paragraph about grounding hotspot names in the real structure), an
argument on `campaign_pdl1.html` that its own scoring CSV contradicts, and
every corpus statistic on `corpus_explorer.html` stale by about 25%.

Each builder now has an `extract()` that reads the run, and goes through
`_facts.load()` (see `_facts.py`):

- On a machine that **has** the run, `extract()` runs and its result is written
  to `facts/<page>.json`, which **is** tracked — `projects/`, `outputs/` and
  `data/` are all gitignored, so the snapshot is the only version-controlled
  record of what the page asserts.
- **Anywhere else**, the snapshot is loaded and the page builds byte-identically.

So a number that moves shows up as a reviewable diff in `facts/` instead of
silently, and the builder prints a `CHANGED` warning when it happens.
`tests/test_showcase_provenance.py` pins it: the counts a reader would look up
must appear in the page, and a builder that stops going through `_facts.load`
fails the suite.

`build_previews.py` and `build_index.py` read the same snapshots, so the og:image
cards and the landing-page copy cannot drift away from the pages they advertise.

## Rebuilding

Use the project venv — `build_index.py` and `build_previews.py` need Pillow,
which is not a declared project dependency:

```bash
.venv/bin/python docs/showcase/build_campaign.py    # -> campaign_pdl1.html  (build first: owns the shared CSS)
.venv/bin/python docs/showcase/build_ppi.py         # -> ppi_discovery.html
.venv/bin/python docs/showcase/build_corpus.py      # -> corpus_explorer.html
.venv/bin/python docs/showcase/build_pain.py        # -> pain_receptors.html
.venv/bin/python docs/showcase/build_previews.py    # -> assets/og_*.png  (social cards)
.venv/bin/python docs/showcase/build_index.py       # -> index.html
```

`campaign_pdl1.html` first: every other page reads its CSS out of that file's
`<style>` block. The preview cards are inputs to the landing page, so
`build_index.py` last.

## Publishing

`.github/workflows/pages.yml` uploads `docs/` to GitHub Pages on every push to
`main` that touches it, plus manually via *Actions → Pages → Run workflow*. The
workflow only uploads — the HTML is committed, not built in CI — so regenerate
locally and commit when a run changes.

**Pages cannot be enabled on a private repo without a paid plan**, so the
workflow *skips* while the repo is private rather than failing — a red Actions
tab on every docs push is how a real test failure gets scrolled past.

`configure-pages` runs with `enablement: true`, but that does **not** save you
the one-time setup: `GITHUB_TOKEN` is refused with *"Create Pages site failed.
Error: Resource not accessible by integration"*, because creating a Pages site
needs admin rights the Actions token never gets. Enable it once by hand
(Settings → Pages → Source: *GitHub Actions*) or with an admin token:

```bash
gh api -X POST repos/MichaUckelmann/little-protein-tiger/pages -f build_type=workflow
```

Done for this repo on 2026-09-09; the site is
<https://michauckelmann.github.io/little-protein-tiger/>. And going public does
not retrigger a deploy — workflows fire on push — so the first one after a
visibility flip needs a commit touching `docs/` or `gh workflow run Pages`.

Note that the workflow uploads the WHOLE of `docs/`, not just this directory:
the `.md` files ship too and are served as unrendered plain text (there is no
Jekyll step). Nothing links to those URLs — the README's links are
GitHub-relative — so it is cosmetic, but it is worth knowing before assuming a
file here is private.

The `og:image` cards (1200×630, PNG) are what a pasted link unfurls into on
Slack, X, LinkedIn and iMessage. They must be reachable by absolute URL — a
crawler will not follow a data URI — which is why they are the one set of
images not inlined.

Each builder inlines its images as base64 WebP, so the output is one file with no
external assets and no network dependency.

## Regenerating the structure images

`render_pain.py` is the one render scripted end to end — run
`.venv/bin/python docs/showcase/render_pain.py` and it re-derives the hotspot
numbering, aims the camera and writes `assets/pain_design.webp` +
`assets/pain_epitope.webp`. It **verifies the author-to-refold residue mapping
against the coordinates and refuses to render if they disagree**, because a
wrong offset paints ten arbitrary residues and produces a figure that looks
perfectly fine. The other pages' renders predate it and were made by hand; port
them to the same shape if they are ever regenerated. Two things it encodes:
ChimeraX cannot write WebP (`No known data format for file suffix '.webp'`), so
render PNG and convert; and aiming straight down the epitope vector puts the
binder between the camera and everything it is covering, so the frame is tilted
~52° off that axis.

`assets/*.webp` are ChimeraX renders of the campaigns' own structures. The
camera is computed, not hand-placed: for each figure a script takes the vector
from the target's centroid to the hotspot centroid and points the camera down
it, so the "bare epitope" and "design bound" images of a pair share one camera
and can be read as before/after.

The PD-1 comparison on the campaign page superposes PDB 4ZQK's PD-L1 chain onto
the campaign's own copy (ChimeraX `matchmaker`, 0.83 A over 115 CA at 99.1%
identity) so both partners share one frame and one camera, then counts contact
residues at a 4.5 A heavy-atom cutoff on each side. 4ZQK took no part in the
run, which is what makes it an independent check rather than a restatement.

Three traps worth knowing if you re-render:

- **RF3 refold chain B is renumbered 1-based**, while the deposited structure
  uses author numbering. The PD-L1 hotspots are residues 39, 41, 52, 58, 96,
  102, 104, 105, 106 in a refold and 56, 58, 69, 75, 113, 119, 121, 122, 123 in
  7CZD. Colouring a refold with author ids silently paints the wrong residues.
- **Binder is chain A and target is chain B in every RF3 refold**, the opposite
  of the deposited complexes, where the target is usually chain A.
- **ChimeraX draws missing-residue pseudobonds with a text label** ("8
  residues") that lands in the render as floating type over the structure.
  `hide pseudobonds` before saving, or it ships in the image.

Renders use `chimerax --offscreen --nogui --exit --silent <script.cxc>` with
absolute paths (a relative path fails, and without `--exit` the process then
waits at an interactive prompt forever), and are cropped to the alpha bounding
box afterwards rather than framed by hand.

## Network maps

Every interaction/DepMap map LPT draws goes through `src/network_svg.py`, so
they share one visual grammar wherever they appear:

| channel | meaning |
|---|---|
| node fill | filled = a seed the query started from, hollow = pulled in |
| node radius | degree within the drawn subgraph |
| edge width | corpus co-mention count (sqrt-scaled) |
| edge colour | sign of the DepMap correlation; dashed grey = no DepMap pair |
| edge opacity | \|r\| — a weak correlation reads as a faint line |

`load_cyjs()` reads a Cytoscape export (what `export_subgraph` emits),
`render_svg()` returns a self-styling `<svg>` that inherits the host page's
theme tokens, and `legend_items()` gives the legend so callers cannot drift.
The layout is a deterministic Fruchterman-Reingold with no RNG — the same graph
always produces the same picture, so regenerating a report does not churn the
figure. `tests/test_network_svg.py` pins the determinism, the no-overlap
property and the colour encoding.

## Chart colours

The two data hues are validated per theme against the six checks in the
`dataviz` skill — lightness band, chroma floor, CVD separation, normal-vision
floor and contrast — for the light and dark surfaces separately, which is why
`--mark-a` / `--mark-b` differ between themes rather than being an automatic
flip. Structure renders keep their own slightly different green and ochre: they
are photographs of a molecule, not abstract marks, and legibility on a curved
surface wins there.
