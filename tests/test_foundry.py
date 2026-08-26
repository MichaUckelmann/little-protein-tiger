"""Spec emission + validation, the prefilter, planning, and the job registry."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml

from src.foundry_runner import (
    FoundryPaths, FoundryValidationError, count_mpnn, count_rf3, count_rfd3,
    plan_campaign, prefilter_designs, run_design, write_campaign_driver,
)
from src.foundry_spec import (
    SpecError, build_mpnn_configs, build_rfd3_spec, parse_contig,
    strip_design_suffixes, validate_spec,
)
from src.job_registry import (
    STATUS_DONE, STATUS_FAILED, JobRegistry, is_alive,
)

_ROOT = Path(__file__).resolve().parents[1]
# LPT_BCR_REFERENCE_DIR points at the root of the reference campaign's data;
# see tests/conftest.py for the shared default and rationale.
_BCR_ROOT = Path(os.environ.get(
    "LPT_BCR_REFERENCE_DIR", str(Path.home() / "data" / "BCR")))
_BCR_INPUTS = _BCR_ROOT / "inputs"
_BCR_RFD3 = _BCR_ROOT / "outputs" / "production" / "CD79b" / "rfd3"


@pytest.fixture(scope="module")
def design_cfg() -> dict:
    return yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))["design"]


# ----------------------------------------------------------------------
# Contigs
# ----------------------------------------------------------------------

def test_parse_contig_round_trip():
    binder, spans = parse_contig("68-86,/0,C44-145")
    assert binder == (68, 86)
    assert spans == [("C", 44, 145)]


def test_parse_contig_multi_segment():
    _, spans = parse_contig("70-86,/0,A12-98,A150-190")
    assert spans == [("A", 12, 98), ("A", 150, 190)]


@pytest.mark.parametrize("bad", ["", "C44-145", "68-86,/0", "68-86,/0,nonsense"])
def test_parse_contig_rejects_malformed(bad):
    with pytest.raises(SpecError):
        parse_contig(bad)


def test_strip_design_suffixes():
    assert strip_design_suffixes("x_model_0.cif.gz") == "x_model_0"
    assert strip_design_suffixes("x_b0_d1.cif") == "x_b0_d1"


# ----------------------------------------------------------------------
# RFD3 spec
# ----------------------------------------------------------------------

@pytest.mark.skipif(not (_BCR_INPUTS / "CD79b_binder_001.json").exists(),
                    reason="reference campaign inputs unavailable")
def test_regenerates_the_reference_cd79b_spec_field_for_field(tmp_path):
    """Ground truth: the spec that actually produced the CD79b campaign."""
    ref = json.loads((_BCR_INPUTS / "CD79b_binder_001.json").read_text())
    name = next(iter(ref))
    entry = ref[name]
    hotspots = [{"residue": "X", "auth_seq_id": int(k[1:]), "rfd3_atoms": v}
                for k, v in entry["select_hotspots"].items()]
    spec = build_rfd3_spec(
        name=name, structure_path=entry["input"], contig=entry["contig"],
        hotspots=hotspots, target_chain="C", out_path=tmp_path / "spec.json",
        binder_min=68, binder_max=86)
    mine = spec.entry
    for key in ("dialect", "contig", "infer_ori_strategy", "is_non_loopy",
                "select_hotspots"):
        assert mine[key] == entry[key], key
    assert Path(mine["input"]).resolve() == Path(entry["input"]).resolve()


def test_spec_requires_hotspots(tmp_path):
    """Without them RFD3 places the binder by centre of mass, missing the epitope."""
    with pytest.raises(SpecError, match="no hotspots"):
        build_rfd3_spec(name="d", structure_path=tmp_path / "x.pdb",
                        contig="68-86,/0,A1-50", hotspots=[], target_chain="A",
                        out_path=tmp_path / "s.json", binder_min=68, binder_max=86)


def test_spec_requires_atom_level_hotspots(tmp_path):
    """RFD3 hotspot selection is atom-level; a bare residue is not enough."""
    with pytest.raises(SpecError, match="atom-level"):
        build_rfd3_spec(name="d", structure_path=tmp_path / "x.pdb",
                        contig="68-86,/0,A1-50",
                        hotspots=[{"auth_seq_id": 10, "rfd3_atoms": ""}],
                        target_chain="A", out_path=tmp_path / "s.json",
                        binder_min=68, binder_max=86)


# ----------------------------------------------------------------------
# validate_spec — the last cheap place to catch a wasted campaign
# ----------------------------------------------------------------------

@pytest.fixture
def real_spec(tmp_path):
    ref_path = _BCR_INPUTS / "CD79b_binder_001.json"
    if not ref_path.exists():
        pytest.skip("reference campaign inputs unavailable")
    ref = json.loads(ref_path.read_text())
    out = tmp_path / "spec.json"
    out.write_text(json.dumps(ref))
    return out, ref, next(iter(ref))


def test_validate_accepts_the_real_spec(real_spec):
    path, _, name = real_spec
    summary = validate_spec(path)
    d = summary["designs"][name]
    assert d["n_target_residues"] == 102
    assert d["n_hotspots"] == 6
    assert d["n_segments"] == 1


def test_validate_rejects_a_hotspot_atom_the_residue_lacks(real_spec, tmp_path):
    """RFD3 accepts this and diffuses against nothing in particular for days."""
    path, ref, name = real_spec
    ref[name]["select_hotspots"]["C76"] = "CG,CD1,ZZ9"
    path.write_text(json.dumps(ref))
    with pytest.raises(SpecError, match="does not have"):
        validate_spec(path)


def test_validate_rejects_a_hotspot_that_is_not_in_the_structure(real_spec):
    path, ref, name = real_spec
    ref[name]["select_hotspots"]["C9999"] = "CB"
    path.write_text(json.dumps(ref))
    with pytest.raises(SpecError, match="not present"):
        validate_spec(path)


def test_validate_rejects_a_hotspot_outside_the_contig(real_spec):
    """i.e. the trim removed it, or the contig is wrong."""
    path, ref, name = real_spec
    ref[name]["contig"] = "68-86,/0,C44-100"
    path.write_text(json.dumps(ref))
    with pytest.raises(SpecError, match="outside every contig"):
        validate_spec(path)


def test_validate_rejects_wrong_dialect(real_spec):
    path, ref, name = real_spec
    ref[name]["dialect"] = 1
    path.write_text(json.dumps(ref))
    with pytest.raises(SpecError, match="dialect"):
        validate_spec(path)


def test_validate_rejects_a_missing_input_structure(real_spec):
    path, ref, name = real_spec
    ref[name]["input"] = "/nonexistent/target.pdb"
    path.write_text(json.dumps(ref))
    with pytest.raises(SpecError, match="does not exist"):
        validate_spec(path)


def test_validate_enforces_the_residue_budget(real_spec):
    path, _, _ = real_spec
    with pytest.raises(SpecError, match="over the"):
        validate_spec(path, max_target_residues=50)


def test_validate_cross_checks_against_the_trim(real_spec):
    path, _, _ = real_spec
    validate_spec(path, kept_segments=[(44, 145)])
    with pytest.raises(SpecError, match="do not match the trim"):
        validate_spec(path, kept_segments=[(44, 120)])


# ----------------------------------------------------------------------
# MPNN configs
# ----------------------------------------------------------------------

def test_mpnn_configs_are_chunked(tmp_path):
    """
    Chunking is not an optimisation: the mpnn CLI flushes only after its last
    input, so one config over thousands of designs holds everything in RAM and
    loses all of it on a late crash.
    """
    designs = []
    for i in range(7):
        f = tmp_path / f"d{i}.cif.gz"
        f.write_bytes(b"")
        designs.append(f)
    configs = build_mpnn_configs(designs, tmp_path / "out", tmp_path / "cfg",
                                 checkpoint="solublempnn", chunk_size=3)
    assert len(configs) == 3
    total = sum(len(json.loads(c.read_text())["inputs"]) for c in configs)
    assert total == 7


def test_mpnn_config_shape(tmp_path):
    f = tmp_path / "d.cif.gz"
    f.write_bytes(b"")
    cfg = json.loads(build_mpnn_configs(
        [f], tmp_path / "out", tmp_path / "cfg",
        checkpoint="/ckpt/solublempnn.pt", n_seq=4)[0].read_text())
    # solubleMPNN is ProteinMPNN with different weights — only the checkpoint
    # changes, so model_type and is_legacy_weights must not be touched.
    assert cfg["model_type"] == "protein_mpnn"
    assert cfg["is_legacy_weights"] is True
    entry = cfg["inputs"][0]
    assert entry["batch_size"] == 4
    # A LIST: this foundry build type-checks it and rejects the bare string,
    # after RFD3 has already run.
    assert entry["designed_chains"] == ["A"]    # RFD3 puts the binder in A
    assert "CYS" in entry["omit"] and "UNK" in entry["omit"]


# ----------------------------------------------------------------------
# Prefilter
# ----------------------------------------------------------------------

def _sidecar(d: Path, name: str, *, breaks=1, sc=0, bb=0, non_loop=0.8):
    (d / f"{name}.cif.gz").write_bytes(b"")
    (d / f"{name}.json").write_text(json.dumps({"metrics": {
        "n_chainbreaks": breaks,
        # RFD3 writes these as FLAT dotted keys, not a nested object.
        "n_clashing.interresidue_clashes_w_sidechain": sc,
        "n_clashing.interresidue_clashes_w_backbone": bb,
        "non_loop_fraction": non_loop,
    }}))


def test_prefilter_gates(tmp_path):
    src, out = tmp_path / "rfd3", tmp_path / "kept"
    src.mkdir()
    _sidecar(src, "good")
    _sidecar(src, "loopy", non_loop=0.3)
    _sidecar(src, "clashy", sc=2)
    _sidecar(src, "broken", breaks=3)
    kept, total = prefilter_designs(src, out, max_chainbreaks=1,
                                    report_path=tmp_path / "r.csv")
    assert (kept, total) == (1, 4)
    assert (out / "good.cif.gz").is_symlink()


def test_prefilter_chainbreak_threshold_tracks_segment_count(tmp_path):
    """A two-segment trim scores 2 chainbreaks; a hardcoded 1 rejects everything."""
    src, out = tmp_path / "rfd3", tmp_path / "kept"
    src.mkdir()
    _sidecar(src, "twoseg", breaks=2)
    assert prefilter_designs(src, out, max_chainbreaks=1)[0] == 0
    assert prefilter_designs(src, out, max_chainbreaks=2)[0] == 1


def test_prefilter_clears_stale_symlinks(tmp_path):
    """Re-running at a stricter threshold must not leave old survivors behind."""
    src, out = tmp_path / "rfd3", tmp_path / "kept"
    src.mkdir()
    _sidecar(src, "loopy", non_loop=0.7)
    assert prefilter_designs(src, out, max_chainbreaks=1, min_non_loop=0.6)[0] == 1
    assert prefilter_designs(src, out, max_chainbreaks=1, min_non_loop=0.9)[0] == 0
    assert not (out / "loopy.cif.gz").exists()


def test_prefilter_handles_a_missing_sidecar(tmp_path):
    src, out = tmp_path / "rfd3", tmp_path / "kept"
    src.mkdir()
    (src / "orphan.cif.gz").write_bytes(b"")
    assert prefilter_designs(src, out, max_chainbreaks=1) == (0, 1)


@pytest.mark.skipif(not _BCR_RFD3.is_dir(), reason="reference RFD3 output unavailable")
def test_prefilter_reproduces_the_reference_survival(tmp_path):
    """The reference driver.log records `prefilter kept 7105/12000`."""
    kept, total = prefilter_designs(_BCR_RFD3, tmp_path / "kept",
                                    max_chainbreaks=1, min_non_loop=0.6)
    assert (kept, total) == (7105, 12000)


# ----------------------------------------------------------------------
# Counting
# ----------------------------------------------------------------------

def test_counters_on_an_empty_or_missing_tree(tmp_path):
    for fn in (count_rfd3, count_mpnn, count_rf3):
        assert fn(tmp_path) == 0
        assert fn(tmp_path / "nope") == 0


def test_rf3_counter_keys_on_summary_confidences(tmp_path):
    """
    RF3's own skip_existing keys on `_metrics.csv`, which it writes only when
    early stopping triggers — trusting it silently refolds finished designs.
    """
    d = tmp_path / "x"
    (d / "design_a").mkdir(parents=True)
    (d / "design_a" / "design_a_summary_confidences.json").write_text("{}")
    (d / "design_b").mkdir()
    (d / "design_b" / "design_b_metrics.csv").write_text("")
    assert count_rf3(d) == 1


# ----------------------------------------------------------------------
# Planning + driver
# ----------------------------------------------------------------------

def test_plan_scales_with_batches(design_cfg, tmp_path, roomy_disk):
    """n_batches scales the campaign linearly — with the disk clamp out of play.

    `plan_campaign` clamps n_batches to what free disk allows, so on a machine
    with little headroom BOTH plans clamp to the same floor and the scaling
    property silently stops being tested: on a 14 GB CI runner this asserted
    4 == 40. The clamp has its own test below; this one is about scaling, so it
    pins free space rather than inheriting the host's.
    """
    paths = FoundryPaths.under(tmp_path)
    small = plan_campaign(design_cfg, paths, mode="pilot", n_batches=10)
    big = plan_campaign(design_cfg, paths, mode="pilot", n_batches=100)
    assert big.expected_rfd3 == 10 * small.expected_rfd3
    assert big.est_disk_gb > small.est_disk_gb


def test_plan_clamps_to_the_disk_budget(design_cfg, tmp_path):
    """48,000 refolds is ~120 GB — half the free space on this workstation."""
    cfg = {**design_cfg, "foundry": {**design_cfg["foundry"], "disk_budget_gb": 1}}
    plan = plan_campaign(cfg, FoundryPaths.under(tmp_path), mode="production")
    assert plan.warnings and "clamped" in plan.warnings[0]
    assert plan.n_batches < design_cfg["foundry"]["production"]["n_batches"]
    assert plan.est_disk_gb <= 1.5


def test_driver_is_valid_bash_and_carries_the_ppi_settings(design_cfg, tmp_path,
                                                           real_spec, foundry_root):
    import subprocess

    spec_path, _, _ = real_spec
    paths = FoundryPaths.under(tmp_path)
    paths.mkdirs()
    plan = plan_campaign(design_cfg, paths, mode="pilot")
    driver = write_campaign_driver(design_cfg, spec_path, paths, plan,
                                   n_target_segments=1)
    assert subprocess.run(["bash", "-n", str(driver)]).returncode == 0
    text = driver.read_text()
    # IPD's PPI-designability settings, which the reference campaign omitted.
    assert "inference_sampler.step_scale=3" in text
    assert "inference_sampler.gamma_0=0.2" in text
    # find, never ls: these directories hold 50-100k entries, where `ls | wc -l`
    # in a pipeline silently reports 0. Check executable lines only — the
    # explanation of this rule lives in a comment that mentions `ls |`.
    code = "\n".join(l for l in text.splitlines()
                     if l.strip() and not l.lstrip().startswith("#"))
    assert "ls |" not in code
    assert "find " in code
    assert "MIN_FREE_GB" in text


def test_driver_derives_the_chainbreak_threshold(design_cfg, tmp_path, real_spec,
                                                 foundry_root):
    spec_path, _, _ = real_spec
    paths = FoundryPaths.under(tmp_path)
    paths.mkdirs()
    plan = plan_campaign(design_cfg, paths, mode="pilot")
    text = write_campaign_driver(design_cfg, spec_path, paths, plan,
                                 n_target_segments=3).read_text()
    assert "--max-chainbreaks 3" in text


def test_run_design_refuses_an_invalid_spec(design_cfg, tmp_path, real_spec):
    """Validation must run BEFORE the GPU does."""
    spec_path, ref, name = real_spec
    ref[name]["select_hotspots"]["C76"] = "CG,QQ1"
    spec_path.write_text(json.dumps(ref))
    with pytest.raises(FoundryValidationError):
        run_design(spec_path, FoundryPaths.under(tmp_path), cfg=design_cfg,
                   plan=plan_campaign(design_cfg, FoundryPaths.under(tmp_path),
                                      mode="pilot"))


# ----------------------------------------------------------------------
# Job registry
# ----------------------------------------------------------------------

def test_job_lifecycle(tmp_path):
    reg = JobRegistry(tmp_path / "jobs.json")
    rec = reg.launch("j", ["sleep", "0.3"], cwd=tmp_path, log_path=tmp_path / "j.log")
    assert is_alive(rec)
    time.sleep(0.8)
    assert reg.refresh("j").status == STATUS_DONE


def test_nonzero_exit_is_failed_not_done(tmp_path):
    reg = JobRegistry(tmp_path / "jobs.json")
    reg.launch("f", ["false"], cwd=tmp_path, log_path=tmp_path / "f.log")
    time.sleep(0.5)
    assert reg.refresh("f").status == STATUS_FAILED


def test_a_reaped_but_unwaited_child_is_not_alive(tmp_path):
    """
    A finished child we have not reaped is a zombie: it keeps its /proc entry and
    still accepts signal 0, so both obvious liveness checks say "running".
    """
    reg = JobRegistry(tmp_path / "jobs.json")
    rec = reg.launch("z", ["true"], cwd=tmp_path, log_path=tmp_path / "z.log")
    time.sleep(0.4)
    assert not is_alive(rec)


def test_recycled_pid_is_not_mistaken_for_the_job(tmp_path):
    """After a reboot a bare os.kill(pid, 0) reports a stranger's process alive."""
    reg = JobRegistry(tmp_path / "jobs.json")
    rec = reg.launch("p", ["sleep", "5"], cwd=tmp_path, log_path=tmp_path / "p.log")
    assert is_alive(rec)
    rec.pid_start_ticks = 999_999_999
    assert not is_alive(rec)
    reg.kill("p")


def test_registry_survives_a_reload(tmp_path):
    reg = JobRegistry(tmp_path / "jobs.json")
    reg.launch("a", ["true"], cwd=tmp_path, log_path=tmp_path / "a.log")
    time.sleep(0.3)
    reg.refresh("a")
    assert list(JobRegistry(tmp_path / "jobs.json").jobs) == ["a"]


def test_relaunching_attaches_instead_of_starting_a_second_run(tmp_path):
    """Two campaigns racing for one GPU is the failure this prevents."""
    reg = JobRegistry(tmp_path / "jobs.json")
    first = reg.launch("c", ["sleep", "5"], cwd=tmp_path, log_path=tmp_path / "c.log")
    second = reg.launch("c", ["sleep", "5"], cwd=tmp_path, log_path=tmp_path / "c.log")
    assert second.pid == first.pid
    reg.kill("c")


def test_poll_until_returns_on_the_disk_condition_not_process_exit(tmp_path):
    """
    The driver restarts RFD3 and RF3 through its own retry loops, so "the
    process is gone" and "the work is done" are independent facts.
    """
    reg = JobRegistry(tmp_path / "jobs.json")
    marker = tmp_path / "done"
    reg.launch("s", ["bash", "-c", f"sleep 0.4; touch {marker}; sleep 10"],
               cwd=tmp_path, log_path=tmp_path / "s.log")
    rec = reg.poll_until("s", until=marker.exists, tick_s=0.1)
    assert marker.exists()
    assert rec.status == "running"
    reg.kill("s")


# ----------------------------------------------------------------------
# Checkpoint aliases
# ----------------------------------------------------------------------

def test_mpnn_checkpoint_aliases_are_resolved_to_real_paths(tmp_path):
    """
    RFD3 and RF3 resolve registry aliases themselves (`ckpt_path=rfd3` works).
    MPNN does not — its config takes a literal path and it dies with
    `checkpoint_path does not exist: solublempnn`, which aborts the campaign
    after RFD3 has already run.
    """
    from src.foundry_stages import resolve_checkpoint

    (tmp_path / "solublempnn_v_48_020.pt").write_bytes(b"")
    resolved = resolve_checkpoint("solublempnn", tmp_path)
    assert resolved.endswith("solublempnn_v_48_020.pt")
    assert Path(resolved).exists()


def test_an_explicit_checkpoint_path_passes_through(tmp_path):
    from src.foundry_stages import resolve_checkpoint

    real = tmp_path / "my.pt"
    real.write_bytes(b"")
    assert resolve_checkpoint(str(real), tmp_path) == str(real.resolve())


def test_an_unmatched_alias_is_passed_through_not_swallowed(tmp_path):
    from src.foundry_stages import resolve_checkpoint

    assert resolve_checkpoint("nonesuch", tmp_path) == "nonesuch"
