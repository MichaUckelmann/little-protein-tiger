#!/usr/bin/env python3
"""
One-time setup for `--compute modal`, and the preflight that checks it.

    python scripts/modal_setup.py            # weights + deploy, then verify
    python scripts/modal_setup.py --check    # verify only, changes nothing
    python scripts/modal_setup.py --estimate 8.5   # what 8.5 local GPU-h costs

Three things have to be true before a Modal campaign can run, and exactly one
of them is non-obvious:

1. `modal setup` has been run (an auth token exists).
2. `src/modal_app.py` is deployed — one function per GPU, because a Modal
   Function's resources are fixed at deploy time in this client.
3. The checkpoint volume holds rfd3, rf3 AND solubleMPNN.

(3) is the non-obvious one. foundry's own `foundry install base-models` fetches
rfd3, rfd3na, rf3, proteinmpnn and ligandmpnn — but NOT solubleMPNN, which is
what LPT designs with. Nothing downstream would announce the substitution: MPNN
would happily run with proteinMPNN weights and produce a different (non-soluble)
sequence distribution, and the campaign would look completely normal. So this
script uploads solubleMPNN from the local checkpoint dir and `--check` fails
loudly if it is absent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.env_config import resolve_env_path  # noqa: E402
from src.modal_runner import (  # noqa: E402
    CKPT_VOLUME, ModalConfig, PRICES_REVIEWED, estimate_usd,
)

APP_FILE = "src/modal_app.py"


def _local_ckpt_dir(cfg: dict) -> Path:
    f = ((cfg.get("design") or {}).get("foundry") or {})
    return Path(
        resolve_env_path("LPT_FOUNDRY_CKPT_DIR", f.get("ckpt_dir"))
        or Path.home() / "pip_rcfoundry_ckpt")


def _load_config() -> dict:
    import yaml

    path = Path(__file__).resolve().parents[1] / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def cmd_check(mcfg: ModalConfig) -> int:
    import modal

    ok = True
    try:
        fn = modal.Function.from_name(mcfg.app_name, "checkpoint_status")
    except Exception as exc:
        print(f"FAIL  app {mcfg.app_name!r} is not deployed ({exc})")
        print(f"      fix:  modal deploy {APP_FILE}")
        return 1
    print(f"ok    app {mcfg.app_name!r} is deployed")

    try:
        modal.Function.from_name(mcfg.app_name, mcfg.function_name)
        print(f"ok    gpu {mcfg.gpu} -> {mcfg.function_name}")
    except Exception:
        print(f"FAIL  design.modal.gpu={mcfg.gpu!r} has no deployed function "
              f"({mcfg.function_name})")
        print(f"      fix:  modal deploy {APP_FILE}")
        ok = False

    status = fn.remote()
    for name in ("rfd3", "rf3", "mpnn"):
        path, gb = status["paths"].get(name), status["gb"].get(name)
        if path:
            print(f"ok    {name:<5} {gb:>6.2f} GB  {path}")
        else:
            ok = False
            extra = ""
            if name == "mpnn":
                extra = ("  <- solubleMPNN; `foundry install base-models` does "
                         "NOT ship it, run this script without --check")
            print(f"FAIL  {name:<5} missing on volume {CKPT_VOLUME!r}{extra}")
    return 0 if ok else 1


def cmd_setup(mcfg: ModalConfig, cfg: dict) -> int:
    import modal

    ckpt_dir = _local_ckpt_dir(cfg)
    soluble = sorted(ckpt_dir.glob("solublempnn*.pt"))
    if not soluble:
        print(f"FAIL  no solublempnn*.pt in {ckpt_dir}. Set LPT_FOUNDRY_CKPT_DIR "
              f"or design.foundry.ckpt_dir to the directory holding it.")
        return 1

    vol = modal.Volume.from_name(CKPT_VOLUME, create_if_missing=True)
    existing = {e.path.lstrip("/") for e in vol.listdir("/", recursive=True)}

    # The big three come from foundry's own installer, run ON Modal — 5.4 GB
    # downloaded inside the datacentre rather than pushed up this machine's
    # uplink through a TLS-inspecting firewall.
    need_base = not any(p.startswith("rfd3") for p in existing) or \
        not any(p.startswith("rf3") for p in existing)
    if need_base:
        print("… installing foundry base models onto the volume (~5.4 GB, "
              "runs remotely; several minutes)")
        installer = modal.Function.from_name(mcfg.app_name, "install_base_models")
        print(installer.remote())
    else:
        print("ok    base models already present")

    target = f"/{soluble[0].name}"
    if target.lstrip("/") in existing:
        print(f"ok    solubleMPNN already present ({target})")
    else:
        print(f"… uploading {soluble[0].name} ({soluble[0].stat().st_size / 1e6:.1f} MB)")
        with vol.batch_upload() as batch:
            batch.put_file(str(soluble[0]), target)
        print("ok    solubleMPNN uploaded")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="verify only; upload and deploy nothing")
    ap.add_argument("--estimate", type=float, metavar="LOCAL_GPU_HOURS",
                    help="price N local GPU-hours on the configured GPU and exit")
    args = ap.parse_args()

    cfg = _load_config()
    mcfg = ModalConfig.from_cfg(cfg)

    if args.estimate is not None:
        usd = estimate_usd(args.estimate, mcfg)
        print(f"{args.estimate:,.1f} local GPU-h x {mcfg.speed_factor:.2f} on "
              f"{mcfg.gpu} @ ${mcfg.usd_per_hour:.2f}/h = ~${usd:,.2f}")
        print(f"cap design.modal.max_usd = ${mcfg.max_usd:,.2f} -> "
              f"{'REFUSED' if usd > mcfg.max_usd else 'allowed'}")
        print(f"(prices transcribed {PRICES_REVIEWED}; Modal's invoice is "
              f"authoritative)")
        return 0

    if args.check:
        return cmd_check(mcfg)

    rc = cmd_setup(mcfg, cfg)
    if rc:
        return rc
    print()
    print(f"Now deploy (or redeploy) the app:\n    modal deploy {APP_FILE}")
    print("then verify:\n    python scripts/modal_setup.py --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
