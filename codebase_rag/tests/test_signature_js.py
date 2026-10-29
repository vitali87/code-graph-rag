# Reading a JavaScript header for change_signature (issue #1908), on headers
# parsed with the JavaScript grammar.

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.signature_js import (
    _find_definition,
    _params_of,
    _parser,
    _refuse_body_conflicts,
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
        (SOURCE, ["a"], [], "still reads `b`"),
        (
            "function helper(a, b = a) {\n  return b;\n}\n",
            ["b"],
            [],
            "still reads `a`",
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
        "added-taken",
        "arguments",
        "rename",
    ],
)
def test_a_change_the_body_would_disagree_with_refuses(
    source: str, new: list[str], renamed: list[tuple[str, str]], reason: str
) -> None:
    header = _header(source)

    with pytest.raises(SignatureRefused, match=reason):
        _refuse_body_conflicts(
            [header], [p.name for p in header.params], _specs(*new), renamed
        )


def test_dropping_a_name_the_body_never_mentions_passes() -> None:
    header = _header("function helper(a, b) {\n  return a;\n}\n")

    _refuse_body_conflicts([header], ["a", "b"], _specs("a"), [])
