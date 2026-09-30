# A Python function whose qn carries a duplicate-variant suffix (an earlier
# same-named def claims the natural qn, the later one registers as
# `command@9`) calls its own nested `decorator`. The enclosing-scope walk
# probed `command@9.decorator`, which never exists (the nested registers
# under the NATURAL qn `command.decorator`), so resolution mis-bound to a
# sibling's `argument.decorator`. The walk must also probe the stripped scope.
# The redefinitions are plain on purpose: `@typing.overload` stubs no longer
# take the natural qn (issue #2590), so they would not reach this path.
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
        "def argument(name):\n"
        "    def decorator(f):\n"
        "        return f\n"
        "    return decorator\n"
        "def command(name: str) -> int: ...\n"
        "\n"
        "def command(name: None) -> int: ...\n"
        "\n"
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
