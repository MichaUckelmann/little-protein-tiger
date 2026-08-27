# Third-party licences

LPT itself is MIT-licensed (see `LICENSE`). **That licence does not cover the
files listed here.** Each was written by someone else and is redistributed in
this repository under its own terms, reproduced in full below.

This file covers material LPT *ships*. The external models and tools LPT merely
*orchestrates* — foundry's model weights, BoltzGen, PyRosetta, Protenix,
ChimeraX/PyMOL — are not redistributed here and grant no rights through this
repository; see the "Licence and third-party tools" section of `README.md`
before relying on any of them commercially.

---

## RFdiffusion3 documentation — BSD 3-Clause

Three reference documents in `skills/protein-design-script/` are verbatim
copies of documentation from [RosettaCommons/foundry](https://github.com/RosettaCommons/foundry)
(branch `production`), retrieved 2026-05-21:

| File in this repo | Upstream path |
|---|---|
| `RFD3_reference.md` | `models/rfd3/README.md` |
| `RFD3input.md` | `models/rfd3/docs/input.md` |
| `RFD3_protein_binder_design.md` | `models/rfd3/docs/examples/protein_binder_design.md` |

They are vendored rather than linked because the `protein-design-script` skill
reads them as in-context reference, and because they pin the contig/spec format
`src/foundry_spec.py` validates against. Upstream is the authority; re-fetch
before assuming these are current.

```
BSD 3-Clause License

Copyright (c) 2025, Institute for Protein Design, University of Washington

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

Per the third clause: neither the Institute for Protein Design, the University
of Washington, nor the foundry contributors endorse LPT.

---

## BoltzGen documentation and example spec — MIT

Two files in `skills/protein-design-script/` come from
[HannesStark/boltzgen](https://github.com/HannesStark/boltzgen) (branch `main`),
retrieved 2026-05-21:

| File in this repo | Upstream path | Relation |
|---|---|---|
| `boltzgen_reference.md` | `README.md` | verbatim copy |
| `boltzgen_example_yaml_cyclic_peptide.yaml` | `example/cylcic_against_kras_with_specific_site/cyclicdesign.yaml` | adapted (LPT structure paths, LPT comments) |

```
MIT License

Copyright (c) 2025 Hannes Stärk

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## Mol* viewer — MIT

`assets/vendor/molstar/` holds the Mol* viewer bundle (`molstar.js` +
`molstar.css`, pinned at 5.11.0), inlined into generated HTML reports so they
work offline. Source: https://github.com/molstar/molstar. Its MIT licence text
is tracked alongside the bundle at `assets/vendor/molstar/LICENSE`, and the
vendoring rationale and update procedure are in
`assets/vendor/molstar/README.md`.
