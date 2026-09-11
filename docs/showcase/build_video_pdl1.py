#!/usr/bin/env python3
"""The binder-track hook video: assets/lpt_hook_pdl1.mp4 (1080x1350, silent).

    .venv/bin/python docs/showcase/render_pdl1.py --only-turntable   # once
    .venv/bin/python docs/showcase/build_video_pdl1.py

**Two videos, two entry points**, exactly as there are two carousels.
`build_video.py` opens on one sentence about a disease and sells the four
reasoning stages that decide WHAT to bind; this one opens on a target the
viewer already has and sells everything downstream of that — nine solved
structures ranked on their measured interfaces, a textbook epitope that is
wrong on this entry, a trial that raised its own success bar, eight gates, and
an independent cross-check against a complex the campaign never saw. Most
people arriving at this repo already know their target, so this is the video
that answers their question. Neither replaces the other.

It imports its palette, type scale, layout primitives, pacing and the
Chrome/ffmpeg machinery from `build_video.py` rather than copying them: two
videos posted together that share a palette but drift in type scale read as
two projects. What lives here is only what is genuinely different — which
scenes exist, and what each one claims.

Every figure comes from `facts/campaign_pdl1.json`, the same snapshot
`campaign_pdl1.html` is built from, so the video cannot quote a number the
page has moved on from. Nothing is typed in.

The turntable is `assets/turntable_pdl1/` — its OWN directory, not the
`assets/turntable/` that `render_hero.py` writes for the PPI video. One shared
folder would mean whichever render ran last silently decided what both videos
showed.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Shared design system and machinery. `build_video` guards its own build
# behind `__main__`, so importing it only loads facts.
from build_video import (                                    # noqa: E402
    BUILD, BUILD_FAST, CHROME, COUNTER, FPS, H, HOLD, PLATE_BOX, TURN_HOLD,
    TYPE_STEP, W, angstrom, img, n, page, shoot,
)

ASSETS = HERE / "assets"
TURNTABLE = ASSETS / "turntable_pdl1"
OUT = ASSETS / "lpt_hook_pdl1.mp4"
POSTER = ASSETS / "lpt_hook_pdl1_poster.png"

F = json.loads((HERE / "facts/campaign_pdl1.json").read_text(encoding="utf-8"))
CAL = F["calibration"]
PROD = F["production"]
GATE = F["gate_current"]
GEO = F["geometry"]
ZQK = F["zqk"]
LEAD = F["designs"][0]

#: The command the campaign was actually launched with. Rebuilt from the facts
#: rather than pasted, so it cannot drift from the run it claims to show.
COMMAND = (f"run_pipeline.py --workflow binder --target PD-L1 "
           f"--budget {F['budget_cap_usd']:.0f}")

#: `.term` ships `word-break:break-word`, which is right for the other
#: video's prose query and wrong for a command line: it broke `--target`
#: across two lines as `-` then `-target`, which reads as a typo in the one
#: frame a viewer is most likely to screenshot. No token here is long enough
#: to overflow, so breaking only at spaces is safe.
TERM_WRAP = "margin-top:64px;word-break:normal;overflow-wrap:normal"


# ── scenes ───────────────────────────────────────────────────────────────────
def scene_title() -> list[tuple[str, int]]:
    """The frame a scroller decides on, so it names the viewer's own position.

    The other video argues that choosing the target is the hard part. That is
    true and it is not this audience's problem: they have a target. So this
    opens on what is still unsolved once you have one — which structure of it,
    which face of that structure, how many designs to pay for, and which of
    the results are real.
    """
    return [(page(
        f'<div class="eyebrow">Little Protein Tiger</div>'
        f'<h1>You already know your target.<br>'
        f'<span class="muted">Everything after that is still a decision.</span>'
        f'</h1>'
        f'<div class="rule" style="margin-top:52px"></div>'
        f'<p>One target name in. {n(GATE["survivors"])} gated designs out, '
        f'for ${F["spend_usd"]:.2f} of API spend and '
        f'{F["gpu_hours"]["total"]:.0f} GPU-hours.</p>'
        f'<figure style="margin-top:20px"><img src="{img("design_face")}" alt="">'
        f'</figure>', dark=True), HOLD + 30)]


def scene_typing() -> list[tuple[str, int]]:
    """The whole brief, typed. It is one flag, and that is the point."""
    out = []
    for i in range(0, len(COMMAND) + 1, 3):
        out.append((page(
            f'<div class="eyebrow">Little Protein Tiger</div>'
            f'<h1>This was the entire brief.</h1>'
            f'<div class="term" style="{TERM_WRAP}">$ {COMMAND[:i]}'
            f'<span class="caret"></span></div>'
            f'<div class="grow"></div>'), TYPE_STEP))
    out.append((page(
        f'<div class="eyebrow">Little Protein Tiger</div>'
        f'<h1>This was the entire brief.</h1>'
        f'<div class="term" style="{TERM_WRAP}">$ {COMMAND}</div>'
        f'<p class="muted" style="margin-top:40px">No structure, no epitope, '
        f'no batch count. {F["target_gene"]} resolved to '
        f'{F["uniprot"]} offline, and the run decided the rest.</p>'
        f'<div class="grow"></div>'), HOLD))
    return out


def scene_structures() -> list[tuple[str, int]]:
    """Nine solved complexes, ranked on interfaces it measured itself.

    The reason this beat exists: "use the best structure" is not a sentence a
    pipeline can act on. What it can do is compute every candidate's buried
    area, H-bond count and hydrophobic fraction and rank on those — which is
    how it passed over the 1.64 A entry with the biggest interface and took
    the one whose partner is the closest analogue of what it was about to
    build.
    """
    cands = F["candidates"][:5]
    head = (f'<div class="eyebrow">{len(F["candidates"])} solved '
            f'{F["target_gene"]} complexes</div>'
            f'<h2>It measured every one before choosing.</h2>')
    out = []
    for k in (2, 4, 5):
        rows = "".join(
            f'<div class="row{" pick" if c["pdb_id"] == F["pdb_id"] else ""}">'
            f'<span class="t">{c["pdb_id"]}</span>'
            f'<span class="sm muted">{c["partner"]}</span>'
            f'<span class="n">{n(c["bsa"])}</span>'
            f'<span class="k">&Aring;&sup2;</span></div>'
            for c in cands[:k])
        out.append((page(head + f'<div style="margin-top:36px">{rows}</div>'
                         + '<div class="grow"></div>'),
                    BUILD_FAST if k < 5 else BUILD))
    rows = "".join(
        f'<div class="row{" pick" if c["pdb_id"] == F["pdb_id"] else ""}">'
        f'<span class="t">{c["pdb_id"]}</span>'
        f'<span class="sm muted">{c["partner"]}</span>'
        f'<span class="n">{n(c["bsa"])}</span>'
        f'<span class="k">&Aring;&sup2;</span></div>' for c in cands)
    out.append((page(
        head + f'<div style="margin-top:36px">{rows}</div>'
        + f'<p class="muted" style="margin-top:34px;font-size:27px">It took '
        f'<b>{F["pdb_id"]}</b> — not the largest interface on the list. Its '
        f'crystallised partner is a de novo mini-protein, the closest analogue '
        f'of the thing about to be designed.</p>'
        + '<div class="grow"></div>'), HOLD))
    return out


def scene_grounding() -> list[tuple[str, int]]:
    """The strongest single claim in the run, and it is checkable.

    Every PD-L1 review names Tyr56. On 8ZNL, chain B residue 56 is a valine —
    so the textbook answer, stated confidently, would have been wrong on this
    exact entry. The guard that catches it reads the residue name out of the
    downloaded file rather than trusting the model, which is the difference
    between an epitope and a plausible-looking list of numbers.
    """
    head = ('<div class="eyebrow">Grounding, not recall</div>'
            '<h2>The textbook hotspot is not in this structure.</h2>')
    a = (f'<p style="margin-top:48px">Every review of {F["target_gene"]} names '
         f'<b>Tyr56</b>.</p>')
    b = (f'<p style="margin-top:26px">In <b>{F["pdb_id"]}</b>, chain '
         f'{F["target_chain"]} residue 56 is a <b>valine</b>.</p>')
    c = (f'<p class="muted" style="margin-top:34px;font-size:27px">So the '
         f'interface stage reads the real residue at every position out of the '
         f'downloaded file, and a guard refuses the run if a single name '
         f'disagrees. The {len(F["hotspots"])} it chose here are '
         f'{F["pdb_id"]}&rsquo;s own — anchored on Tyr124 and Tyr57 — with '
         f'{F["citations_verified"]} of {F["citations_checked"]} cited DOIs '
         f'verified against the local corpus.</p>')
    return [(page(head + a + '<div class="grow"></div>'), BUILD),
            (page(head + a + b + '<div class="grow"></div>'), BUILD + 20),
            (page(head + a + b + c + '<div class="grow"></div>'), HOLD)]


def scene_epitope() -> list[tuple[str, int]]:
    hs = F["hotspots"]
    chips = " ".join(f'<span class="k">{h["name"]}{h["auth"]}</span>' for h in hs)
    return [(page(
        f'<div class="eyebrow">Stage 2 &middot; the epitope</div>'
        f'<h2>{len(hs)} residues, each one measured.</h2>'
        f'<figure style="margin-top:28px"><img src="{img("epitope")}" alt="">'
        f'</figure>'
        f'<div style="margin-top:26px">{chips}</div>'
        f'<p class="muted" style="margin-top:22px;font-size:26px">'
        f'{F["bsa_A2"]:,} &Aring;&sup2; buried across the chosen face. The '
        f'target was trimmed to {F["trim_residues"]} residues so RFD3 could '
        f'hold it fixed.</p>'), HOLD)]


def scene_trial() -> list[tuple[str, int]]:
    """It never scales straight to production — and here it scaled the bar UP.

    `adaptive_bar` is the part worth showing: a trial that clears the bar
    comfortably does not mean "go", it means the bar was set too low for this
    target. 33 of 479 backbones at iptm > 0.7, so the campaign was sized at
    0.85 instead and still came back SCALE_UP.
    """
    b = CAL["backbone_rate"]
    lo, hi, pt = b["p_low"] * 100, b["p_high"] * 100, b["p_hat"] * 100
    span = 15.0
    head = ('<div class="eyebrow">It never scales straight to production</div>'
            '<h2>A trial first, to measure its own hit rate.</h2>')
    out = []
    for frac in (0.0, 0.35, 0.7, 0.92, 1.0):
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
            f'95% Wilson interval {lo:.1f}&ndash;{hi:.1f}%. The campaign is '
            f'sized on the pessimistic end.</p>')
    out.append((page(head + tail + '<div class="grow"></div>'), BUILD_FAST + 12))
    out.append((page(head + tail
                     + f'<div class="stamp">{CAL["verdict"].replace("_", " ")}</div>'
                     + f'<p style="margin-top:26px;font-size:30px">and it raised '
                     f'its own success bar from {CAL["requested_bar"]} to '
                     f'<b>{CAL["bar_raised_to"]}</b> — the trial said this '
                     f'target could afford a stricter one.</p>'
                     + '<div class="grow"></div>'), HOLD))
    return out


def scene_funnel() -> list[tuple[str, int]]:
    head = ('<div class="eyebrow">Production, then eight gates</div>'
            '<h2>Confidence is not enough. Geometry decides.</h2>')
    stats = (f'<div class="stats">'
             f'<div><div class="n">{n(PROD["n_rfd3"])}</div>'
             f'<div class="k">backbones</div></div>'
             f'<div><div class="n">{n(PROD["n_rf3"])}</div>'
             f'<div class="k">refolds</div></div>'
             f'<div><div class="n">{n(GATE["survivors"])}</div>'
             f'<div class="k">survivors</div></div></div>')
    gates = sorted(GATE["alone"], key=lambda g: g[2])
    out = [(page(head + stats + '<div class="grow"></div>'), BUILD_FAST + 16)]
    for k in (3, 6, 8):
        bars = "".join(
            f'<div class="glab"><span>{angstrom(g[0])}</span>'
            f'<span class="muted">{g[2]:.1f}%</span></div>'
            f'<div class="bar"><i style="width:{g[2]:.1f}%"></i>'
            f'<b>{n(g[1])}</b></div>' for g in gates[:k])
        out.append((page(head + stats
                         + f'<div style="margin-top:34px">{bars}</div>'
                         + '<div class="grow"></div>'),
                    BUILD_FAST if k < 8 else BUILD))
    out.append((page(
        head + stats
        + f'<p style="margin-top:40px">Of the refolds the model was confident '
        f'about — ipTM above {GEO["iptm_gate"]} — only '
        f'<b>{GEO["pct_docked"]:.1f}%</b> were actually docked on the intended '
        f'site.</p>'
        f'<p class="muted" style="margin-top:24px;font-size:27px">'
        f'{n(GEO["n_confidently_misdocked"])} were confidently mis-docked. '
        f'Confidence reports the interface the model chose, not the one you '
        f'asked for, which is why dock RMSD is a hard gate and not a weight.</p>'
        + '<div class="grow"></div>'), HOLD))
    return out


def plate(caption: str) -> str:
    """A still with a known empty rectangle, for PIL to paste turntable frames.

    Compositing 90 frames through Chrome would be 90 browser launches; one
    plate plus 90 pastes is the same picture in a fraction of the time.
    """
    return page(
        f'<div class="eyebrow">The lead design</div>'
        f'<h2>{LEAD["len"]} residues, folded back onto the target.</h2>'
        f'<div class="hole"></div><div class="plate-cap">{caption}</div>')


def scene_check() -> list[tuple[str, int]]:
    """The one piece of evidence the campaign did not generate itself."""
    head = ('<div class="eyebrow">Independent check</div>'
            '<h2>Would it actually get in PD-1&rsquo;s way?</h2>')
    a = (f'<p style="margin-top:40px">Nothing in this campaign ever saw PD-1. '
         f'So: superpose the real PD-1/PD-L1 complex — <b>{ZQK["pdb_id"]}</b>, '
         f'which took no part in the run — and count what each partner '
         f'touches.</p>')
    fig = (f'<figure style="margin-top:24px"><img src="{img("footprint")}" '
           f'alt=""></figure>')
    stats = (f'<div class="stats" style="margin-top:24px">'
             f'<div><div class="n">{ZQK["pct"]}%</div>'
             f'<div class="k">of PD-1&rsquo;s footprint covered</div></div>'
             f'<div><div class="n">{ZQK["shared"]}/{ZQK["pd1_contacts"]}</div>'
             f'<div class="k">shared contact residues</div></div>'
             f'<div><div class="n">{ZQK["superpose_rmsd_A"]} &Aring;</div>'
             f'<div class="k">superposition RMSD</div></div></div>')
    return [(page(head + a + '<div class="grow"></div>'), BUILD),
            (page(head + a + fig + '<div class="grow"></div>'), BUILD + 14),
            (page(head + a + fig + stats + '<div class="grow"></div>'), HOLD)]


def scene_cost() -> list[tuple[str, int]]:
    return [(page(
        f'<div class="eyebrow">What the whole thing cost</div>'
        f'<h2>${F["spend_usd"]:.2f} of API spend.</h2>'
        f'<div class="stats" style="margin-top:44px">'
        f'<div><div class="n">${F["spend_usd"]:.2f}</div>'
        f'<div class="k">LLM stages, {F["llm_calls"]} calls</div></div>'
        f'<div><div class="n">{F["gpu_hours"]["total"]:.1f}</div>'
        f'<div class="k">GPU-hours, one card</div></div>'
        f'<div><div class="n">{F["top_k_count"]}</div>'
        f'<div class="k">ranked designs</div></div></div>'
        f'<p style="margin-top:44px">Three model calls decided the target, the '
        f'epitope and the write-up. Everything between them — the trim, the '
        f'spec, the trial, the sizing, the gates, the ranking — is '
        f'deterministic Python.</p>'
        f'<p class="muted" style="margin-top:26px;font-size:26px">Default '
        f'provider {F["default_provider"]} ({F["default_model"]}), against a '
        f'${F["budget_cap_usd"]:.0f} cap it never came close to.</p>'
        f'<div class="grow"></div>'), HOLD + 20)]


def scene_end() -> list[tuple[str, int]]:
    return [(page(
        f'<div class="eyebrow">Little Protein Tiger</div>'
        f'<h1>Every design here is an<br>unvalidated hypothesis.</h1>'
        f'<p style="margin-top:44px">A sequence and a predicted pose. Nothing '
        f'on this page has been expressed, purified or measured, and screening '
        f'anything you synthesise is your responsibility.</p>'
        f'<div class="rule" style="margin-top:48px"></div>'
        f'<p class="muted" style="margin-top:28px">'
        f'github.com/MichaUckelmann/little-protein-tiger '
        f'&middot; docs/responsible-use.md</p>'
        f'<div class="grow"></div>', dark=True), HOLD + 30)]


def main() -> int:
    if CHROME is None:
        raise SystemExit("no Chrome/Chromium on PATH")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not on PATH")
    turns = sorted(TURNTABLE.glob("pdl1_*.png"))
    if not turns:
        raise SystemExit(
            "no turntable frames — run `render_pdl1.py --only-turntable` first")

    stills: list[tuple[str, int]] = []
    for scene in (scene_title, scene_typing, scene_structures, scene_grounding,
                  scene_epitope, scene_trial, scene_funnel):
        stills += scene()
    caption = (f'<p class="muted" style="font-size:27px">ipTM '
               f'{LEAD["iptm"]:.3f} &middot; dock RMSD {LEAD["dock"]:.2f} '
               f'&Aring; &middot; pLDDT {LEAD["plddt"]:.2f} &middot; ipSAE '
               f'{LEAD["ipsae_min"]:.3f}. Its own RF3 refold, not the '
               f'design model.</p>')
    plate_idx = len(stills)
    stills.append((plate(caption), 0))            # composited, not held directly
    stills += scene_check()
    stills += scene_cost()
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
                bw = PLATE_BOX[2] - PLATE_BOX[0]
                bh = PLATE_BOX[3] - PLATE_BOX[1]
                # Crop every frame to ONE box — the union of all 90 bounding
                # boxes — before scaling into the plate. Scaling the raw
                # 1080x1080 canvases fits their transparent margins too, so
                # the complex arrived noticeably smaller than the plate it
                # sits in; and cropping each frame to its OWN box would
                # rescale the model on every frame, which reads as the
                # structure breathing rather than turning.
                loaded = [Image.open(t).convert("RGBA") for t in turns]
                boxes = [im.getbbox() for im in loaded if im.getbbox()]
                union = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                         max(b[2] for b in boxes), max(b[3] for b in boxes))
                for j, tf in enumerate(loaded):
                    tf = tf.crop(union)
                    tf.thumbnail((bw, bh), Image.LANCZOS)
                    im = base.copy()
                    im.paste(tf, (PLATE_BOX[0] + (bw - tf.width) // 2,
                                  PLATE_BOX[1] + (bh - tf.height) // 2), tf)
                    emit(im, 1)
                    if j == len(turns) - 1:
                        emit(im, TURN_HOLD)
                continue
            if hold:
                emit(Image.open(tmp / f"s{i:04d}.png").convert("RGB"), hold)

        shutil.copy(tmp / "s0000.png", POSTER)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS),
             "-i", str(frames / "%05d.png"), "-c:v", "libx264",
             "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "slow",
             "-movflags", "+faststart", str(OUT)], check=True)

    root = HERE.parent.parent
    print(f"  {OUT.relative_to(root)}  {k} frames  {k / FPS:.1f} s  "
          f"{OUT.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"  {POSTER.relative_to(root)}  (upload as the thumbnail)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
