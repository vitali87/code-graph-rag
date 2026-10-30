"""`PythonCallResolution`: the calls Python syntax makes without naming them
(a bare decorator, an operator's dunder, an instance call, a property read,
a class-body alias, a callable field, a Protocol stub's conformers), and the
helpers that read those shapes off the tree."""

from __future__ import annotations

from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.python.calls import (
    PythonCallResolution,
    _annotated_field_names,
    _bare_decorator_name,
    _operator_dunder_target,
    _ordered_param_names,
    _runs_at_module_load,
    _truthiness_operands,
)
from codebase_rag.tests.collaborator_support import edges, first, index, nodes_of, parse
from evals.recorded_graph import RecordingIngestor

_PY = cs.SupportedLanguage.PYTHON
_FILES = {
    "deco.py": (
        "def register(fn):\n    return fn\n\n"
        "@register\ndef handler():\n    return 1\n\n"
        "def outer():\n    @register\n    def inner():\n        return 1\n\n"
        "def wrap(fn):\n    return fn\n\n"
        "def own():\n    def wrap(fn):\n        return fn\n\n"
        "    @wrap\n    def inner():\n        return 1\n"
    ),
    "bag.py": (
        "class Bag:\n"
        "    def __getitem__(self, k):\n        return 1\n"
        "    def __setitem__(self, k, v):\n        pass\n"
        "    def __contains__(self, k):\n        return True\n"
        "    def __len__(self):\n        return 0\n"
        "    def __call__(self):\n        return 1\n"
        "    @property\n    def size(self):\n        return 3\n"
        "    def _plain(self):\n        return 1\n"
        "    run = _plain\n\n"
        "def use(b: Bag):\n"
        "    x = b[1]\n    b[2] = x\n    if 3 in b:\n        pass\n"
        "    n = len(b)\n    if b:\n        pass\n"
        "    b()\n    s = b.size\n    b.run()\n"
    ),
    "cb.py": (
        "def on_done():\n    return 1\n\n"
        "class Job:\n    def __init__(self, cb):\n        self.cb = cb\n"
        "    def finish(self):\n        self.cb()\n\n"
        "def go():\n    Job(on_done).finish()\n"
    ),
    "proto.py": (
        "from typing import Protocol\n\n"
        "class Speaker(Protocol):\n    def speak(self) -> str: ...\n\n"
        "class Dog:\n    def speak(self) -> str:\n        return 'woof'\n\n"
        "def talk(s: Speaker):\n    return s.speak()\n"
    ),
}


@pytest.fixture
def calls(temp_repo: Path, mock_ingestor: RecordingIngestor) -> set[tuple[str, str]]:
    updater = index(temp_repo / "pp", mock_ingestor, _FILES)
    assert isinstance(
        updater.factory.call_processor.calls_for(_PY), PythonCallResolution
    )
    return edges(mock_ingestor, cs.RelationshipType.CALLS)


def test_a_bare_decorator_runs_at_module_load_only_at_module_scope(
    calls: set[tuple[str, str]],
) -> None:
    module_callees = {target for source, target in calls if source == "pp.deco"}
    # Applied once, to `handler`: the nested `inner` is not decorated at load.
    assert module_callees == {"pp.deco.register"}


def test_a_bare_decorator_inside_a_function_is_that_functions_call(
    calls: set[tuple[str, str]],
) -> None:
    # `register(inner)` runs each time `outer` runs; `inner` itself never
    # calls it.
    assert ("pp.deco.outer", "pp.deco.register") in calls
    assert ("pp.deco.outer.inner", "pp.deco.register") not in calls


def test_a_bare_decorator_inside_a_function_binds_in_that_functions_scope(
    calls: set[tuple[str, str]],
) -> None:
    # `own`'s local `wrap` shadows the module's.
    assert ("pp.deco.own", "pp.deco.own.wrap") in calls
    assert ("pp.deco.own", "pp.deco.wrap") not in calls


@pytest.mark.parametrize(
    "dunder",
    ["__getitem__", "__setitem__", "__contains__", "__len__", "__call__"],
)
def test_operator_syntax_calls_the_dunder_the_class_defines(
    calls: set[tuple[str, str]], dunder: str
) -> None:
    assert ("pp.bag.use", f"pp.bag.Bag.{dunder}") in calls


def test_a_property_read_and_an_alias_call_run_their_methods(
    calls: set[tuple[str, str]],
) -> None:
    assert ("pp.bag.use", "pp.bag.Bag.size") in calls
    assert ("pp.bag.use", "pp.bag.Bag._plain") in calls


def test_a_callable_field_calls_the_function_the_construction_bound(
    calls: set[tuple[str, str]],
) -> None:
    assert ("pp.cb.Job.finish", "pp.cb.on_done") in calls


def test_a_call_through_a_protocol_stub_runs_on_its_conformers(
    calls: set[tuple[str, str]],
) -> None:
    assert ("pp.proto.talk", "pp.proto.Dog.speak") in calls


def _dunder(node: Node) -> tuple[bytes | None, str] | None:
    target = _operator_dunder_target(node)
    if target is None:
        return None
    receiver, dunder = target
    return (receiver.text if receiver is not None else None), dunder


def test_the_operator_shapes_and_their_dunders() -> None:
    root = parse(_PY, "x = b[1]\nb[2] = x\nk in b\nlen(b)\nlen(a, b)\na + b\n")
    read, write = nodes_of(root, cs.TS_PY_SUBSCRIPT)
    assert _dunder(read) == (b"b", "__getitem__")
    assert _dunder(write) == (b"b", "__setitem__")
    assert _dunder(first(root, cs.TS_PY_COMPARISON_OPERATOR)) == (b"b", "__contains__")
    one_arg, two_args = nodes_of(root, cs.TS_PY_CALL)
    assert _dunder(one_arg) == (b"b", "__len__")
    assert _dunder(two_args) is None
    # A binary operator is no dunder this pass reads.
    assert _dunder(first(root, "binary_operator")) is None


def test_the_operands_a_condition_tests_for_truthiness() -> None:
    root = parse(_PY, "if a:\n    pass\nwhile not b:\n    pass\nx = c and d\n")
    if_operands = _truthiness_operands(first(root, cs.TS_PY_IF_STATEMENT))
    assert [n.text for n in if_operands if n is not None] == [b"a"]
    not_operands = _truthiness_operands(first(root, cs.TS_PY_NOT_OPERATOR))
    assert [n.text for n in not_operands if n is not None] == [b"b"]
    and_operands = _truthiness_operands(first(root, cs.TS_PY_BOOLEAN_OPERATOR))
    assert [n.text for n in and_operands if n is not None] == [b"c", b"d"]
    assert _truthiness_operands(first(root, "assignment")) == []


def test_only_a_bare_decorator_is_named_here() -> None:
    root = parse(
        _PY,
        "@task\ndef a(): ...\n@pkg.deco\ndef b(): ...\n@deco(1)\ndef c(): ...\n",
    )
    assert [_bare_decorator_name(d) for d in nodes_of(root, "decorator")] == [
        "task",
        "pkg.deco",
        None,
    ]


def test_a_definition_runs_at_module_load_outside_any_function() -> None:
    root = parse(
        _PY,
        "class C:\n    def m(self): ...\n\ndef f():\n    def g(): ...\n",
    )
    m, f, g = nodes_of(root, cs.TS_PY_FUNCTION_DEFINITION)
    assert _runs_at_module_load(m)
    assert _runs_at_module_load(f)
    assert not _runs_at_module_load(g)


def test_constructor_and_field_order_skip_self_and_unannotated_names() -> None:
    root = parse(
        _PY,
        "class P:\n    fetch: object\n    other: int = 3\n    plain = 1\n"
        "    def __init__(self, a, b: int = 2, *rest, **kw): ...\n",
    )
    class_node = first(root, cs.TS_PY_CLASS_DEFINITION)
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    assert body is not None
    assert _annotated_field_names(body) == ("fetch", "other")
    assert _ordered_param_names(first(root, cs.TS_PY_FUNCTION_DEFINITION)) == ("a", "b")
