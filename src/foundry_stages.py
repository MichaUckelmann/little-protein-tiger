#!/usr/bin/env python3
"""
The per-stage workers the campaign driver shells out to.

`run_campaign.sh` owns orchestration only — retry loops, disk counting, the
free-space abort. Everything with a decision in it lives here, in Python that
LPT tests. Each subcommand is idempotent and safe to re-run.

Ported from the reference campaign's `filter_designs.py` / `run_mpnn.py` /
`run_rf3.py`, whose hard-won details are preserved and commented at the point
they matter.

    foundry_stages.py prefilter <rfd3_dir> <out_dir> [--max-chainbreaks N] ...
    foundry_stages.py mpnn      <design_dir> <out_dir> --checkpoint C ...
    foundry_stages.py rf3       <mpnn_dir> <out_dir> --checkpoint C ...
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger  # noqa: E402

from src.foundry_spec import build_mpnn_configs, strip_design_suffixes  # noqa: E402
from src.foundry_runner import count_mpnn, count_rf3, prefilter_designs  # noqa: E402


# Where the foundry checkpoint registry keeps its files.
DEFAULT_CKPT_DIR = Path.home() / "pip_rcfoundry_ckpt"


def resolve_checkpoint(value: str, ckpt_dir: Path = DEFAULT_CKPT_DIR) -> str:
    """
    Turn a checkpoint-registry alias into a real path, for MPNN.

    RFD3 and RF3 resolve aliases themselves through the registry, so
    `ckpt_path=rfd3` works. MPNN does not: its config takes a literal path and it
    fails with `checkpoint_path does not exist: solublempnn`. Passing an already
    valid path through unchanged keeps explicit configuration working.
    """
    if not value:
        return value
    if Path(value).exists():
        return str(Path(value).resolve())
    matches = sorted(Path(ckpt_dir).glob(f"{value}*")) if ckpt_dir.is_dir() else []
    if not matches:
        logger.warning(
            f"checkpoint alias {value!r} matched nothing in {ckpt_dir}; passing "
            f"it through unchanged")
        return value
    if len(matches) > 1:
        logger.info(f"alias {value!r} matched {len(matches)} files; using "
                    f"{matches[0].name}")
    return str(matches[0].resolve())


def _run(argv: list[str], cwd: Path) -> int:
    logger.info(f"$ (cd {cwd} && {' '.join(argv)})")
    proc = subprocess.run(argv, cwd=str(cwd))
    return proc.returncode


# ----------------------------------------------------------------------

def cmd_prefilter(args) -> int:
    kept, total = prefilter_designs(
        args.rfd3_dir, args.out_dir,
        max_chainbreaks=args.max_chainbreaks,
        max_sidechain_clashes=args.max_sidechain_clashes,
        max_backbone_clashes=args.max_backbone_clashes,
        min_non_loop=args.min_non_loop,
        report_path=args.report,
    )
    if total and kept == 0:
        # Falling back to the unfiltered set is better than stalling the whole
        # campaign on a threshold mistake; the report says what happened.
        logger.warning("prefilter kept nothing — linking the unfiltered designs")
        out = Path(args.out_dir)
        with os.scandir(args.rfd3_dir) as it:
            for e in it:
                if e.is_file() and e.name.endswith(".cif.gz"):
                    link = out / e.name
                    if not link.exists():
                        link.symlink_to(Path(e.path).resolve())
    return 0


def cmd_mpnn(args) -> int:
    with os.scandir(args.design_dir) as it:
        designs = sorted(Path(e.path) for e in it if e.name.endswith(".cif.gz"))
    if args.skip_existing:
        out = Path(args.out_dir)
        # MPNN writes `<name>.fa` per input and flushes a config all-or-nothing,
        # so a crashed chunk marks none of its inputs done and resumes cleanly.
        before = len(designs)
        designs = [d for d in designs
                   if not (out / f"{strip_design_suffixes(d.name)}.fa").exists()]
        logger.info(f"skip-existing: {before - len(designs):,} already done")
    if not designs:
        logger.info("nothing to do")
        return 0

    checkpoint = resolve_checkpoint(args.checkpoint, Path(args.ckpt_dir))
    logger.info(f"MPNN checkpoint: {checkpoint}")
    configs = build_mpnn_configs(
        designs, args.out_dir, Path(args.out_dir) / "configs",
        checkpoint=checkpoint, n_seq=args.n_seq,
        temperature=args.temperature,
        omit=[a for a in args.omit.split(",") if a.strip()],
        chunk_size=args.chunk_size, seed=args.seed,
    )
    launcher = args.launcher.split() if args.launcher else ["uv", "run"]
    bin_path = args.mpnn_bin
    if not Path(bin_path).is_absolute():
        bin_path = str(Path(args.foundry) / bin_path)
    rc = 0
    for i, cfg in enumerate(configs, 1):
        logger.info(f"MPNN chunk {i}/{len(configs)}")
        rc = _run([*launcher, bin_path, "--config_json", str(cfg)],
                  Path(args.foundry))
        if rc != 0:
            logger.error(f"MPNN chunk {i} exited {rc}")
            break
    logger.info(f"MPNN produced {count_mpnn(Path(args.out_dir)):,} structures")
    return rc


def cmd_rf3(args) -> int:
    launcher = args.launcher.split() if args.launcher else ["uv", "run"]
    bin_path = args.rf3_bin
    if not Path(bin_path).is_absolute():
        bin_path = str(Path(args.foundry) / bin_path)

    inputs = Path(args.mpnn_dir)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    staging = inputs
    if args.skip_existing:
        # RF3's own skip_existing keys on `_metrics.csv`, which it writes only
        # when early stopping triggers — so it silently refolds finished designs.
        # Stage the outstanding inputs as symlinks and point RF3 at those.
        done = set()
        with os.scandir(out) as it:
            for e in it:
                if e.is_dir() and (Path(e.path) /
                                   f"{e.name}_summary_confidences.json").exists():
                    done.add(e.name)
        staging = out.parent / ".rf3_staging"
        if staging.exists():
            for f in staging.iterdir():
                if f.is_symlink():
                    f.unlink()
        staging.mkdir(parents=True, exist_ok=True)
        n = 0
        with os.scandir(inputs) as it:
            for e in it:
                if not (e.is_file() and "_b" in e.name and e.name.endswith(".cif")):
                    continue
                if e.name[: -len(".cif")] in done:
                    continue
                link = staging / e.name
                if not link.exists():
                    link.symlink_to(Path(e.path).resolve())
                n += 1
        logger.info(f"RF3: {len(done):,} already refolded, {n:,} to go")
        if n == 0:
            return 0

    argv = [*launcher, bin_path, "fold",
            f"inputs={staging}", f"out_dir={out}",
            f"ckpt_path={args.checkpoint}",
            f"template_selection={'B' if args.template == 'target' else args.template}",
            f"diffusion_batch_size={args.diffusion_batch_size}",
            f"seed={args.seed}"]
    rc = _run(argv, Path(args.foundry))
    logger.info(f"RF3 has {count_rf3(out):,} completed refolds")
    return rc


# ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prefilter")
    p.add_argument("rfd3_dir", type=Path)
    p.add_argument("out_dir", type=Path)
    p.add_argument("--max-chainbreaks", type=int, default=1,
                   help="Derive from the target's segment count; a two-segment "
                        "trim scores 2 and the default of 1 rejects everything.")
    p.add_argument("--max-sidechain-clashes", type=int, default=0)
    p.add_argument("--max-backbone-clashes", type=int, default=0)
    p.add_argument("--min-non-loop", type=float, default=0.6)
    p.add_argument("--report", type=Path, default=None)
    p.set_defaults(func=cmd_prefilter)

    m = sub.add_parser("mpnn")
    m.add_argument("design_dir", type=Path)
    m.add_argument("out_dir", type=Path)
    m.add_argument("--checkpoint", default="solublempnn")
    m.add_argument("--n-seq", type=int, default=4)
    m.add_argument("--temperature", type=float, default=0.1)
    m.add_argument("--omit", default="CYS")
    m.add_argument("--chunk-size", type=int, default=250)
    m.add_argument("--seed", type=int, default=0)
    m.add_argument("--foundry", default="/home/m.uckelmann_cbs-niob.local/code/foundry")
    m.add_argument("--mpnn-bin", default=".venv-blackwell/bin/mpnn")
    m.add_argument("--launcher", default=None)
    m.add_argument("--skip-existing", action="store_true")
    m.add_argument("--ckpt-dir", default=str(DEFAULT_CKPT_DIR),
                   help="Where checkpoint aliases are resolved from.")
    m.set_defaults(func=cmd_mpnn)

    r = sub.add_parser("rf3")
    r.add_argument("mpnn_dir", type=Path)
    r.add_argument("out_dir", type=Path)
    r.add_argument("--checkpoint", default="rf3")
    r.add_argument("--template", default="target")
    r.add_argument("--diffusion-batch-size", type=int, default=1)
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--foundry", default="/home/m.uckelmann_cbs-niob.local/code/foundry")
    r.add_argument("--rf3-bin", default=".venv-blackwell/bin/rf3")
    r.add_argument("--launcher", default=None)
    r.add_argument("--skip-existing", action="store_true")
    r.set_defaults(func=cmd_rf3)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
