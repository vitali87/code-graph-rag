"""`_parsed_files` holds one entry per file, and every parsed file is in it.

Issue #1784: `register_parsed_file` — the only DEDUPLICATING appender — had
no callers. It was added for #1028 (a file parsed outside `run()` must join
the call pass's iteration set, or its outgoing CALLS edges are never emitted)
and left callerless by #1524, which moved the watcher onto `reingest`.

Investigating it turned up that both properties still hold, but not for the
reason the dead function implied:

* the live appender (`_process_single_file`) appends UNCONDITIONALLY, so it
  can duplicate — verified by calling it directly, which does produce a
  second entry;
* no production path does that, because every re-parse route strips the old
  entry first (`remove_file_from_state` on the incremental path,
  `_reingest_delete` under `reingest`).

So the invariant is maintained by remove-then-append, and the deleted
function was a false signal about where it comes from. These tests pin the
invariant to the mechanism that actually provides it, so removing the
removal fails here rather than silently growing the list on every save.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

CALLS = cs.RelationshipType.CALLS.value

_FILES = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper():\n    return 1\n",
    "pkg/mod.py": "from pkg.util import helper\n\n\ndef go():\n    return helper()\n",
}


def _build(tmp_path: Path) -> tuple[GraphUpdater, MagicMock]:
    for rel, content in _FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    parsers, queries = load_parsers()
    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    )
    updater.run()
    return updater, mock


def _counts(updater: GraphUpdater) -> Counter[Path]:
    return Counter(fp for fp, _ in updater._parsed_files)


def test_repeated_reingest_does_not_duplicate_entries(tmp_path: Path) -> None:
    """A retained updater (the MCP `_live_updater`, a watcher) reingests the
    same file on every save. Each one must replace its entry, not add one."""
    updater, _ = _build(tmp_path)
    target = tmp_path / "pkg" / "mod.py"
    assert _counts(updater)[target] == 1

    for i in range(3):
        target.write_text(
            f"from pkg.util import helper\n\n\ndef go():\n    return helper()  # {i}\n",
            encoding="utf-8",
        )
        updater.reingest((target,))
        assert _counts(updater)[target] == 1, f"duplicated after reingest {i + 1}"

    duplicated = {fp.name: n for fp, n in _counts(updater).items() if n > 1}
    assert not duplicated, f"duplicate _parsed_files entries: {duplicated}"


def test_the_appender_itself_does_not_deduplicate(tmp_path: Path) -> None:
    """Pin the MECHANISM, not just the outcome.

    The test above would pass just as well if the appender deduplicated, so
    it cannot show WHERE the invariant comes from. Calling the appender
    without the preceding removal must duplicate — that is what makes the
    removal load-bearing rather than incidental, and what a future
    "simplification" of `remove_file_from_state` would break.
    """
    updater, _ = _build(tmp_path)
    target = tmp_path / "pkg" / "mod.py"
    assert _counts(updater)[target] == 1

    updater._process_single_file(target)
    assert _counts(updater)[target] == 2, (
        "the appender deduplicates after all; if this is now intended, the "
        "comment in remove_file_from_state naming the removal as the source "
        "of the invariant is stale"
    )


def test_removal_then_reparse_restores_exactly_one_entry(tmp_path: Path) -> None:
    """The remove-then-append pairing, exercised directly."""
    updater, _ = _build(tmp_path)
    target = tmp_path / "pkg" / "mod.py"

    updater.remove_file_from_state(target)
    assert _counts(updater)[target] == 0

    updater._process_single_file(target)
    assert _counts(updater)[target] == 1


def test_a_file_created_after_run_still_emits_its_calls(tmp_path: Path) -> None:
    """The #1028 guarantee `register_parsed_file` was written to provide.

    It must survive the function's removal: a file created after `run()`
    joins the iteration set and its outgoing CALLS edges are emitted.
    """
    updater, mock = _build(tmp_path)
    fresh = tmp_path / "pkg" / "fresh.py"
    fresh.write_text(
        "from pkg.util import helper\n\n\ndef go():\n    return helper()\n",
        encoding="utf-8",
    )
    mock.reset_mock()
    updater.reingest((fresh,))

    assert _counts(updater)[fresh] == 1, "created file never joined _parsed_files"

    edges = {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == CALLS
    }
    project = tmp_path.name
    assert (f"{project}.pkg.fresh.go", f"{project}.pkg.util.helper") in edges


def test_the_dead_appender_is_gone(tmp_path: Path) -> None:
    """It never ran, and it read as the source of an invariant it did not
    provide. If it comes back, it needs a caller and a reason."""
    assert not hasattr(GraphUpdater, "register_parsed_file")


@pytest.mark.parametrize("rel", ["pkg/mod.py", "pkg/util.py"])
def test_every_parsed_file_appears_exactly_once_after_a_full_run(
    tmp_path: Path, rel: str
) -> None:
    updater, _ = _build(tmp_path)
    assert _counts(updater)[tmp_path / rel] == 1
