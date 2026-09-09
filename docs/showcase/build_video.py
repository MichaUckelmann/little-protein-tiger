#!/usr/bin/env python3
"""Build the launch hook video: assets/lpt_hook.mp4 (1080x1350, 30 fps, silent).

LinkedIn autoplays feed video muted, so this carries no audio and every claim is
on screen. It runs about 27 seconds: the prompt being typed, then one beat per
decision the pipeline made on its own, ending on the lead design turning on the
full-length receptor.

    .venv/bin/python docs/showcase/build_video.py

Three tools, each doing the part it is actually good at:

  * TYPOGRAPHY is rendered by headless Chrome, one screenshot per still, so the
    video shares its palette, type scale and layout primitives with the showcase
    pages and the carousel instead of being a second design system maintained by
    hand in PIL.
  * MOTION of the structure comes from ChimeraX (`render_hero.py --turntable`).
    Compositing those 90 frames through Chrome would mean 90 browser launches,
    so one Chrome-rendered PLATE is produced with a known empty rectangle and
    PIL pastes each turntable frame into it.
  * ENCODING is ffmpeg over the assembled frame sequence.

Every figure comes from `facts/pain_receptors.json` and `facts/hero_check.json`,
like the pages and the deck. Nothing is typed in.

Webfonts are downloaded once and inlined as data URIs (cached in
`assets/.fontcache.css`). Left as a stylesheet link, each of the ~49 stills
re-fetches from Google and any one slow response ships a frame set in Georgia
while its neighbours are in Newsreader — a flicker that only shows up in the
finished video, after everything has been rendered.
"""
from __future__ import annotations

import base64
import concurrent.futures as cf
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ASSETS = HERE / "assets"
TURNTABLE = ASSETS / "turntable"
OUT = ASSETS / "lpt_hook.mp4"
POSTER = ASSETS / "lpt_hook_poster.png"
FONT_CACHE = ASSETS / ".fontcache.css"

W, H, FPS = 1080, 1350, 30

# Pacing, in frames at FPS. Named rather than sprinkled through the scenes: the
# first cut ran every beat at roughly a second and a half, which is enough to
# see a slide and not enough to read one — a viewer who is still parsing the
# Wilson interval when the funnel arrives has learnt nothing from either.
#
#   TYPE_STEP  one still of the prompt being typed (3 characters)
#   BUILD      an intermediate reveal, where what is already on screen STAYS
#              and something is added to it — so it needs long enough to notice
#              the addition, not long enough to read the whole frame again
#   HOLD       a completed section, the beat before the cut. This is the one
#              that was too short; everything a viewer has to actually take in
#              is on screen for the whole of it.
TYPE_STEP, BUILD, HOLD = 4, 38, 90
COUNTER = 15           # one tick of the hit-rate counter spinning up
TURN_HOLD = 72         # the last turntable frame, held to read its caption
WORKERS = 4
SITE = "michauckelmann.github.io/little-protein-tiger"

# The turntable is pasted into this rectangle of the plate. Kept in one place
# because the CSS below reserves exactly this box and PIL fills exactly this box.
PLATE_BOX = (60, 372, 1020, 1150)          # left, top, right, bottom

CHROME = next((c for c in ("google-chrome", "google-chrome-stable", "chromium")
               if shutil.which(c)), None)

GOOGLE_FONTS = ("https://fonts.googleapis.com/css2"
                "?family=Newsreader:opsz,wght@6..72,400;6..72,500"
                "&family=IBM+Plex+Sans:wght@400;500;600;700"
                "&family=IBM+Plex+Mono:wght@400;500&display=swap")

F = json.loads((HERE / "facts/pain_receptors.json").read_text(encoding="utf-8"))
CHECK = json.loads((HERE / "facts/hero_check.json").read_text(encoding="utf-8"))
CAL = F["calibration"]


def fonts() -> str:
    """@font-face rules with the woff2 payloads inlined."""
    if FONT_CACHE.is_file():
        return FONT_CACHE.read_text(encoding="utf-8")
    import requests

    ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
          "Gecko) Chrome/125.0 Safari/537.36")     # woff2, not ttf
    css = requests.get(GOOGLE_FONTS, headers={"User-Agent": ua}, timeout=30).text
    for url in sorted(set(re.findall(r"url\((https://[^)]+\.woff2)\)", css))):
        blob = base64.b64encode(
            requests.get(url, timeout=30).content).decode()
        css = css.replace(url, f"data:font/woff2;base64,{blob}")
    FONT_CACHE.write_text(css, encoding="utf-8")
    print(f"  fonts inlined -> {FONT_CACHE.name} ({len(css) / 1024:.0f} kB)")
    return css


def img(name: str) -> str:
    path = ASSETS / f"{name}.webp"
    if not path.is_file():
        raise SystemExit(f"assets/{name}.webp missing — run the renders first")
    return "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode()


CSS = """
:root {
  --ground:#F4F4F1; --surface:#FFFFFF; --sunk:#E7E9E2;
  --ink:#171A14; --muted:#5D6357; --rule:#DEE0D8; --accent:#2b8f5d;
  --dark:#14170F; --dark-surface:#1C2019; --dark-ink:#E9ECE4;
  --dark-muted:#959C8B; --dark-rule:#2A2F25; --dark-accent:#63b98c;
  --display:"Newsreader",Georgia,serif;
  --body:"IBM Plex Sans",Arial,sans-serif;
  --mono:"IBM Plex Mono",monospace;
}
* { box-sizing:border-box; margin:0; padding:0 }
html, body { width:1080px; height:1350px; overflow:hidden }
body { font-family:var(--body); color:var(--ink); background:var(--ground);
       padding:96px 76px 76px; display:flex; flex-direction:column }
body.dark { background:var(--dark); color:var(--dark-ink) }
body.dark .muted { color:var(--dark-muted) }
body.dark .eyebrow { color:var(--dark-accent) }
body.dark .card { background:var(--dark-surface); border-color:var(--dark-rule) }
body.dark .rule { background:var(--dark-rule) }
body.dark .track { background:#242A20 }
body { position:relative }

.eyebrow { font-family:var(--mono); font-size:24px; letter-spacing:.18em;
           text-transform:uppercase; color:var(--accent); font-weight:500 }
h1 { font-family:var(--display); font-weight:400; font-size:96px; line-height:1.0;
     letter-spacing:-.025em; margin-top:34px; text-wrap:balance }
h2 { font-family:var(--display); font-weight:400; font-size:72px; line-height:1.05;
     letter-spacing:-.02em; margin-top:28px; text-wrap:balance }
p { font-size:34px; line-height:1.42 }
.muted { color:var(--muted) }
.rule { height:1px; background:var(--rule); margin:34px 0 }
.grow { flex:1 1 auto; min-height:0 }
.foot { margin-top:auto; font-family:var(--mono); font-size:22px;
        color:var(--muted); letter-spacing:.04em }

.term { font-family:var(--mono); font-size:38px; line-height:1.5;
        background:var(--dark-surface); border-left:6px solid var(--dark-accent);
        padding:34px 36px; margin-top:44px; min-height:210px;
        color:var(--dark-ink); word-break:break-word }
.caret { display:inline-block; width:20px; height:38px; background:var(--dark-accent);
         vertical-align:-6px; margin-left:3px }

.card { background:var(--surface); border:1px solid var(--rule); padding:34px }
.row { display:flex; gap:26px; align-items:baseline; padding:26px 0;
       border-bottom:1px solid var(--rule); font-size:36px }
.row.off { opacity:0 }
.row.pick { box-shadow:inset 6px 0 0 var(--accent); padding-left:20px;
            font-weight:600 }
.row .t { font-family:var(--mono); font-size:22px; letter-spacing:.1em;
          color:var(--muted); margin-left:auto; white-space:nowrap }

.stats { display:flex; gap:26px; margin-top:38px }
.stats > div { flex:1; background:var(--surface); border:1px solid var(--rule);
               padding:28px }
body.dark .stats > div { background:var(--dark-surface); border-color:var(--dark-rule) }
.n { font-family:var(--display); font-size:74px; line-height:1;
     font-variant-numeric:tabular-nums }
.n.sm { font-size:52px }
.k { font-family:var(--mono); font-size:19px; letter-spacing:.1em;
     text-transform:uppercase; color:var(--muted); margin-top:14px }

.big { font-family:var(--display); font-size:210px; line-height:.92;
       letter-spacing:-.035em; font-variant-numeric:tabular-nums }
.track { height:16px; background:var(--sunk); position:relative; margin-top:16px }
.track .ci { position:absolute; top:-8px; height:32px; background:#BFDDCB }
.track .pt { position:absolute; top:-14px; width:4px; height:44px;
             background:var(--accent) }
.stamp { font-family:var(--mono); font-size:44px; letter-spacing:.14em;
         border:3px solid var(--accent); color:var(--accent); padding:16px 28px;
         display:inline-block; margin-top:40px }

.bar { height:34px; background:var(--sunk); position:relative; margin:9px 0 15px }
.bar i { position:absolute; inset:0 auto 0 0; background:var(--accent) }
.bar b { position:absolute; left:13px; top:4px; font-family:var(--mono);
         font-size:21px; font-weight:500; color:#fff }
.glab { font-family:var(--mono); font-size:22px; display:flex;
        justify-content:space-between }

figure { margin:0; flex:1 1 auto; min-height:0; display:flex;
         align-items:center; justify-content:center }
figure img { max-width:100%; max-height:100%; object-fit:contain }
/* Absolutely positioned from PLATE_BOX, because PIL pastes at exactly those
   coordinates. Left in normal flow the box lands wherever the headline above
   it happens to wrap, and the receptor is composited over the caption. */
.hole { position:absolute; left:HOLE_Lpx; top:HOLE_Tpx;
        width:HOLE_Wpx; height:HOLE_Hpx }
.plate-cap { position:absolute; left:76px; right:76px; top:HOLE_Bpx }
"""
CSS = (CSS.replace("HOLE_L", str(PLATE_BOX[0])).replace("HOLE_T", str(PLATE_BOX[1]))
          .replace("HOLE_W", str(PLATE_BOX[2] - PLATE_BOX[0]))
          .replace("HOLE_H", str(PLATE_BOX[3] - PLATE_BOX[1]))
          .replace("HOLE_B", str(PLATE_BOX[3] + 22)))


def page(body: str, dark: bool = False) -> str:
    return (f'<meta charset="utf-8"><style>{fonts()}{CSS}</style>'
            f'<body class="{"dark" if dark else ""}">{body}</body>')


def angstrom(text: str) -> str:
    """Angstrom units in prose written for a terminal, as &Aring;."""
    return re.sub(r"(?<=\d) A\b", " &Aring;", text)


def n(v: float, d: int = 0) -> str:
    return f"{v:,.{d}f}"


# ── scenes ───────────────────────────────────────────────────────────────────
def scene_title() -> list[tuple[str, int]]:
    """The title card, and the video's thumbnail.

    It names the gap rather than the parts: RFdiffusion-class backbone
    generation is commoditised, and what is actually unsolved is deciding WHAT
    to bind — which target, which structure of it, which face of that
    structure. That is the work the four reasoning stages do, so it is what the
    first frame should claim.

    Held longer than a section beat: it is the frame a scroller decides on, and
    the frame LinkedIn shows before the video plays.
    """
    return [(page(
        f'<div class="eyebrow">Little Protein Tiger</div>'
        f'<h1>Diffusion models can design a binder.<br>'
        f'<span class="muted">Deciding what to bind is the hard part.</span></h1>'
        f'<div class="rule" style="margin-top:52px"></div>'
        f'<p>One sentence in. Target, structure, epitope, budget and '
        f'{n(F["n_survivors"])} candidates out.</p>'
        f'<figure style="margin-top:20px"><img src="{img("hero_receptor")}" alt="">'
        f'</figure>', dark=True), HOLD + 30)]



def scene_typing() -> list[tuple[str, int]]:
    q = F["query"]
    out = []
    for i in range(0, len(q) + 1, 3):
        out.append((page(
            f'<div class="eyebrow">Little Protein Tiger</div>'
            f'<h1>This was the entire brief.</h1>'
            f'<div class="term">&gt; {q[:i]}<span class="caret"></span></div>'
            f'<div class="grow"></div>', dark=True), TYPE_STEP))
    out.append((page(
        f'<div class="eyebrow">Little Protein Tiger</div>'
        f'<h1>This was the entire brief.</h1>'
        f'<div class="term">&gt; {q}</div>'
        f'<p class="muted" style="margin-top:44px">No target named. No structure '
        f'given. No epitope chosen.</p>'
        f'<div class="grow"></div>', dark=True), HOLD))
    return out


def scene_targets() -> list[tuple[str, int]]:
    rows = []
    for tier, gene, _why, _risk, entry in F["tiers"]:
        rows.append(f'<div class="row"><span>{gene}</span>'
                    f'<span class="t">{tier} &middot; {entry.split(" (")[0]}</span></div>')
    out = []
    for k in range(1, 4):
        shown = "".join(rows[:k]) + "".join(
            r.replace('class="row"', 'class="row off"') for r in rows[k:])
        out.append((page(
            f'<div class="eyebrow">It picked the target itself</div>'
            f'<h2>Three candidates, ranked by evidence.</h2>{shown}'
            f'<div class="grow"></div>'), BUILD))
    picked = rows[0].replace('class="row"', 'class="row pick"') + "".join(rows[1:])
    out.append((page(
        f'<div class="eyebrow">It picked the target itself</div>'
        f'<h2>Three candidates, ranked by evidence.</h2>{picked}'
        f'<p style="margin-top:40px">CALCRL / RAMP1 &mdash; the CGRP receptor. '
        f'The target class of an approved migraine antibody.</p>'
        f'<div class="grow"></div>'), HOLD))
    return out


def scene_structure() -> list[tuple[str, int]]:
    sw = F["switch"]
    a, b = sw["profiles"][sw["from"]], sw["profiles"][sw["to"]]

    def block(pdb, prof, label, pick):
        return (f'<div class="row{" pick" if pick else ""}">'
                f'<span>{pdb}</span>'
                f'<span class="t">{prof["res"]} &Aring; &middot; '
                f'{prof["target_len"]} res &middot; '
                f'{prof["scaffold"]} scaffolding chains</span></div>'
                f'<p class="muted" style="font-size:28px;margin:10px 0 26px">{label}</p>')

    head = ('<div class="eyebrow">Then it overruled the literature</div>'
            '<h2>The best-known structure is not the best one to design on.</h2>')
    return [
        (page(head + block(sw["from"], a, "what the papers cite", False)
              + block(sw["to"], b, "&nbsp;", False).replace(sw["to"], "&mdash;")
              + '<div class="grow"></div>'), BUILD + 16),
        (page(head + block(sw["from"], a, "what the papers cite", False)
              + block(sw["to"], b, "what it designed on instead", True)
              + f'<div class="card"><p style="font-size:30px">'
              f'{angstrom(sw["reason"])}</p></div>'
              + '<div class="grow"></div>'), HOLD),
    ]


def scene_epitope() -> list[tuple[str, int]]:
    head = ('<div class="eyebrow">Where a drug can actually reach</div>'
            '<h2>On a membrane receptor, most of the surface is useless.</h2>')
    fig = f'<figure><img src="{img("hero_apo")}" alt=""></figure>'
    return [
        (page(head + fig), BUILD + 16),
        (page(head + fig + f'<div class="stats">'
              f'<div><div class="n">{len(F["hotspots"])}</div>'
              f'<div class="k">hotspots</div></div>'
              f'<div><div class="n">{F["trim_residues"]}</div>'
              f'<div class="k">residues kept</div></div>'
              f'<div><div class="n sm">dropped</div>'
              f'<div class="k">transmembrane face</div></div></div>'), HOLD),
    ]


def scene_trial() -> list[tuple[str, int]]:
    b = CAL["backbone"]
    lo, hi, pt = b["p_low"] * 100, b["p_high"] * 100, b["p_hat"] * 100
    span = 30.0
    head = ('<div class="eyebrow">It never scales straight to production</div>'
            '<h2>A trial first, to measure its own hit rate.</h2>')
    out = []
    for frac in (0.0, 0.35, 0.7, 0.92, 1.0):                 # the counter
        out.append((page(head + f'<div class="big" style="margin-top:56px">'
                         f'{pt * frac:.1f}%</div>'
                         f'<p class="muted" style="margin-top:24px">of backbones '
                         f'cleared the bar</p><div class="grow"></div>'), COUNTER))
    tail = (f'<div class="big" style="margin-top:56px">{pt:.1f}%</div>'
            f'<p class="muted" style="margin-top:24px">{b["k"]} of {n(b["n"])} '
            f'backbones cleared the bar</p>'
            f'<div class="track" style="margin-top:64px">'
            f'<div class="ci" style="left:{lo / span * 100:.2f}%;'
            f'width:{(hi - lo) / span * 100:.2f}%"></div>'
            f'<div class="pt" style="left:{pt / span * 100:.2f}%"></div></div>'
            f'<p class="muted" style="font-size:26px;margin-top:30px">'
            f'95% Wilson interval {lo:.1f}&ndash;{hi:.1f}%. The campaign is sized '
            f'on the pessimistic end.</p>')
    out.append((page(head + tail + '<div class="grow"></div>'), BUILD + 12))
    out.append((page(head + tail
                     + f'<div class="stamp">{CAL["verdict"].replace("_", " ")}</div>'
                     + f'<p style="margin-top:26px;font-size:30px">and it raised '
                     f'its own success bar from {CAL["requested_bar"]} to '
                     f'{CAL["bar_raised_to"]}</p>'
                     + '<div class="grow"></div>'), HOLD))
    return out


def scene_funnel() -> list[tuple[str, int]]:
    head = ('<div class="eyebrow">Production, then eight gates</div>'
            '<h2>Confidence is not enough. Geometry decides.</h2>')
    gates = sorted(F["gates"], key=lambda g: g[2])
    stats = (f'<div class="stats">'
             f'<div><div class="n">{n(F["n_rfd3"])}</div><div class="k">backbones</div></div>'
             f'<div><div class="n">{n(F["n_scored"])}</div><div class="k">refolds</div></div>'
             f'<div><div class="n">{n(F["n_survivors"])}</div><div class="k">survivors</div></div>'
             f'</div>')
    out = [(page(head + stats + '<div class="grow"></div>'), BUILD + 16)]
    for k in (3, 6, 8):
        bars = "".join(
            f'<div class="glab"><span>{g[0]}</span>'
            f'<span class="muted">{g[2]:.1f}%</span></div>'
            f'<div class="bar"><i style="width:{g[2]:.1f}%"></i>'
            f'<b>{n(g[1])}</b></div>' for g in gates[:k])
        out.append((page(head + stats
                         + f'<div style="margin-top:34px">{bars}</div>'
                         + '<div class="grow"></div>'), BUILD if k < 8 else HOLD))
    return out


def scene_design() -> list[tuple[str, int]]:
    d = F["designs"][0]
    head = ('<div class="eyebrow">The lead candidate</div>'
            f'<h2>{d["len"]} residues, docked to {d["dock"]:.2f} &Aring;.</h2>')
    fig = f'<figure><img src="{img("pain_design")}" alt=""></figure>'
    return [
        (page(head + fig), 30),
        (page(head + fig + f'<div class="stats">'
              f'<div><div class="n">{d["iptm"]:.3f}</div><div class="k">interface ipTM</div></div>'
              f'<div><div class="n">{d["dock"]:.2f} &Aring;</div><div class="k">dock RMSD</div></div>'
              f'<div><div class="n">{d["engagement"] * 100:.0f}%</div>'
              f'<div class="k">hotspots engaged</div></div></div>'), HOLD),
    ]


def plate(caption: str) -> str:
    """The turntable's surround. `.hole` reserves exactly PLATE_BOX."""
    return page(
        f'<div class="eyebrow">On the full-length receptor</div>'
        f'<h2 style="font-size:60px">It lands on the agonist’s own site.</h2>'
        f'<div class="hole"></div>'
        f'<div class="plate-cap">{caption}</div>', dark=True)


def scene_end() -> list[tuple[str, int]]:
    return [(page(
        f'<div class="eyebrow">Little Protein Tiger</div>'
        f'<h1>Free for any non&#8209;commercial use.</h1>'
        f'<p style="margin-top:44px">Source-available under PolyForm '
        f'Noncommercial 1.0.0. Four campaigns written up end to end, every '
        f'figure extracted from the run that produced it.</p>'
        f'<div class="rule"></div>'
        f'<p style="font-family:var(--mono);font-size:33px;'
        f'color:var(--dark-accent)">{SITE}</p>'
        f'<div class="grow"></div>', dark=True), HOLD + 42)]   # the CTA: long enough to read a URL and keep it


# ── rendering ────────────────────────────────────────────────────────────────
def shoot(jobs: list[tuple[int, str]], tmp: pathlib.Path) -> None:
    """One Chrome launch per still, WORKERS at a time, profiles reused."""
    def one(job):
        i, html = job
        src = tmp / f"p{i:04d}.html"
        src.write_text(html, encoding="utf-8")
        subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={tmp}/prof{i % WORKERS}", "--hide-scrollbars",
             f"--window-size={W},{H}", f"--screenshot={tmp}/s{i:04d}.png",
             str(src)], capture_output=True, timeout=180, check=False)
        out = tmp / f"s{i:04d}.png"
        if not out.is_file():
            raise SystemExit(f"Chrome produced no screenshot for still {i}")

    with cf.ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(one, jobs))


def main() -> int:
    if CHROME is None:
        raise SystemExit("no Chrome/Chromium on PATH")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not on PATH")
    turns = sorted(TURNTABLE.glob("hero_*.png"))
    if not turns:
        raise SystemExit("no turntable frames — run "
                         "`render_hero.py --turntable` first")

    stills: list[tuple[str, int]] = []
    for scene in (scene_title, scene_typing, scene_targets, scene_structure,
                  scene_epitope, scene_trial, scene_funnel, scene_design):
        stills += scene()
    caption = (f'<p class="muted" style="font-size:27px">Superposed onto '
               f'{CHECK["pdb_id"]}, the receptor with CGRP bound &mdash; '
               f'{CHECK["cgrp"]["binder_atoms_in_contact"]} binder atoms within '
               f'{CHECK["cutoff_A"]} &Aring; of the agonist. The campaign never '
               f'saw it.</p>')
    plate_idx = len(stills)
    stills.append((plate(caption), 0))            # composited, not held directly
    stills += scene_end()

    print(f"  {len(stills)} stills, {len(turns)} turntable frames")
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        shoot(list(enumerate(h for h, _ in stills)), tmp)

        from PIL import Image
        frames = tmp / "frames"
        frames.mkdir()
        k = 0

        def emit(im: "Image.Image", count: int) -> None:
            nonlocal k
            for _ in range(count):
                k += 1
                im.save(frames / f"{k:05d}.png")

        for i, (_html, hold) in enumerate(stills):
            if i == plate_idx:
                base = Image.open(tmp / f"s{i:04d}.png").convert("RGB")
                bw, bh = PLATE_BOX[2] - PLATE_BOX[0], PLATE_BOX[3] - PLATE_BOX[1]
                for j, t in enumerate(turns):
                    tf = Image.open(t).convert("RGBA")
                    tf.thumbnail((bw, bh), Image.LANCZOS)
                    im = base.copy()
                    im.paste(tf, (PLATE_BOX[0] + (bw - tf.width) // 2,
                                  PLATE_BOX[1] + (bh - tf.height) // 2), tf)
                    emit(im, 1)
                    if j == len(turns) - 1:
                        emit(im, TURN_HOLD)   # hold the last turn to read the caption
                continue
            if hold:
                emit(Image.open(tmp / f"s{i:04d}.png").convert("RGB"), hold)

        shutil.copy(tmp / "s0000.png", POSTER)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS),
             "-i", str(frames / "%05d.png"), "-c:v", "libx264",
             "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "slow",
             "-movflags", "+faststart", str(OUT)], check=True)

    print(f"  {OUT.relative_to(HERE.parent.parent)}  {k} frames  "
          f"{k / FPS:.1f} s  {OUT.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"  {POSTER.relative_to(HERE.parent.parent)}  (upload as the thumbnail)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
