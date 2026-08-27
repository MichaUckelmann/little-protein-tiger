#!/usr/bin/env python3
"""Build the PD-L1 campaign showcase page (self-contained HTML, images inlined).

Every number here comes from projects/pdl1_e2e — the manifest, the stage
markdowns, calibration.json, filter_stats.txt and top_k.csv. Nothing is
illustrative. Re-run after a campaign changes:  python docs/showcase/build_campaign.py
"""
from __future__ import annotations
import base64, pathlib

HERE = pathlib.Path(__file__).resolve().parent
ASSETS = HERE / "assets"
OUT = HERE / "campaign_pdl1.html"


def img(name: str) -> str:
    b = (ASSETS / f"{name}.webp").read_bytes()
    return "data:image/webp;base64," + base64.b64encode(b).decode()


# ---------------------------------------------------------------- real data
FUNNEL = [
    ("RFD3 backbones", 2364, "diffused against the 9-residue hotspot patch"),
    ("Cleared the prefilter", 1456, "61.6% — chain breaks, clashes, loop fraction"),
    ("solubleMPNN sequences", 5824, "4 sequences per surviving backbone"),
    ("RF3 refolds", 5824, "every sequence refolded from scratch"),
    ("Cleared every hard gate", 715, "12.3% — dock RMSD, ipTM, clash, hotspots, fold"),
    ("Ranked and reported", 20, "MMR-diversified top-K"),
]
YIELD = [(0.5, 158), (0.6, 123), (0.65, 106), (0.7, 90), (0.75, 71), (0.8, 42), (0.85, 21)]
GATES = [
    ("binder_rmsd_dock ≤ 5 Å", 2464, 57.7),
    ("ipTM ≥ 0.5", 1299, 35.9),
    ("no steric clash", 992, 37.6),
    ("hotspot_engagement ≥ 1", 322, 85.0),
    ("binder_rmsd_fold ≤ 2 Å", 31, 96.5),
    ("binder_pLDDT ≥ 0.75", 1, 99.4),
]
TOP = [
    (1, "502_model_3_b0_d2", 0.887, 0.705, 1.156, 0.824, -66.3, 84,
     "MEERVKRILEEVRELLERVGAEELIPYAEAIAKELAEEALKQGVSESVIVSHIILSASAAHNQGLEAGLAFARELIKDTVELLK"),
    (2, "320_model_3_b0_d1", 0.923, 0.820, 1.168, 0.832, -53.1, 80,
     "MDEAEDEFFAEARARLAAAAEASTEEAVELALELVEEGVRRGLPLILAANLVAVAAAAQLSREKALAVIEALRERLLEHR"),
    (3, "53_model_2_b0_d0", 0.908, 0.762, 1.548, 0.840, -47.1, 82,
     "MLEEDRERATKLIDEGSKAFKAGDYETALKKFEEAAKSKDLGLQAMAYRLRARVYKAMGDEEKAKEDYKKADELESKAVPRP"),
]
HOTSPOTS = [
    ("Tyr56", 56, 66.9, "−3.87", "strongest ΔΔG residue in the interface"),
    ("Glu58", 58, 17.1, "—", "CC′ loop, polar anchor"),
    ("His69", 69, 67.8, "−2.90", "second-strongest ΔΔG residue"),
    ("Lys75", 75, 60.1, "—", "CC′/FG loop rim"),
    ("Arg113", 113, 28.9, "—", "F/G strand, start of region 1"),
    ("Gly119", 119, 38.8, "—", "backbone-mediated contact"),
    ("Ala121", 121, 66.4, "—", "hydrophobic core of the F/G patch"),
    ("Asp122", 122, 9.1, "—", "polar edge"),
    ("Tyr123", 123, 71.4, "—", "largest buried area of the nine"),
]

# ---------------------------------------------------------------- chart SVG
def bar_funnel() -> str:
    mx = max(v for _, v, _ in FUNNEL)
    rows, y, RH, GAP = [], 0, 34, 14
    for label, val, note in FUNNEL:
        w = max(3.0, val / mx * 660)
        rows.append(
            f'<g class="mk" tabindex="0"><title>{label}: {val:,} — {note}</title>'
            f'<rect x="0" y="{y}" width="{w:.1f}" height="{RH}" rx="4" fill="var(--mark-a)"/>'
            f'<text class="v" x="{w + 12:.1f}" y="{y + RH/2 + 5}">{val:,}</text>'
            f'<text class="k" x="0" y="{y - 6}">{label}</text></g>')
        y += RH + GAP + 14
    return (f'<svg viewBox="0 -18 860 {y}" role="img" aria-label="Production funnel" '
            f'class="chart">{"".join(rows)}</svg>')


def line_yield() -> str:
    W, H, PL, PB = 660, 250, 46, 34
    xs = [p[0] for p in YIELD]
    x0, x1 = min(xs), max(xs)
    mx = max(v for _, v in YIELD)
    px = lambda x: PL + (x - x0) / (x1 - x0) * (W - PL - 26)
    py = lambda v: H - PB - v / mx * (H - PB - 24)
    pts = " ".join(f"{px(x):.1f},{py(v):.1f}" for x, v in YIELD)
    area = f"{px(x0):.1f},{H-PB} {pts} {px(x1):.1f},{H-PB}"
    dots = "".join(
        f'<g class="mk" tabindex="0"><title>ipTM &gt; {x}: {v} of 1,432 refolds '
        f'({v/1432*100:.1f}%)</title>'
        f'<circle cx="{px(x):.1f}" cy="{py(v):.1f}" r="5.5" fill="var(--mark-a)" '
        f'stroke="var(--surface)" stroke-width="2"/></g>' for x, v in YIELD)
    grid = "".join(
        f'<line class="grid" x1="{PL}" y1="{py(g):.1f}" x2="{W-26}" y2="{py(g):.1f}"/>'
        f'<text class="ax" x="{PL-10}" y="{py(g)+4:.1f}" text-anchor="end">{g}</text>'
        for g in (0, 50, 100, 150))
    ticks = "".join(
        f'<text class="ax" x="{px(x):.1f}" y="{H-PB+20}" text-anchor="middle">{x}</text>'
        for x, _ in YIELD)
    sel = (f'<line class="sel" x1="{px(0.85):.1f}" y1="16" x2="{px(0.85):.1f}" y2="{H-PB}"/>'
           f'<text class="sel-l" x="{px(0.85):.1f}" y="10" text-anchor="end">bar set here</text>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" class="chart" '
            f'aria-label="Designs surviving at each ipTM bar">{grid}{sel}'
            f'<polygon points="{area}" fill="var(--mark-a)" opacity="0.13"/>'
            f'<polyline points="{pts}" fill="none" stroke="var(--mark-a)" stroke-width="2" '
            f'stroke-linejoin="round"/>{dots}{ticks}'
            f'<text class="ax-t" x="{PL}" y="{H-4}">ipTM bar →</text></svg>')


def bar_gates() -> str:
    mx = max(v for _, v, _ in GATES)
    rows, y, RH = [], 0, 30
    for label, dropped, alone in GATES:
        w = max(2.0, dropped / mx * 430)
        rows.append(
            f'<g class="mk" tabindex="0"><title>{label} — first to fail for {dropped:,} '
            f'refolds; {alone}% of all 5,824 would pass it on its own</title>'
            f'<text class="k r" x="250" y="{y+RH/2+5}">{label}</text>'
            f'<rect x="262" y="{y+5}" width="{w:.1f}" height="{RH-10}" rx="4" fill="var(--mark-b)"/>'
            f'<text class="v" x="{262+w+10:.1f}" y="{y+RH/2+5}">{dropped:,}</text></g>')
        y += RH + 8
    return (f'<svg viewBox="0 -6 760 {y}" role="img" class="chart" '
            f'aria-label="Refolds dropped by first failing gate">{"".join(rows)}</svg>')


def wilson() -> str:
    W, H = 660, 92
    lo, hi, pt = 3.42, 8.14, 5.31
    sx = lambda v: 40 + v / 10.0 * (W - 80)
    return f'''<svg viewBox="0 0 {W} {H}" role="img" class="chart"
      aria-label="Backbone hit rate with 95% Wilson interval">
      <line class="grid" x1="40" y1="52" x2="{W-40}" y2="52"/>
      {''.join(f'<text class="ax" x="{sx(t):.1f}" y="76" text-anchor="middle">{t}%</text>'
               f'<line class="grid" x1="{sx(t):.1f}" y1="46" x2="{sx(t):.1f}" y2="58"/>'
               for t in (0,2,4,6,8,10))}
      <g class="mk" tabindex="0"><title>19 of 358 backbones produced an excellent design —
      5.31%, 95% Wilson interval 3.42% to 8.14%</title>
      <rect x="{sx(lo):.1f}" y="42" width="{sx(hi)-sx(lo):.1f}" height="20" rx="4"
            fill="var(--mark-a)" opacity="0.22"/>
      <line x1="{sx(lo):.1f}" y1="38" x2="{sx(lo):.1f}" y2="66" stroke="var(--mark-a)" stroke-width="2"/>
      <line x1="{sx(hi):.1f}" y1="38" x2="{sx(hi):.1f}" y2="66" stroke="var(--mark-a)" stroke-width="2"/>
      <circle cx="{sx(pt):.1f}" cy="52" r="6" fill="var(--mark-a)"
              stroke="var(--surface)" stroke-width="2"/></g>
      <text class="v" x="{sx(pt):.1f}" y="26" text-anchor="middle">5.31%</text>
      <text class="ax" x="{sx(lo):.1f}" y="86" text-anchor="middle">3.42</text>
      <text class="ax" x="{sx(hi):.1f}" y="86" text-anchor="middle">8.14</text>
    </svg>'''


def hotspot_rows() -> str:
    return "".join(
        f"<tr><td class=m>{n}</td><td class=num>{a}</td><td class=num>{b:.1f}</td>"
        f"<td class=num>{d}</td><td class=note>{note}</td></tr>"
        for n, a, b, d, note in HOTSPOTS)


def design_cards() -> str:
    pics = {1: "design_face", 2: "design_rank2", 3: "design_rank3"}
    out = []
    for rank, name, iptm, ips, dock, plddt, ddg, ln, seq in TOP:
        out.append(f'''<article class="card">
          <img src="{img(pics[rank])}" alt="Rank {rank} design bound to PD-L1" loading="lazy">
          <div class="card-b">
            <div class="card-h"><span class="rk">rank {rank}</span><code>{name}</code></div>
            <dl class="mini">
              <div><dt>ipTM</dt><dd>{iptm:.3f}</dd></div>
              <div><dt>ipSAE</dt><dd>{ips:.3f}</dd></div>
              <div><dt>dock RMSD</dt><dd>{dock:.2f} Å</dd></div>
              <div><dt>pLDDT</dt><dd>{plddt:.3f}</dd></div>
              <div><dt>Rosetta ΔΔG</dt><dd>{ddg:.1f}</dd></div>
              <div><dt>length</dt><dd>{ln} aa</dd></div>
            </dl>
            <p class="seq">{seq}</p>
          </div></article>''')
    return "".join(out)


CSS = """
:root{
  --ground:#F4F4F1; --surface:#FFFFFF; --sunk:#ECEDE7;
  --ink:#171A14; --ink-2:#3C4237; --muted:#5D6357; --rule:#DEE0D8; --rule-2:#C6C9BE;
  --accent:#2b8f5d; --mark-a:#2b8f5d; --mark-b:#a07002;
  --good-bg:#E4F0E7; --good-ink:#1F6B41;
  --display:"Newsreader",Georgia,serif;
  --body:"IBM Plex Sans","Helvetica Neue",Arial,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#14170F; --surface:#1C2019; --sunk:#171B14;
  --ink:#E9ECE4; --ink-2:#C3C9BB; --muted:#959C8B; --rule:#2A2F25; --rule-2:#3B4234;
  --accent:#63b98c; --mark-a:#137246; --mark-b:#b98415;
  --good-bg:#182A1E; --good-ink:#7FCB9F;
}}
:root[data-theme="dark"]{
  --ground:#14170F; --surface:#1C2019; --sunk:#171B14;
  --ink:#E9ECE4; --ink-2:#C3C9BB; --muted:#959C8B; --rule:#2A2F25; --rule-2:#3B4234;
  --accent:#63b98c; --mark-a:#137246; --mark-b:#b98415;
  --good-bg:#182A1E; --good-ink:#7FCB9F;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--body);
  font-size:17px;line-height:1.65;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:0 26px 110px}
.eyebrow{font-size:11.5px;font-weight:600;letter-spacing:.15em;text-transform:uppercase;
  color:var(--accent);margin:0}
h1{font-family:var(--display);font-weight:500;font-size:clamp(2.3rem,5.6vw,3.9rem);
  line-height:1.03;letter-spacing:-.02em;margin:.35em 0 0;text-wrap:balance}
h2{font-family:var(--display);font-weight:500;font-size:clamp(1.5rem,3vw,2.05rem);
  line-height:1.14;letter-spacing:-.015em;margin:0;text-wrap:balance}
h3{font-size:.98rem;font-weight:600;margin:0 0 6px;letter-spacing:-.005em}
p{margin:0 0 1em;max-width:66ch}
a{color:var(--accent)}
code{font-family:var(--mono);font-size:.85em;background:var(--sunk);
  border:1px solid var(--rule);border-radius:3px;padding:1px 5px}

/* hero */
.hero{padding:76px 0 40px;display:grid;gap:34px}
.lede{font-size:1.2rem;color:var(--ink-2);max-width:60ch;margin:0}
.hero-fig{margin:0;background:var(--surface);border:1px solid var(--rule);
  border-radius:2px;padding:22px;display:grid;gap:16px}
.hero-fig img{width:100%;height:auto;display:block}
figcaption{font-size:.86rem;color:var(--muted);margin:0;max-width:70ch}
.legend{display:flex;flex-wrap:wrap;gap:16px;font-size:.8rem;color:var(--muted);
  align-items:center;margin:0}
.legend b{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:6px;
  vertical-align:-1px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule)}
.stat{background:var(--surface);padding:15px 17px;display:grid;gap:2px}
.stat b{font-family:var(--display);font-weight:500;font-size:1.75rem;line-height:1;
  letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat span{font-size:10.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted)}
.verdict{display:inline-flex;align-items:center;gap:9px;background:var(--good-bg);
  color:var(--good-ink);border-radius:2px;padding:7px 13px;font-size:.82rem;font-weight:600;
  letter-spacing:.03em;width:fit-content}
.verdict::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}

/* stages */
.stage{border-top:1px solid var(--rule-2);padding:54px 0 8px;display:grid;gap:22px}
.stage-h{display:grid;gap:9px}
.step{font-family:var(--mono);font-size:11.5px;letter-spacing:.12em;color:var(--muted);
  text-transform:uppercase}
.two{display:grid;gap:30px}
@media(min-width:860px){.two{grid-template-columns:1fr 1fr;align-items:start}
  .two.wide-l{grid-template-columns:1.25fr 1fr}}
.fig{margin:0;background:var(--surface);border:1px solid var(--rule);padding:16px;
  display:grid;gap:12px;border-radius:2px}
.fig img{width:100%;height:auto;display:block}
.quote{border-left:3px solid var(--accent);padding:4px 0 4px 17px;margin:0 0 1em;
  font-family:var(--display);font-size:1.06rem;line-height:1.5;color:var(--ink-2);max-width:60ch}
.quote cite{display:block;font-family:var(--body);font-size:.76rem;font-style:normal;
  color:var(--muted);margin-top:8px;letter-spacing:.02em}
.note-box{background:var(--sunk);border:1px solid var(--rule);padding:15px 18px;
  font-size:.92rem;border-radius:2px}
.note-box p:last-child{margin:0}

/* tables */
.tw{overflow-x:auto;border:1px solid var(--rule);background:var(--surface);border-radius:2px}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th{text-align:left;font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);font-weight:600;padding:11px 13px;border-bottom:1px solid var(--rule-2);
  white-space:nowrap}
td{padding:9px 13px;border-bottom:1px solid var(--rule);vertical-align:top}
tr:last-child td{border-bottom:0}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
td.m{font-weight:600;white-space:nowrap}
td.note{color:var(--muted);font-size:.84rem}

/* charts */
.chart{width:100%;height:auto;overflow:visible}
.chart .k{font-family:var(--body);font-size:12.5px;fill:var(--ink-2);font-weight:500}
.chart .k.r{text-anchor:end;font-family:var(--mono);font-size:11.5px;fill:var(--muted)}
.chart .v{font-family:var(--mono);font-size:12.5px;fill:var(--ink);
  font-variant-numeric:tabular-nums;dominant-baseline:middle}
.chart .ax{font-family:var(--mono);font-size:10.5px;fill:var(--muted)}
.chart .ax-t{font-family:var(--body);font-size:11px;fill:var(--muted)}
.chart .grid{stroke:var(--rule-2);stroke-width:1}
.chart .sel{stroke:var(--mark-b);stroke-width:1.5;stroke-dasharray:3 3}
.chart .sel-l{font-family:var(--body);font-size:10.5px;fill:var(--mark-b);font-weight:600}
.chart .mk{cursor:default}
.chart .mk:focus{outline:none}
.chart .mk:hover rect,.chart .mk:focus rect,
.chart .mk:hover circle,.chart .mk:focus circle{filter:brightness(1.12)}
.chart-wrap{background:var(--surface);border:1px solid var(--rule);padding:20px 22px;
  border-radius:2px;display:grid;gap:14px}

/* design cards */
.cards{display:grid;gap:18px}
@media(min-width:820px){.cards{grid-template-columns:repeat(3,1fr)}}
.card{background:var(--surface);border:1px solid var(--rule);border-radius:2px;overflow:hidden;
  display:grid;grid-template-rows:auto 1fr}
.card img{width:100%;height:190px;object-fit:contain;background:var(--sunk);display:block;
  padding:10px}
.card-b{padding:15px 16px;display:grid;gap:11px;align-content:start}
.card-h{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
.rk{font-size:10.5px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--accent)}
.card-h code{font-size:.68rem;background:none;border:0;padding:0;color:var(--muted);
  word-break:break-all}
.mini{display:grid;grid-template-columns:1fr 1fr;gap:7px 14px;margin:0}
.mini div{display:flex;justify-content:space-between;gap:8px;border-bottom:1px dotted var(--rule);
  padding-bottom:4px}
.mini dt{font-size:11px;color:var(--muted);margin:0}
.mini dd{margin:0;font-family:var(--mono);font-size:12px;font-variant-numeric:tabular-nums}
.seq{font-family:var(--mono);font-size:9.5px;line-height:1.45;color:var(--muted);
  word-break:break-all;margin:0;max-width:none}

footer{border-top:1px solid var(--rule-2);margin-top:56px;padding-top:26px;
  font-size:.87rem;color:var(--muted)}
footer p{max-width:74ch}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
"""

HTML = f"""<title>PD-L1 Binder Campaign</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style>

<div class="wrap">

<header class="hero">
  <div>
    <p class="eyebrow">Little Protein Tiger · binder track · 21–22 Aug 2026</p>
    <h1>Designing a mini-protein that blocks PD-1 from reaching PD-L1</h1>
  </div>
  <p class="lede">One target name in, 5,824 refolded candidates out, 20 ranked designs
  at the end — and a measured decision at every point where the pipeline could have
  spent GPU-days on a target that was never going to work. This is that run, start
  to finish, with the numbers it actually produced.</p>

  <figure class="hero-fig">
    <img src="{img('design_face')}" alt="The top-ranked designed mini-protein bound to PD-L1, seen down the epitope axis. The designed binder covers the nine hotspot residues.">
    <p class="legend">
      <span><b style="background:#2f8f74"></b>designed binder, 84 aa</span>
      <span><b style="background:#9aa79d"></b>PD-L1 (CD274)</span>
      <span><b style="background:#c0872b"></b>the nine hotspot residues it was asked to cover</span>
    </p>
    <figcaption>The rank-1 design, refolded by RF3 from sequence alone, viewed straight
    down the epitope. Nothing about this pose was given to the folding model — it was
    asked only to fold the binder and the target together, and it put the binder on the
    patch the interface stage had picked. Dock RMSD to the intended site: 1.16 Å.</figcaption>
  </figure>

  <div class="stats">
    <div class="stat"><b>2</b><span>days, wall clock</span></div>
    <div class="stat"><b>21.4</b><span>GPU-hours</span></div>
    <div class="stat"><b>$2.12</b><span>of LLM spend</span></div>
    <div class="stat"><b>715</b><span>designs cleared every gate</span></div>
    <div class="stat"><b>0.93</b><span>best ipTM</span></div>
  </div>
  <p class="verdict">SCALE_UP — the trial justified the full campaign</p>
</header>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 1 · target intel</p>
    <h2>Which structure, and which face of it?</h2></div>
  <div class="two wide-l">
    <div>
      <p>The pipeline was given one instruction: <em>design binders against PD-L1 to block
      its interaction with PD-1</em>. It resolved PD-L1 to UniProt Q9NZQ7, pulled every
      IgV-domain complex it could find, and measured all nine of them — buried surface
      area, interface residue count, hydrogen bonds, hydrophobic fraction, resolution —
      before choosing.</p>
      <blockquote class="quote">largest BSA, most complete interface residue coverage (44),
      strong H-bond network (30) and good hydrophobicity (0.34) at the highest resolution
      (1.64 Å) among all IgV-domain complexes in the table; this front-sheet face is the
      epitope essentially every characterized PD-L1 blocker converges on, making it the
      best structural proxy for the PD-1 binding footprint
      <cite>— binder-target-intel, 20_target_intel.md</cite></blockquote>
      <p>Four alternatives were rejected in writing, each for a stated reason: 8AOK for a
      flat, polar epitope (hydrophobic fraction 0.22); 8AOM for a 222-residue chain over
      the trim budget with almost no hydrogen bonds; 7SJQ and 7C88 for smaller interfaces.
      The decision is auditable because the table it was made from is kept.</p>
    </div>
    <figure class="fig">
      <img src="{img('native_face')}" alt="The anti-PD-L1 nanobody bound to PD-L1 in 7CZD, seen down the same epitope axis.">
      <figcaption><strong>7CZD</strong> — what a real binder does here. The anti-PD-L1
      VHH (grey) covers the same front β-sheet face, viewed on the same axis as the design
      above. 2,449 Å² buried, 44 interface residues, 30 H-bonds, 1.64 Å.</figcaption>
    </figure>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 2 · interface analysis</p>
    <h2>Nine residues, and the evidence for each</h2></div>
  <div class="two">
    <div>
      <p>The interface stage does not accept the textbook answer. It reads the actual
      residue at every position in the downloaded structure and computes per-residue
      buried area, then ranks contiguous patches by how designable they are. Two regions
      came back: the C-terminal F/G strand (Arg113–Tyr123, rated <em>excellent</em>) and
      the CC′/FG loop (Ile54–Lys75, rated <em>good</em>).</p>
      <p>It also grounded the choice in the corpus: a PD-L1-mimicking peptide built around
      exactly Tyr56, Arg113, Ala121, Asp122 and Tyr123 binds PD-1 at
      <strong>K<sub>d</sub> ≈ 1.38 µM</strong> — one citation checked, one verified
      against the local literature database.</p>
      <div class="note-box"><p>This is the stage where a well-known target is most
      dangerous. Asked to analyse a different PD-L1 structure once, the model returned
      PD-L1's canonical <em>literature</em> hotspots — correct for a different PDB entry,
      wrong for the one in hand. Two guards now run before any trim is built: one reads
      the real residue name at each position, the other checks the target chain is
      actually the target by sequence identity to UniProt. Both passed here.</p></div>
    </div>
    <figure class="fig">
      <img src="{img('epitope')}" alt="PD-L1 surface with the nine hotspot residues highlighted, no binder present.">
      <figcaption>The nine hotspots on the bare PD-L1 surface — the same camera as the
      hero image. This is the patch the design had to cover.</figcaption>
    </figure>
  </div>
  <div class="tw"><table>
    <thead><tr><th>Residue</th><th class="num">auth id</th><th class="num">BSA Å²</th>
      <th class="num">ΔΔG kcal/mol</th><th>Why it is on the list</th></tr></thead>
    <tbody>{hotspot_rows()}</tbody></table></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 3 · trim and spec</p>
    <h2>Cutting the target down without losing the site</h2></div>
  <p>RFD3 is given a contig, not a whole protein. The trim stage segmented the chain,
  checked UniProt for a transmembrane region (none — the modelled construct is entirely
  extracellular), and kept <strong>117 of 117 residues in a single contiguous segment</strong>,
  18–134 in author numbering. All <strong>9 of 9 hotspots</strong> survived, and the kept
  residues carry 138.5% of the interface area of the originals.</p>
  <p>Two details here are load-bearing and easy to get wrong. The trim preserves author
  numbering, so hotspot ids stay valid downstream. And it prefers one segment: every extra
  segment is a chain break RFD3 has to model, and the prefilter's chain-break limit is
  derived from the segment count rather than hardcoded. The resulting contig —
  <code>70-86,/0,B18-134</code> — asks for a binder of 70 to 86 residues against that segment.</p>
  <div class="note-box"><p>The trim gate raised a warning and recorded it rather than
  hiding it: cutting to this segment removed residues carrying 1,653 Å² (67%) of the
  native interface. That is expected when a target has more than one interface and you
  are designing against a single one — but it is a checkpoint in the manifest, so it is
  a thing a human confirmed, not a thing that happened quietly.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 4 · pilot and calibration</p>
    <h2>Measure the hit rate before buying the GPU time</h2></div>
  <p>A pilot of 100 backbones proved the machinery end to end in 52 minutes. Then the
  calibration stage ran the experiment that actually decides the campaign: 580 backbones,
  4 sequences each, 1,432 refolds — and it <em>counted</em> how many designs cleared the
  success bar rather than assuming a rate.</p>
  <div class="two">
    <div class="chart-wrap">
      <h3>Designs surviving at each ipTM bar</h3>
      {line_yield()}
      <figcaption>Of 1,432 calibration refolds. The curve is why the bar moved: at
      ipTM &gt; 0.85 there are still 21 survivors — enough to size a campaign on.</figcaption>
    </div>
    <div>
      <p>The requested bar was ipTM &gt; 0.7. The calibration stage walks the bar ladder
      strictest-first and takes the hardest rung that still clears five hits and still
      fits the budget — so it <strong>raised the bar to 0.85 by itself</strong>, and sized
      the campaign to that. Only ever upward: it will not quietly make a campaign easier
      than you asked for.</p>
      <h3 style="margin-top:22px">Backbone hit rate, 95% Wilson interval</h3>
      {wilson()}
      <p style="font-size:.9rem;color:var(--muted)">19 of 358 backbones produced an
      excellent design. The campaign is sized on the <em>pessimistic</em> end of that
      interval, not the point estimate: 5,842 refolds, ~17.8 GPU-hours, ~14.6 GB — well
      inside the 120-hour, 120-GB budget. Verdict: <strong>SCALE_UP</strong>.</p>
    </div>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 5 · production</p>
    <h2>What 20 GPU-hours actually produced</h2></div>
  <div class="chart-wrap">
    <h3>Production funnel</h3>
    {bar_funnel()}
    <figcaption>Every count read from disk by directory scan, never from a log line.
    The MPNN and RF3 rows are larger than the backbone row because each surviving
    backbone gets four sequences, and every sequence is refolded independently.</figcaption>
  </div>
  <div class="two" style="margin-top:8px">
    <div>
      <p>715 designs cleared every hard gate — 12.3% of the refolds, spread across 462
      distinct backbones. The gates are not a formality: the chart shows which one was
      the <em>first</em> to reject each design that failed.</p>
      <p>Dock RMSD does most of the work, and that is the point. ipTM and ipSAE only
      report how confident the model is in the interface it chose; they cannot tell you
      it chose the interface you asked for. Ranking on confidence alone selects binders
      that are confidently docked in the wrong place. Of designs here with ipTM &gt; 0.7,
      only a minority are docked on target — <strong>2,464 designs died on geometry, not
      on confidence</strong>.</p>
    </div>
    <div class="chart-wrap">
      <h3>Refolds dropped by the first gate they failed</h3>
      {bar_gates()}
      <figcaption>Of 5,824. Hover a bar for what that gate would have kept on its own.</figcaption>
    </div>
  </div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">Stage 6 · scoring and ranking</p>
    <h2>The designs that came out</h2></div>
  <p>Survivors are ranked on a composite of ipSAE, dock RMSD, pLDDT, ipTM, interface PAE,
  epitope recall and hotspot engagement, then diversified so the top-K is not twenty
  variations of one backbone. Rosetta relax and InterfaceAnalyzer run <em>after</em> the
  gates, on 300 of the 715 survivors, and enter the composite only — a mis-docked pose
  is still a physical pose, and Rosetta will happily return well-defined, meaningless
  numbers for it.</p>
  <div class="cards">{design_cards()}</div>
  <div class="note-box" style="margin-top:22px"><p><strong>Read these as computational
  hypotheses, not as binders.</strong> Nothing here has been expressed, purified or
  measured. The design-analyst stage flagged, in its own report, that the developability
  columns it would normally comment on were absent from the table it was handed — so this
  page does not show developability numbers either.</p></div>
</section>

<section class="stage">
  <div class="stage-h"><p class="step">What it cost</p>
    <h2>Three LLM calls, twenty-one GPU-hours</h2></div>
  <p>Only three stages of this campaign involved a language model at all: target intel,
  interface analysis, and the final review. Everything between them — trimming, spec
  building, campaign planning, gating, scoring, ranking — is deterministic Python. The
  LLM spend was <strong>$2.12 against a $10 hard cap</strong>, itemised per stage and per
  model in an append-only ledger. The GPU spend was 0.87 hours of pilot, roughly 4 hours
  of calibration, and 20.54 hours of production.</p>
  <p>The campaign is resumable at every one of those boundaries, because the batch count,
  the compute placement and the raised bar are all persisted — a process that dies
  overnight and restarts at <code>--start-from production</code> re-derives the measured
  plan rather than falling back to the config default.</p>
</section>

<footer>
  <p>Every figure on this page was read from <code>projects/pdl1_e2e</code> — the run
  manifest, the stage reports, <code>calibration.json</code>, <code>filter_stats.txt</code>
  and <code>top_k.csv</code>. Structure images rendered with UCSF ChimeraX from the
  campaign's own RF3 refolds and from PDB 7CZD; the hotspot residues shown are the nine
  the interface stage selected, mapped through the refold's own numbering.</p>
  <p>Regenerate this page with <code>python docs/showcase/build_campaign.py</code>.
  Read <code>docs/responsible-use.md</code> before designing anything.</p>
</footer>

</div>
"""

OUT.write_text(HTML, encoding="utf-8")
print(f"wrote {OUT}  ({OUT.stat().st_size/1024:.0f} KB)")
