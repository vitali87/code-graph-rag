# (H) The thrift sweep found 521 JS/TS source locations minting MULTIPLE nodes:
# (H) the generic function pass registers every unnamed function expression as
# (H) `anonymous_row_col` BEFORE the named JS passes (object literals, exports,
# (H) assignment arrows, prototype methods) register the same source function
# (H) under its real name, and two named passes registering the same function
# (H) collide in register_unique_qn, minting a spurious `name@line` twin. One
# (H) source function must yield exactly one node: named passes claim their
# (H) function node's span in function_locations (first claim wins), and the
# (H) generic pass defers anonymous JS registration until after the named
# (H) passes, registering only unclaimed spans.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

EXPORTS_JS = """
exports.readByte = function (b) {
  return b > 127 ? b - 256 : b;
};
"""

OBJECT_LITERAL_JS = """
var helloHandler = {
  hello_func: function (result) {
    return "Hello " + result;
  },
};
"""

PAREN_EXPORT_JS = """
var Connection = (exports.Connection = function (stream) {
  this.stream = stream;
});
"""

CALLBACK_JS = """
setTimeout(function () {
  return 42;
}, 100);
"""

PROTOTYPE_JS = """
function Reader(buf) {
  this.buf = buf;
}

Reader.prototype.readAll = function () {
  return this.buf;
};
"""

CALLER_JS = """
function shift(b) {
  return b << 1;
}

exports.readWord = function (b) {
  return shift(b);
};
"""

NESTED_RETURN_JS = """
exports.receiver = function (callback) {
  return function (data) {
    return callback(data);
  };
};
"""

SAME_NAME_EXPR_JS = """
register("first", function t(x) {
  return x;
});

register("second", function t(y) {
  return function inner() {
    return y;
  };
});
"""


def _function_nodes_by_location(
    mock_ingestor: MagicMock,
) -> dict[tuple[str, int], set[str]]:
    by_loc: dict[tuple[str, int], set[str]] = {}
    for call in mock_ingestor.ensure_node_batch.call_args_list:
        label = str(call.args[0])
        if label not in (
            cs.NodeLabel.FUNCTION.value,
            cs.NodeLabel.METHOD.value,
        ):
            continue
        props = call.args[1]
        path = props.get(cs.KEY_PATH)
        start = props.get(cs.KEY_START_LINE)
        if path is None or start is None:
            continue
        by_loc.setdefault((str(path), int(start)), set()).add(
            props[cs.KEY_QUALIFIED_NAME]
        )
    return by_loc


def _assert_single_node_per_location(mock_ingestor: MagicMock) -> None:
    duplicated = {
        loc: qns
        for loc, qns in _function_nodes_by_location(mock_ingestor).items()
        if len(qns) > 1
    }
    assert not duplicated, duplicated


def _all_function_qns(mock_ingestor: MagicMock) -> set[str]:
    return {
        qns
        for qn_set in _function_nodes_by_location(mock_ingestor).values()
        for qns in qn_set
    }


def test_commonjs_export_function_yields_single_named_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "binary.js").write_text(EXPORTS_JS)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="javascript")

    project = temp_repo.name
    qns = _all_function_qns(mock_ingestor)
    assert f"{project}.binary.readByte" in qns, qns
    _assert_single_node_per_location(mock_ingestor)


def test_object_literal_method_yields_single_named_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "hello.js").write_text(OBJECT_LITERAL_JS)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="javascript")

    qns = _all_function_qns(mock_ingestor)
    assert any(qn.endswith("hello_func") for qn in qns), qns
    _assert_single_node_per_location(mock_ingestor)


def test_parenthesized_export_assignment_yields_single_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "connection.js").write_text(PAREN_EXPORT_JS)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="javascript")

    project = temp_repo.name
    qns = _all_function_qns(mock_ingestor)
    assert f"{project}.connection.Connection" in qns, qns
    assert not any(cs.DUP_QN_MARKER in qn for qn in qns), qns
    _assert_single_node_per_location(mock_ingestor)


def test_prototype_method_yields_single_constructor_scoped_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "reader.js").write_text(PROTOTYPE_JS)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="javascript")

    project = temp_repo.name
    qns = _all_function_qns(mock_ingestor)
    assert f"{project}.reader.Reader.readAll" in qns, qns
    _assert_single_node_per_location(mock_ingestor)


def test_true_anonymous_callback_still_gets_its_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A callback argument is claimed by NO named pass; deferring anonymous
    # (H) registration must not lose it.
    (temp_repo / "timer.js").write_text(CALLBACK_JS)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="javascript")

    qns = _all_function_qns(mock_ingestor)
    assert any(
        qn.rsplit(cs.SEPARATOR_DOT, 1)[-1].startswith(cs.PREFIX_ANONYMOUS) for qn in qns
    ), qns
    _assert_single_node_per_location(mock_ingestor)


def _defines_pairs(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(call.args[0][2]), str(call.args[2][2]))
        for call in get_relationships(
            mock_ingestor, cs.RelationshipType.DEFINES.value
        )
    }


def test_nested_function_parents_to_claimed_named_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) The enclosing function's span is claimed under `receiver`; deriving
    # (H) the nested function's parent structurally would produce the
    # (H) since-unregistered anonymous qn and hoist the child to the module.
    # (H) The parent derivation must reuse the claimed identity.
    (temp_repo / "transport.js").write_text(NESTED_RETURN_JS)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="javascript")

    assert any(
        parent.endswith(".receiver")
        and cs.PREFIX_ANONYMOUS in child.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        for parent, child in _defines_pairs(mock_ingestor)
    ), _defines_pairs(mock_ingestor)


def test_nested_function_parents_to_its_own_same_name_enclosing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Two function expressions share the name `t`; the second registers as
    # (H) `t@line`. A function nested in the SECOND must parent to `t@line`,
    # (H) not to the first `t` (structural re-derivation binds the wrong
    # (H) function; the span record knows which one encloses the child).
    (temp_repo / "suite.js").write_text(SAME_NAME_EXPR_JS)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="javascript")

    inner_parents = {
        parent
        for parent, child in _defines_pairs(mock_ingestor)
        if child.endswith(".inner")
    }
    assert any(cs.DUP_QN_MARKER in parent for parent in inner_parents), (
        inner_parents,
        _defines_pairs(mock_ingestor),
    )


def test_calls_from_exported_function_attribute_to_named_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) With the anonymous twin gone, Pass-3 caller attribution must bind the
    # (H) body's calls to the surviving NAMED node, not re-derive a phantom
    # (H) anonymous caller (the conftest gate would flag that as dangling).
    (temp_repo / "word.js").write_text(CALLER_JS)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="javascript")

    project = temp_repo.name
    callers = {
        str(call.args[0][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
        if str(call.args[2][2]) == f"{project}.word.shift"
    }
    assert f"{project}.word.readWord" in callers, callers
