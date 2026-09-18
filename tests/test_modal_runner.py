"""
`--compute modal` is the only BILLED compute target, so what these tests pin is
almost entirely about how a run can REACH it, not about what it computes.

The three rules, each with a test:
  1. `choose_compute` can never return "modal" — `auto` cannot drift onto a
     paid backend.
  2. A resume cannot continue on Modal without `--compute modal` being named
     again on that invocation.
  3. A launch over `design.modal.max_usd` is refused before any GPU-second is
     spent, and refused by RAISING, not by logging.

None of these touch the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import modal_runner as mr
from src.campaign_calibration import (
    CalibrationResult, RateEstimate, ScaleEstimate, choose_compute,
)


# ----------------------------------------------------------------------
# Rule 1: "auto" can never select a billed backend
# ----------------------------------------------------------------------

def _scale_up_result(est_gpu_hours: float) -> CalibrationResult:
    """A SCALE_UP result whose pessimistic bound costs `est_gpu_hours`.

    `choose_compute` reads exactly two fields — `verdict` and
    `pessimistic.est_gpu_hours` — so everything else is filled with values
    that are obviously inert rather than plausible, to keep it clear that no
    other field is under test here.
    """
    est = ScaleEstimate(basis="pessimistic", required_backbones=0.0,
                        required_refolds=0.0, est_gpu_hours=est_gpu_hours,
                        est_disk_gb=0.0)
    rate = RateEstimate(unit="refold", k=1, n=100, p_hat=0.01,
                        p_low=0.001, p_high=0.05)
    return CalibrationResult(
        target_designs=50, excellence_bar="iptm>0.7", success_metric="iptm",
        n_refolds_scored=100, n_backbones_scored=100, prefilter_rate=0.59,
        n_seq=4, refold_rate=rate, backbone_rate=rate,
        central=est, pessimistic=est, verdict="SCALE_UP")


@pytest.mark.parametrize("hours", [1.0, 10.0, 100.0, 10_000.0])
def test_choose_compute_never_returns_modal(hours):
    """No trial outcome, however expensive, may place a campaign on Modal.

    This is the load-bearing half of the opt-in guarantee: everything else
    guards a command line, but this guards the AUTOMATIC decision.
    """
    res = _scale_up_result(hours)
    choice = choose_compute(res, max_local_hours=48.0, n_gpus_cluster=8)
    assert choice is not None
    assert choice.compute in ("local", "cluster")
    assert choice.compute != "modal"


def test_choose_compute_source_mentions_no_modal():
    """A cheap structural canary for the rule above.

    If someone adds a third branch to `choose_compute`, the parametrized test
    only catches it when the new branch is reachable at one of the sampled
    hour counts. Reading the source catches it always.
    """
    import inspect

    from src import campaign_calibration

    src = inspect.getsource(campaign_calibration.choose_compute)
    body = src.split('"""')[-1]  # everything after the docstring
    assert "modal" not in body.lower(), (
        "choose_compute now references 'modal' outside its docstring — the "
        "'auto' path must never place a campaign on a billed backend")


# ----------------------------------------------------------------------
# Rule 2: a resume must name --compute modal again
# ----------------------------------------------------------------------

def test_assert_opt_in_allows_explicit_request():
    mr.assert_opt_in("modal", "modal")  # must not raise


@pytest.mark.parametrize("requested", ["auto", "local", "cluster"])
def test_assert_opt_in_refuses_persisted_modal(requested):
    """calibration.json saying "modal" is not consent from THIS invocation."""
    with pytest.raises(mr.ModalOptInError) as exc:
        mr.assert_opt_in(requested, "modal")
    msg = str(exc.value)
    assert "--compute modal" in msg
    assert "--compute local" in msg, "refusal must name both ways out"


@pytest.mark.parametrize("resolved", ["local", "cluster", "auto"])
def test_assert_opt_in_ignores_non_modal(resolved):
    mr.assert_opt_in("auto", resolved)  # unbilled targets are unaffected


# ----------------------------------------------------------------------
# Rule 3: cost is checked before the spend, and refuses by raising
# ----------------------------------------------------------------------

class _Plan:
    def __init__(self, est_gpu_hours: float):
        self.est_gpu_hours = est_gpu_hours


def test_preflight_refuses_over_cap():
    cfg = mr.ModalConfig(gpu="A10", max_usd=5.0, speed_factor=1.0)
    with pytest.raises(mr.ModalBudgetError) as exc:
        mr.preflight(_Plan(100.0), cfg, mode="production")
    msg = str(exc.value)
    assert "production" in msg
    assert "max_usd" in msg


def test_preflight_allows_under_cap_and_returns_estimate():
    cfg = mr.ModalConfig(gpu="A10", max_usd=100.0, speed_factor=1.0)
    usd = mr.preflight(_Plan(2.0), cfg, mode="pilot")
    assert usd == pytest.approx(2.0 * 1.10, rel=1e-6)


def test_estimate_uses_speed_factor_and_gpu_price():
    a10 = mr.ModalConfig(gpu="A10", speed_factor=1.25)
    h100 = mr.ModalConfig(gpu="H100", speed_factor=1.25)
    assert mr.estimate_usd(10.0, h100) > mr.estimate_usd(10.0, a10)
    assert mr.estimate_usd(10.0, a10) == pytest.approx(10.0 * 1.25 * 1.10)


def test_every_priced_gpu_has_a_deployed_function_name():
    """A GPU you can price but cannot call is a runtime failure, not a config
    error — the two tables must not drift."""
    assert set(mr._GPU_USD_PER_HOUR) == set(mr._GPU_FUNCTIONS)


def test_unknown_gpu_is_refused_at_config_time():
    with pytest.raises(mr.ModalError):
        mr.ModalConfig.from_cfg({"design": {"modal": {"gpu": "RTX-9090"}}})


# ----------------------------------------------------------------------
# Campaign identity must survive a fresh process
# ----------------------------------------------------------------------

def test_campaign_id_is_stable_and_path_derived(tmp_path):
    """`--start-from production` in a new process recomputes the same id.

    The whole resume story depends on this: nothing is persisted to derive it
    from, exactly as `_cluster_paths_for_mode` reconstructs cluster paths from
    `dirs`/`mode` alone.
    """
    d = tmp_path / "projects" / "demo" / "runs" / "round-1" / "binder" / "campaign" / "pilot"
    d.mkdir(parents=True)
    assert mr.campaign_id_for(d) == mr.campaign_id_for(Path(str(d)))


def test_campaign_id_differs_per_mode(tmp_path):
    base = tmp_path / "binder" / "campaign"
    (base / "pilot").mkdir(parents=True)
    (base / "production").mkdir(parents=True)
    assert mr.campaign_id_for(base / "pilot") != mr.campaign_id_for(base / "production")


def test_job_record_round_trips(tmp_path):
    rec = {"call_id": "fc-123", "campaign_id": "demo", "gpu": "A10"}
    mr.write_job(tmp_path, rec)
    assert mr.read_job(tmp_path) == rec
    assert (tmp_path / mr.JOB_FILE).is_file()


def test_read_job_survives_a_torn_write(tmp_path):
    (tmp_path / mr.JOB_FILE).write_text("{not json", encoding="utf-8")
    assert mr.read_job(tmp_path) is None


# ----------------------------------------------------------------------
# The pipeline-level wiring
# ----------------------------------------------------------------------

def test_pipeline_accepts_modal_as_a_compute_value():
    import inspect

    from src import pipeline_runner

    src = inspect.getsource(pipeline_runner.PipelineRunner.__init__)
    assert '"modal"' in src, "PipelineRunner must accept compute='modal'"


def test_scoring_only_branches_on_cluster():
    """A synced Modal tree is scored by the LOCAL path.

    `_run_modal_stage` downloads the campaign into the ordinary
    `campaign_dir`, so if `_stage_binder_scoring` ever grew a `== "modal"`
    branch it would be looking for remote paths that do not exist.
    """
    import inspect

    from src import pipeline_runner

    src = inspect.getsource(pipeline_runner.PipelineRunner._stage_binder_scoring)
    assert '== "modal"' not in src


def test_calibration_persists_a_modal_batch_count():
    """`_resolve_production_plan` reads `n_batches_{compute}` by f-string.

    A missing `n_batches_modal` sends a modal resume to config.yaml's raw
    production default instead of the measured recommendation.
    """
    import inspect

    from src import pipeline_runner

    src = inspect.getsource(pipeline_runner.PipelineRunner._stage_calibration)
    assert '"n_batches_modal"' in src


# ----------------------------------------------------------------------
# Fan-out: free in money, not in overhead
# ----------------------------------------------------------------------

class _ShardPlan:
    def __init__(self, n_batches: int, est_gpu_hours: float):
        self.n_batches = n_batches
        self.est_gpu_hours = est_gpu_hours


def test_no_fanout_by_default():
    cfg = mr.ModalConfig()
    assert cfg.n_containers == 1
    assert mr.plan_shards(_ShardPlan(3000, 87.0), cfg) == [3000]


def test_shards_partition_the_batches_exactly():
    """Every batch must be run once — no loss, no duplication.

    A shard split that dropped or double-counted batches would change the
    campaign's size without changing what it was costed at, and the Wilson
    interval would then be computed over a denominator that never ran.
    """
    cfg = mr.ModalConfig(n_containers=6, min_shard_minutes=0.0)
    shards = mr.plan_shards(_ShardPlan(1000, 40.0), cfg)
    assert len(shards) == 6
    assert sum(shards) == 1000
    assert max(shards) - min(shards) <= 1  # balanced


def test_tiny_shards_are_refused_and_clamped():
    """20 containers over 1 GPU-h would be 3-minute shards; each re-pays ~90 s
    of model loading, so the count is reduced rather than honoured."""
    cfg = mr.ModalConfig(n_containers=20, min_shard_minutes=20.0)
    shards = mr.plan_shards(_ShardPlan(1000, 1.0), cfg)
    assert len(shards) <= 3           # 1 GPU-h / 20 min
    assert sum(shards) == 1000


def test_shard_count_never_exceeds_batch_count():
    cfg = mr.ModalConfig(n_containers=8, min_shard_minutes=0.0)
    shards = mr.plan_shards(_ShardPlan(3, 10.0), cfg)
    assert sum(shards) == 3 and all(s > 0 for s in shards) and len(shards) <= 3


def test_overhead_is_charged_per_extra_container_only():
    cfg = mr.ModalConfig(gpu="A10")
    assert mr.overhead_usd(1, cfg) == 0.0
    six = mr.overhead_usd(6, cfg)
    assert six == pytest.approx(5 * mr.SEC_PER_CONTAINER_OVERHEAD / 3600 * 1.10)
    # Real but small: fanning out 6 ways costs well under a dollar in reloads.
    assert six < 0.25


def test_aggregate_status_reports_the_slowest_shard():
    """One failed shard fails the campaign; one running shard keeps it running."""
    import unittest.mock as m

    rec = {"calls": [{"call_id": f"fc-{i}", "campaign_id": f"c/shard-{i}",
                      "shard": i} for i in range(3)]}
    with m.patch.object(mr, "_one_call_status", side_effect=["done", "done", "done"]):
        assert mr.call_status(rec) == "done"
    with m.patch.object(mr, "_one_call_status", side_effect=["done", "running", "done"]):
        assert mr.call_status(rec) == "running"
    with m.patch.object(mr, "_one_call_status", side_effect=["done", "failed", "done"]):
        assert mr.call_status(rec) == "failed"


def test_aggregate_progress_sums_shards_and_reports_the_laggard():
    import unittest.mock as m

    rec = {"calls": [{"call_id": "a", "campaign_id": "c/shard-00", "shard": 0},
                     {"call_id": "b", "campaign_id": "c/shard-01", "shard": 1}]}
    snaps = {"c/shard-00": {"stage": "rf3", "n_rfd3": 100, "n_mpnn": 200, "n_rf3": 50},
             "c/shard-01": {"stage": "rfd3", "n_rfd3": 40, "n_mpnn": 0, "n_rf3": 0}}
    with m.patch.object(mr, "remote_progress", side_effect=lambda c: snaps[c]):
        agg = mr.aggregate_progress(rec)
    assert agg["n_rfd3"] == 140 and agg["n_rf3"] == 50
    # Slowest shard, not the most advanced one.
    assert agg["stage"] == "rfd3"
    assert agg["n_shards"] == 2


def test_a_prefanout_record_still_reads():
    """`modal_job.json` written before fan-out existed has no `calls` list."""
    old = {"call_id": "fc-legacy", "campaign_id": "demo"}
    shards = mr._shards(old)
    assert len(shards) == 1 and shards[0]["call_id"] == "fc-legacy"


# ----------------------------------------------------------------------
# Merging shards: design outputs are unique, bookkeeping files are not
# ----------------------------------------------------------------------

def test_per_shard_bookkeeping_is_namespaced_not_overwritten():
    """Every shard writes `filter_report.csv`, `progress.json` and `logs/*` at
    the SAME path. Flat-merging lets the last shard win, which silently
    discards the other shards' prefilter decisions."""
    assert mr._shard_rel("rfd3/s00_x_0_model_0.cif.gz", "shard-01") == \
        "rfd3/s00_x_0_model_0.cif.gz"                      # uniquely named already
    assert mr._shard_rel("rf3_out/x/x_summary_confidences.json", "shard-01") == \
        "rf3_out/x/x_summary_confidences.json"
    assert mr._shard_rel("filter_report.csv", "shard-01") == \
        "shards/shard-01/filter_report.csv"
    assert mr._shard_rel("logs/rfd3.log", "shard-01") == \
        "shards/shard-01/logs/rfd3.log"
    # Single-container runs keep the flat layout a local campaign has.
    assert mr._shard_rel("filter_report.csv", None) == "filter_report.csv"


def test_merged_filter_report_covers_every_shard(tmp_path):
    """The merged report is what `_rebuild_filtered` and
    `prefilter_rate_observed` read, so a partial one mis-reports the prefilter
    rate — which feeds the NEXT campaign's sizing and disk clamp."""
    for i, names in enumerate([("s00_a", "s00_b"), ("s01_a",), ("s02_a", "s02_b")]):
        d = tmp_path / "shards" / f"shard-{i:02d}"
        d.mkdir(parents=True)
        rows = "\n".join(f"{n},True," for n in names)
        (d / "filter_report.csv").write_text(f"name,kept,reason\n{rows}\n",
                                             encoding="utf-8")
    assert mr._merge_filter_reports(tmp_path) == 5
    text = (tmp_path / "filter_report.csv").read_text()
    assert text.count("\n") == 6            # header + 5 rows
    for stem in ("s00_a", "s01_a", "s02_b"):
        assert stem in text


def test_rebuild_filtered_uses_the_merged_report(tmp_path):
    """End-to-end of the bug: 3 shards, 5 survivors, all relinked from one
    merged report rather than only the last shard's."""
    rfd3 = tmp_path / "rfd3"
    rfd3.mkdir()
    for i, names in enumerate([("s00_a", "s00_b"), ("s01_a",), ("s02_a", "s02_b")]):
        d = tmp_path / "shards" / f"shard-{i:02d}"
        d.mkdir(parents=True)
        rows = "\n".join(f"{n},True," for n in names)
        (d / "filter_report.csv").write_text(f"name,kept,reason\n{rows}\n",
                                             encoding="utf-8")
        for n in names:
            (rfd3 / f"{n}.cif.gz").write_bytes(b"x")
    mr._merge_filter_reports(tmp_path)
    assert mr._rebuild_filtered(tmp_path) == 5
    assert len(list((tmp_path / "designs_filtered").glob("*.cif.gz"))) == 5
