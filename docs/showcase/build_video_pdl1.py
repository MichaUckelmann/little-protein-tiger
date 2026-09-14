#!/usr/bin/env python3
"""The binder-track hook video: assets/lpt_hook_pdl1.mp4 (1080x1350, silent).

    .venv/bin/python docs/showcase/render_pdl1.py --only-turntable        # once
    .venv/bin/python docs/showcase/render_pdl1.py --only-macro-turntable  # once
    .venv/bin/python docs/showcase/build_video_pdl1.py

**Two videos, two entry points**, exactly as there are two carousels.
`build_video.py` opens on one sentence about a disease and sells the four
reasoning stages that decide WHAT to bind; this one opens on a target the
viewer already has and sells everything downstream of that — nine solved
structures ranked on their measured interfaces, the epitope those measurements
picked, a trial that raised its own success bar, the lead design, the SAME
target designed again as a cyclic peptide on a second engine, and an
independent cross-check against a complex neither campaign ever saw. Most
people arriving at this repo already know their target, so this is the video
that answers their question. Neither replaces the other.

It used to carry a "the textbook hotspot is not in this structure" beat
(Tyr56 against 8ZNL's valine at 56) and a production-funnel beat. The first
was withdrawn because the claim is wrong as stated — 8ZNL numbers its chain B
one higher than the canonical sequence, so the residue every review calls
Tyr56 is auth 57 there, and the interface stage DID select it; that is an
indexing frame, not a missing hotspot. The second went because the video was
running long and the funnel is the beat the page does better than 7 seconds
of bars can.

It imports its palette, type scale, layout primitives, pacing and the
Chrome/ffmpeg machinery from `build_video.py` rather than copying them: two
videos posted together that share a palette but drift in type scale read as
two projects. What lives here is only what is genuinely different — which
scenes exist, and what each one claims.

Every figure comes from `facts/campaign_pdl1.json`, the same snapshot
`campaign_pdl1.html` is built from, so the video cannot quote a number the
page has moved on from. Nothing is typed in.

There are TWO turntables, `assets/turntable_pdl1/` (foundry, 8ZNL) and
`assets/turntable_pdl1_macro/` (BoltzGen, 7CZD) — each its OWN directory, and
neither the `assets/turntable/` that `render_hero.py` writes for the PPI
video. One shared folder would mean whichever render ran last silently decided
what every video showed, and here the two hold different projects.
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
    TYPE_STEP, W, img, n, page, shoot,
)

ASSETS = HERE / "assets"
TURNTABLE = ASSETS / "turntable_pdl1"
MACRO_TURNTABLE = ASSETS / "turntable_pdl1_macro"
OUT = ASSETS / "lpt_hook_pdl1.mp4"
POSTER = ASSETS / "lpt_hook_pdl1_poster.png"

F = json.loads((HERE / "facts/campaign_pdl1.json").read_text(encoding="utf-8"))
CAL = F["calibration"]
GATE = F["gate_current"]
ZQK = F["zqk"]
LEAD = F["designs"][0]
#: The second campaign: same target, BoltzGen, cyclic peptide. Its own block
#: in the snapshot — a different project (7CZD) with BoltzGen-native columns,
#: so nothing here is mixed into the foundry numbers above.
MAC = F["macrocycle"]
MAC_LEAD = MAC["designs"][0]

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
        f'{F["gpu_hours"]["total"]:.0f} GPU-hours '
        f'<span class="muted">({F["gpu_name"]})</span>.</p>'
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


def plate(caption: str) -> str:
    """A still with a known empty rectangle, for PIL to paste turntable frames.

    Compositing 90 frames through Chrome would be 90 browser launches; one
    plate plus 90 pastes is the same picture in a fraction of the time.
    """
    return page(
        f'<div class="eyebrow">The lead design</div>'
        f'<h2>{LEAD["len"]} residues, folded back onto the target.</h2>'
        f'<div class="hole"></div><div class="plate-cap">{caption}</div>')


def legend() -> str:
    """The footprint figure's own colour key.

    The swatches are IMPORTED from the renderer that painted the figure rather
    than restated here — `render_pdl1.py` colours `footprint.webp` with exactly
    these four constants, and a key that says something else is worse than no
    key at all. The campaign page's legend reads the same values.

    Inline styles because the stylesheet in `build_video.py` has no legend
    primitive and belongs to BOTH videos; only this one has a figure that needs
    one, so the styling stays local rather than growing the shared CSS for a
    single caller.
    """
    from render_pdl1 import BINDER_COL, HOTSPOT_COL, PD1_COL, TARGET_COL

    keys = [
        (HOTSPOT_COL, f'both ({ZQK["shared"]})'),
        (PD1_COL, f'PD-1 only ({len(ZQK["pd1_only"])})'),
        (BINDER_COL, f'design only ({ZQK["design_contacts"] - ZQK["shared"]})'),
        (TARGET_COL, "neither"),
    ]
    swatches = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:11px">'
        f'<b style="width:26px;height:26px;background:{col};'
        f'display:inline-block;flex:none"></b>{label}</span>'
        for col, label in keys)
    return (f'<div class="muted" style="display:flex;flex-wrap:wrap;'
            f'gap:14px 26px;margin-top:20px;font-family:var(--mono);'
            f'font-size:23px">{swatches}</div>')


def plate_macro(caption: str) -> str:
    """The same plate box, for the second campaign's turntable.

    Deliberately its own function rather than an argument to `plate()`: the
    two scenes make different claims and PLATE_BOX is the only thing they
    actually share. The headline is the beat — RFD3 has no cyclic-peptide path
    at all, so this is not the first campaign at a smaller size, it is the same
    stage machine driving a different generator.
    """
    return page(
        f'<div class="eyebrow">Same target, other engine</div>'
        f'<h2>BoltzGen enables macrocycle design.</h2>'
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
           f'alt=""></figure>' + legend())
    stats = (f'<div class="stats" style="margin-top:18px">'
             f'<div><div class="n">{ZQK["pct"]}%</div>'
             f'<div class="k">of PD-1&rsquo;s footprint covered</div></div>'
             f'<div><div class="n">{ZQK["shared"]}/{ZQK["pd1_contacts"]}</div>'
             f'<div class="k">shared contact residues</div></div>'
             f'<div><div class="n">{ZQK["superpose_rmsd_A"]} &Aring;</div>'
             f'<div class="k">superposition RMSD</div></div></div>')
    return [(page(head + a + '<div class="grow"></div>'), BUILD),
            (page(head + a + fig + '<div class="grow"></div>'), BUILD + 14),
            (page(head + a + fig + stats + '<div class="grow"></div>'), HOLD)]


def cost_row(label: str, spend: float, calls: int, gpu: float,
             ranked: int) -> str:
    """One campaign's row of the cost scene.

    Both rows report the same three figures — LLM spend, GPU-hours, ranked
    designs — because those are the three that are directly comparable between
    the two campaigns. Everything else (the gate, the bar, the ranker) is
    engine-specific and side by side would read as a head-to-head it is not.
    """
    return (f'<div class="k" style="margin-top:30px">{label}</div>'
            f'<div class="stats" style="margin-top:12px">'
            f'<div><div class="n sm">${spend:.2f}</div>'
            f'<div class="k">LLM stages, {calls} calls</div></div>'
            f'<div><div class="n sm">{gpu:.1f}</div>'
            f'<div class="k">GPU-hours</div></div>'
            f'<div><div class="n sm">{ranked}</div>'
            f'<div class="k">ranked designs</div></div></div>')


def scene_cost() -> list[tuple[str, int]]:
    """Both campaigns, priced the same way.

    The headline is the SUM, because two rows under a single-campaign total
    would read as a contradiction. The foundry row is unchanged; the second is
    the macrocycle campaign, whose GPU-hours include the production stage's
    two legs (it was killed and resumed) and exclude the idle night between
    them.
    """
    total = F["spend_usd"] + MAC["spend_usd"]
    return [(page(
        f'<div class="eyebrow">What the whole thing cost</div>'
        f'<h2>Two campaigns, ${total:.2f} of API spend.</h2>'
        + cost_row(f'mini-protein &middot; foundry &middot; {F["pdb_id"]}',
                   F["spend_usd"], F["llm_calls"], F["gpu_hours"]["total"],
                   F["top_k_count"])
        + cost_row(f'{MAC["modality"].replace("_", " ")} &middot; '
                   f'{MAC["engine"]} &middot; {MAC["pdb_id"]}',
                   MAC["spend_usd"], MAC["llm_calls"],
                   MAC["gpu_hours"]["total"], MAC["top_k_count"])
        + f'<p style="margin-top:38px">Three model stages per campaign decided '
          f'the target, the epitope and the write-up. Everything between them '
          f'— the trim, the spec, the trial, the sizing, the gates, the '
          f'ranking — is deterministic Python.</p>'
          f'<p class="muted" style="margin-top:22px;font-size:26px">Default '
          f'provider {F["default_provider"]} ({F["default_model"]}) on both, '
          f'against caps neither came close to.</p>'
        + '<div class="grow"></div>'), HOLD + 20)]


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
    macro_turns = sorted(MACRO_TURNTABLE.glob("macro_*.png"))
    if not macro_turns:
        raise SystemExit(
            "no macrocycle turntable frames — run `render_pdl1.py "
            "--only-macro-turntable` first")

    stills: list[tuple[str, int]] = []
    for scene in (scene_title, scene_typing, scene_structures,
                  scene_epitope, scene_trial):
        stills += scene()
    caption = (f'<p class="muted" style="font-size:27px">ipTM '
               f'{LEAD["iptm"]:.3f} &middot; dock RMSD {LEAD["dock"]:.2f} '
               f'&Aring; &middot; pLDDT {LEAD["plddt"]:.2f} &middot; ipSAE '
               f'{LEAD["ipsae_min"]:.3f}. Its own RF3 refold, not the '
               f'design model.</p>')
    # BoltzGen's own columns, and only those: it writes no PAE matrix for
    # ipSAE and no dock RMSD, so the caption cannot mirror the one above and
    # does not pretend to.
    macro_caption = (
        f'<p class="muted" style="font-size:27px">{MAC_LEAD["len"]} residues, '
        f'cyclic &middot; ipTM {MAC_LEAD["iptm"]:.3f} &middot; interface PAE '
        f'{MAC_LEAD["ipae"]:.2f} &Aring; &middot; complex pLDDT '
        f'{MAC_LEAD["plddt"]:.3f}. RFD3 has no cyclic path, so '
        f'<span style="font-family:var(--mono)">--modality '
        f'{MAC["modality"]}</span> picks {MAC["engine"]} and the rest of the '
        f'run is unchanged.</p>')
    # Every plate index maps to the frames PIL must paste into it; the compose
    # loop below reads this rather than comparing against one index, so a
    # third turntable scene needs no change there.
    plates: dict[int, list[pathlib.Path]] = {}
    plates[len(stills)] = turns
    stills.append((plate(caption), 0))            # composited, not held directly
    stills += scene_check()
    plates[len(stills)] = macro_turns
    stills.append((plate_macro(macro_caption), 0))
    stills += scene_cost()
    stills += scene_end()

    print(f"  {len(stills)} stills, {len(turns)} + {len(macro_turns)} "
          f"turntable frames")
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
            if i in plates:
                frames_in = plates[i]
                base = Image.open(tmp / f"s{i:04d}.png").convert("RGB")
                bw = PLATE_BOX[2] - PLATE_BOX[0]
                bh = PLATE_BOX[3] - PLATE_BOX[1]
                # Crop every frame to ONE box — the union of that turntable's
                # own 90 bounding boxes — before scaling into the plate.
                # Scaling the raw 1080x1080 canvases fits their transparent
                # margins too, so the complex arrived noticeably smaller than
                # the plate it sits in; and cropping each frame to its OWN box
                # would rescale the model on every frame, which reads as the
                # structure breathing rather than turning. The union is per
                # turntable, never across both: the macrocycle complex is
                # physically smaller, and one shared box would shrink it again.
                loaded = [Image.open(t).convert("RGBA") for t in frames_in]
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
                    if j == len(loaded) - 1:
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
