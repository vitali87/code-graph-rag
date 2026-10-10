"""Every member of an ExceptionGroup is analysed, beside the group's own stack.

`parse_python_traceback` stripped the group's box margin and kept the lines
after the last traceback header, so `explain_traceback` and
`rank_root_causes` reported only the last sub-exception, as if it were the
whole failure. A `KeyError` beside a `ValueError`, and the stack that raised
the group, were dropped without a word (issue #3235).
"""

from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.crash_correlation import (
    CYPHER_CRASH_CALLS,
    ParsedTraceback,
    explain_traceback,
    parse_python_traceback,
    rank_root_causes,
)
from codebase_rag.cypher_queries import CYPHER_TRACE_CALLABLES
from codebase_rag.graph_query import QueryFn
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, ResultRow
from codebase_rag.utils.path_utils import derive_project_name
from evals.cgr_graph import _StatefulIngestor

# The issue's reproduction: two checks fail, and `validate` raises both.
APP = """\
def check_price(item):
    return item["price"] > 0


def check_name(item):
    if not item.get("name"):
        raise ValueError("item has no name")


def validate(item):
    errors = []
    for check in (check_price, check_name):
        try:
            check(item)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise ExceptionGroup("invalid item", errors)


if __name__ == "__main__":
    validate({"name": ""})
"""

TASKS = """\
import asyncio


async def total(items):
    return sum(i["price"] for i in items)


async def name(item):
    raise ValueError("item has no name")


async def main():
    async with asyncio.TaskGroup() as tg:
        tg.create_task(total([{}]))
        tg.create_task(name({}))


asyncio.run(main())
"""

NESTED = """\
def a():
    raise KeyError("a")


def b():
    raise ValueError("b")


def c():
    raise TypeError("c")


def collect(*fns):
    errors = []
    for fn in fns:
        try:
            fn()
        except Exception as exc:
            errors.append(exc)
    return errors


def inner():
    raise ExceptionGroup("inner", collect(a, b))


def outer():
    raise ExceptionGroup("outer", collect(inner, c))


outer()
"""


def _run(root: Path, name: str, source: str) -> str:
    (root / name).write_text(source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-B", name],
        cwd=root,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )
    assert result.returncode == 1, result
    return result.stderr


type _Outline = tuple[str, str, list[str], list[_Outline]]


def _outline(parsed: ParsedTraceback) -> _Outline:
    """(type, message, frame names, member outlines) of a parsed traceback."""
    return (
        parsed.exception_type,
        parsed.exception_message,
        [frame.qualname for frame in parsed.frames],
        [_outline(member) for member in parsed.members],
    )


# --- parsing -----------------------------------------------------------------


def test_a_group_keeps_its_own_stack_and_every_member(temp_repo: Path) -> None:
    parsed = parse_python_traceback(_run(temp_repo, "app.py", APP))

    assert _outline(parsed) == (
        "ExceptionGroup",
        "invalid item (2 sub-exceptions)",
        ["<module>", "validate"],
        [
            ("KeyError", "'price'", ["validate", "check_price"], []),
            ("ValueError", "item has no name", ["validate", "check_name"], []),
        ],
    )
    assert [frame.line for frame in parsed.frames] == [22, 18]
    assert [frame.line for frame in parsed.members[0].frames] == [14, 2]
    assert parsed.omitted_members == 0


def test_a_task_group_lists_each_failed_task(temp_repo: Path) -> None:
    parsed = parse_python_traceback(_run(temp_repo, "tasks.py", TASKS))

    assert parsed.exception_type == "ExceptionGroup"
    assert parsed.exception_message == (
        "unhandled errors in a TaskGroup (2 sub-exceptions)"
    )
    # The group's stack runs through asyncio to the `async with` in main.
    names = [frame.qualname for frame in parsed.frames]
    assert names[0] == "<module>"
    assert "main" in names
    assert [_outline(member) for member in parsed.members] == [
        ("KeyError", "'price'", ["total", "<genexpr>"], []),
        ("ValueError", "item has no name", ["name"], []),
    ]


def test_a_nested_group_is_a_member_with_members(temp_repo: Path) -> None:
    parsed = parse_python_traceback(_run(temp_repo, "nested.py", NESTED))

    assert _outline(parsed) == (
        "ExceptionGroup",
        "outer (2 sub-exceptions)",
        ["<module>", "outer"],
        [
            (
                "ExceptionGroup",
                "inner (2 sub-exceptions)",
                ["collect", "inner"],
                [
                    ("KeyError", "'a'", ["collect", "a"], []),
                    ("ValueError", "b", ["collect", "b"], []),
                ],
            ),
            ("TypeError", "c", ["collect", "c"], []),
        ],
    )


def _format(exc: BaseException, **limits: int) -> str:
    return "".join(traceback.TracebackException.from_exception(exc, **limits).format())


def _raised(
    make: type[BaseException], *args: str | list[BaseException]
) -> BaseException:
    try:
        raise make(*args)
    except BaseException as exc:
        return exc


def test_members_the_rendering_left_out_are_counted() -> None:
    errors = [_raised(ValueError, str(i)) for i in range(5)]
    group = _raised(ExceptionGroup, "many", errors)

    parsed = parse_python_traceback(_format(group, max_group_width=3))

    assert [member.exception_message for member in parsed.members] == ["0", "1", "2"]
    assert parsed.omitted_members == 2


def test_a_group_nested_past_the_depth_limit_is_counted() -> None:
    deepest = _raised(ExceptionGroup, "deepest", [_raised(KeyError, "k")])
    middle = _raised(ExceptionGroup, "middle", [deepest, _raised(ValueError, "v")])
    top = _raised(ExceptionGroup, "top", [middle])

    parsed = parse_python_traceback(_format(top, max_group_depth=2))

    (shown,) = parsed.members
    assert shown.exception_message == "middle (2 sub-exceptions)"
    assert [member.exception_type for member in shown.members] == ["ValueError"]
    assert shown.omitted_members == 1


def test_a_member_that_is_a_chain_keeps_its_propagated_section() -> None:
    def wrapped() -> BaseException:
        try:
            try:
                {}["missing"]
            except KeyError as cause:
                raise ValueError("wrapped") from cause
        except ValueError as exc:
            return exc
        raise AssertionError

    group = _raised(ExceptionGroup, "one", [wrapped(), _raised(TypeError, "t")])

    parsed = parse_python_traceback(_format(group))

    first, second = parsed.members
    assert (first.exception_type, first.exception_message) == ("ValueError", "wrapped")
    assert [frame.qualname for frame in first.frames] == ["wrapped"]
    assert second.exception_type == "TypeError"


def test_a_group_raised_while_handling_another_error_is_the_failure() -> None:
    def failing() -> None:
        try:
            {}["missing"]
        except KeyError:
            raise ExceptionGroup("late", [_raised(ValueError, "v")]) from None

    def chained() -> None:
        try:
            {}["first"]
        except KeyError:
            failing()

    try:
        chained()
    except ExceptionGroup:
        text = traceback.format_exc()

    parsed = parse_python_traceback(text)

    assert parsed.exception_type == "ExceptionGroup"
    assert [frame.qualname for frame in parsed.frames] == [
        "test_a_group_raised_while_handling_another_error_is_the_failure",
        "chained",
        "failing",
    ]
    assert [member.exception_type for member in parsed.members] == ["ValueError"]


def test_an_error_raised_while_handling_a_group_has_no_members() -> None:
    # Negative: the group is only context; the propagated failure is plain.
    def handler() -> None:
        try:
            raise ExceptionGroup("first", [_raised(ValueError, "v")])
        except ExceptionGroup:
            raise RuntimeError("cleanup failed")  # noqa: B904

    try:
        handler()
    except RuntimeError:
        text = traceback.format_exc()

    parsed = parse_python_traceback(text)

    assert (parsed.exception_type, parsed.exception_message) == (
        "RuntimeError",
        "cleanup failed",
    )
    assert parsed.members == ()
    assert parsed.omitted_members == 0


def test_one_member_pasted_alone_still_parses(temp_repo: Path) -> None:
    # Negative: a member's box copied without the group around it keeps its
    # margin, and parses as that member's own traceback.
    text = _run(temp_repo, "app.py", APP)
    lines = text.splitlines()
    first = lines.index("    +---------------- 2 ----------------") + 1
    last = lines.index("    +------------------------------------")

    parsed = parse_python_traceback("\n".join(lines[first:last]))

    assert _outline(parsed) == (
        "ValueError",
        "item has no name",
        ["validate", "check_name"],
        [],
    )


def test_a_separator_without_its_group_does_not_crash() -> None:
    # Negative: a fragment that starts at a later member's separator.
    text = (
        "    +---------------- 2 ----------------\n"
        "    | Traceback (most recent call last):\n"
        '    |   File "/app/a.py", line 7, in check_name\n'
        "    | ValueError: item has no name\n"
        "    +------------------------------------\n"
    )

    parsed = parse_python_traceback(text)

    # The group's own lines were not pasted, so nothing of the member is
    # mistaken for them.
    assert (parsed.exception_type, parsed.frames) == ("", ())
    (member,) = parsed.members
    assert (member.exception_type, member.exception_message) == (
        "ValueError",
        "item has no name",
    )


def test_a_plain_traceback_has_no_members() -> None:
    # Negative.
    text = (
        "Traceback (most recent call last):\n"
        '  File "/app/main.py", line 3, in <module>\n'
        "    run()\n"
        '  File "/app/main.py", line 1, in run\n'
        "    raise KeyError('k')\n"
        "KeyError: 'k'\n"
    )

    parsed = parse_python_traceback(text)

    assert (parsed.exception_type, parsed.members) == ("KeyError", ())
    assert [frame.qualname for frame in parsed.frames] == ["<module>", "run"]


def test_a_source_line_starting_with_a_bar_is_not_a_group_margin() -> None:
    # Negative: outside a group's box, `| c.missing` is a quoted source line,
    # and stripping a margin off it read `c.missing` as the exception.
    text = (
        "Traceback (most recent call last):\n"
        '  File "/app/flags.py", line 9, in <module>\n'
        "    flags(C())\n"
        '  File "/app/flags.py", line 6, in flags\n'
        "    | c.missing\n"
        "      ^^^^^^^^^\n"
        "AttributeError: 'C' object has no attribute 'missing'\n"
    )

    parsed = parse_python_traceback(text)

    assert parsed.exception_type == "AttributeError"
    assert [frame.qualname for frame in parsed.frames] == ["<module>", "flags"]


# --- explain_traceback and rank_root_causes -----------------------------------


def _graph(root: Path) -> tuple[str, _StatefulIngestor]:
    project = derive_project_name(root)
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
    ).run(force=True)
    return project, store


def _fetch_all(store: _StatefulIngestor) -> QueryFn:
    """The two crash queries, answered from the indexed graph."""
    callable_labels = {
        cs.NodeLabel.FUNCTION.value,
        cs.NodeLabel.METHOD.value,
        cs.NodeLabel.MODULE.value,
    }
    calls = cs.RelationshipType.CALLS.value

    def fetch_all(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        if query == CYPHER_TRACE_CALLABLES:
            return [
                {
                    cs.KEY_LABEL: label,
                    cs.KEY_QUALIFIED_NAME: props.get(cs.KEY_QUALIFIED_NAME),
                    cs.KEY_PATH: props.get(cs.KEY_PATH),
                    cs.KEY_START_LINE: props.get(cs.KEY_START_LINE),
                    cs.KEY_END_LINE: props.get(cs.KEY_END_LINE),
                }
                for (label, _key), props in store.nodes.items()
                if label in callable_labels
            ]
        if query == CYPHER_CRASH_CALLS:
            return [
                {"from_qn": source, "to_qn": target}
                for (_sl, source, rel, _tl, target, _site) in store.edge_props
                if rel == calls
            ]
        return []

    return fetch_all


def _short(qn: str | None) -> str | None:
    return qn.rsplit(".", 1)[-1] if qn else qn


def test_explain_reports_the_group_and_each_member(temp_repo: Path) -> None:
    text = _run(temp_repo, "app.py", APP)
    project, store = _graph(temp_repo)

    report = explain_traceback(_fetch_all(store), project, temp_repo, text)

    assert (report.exception_type, report.exception_message) == (
        "ExceptionGroup",
        "invalid item (2 sub-exceptions)",
    )
    assert [_short(frame.qualified_name) for frame in report.frames] == [
        "app",
        "validate",
    ]
    assert [
        (
            member.exception_type,
            [_short(frame.qualified_name) for frame in member.frames],
            member.resolution.rate,
        )
        for member in report.members
    ] == [
        ("KeyError", ["validate", "check_price"], 1.0),
        ("ValueError", ["validate", "check_name"], 1.0),
    ]
    assert report.note is not None
    assert "2 sub-exceptions" in report.note
    assert "members" in report.note
    assert all(member.note is None for member in report.members)


def test_rank_anchors_each_member_on_its_own_failure(temp_repo: Path) -> None:
    text = _run(temp_repo, "app.py", APP)
    project, store = _graph(temp_repo)

    report = rank_root_causes(_fetch_all(store), project, temp_repo, text)

    # The group itself failed where it was raised; each member where it was.
    assert _short(report.failing) == "validate"
    assert [
        (member.exception_type, _short(member.failing), member.anchor_is_crash_site)
        for member in report.members
    ] == [
        ("KeyError", "check_price", True),
        ("ValueError", "check_name", True),
    ]
    for member in report.members:
        assert [_short(c.qualified_name) for c in member.candidates][0] == "validate"
        assert member.note is None
    assert report.note is not None
    assert "under members" in report.note


def test_members_from_another_checkout_resolve_under_the_inferred_root(
    temp_repo: Path,
) -> None:
    # The root is inferred from every frame the traceback prints, members'
    # included, so a member resolves as the group's frames do.
    text = _run(temp_repo, "app.py", APP).replace(
        temp_repo.as_posix(), "/home/runner/work/eg/eg"
    )
    project, store = _graph(temp_repo)

    report = explain_traceback(_fetch_all(store), project, temp_repo, text)

    assert report.inferred_root == "/home/runner/work/eg/eg/"
    assert [member.resolution.rate for member in report.members] == [1.0, 1.0]


def test_a_logged_group_resolves_its_members_from_another_checkout(
    temp_repo: Path,
) -> None:
    # A group printed without being raised has no stack of its own, so only
    # its members' frames can name the checkout root they were recorded under.
    logged = APP.replace(
        '        raise ExceptionGroup("invalid item", errors)',
        "        import sys, traceback\n"
        '        traceback.print_exception(ExceptionGroup("invalid item", errors))\n'
        "        sys.exit(1)",
    )
    text = _run(temp_repo, "app.py", logged).replace(
        temp_repo.as_posix(), "/home/runner/work/eg/eg"
    )
    project, store = _graph(temp_repo)

    report = explain_traceback(_fetch_all(store), project, temp_repo, text)

    assert report.exception_type == "ExceptionGroup"
    assert report.frames == ()
    assert [member.resolution.rate for member in report.members] == [1.0, 1.0]


def test_the_note_counts_members_the_traceback_left_out(tmp_path: Path) -> None:
    errors = [_raised(ValueError, str(i)) for i in range(3)]
    text = _format(_raised(ExceptionGroup, "many", errors), max_group_width=1)

    report = explain_traceback(lambda _q, _p=None: [], "p", tmp_path, text)

    assert report.omitted_members == 2
    assert report.note is not None
    assert "ExceptionGroup with 1 sub-exception:" in report.note
    assert "left out 2 more" in report.note
    # The group's own frames lie outside the checkout, and that is said too.
    assert report.note.startswith("0 of 1 frames resolved")


def test_a_group_whose_only_member_was_cut_still_says_so(tmp_path: Path) -> None:
    inner = _raised(ExceptionGroup, "inner", [_raised(KeyError, "k")])
    text = _format(_raised(ExceptionGroup, "outer", [inner]), max_group_depth=1)

    report = explain_traceback(lambda _q, _p=None: [], "p", tmp_path, text)

    assert (report.members, report.omitted_members) == ((), 1)
    assert report.note is not None
    assert "left out 1 more" in report.note


def test_a_plain_traceback_reports_no_members(temp_repo: Path) -> None:
    # Negative: no group, no members, and no group note.
    text = _run(
        temp_repo, "app.py", APP.replace('validate({"name": ""})', "check_price({})")
    )
    project, store = _graph(temp_repo)
    fetch_all = _fetch_all(store)

    explained = explain_traceback(fetch_all, project, temp_repo, text)
    ranked = rank_root_causes(fetch_all, project, temp_repo, text)

    assert explained.exception_type == "KeyError"
    assert explained.members == ()
    assert explained.note is None
    assert _short(ranked.failing) == "check_price"
    assert ranked.members == ()


# --- MCP ----------------------------------------------------------------------


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _registry(root: Path) -> MCPToolsRegistry:
    _project, store = _graph(root)
    ingestor = MagicMock()
    ingestor.fetch_all = _fetch_all(store)
    return MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )


@pytest.mark.anyio
async def test_mcp_explain_traceback_lists_the_members(temp_repo: Path) -> None:
    text = _run(temp_repo, "app.py", APP)

    payload = await _registry(temp_repo).explain_traceback(text)

    json.dumps(payload)
    assert payload["exception_type"] == "ExceptionGroup"
    assert [frame["name"] for frame in payload["frames"]] == ["<module>", "validate"]
    assert [
        (member["exception_type"], [frame["name"] for frame in member["frames"]])
        for member in payload["members"]
    ] == [
        ("KeyError", ["validate", "check_price"]),
        ("ValueError", ["validate", "check_name"]),
    ]
    assert payload["omitted_members"] == 0
    assert payload["members"][0]["resolution"]["rate"] == 1.0


@pytest.mark.anyio
async def test_mcp_rank_root_causes_ranks_each_member(temp_repo: Path) -> None:
    text = _run(temp_repo, "app.py", APP)

    payload = await _registry(temp_repo).rank_root_causes(text)

    json.dumps(payload)
    assert [
        (member["exception_type"], _short(member["failing"]))
        for member in payload["members"]
    ] == [("KeyError", "check_price"), ("ValueError", "check_name")]
    assert payload["members"][0]["candidates"]
