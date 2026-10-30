"""`cpp.local_types` (issue #2555): the qn a C++ type written in a function
body is named under (its callable, the local types around it, a position
name for an unnamed functor), the node that defines it, the anchors a name
is looked up under, an out-of-line overload's own marker (#2631), and the
type spellings that name a class rather than a local one."""

from __future__ import annotations

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.cpp.local_types import (
    callable_anchor_qn,
    in_local_type_member,
    is_local_type,
    is_positional_name,
    is_qualified_type,
    local_type_name,
    local_type_parent,
    local_type_qn,
    named_type,
    positional_name,
    scope_anchor_qns,
)
from codebase_rag.parsers.utils import function_span_key
from codebase_rag.tests.collaborator_support import first, nodes_of, parse, walk
from codebase_rag.types_defs import FunctionLocation, FunctionSpanKey

_CPP = cs.SupportedLanguage.CPP
_MODULE = "proj.m"

_SOURCE = """\
namespace ns {
struct Top { int v; };
int f() {
    struct Point { int x; };
    struct { void operator()(int) {} } enter;
    struct { int only; } data;
    enum Mode { A };
    struct Outer { struct Inner { void run() { helper(); } }; };
    auto l = [] { struct InLambda { int y; }; };
    return 0;
}
}
"""


class _File:
    def __init__(self) -> None:
        self.root = parse(_CPP, _SOURCE)
        self.f = next(
            n
            for n in nodes_of(self.root, "function_definition")
            if n.text is not None and n.text.startswith(b"int f()")
        )
        # What the definition pass recorded for `f`; the lambda has nothing.
        self.locations: dict[FunctionSpanKey, FunctionLocation] = {
            function_span_key(_MODULE, self.f): FunctionLocation(
                label=cs.NodeLabel.FUNCTION.value,
                qualified_name="proj.m.ns.f",
                container_qn=None,
            )
        }

    def type_at(self, row: int, column: int) -> Node:
        return next(
            n
            for n in walk(self.root)
            if n.type in ("struct_specifier", "enum_specifier")
            and n.child_by_field_name(cs.FIELD_BODY) is not None
            and n.start_point == (row, column)
        )

    def qn(self, row: int, column: int) -> str | None:
        return local_type_qn(self.type_at(row, column), _MODULE, self.locations)


def test_a_named_local_type_is_named_under_its_callable() -> None:
    file = _File()
    point = file.type_at(3, 4)
    assert is_local_type(point)
    assert file.qn(3, 4) == "proj.m.ns.f.Point"
    assert file.qn(6, 4) == "proj.m.ns.f.Mode"
    assert local_type_parent(point) == file.f


def test_an_unnamed_local_type_gets_a_node_only_when_it_has_members_to_call() -> None:
    file = _File()
    # The functor holds `operator()`, so a call needs somewhere to land.
    assert local_type_name(file.type_at(4, 4)) == "anonymous_4_4"
    assert file.qn(4, 4) == "proj.m.ns.f.anonymous_4_4"
    assert local_type_name(file.type_at(5, 4)) is None
    assert file.qn(5, 4) is None


def test_a_type_nested_in_a_local_type_is_named_and_defined_through_it() -> None:
    file = _File()
    inner = file.type_at(7, 19)
    assert file.qn(7, 19) == "proj.m.ns.f.Outer.Inner"
    assert local_type_parent(inner) == file.type_at(7, 4)


def test_a_type_at_namespace_scope_is_not_local() -> None:
    file = _File()
    top = file.type_at(1, 0)
    assert not is_local_type(top)
    assert file.qn(1, 0) is None
    assert local_type_parent(top) is None


def test_a_local_type_of_a_callable_with_no_node_gets_no_qn() -> None:
    file = _File()
    assert is_local_type(file.type_at(8, 18))
    assert file.qn(8, 18) is None
    assert local_type_qn(file.type_at(3, 4), _MODULE, {}) is None


def test_names_are_looked_up_from_the_innermost_scope_out() -> None:
    file = _File()
    call = first(file.root, "call_expression")
    # `Inner::run` itself has no recorded node, so it anchors nothing.
    assert scope_anchor_qns(call, _MODULE, file.locations) == [
        "proj.m.ns.f.Outer.Inner",
        "proj.m.ns.f.Outer",
        "proj.m.ns.f",
    ]


def test_a_call_in_a_local_types_member_is_not_the_callables_own() -> None:
    file = _File()
    assert in_local_type_member(first(file.root, "call_expression"), file.f)
    assert not in_local_type_member(first(file.root, "return_statement"), file.f)


def test_each_out_of_line_overload_keeps_its_own_local_types() -> None:
    root = parse(
        _CPP,
        "namespace ui {\n"
        "class Widget { int run(); int run(double); };\n"
        "int Widget::run() { struct L { int a; }; return 0; }\n"
        "int Widget::run(double d) { struct L { int a; }; return 1; }\n"
        "}\n",
    )
    # Named from what is written, with no recorded location needed; the
    # second overload carries the marker of the declaration it defines.
    assert [
        callable_anchor_qn(n, "proj.w", {})
        for n in nodes_of(root, "function_definition")
    ] == ["proj.w.ui.Widget.run", "proj.w.ui.Widget.run@2"]
    assert [
        local_type_qn(n, "proj.w", {}) for n in nodes_of(root, "struct_specifier")
    ] == ["proj.w.ui.Widget.run.L", "proj.w.ui.Widget.run@2.L"]


def test_a_position_name_is_recognised_only_in_full() -> None:
    root = parse(_CPP, "\n  struct { void g() {} } s;\n")
    assert positional_name(first(root, "struct_specifier")) == "anonymous_1_2"
    assert is_positional_name("anonymous_1_2")
    assert not is_positional_name("anonymous_1")
    assert not is_positional_name("my_anonymous_1_2")


def test_a_qualified_spelling_names_a_class_never_a_local_type() -> None:
    root = parse(_CPP, "ns::B b; ::C c; E e; F<int> f;")
    spellings = {
        n.text.decode(): (is_qualified_type(n), named_type(n))
        for n in walk(root)
        if n.parent is not None
        and n.parent.type == "declaration"
        and n.type in ("qualified_identifier", "template_type", "type_identifier")
        and n.text is not None
    }
    assert spellings == {
        "ns::B": (True, "B"),
        "::C": (True, "C"),
        "E": (False, "E"),
        "F<int>": (False, "F"),
    }
