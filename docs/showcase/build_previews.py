#!/usr/bin/env python3
"""Render the 1200x630 OpenGraph preview cards for the showcase pages.

One card per page, written to assets/og_<page>.png. Social platforms fetch these
by absolute URL, so they cannot be data URIs like the in-page images.
Rebuild:  python docs/showcase/build_previews.py
"""
from __future__ import annotations
import math, pathlib
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
    src = (HERE / "build_corpus.py").read_text().split("def trace_rows")[0]
    ns = {"__file__": str(HERE / "build_corpus.py")}
    exec(compile(src, "b", "exec"), ns)
    nodes, edges, meta = ns["subgraph"]()
    P = ns["layout"](nodes, edges)
    S = 2
    im = Image.new("RGBA", (760 * S, 470 * S), GROUND + (255,))
    d = ImageDraw.Draw(im, "RGBA")
    deg = {n: 0 for n in nodes}
    for s, t, *_ in edges:
        deg[s] += 1; deg[t] += 1
    for s, t, r, m, kd in edges:
        col = (RULE if r is None else (OCHRE if r < 0 else ACCENT))
        op = int(255 * (0.30 if r is None else min(0.95, 0.32 + abs(r) * 1.5)))
        d.line([tuple(v * S for v in P[s]), tuple(v * S for v in P[t])],
               fill=col + (op,), width=int((0.9 + min(3.2, (m or 1) ** 0.5)) * S))
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


def main():
    card("campaign", "binder campaign · PD-L1",
         "One target name in, twenty ranked designs out",
         ["5,824 refolds", "715 through the gates", "21 GPU-hours", "$2.12"],
         Image.open(ASSETS / "design_face.webp").convert("RGBA"))
    card("corpus", "corpus-explorer",
         "Ask eleven thousand papers a question, get a target map",
         ["55,689 indexed", "11,055 curated", "20,658 edges", "65 s"],
         network_art())
    card("ppi", "ppi track · discovery",
         "From a disease name to a specific groove on a specific protein",
         ["3 tiered targets", "3,402 A2 interface", "6 hotspots"],
         Image.open(ASSETS / "tead1_yap1.webp").convert("RGBA"))


if __name__ == "__main__":
    main()
