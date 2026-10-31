# Writing for a JavaScript definition in change_signature (issue #1908): new
# parameters, the rewritten header and call sites, parsed with the
# JavaScript grammar.

from __future__ import annotations

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.editing.signature_js import _find_definition, _params_of, _parser
from codebase_rag.editing.signature_js_bind import (
    _bind_arguments,
    _definition_edits,
    _parse_new_param,
    _render_arguments,
    _site_arguments,
)
from codebase_rag.editing.signature_spec import (
    ParamSpec,
    SignatureRefused,
    _Binding,
    _Header,
    _Source,
    _Unmapped,
)

OLD = [ParamSpec("a", "a", None, False), ParamSpec("b", "b = 0", None, True)]


def _call(text: str) -> tuple[Node, bytes]:
    data = f"{text};\n".encode(cs.ENCODING_UTF8)
    root = _parser().parse(data).root_node
    statement = root.named_children[0]
    call = statement.named_children[0]
    assert call.type == cs.TS_CALL_EXPRESSION, call.type
    return call, data


def _bound(text: str, old: list[ParamSpec] = OLD) -> dict[int, _Binding]:
    call, data = _call(text)
    header = _Header("m.helper", "a.js", (0, 0), 1, 0, None, old, call, data)
    return _bind_arguments(_site_arguments(header, call, data), data, old)


def test_values_bind_by_position_and_names_and_literals_run_no_code() -> None:
    bound = _bound("helper(x, 'y')")

    assert [(b.text, b.position, b.inert) for b in bound.values()] == [
        ("x", 0, True),
        ("'y'", 1, True),
    ]


@pytest.mark.parametrize("value", ["f()", "-1", "x.y", "`t${x}`"])
def test_a_value_that_can_run_code_is_not_inert(value: str) -> None:
    assert not _bound(f"helper({value})")[0].inert


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("helper(...xs)", "cannot be read positionally"),
        ("helper(a /* note */)", "cannot be read positionally"),
        ("helper(1, 2, 3)", "passes 3"),
        ("helper`x`", "cannot be read positionally"),
        ("helper.call(null, 1)", "passes the receiver"),
        ("helper.apply(null, [1])", "passes the receiver"),
    ],
    ids=["spread", "comment", "too-many", "tagged-template", "call", "apply"],
)
def test_a_site_it_cannot_bind_is_left_unmapped(text: str, reason: str) -> None:
    with pytest.raises(_Unmapped, match=reason):
        _bound(text)


def _render(text: str, new: list[ParamSpec], sources: list[_Source | None]) -> str:
    return _render_arguments(new, sources, _bound(text))


A, B = OLD
C = ParamSpec("c", "c = 1", None, True)


def test_names_swap_places() -> None:
    assert _render("helper(x, y)", [B, A], [_Source(1), _Source(0)]) == "(y, x)"


def test_a_skipped_default_is_passed_as_void_0() -> None:
    # Not `undefined`: inside `function run(undefined) { helper(x); }` that
    # name holds whatever run was given, and the default would not apply.
    assert (
        _render("helper(x)", [A, B, C], [_Source(0), _Source(1), _Source(literal="5")])
        == "(x, void 0, 5)"
    )


def test_trailing_defaults_the_site_skips_stay_out() -> None:
    assert _render("helper(x)", [A, B, C], [_Source(0), _Source(1), None]) == "(x)"


def test_values_that_run_code_keep_their_order() -> None:
    assert (
        _render("helper(f(), g())", [A, C, B], [_Source(0), None, _Source(1)])
        == "(f(), void 0, g())"
    )


def test_values_that_run_code_are_never_reordered() -> None:
    sources: list[_Source | None] = [_Source(1), _Source(0)]

    with pytest.raises(_Unmapped, match="in another order"):
        _render("helper(f(), g())", [B, A], sources)


SOURCE = "function helper(a, b = 'x') {\n  return String(a) + b;\n}\n"


def _header(source: str = SOURCE) -> _Header:
    data = source.encode(cs.ENCODING_UTF8)
    node = _find_definition(_parser().parse(data).root_node, "helper", 1, 3)
    assert node is not None
    params = node.child_by_field_name(cs.FIELD_PARAMETERS)
    assert params is not None
    span = (params.start_byte, params.end_byte)
    specs = _params_of(params, data, "m.helper")
    return _Header("m.helper", "a.js", span, 1, 15, None, specs, node, data)


def test_a_new_parameter_is_a_name_or_a_name_with_a_default() -> None:
    old = {"a": ParamSpec("a", "a", None, False)}

    assert _parse_new_param("n", old) == ParamSpec("n", "n", None, False)
    assert _parse_new_param(" n = 0 ", old) == ParamSpec("n", "n = 0", None, True)
    assert _parse_new_param("a", old) is old["a"]


@pytest.mark.parametrize(
    "text", ["...xs", "{ a }", "a b", "a = ", "a, b", "a) {} function g(b"]
)
def test_a_new_parameter_that_is_not_one_plain_name_refuses(text: str) -> None:
    with pytest.raises(SignatureRefused, match="one plain parameter"):
        _parse_new_param(text, {})


def test_the_header_keeps_a_carried_parameter_as_written() -> None:
    header = _header()

    edits = _definition_edits(
        header,
        [*(_parse_new_param(t, {}) for t in ("a", "n")), header.params[1]],
        {"b"},
        [],
    )

    assert [edit.text for edit in edits] == ["(a, n, b = 'x')"]
    assert edits[0].span == header.span


def test_a_header_that_would_not_parse_refuses() -> None:
    header = _header()
    broken = ParamSpec("n", "n = )", None, True)

    with pytest.raises(SignatureRefused, match="not valid JavaScript"):
        _definition_edits(header, [header.params[0], broken], set(), [])


def test_a_reorder_that_moves_a_default_before_what_it_reads_refuses() -> None:
    header = _header("function helper(a = 1, b = a) {\n  return b;\n}\n")
    swapped = [header.params[1], header.params[0]]

    with pytest.raises(SignatureRefused, match="the default of b reads `a`"):
        _definition_edits(header, swapped, {"a", "b"}, [])


@pytest.mark.parametrize(
    ("params", "new"),
    [
        ("(a = f(), b = g())", ["b", "a"]),
        ("(a = f(), n, b = cfg.size)", ["b", "n", "a"]),
        # `b` reads `state` after `a` sets it; swapped, it reads the old one.
        ("(a = (state = 1), b = state)", ["b", "a"]),
    ],
    ids=["calls", "getter", "read-after-write"],
)
def test_a_reorder_that_runs_defaults_in_another_order_refuses(
    params: str, new: list[str]
) -> None:
    header = _header(f"function helper{params} {{\n  return 0;\n}}\n")
    own = {p.name: p for p in header.params}
    reordered = [own[name] for name in new]
    carried = set(new)

    with pytest.raises(SignatureRefused, match="would run in the other order"):
        _definition_edits(header, reordered, carried, [])


@pytest.mark.parametrize(
    ("params", "new"),
    [
        ("(a = 1, b = g())", ["b", "a"]),
        ("(a = x, b = y)", ["b", "a"]),
        ("(a = f(), b = g())", ["b"]),
        ("(a = f(), b = g())", ["a", "c", "b"]),
    ],
    ids=["literal-and-call", "two-reads", "one-dropped", "kept-in-order"],
)
def test_defaults_that_still_run_in_written_order_pass(
    params: str, new: list[str]
) -> None:
    header = _header(f"function helper{params} {{\n  return 0;\n}}\n")
    own = {p.name: p for p in header.params}
    specs = [own.get(name) or ParamSpec(name, name, None, False) for name in new]

    assert _definition_edits(header, specs, set(own) & set(new), [])
