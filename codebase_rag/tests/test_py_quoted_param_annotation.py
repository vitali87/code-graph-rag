"""A quoted (forward-reference) parameter annotation types the parameter.

`def use(self, n: "Node")` stored the annotation as written, quotes and all,
so the receiver was typed `"Node"`, which names no class: `n.weight()` had no
CALLS edge while the bare `n: Node` had one. fastapi's
`APIRouter._contains_router(self, router: "APIRouter", ...)` lost its only
outside caller, `router._contains_router(self)`, and was reported dead
(issue #2837).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.parsers.py.forward_refs import unquote_forward_refs
from codebase_rag.tests.test_js_ts_separate_export_class_members import (
    _dead,
    _index,
)

# Two classes define `weight`, so only the parameter's type can pick one.
_CLASSES = """\
from typing import Literal, Optional


class Node:
    def weight(self) -> int:
        return 1


class Other:
    def weight(self) -> int:
        return 2


"""


def _callees(tmp_path: Path, signature: str, body: str) -> set[str]:
    src = (
        f"{_CLASSES}class Quoted:\n"
        f"    def use(self, {signature}) -> int:\n"
        f"        return {body}\n"
    )
    graph = _index(tmp_path, {"m.py": src})
    return {
        str(r["to_qn"])
        for r in graph.rels
        if r["rel_type"] == "CALLS" and r["from_qn"] == "p.m.Quoted.use"
    }


@pytest.mark.parametrize(
    ("quoted", "body"),
    [
        ('n: "Node"', "n.weight()"),
        ("n: 'Node'", "n.weight()"),
        ('n: "Node" = None', "n.weight()"),
        ('*, n: "Node"', "n.weight()"),
        ('n: "Node | None"', "n.weight()"),
        ('ns: list["Node"]', "ns[0].weight()"),
        ('ns: "list[Node]"', "ns[0].weight()"),
    ],
)
def test_a_quoted_annotation_types_the_parameter(
    tmp_path: Path, quoted: str, body: str
) -> None:
    assert _callees(tmp_path / "q", quoted, body) == {"p.m.Node.weight"}


@pytest.mark.parametrize(
    ("quoted", "bare", "body"),
    [
        ('n: "Node"', "n: Node", "n.weight()"),
        ('n: Optional["Node"]', "n: Optional[Node]", "n.weight()"),
        ('ns: list["Node"]', "ns: list[Node]", "ns[0].weight()"),
    ],
)
def test_the_quoted_form_reads_as_the_bare_form(
    tmp_path: Path, quoted: str, bare: str, body: str
) -> None:
    assert _callees(tmp_path / "q", quoted, body) == _callees(
        tmp_path / "b", bare, body
    )


def test_the_fastapi_router_method_is_live(tmp_path: Path) -> None:
    src = """\
class APIRouter:
    def _contains_router(self, router: "APIRouter", seen=None) -> bool:
        for route in self.routes:
            if route.original_router._contains_router(router, seen):
                return True
        return router is self

    def include_router(self, router: "APIRouter") -> None:
        assert not router._contains_router(self)


def main() -> None:
    APIRouter().include_router(APIRouter())
"""
    graph = _index(tmp_path, {"routing.py": src})
    assert {
        "from_qn": "p.routing.APIRouter.include_router",
        "rel": "CALLS",
        "to_qn": "p.routing.APIRouter._contains_router",
    } in [
        {"from_qn": r["from_qn"], "rel": r["rel_type"], "to_qn": r["to_qn"]}
        for r in graph.rels
    ], graph.rels
    dead = _dead(graph)
    assert "routing.APIRouter._contains_router" not in dead, dead


def test_a_literal_string_is_not_a_type(tmp_path: Path) -> None:
    # Negatives: `Literal["Node"]` is the string "Node", not the class, and a
    # string that spells no type leaves the parameter untyped.
    assert _callees(tmp_path / "l", 'n: Literal["Node"]', "n.weight()") == set()
    assert _callees(tmp_path / "s", 'n: "a node"', "n.weight()") == set()


@pytest.mark.parametrize(
    ("written", "read"),
    [
        ('"Node"', "Node"),
        ("'pkg.mod.Node'", "pkg.mod.Node"),
        ('Dict[str, "Node"]', "Dict[str, Node]"),
        ("\"Optional['Node']\"", "Optional[Node]"),
        ('Annotated["Node", "meta"]', "Annotated[Node, 'meta']"),
    ],
)
def test_a_forward_reference_reads_as_the_name_it_quotes(
    written: str, read: str
) -> None:
    assert unquote_forward_refs(written) == read


@pytest.mark.parametrize(
    "written",
    [
        "Dict[str,int]",
        'Literal["a", "b"]',
        'typing.Literal["Node"]',
        '"a node"',
        '"Node',
        "",
    ],
)
def test_text_without_a_forward_reference_is_kept_as_written(written: str) -> None:
    # Negatives: no quotes, a Literal's values, a string that is not a type
    # expression and broken text all come back exactly as written.
    assert unquote_forward_refs(written) == written
