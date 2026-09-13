"""A class-only capture entry must not make the call walk skip a file
(issue #1837).

`_process_function_calls` skipped any cached file whose captures held no call
and no function. The premise -- "nothing to walk" -- is false twice over. A
MODULE-LEVEL reference (a dispatch table holding an imported handler, a bare
`x = handler`, a JSX element) is a real CALLS edge living under no capture
kind at all, and `process_calls_in_file` runs those passes ahead of its own
no-calls early return precisely so such a file is covered. A class is
positive evidence too: the Python decorator pass takes captured class nodes
as its targets.

A file with NO captures never reaches the cache, so it was walked; a file
whose only capture was a class had a truthy entry and was skipped. Adding an
unrelated class to a module silently dropped its module-level call edges.
This is a full-index bug, not only an incremental one.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

CALLS = cs.RelationshipType.CALLS.value

# The two modules differ ONLY by the presence of an unrelated class. Both hold
# the same module-level dispatch table referencing the same imported handler,
# so any difference in their edges is the skip and nothing else.
_FILES = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper():\n    return 1\n",
    "pkg/plain.py": 'from pkg.util import helper\n\nTABLE2 = {"h": helper}\n',
    "pkg/disp.py": (
        "from pkg.util import helper\n\n\n"
        "class Marker:\n    pass\n\n\n"
        'TABLE = {"h": helper}\n'
    ),
}


def _run(
    tmp_path: Path, files: dict[str, str]
) -> tuple[set[tuple[str, str]], list[Path], GraphUpdater]:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    parsers, queries = load_parsers()
    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    )

    # Record which files the walk actually reached. The edge assertions below
    # say what was emitted; this says whether the skip fired, which is the
    # only way the control can tell "walked and found nothing" from "skipped".
    # CallProcessor uses __slots__, so the spy goes on the class, not the
    # instance, and is removed again before the assertions run.
    walked: list[Path] = []
    processor = updater.factory.call_processor
    original = type(processor).process_calls_in_file

    def _spy(self: object, file_path: Path, *args: object, **kwargs: object) -> None:
        if self is processor:
            walked.append(file_path)
        return original(self, file_path, *args, **kwargs)

    with patch.object(type(processor), "process_calls_in_file", _spy):
        updater.run()

    calls = {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == CALLS
    }
    return calls, walked, updater


def test_class_only_file_still_emits_its_module_level_call(tmp_path: Path) -> None:
    calls, walked, updater = _run(tmp_path, _FILES)
    project = tmp_path.name
    helper = f"{project}.pkg.util.helper"

    # The control module, identical but for the class, has always worked. If
    # this one regresses the fixture is broken rather than the fix, so assert
    # it before the case under test.
    assert (f"{project}.pkg.plain", helper) in calls

    # The defect: the class-only module dropped its dispatch-table edge.
    assert (f"{project}.pkg.disp", helper) in calls

    # Pin the mechanism, not only the outcome: disp.py is cached under a
    # class-only entry (no call, no function), which is exactly the shape
    # that used to take the skip.
    disp_path = tmp_path / "pkg" / "disp.py"
    entry = updater.factory._func_class_captures_cache[disp_path]
    assert not entry.get(cs.CAPTURE_CALL)
    assert not entry.get(cs.CAPTURE_FUNCTION)
    assert entry.get(cs.CAPTURE_CLASS)
    assert disp_path in walked


def test_every_parsed_file_reaches_the_call_walk(tmp_path: Path) -> None:
    """The walk is uniform: no cached file is skipped on its captures.

    This replaces the "an empty file is still skipped" control, whose premise
    turned out to be unreachable. The populator stores a capture key only
    when the query matched it, and drops an entry with no keys at all, so no
    cache entry can record none of call/function/class -- a narrowed guard
    would have been dead code rather than a live optimisation (0 hits over
    105 real files). Asserting a state that cannot occur proves nothing, so
    the control asserts the property that actually holds and would catch a
    reintroduced capture-based skip of any shape.
    """
    files = dict(_FILES)
    # Shapes that a capture-based skip would be most tempted to discard:
    # a class-only module, and a module of literals with no captures at all.
    files["pkg/data.py"] = 'VALUES = [1, 2, 3]\nNAME = "constant"\n'
    files["pkg/marker.py"] = "class Only:\n    pass\n"
    _, walked, updater = _run(tmp_path, files)

    walked_set = set(walked)
    parsed = {fp for fp, _ in updater._parsed_files}
    assert parsed, "fixture parsed nothing; the assertion below would be vacuous"
    missed = sorted(fp.name for fp in parsed - walked_set)
    assert not missed, f"parsed but never walked: {missed}"


def test_class_only_file_emits_its_decorator_call(tmp_path: Path) -> None:
    """The skip also discarded the Python decorator pass, which reads captured
    CLASS nodes as its targets -- a class-only file is the input it exists for.
    """
    files = {
        "pkg/__init__.py": "",
        "pkg/reg.py": "def register(cls):\n    return cls\n",
        "pkg/models.py": (
            "from pkg.reg import register\n\n\n@register\nclass Model:\n    pass\n"
        ),
    }
    calls, walked, _ = _run(tmp_path, files)
    project = tmp_path.name

    assert (f"{project}.pkg.models", f"{project}.pkg.reg.register") in calls
    assert tmp_path / "pkg" / "models.py" in walked
