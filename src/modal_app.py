"""
Remote execution of one foundry campaign stage on Modal.

This is the REMOTE half of `--compute modal`; `src/modal_runner.py` is the
local half. Deploy it once per checkout::

    modal deploy src/modal_app.py

after which `modal_runner` spawns `run_campaign` by name and polls a small
`progress.json` the run writes into the campaign volume.

Three facts decided the shape of this module, all measured on a real A10 run
against `projects/smoke_foundry`'s TEAD1 spec (208-residue target, 286 tokens),
not read out of anyone's docs:

- **The published image needs a C compiler that it does not ship.** triton
  JIT-compiles a CUDA driver shim on the first RF3 forward pass, so
  `rosettacommons/foundry:0.2.0-slim` dies with `Failed to find C compiler` —
  *after* RFD3 and MPNN have already run and spent their GPU minutes. Hence
  `apt_install("build-essential")`.
- **`foundry install base-models` does not include solubleMPNN.** It fetches
  rfd3/rfd3na/rf3/proteinmpnn/ligandmpnn only. LPT designs with solubleMPNN, so
  that one checkpoint (6.7 MB) is uploaded to the volume separately —
  `modal_runner.ensure_checkpoints` refuses to launch without it rather than
  letting MPNN silently fall back to a different model.
- **The three binaries are invoked directly, not through `uv run`.** Locally
  LPT does `cd $FOUNDRY && uv run .venv-blackwell/bin/rfd3` because that venv
  is hand-built for this workstation's sm_120 card. In the container the stock
  venv's binaries carry their own shebang, so the `uv run` wrapper — and the
  whole `.venv-blackwell` accident — simply does not travel.

What DOES travel unchanged is the decision logic: `prefilter_designs` and
`build_mpnn_configs` are imported from LPT itself, so a remote campaign applies
byte-identical prefilter thresholds and MPNN configs to a local one. Only the
three GPU invocations are re-expressed here.
"""

from __future__ import annotations

import pathlib

import modal

APP_NAME = "lpt-foundry"
CKPT_VOLUME = "lpt-foundry-ckpt"
CAMPAIGN_VOLUME = "lpt-campaign"

_REPO = pathlib.Path(__file__).resolve().parents[1]

# The binaries live in the image's own venv; `add_python` gives Modal a
# separate interpreter, which is the same split LPT uses locally (LPT's venv
# python drives, foundry's venv holds the models).
BIN = "/app/foundry/.venv/bin"

image = (
    modal.Image.from_registry("rosettacommons/foundry:0.2.0-slim", add_python="3.12")
    # triton needs this at RUN time, not build time — see module docstring.
    .apt_install("build-essential")
    # The two packages the imported LPT code needs, and NOTHING else.
    #
    # Two traps here, both paid for once:
    #
    # 1. The base image sets `ENV PATH=/app/foundry/.venv/bin:$PATH`, so `python`
    #    — in Modal's builder AND in its function runtime — is foundry's own
    #    uv-created venv, not the `add_python` interpreter at /usr/local/bin.
    #    A plain `.pip_install(...)` therefore dies at build time with "No
    #    module named pip" (a uv venv ships none), and installing into
    #    /usr/local/bin/python instead builds cleanly and then fails at RUN
    #    time with `ModuleNotFoundError: loguru` — the packages land in an
    #    interpreter nothing executes. The venv is the target; `ensurepip`
    #    gives it a pip.
    # 2. That venv is also where torch and the models live, so installing into
    #    it is installing into the model environment. Hence the deliberately
    #    minimal list: everything reached remotely (`foundry_runner`,
    #    `foundry_spec`, `job_registry`, `env_config`) needs only these two.
    #    numpy/scipy/biotite/gemmi are NOT installed — pulling them in could
    #    let pip resolve a different numpy under torch and silently change what
    #    the GPU computes, to buy nothing this path uses.
    .run_commands(
        "/app/foundry/.venv/bin/python -m ensurepip --default-pip",
        "/app/foundry/.venv/bin/python -m pip install --no-cache-dir "
        "loguru python-dotenv",
    )
    .add_local_dir(_REPO / "src", "/lpt/src", copy=True)
    .env({"PYTHONPATH": "/lpt", "CC": "gcc"})
)

app = modal.App(APP_NAME, image=image)

ckpt_vol = modal.Volume.from_name(CKPT_VOLUME, create_if_missing=True)
campaign_vol = modal.Volume.from_name(CAMPAIGN_VOLUME, create_if_missing=True)

VOLUMES = {"/ckpt": ckpt_vol, "/campaign": campaign_vol}


def _checkpoints() -> dict[str, str]:
    """Resolve the three checkpoints by glob, newest-name-wins.

    MPNN needs a literal path (its config takes no alias), and RFD3/RF3 are
    given literal paths too rather than the `rfd3`/`rf3` registry aliases: the
    registry points at whatever `--checkpoint-dir` the image was built with,
    and ours live on a volume mounted at /ckpt instead.
    """
    import glob

    def one(pattern: str) -> str | None:
        hits = sorted(glob.glob(f"/ckpt/**/{pattern}", recursive=True))
        return hits[0] if hits else None

    return {
        "rfd3": one("rfd3_latest.ckpt") or one("rfd3*.ckpt"),
        "rf3": one("rf3*.ckpt"),
        # solubleMPNN ONLY. Falling back to proteinmpnn here would silently
        # design a different (non-soluble) sequence distribution and nothing
        # downstream would say so.
        "mpnn": one("solublempnn*.pt"),
    }


@app.function(volumes=VOLUMES, timeout=600)
def checkpoint_status() -> dict:
    """What is on the checkpoint volume — used by the local preflight."""
    import os

    ck = _checkpoints()
    sizes = {}
    for name, path in ck.items():
        sizes[name] = round(os.path.getsize(path) / 1e9, 3) if path else None
    return {"paths": ck, "gb": sizes}


@app.function(volumes=VOLUMES, timeout=3600, cpu=4.0)
def install_base_models() -> dict:
    """
    Populate the checkpoint volume using foundry's own installer.

    Deliberately **no GPU**: this is 5.4 GB of downloading and nothing else, so
    renting an accelerator for it would be paying GPU rates to wait on a
    network. It also runs inside the datacentre rather than pushing weights up
    a workstation uplink.

    It does NOT fetch solubleMPNN — see `scripts/modal_setup.py`, which
    uploads that one separately and refuses to proceed without it.
    """
    import os
    import subprocess

    r = subprocess.run(
        [f"{BIN}/foundry", "install", "base-models", "--checkpoint-dir", "/ckpt"],
        capture_output=True, text=True, timeout=3300)
    listing = {}
    for root, _dirs, files in os.walk("/ckpt"):
        for f in files:
            p = os.path.join(root, f)
            listing[os.path.relpath(p, "/ckpt")] = round(
                os.path.getsize(p) / 1e9, 3)
    ckpt_vol.commit()
    return {"returncode": r.returncode, "files": dict(sorted(listing.items())),
            "stderr_tail": (r.stderr or "")[-800:]}


@app.function(volumes=VOLUMES, timeout=600)
def campaign_counts(campaign_id: str) -> dict:
    """Disk counts for one campaign, in LPT's own vocabulary.

    A fallback for `progress.json` — the run writes that file itself, and this
    exists for the case where the run died before writing one.
    """
    import sys

    sys.path.insert(0, "/lpt")
    from src.foundry_runner import count_filtered, count_mpnn, count_rf3, count_rfd3

    from pathlib import Path

    d = Path(f"/campaign/{campaign_id}")
    return {
        "n_rfd3": count_rfd3(d / "rfd3"),
        "n_filtered": count_filtered(d / "designs_filtered"),
        "n_mpnn": count_mpnn(d / "mpnn_out"),
        "n_rf3": count_rf3(d / "rf3_out"),
    }


def _run_campaign(payload: dict) -> dict:
    """
    Run one RFD3 -> prefilter -> solubleMPNN -> RF3 campaign shard.

    `payload` carries everything the shard needs, because a Modal container
    shares no filesystem with the caller:
      campaign_id, spec (dict), structure_name, structure_bytes,
      n_batches, diffusion_batch_size, n_seq, prefilter (dict),
      mpnn (dict), rf3 (dict), rfd3 (dict), seed_offset

    Returns the final counts plus per-stage seconds. Everything it writes lands
    under `/campaign/<campaign_id>/` in exactly the layout `FoundryPaths.under`
    describes, so the tree can be downloaded straight into a local
    `campaign_dir` and every existing consumer reads it unchanged.
    """
    import json
    import os
    import subprocess
    import sys
    import threading
    import time
    from pathlib import Path

    sys.path.insert(0, "/lpt")
    from src.foundry_runner import (
        count_filtered, count_mpnn, count_rf3, count_rfd3, prefilter_designs,
    )
    from src.foundry_spec import build_mpnn_configs

    cid = payload["campaign_id"]
    root = Path(f"/campaign/{cid}")
    rfd3_dir, filt_dir = root / "rfd3", root / "designs_filtered"
    mpnn_dir, rf3_dir = root / "mpnn_out", root / "rf3_out"
    logs = root / "logs"
    for d in (rfd3_dir, filt_dir, mpnn_dir, mpnn_dir / "configs", rf3_dir, logs):
        d.mkdir(parents=True, exist_ok=True)

    ck = _checkpoints()
    missing = [k for k, v in ck.items() if not v]
    if missing:
        raise RuntimeError(
            f"checkpoint volume {CKPT_VOLUME!r} is missing: {', '.join(missing)}. "
            f"Run `python scripts/modal_setup.py` locally to populate it.")

    # The spec's `input` is an absolute path on the SUBMITTING machine, which
    # means nothing here — rewrite it to the copy we just landed, exactly as
    # cluster_runner rewrites it to a bare name for the share.
    structure = root / payload["structure_name"]
    structure.write_bytes(payload["structure_bytes"])
    spec = dict(payload["spec"])
    name = next(iter(spec))
    spec[name] = {**spec[name], "input": str(structure)}
    # The spec FILENAME is load-bearing under fan-out: RFD3 names its outputs
    # `<spec-file-stem>_<spec-key>_<batch>_model_<k>` and the batch index
    # restarts at 0 in every shard, so a shared stem makes shard 0 and shard 1
    # write the same filenames for different designs. The caller passes a
    # per-shard stem; "spec" is the single-container default.
    spec_path = root / f"{payload.get('spec_stem', 'spec')}.json"
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")

    timings: dict[str, float] = {}
    stage_now = {"stage": "rfd3"}

    def snapshot() -> dict:
        return {
            "campaign_id": cid,
            "stage": stage_now["stage"],
            "n_rfd3": count_rfd3(rfd3_dir),
            "n_filtered": count_filtered(filt_dir),
            "n_mpnn": count_mpnn(mpnn_dir),
            "n_rf3": count_rf3(rf3_dir),
            "expected_rfd3": payload["n_batches"] * payload["diffusion_batch_size"],
            "timings": dict(timings),
            "updated_at": time.time(),
        }

    stop = threading.Event()

    def write_progress() -> None:
        """Publish counts every 30 s so the local side can poll one small file.

        Counting from OUTSIDE (a second Modal function) would start a container
        per poll; counting from inside costs nothing and is the same
        disk-derived number `foundry_runner.progress` computes locally.
        """
        while not stop.is_set():
            try:
                (root / "progress.json").write_text(
                    json.dumps(snapshot(), indent=2), encoding="utf-8")
                campaign_vol.commit()
            except Exception:  # never let telemetry kill a campaign
                pass
            stop.wait(30)

    watcher = threading.Thread(target=write_progress, daemon=True)
    watcher.start()

    def run(tag: str, argv: list[str]) -> int:
        t = time.time()
        with open(logs / f"{tag}.log", "ab") as fh:
            r = subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT,
                               cwd="/app/foundry", env={**os.environ, "CC": "gcc"})
        timings[tag] = round(time.time() - t, 1)
        return r.returncode

    try:
        # ---- 1. RFD3 ---------------------------------------------------
        stage_now["stage"] = "rfd3"
        rfd3_cfg = payload.get("rfd3") or {}
        argv = [f"{BIN}/rfd3", f"out_dir={rfd3_dir}", f"inputs={spec_path}",
                f"n_batches={payload['n_batches']}",
                f"diffusion_batch_size={payload['diffusion_batch_size']}",
                f"ckpt_path={ck['rfd3']}"]
        for key, val in (rfd3_cfg.get("inference_sampler") or {}).items():
            argv.append(f"inference_sampler.{key}={val}")
        run("rfd3", argv)
        if count_rfd3(rfd3_dir) == 0:
            raise RuntimeError(f"RFD3 produced nothing; see {logs / 'rfd3.log'}")

        # ---- 2. prefilter (CPU, LPT's own implementation) ---------------
        stage_now["stage"] = "filter"
        pf = payload.get("prefilter") or {}
        kept, total = prefilter_designs(
            rfd3_dir, filt_dir,
            max_chainbreaks=int(pf.get("max_chainbreaks", 1)),
            max_sidechain_clashes=int(pf.get("max_sidechain_clashes", 0)),
            max_backbone_clashes=int(pf.get("max_backbone_clashes", 0)),
            min_non_loop=float(pf.get("min_non_loop", 0.6)),
            report_path=root / "filter_report.csv")
        if kept == 0:
            raise RuntimeError(
                f"prefilter kept 0 of {total} RFD3 designs — nothing to refold")

        # ---- 3. solubleMPNN --------------------------------------------
        stage_now["stage"] = "mpnn"
        mp = payload.get("mpnn") or {}
        designs = sorted(filt_dir.glob("*.cif.gz"))
        configs = build_mpnn_configs(
            designs, mpnn_dir, mpnn_dir / "configs",
            checkpoint=ck["mpnn"], n_seq=int(mp.get("n_seq", 4)),
            temperature=float(mp.get("temperature", 0.1)),
            omit=tuple((mp.get("omit") or "CYS").split(",")),
            chunk_size=int(mp.get("chunk_size", 250)))
        for cfg_path in configs:
            run("mpnn", [f"{BIN}/mpnn", "--config_json", str(cfg_path)])
        if count_mpnn(mpnn_dir) == 0:
            raise RuntimeError(f"MPNN produced nothing; see {logs / 'mpnn.log'}")

        # ---- 4. RF3 refold ---------------------------------------------
        stage_now["stage"] = "rf3"
        rf = payload.get("rf3") or {}
        template = rf.get("template", "target")
        run("rf3", [
            f"{BIN}/rf3", "fold", f"inputs={mpnn_dir}", f"out_dir={rf3_dir}",
            f"ckpt_path={ck['rf3']}",
            f"template_selection={'B' if template == 'target' else template}",
            f"diffusion_batch_size={int(rf.get('diffusion_batch_size', 1))}",
            f"seed={int(rf.get('seed', 0))}"])
        stage_now["stage"] = "done"
    finally:
        stop.set()
        watcher.join(timeout=5)
        final = snapshot()
        (root / "progress.json").write_text(
            json.dumps(final, indent=2), encoding="utf-8")
        campaign_vol.commit()

    return final


# One entry point per GPU type, because a Modal Function's resources are fixed
# at DEPLOY time — there is no per-call `.options(gpu=...)` in this client. The
# runner picks a function by name, which also means the GPU (and therefore the
# price) is visible at the call site rather than buried in a config read.
#
# A10 is the default and is not the slowest choice it looks like: measured at
# 286 tokens it refolds at ~17 s against this workstation's fitted 17.8 s, i.e.
# at par for RF3, which is the bulk of a campaign. It is ~1.7x slower than the
# local card for RFD3 only.
_GPU_TIMEOUT = 24 * 3600


@app.function(volumes=VOLUMES, gpu="A10", timeout=_GPU_TIMEOUT)
def run_campaign_a10(payload: dict) -> dict:
    return _run_campaign(payload)


@app.function(volumes=VOLUMES, gpu="L40S", timeout=_GPU_TIMEOUT)
def run_campaign_l40s(payload: dict) -> dict:
    return _run_campaign(payload)


@app.function(volumes=VOLUMES, gpu="A100-40GB", timeout=_GPU_TIMEOUT)
def run_campaign_a100(payload: dict) -> dict:
    return _run_campaign(payload)


@app.function(volumes=VOLUMES, gpu="H100", timeout=_GPU_TIMEOUT)
def run_campaign_h100(payload: dict) -> dict:
    return _run_campaign(payload)


@app.local_entrypoint()
def main(campaign_id: str = ""):
    """`modal run src/modal_app.py` — report what the volumes hold."""
    import json

    print(json.dumps(checkpoint_status.remote(), indent=2))
    if campaign_id:
        print(json.dumps(campaign_counts.remote(campaign_id), indent=2))
