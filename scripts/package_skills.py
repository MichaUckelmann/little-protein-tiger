#!/usr/bin/env python3
"""Regenerate the packaged `skills/<name>.zip` artifacts from their live SKILL.md dirs.

Each `skills/<name>/` directory whose SKILL.md is the system prompt for that skill
gets zipped into `skills/<name>.zip`, matching the packaging convention already used
by the existing zips (verified against `chimerax-visualization.zip`): every file in
the directory is stored under a top-level `<name>/` path inside the archive (i.e. the
zip root is `skills/`, not the skill directory itself), and an explicit directory
entry for `<name>/` is written first.

`.zip` files are packaged artifacts, not hand-edited — see CLAUDE.md's "Skill
execution model" section. Directories with no SKILL.md (nothing ready to package
yet, e.g. work-in-progress skills) are skipped.

A `skills/<name>.zip` with NO `skills/<name>/` behind it is an ERROR, not a
skip: it is a retired skill still shipping an installable prompt (the shape
`protein-design-script.zip` would have taken after step 3 of
LEGACY_RETIREMENT_SCOPE.md). This script names them and exits non-zero;
`tests/test_release_fixes.py` pins the same rule.

Usage:
    python scripts/package_skills.py
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def package_skill(skill_dir: Path) -> Path:
    """Zip skill_dir's contents into skills/<name>.zip, overwriting any existing zip."""
    name = skill_dir.name
    zip_path = SKILLS_DIR / f"{name}.zip"

    files = sorted(p for p in skill_dir.rglob("*") if p.is_file())

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Explicit directory entry first, matching the existing packaged zips.
        zf.writestr(zipfile.ZipInfo(f"{name}/"), b"")
        for f in files:
            arcname = f"{name}/{f.relative_to(skill_dir).as_posix()}"
            zf.write(f, arcname)

    return zip_path


def main() -> int:
    if not SKILLS_DIR.is_dir():
        print(f"error: {SKILLS_DIR} does not exist", file=sys.stderr)
        return 1

    packaged = []
    skipped = []
    for skill_dir in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        if not (skill_dir / "SKILL.md").is_file():
            skipped.append(skill_dir.name)
            continue
        zip_path = package_skill(skill_dir)
        packaged.append(zip_path.name)

    orphans = sorted(z.name for z in SKILLS_DIR.glob("*.zip")
                     if not (SKILLS_DIR / z.stem / "SKILL.md").is_file())

    for name in packaged:
        print(f"packaged: {name}")
    for name in skipped:
        print(f"skipped (no SKILL.md): {name}")
    for name in orphans:
        print(f"orphan (no skills/{name[:-4]}/SKILL.md — delete it): {name}",
              file=sys.stderr)

    print(f"\n{len(packaged)} skill(s) packaged, {len(skipped)} skipped.")
    if orphans:
        print(f"error: {len(orphans)} orphaned zip(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
