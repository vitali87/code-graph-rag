# Dart CALLS extraction (follow-up to #140): the tree-sitter-dart grammar
# has no call-expression node (an invocation is an identifier or selector
# chain followed by a `selector` holding an `argument_part`),
# and it splits every definition into a signature node plus a SIBLING
# function_body, so both call capture and caller attribution need
# Dart-specific handling. Also fixes named constructors: the grammar's
# `name` field on constructor_signature is the CLASS identifier, which
# collapsed `Greeter.named` into a duplicate `Greeter` method.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

SKIP = "dart"

APP_DART = """
int helper(int x) {
  return x + 1;
}

class Greeter {
  String name;
  Greeter(this.name);
  Greeter.named(String n) : name = n;

  String greet() {
    return sayHello(name);
  }

  String describe() {
    return this.greet();
  }

  String sayHello(String who) {
    return 'hi ' + who;
  }

  static Greeter create() {
    return Greeter('static');
  }
}

void main() {
  var g = Greeter('world');
  var n = Greeter.named('x');
  var s = Greeter.create();
  helper(41);
}
"""


@pytest.fixture
def dart_calls_project(temp_repo: Path) -> Path:
    root = temp_repo / "dcalls"
    root.mkdir()
    (root / "app.dart").write_text(APP_DART, encoding="utf-8")
    return root


def _edges(mock_ingestor: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == rel
    }


def _has(edges: set[tuple[str, str]], src_suffix: str, dst_suffix: str) -> bool:
    return any(
        src.endswith(src_suffix) and dst.endswith(dst_suffix) for src, dst in edges
    )


def test_module_function_call(dart_calls_project: Path, mock_ingestor: MagicMock):
    run_updater(dart_calls_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert _has(calls, ".app.main", ".app.helper"), sorted(calls)


def test_same_class_method_call(dart_calls_project: Path, mock_ingestor: MagicMock):
    run_updater(dart_calls_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS.value)
    # implicit-this member call
    assert _has(calls, ".Greeter.greet", ".Greeter.sayHello"), sorted(calls)
    # explicit this.method()
    assert _has(calls, ".Greeter.describe", ".Greeter.greet"), sorted(calls)


def test_construction_emits_instantiates_and_ctor_call(
    dart_calls_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_calls_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS.value)
    inst = _edges(mock_ingestor, cs.RelationshipType.INSTANTIATES.value)
    assert _has(inst, ".app.main", ".app.Greeter"), sorted(inst)
    assert _has(calls, ".app.main", ".Greeter.Greeter"), sorted(calls)
    # construction inside a static method of the class itself
    assert _has(calls, ".Greeter.create", ".Greeter.Greeter"), sorted(calls)


def test_named_constructor_registers_and_receives_call(
    dart_calls_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_calls_project, mock_ingestor, skip_if_missing=SKIP)
    methods = {
        str(c.args[1].get("qualified_name"))
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == cs.NodeLabel.METHOD.value
    }
    # the grammar's name field on constructor_signature is the CLASS name;
    # the ctor's declared name is its LAST identifier
    assert any(q.endswith(".Greeter.named") for q in methods), sorted(methods)
    assert not any("Greeter@" in q for q in methods), sorted(methods)

    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert _has(calls, ".app.main", ".Greeter.named"), sorted(calls)


def test_static_method_call_via_class_name(
    dart_calls_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_calls_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert _has(calls, ".app.main", ".Greeter.create"), sorted(calls)


def test_call_name_shapes() -> None:
    # unit coverage of every chain shape dart_call_name handles, straight
    # off real parse trees
    import pytest as _pytest

    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.dart import (
        dart_body_node,
        dart_call_name,
        dart_definition_end_byte,
    )

    parsers, _ = load_parsers()
    if SKIP not in parsers:
        _pytest.skip("dart parser not available")
    dart = parsers[cs.SupportedLanguage.DART]

    src = b"""
class A extends B {
  void run() {
    plain();
    obj.member();
    obj?.maybe();
    this.step();
    super.init();
    b..first()..second();
    obj.field..chainCascade();
    f()..brokenCascade();
    items[0].touch();
    f().chained();
    Widget.of(context);
  }
}
"""
    tree = dart.parse(src)
    calls: list[str | None] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in ("selector", "cascade_section") and any(
            c.type == "argument_part" for c in node.named_children
        ):
            calls.append(dart_call_name(node))
        stack.extend(reversed(node.named_children))
    assert "plain" in calls
    assert "obj.member" in calls
    assert "obj.maybe" in calls
    # this./super. bases drop so the member resolves against the class
    assert "step" in calls
    assert "init" in calls
    assert "b.first" in calls
    assert "b.second" in calls
    # a cascade on a member chain keeps the full receiver; resolving it as
    # a bare name would risk binding an unrelated same-name function
    # (PR #804 review)
    assert "obj.field.chainCascade" in calls
    assert "brokenCascade" not in calls
    assert "Widget.of" in calls
    # a call-result receiver is now preserved as a `()` chain form so the
    # resolver can type it (`f().chained` -> f's return type); an index
    # receiver and a cascade on a call result still have no static name
    assert "f().chained" in calls
    assert calls.count(None) >= 2

    # span helpers pass non-signature nodes through unchanged
    root = tree.root_node
    assert dart_body_node(root) is None
    assert dart_definition_end_byte(root) == root.end_byte


def test_body_calls_not_attributed_to_module(
    dart_calls_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_calls_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS.value)
    # helper(41) runs inside main's body; the signature/body split must not
    # leak it up to the module
    assert not _has(calls, "dcalls.app", ".app.helper") or _has(
        calls, ".app.main", ".app.helper"
    ), sorted(calls)
    module_sources = {src for src, _ in calls if src.endswith("dcalls.app")}
    assert not module_sources, sorted(calls)
