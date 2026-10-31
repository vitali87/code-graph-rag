# Reading a JavaScript header for change_signature (issue #1908), on headers
# parsed with the JavaScript grammar.

from __future__ import annotations

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.editing.signature_js import (
    _check_literal,
    _find_definition,
    _params_of,
    _parser,
    _refuse_body_conflicts,
    _refuse_forward_defaults,
    _take_receiver,
)
from codebase_rag.editing.signature_spec import ParamSpec, SignatureRefused, _Header

QN = "sigjs.lib.util.helper"


def _header(source: str, name: str = "helper") -> _Header:
    data = source.encode(cs.ENCODING_UTF8)
    root = _parser().parse(data).root_node
    node = _find_definition(root, name, 1, data.count(b"\n") + 1)
    assert node is not None
    params = node.child_by_field_name(cs.FIELD_PARAMETERS)
    assert params is not None
    return _Header(
        QN,
        "lib/util.js",
        (params.start_byte, params.end_byte),
        params.start_point[0] + 1,
        params.start_point[1],
        None,
        _params_of(params, data, QN),
        node,
        data,
        cs.SupportedLanguage.JS,
    )


def _specs(*names: str) -> list[ParamSpec]:
    return [ParamSpec(name, name, None, False) for name in names]


def test_names_and_defaults_are_read_as_written() -> None:
    header = _header("function helper(a, b = 'x') {\n  return a + b;\n}\n")

    assert header.params == [
        ParamSpec("a", "a", None, False),
        ParamSpec("b", "b = 'x'", None, True),
    ]


@pytest.mark.parametrize(
    "source",
    [
        "class Shape {\n  area(scale) {\n    return scale;\n  }\n}\n",
        "function* helper(a) {\n  yield a;\n}\n",
    ],
    ids=["method", "generator"],
)
def test_a_method_and_a_generator_are_found(source: str) -> None:
    name = "area" if "area" in source else "helper"

    assert _header(source, name).params[0].name in ("scale", "a")


def test_an_arrow_function_is_not_a_header_it_reads() -> None:
    data = b"const helper = (a) => a;\n"
    root = _parser().parse(data).root_node

    assert _find_definition(root, "helper", 1, 1) is None


@pytest.mark.parametrize("param", ["...rest", "{ a }", "[a, b]", "{ a } = {}"])
def test_a_parameter_that_is_not_a_plain_name_refuses(param: str) -> None:
    with pytest.raises(SignatureRefused, match="is not a plain parameter") as refused:
        _header(f"function helper(x, {param}) {{\n  return x;\n}}\n")
    assert param in str(refused.value)


def test_a_name_declared_twice_refuses() -> None:
    # Sloppy mode accepts it, and the body sees the second `a`.
    with pytest.raises(SignatureRefused, match="declares parameter a more than once"):
        _header("function helper(a, a) {\n  return a;\n}\n")


@pytest.mark.parametrize(
    ("method", "reason"),
    [
        ("constructor(a) {\n    this.a = a;\n  }", "called through `new`"),
        ("set size(a) {\n    this.a = a;\n  }", "getter or setter"),
        ("get size() {\n    return 1;\n  }", "getter or setter"),
    ],
    ids=["constructor", "setter", "getter"],
)
def test_a_method_that_is_never_called_with_arguments_refuses(
    method: str, reason: str
) -> None:
    source = f"class Shape {{\n  {method}\n}}\n".encode(cs.ENCODING_UTF8)
    root = _parser().parse(source).root_node
    node = _find_definition(root, method.split("(", 1)[0].rsplit(maxsplit=1)[-1], 1, 5)
    assert node is not None

    with pytest.raises(SignatureRefused, match=reason):
        _take_receiver(QN, cs.NodeLabel.METHOD, [], node, source)


def test_an_ordinary_method_has_no_receiver_to_take() -> None:
    header = _header(
        "class Shape {\n  area(scale) {\n    return scale;\n  }\n}\n", "area"
    )

    assert (
        _take_receiver(QN, cs.NodeLabel.METHOD, [], header.function, header.source)
        is None
    )


SOURCE = "function helper(a, b = 'x') {\n  return String(a) + b;\n}\n"


@pytest.mark.parametrize(
    ("source", "new", "renamed", "reason"),
    [
        (SOURCE, ["a"], [], "still uses `b`"),
        (
            "function helper(a, b = a) {\n  return b;\n}\n",
            ["b"],
            [],
            "still uses `a`",
        ),
        (
            "function helper(a, b) {\n  ({ a } = b);\n  return b;\n}\n",
            ["b"],
            [],
            "still uses `a`",
        ),
        (SOURCE, ["a", "b", "String"], [], "already used"),
        (
            "function helper(a, b) {\n  return arguments.length;\n}\n",
            ["b", "a"],
            [],
            "reads `arguments`",
        ),
        (SOURCE, ["count", "b"], [("a", "count")], "renaming is not supported"),
    ],
    ids=[
        "dropped-read",
        "dropped-read-by-default",
        "dropped-assigned-by-shorthand-pattern",
        "added-taken",
        "arguments",
        "rename",
    ],
)
def test_a_change_the_body_would_disagree_with_refuses(
    source: str, new: list[str], renamed: list[tuple[str, str]], reason: str
) -> None:
    header = _header(source)
    old_names = [p.name for p in header.params]
    specs = _specs(*new)

    with pytest.raises(SignatureRefused, match=reason):
        _refuse_body_conflicts([header], old_names, specs, renamed)


def test_dropping_a_name_the_body_never_mentions_passes() -> None:
    header = _header("function helper(a, b) {\n  return a;\n}\n")

    _refuse_body_conflicts([header], ["a", "b"], _specs("a"), [])


def _param_list(params: str) -> tuple[Node, bytes]:
    header = _header(f"function helper{params} {{\n  return 0;\n}}\n")
    node = header.function.child_by_field_name(cs.FIELD_PARAMETERS)
    assert node is not None
    return node, header.source


@pytest.mark.parametrize(
    ("params", "name", "later"),
    [
        ("(b = a, a = 1)", "b", "a"),
        ("(b = a + 1, a)", "b", "a"),
        ("(a = a)", "a", "a"),
        ("(c, b = () => a, a)", "b", "a"),
    ],
    ids=["reordered", "expression", "itself", "closure"],
)
def test_a_default_reading_a_later_parameter_refuses(
    params: str, name: str, later: str
) -> None:
    node, source = _param_list(params)

    with pytest.raises(
        SignatureRefused, match=f"the default of {name} reads `{later}`"
    ):
        _refuse_forward_defaults(QN, node, source)


@pytest.mark.parametrize(
    "params", ["(a = 1, b = a)", "(a, b = a + c)", "(a, b)", "(a = String(1))"]
)
def test_a_default_reading_only_earlier_names_passes(params: str) -> None:
    node, source = _param_list(params)

    _refuse_forward_defaults(QN, node, source)


@pytest.mark.parametrize(
    "literal", ["1", " 'x' ", "LIMIT", "f(1, 2)", "[1, 2]", "{ a: 1 }", "a => a"]
)
def test_a_mapped_value_that_is_one_expression_passes(literal: str) -> None:
    _check_literal(ParamSpec("n", "n", None, False), literal)


@pytest.mark.parametrize(
    "literal", ["1, 2", "...xs", "1)(2", "1); g(", "", "1 /* note */"]
)
def test_a_mapped_value_that_is_not_one_argument_refuses(literal: str) -> None:
    # `1)(2` would turn `helper(3, <n>, 'z')` into `helper(3, 1)(2, 'z')`.
    spec = ParamSpec("n", "n", None, False)

    with pytest.raises(SignatureRefused, match="not one JavaScript expression"):
        _check_literal(spec, literal)
