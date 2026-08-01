# A Dart method passed as a tear-off (`Timer(d, _tick)`,
# `onPressed: _handleTap`, `controller.addListener(_update)`) is invoked by
# the receiving framework, never by first-party code, so dead-code flagged
# every callback handler: 129 of the wonderous app's remaining candidates
# were `_handleX` tear-offs. Mirror the C# method-group treatment: record the
# pass as a REFERENCES edge from the passing scope.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

REFERENCES = cs.RelationshipType.REFERENCES.value
CALLS = cs.RelationshipType.CALLS.value


def _run_rels(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
    if "dart" not in parsers:
        pytest.skip("dart parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
    }


def _has(
    rels: set[tuple[str, str, str]], caller_suffix: str, rel: str, callee_suffix: str
) -> bool:
    return any(
        a.endswith(caller_suffix) and r == rel and b.endswith(callee_suffix)
        for a, r, b in rels
    )


def test_positional_tearoff_to_external_callee_is_referenced(
    tmp_path: Path,
) -> None:
    files = {
        "app.dart": (
            "class Controller {\n"
            "  void tick() {}\n"
            "  void unused() {}\n"
            "  void start() {\n"
            "    schedule(tick);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, ".Controller.start", REFERENCES, ".Controller.tick"), rels
    assert not _has(rels, ".Controller.start", REFERENCES, ".Controller.unused")
    # A tear-off hands the method over without invoking it; it must never
    # double as a CALLS edge.
    assert not _has(rels, ".Controller.start", CALLS, ".Controller.tick")


def test_named_argument_tearoff_is_referenced(tmp_path: Path) -> None:
    files = {
        "app.dart": (
            "class Panel {\n"
            "  void handleTap() {}\n"
            "  void build() {\n"
            "    render(onPressed: handleTap, count: 3);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, ".Panel.build", REFERENCES, ".Panel.handleTap"), rels


def test_receiver_call_tearoff_is_referenced(tmp_path: Path) -> None:
    files = {
        "app.dart": (
            "class Watcher {\n"
            "  void update() {}\n"
            "  void attach(dynamic controller) {\n"
            "    controller.addListener(update);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, ".Watcher.attach", REFERENCES, ".Watcher.update"), rels


def test_conditional_tearoff_references_both_branches(tmp_path: Path) -> None:
    # `onUpdate: isRight ? _handleRightDrag : _handleLeftDrag` hands one of
    # two methods over; both are live. Dart's conditional_expression orders
    # operands [condition, consequence, alternative], unlike Python's
    # [body, condition, alternative], so index-based expansion must not
    # reference the condition.
    files = {
        "app.dart": (
            "class Selector {\n"
            "  void goRight() {}\n"
            "  void goLeft() {}\n"
            "  bool get isRight => true;\n"
            "  void build() {\n"
            "    render(onUpdate: isRight ? goRight : goLeft);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, ".Selector.build", REFERENCES, ".Selector.goRight"), rels
    assert _has(rels, ".Selector.build", REFERENCES, ".Selector.goLeft"), rels
    # The condition is never handed over as a value, but truthiness-testing
    # a GETTER invokes it: the getter-read pass (issue #869) records that
    # read. An operand-order regression is caught by the positive
    # assertions above (the consequence would go missing).
    assert _has(rels, ".Selector.build", REFERENCES, ".Selector.isRight"), rels


def test_list_literal_tearoffs_are_referenced(tmp_path: Path) -> None:
    files = {
        "app.dart": (
            "class Registry {\n"
            "  void first() {}\n"
            "  void second() {}\n"
            "  void register() {\n"
            "    addAll(handlers: [first, second]);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, ".Registry.register", REFERENCES, ".Registry.first"), rels
    assert _has(rels, ".Registry.register", REFERENCES, ".Registry.second"), rels


def test_map_and_set_literal_tearoffs_are_referenced(tmp_path: Path) -> None:
    # A dispatch table (`{"tap": onTap}`) stores its handlers in `pair`
    # nodes whose value field holds the tear-off; a set literal exposes them
    # directly. A typed map inserts a `type_arguments` child that carries no
    # values at all.
    files = {
        "app.dart": (
            "class Table {\n"
            "  void onTap() {}\n"
            "  void onDrag() {}\n"
            "  void onSetA() {}\n"
            "  void onTyped() {}\n"
            "  void register() {\n"
            '    addAll(handlers: {"tap": onTap, "drag": onDrag});\n'
            "    addSet({onSetA});\n"
            '    addTyped(<String, Function>{"t": onTyped});\n'
            "  }\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, ".Table.register", REFERENCES, ".Table.onTap"), rels
    assert _has(rels, ".Table.register", REFERENCES, ".Table.onDrag"), rels
    assert _has(rels, ".Table.register", REFERENCES, ".Table.onSetA"), rels
    assert _has(rels, ".Table.register", REFERENCES, ".Table.onTyped"), rels


def test_constructor_argument_ternary_tearoff_is_referenced(tmp_path: Path) -> None:
    # A tear-off inside a ternary handed to a RESOLVABLE constructor
    # (`Footer(onReset: cond ? _handleReset : null)`) is stored by the
    # constructed object and invoked later; issue #873's second repro
    # (wonderous `_CollectionScreenState._handleReset`).
    files = {
        "app.dart": (
            "class Footer {\n"
            "  final void Function()? onReset;\n"
            "  Footer({this.onReset});\n"
            "}\n"
            "class Screen {\n"
            "  void _handleReset() {}\n"
            "  Footer build(int discovered, int explored) {\n"
            "    return Footer(onReset: discovered + explored > 0"
            " ? _handleReset : null);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, ".Screen.build", REFERENCES, ".Screen._handleReset"), rels


def test_constructor_argument_direct_tearoff_is_referenced(tmp_path: Path) -> None:
    files = {
        "app.dart": (
            "class Footer {\n"
            "  final void Function()? onReset;\n"
            "  Footer({this.onReset});\n"
            "}\n"
            "class Screen {\n"
            "  void _handleReset() {}\n"
            "  Footer build() {\n"
            "    return Footer(onReset: _handleReset);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has(rels, ".Screen.build", REFERENCES, ".Screen._handleReset"), rels


def test_closure_argument_is_not_unwrapped_as_ternary(tmp_path: Path) -> None:
    # A closure argument (`on: () => flag ? f : g`) IS the first-class value:
    # the swallowed-ternary expansion must never tear a scope-opening node
    # apart, even though `function_expression` shares the `_expression`
    # suffix with the grammar's flat operator nodes. The grammar wraps the
    # body in `function_expression_body`, and the expansion additionally
    # excludes nested-scope nodes outright.
    from codebase_rag.parsers.call_processor import _first_class_value_children

    parsers, _ = load_parsers()
    if "dart" not in parsers:
        pytest.skip("dart parser not available")
    tree = parsers["dart"].parse(b"void t() { render(on: () => flag ? f : g); }")
    stack = [tree.root_node]
    closure = None
    while stack:
        node = stack.pop()
        if node.type == cs.TS_DART_FUNCTION_EXPRESSION:
            closure = node
            break
        stack.extend(node.named_children)
    assert closure is not None
    assert _first_class_value_children(closure, is_dart=True) is None
