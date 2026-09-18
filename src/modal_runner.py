"""
`--compute modal`: run one foundry campaign stage on Modal's GPUs.

This is the LOCAL half; `src/modal_app.py` is the remote half. The compute
model is foundry's ("LPT drives the GPU"), not the cluster's ("LPT stages, a
human submits"): LPT spawns the work itself, polls a small `progress.json`, and
syncs the finished tree back into the ordinary local `campaign_dir`. Once that
sync lands, EVERY downstream consumer — `progress`, `collect`,
`_score_campaign`, `--start-from binder_scoring` — reads the same directory
layout it always did, which is why this module adds no scoring adapter and no
second refold vocabulary.

WHY THIS IS OPT-IN, AND STAYS OPT-IN
------------------------------------
Every other compute target in LPT is free at the point of use: the local GPU is
already bought, and the cluster is someone else's allocation. Modal is billed
per GPU-second to a card with a real balance on it, so a wrong verdict here
does not cost an operator time — it costs them money, silently, while they are
not watching. Three rules follow, and all three are enforced, not documented:

1. **`--compute auto` can never choose Modal.** `choose_compute` picks between
   local and cluster only; `modal` is reachable solely by an operator typing
   `--compute modal`. A campaign therefore cannot drift onto a paid backend
   because a calibration came back slower than expected.
2. **A resume will not silently continue on Modal.** If `calibration.json`
   records that a campaign was placed on Modal and the current invocation did
   not pass `--compute modal`, `assert_opt_in` refuses and says so. The
   alternative — honouring the persisted choice — would let
   `--start-from production` spend hundreds of dollars from a command line that
   never mentions Modal. Falling back to local silently would be the same
   surprise pointed the other way (a 240 GPU-h campaign landing on the
   workstation), so it refuses rather than choosing for the operator.
3. **Every launch is costed first and refused above a cap.**
   `design.modal.max_usd` is a hard ceiling checked before a single GPU-second
   is spent. It is not a warning: a warning in a detached run is a line in a log
   nobody reads until the bill arrives.

PRICES AND RATES ARE MEASURED, NOT ASSUMED
------------------------------------------
`_GPU_USD_PER_HOUR` is transcribed from modal.com/pricing on the date below and
will go stale — it is used only to refuse, never to bill, so a stale LOW price
is the dangerous direction and `max_usd` is deliberately conservative.

`speed_factor` is the measured ratio of Modal-GPU seconds to this workstation's
seconds for the same work, and it is NOT a single number in reality: on a
286-token complex the A10 ran RFD3 at 17.3 s/design against the local card's
10.0 s (1.72x) but RF3 at ~17 s/refold against a fitted 17.8 s (0.96x). RF3 is
the bulk of a campaign, so the blended factor is near 1.1; the default is 1.25,
rounded up because over-costing refuses a run and under-costing spends money.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from src.env_config import resolve_env_path

# Transcribed from https://modal.com/pricing on 2026-09-18. Used only to decide
# whether to REFUSE a launch; Modal's own invoice is authoritative.
PRICES_REVIEWED = "2026-09-18"
_GPU_USD_PER_HOUR = {
    "A10": 1.10,
    "L40S": 1.95,
    "A100-40GB": 2.10,
    "H100": 3.95,
}

# Which deployed function serves which GPU. Resources are fixed at deploy time
# in this Modal client, so the GPU is chosen by picking a function name.
_GPU_FUNCTIONS = {
    "A10": "run_campaign_a10",
    "L40S": "run_campaign_l40s",
    "A100-40GB": "run_campaign_a100",
    "H100": "run_campaign_h100",
}

APP_NAME = "lpt-foundry"
CAMPAIGN_VOLUME = "lpt-campaign"
CKPT_VOLUME = "lpt-foundry-ckpt"

#: Written into the campaign dir so a resume in a fresh process can re-attach
#: to a running Modal call. The pid-based `jobs.json` cannot express a remote
#: call, and `job_registry.is_alive` would read `/proc/<pid>` for a process that
#: was never on this machine.
JOB_FILE = "modal_job.json"


class ModalError(RuntimeError):
    """Modal staging, launch, or sync failed."""


class ModalBudgetError(ModalError):
    """A launch was refused because its estimated cost exceeds the cap."""


class ModalOptInError(ModalError):
    """Modal was reached without an explicit `--compute modal` on this run."""


@dataclass(frozen=True)
class ModalConfig:
    gpu: str = "A10"
    #: Hard ceiling, in USD, on ONE stage launch. Refuses above it.
    max_usd: float = 10.0
    #: Modal seconds per local second for the same work; see module docstring.
    speed_factor: float = 1.25
    #: How many containers to split a stage across. 1 = no fan-out.
    #: Parallelism is FREE in money (Modal bills GPU-seconds, so N containers
    #: for T hours costs exactly what one container for N*T hours costs) and
    #: buys wall-clock. It is not free in OVERHEAD — see `min_shard_minutes`.
    n_containers: int = 1
    #: Refuse to cut shards smaller than this much work. Each container
    #: re-pays model loading for all three stages (~90 s measured: 23 s RFD3,
    #: ~25 s RF3, plus MPNN), so a 5-minute shard wastes ~30% of what it costs.
    #: This is the same fixed-cost effect that makes a 24-design probe
    #: over-cost by 1.5-2x against its own at-scale rate.
    min_shard_minutes: float = 20.0
    app_name: str = APP_NAME
    poll_interval_s: float = 60.0
    #: Skip downloading the PAE matrices. They are ~half the bytes and ipSAE
    #: cannot be recomputed without them, so this is off by default.
    skip_confidences: bool = False
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_cfg(cls, cfg: dict) -> "ModalConfig":
        c = ((cfg or {}).get("design") or {}).get("modal") or {}
        gpu = str(c.get("gpu", "A10"))
        if gpu not in _GPU_FUNCTIONS:
            raise ModalError(
                f"design.modal.gpu={gpu!r} is not one of "
                f"{', '.join(sorted(_GPU_FUNCTIONS))}. Each GPU is a separately "
                f"deployed Modal function, so an unknown name has nothing to "
                f"call.")
        return cls(
            gpu=gpu,
            max_usd=float(c.get("max_usd", 10.0)),
            speed_factor=float(c.get("speed_factor", 1.25)),
            n_containers=max(1, int(c.get("n_containers", 1))),
            min_shard_minutes=float(c.get("min_shard_minutes", 20.0)),
            app_name=str(resolve_env_path("LPT_MODAL_APP", c.get("app_name"))
                         or APP_NAME),
            poll_interval_s=float(c.get("poll_interval_s", 60.0)),
            skip_confidences=bool(c.get("skip_confidences", False)),
        )

    @property
    def usd_per_hour(self) -> float:
        return _GPU_USD_PER_HOUR[self.gpu]

    @property
    def function_name(self) -> str:
        return _GPU_FUNCTIONS[self.gpu]


# ----------------------------------------------------------------------
# The opt-in guarantee
# ----------------------------------------------------------------------

def assert_opt_in(requested_compute: str, resolved_compute: str) -> None:
    """
    Refuse to reach Modal unless THIS invocation asked for it.

    `requested_compute` is what the operator typed (`self._compute`);
    `resolved_compute` is what the run is about to do, which on a resume may
    have come out of `calibration.json` instead. Honouring a persisted `modal`
    placement would let `--start-from production` spend a production campaign's
    budget from a command line that never mentions Modal — see rule 2 in the
    module docstring.
    """
    if resolved_compute != "modal":
        return
    if requested_compute == "modal":
        return
    raise ModalOptInError(
        "this campaign was calibrated for `--compute modal`, but this run did "
        f"not ask for it (--compute {requested_compute}). Modal is billed per "
        "GPU-second, so it is never entered implicitly.\n"
        "  Re-run with --compute modal to continue on Modal, or\n"
        "  --compute local to run the remaining stages on this workstation "
        "(check the estimate first: a production campaign is GPU-DAYS here).")


def estimate_usd(est_gpu_hours: float, mcfg: ModalConfig) -> float:
    """Cost of `est_gpu_hours` LOCAL GPU-hours of work, run on Modal."""
    return float(est_gpu_hours) * mcfg.speed_factor * mcfg.usd_per_hour


def preflight(plan, mcfg: ModalConfig, *, mode: str) -> float:
    """
    Cost this stage and refuse if it exceeds `design.modal.max_usd`.

    Called before the spawn, never after: the point is to not spend the money,
    and a detached campaign has nobody watching a warning.
    """
    usd = estimate_usd(getattr(plan, "est_gpu_hours", 0.0) or 0.0, mcfg)
    logger.info(
        f"[modal] {mode}: ~{plan.est_gpu_hours:,.1f} local GPU-h "
        f"x {mcfg.speed_factor:.2f} on {mcfg.gpu} @ "
        f"${mcfg.usd_per_hour:.2f}/h = ~${usd:,.2f} "
        f"(cap ${mcfg.max_usd:,.2f}, prices reviewed {PRICES_REVIEWED})")
    if usd > mcfg.max_usd:
        raise ModalBudgetError(
            f"the {mode} stage is estimated at ~${usd:,.2f} on {mcfg.gpu}, "
            f"over the ${mcfg.max_usd:,.2f} cap in design.modal.max_usd.\n"
            f"  ({plan.est_gpu_hours:,.1f} local GPU-h x {mcfg.speed_factor:.2f} "
            f"x ${mcfg.usd_per_hour:.2f}/h)\n"
            f"Raise the cap deliberately, pick a cheaper GPU, or size the stage "
            f"down (--n-batches). The estimate is the PLAN's, so it is only as "
            f"good as the rate it was costed at — a campaign with no measured "
            f"refold rate is costed from the size law.")
    return usd


# ----------------------------------------------------------------------
# Campaign identity + the remote job record
# ----------------------------------------------------------------------

#: Fixed per-container cost: each shard loads RFD3 (2.7 GB) and RF3 (3.0 GB)
#: from scratch. Measured on A10 — RFD3 91.7 s wall for 69 s of inference, RF3
#: ~25 s before its first refold — so ~90 s of the three stages is pure load.
SEC_PER_CONTAINER_OVERHEAD = 90.0


def plan_shards(plan, mcfg: ModalConfig) -> list[int]:
    """
    Split `plan.n_batches` across containers, refusing shards too small to pay
    for themselves.

    Returns one batch count per shard; `[plan.n_batches]` when not fanning out.

    The arithmetic that matters: fan-out costs the same GPU-seconds, so the
    ONLY thing that makes it a bad trade is per-container overhead. A shard
    doing less than `min_shard_minutes` of work is spending a growing fraction
    of its bill on loading the same two checkpoints again, so the shard count
    is clamped down to whatever keeps shards above that bar rather than
    silently honouring `n_containers`.
    """
    want = max(1, int(mcfg.n_containers))
    n_batches = max(1, int(plan.n_batches))
    if want == 1:
        return [n_batches]

    # Whole-stage seconds, from the plan the campaign was costed with.
    total_s = float(getattr(plan, "est_gpu_hours", 0.0) or 0.0) * 3600.0
    # `min_shard_minutes: 0` is a legitimate "do not clamp me" — and dividing
    # by it is not. A plan with no cost estimate skips the check too: there is
    # nothing to judge shard size against, and guessing would either block a
    # legitimate fan-out or wave through a wasteful one.
    if total_s > 0 and mcfg.min_shard_minutes > 0:
        affordable = int(total_s // (mcfg.min_shard_minutes * 60.0))
        if affordable < want:
            logger.warning(
                f"[modal] {want} containers would cut shards below "
                f"{mcfg.min_shard_minutes:.0f} min of work each "
                f"(~{total_s / 3600:.1f} GPU-h total); using "
                f"{max(1, affordable)} instead. Each container re-pays "
                f"~{SEC_PER_CONTAINER_OVERHEAD:.0f}s of model loading, so "
                f"smaller shards buy wall-clock by wasting spend.")
            want = max(1, affordable)

    want = min(want, n_batches)  # never more shards than batches
    base, extra = divmod(n_batches, want)
    shards = [base + (1 if i < extra else 0) for i in range(want)]
    return [s for s in shards if s > 0]


def overhead_usd(n_shards: int, mcfg: ModalConfig) -> float:
    """What fanning out to `n_shards` costs in duplicated model loading."""
    extra = max(0, n_shards - 1) * SEC_PER_CONTAINER_OVERHEAD
    return extra / 3600.0 * mcfg.usd_per_hour


def campaign_id_for(campaign_dir: Path) -> str:
    """
    A stable, readable id for one campaign stage on the shared volume.

    Derived from the local path so a resume in a fresh process recomputes the
    SAME id without needing any state — the same reasoning as
    `_cluster_paths_for_mode` being reconstructable from `dirs`/`mode` alone.
    """
    p = Path(campaign_dir).resolve()
    digest = hashlib.sha256(str(p).encode()).hexdigest()[:10]
    # <project>-<round>-<mode>-<hash>: the parts a human needs to recognise it
    # in `modal volume ls`, plus enough entropy to keep two checkouts apart.
    parts = [q for q in (p.parts[-5:] if len(p.parts) >= 5 else p.parts)
             if q not in ("runs", "binder", "campaign")]
    slug = "-".join(parts)[-60:].strip("-").replace("/", "-")
    return f"{slug}-{digest}"


def write_job(campaign_dir: Path, record: dict) -> None:
    path = Path(campaign_dir) / JOB_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_job(campaign_dir: Path) -> dict | None:
    path = Path(campaign_dir) / JOB_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ----------------------------------------------------------------------
# Modal handles
# ----------------------------------------------------------------------

def _require_modal():
    try:
        import modal  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ModalError(
            "the `modal` package is not installed in this environment. "
            "`uv pip install modal`, then `modal setup`.") from exc
    return __import__("modal")


def _function(mcfg: ModalConfig):
    modal = _require_modal()
    try:
        return modal.Function.from_name(mcfg.app_name, mcfg.function_name)
    except Exception as exc:
        raise ModalError(
            f"cannot resolve Modal function {mcfg.function_name!r} in app "
            f"{mcfg.app_name!r}. Deploy it first:\n"
            f"    modal deploy src/modal_app.py\n"
            f"(each GPU is its own function, so deploying is also what makes "
            f"design.modal.gpu={mcfg.gpu!r} available.)") from exc


def _volume(name: str):
    modal = _require_modal()
    return modal.Volume.from_name(name, create_if_missing=True)


def checkpoint_status(mcfg: ModalConfig) -> dict:
    """What the checkpoint volume holds — the doctor/preflight view."""
    modal = _require_modal()
    fn = modal.Function.from_name(mcfg.app_name, "checkpoint_status")
    return fn.remote()


# ----------------------------------------------------------------------
# Launch / poll / sync
# ----------------------------------------------------------------------

def launch(spec_path: Path, paths, cfg: dict, plan, mcfg: ModalConfig,
           *, mode: str, n_target_segments: int = 1) -> dict:
    """
    Spawn one campaign shard on Modal and record the call id.

    Detached by design (`.spawn`, not `.remote`): a calibration is hours and a
    production campaign is days, which is exactly the shape `--detach` and
    `--start-from` already exist for.
    """
    preflight(plan, mcfg, mode=mode)

    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    name = next(iter(spec))
    structure = Path(spec[name]["input"])
    if not structure.is_file():
        raise ModalError(
            f"the spec's input structure {structure} does not exist locally, so "
            f"it cannot be uploaded. A Modal container shares no filesystem "
            f"with this machine.")

    fcfg = (cfg.get("foundry") or {})
    pf = dict(fcfg.get("prefilter") or {})
    # Same derivation as the local driver: an N-segment target contributes an
    # unavoidable N-1 chainbreaks before the binder is looked at, so a
    # hardcoded 1 would reject every design.
    if pf.get("max_chainbreaks") is None:
        pf["max_chainbreaks"] = max(1, int(n_target_segments))

    base_id = campaign_id_for(paths.campaign_dir)
    shards = plan_shards(plan, mcfg)
    fn = _function(mcfg)
    structure_bytes = structure.read_bytes()

    calls = []
    for i, n_batches in enumerate(shards):
        # Two independent reasons a shard's output cannot collide with another's.
        #
        # 1. Its own subdirectory on the volume. Modal volumes are committed per
        #    container, so keeping shards on disjoint paths is what stops two
        #    commits racing over the same state.
        # 2. Its own SPEC FILENAME. RFD3 names outputs
        #    `<spec-file-stem>_<spec-key>_<batch>_model_<k>`, and the batch
        #    index restarts at 0 in every shard — so with a shared stem, shard 0
        #    and shard 1 both write `..._0_model_0` for DIFFERENT designs, and
        #    the merge silently keeps one. Verified the designs really do
        #    differ: two containers given the identical spec produced binders of
        #    73 and 78 residues, because RFD3 inference is unseeded
        #    (`engine.py` only records `seed`, never applies it). That is what
        #    makes fan-out sound in the first place, and what makes the naming
        #    collision dangerous rather than harmless.
        shard_id = f"{base_id}/shard-{i:02d}" if len(shards) > 1 else base_id
        spec_stem = f"s{i:02d}" if len(shards) > 1 else "spec"
        payload = {
            "campaign_id": shard_id,
            "spec_stem": spec_stem,
            "spec": spec,
            "structure_name": structure.name,
            "structure_bytes": structure_bytes,
            "n_batches": n_batches,
            "diffusion_batch_size": int(
                (fcfg.get("rfd3") or {}).get("diffusion_batch_size", 4)),
            "n_seq": int((fcfg.get("mpnn") or {}).get("n_seq", 4)),
            "rfd3": fcfg.get("rfd3") or {},
            "prefilter": pf,
            "mpnn": fcfg.get("mpnn") or {},
            "rf3": fcfg.get("rf3") or {},
        }
        call = fn.spawn(payload)
        calls.append({"call_id": call.object_id, "campaign_id": shard_id,
                      "n_batches": n_batches, "shard": i})

    record = {
        # `calls` is the list even for one container, so nothing downstream
        # needs a single-vs-many branch.
        "calls": calls,
        "call_id": calls[0]["call_id"],          # back-compat / logging
        "campaign_id": base_id,
        "n_shards": len(calls),
        "app_name": mcfg.app_name,
        "function": mcfg.function_name,
        "gpu": mcfg.gpu,
        "mode": mode,
        "estimated_usd": round(
            estimate_usd(plan.est_gpu_hours, mcfg) + overhead_usd(len(calls), mcfg), 2),
        "usd_per_hour": mcfg.usd_per_hour,
        "started_at": time.time(),
        "expected_rfd3": plan.expected_rfd3,
        "expected_rf3": plan.expected_rf3,
    }
    write_job(paths.campaign_dir, record)
    if len(calls) > 1:
        logger.info(
            f"[modal] {mode} fanned out across {len(calls)} containers on "
            f"{mcfg.gpu}: batches {shards} | campaign {base_id} | "
            f"~${record['estimated_usd']:.2f} "
            f"(incl. ~${overhead_usd(len(calls), mcfg):.2f} duplicated model "
            f"loading)")
    else:
        logger.info(
            f"[modal] {mode} spawned: call {calls[0]['call_id']} on {mcfg.gpu}, "
            f"campaign {base_id}")
    return record


def resume(paths, cfg: dict, plan, mcfg: ModalConfig, *, mode: str,
           spec_path: Path | None = None, n_target_segments: int = 1) -> dict:
    """
    Re-attach to a recorded call, or launch if there is none.

    A FAILED previous call is deliberately NOT relaunched automatically, which
    is where this differs from `foundry_runner.resume` (that one re-runs the
    driver whenever it is not still alive). Two reasons, and both are specific
    to this being the billed backend: a silent relaunch spends money the
    command line did not visibly ask for a second time, and the failed call may
    already have written real output — RFD3 would run again into the same
    directory and add designs on top, so the campaign quietly becomes larger
    than it was sized and costed for. The operator is told what failed and how
    to restart it instead.
    """
    record = read_job(paths.campaign_dir)
    if record and record.get("call_id"):
        modal = _require_modal()
        try:
            modal.FunctionCall.from_id(record["call_id"])
        except Exception:
            logger.warning(
                f"[modal] recorded call {record['call_id']} is no longer "
                f"resolvable; relaunching {mode}")
        else:
            status = call_status(record)
            if status == "failed":
                raise ModalError(
                    f"the recorded Modal call for {mode} "
                    f"({record['call_id']}) FAILED; not relaunching it "
                    f"automatically, because that would spend again and would "
                    f"add designs on top of whatever it already wrote.\n"
                    f"  logs:     modal app logs "
                    f"{record.get('app_name', APP_NAME)}\n"
                    f"  retry:    rm {Path(paths.campaign_dir) / JOB_FILE} "
                    f"and re-run the same command\n"
                    f"  (delete the partial tree too if you want a clean "
                    f"count: modal volume rm -r {CAMPAIGN_VOLUME} "
                    f"{record['campaign_id']})")
            logger.info(
                f"[modal] re-attached to call {record['call_id']} "
                f"({record.get('mode', mode)}, {status})")
            return record
    if spec_path is None:
        raise ModalError(
            f"no live Modal call for {mode} and no spec to relaunch from")
    return launch(spec_path, paths, cfg, plan, mcfg, mode=mode,
                  n_target_segments=n_target_segments)


def aggregate_progress(record: dict) -> dict | None:
    """Sum every shard's `progress.json` into one campaign-level view."""
    snaps = [remote_progress(s["campaign_id"]) for s in _shards(record)]
    live = [s for s in snaps if s]
    if not live:
        return None
    total = {k: sum(int(s.get(k, 0) or 0) for s in live)
             for k in ("n_rfd3", "n_filtered", "n_mpnn", "n_rf3", "expected_rfd3")}
    stages = [s.get("stage", "?") for s in live]
    # The campaign is only as far along as its SLOWEST shard, so report that
    # rather than the most advanced one — otherwise a six-shard run claims
    # "rf3" while a straggler is still diffusing backbones.
    order = {"rfd3": 0, "filter": 1, "mpnn": 2, "rf3": 3, "done": 4}
    total["stage"] = min(stages, key=lambda s: order.get(s, 0))
    total["n_shards_reporting"] = len(live)
    total["n_shards"] = len(_shards(record))
    return total


def remote_progress(campaign_id: str) -> dict | None:
    """Read the campaign's `progress.json` off the volume — no container.

    The run writes this itself every 30 s. Counting from here instead would
    start a container per poll, which costs money to learn nothing.
    """
    vol = _volume(CAMPAIGN_VOLUME)
    try:
        chunks = list(vol.read_file(f"{campaign_id}/progress.json"))
    except Exception:
        return None
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _shards(record: dict) -> list[dict]:
    """The shard list, tolerating a record written before fan-out existed."""
    calls = record.get("calls")
    if calls:
        return calls
    return [{"call_id": record["call_id"],
             "campaign_id": record.get("campaign_id", ""), "shard": 0}]


def _one_call_status(call_id: str) -> str:
    modal = _require_modal()
    try:
        call = modal.FunctionCall.from_id(call_id)
    except Exception:
        return "unknown"
    try:
        call.get(timeout=0)
        return "done"
    except TimeoutError:
        return "running"
    except Exception:
        return "failed"


def call_status(record: dict) -> str:
    """
    Aggregate status over every shard: `running` | `done` | `failed` | `unknown`.

    A campaign is only `done` when ALL shards are, and `failed` as soon as any
    shard is — but note the run is NOT abandoned on a failure: the partial
    output of the surviving shards is still synced and still scorable, which is
    the same posture the local path takes toward a campaign that stopped short.
    One dead shard out of six is a smaller sample, not a void one; that is
    exactly what a cluster GPU ECC fault looks like here.
    """
    states = [_one_call_status(s["call_id"]) for s in _shards(record)]
    if any(s == "failed" for s in states):
        return "failed"
    if any(s == "running" for s in states):
        return "running"
    if all(s == "done" for s in states):
        return "done"
    return "unknown"


#: Files every shard writes at the SAME relative path. Design outputs
#: (`rfd3/`, `mpnn_out/`, `rf3_out/`) are uniquely named per shard by their
#: spec stem, but these are not — so merging shards flat lets the last one
#: overwrite the rest. `filter_report.csv` is the one that does damage:
#: `_rebuild_filtered` reads it to recreate `designs_filtered/`, so a partial
#: report makes a re-synced campaign report a prefilter rate of (last shard's
#: survivors)/(all designs) — 0.33 where the truth was 0.92 — and that rate
#: feeds `plan_campaign`'s sizing and disk clamp for the NEXT campaign.
_PER_SHARD_FILES = ("filter_report.csv", "progress.json")
_PER_SHARD_DIRS = ("logs/",)


def _shard_rel(rel: str, shard_label: str | None) -> str:
    """Where a shard's copy of `rel` goes in the merged tree."""
    if not shard_label:
        return rel
    if rel in _PER_SHARD_FILES or any(rel.startswith(d) for d in _PER_SHARD_DIRS):
        return f"shards/{shard_label}/{rel}"
    return rel


def _download_tree(campaign_id: str, into: Path) -> Path | None:
    """
    Pull one campaign tree off the volume, via the Modal CLI's parallel
    downloader. Returns the staged directory, or None if it isn't there.

    Deliberately a subprocess rather than `Volume.read_file` in a loop.
    Measured on the same 148-file shard: **2.3 s through the CLI against ~90 s
    file-by-file**, ~39x. That is not a micro-optimisation — a real campaign is
    ~12,000 files (1,352 refolds x 7 + design outputs, from
    `projects/mesothelioma_showcase`), so the serial path would have spent
    **~2.1 hours** downloading a calibration that took ~6 GPU-h to compute, and
    every resume would pay it again. `Volume.read_file` also can't simply be
    thread-pooled: the sync wrapper around the async client is not safe to
    share across threads.
    """
    import subprocess
    import sys

    into.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "modal", "volume", "get", "--force",
         CAMPAIGN_VOLUME, campaign_id, str(into)],
        capture_output=True, text=True)
    staged = into / Path(campaign_id).name
    if proc.returncode != 0 or not staged.is_dir():
        logger.debug(
            f"[modal] volume get {campaign_id!r} rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '')[-300:]}")
        return None
    return staged


def sync_campaign(record: dict, campaign_dir: Path, mcfg: ModalConfig,
                  *, missing_ok: bool = False) -> dict:
    """
    Download every shard into one local campaign directory.

    Per-shard bookkeeping lands under `shards/<label>/` instead of overwriting
    its siblings, and the shard filter reports are then concatenated into the
    campaign-level `filter_report.csv` that `_rebuild_filtered` and
    `prefilter_rate_observed` actually read. `designs_filtered/` is rebuilt
    ONCE, at the end, from that merged report — rebuilding per shard happens to
    give the right answer only because symlinks accumulate, which is luck
    rather than a property worth relying on.
    """
    shards = _shards(record)
    multi = len(shards) > 1
    totals = {"n_files": 0, "n_bytes": 0, "n_skipped": 0}
    for shard in shards:
        label = f"shard-{shard.get('shard', 0):02d}" if multi else None
        got = sync_back(shard["campaign_id"], campaign_dir, mcfg,
                        missing_ok=missing_ok or multi,
                        shard_label=label, rebuild=False)
        for k in totals:
            totals[k] += got.get(k, 0)

    if multi:
        _merge_filter_reports(Path(campaign_dir))
    totals["n_filtered"] = _rebuild_filtered(Path(campaign_dir))
    logger.info(
        f"[modal] campaign synced: {totals['n_files']:,} files "
        f"({totals['n_bytes'] / 1e9:.2f} GB) from {len(shards)} shard(s); "
        f"{totals['n_filtered']:,} prefilter survivors relinked")
    return totals


def _merge_filter_reports(campaign_dir: Path) -> int:
    """Concatenate `shards/*/filter_report.csv` into the campaign-level one."""
    import csv

    rows: list[dict] = []
    header: list[str] = []
    for part in sorted((campaign_dir / "shards").glob("*/filter_report.csv")):
        with open(part, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            header = header or list(reader.fieldnames or [])
            rows.extend(reader)
    if not rows:
        return 0
    out = campaign_dir / "filter_report.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def sync_back(campaign_id: str, campaign_dir: Path, mcfg: ModalConfig,
              *, missing_ok: bool = False, shard_label: str | None = None,
              rebuild: bool = True) -> dict:
    """
    Download the finished campaign tree into the local `campaign_dir`.

    This is what lets every downstream consumer stay unaware that the work ran
    elsewhere: after it returns, `campaign_dir` holds the same `rfd3/`,
    `mpnn_out/`, `rf3_out/` layout a local run produces, so `progress`,
    `collect`, `_score_campaign` and `--start-from binder_scoring` need no
    Modal-specific branch at all.

    `designs_filtered/` is deliberately NOT downloaded: the prefilter writes
    symlinks into it, which do not survive a volume round-trip. It is rebuilt
    locally from `filter_report.csv`, which carries the same decision.
    """
    dest = Path(campaign_dir)
    dest.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".modal-sync-", dir=dest) as tmp:
        staged = _download_tree(campaign_id, Path(tmp))
        if staged is None:
            if missing_ok:
                # The run failed before writing anything. Saying so plainly
                # beats "cannot list campaign", which reads like a transfer
                # problem and sends the reader looking in the wrong place.
                logger.warning(
                    f"[modal] campaign {campaign_id!r} wrote nothing to the "
                    f"volume — nothing to sync")
                return {"n_files": 0, "n_bytes": 0, "n_filtered": 0,
                        "n_skipped": 0}
            raise ModalError(
                f"cannot download campaign {campaign_id!r} from volume "
                f"{CAMPAIGN_VOLUME!r}")

        n_files = n_bytes = n_skipped = 0
        for src in staged.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(staged).as_posix()
            if rel.startswith("designs_filtered/"):
                continue  # symlinks; rebuilt from filter_report.csv below
            if mcfg.skip_confidences and rel.endswith("_confidences.json") \
                    and not rel.endswith("_summary_confidences.json"):
                n_skipped += 1
                continue
            target = dest / _shard_rel(rel, shard_label)
            target.parent.mkdir(parents=True, exist_ok=True)
            size = src.stat().st_size
            os.replace(src, target)   # same filesystem: a rename, not a copy
            n_files += 1
            n_bytes += size

    kept = _rebuild_filtered(dest) if rebuild else 0
    logger.info(
        f"[modal] synced {n_files:,} files ({n_bytes / 1e9:.2f} GB)"
        + (f" [{shard_label}]" if shard_label else "")
        + f" into {dest}"
        + (f"; {kept:,} prefilter survivors relinked" if rebuild else "")
        + (f"; {n_skipped:,} PAE matrices skipped" if n_skipped else ""))
    return {"n_files": n_files, "n_bytes": n_bytes, "n_filtered": kept,
            "n_skipped": n_skipped}


def _rebuild_filtered(campaign_dir: Path) -> int:
    """Recreate `designs_filtered/` symlinks from the downloaded report.

    `prefilter_rate_observed` counts entries here, and `collect` reports the
    rate in the stage report, so an empty directory would make a synced
    campaign look like it had prefiltered everything away.
    """
    import csv

    report = campaign_dir / "filter_report.csv"
    filt = campaign_dir / "designs_filtered"
    rfd3 = campaign_dir / "rfd3"
    if not report.is_file():
        return 0
    filt.mkdir(parents=True, exist_ok=True)
    kept = 0
    with open(report, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if str(row.get("kept", "")).strip().lower() not in ("1", "true", "yes"):
                continue
            src = rfd3 / f"{row['name']}.cif.gz"
            if not src.is_file():
                continue
            link = filt / src.name
            if not link.exists():
                try:
                    link.symlink_to(src.resolve())
                except OSError:
                    continue
            kept += 1
    return kept


def wait_for_campaign(paths, plan, mcfg: ModalConfig, *, record: dict,
                      poll_s: float | None = None) -> dict:
    """
    Block until the Modal call finishes, then sync the tree back.

    Like `foundry_runner.wait_for_campaign`, returning is not proof the work
    succeeded — the caller still checks the counts. A remote call that FAILED
    is distinguishable here, though, which the cluster path cannot do: a failed
    SLURM job and an in-progress one look identical on a shared mount.
    """
    interval = float(poll_s or mcfg.poll_interval_s)
    cid = record["campaign_id"]
    while True:
        status = call_status(record)
        if status in ("done", "failed"):
            break
        snap = aggregate_progress(record)
        if snap:
            # Same correction `foundry_runner.progress` makes: the plan's
            # expected_rf3 is an ESTIMATE (expected_rfd3 x prefilter_rate x
            # n_seq), and the real MPNN count supersedes it the moment it
            # exists. Reporting against the estimate reads as overshooting —
            # "12/8 refolds" — on any campaign whose prefilter beat 0.59.
            expected = snap.get("n_mpnn") or plan.expected_rf3
            fan = (f" [{snap.get('n_shards_reporting')}/{snap.get('n_shards')} "
                   f"shards]" if snap.get("n_shards", 1) > 1 else "")
            logger.info(
                f"[modal] {snap.get('stage', '?')}{fan}: "
                f"rfd3 {snap.get('n_rfd3', 0):,} | mpnn {snap.get('n_mpnn', 0):,} "
                f"| rf3 {snap.get('n_rf3', 0):,}/{expected:,}")
        time.sleep(interval)

    if status != "failed":
        sync_campaign(record, paths.campaign_dir, mcfg)
        return {"status": status, "progress": aggregate_progress(record)}

    # Failed: sync whatever exists (a campaign that died in RF3 still has real
    # backbones and sequences worth keeping), then raise with the REMOTE
    # exception. Falling through to a generic "nothing to score" would report a
    # container-level failure as a scientific result, and the operator has
    # already paid for the GPU time either way.
    n_files = sync_campaign(record, paths.campaign_dir, mcfg,
                            missing_ok=True)["n_files"]
    failed: list[tuple[int, BaseException | None]] = []
    modal = _require_modal()
    for shard in _shards(record):
        if _one_call_status(shard["call_id"]) != "failed":
            continue
        try:
            modal.FunctionCall.from_id(shard["call_id"]).get(timeout=0)
        except TimeoutError:  # pragma: no cover - status said otherwise
            failed.append((shard["shard"], None))
        except Exception as exc:
            failed.append((shard["shard"], exc))
    detail = "; ".join(f"shard {i}: {exc!r}" for i, exc in failed) or "unknown"
    raise ModalError(
        f"{len(failed)} of {len(_shards(record))} Modal shards failed for "
        f"{record.get('mode', '?')} on {record.get('gpu', '?')} — {detail}\n"
        f"  synced {n_files:,} files of partial output into "
        f"{paths.campaign_dir}\n"
        f"  surviving shards are still scorable: a smaller sample widens the "
        f"Wilson interval, it does not invalidate it\n"
        f"  full remote logs:  modal app logs {record.get('app_name', APP_NAME)}"
    ) from (failed[0][1] if failed else None)


def stop(campaign_dir: Path) -> int:
    """Cancel every recorded shard. Returns how many were cancelled.

    Cancelling one shard of a fan-out and leaving the rest burning would be the
    worst possible outcome on a billed backend, so this always sweeps the whole
    list and reports failures per shard rather than stopping at the first.
    """
    record = read_job(campaign_dir)
    if not record:
        return 0
    modal = _require_modal()
    n = 0
    for shard in _shards(record):
        try:
            modal.FunctionCall.from_id(shard["call_id"]).cancel()
            n += 1
        except Exception as exc:
            logger.warning(
                f"[modal] could not cancel shard {shard.get('shard')} "
                f"({shard['call_id']}): {exc}")
    return n
