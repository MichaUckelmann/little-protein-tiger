#!/usr/bin/env python3
"""Render the 1200x630 OpenGraph preview cards for the showcase pages.

One card per page, written to assets/og_<page>.png. Social platforms fetch these
by absolute URL, so they cannot be data URIs like the in-page images.
Rebuild:  python docs/showcase/build_previews.py
"""
from __future__ import annotations
import math, pathlib, sys
from PIL import Image, ImageDraw, ImageFont

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ASSETS = HERE / "assets"
W, H = 1200, 630

F = "/usr/share/fonts/truetype/noto/"
SERIF = F + "NotoSerifDisplay-Medium.ttf"
SERIF_R = F + "NotoSerifDisplay-Regular.ttf"
SANS_SB = F + "NotoSans-SemiBold.ttf"
MONO = F + "NotoSansMono-Regular.ttf"

GROUND = (244, 244, 241)
INK = (23, 26, 20)
MUTED = (93, 99, 87)
ACCENT = (43, 143, 93)
OCHRE = (160, 112, 2)
RULE = (206, 209, 199)


def wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w_ in words:
        trial = (cur + " " + w_).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines


def card(name, eyebrow, title, stats, art):
    im = Image.new("RGB", (W, H), GROUND)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 7], fill=ACCENT)

    art_w = 430
    if art is not None:
        a = art.copy()
        a.thumbnail((art_w, 470), Image.LANCZOS)
        bg = Image.new("RGB", a.size, GROUND)
        if a.mode == "RGBA":
            bg.paste(a, (0, 0), a)
        else:
            bg.paste(a, (0, 0))
        im.paste(bg, (W - art_w - 54, (H - a.size[1]) // 2))

    x, max_w = 64, W - art_w - 150
    fe = ImageFont.truetype(SANS_SB, 19)
    d.text((x, 78), eyebrow.upper(), font=fe, fill=ACCENT)

    ft = ImageFont.truetype(SERIF, 54)
    lines = wrap(d, title, ft, max_w)
    if len(lines) > 4:
        ft = ImageFont.truetype(SERIF, 46)
        lines = wrap(d, title, ft, max_w)
    y = 128
    for ln in lines:
        d.text((x, y), ln, font=ft, fill=INK)
        y += int(ft.size * 1.16)

    y = max(y + 26, H - 152)
    d.line([(x, y), (x + max_w, y)], fill=RULE, width=1)
    fs = ImageFont.truetype(MONO, 21)
    d.text((x, y + 22), "   ·   ".join(stats), font=fs, fill=MUTED)

    fb = ImageFont.truetype(SANS_SB, 20)
    d.text((x, H - 62), "Little Protein Tiger", font=fb, fill=INK)
    im.save(ASSETS / f"og_{name}.png", optimize=True)
    print(f"og_{name}.png  {(ASSETS / f'og_{name}.png').stat().st_size // 1024} KB")


def network_art():
    """Draw the same subgraph the corpus page shows, as raster."""
    sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT))
    from src.network_svg import load_cyjs, layout as net_layout
    from build_corpus import CANON
    nodes, edges, meta = load_cyjs(ROOT / "mesothelioma_target_network.cyjs", CANON)
    P = net_layout(nodes, edges)
    S = 2
    im = Image.new("RGBA", (760 * S, 470 * S), GROUND + (255,))
    d = ImageDraw.Draw(im, "RGBA")
    deg = {n: 0 for n in nodes}
    for e in edges:
        deg[e.source] += 1; deg[e.target] += 1
    for e in edges:
        col = (RULE if e.r is None else (OCHRE if e.r < 0 else ACCENT))
        op = int(255 * (0.30 if e.r is None else min(0.95, 0.32 + abs(e.r) * 1.5)))
        d.line([tuple(v * S for v in P[e.source]), tuple(v * S for v in P[e.target])],
               fill=col + (op,), width=int((0.9 + min(3.2, (e.mentions or 1) ** 0.5)) * S))
    fn = ImageFont.truetype(MONO, 11 * S)
    for n in nodes:
        x, y = (v * S for v in P[n])
        r = (6 + min(9, deg[n] * 0.9)) * S
        seed = meta[n].get("is_seed")
        d.ellipse([x - r, y - r, x + r, y + r],
                  fill=ACCENT if seed else GROUND, outline=ACCENT if seed else RULE, width=2 * S)
        if deg[n] >= 3 or seed:
            tw = d.textlength(n, font=fn)
            d.text((x - tw / 2, y - r - 15 * S), n, font=fn, fill=INK if seed else MUTED)
    return im


def facts(name):
    """The card's numbers come from the same snapshot the page was built from.

    These four strings are what unfurls in Slack, X and iMessage — the most-read
    numbers the project has — and they used to be typed here by hand. The corpus
    card drifted ~25% that way before anyone noticed.
    """
    import json
    return json.loads((HERE / "facts" / f"{name}.json").read_text(encoding="utf-8"))


def main():
    c = facts("campaign_pdl1")
    card("campaign", "binder campaign · PD-L1",
         "One target name in, twenty ranked designs out",
         [f"{c['n_scored']:,} refolds",
          f"{c['gate_current']['survivors']} through the gates",
          f"{c['gpu_hours']['total']:.0f} GPU-hours", f"${c['spend_usd']:.2f}"],
         Image.open(ASSETS / "design_face.webp").convert("RGBA"))

    x = facts("corpus_explorer")
    card("corpus", "corpus-explorer",
         "Ask fourteen thousand papers a question, get a target map",
         [f"{x['indexed']:,} indexed", f"{x['curated']:,} curated",
          f"{x['lit_edges']:,} edges", f"{x['session']['seconds']:.0f} s"],
         network_art())

    p_ = facts("ppi_discovery")
    card("ppi", "ppi track · mesothelioma",
         "One sentence about a disease, twenty designed binders out",
         [f"{100 * p_['calibration']['backbone']['p_hat']:.1f}% hit rate",
          f"{p_['n_survivors']} gated designs",
          f"{sum(p_['gpu_hours'].values()):.0f} GPU-h", f"${p_['spend_usd']:.2f}"],
         Image.open(ASSETS / "meso_design.webp").convert("RGBA"))

    n = facts("pain_receptors")
    card("pain", "ppi track · pain receptors",
         "A sentence about pain, 365 designed binders",
         [f"{100 * n['calibration']['backbone']['p_hat']:.1f}% hit rate",
          f"{n['n_survivors']} gated designs",
          f"{n['gpu_hours']['total']:.1f} GPU-h", f"${n['spend_usd']:.2f}"],
         # No artwork: the only images available are the corpus network (which
         # this page does not contain) and other campaigns' structures (which are
         # other proteins). A ChimeraX render of the CALCRL/RAMP1 lead would fill
         # this properly; a borrowed picture would assert something untrue.
         None)


if __name__ == "__main__":
    main()
