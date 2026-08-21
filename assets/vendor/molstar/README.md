# Mol* viewer (vendored)

`molstar.js` + `molstar.css` — the Mol* viewer app bundle, pinned at **5.11.0**.
Source: https://github.com/molstar/molstar (MIT license, see `LICENSE` in this
directory — the bundle's own trailing comment carries the same notice).

Intended for the binder-campaign HTML report generator (embeds an
interactive structure viewer directly in a self-contained report page): the
JS/CSS get inlined into the report's `<script>`/`<style>` tags at build time,
not loaded from a CDN, so a generated report has no runtime network
dependency and works offline.

## Updating

```
curl -sL -o molstar.js  "https://cdn.jsdelivr.net/npm/molstar@<version>/build/viewer/molstar.js"
curl -sL -o molstar.css "https://cdn.jsdelivr.net/npm/molstar@<version>/build/viewer/molstar.css"
```

Bump the pinned version above, and re-verify a real structure still loads and
renders before committing — the API surface (`Viewer.create`,
`loadStructureFromData`, `structureInteractivity`) has moved before.
