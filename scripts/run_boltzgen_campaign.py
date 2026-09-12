"""Run one BoltzGen campaign directly, with no LLM stage and no pipeline.

`--workflow ppi --design-engine boltzgen` reaches BoltzGen through three LLM
stages and reads its design counts out of `config.yaml`, which makes it the
wrong instrument for a calibration sweep: it costs API spend to re-derive a
target that is already known, and the only way to change `num_designs` is to
edit the shared config. This drives `src.design_runner` directly against an
already-built YAML, so the sampling size is an argument.

Scoring is deliberately left to BoltzGen itself — `all_designs_metrics.csv` is
what the pipeline reports today and what its users read. Re-scoring the result
with the binder track's own metrics is a separate, later question, answered by
`scripts/measure_boltzgen_binder_gates.py` over the same output directory.

`--reuse` is on (it is `run_design`'s default), so a small probe run followed
by a large one EXTENDS the first rather than repeating it: a 24-design timing
probe is the first 24 of the eventual 1000, not 24 designs thrown away. That
is also why a run can be enlarged after the fact instead of being sized by
guesswork.

One GPU means one campaign at a time. This script makes no attempt to
serialise itself against another process — check `nvidia-smi` first.

Usage:

    python scripts/run_boltzgen_campaign.py \
        --yaml /path/RAMP1_mini_boltzgen.yaml \
        --out outputs/bz_calib_ramp1_mini \
        --protocol protein-anything --num-designs 24 --budget 8
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

#: Subdirectory of `intermediate_designs_inverse_folded` holding the
#: complex refolds — BoltzGen's final per-design structure.
_REFOLD_SUBDIR = "refold_cif"
#: BoltzGen's own stage banner, e.g. "[Step 3/5] folding".
_STEP_RE = re.compile(r"\[Step \d+/\d+\] [a-z_]+")


def _count_cifs(d: Path) -> int:
    """`.cif` files directly in `d`, or 0 if it does not exist.

    `os.scandir`, never a glob or `ls`: these directories reach tens of
    thousands of entries on a real campaign, and the pipeline has a documented
    history of `ls | wc -l` silently reporting 0 and reading as "this stage
    produced nothing".
    """
    if not d.is_dir():
        return 0
    try:
        with os.scandir(d) as it:
            return sum(1 for e in it if e.is_file() and e.name.endswith(".cif"))
    except OSError:
        return 0


def _design_count(out_dir: Path) -> int:
    """Completed designs = refolds, the campaign's final per-design artifact."""
    return _count_cifs(out_dir / "intermediate_designs_inverse_folded"
                       / _REFOLD_SUBDIR)


def _progress(out_dir: Path) -> dict:
    """Per-stage counts plus the step BoltzGen last reported.

    Refolds alone are the wrong progress signal, and misleadingly so: they are
    written by the `folding` step, which is third of five (peptide) or six
    (protein), so a campaign shows **zero refolds for the first stretch of its
    life** while `design` and `inverse_folding` work through every design. On a
    1000-design run that is hours of apparent standstill — the same "this stage
    produced nothing" misreading the foundry driver's counters were written to
    avoid. Report what each stage has actually produced instead.
    """
    inv = out_dir / "intermediate_designs_inverse_folded"
    step = ""
    log = out_dir / "boltzgen.log"
    if log.is_file():
        try:
            # tqdm writes \r-separated frames, so split on both.
            text = log.read_text(errors="replace").replace("\r", "\n")
            hits = _STEP_RE.findall(text)
            step = hits[-1] if hits else ""
        except OSError:
            pass
    return {
        "step": step,
        "designs": _count_cifs(out_dir / "intermediate_designs"),
        "inverse_folded": _count_cifs(inv),
        "refolds": _count_cifs(inv / _REFOLD_SUBDIR),
    }


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--yaml", type=Path, required=True,
                    help="design YAML (see scripts/build_boltzgen_spec.py)")
    ap.add_argument("--out", type=Path, required=True,
                    help="BoltzGen output directory; reused across calls")
    ap.add_argument("--protocol", required=True,
                    choices=("protein-anything", "peptide-anything"))
    ap.add_argument("--num-designs", type=int, required=True)
    ap.add_argument("--budget", type=int, required=True,
                    help="BoltzGen's own final-set size. Does not affect "
                         "all_designs_metrics.csv, which carries every design.")
    ap.add_argument("--timeout-hours", type=float, default=None,
                    help="per-call cap; defaults to "
                         "design.workstation.timeout_hours")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    import yaml as _yaml

    from src.design_runner import run_design
    from src.env_config import load_env, resolve_env_path

    load_env()
    cfg = _yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    ws = ((cfg.get("design") or {}).get("workstation")) or {}
    exe = (resolve_env_path("LPT_BOLTZGEN_EXECUTABLE",
                            ws.get("boltzgen_executable")) or "boltzgen")
    timeout_h = float(args.timeout_hours
                      if args.timeout_hours is not None
                      else ws.get("timeout_hours", 24.0))

    args.out.mkdir(parents=True, exist_ok=True)
    before = _design_count(args.out)
    prog_before = _progress(args.out)
    t0 = time.time()
    logger.info(f"BoltzGen: {args.num_designs} designs, protocol "
                f"{args.protocol}, budget {args.budget}, {before} already on "
                f"disk -> {args.out}")

    res = run_design(
        args.yaml, args.out,
        protocol=args.protocol,
        num_designs=args.num_designs,
        budget=args.budget,
        executable=exe,
        cuda_device=ws.get("cuda_device", 0),
        timeout_h=timeout_h,
    )

    elapsed = time.time() - t0
    after = _design_count(args.out)
    made = max(after - before, 0)
    stamp = {
        "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
        "yaml": str(args.yaml),
        "protocol": args.protocol,
        "requested": args.num_designs,
        "budget": args.budget,
        "designs_before": before,
        "designs_after": after,
        "progress_before": prog_before,
        "progress_after": _progress(args.out),
        "designs_added": made,
        "elapsed_s": round(elapsed, 1),
        "sec_per_design": round(elapsed / made, 2) if made else None,
        "returncode": getattr(res, "returncode", None),
        "log": str(getattr(res, "log_path", "")),
    }
    # Append-only: one object per call, so a probe and the run that extended it
    # both survive and the rate can be read back per slice.
    ledger = args.out / "campaign_timing.jsonl"
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(stamp) + "\n")

    logger.info(f"added {made} designs in {elapsed / 60:.1f} min"
                + (f" ({stamp['sec_per_design']} s/design)" if made else ""))
    logger.info(f"timing -> {ledger}")
    print(json.dumps(stamp, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
