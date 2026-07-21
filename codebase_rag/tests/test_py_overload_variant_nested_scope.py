# A Python function whose qn carries a duplicate-variant suffix (click's
# `command` -- @t.overload stubs claim the natural qn, the REAL def registers as
# `command@168`) calls its own nested function (`return decorator(func)`). The
# enclosing-scope walk probed `command@168.decorator`, which never exists (the
# def pass registers the nested under the NATURAL qn `command.decorator`), so
# resolution fell to the module trie and mis-bound to an alphabetically-earlier
# sibling's nested (`argument.decorator`) -- a false edge AND a false-dead
# `command.decorator`. The walk must also probe the variant-stripped scope.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def test_variant_caller_nested_call_binds_to_own_nested(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "pyvar"
    root.mkdir(parents=True)
    (root / "decorators.py").write_text(
        "import typing as t\n"
        "def argument(name):\n"
        "    def decorator(f):\n"
        "        return f\n"
        "    return decorator\n"
        "@t.overload\n"
        "def command(name: str) -> int: ...\n"
        "@t.overload\n"
        "def command(name: None) -> int: ...\n"
        "def command(name=None):\n"
        "    def decorator(f):\n"
        "        return f\n"
        "    if callable(name):\n"
        "        return decorator(name)\n"
        "    return decorator\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing=None)
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(
        ".command" in f and t.endswith(".command.decorator") for f, t in calls
    ), sorted(calls)
    assert not any(
        ".command" in f and t.endswith(".argument.decorator") for f, t in calls
    ), "command's nested call mis-bound to the sibling's decorator"
