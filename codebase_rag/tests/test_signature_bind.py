# Unit tests for the tree-sitter half of `change_signature` (issue #1533):
# reading a Python header, renaming a parameter in the body, binding a call
# site's arguments to the old parameters and rendering the new argument
# list. Each helper runs on a real tree-sitter parse of a small snippet.

from __future__ import annotations

import re
from collections.abc import Sequence

import pytest
import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from codebase_rag import constants as cs
from codebase_rag.editing.signature_bind import (
    _bind_arguments,
    _body_reads,
    _body_references,
    _check_hierarchy,
    _definition_edits,
    _find_definition,
    _params_of,
    _render_arguments,
    _render_site,
    _take_receiver,
)
from codebase_rag.editing.signature_spec import (
    ParamSpec,
    SignatureRefused,
    SignatureSite,
    _Candidate,
    _Edit,
    _Header,
    _Source,
    _Unmapped,
)

_PARSER = Parser(Language(tspython.language()))
PATH = "m.py"


def _root(code: str) -> tuple[Node, bytes]:
    source = code.encode()
    return _PARSER.parse(source).root_node, source


def _function(root: Node, name: str) -> Node:
    node = _find_definition(root, name, 1, root.end_point[0] + 1)
    assert node is not None
    return node


def _header(
    code: str, name: str = "f", label: cs.NodeLabel = cs.NodeLabel.FUNCTION
) -> _Header:
    root, source = _root(code)
    function = _function(root, name)
    params = function.child_by_field_name("parameters")
    assert params is not None
    specs = _params_of(params, source, name)
    receiver = _take_receiver(name, label, specs, function, source)
    return _Header(
        qn=f"m.{name}",
        path=PATH,
        span=(params.start_byte, params.end_byte),
        line=function.start_point[0] + 1,
        col=function.start_point[1],
        receiver=receiver,
        params=specs,
        function=function,
        source=source,
    )


def _apply(source: bytes, edits: list[_Edit]) -> str:
    out = bytearray(source)
    for edit in sorted(edits, key=lambda e: e.span[0], reverse=True):
        out[edit.span[0] : edit.span[1]] = edit.text.encode()
    return out.decode()


def _calls(root: Node) -> list[Node]:
    found: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "call":
            found.append(node)
        stack.extend(reversed(node.children))
    return found


def _candidate(code: str, old: list[ParamSpec], nth: int = 0) -> _Candidate:
    root, source = _root(code)
    call = _calls(root)[nth]
    args = call.child_by_field_name("arguments")
    assert args is not None
    site = SignatureSite("call", PATH, call.start_point[0] + 1, 0, "m.g", "exact")
    return _Candidate(
        site,
        (args.start_byte, args.end_byte),
        source[args.start_byte : args.end_byte].decode(),
        _bind_arguments(args, source, old),
    )


def _p(name: str, default: bool = False) -> ParamSpec:
    return ParamSpec(name, f"{name}=0" if default else name, None, default)


# --- reading the header --------------------------------------------------------------


def test_find_definition_matches_name_within_line_window() -> None:
    root, _ = _root("def f():\n    pass\n\nclass C:\n    def f(self):\n        pass\n")
    top = _find_definition(root, "f", 1, 1)
    method = _find_definition(root, "f", 5, 5)
    assert top is not None and top.start_point[0] == 0
    assert method is not None and method.start_point[0] == 4
    assert _find_definition(root, "f", 2, 3) is None
    assert _find_definition(root, "g", 1, 6) is None


def test_params_are_read_as_plain_specs() -> None:
    header = _header("def f(a, b: int, c=1, d: str = 'x'):\n    pass\n")
    assert header.receiver is None
    assert header.params == [
        ParamSpec("a", "a", None, False),
        ParamSpec("b", "b: int", "int", False),
        ParamSpec("c", "c=1", None, True),
        ParamSpec("d", "d: str = 'x'", "str", True),
    ]


@pytest.mark.parametrize(
    ("params", "odd"),
    [
        ("a, *args", "*args"),
        ("a, **kw", "**kw"),
        ("a, *, k", "*"),
        ("a, /, b", "/"),
        ("*args: int", "*args: int"),
    ],
)
def test_non_plain_parameter_is_refused(params: str, odd: str) -> None:
    with pytest.raises(SignatureRefused, match=f"`{re.escape(odd)}` is not a plain"):
        _header(f"def f({params}):\n    pass\n")


def test_method_receiver_is_taken_off_the_params() -> None:
    code = "class C:\n    def f(self, a):\n        pass\n"
    header = _header(code, label=cs.NodeLabel.METHOD)
    assert header.receiver == "self"
    assert [p.name for p in header.params] == ["a"]


def test_function_keeps_a_parameter_named_self() -> None:
    header = _header("def f(self, a):\n    pass\n")
    assert header.receiver is None
    assert [p.name for p in header.params] == ["self", "a"]


@pytest.mark.parametrize("decorator", ["staticmethod", "builtins.staticmethod"])
def test_static_method_has_no_receiver(decorator: str) -> None:
    code = f"class C:\n    @{decorator}\n    def f(self, a):\n        pass\n"
    header = _header(code, label=cs.NodeLabel.METHOD)
    assert header.receiver is None
    assert [p.name for p in header.params] == ["self", "a"]


def test_classmethod_receiver_is_taken() -> None:
    code = "class C:\n    @classmethod\n    def f(cls, a):\n        pass\n"
    assert _header(code, label=cs.NodeLabel.METHOD).receiver == "cls"


def test_method_with_no_params_has_no_receiver() -> None:
    code = "class C:\n    def f():\n        pass\n"
    assert _header(code, label=cs.NodeLabel.METHOD).receiver is None


def test_unusual_receiver_name_is_refused() -> None:
    code = "class C:\n    def f(this, a):\n        pass\n"
    with pytest.raises(SignatureRefused, match="`this` is not"):
        _header(code, label=cs.NodeLabel.METHOD)


# --- the body --------------------------------------------------------------------


def test_body_reads_ignores_attribute_and_keyword_labels() -> None:
    header = _header("def f(a, b):\n    obj.a\n    g(a=1)\n    return b\n")
    assert _body_reads(header, "b")
    assert not _body_reads(header, "a")


def test_body_references_are_the_parameter_uses() -> None:
    code = "def f(a):\n    x = a + obj.a\n    g(a=a)\n    return [v for v in a]\n"
    header = _header(code)
    spans = _body_references(header, "a", "z", ())
    renamed = _apply(header.source, [_Edit(PATH, s, "z") for s in spans])
    assert renamed == (
        "def f(a):\n    x = z + obj.a\n    g(a=z)\n    return [v for v in z]\n"
    )


@pytest.mark.parametrize(
    "body",
    [
        "    def inner():\n        return a\n",
        "    h = lambda: a\n",
        "    class K:\n        y = a\n",
        "    global a\n",
        "    import a\n",
        "    return [1 for a in range(3)]\n",
    ],
)
def test_use_the_walk_cannot_follow_is_refused(body: str) -> None:
    header = _header(f"def f(a):\n{body}")
    with pytest.raises(SignatureRefused, match="nested scope"):
        _body_references(header, "a", "z", ())


@pytest.mark.parametrize(
    "body",
    ["    return z\n", "    def inner():\n        return z\n"],
)
def test_new_name_already_read_is_refused(body: str) -> None:
    header = _header(f"def f(a):\n    a\n{body}")
    with pytest.raises(SignatureRefused, match="already used"):
        _body_references(header, "a", "z", ())


def test_nested_scope_not_mentioning_either_name_is_left_alone() -> None:
    header = _header("def f(a):\n    def inner(q):\n        return q\n    return a\n")
    at = header.source.index(b"return a") + len("return ")
    assert _body_references(header, "a", "z", ()) == [(at, at + 1)]


def test_swapping_two_names_is_not_a_clash() -> None:
    header = _header("def f(a, b):\n    return a - b\n")
    at = header.source.index(b"a - b")
    assert _body_references(header, "a", "b", ["a", "b"]) == [(at, at + 1)]


# --- binding a call site ---------------------------------------------------------


OLD = [_p("a"), _p("b"), _p("c", default=True)]


def test_positional_and_keyword_arguments_bind_to_old_indexes() -> None:
    candidate = _candidate("g(1, c=3, b=2)\n", OLD)
    assert {i: (b.text, b.keyword, b.position) for i, b in candidate.bound.items()} == {
        0: ("1", False, 0),
        2: ("3", True, 1),
        1: ("2", True, 2),
    }


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        ("g(1, 2, 3, 4)\n", "4 positional arguments but 3"),
        ("g(*xs)\n", "cannot be read positionally"),
        ("g(**kw)\n", "cannot be read positionally"),
        ("g(1,  # note\n  2)\n", "cannot be read positionally"),
        ("g(1, d=2)\n", "keyword `d`"),
        ("g(1, a=2)\n", "`a` more than once"),
    ],
)
def test_unreadable_site_is_unmapped(call: str, reason: str) -> None:
    with pytest.raises(_Unmapped, match=reason):
        _candidate(call, OLD)


# --- rendering a call site -------------------------------------------------------


def _render(call: str, new: list[ParamSpec], sources: Sequence[_Source | None]) -> str:
    return _render_arguments(new, sources, _candidate(call, OLD).bound)


def test_reordered_positional_values_stay_positional() -> None:
    new = [_p("b"), _p("a")]
    assert _render("g(1, 2)\n", new, [_Source(index=1), _Source(index=0)]) == "(2, 1)"


def test_values_after_an_omitted_default_go_by_keyword() -> None:
    new = [_p("a"), _p("c", default=True), _p("b")]
    sources = [_Source(index=0), _Source(index=2), _Source(index=1)]
    assert _render("g(1, 2)\n", new, sources) == "(1, b=2)"


def test_keywords_keep_their_order_and_literals_are_inserted() -> None:
    new = [_p("a"), _p("n"), _p("x"), _p("y")]
    sources = [
        _Source(index=0),
        _Source(literal="'lit'"),
        _Source(index=2),
        _Source(index=1),
    ]
    assert _render("g(1, c=3, b=2)\n", new, sources) == "(1, 'lit', x=3, y=2)"


def test_missing_value_without_default_is_unmapped() -> None:
    with pytest.raises(_Unmapped, match="no value for `n`"):
        _render("g(1, 2)\n", [_p("a"), _p("n")], [_Source(index=0), None])


def test_unchanged_site_renders_no_edit() -> None:
    candidate = _candidate("g(1, 2)\n", OLD)
    sources = [_Source(index=0), _Source(index=1), _Source(index=2)]
    assert _render_site(candidate, OLD, sources, []) is None


def test_edit_nested_in_an_argument_is_folded_into_the_site() -> None:
    code = "g(a, g(1, 2))\n"
    outer = _candidate(code, OLD, nth=0)
    inner = _candidate(code, OLD, nth=1)
    new = [_p("b"), _p("a")]
    sources: list[_Source | None] = [_Source(index=1), _Source(index=0)]
    body_rename = _Edit(PATH, (2, 3), "z")
    inner_edit = _render_site(inner, new, sources, [])
    assert inner_edit is not None and inner_edit.text == "(2, 1)"
    edits = [body_rename, inner_edit]
    outer_edit = _render_site(outer, new, sources, edits)
    assert outer_edit is not None
    assert outer_edit.text == "(g(2, 1), z)"
    assert edits == []  # both folded edits left the plan


def test_edit_straddling_an_argument_leaves_the_site_unmapped() -> None:
    candidate = _candidate("g(1, 2)\n", OLD)
    straddling = _Edit(PATH, (2, 5), "x")
    edits = [straddling]
    with pytest.raises(_Unmapped, match="overlap"):
        _render_site(candidate, [_p("b"), _p("a")], [_Source(1), _Source(0)], edits)
    assert edits == [straddling]


def test_edit_in_another_file_is_not_folded() -> None:
    candidate = _candidate("g(1, 2)\n", OLD)
    elsewhere = _Edit("other.py", (2, 3), "x")
    edit = _render_site(candidate, [_p("b"), _p("a")], [_Source(1), _Source(0)], [])
    assert edit == _Edit(PATH, candidate.span, "(2, 1)")
    assert _render_site(
        candidate, [_p("b"), _p("a")], [_Source(1), _Source(0)], [elsewhere]
    ) == _Edit(PATH, candidate.span, "(2, 1)")


# --- the definition and its hierarchy --------------------------------------------


def test_hierarchy_must_declare_the_same_names() -> None:
    base = _header("def f(a, b):\n    pass\n")
    same = _header("def f(a, b=1):\n    pass\n")
    other = _header("def f(a, c):\n    pass\n")
    _check_hierarchy("m.f", [base, same], ["a", "b"])
    with pytest.raises(SignatureRefused, match=r"declares \(a, c\)"):
        _check_hierarchy("m.f", [base, same, other], ["a", "b"])


def test_definition_edits_rewrite_header_and_body() -> None:
    code = "class C:\n    def f(self, a: int, b=2):\n        return a + b\n"
    header = _header(code, label=cs.NodeLabel.METHOD)
    new = [ParamSpec("z", "z", None, False), header.params[1], _p("q", True)]
    edits = _definition_edits(header, new, {"b"}, [("a", "z")])
    assert _apply(header.source, edits) == (
        "class C:\n    def f(self, z, b=2, q=0):\n        return z + b\n"
    )


def test_definition_edits_refuse_a_default_reading_the_renamed_name() -> None:
    header = _header("def f(a, b=a):\n    return b\n")
    new = [ParamSpec("x", "x", None, False), header.params[1]]
    with pytest.raises(SignatureRefused, match="default of `b` reads `a`"):
        _definition_edits(header, new, {"b"}, [("a", "x")])
