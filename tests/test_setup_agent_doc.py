"""Every command `SETUP_AGENT.md` hands to a user must actually parse.

This file exists because of one setup trial. The agent finished a working
install and then suggested two commands as the first real things to run:

    run_pipeline.py --workflow ppi --target RING1B ...   # --target is binder-only
    ask_corpus.py "..." --budget 1                       # no such flag

Both failed immediately. Neither was a bug in the code they named — they were
invented by pattern-matching: Phase 9 listed a `binder` command using
`--target` but no `ppi` command at all, and ground rule 2 said to recommend
`--budget` on "every metered command" while only `run_pipeline.py` has it.

That is a doc-vs-CLI drift, and prose is exactly where this kind of drift
survives review. A command that errors is worse than no suggestion: it is the
last thing a user sees from a setup that otherwise worked, and they cannot
tell a wrong suggestion from a broken install.

Nothing here runs a pipeline, spends money or touches a GPU — the parsers are
built and `parse_args` is called, nothing more.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import re
import shlex

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DOC = _ROOT / "SETUP_AGENT.md"

#: Scripts whose flag surface is inspectable. A script without a
#: `_build_parser` cannot be checked, so adding a command for one to the doc
#: should come with extracting its parser — see `test_documented_scripts_are_checkable`.
_PARSERS: dict[str, str] = {
    "run_pipeline.py": "scripts.run_pipeline",
    "ask_corpus.py": "scripts.ask_corpus",
    "run_skill.py": "scripts.run_skill",
}

#: Placeholders the doc uses for values the user substitutes.
_PLACEHOLDER_RE = re.compile(r"<[^>]+>")


def _phase(name: str, until: str) -> str:
    text = _DOC.read_text(encoding="utf-8")
    return text[text.index(name):text.index(until)]


def _commands(section: str) -> list[list[str]]:
    """Every `python scripts/<x>.py ...` invocation in `section`, tokenised.

    Backslash-continuations are joined first: the doc wraps long commands, and
    reading only the first physical line would check a prefix rather than the
    command.
    """
    flat = re.sub(r"\\\n\s*", " ", section)
    out = []
    for raw in re.findall(r"python (scripts/[a-z_]+\.py[^\n`)]*)", flat):
        parts = shlex.split(_PLACEHOLDER_RE.sub("PLACEHOLDER", raw.strip()))
        out.append(parts)
    return out


def _parser_for(script: str) -> argparse.ArgumentParser | None:
    import importlib

    mod_name = _PARSERS.get(script)
    if mod_name is None:
        return None
    mod = importlib.import_module(mod_name)
    return mod._build_parser()


_PHASE9 = _phase("### Phase 9 — Finish", "### If something fails")
_PHASE9_COMMANDS = _commands(_PHASE9)


def test_the_doc_still_suggests_commands_at_all():
    """A guard on the guard: if the section is renamed or the regex stops
    matching, every test below would pass by finding nothing."""
    assert len(_PHASE9_COMMANDS) >= 3, (
        f"only found {len(_PHASE9_COMMANDS)} commands in Phase 9 — the "
        f"extractor has probably gone blind: {_PHASE9_COMMANDS}")


@pytest.mark.parametrize("argv", _PHASE9_COMMANDS,
                         ids=lambda a: " ".join(a[:4]))
def test_every_phase_9_command_parses(argv):
    script, args = argv[0].split("/")[-1], argv[1:]
    parser = _parser_for(script)
    if parser is None:
        pytest.skip(f"{script} exposes no _build_parser")
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            parser.parse_args(args)
        except SystemExit:
            pytest.fail(f"{script} rejects a command the doc hands over:\n"
                        f"  {' '.join(argv)}\n{err.getvalue()}")


def test_target_is_not_offered_on_a_track_that_refuses_it():
    """The specific incident. `--target` is the binder track's entry point;
    `run_pipeline.py` errors on it for ppi and structure. A doc that shows it
    under the wrong `--workflow` is what produced the invented command."""
    for argv in _PHASE9_COMMANDS:
        if argv[0].split("/")[-1] != "run_pipeline.py" or "--target" not in argv:
            continue
        workflow = argv[argv.index("--workflow") + 1] if "--workflow" in argv else "ppi"
        assert workflow == "binder", (
            f"Phase 9 shows --target with --workflow {workflow}, which "
            f"run_pipeline.py refuses: {' '.join(argv)}")


def test_budget_is_only_offered_where_it_exists():
    """Ground rule 2 used to say "every metered command", and an agent duly
    added `--budget` to `ask_corpus.py`, which has never had it. `--budget` is
    per-PROJECT and cumulative, which is why it lives on the pipeline only."""
    for script, mod_name in _PARSERS.items():
        parser = _parser_for(script)
        flags = {opt for a in parser._actions for opt in a.option_strings}
        has_budget = "--budget" in flags
        assert has_budget == (script == "run_pipeline.py"), (
            f"{script} --budget present={has_budget}; only run_pipeline.py "
            f"should have it, and the doc's guidance is written to match")

    for argv in _PHASE9_COMMANDS:
        script = argv[0].split("/")[-1]
        if "--budget" in argv:
            assert script == "run_pipeline.py", (
                f"Phase 9 passes --budget to {script}, which has no such flag")


def test_documented_scripts_are_checkable():
    """A command for a script with no `_build_parser` silently skips above.

    Keeping this honest is the whole point: the two failures this file was
    written for were *unchecked*, not wrongly checked.
    """
    unchecked = sorted({c[0].split("/")[-1] for c in _PHASE9_COMMANDS}
                       - set(_PARSERS) - {"doctor.py", "quickstart.py",
                                          "warm_embedding_cache.py",
                                          "fetch_reference_data.py",
                                          "fetch_corpus.py",
                                          "setup_mcp_json.py",
                                          "generate_binder_report.py",
                                          "generate_ppi_report.py",
                                          "fetch_papers.py",
                                          "curate_papers.py",
                                          "launch_mcp.py",
                                          "launch_structure_tools.py"})
    assert not unchecked, (
        f"Phase 9 hands over {unchecked} but they expose no _build_parser, so "
        f"test_every_phase_9_command_parses skips them. Extract a "
        f"_build_parser() and add it to _PARSERS.")
