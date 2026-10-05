"""A long receiver chain costs O(cap) per call, not O(chain) (#2262).

Every call in an n-hop chain (`a.m().m()...`) was resolved on its own text,
which holds the whole receiver, so indexing one such file took O(n^2): a
20 KB file of `.m()` hops took minutes and gigabytes. Calls past
MAX_RECEIVER_CHAIN_HOPS stay unresolved; shorter chains resolve as before.

The same issue's related report: a deeply nested list literal re-expanded
every nested level as the collection walk reached it, also O(n^2).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from tree_sitter import Language, Parser

from codebase_rag import constants as cs
from codebase_rag.parsers.call_processor import _exceeds_receiver_chain_cap
from codebase_rag.parsers.call_resolver import CallResolver, _split_receiver_chain
from evals.cgr_graph import _capture

_CAP = cs.MAX_RECEIVER_CHAIN_HOPS


def test_split_stops_past_the_cap() -> None:
    at_cap = "a" + ".m()" * (_CAP - 1)
    assert _split_receiver_chain(at_cap) == ["a", *["m()"] * (_CAP - 1)]
    assert _split_receiver_chain(at_cap + ".m()") is None


def test_split_still_ignores_dots_inside_arguments() -> None:
    # Dots inside arguments are not hops, so they never count toward the cap.
    args = ".".join(["x"] * (_CAP * 2))
    assert _split_receiver_chain(f"a.m({args})") == ["a", f"m({args})"]


def _python_calls(source: str) -> list:
    import tree_sitter_python

    tree = Parser(Language(tree_sitter_python.language())).parse(source.encode())
    found = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "call":
            found.append(node)
        stack.extend(node.children)
    return found


@pytest.mark.parametrize(
    ("hops", "over_cap"),
    [(_CAP - 1, 0), (_CAP, 0), (_CAP + 1, 1), (_CAP + 10, 10)],
)
def test_guard_skips_only_the_calls_past_the_cap(hops: int, over_cap: int) -> None:
    calls = _python_calls("a" + ".m()" * hops)

    assert len(calls) == hops
    assert sum(_exceeds_receiver_chain_cap(c) for c in calls) == over_cap


def test_a_long_operator_spine_is_not_a_chain() -> None:
    (call,) = _python_calls("(" + " + ".join(["x"] * (_CAP * 20)) + ").m()")

    assert not _exceeds_receiver_chain_cap(call)


def test_a_huge_chain_resolves_a_bounded_number_of_calls(tmp_path: Path) -> None:
    hops = 5_000
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "mod.py").write_text(
        "def g():\n    return a" + ".m()" * hops + "\n", encoding="utf-8"
    )
    original = CallResolver.resolve_function_call
    seen: list[str] = []

    def counting(self: CallResolver, call_name: str, *args, **kwargs):
        seen.append(call_name)
        return original(self, call_name, *args, **kwargs)

    with patch.object(CallResolver, "resolve_function_call", counting):
        _capture(tmp_path, "proj")

    # One resolution per call up to the cap; none of the skipped 4,936. The
    # returned-reference pass looks the whole `return` expression up once,
    # which is linear, so at most one name may be long.
    assert len(seen) <= _CAP + 2, len(seen)
    assert sum(len(name) > 4 * (_CAP + 2) for name in seen) <= 1


def test_a_typed_chain_under_the_cap_still_resolves(tmp_path: Path) -> None:
    hops = 40
    (tmp_path / "m.go").write_text(
        "package p\n"
        "type Command struct{}\n"
        "func (c *Command) Root() *Command { return c }\n"
        "func (c *Command) Run() int { return 1 }\n"
        "func (c *Command) Use() int { return c" + ".Root()" * hops + ".Run() }\n",
        encoding="utf-8",
    )
    ingestor = _capture(tmp_path, "proj")
    calls = {
        (str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "CALLS"
    }

    assert ("proj.m.Command.Use", "proj.m.Command.Root") in calls
    assert ("proj.m.Command.Use", "proj.m.Command.Run") in calls


def test_a_typed_chain_past_the_cap_leaves_its_tail_unresolved(tmp_path: Path) -> None:
    hops = _CAP + 5
    (tmp_path / "m.go").write_text(
        "package p\n"
        "type Command struct{}\n"
        "func (c *Command) Root() *Command { return c }\n"
        "func (c *Command) Run() int { return 1 }\n"
        "func (c *Command) Use() int { return c" + ".Root()" * hops + ".Run() }\n",
        encoding="utf-8",
    )
    ingestor = _capture(tmp_path, "proj")
    calls = {
        (str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "CALLS"
    }

    assert ("proj.m.Command.Use", "proj.m.Command.Root") in calls
    assert ("proj.m.Command.Use", "proj.m.Command.Run") not in calls


def _references(ingestor) -> set[tuple[str, str]]:
    # A function stored in a literal is a dispatch table entry, recorded as a
    # call from the enclosing scope.
    return {(str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "CALLS"}


def test_handlers_at_every_level_of_a_nested_literal_are_referenced(
    tmp_path: Path,
) -> None:
    # Lists, tuples and dict values nested in each other: each level's
    # handler is still referenced once the outer expansion covers the inner
    # containers.
    (tmp_path / "t.py").write_text(
        "def h0():\n    pass\n\n\n"
        "def h1():\n    pass\n\n\n"
        "def h2():\n    pass\n\n\n"
        "def h3():\n    pass\n\n\n"
        "def table():\n"
        "    x = [h0, {'k': [h1, (h2, {'j': [h3]})]}]\n"
        "    return x\n",
        encoding="utf-8",
    )
    refs = _references(_capture(tmp_path, "proj"))

    for name in ("h0", "h1", "h2", "h3"):
        assert ("proj.t.table", f"proj.t.{name}") in refs, sorted(refs)


def test_a_deeply_nested_literal_is_expanded_once(tmp_path: Path) -> None:
    from codebase_rag.parsers import call_processor

    depth = 2_000
    (tmp_path / "t.py").write_text(
        "def h():\n    pass\n\n\n"
        "def table():\n    x = " + "[" * depth + "h" + "]" * depth + "\n    return x\n",
        encoding="utf-8",
    )
    original = call_processor._first_class_value_children
    visits = 0

    def counting(*args, **kwargs):
        nonlocal visits
        visits += 1
        return original(*args, **kwargs)

    with patch.object(call_processor, "_first_class_value_children", counting):
        refs = _references(_capture(tmp_path, "proj"))

    assert ("proj.t.table", "proj.t.h") in refs
    # Linear in the depth; re-expanding every level was depth^2 / 2.
    assert visits < 10 * depth, visits
