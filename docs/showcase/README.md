# Showcase pages

Three self-contained HTML pages that walk through what LPT actually does, built
from real runs in this repository. No illustrative numbers: every figure is read
out of a run directory, the corpus database, or a recorded session transcript.

`index.html` is the landing page that links the three. `_common.py` holds the
public base URL and the OpenGraph/Twitter block every page shares —
**change `SITE` there if the Pages URL ever changes**, then rebuild all four,
or the link previews will point at the old host.

| Page | Built from | What it shows |
|---|---|---|
| `campaign_pdl1.html` | `projects/pdl1_e2e` | A complete binder campaign against PD-L1 — target choice, epitope, calibration gate, production funnel, ranked designs |
| `corpus_explorer.html` | `outputs/mesothelioma_showcase.txt`, `data/` | One corpus-explorer session: tool trace, fingerprint schema, interaction + DepMap graphs |
| `ppi_discovery.html` | `outputs/e2e_mesothelioma` | The PPI track's discovery half — disease name to tiered targets to six hotspot residues |

## Rebuilding

```bash
python docs/showcase/build_previews.py     # -> assets/og_*.png  (social cards)
python docs/showcase/build_campaign.py     # -> campaign_pdl1.html
python docs/showcase/build_corpus.py       # -> corpus_explorer.html
python docs/showcase/build_ppi.py          # -> ppi_discovery.html
python docs/showcase/build_index.py        # -> index.html
```

Run them in that order: the preview cards are inputs to the landing page, and
the other three read their CSS out of `campaign_pdl1.html`.

## Publishing

`.github/workflows/pages.yml` uploads `docs/` to GitHub Pages on every push to
`main` that touches it, plus manually via *Actions → Pages → Run workflow*. The
workflow only uploads — the HTML is committed, not built in CI — so regenerate
locally and commit when a run changes.

**Pages has to be turned on once by hand:** Settings → Pages → Source:
*GitHub Actions*. On a private repo that needs a paid plan; on a public repo it
is free. Until then the workflow will fail at the deploy step, which is
expected rather than broken.

The `og:image` cards (1200×630, PNG) are what a pasted link unfurls into on
Slack, X, LinkedIn and iMessage. They must be reachable by absolute URL — a
crawler will not follow a data URI — which is why they are the one set of
images not inlined.

Each builder inlines its images as base64 WebP, so the output is one file with no
external assets and no network dependency. `build_corpus.py` and `build_ppi.py`
read their CSS out of `campaign_pdl1.html`, so build that one first.

## Regenerating the structure images

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
