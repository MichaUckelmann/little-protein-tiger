"""Shared path-recovery logic for `_resolve()` in `skill_runner.py` and
`structure_tools_server.py`.

Both modules gate every file-path-taking MCP/skill tool through a `_resolve()`
call that is supposed to confine a (possibly LLM-emitted) path to the
project root, with recovery for model-emitted absolute paths that point
outside the repo (e.g. `/app/data/structures/x.cif`). Previously each module
carried its own copy of this logic; they are unified here so there is a
single, tested implementation, parameterized on `root` instead of a
module-global constant.
"""
from __future__ import annotations

import sys
from pathlib import Path


def resolve(file_path: str, *, root: Path) -> str:
    """Resolve a file path against `root`, with recovery for model-emitted
    absolute paths that point outside the repo.

    Resolution order:
    1. If the path exists as-is (relative or absolute) AND is genuinely
       under `root`, return it unmodified. An existing path that is NOT
       under `root` is not trusted — it falls through to the same
       recovery machinery used for a non-existent path below, rather than
       being returned verbatim (that used to be a path-confinement bug:
       any existing path anywhere on the filesystem was returned as-is).
    2. If it's absolute but doesn't exist (or exists outside root), walk
       the path components left-to-right and try `root / <suffix>` for
       each successive tail. This recovers `/app/data/structures/x.cif`,
       `/workspace/code/data/x.cif`, and root-relative `/data/x.cif`
       (Linux-priors the model emits even when the prompt provided a
       relative path).
    3. As a last resort, try the basename under `data/structures/` (the
       canonical structures directory) and under `root`.
    4. If nothing matches, fall back to the original `root / stripped`
       behaviour so the caller sees a consistent error path.

    The leading-slash strip in the relative branch is also critical on
    Windows: pathlib's `/` operator interprets a slash-prefixed path as
    drive-relative, producing `C:\\data\\...` instead of `root\\data\\...`.
    """
    root = Path(root)
    p = Path(file_path)
    if p.exists():
        try:
            if p.resolve().is_relative_to(root.resolve()):
                return str(p)
        except (OSError, ValueError):
            pass
        # Existing but outside root: don't trust it — fall through to the
        # same recovery logic used for "doesn't exist" paths below.

    is_real_absolute = p.is_absolute() and (sys.platform != "win32" or bool(p.drive))
    if is_real_absolute:
        recovered = _recover_under_root(p, root)
        if recovered is not None:
            return str(recovered)
        # Fall through to the strip-and-rejoin branch — same effect as
        # the relative case, so the caller sees a path under root in
        # the error message rather than a wholly external absolute path.
    return str(root / Path(file_path.lstrip("/\\")))


def _recover_under_root(p: Path, root: Path) -> "Path | None":
    """Find an existing file under `root` whose tail matches `p`.

    Strategy: drop leading path components one at a time and test
    whether the remaining suffix exists under `root`. Then try the
    basename under common canonical directories. Returns `None` if no
    clean match is found.
    """
    parts = list(p.parts)
    # Drop the root marker ("/", "\\", or "C:\\") so we can join cleanly.
    if parts and (parts[0] in ("/", "\\") or parts[0].endswith(":\\") or parts[0].endswith(":/")):
        parts = parts[1:]
    for i in range(len(parts)):
        candidate = root.joinpath(*parts[i:])
        if candidate.exists():
            return candidate
    basename = p.name
    for canonical in (root / "data" / "structures" / basename, root / basename):
        if canonical.exists():
            return canonical
    return None
