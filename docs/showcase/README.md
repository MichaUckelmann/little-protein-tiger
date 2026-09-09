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
`assets/pain_epitope.webp` (the hero pair) and `assets/pain_rank1-4.webp` (the
design cards).

**The four card renders share one camera and one crop.** Each is a separate RF3
refold, so its target sits in its own frame; rendered independently the cards
would each be aimed differently and could not be compared, which is the only
reason to show four side by side. So every target chain is superposed onto rank
1's with `matchmaker` (aligning on `/B`, the target — never on the binders,
which are different molecules), one camera is computed and framed over the
union, and the models are shown one at a time **without** re-running `view`.
The crop is the union of the four bounding boxes, not each image's own: cropping
per image rescales and shifts every card independently and silently undoes the
superposition. It **verifies the author-to-refold residue mapping
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

## Launch material

Two assets for announcing the project, built from the same `facts/` snapshots as
the pages so a launch post cannot quote a number the showcases have moved on
from — the one artefact you cannot quietly correct after publishing.

```bash
.venv/bin/python docs/showcase/render_hero.py --turntable   # -> hero_*.webp + turntable/
.venv/bin/python docs/showcase/build_carousel.py            # -> assets/lpt_carousel.pdf
.venv/bin/python docs/showcase/build_video.py               # -> assets/lpt_hook.mp4
```

| Asset | Shape | For |
|---|---|---|
| `assets/lpt_carousel.pdf` | 10 slides, 1080x1350 | LinkedIn renders an uploaded PDF as a swipeable deck |
| `assets/lpt_hook.mp4` | 42 s, 1080x1350, silent | feed video; autoplay is muted, so every claim is on screen |
| `assets/lpt_hook_poster.png` | 1080x1350 | upload as the video thumbnail — the first frame is a half-typed prompt |

Slide 9 is the limitations note, and it is deliberately not phrased as "the
pipeline is only as strong as its literature database". That claim is true of
target discovery and prior art and false of everything else — structure
selection, the trim, calibration, the gates and the ranking read no papers at
all — so the sweeping version overstates one dependency while omitting the real
one, which is that nothing in the deck has been tested at a bench. It also
quotes the run against itself rather than describing it: the candidate the
pathway stage marked "not found in corpus" is pulled out of the tier table, so
the slide's evidence of candour is the run's own words.

`render_hero.py` is the one that needed thought. It superposes the lead design's
own refold onto **6E3Y**, the full-length CGRP receptor with its agonist and G
protein, by the target chain. The campaign designed against 3N7S — an ectodomain
crystal form with no peptide in it — so the overlap with CGRP that this reveals
(122 binder atoms within 4.5 A, closest approach 0.77 A, written to
`facts/hero_check.json`) is a check the run could not have optimised toward, the
same role 4ZQK plays for PD-L1. **Say what it is:** a superposition, not a
docking run, and the *site* was chosen deliberately by the interface stage — what
is independent is the *occupancy*.

The bilayer is not drawn where it looks right. `membrane()` asks
`src.membrane_topology` — the same UniProt annotation that dropped the
transmembrane residues from the trim — for CALCRL's TM spans, maps them into
6E3Y author numbering, and centres a 30 A slab on those residues. It refuses if
the observed span and the bilayer constant disagree by more than 6 A; they agree
to 2 A, and both numbers are printed. Cached to `facts/hero_membrane.json` so
the figure survives a dead network.

Three traps these two scripts encode:

- **`shape cylinder` accepts `center` and `axis` and silently ignores both** in
  ChimeraX 1.12. The slab is built at the origin along z — 160 A away, face-on to
  a camera expecting it edge-on — with no error and a frame that still renders.
  Use `fromPoint`/`toPoint`.
- **`show cartoon` is global and undoes every hide issued above it.** The first
  hero render came back with the whole Gs heterotrimer in default rainbow.
- **Inline the webfonts.** Left as a stylesheet link, each of the video's ~49
  stills re-fetches from Google, and one slow response ships a run of frames in
  Georgia while its neighbours are in Newsreader — visible only in the finished
  video, after everything has been rendered.

Pacing lives in three named constants at the top of `build_video.py`, not
scattered through the scenes. `HOLD` is the beat a completed section gets before
the cut and is the one worth tuning: the first cut ran every beat at about 1.5 s,
which is long enough to see a slide and not long enough to read one. `BUILD` is
shorter on purpose — an intermediate reveal only has to register the thing being
added, since what is already on screen stays there.

`build_video.py` splits the work three ways: Chrome renders the typography (so
the video shares its type scale and layout primitives with the pages instead of
being a second design system hand-maintained in PIL), ChimeraX supplies the
structure's motion, and PIL composites those 90 frames into one Chrome-rendered
*plate* — 90 browser launches would not be worth it. The plate's `.hole` is
absolutely positioned from `PLATE_BOX`, the same constant PIL pastes at: left in
normal flow it lands wherever the headline above it wraps, and the receptor
composites over its own caption.

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
