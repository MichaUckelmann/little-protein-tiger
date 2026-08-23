"""Regression tests for src/_path_resolve.py — the shared path-confinement
helper behind `src/skill_runner.py:_resolve` and
`src/structure_tools_server.py:_resolve`.

Covers the fix for the confinement bug: previously, ANY existing path was
returned verbatim with zero check that it was under `root` — an
arbitrary-file-read/overwrite primitive for any tool-call argument (e.g.
`write_file`) naming a path that happened to exist anywhere on the
filesystem. Now an existing-but-outside-root path falls through into the
same recovery machinery used for a non-existent path, instead of being
trusted as-is.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src._path_resolve import resolve


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    r = tmp_path / "project_root"
    r.mkdir()
    return r


# ---------------------------------------------------------------------------
# The bug fix: existing-but-outside-root paths are not trusted verbatim.
# ---------------------------------------------------------------------------


def test_existing_path_outside_root_is_not_returned_verbatim(tmp_path, root):
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("attacker-visible content")
    assert outside.exists()

    result = resolve(str(outside), root=root)

    assert result != str(outside)
    assert Path(result).resolve().is_relative_to(root.resolve())


def test_existing_absolute_path_outside_root_recovers_via_tail_walk(tmp_path, root):
    # Simulates a model-emitted absolute path like /app/data/structures/x.cif
    # where the tail ("data/structures/x.cif") happens to exist under root.
    (root / "data" / "structures").mkdir(parents=True)
    target = root / "data" / "structures" / "x.cif"
    target.write_text("real structure")

    # An "existing" path elsewhere on disk whose tail matches the real file.
    fake_container = tmp_path / "app"
    (fake_container / "data" / "structures").mkdir(parents=True)
    decoy = fake_container / "data" / "structures" / "x.cif"
    decoy.write_text("decoy content — should never be read")
    assert decoy.exists()

    result = resolve(str(decoy), root=root)

    assert Path(result) == target
    assert Path(result).resolve().is_relative_to(root.resolve())


# ---------------------------------------------------------------------------
# No regression on the common case: existing path already under root.
# ---------------------------------------------------------------------------


def test_existing_path_inside_root_fast_returns_unmodified(root):
    inside = root / "data.cif"
    inside.write_text("real")

    result = resolve(str(inside), root=root)

    assert result == str(inside)


def test_existing_relative_path_inside_root_fast_returns(root, monkeypatch):
    (root / "data").mkdir()
    (root / "data" / "y.cif").write_text("real")
    monkeypatch.chdir(root)

    result = resolve("data/y.cif", root=root)

    assert result == "data/y.cif"


# ---------------------------------------------------------------------------
# Existing recovery contract (non-existent absolute paths), per the
# docstring, must still work exactly as before.
# ---------------------------------------------------------------------------


def test_nonexistent_absolute_app_path_recovers_under_root(root):
    (root / "data" / "structures").mkdir(parents=True)
    target = root / "data" / "structures" / "x.cif"
    target.write_text("real structure")

    result = resolve("/app/data/structures/x.cif", root=root)

    assert Path(result) == target


def test_nonexistent_root_relative_path_recovers_under_root(root):
    (root / "data").mkdir()
    target = root / "data" / "x.cif"
    target.write_text("real structure")

    result = resolve("/data/x.cif", root=root)

    assert Path(result) == target


def test_basename_recovers_under_canonical_structures_dir(root):
    (root / "data" / "structures").mkdir(parents=True)
    target = root / "data" / "structures" / "orphan.cif"
    target.write_text("real structure")

    # A path whose directory doesn't match anything under root, but whose
    # basename matches a file in the canonical data/structures/ directory.
    result = resolve("/some/unrelated/path/orphan.cif", root=root)

    assert Path(result) == target


def test_nothing_matches_falls_back_to_root_rejoin(root):
    result = resolve("/totally/nonexistent/file.cif", root=root)

    assert result == str(root / "totally" / "nonexistent" / "file.cif")


def test_relative_nonexistent_path_falls_back_to_root_rejoin(root):
    result = resolve("some/relative/file.cif", root=root)

    assert result == str(root / "some" / "relative" / "file.cif")


# ---------------------------------------------------------------------------
# write_file-style integration: an attempted write to a path that resolves
# outside root instead lands under root — it cannot actually escape.
# Mirrors the exact pattern in skill_runner.py's "write_file" tool dispatch:
#   dest = Path(_resolve(input_dict["path"])); dest.parent.mkdir(...); dest.write_text(...)
# ---------------------------------------------------------------------------


def test_write_file_pattern_cannot_escape_root_via_existing_outside_path(tmp_path, root):
    # An attacker/model-controlled path argument names a file that already
    # exists outside root (e.g. a sensitive file the process can see).
    outside_target = tmp_path / "etc_style_target.conf"
    outside_target.write_text("original untouched content")

    dest = Path(resolve(str(outside_target), root=root))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("attacker-controlled overwrite content", encoding="utf-8")

    # The outside file must be untouched...
    assert outside_target.read_text() == "original untouched content"
    # ...and the write must have landed under root instead.
    assert dest.resolve().is_relative_to(root.resolve())
    assert dest.read_text() == "attacker-controlled overwrite content"


# ---------------------------------------------------------------------------
# Both call sites delegate to the shared implementation with their own root.
# ---------------------------------------------------------------------------


def test_skill_runner_resolve_delegates_to_shared_module(tmp_path, monkeypatch):
    import src.skill_runner as skill_runner

    fake_root = tmp_path / "fake_root"
    fake_root.mkdir()
    monkeypatch.setattr(skill_runner, "_ROOT", fake_root)

    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    result = skill_runner._resolve(str(outside))

    assert result != str(outside)
    assert Path(result).resolve().is_relative_to(fake_root.resolve())


def test_structure_tools_server_resolve_delegates_to_shared_module(tmp_path, monkeypatch):
    import src.structure_tools_server as structure_tools_server

    fake_root = tmp_path / "fake_root"
    fake_root.mkdir()
    monkeypatch.setattr(structure_tools_server, "ROOT", fake_root)

    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    result = structure_tools_server._resolve(str(outside))

    assert result != str(outside)
    assert Path(result).resolve().is_relative_to(fake_root.resolve())
