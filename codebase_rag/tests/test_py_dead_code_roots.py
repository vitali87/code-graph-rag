# Python symbols only a type checker or a runtime dispatcher reaches, found by
# running `cgr dead-code` on this repository:
# - a class declared under `if TYPE_CHECKING:` types `self` in the parser
#   mixins (`_AstAnalyzerDeps`); every `self._x()` resolves to the real
#   implementation, so 23 of the 25 default-mode candidates were its stubs;
# - a typing.Protocol is named only in annotations, which the walk does not
#   follow, so with --classes every protocol was reported unreachable;
# - `ast.NodeVisitor.visit` finds its `visit_*` handlers with getattr, so
#   with --no-public-roots every visitor handler was reported;
# - pytest calls a plugin's `pytest_*` hooks by name, so with
#   --no-public-roots the hooks of the `pytest11` plugin were reported.
# Each test indexes a small project end to end and reads the dead set from
# the same node properties the `CYPHER_DEAD_CODE_NODES` fetch returns.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import (
    _node_props,
    dead_code_from_graph,
    default_dead_code_config,
)
from evals.cgr_graph import _capture, _CapturingIngestor

_PROJECT = "pydc"


def _index(tmp_path: Path, files: dict[str, str]) -> _CapturingIngestor:
    project = tmp_path / _PROJECT
    for rel, source in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return _capture(project, _PROJECT)


def _dead(
    ingestor: _CapturingIngestor, public_roots: bool = True, include_tests: bool = True
) -> set[str]:
    # Only what the dead-code fetch returns reaches the engine in
    # production, so a rule keyed on any other property would pass here and
    # do nothing against Memgraph. Classes are reported too: a root rule that
    # covers members must not leave their class behind.
    nodes = {key: _node_props(props) for key, props in ingestor.nodes.items()}
    config = default_dead_code_config(include_tests, include_classes=True)
    return dead_code_from_graph(
        nodes,
        list(ingestor.rels),
        f"{_PROJECT}.",
        config._replace(public_roots=public_roots),
    )


def _defined(ingestor: _CapturingIngestor, *qns: str) -> None:
    # "Not dead" must never pass for a symbol that was not indexed at all.
    indexed = {str(uid) for _label, uid in ingestor.nodes}
    missing = [qn for qn in qns if qn not in indexed]
    assert not missing, f"not indexed: {missing}"


def _class_props(ingestor: _CapturingIngestor, qn: str) -> dict:
    props = ingestor.nodes.get((cs.NodeLabel.CLASS.value, qn))
    assert props is not None, f"{qn} not indexed as a Class"
    return dict(props)


# --- 1. A class declared under `if TYPE_CHECKING:` is a declaration --------

# The shape of `_AstAnalyzerDeps` and the parser mixins: the checker reads the
# mixin's base as the stub class, the runtime as `object`, and the concrete
# `_lookup` arrives from a sibling base of the composed engine.
_CHECKER_ONLY = """from __future__ import annotations

import typing
from typing import TYPE_CHECKING

if {guard}:

    class _EngineDeps:
        def _lookup(self, name: str) -> int: ...

    _Base: type = _EngineDeps
else:
    _Base = object

    class _RuntimeOnly:
        def _never_called(self) -> int:
            return 1


class LookupMixin(_Base):
    def describe(self, name: str) -> int:
        return self._lookup(name)


class Store:
    def _lookup(self, name: str) -> int:
        return len(name)

    def _unused(self) -> int:
        return 0


class Engine(LookupMixin, Store):
    pass


def main() -> int:
    return Engine().describe("x")
"""

_ENGINE = f"{_PROJECT}.pkg.engine"


@pytest.mark.parametrize("guard", ["TYPE_CHECKING", "typing.TYPE_CHECKING"])
@pytest.mark.parametrize("public_roots", [True, False])
def test_checker_only_class_and_its_stubs_are_not_dead(
    tmp_path: Path, guard: str, public_roots: bool
) -> None:
    ingestor = _index(
        tmp_path,
        {"pkg/__init__.py": "", "pkg/engine.py": _CHECKER_ONLY.format(guard=guard)},
    )
    deps, stub = f"{_ENGINE}._EngineDeps", f"{_ENGINE}._EngineDeps._lookup"
    _defined(ingestor, deps, stub, f"{_ENGINE}.Store._unused")
    dead = _dead(ingestor, public_roots)
    assert deps not in dead
    assert stub not in dead
    # The rule is about the checker-only block, not the stub's name or body:
    # unused code beside it, or in the branch that runs, is still reported.
    assert f"{_ENGINE}.Store._unused" in dead
    assert f"{_ENGINE}._RuntimeOnly._never_called" in dead


def test_only_the_checker_only_class_is_marked(tmp_path: Path) -> None:
    ingestor = _index(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/engine.py": _CHECKER_ONLY.format(guard="TYPE_CHECKING"),
        },
    )
    marked = cs.KEY_TYPE_CHECKING_ONLY
    assert _class_props(ingestor, f"{_ENGINE}._EngineDeps").get(marked) is True
    for runtime_class in ("_RuntimeOnly", "LookupMixin", "Store", "Engine"):
        props = _class_props(ingestor, f"{_ENGINE}.{runtime_class}")
        assert marked not in props, runtime_class


def test_a_negated_guard_runs_and_is_not_a_declaration(tmp_path: Path) -> None:
    source = (
        "from typing import TYPE_CHECKING\n\n"
        "if not TYPE_CHECKING:\n\n"
        "    class _Runtime:\n"
        "        def _never_called(self) -> int:\n"
        "            return 1\n"
    )
    ingestor = _index(tmp_path, {"pkg/__init__.py": "", "pkg/guard.py": source})
    qn = f"{_PROJECT}.pkg.guard._Runtime"
    assert cs.KEY_TYPE_CHECKING_ONLY not in _class_props(ingestor, qn)
    assert f"{qn}._never_called" in _dead(ingestor)


def test_the_dead_code_fetch_returns_the_checker_only_mark() -> None:
    # `_node_props` reads the mark off the fetched row; a query that does not
    # return it would make the rule a no-op against Memgraph.
    assert f"AS {cs.KEY_TYPE_CHECKING_ONLY}" in cq.CYPHER_DEAD_CODE_NODES


# --- 2. A typing.Protocol is a declaration with its members -----------------

# Every spelling of the base the inheritance pass resolves to typing's or
# typing_extensions' Protocol, generic ones included. `Implementation` names
# a protocol without relisting Protocol, which makes it an ordinary class.
_PROTOCOLS = """from __future__ import annotations

import typing
import typing_extensions
from typing import Protocol, TypeVar
from typing_extensions import Protocol as ExtProtocol

T = TypeVar("T")


class Plain(Protocol):
    def _plain(self) -> int: ...


class Generic(Protocol[T]):
    def _generic(self) -> T: ...


class Dotted(typing.Protocol):
    def _dotted(self) -> int: ...


class DottedGeneric(typing.Protocol[T]):
    def _dotted_generic(self) -> T: ...


class Extensions(typing_extensions.Protocol):
    def _extensions(self) -> int: ...


class Aliased(ExtProtocol):
    def _aliased(self) -> int: ...


class Extended(Plain, Protocol):
    def _extended(self) -> int: ...


class _Private(Protocol):
    def _private(self) -> int: ...


class Implementation(Plain):
    def _plain(self) -> int:
        return 1

    def _unused(self) -> int:
        return 0
"""

_PROTOCOL_MEMBERS = {
    "Plain": "_plain",
    "Generic": "_generic",
    "Dotted": "_dotted",
    "DottedGeneric": "_dotted_generic",
    "Extensions": "_extensions",
    "Aliased": "_aliased",
    "Extended": "_extended",
    "_Private": "_private",
}

_PROTOS = f"{_PROJECT}.pkg.protos"


@pytest.mark.parametrize("public_roots", [True, False])
def test_protocol_classes_and_their_stubs_are_not_dead(
    tmp_path: Path, public_roots: bool
) -> None:
    ingestor = _index(tmp_path, {"pkg/__init__.py": "", "pkg/protos.py": _PROTOCOLS})
    declared = {f"{_PROTOS}.{cls}" for cls in _PROTOCOL_MEMBERS} | {
        f"{_PROTOS}.{cls}.{member}" for cls, member in _PROTOCOL_MEMBERS.items()
    }
    _defined(ingestor, *declared, f"{_PROTOS}.Implementation._unused")
    dead = _dead(ingestor, public_roots)
    assert not declared & dead, sorted(declared & dead)
    assert f"{_PROTOS}.Implementation._unused" in dead
    if not public_roots:
        assert f"{_PROTOS}.Implementation" in dead


# --- 3. ast.NodeVisitor handlers are dispatched by name ---------------------

# Both bases, both import spellings, and a visitor that inherits the dispatch
# from a project visitor. Handlers are public, so only --no-public-roots
# could ever report them.
_VISITORS = """from __future__ import annotations

import ast
from ast import NodeTransformer, NodeVisitor


class Calls(ast.NodeVisitor):
    def visit_Call(self, node: ast.Call) -> None:
        self._record(node)

    def _record(self, node: ast.Call) -> None:
        return None

    def _unused(self) -> None:
        return None


class Names(NodeVisitor):
    def visit_Name(self, node: ast.Name) -> None:
        return None

    def generic_visit(self, node: ast.AST) -> None:
        return None


class Attributes(Names):
    def visit_Attribute(self, node: ast.Attribute) -> None:
        return None


class Rewriter(NodeTransformer):
    def visit_Import(self, node: ast.Import) -> ast.Import:
        return node


class Dotted(ast.NodeTransformer):
    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.ImportFrom:
        return node


class NotAVisitor:
    def visit_Call(self, node: ast.Call) -> None:
        return None
"""

_VISIT = f"{_PROJECT}.pkg.visitors"


def test_ast_visitor_handlers_and_their_callees_are_not_dead(
    tmp_path: Path,
) -> None:
    ingestor = _index(tmp_path, {"pkg/__init__.py": "", "pkg/visitors.py": _VISITORS})
    dispatched = {
        f"{_VISIT}.{member}"
        for member in (
            "Calls.visit_Call",
            "Calls._record",
            "Names.visit_Name",
            "Names.generic_visit",
            "Attributes.visit_Attribute",
            "Rewriter.visit_Import",
            "Dotted.visit_ImportFrom",
        )
    }
    unused = {f"{_VISIT}.Calls._unused", f"{_VISIT}.NotAVisitor.visit_Call"}
    _defined(ingestor, *dispatched, *unused)
    dead = _dead(ingestor, public_roots=False)
    assert not dispatched & dead, sorted(dispatched & dead)
    # The name alone roots nothing: a `visit_*` method on a class that is no
    # visitor, and a visitor's own unused helper, are still reported.
    assert unused <= dead, sorted(unused - dead)


# --- 4. pytest hooks are called by name ------------------------------------

# A plugin module (what a `pytest11` entry point names) with a plugin object
# it registers. A conftest.py hook is test code, rooted with the tests and
# left out without them, so it is never reported either way.
_PLUGIN = """from __future__ import annotations


def pytest_addoption(parser) -> None:
    parser.addoption("--trace-out")


def pytest_unconfigure(config) -> None:
    _flush()


def _flush() -> None:
    return None


def _unused() -> None:
    return None


class _Reporter:
    def pytest_runtest_logreport(self, report) -> None:
        return None


def outer() -> None:
    def pytest_nested() -> None:
        return None
"""

_CONFTEST = """def pytest_collection_modifyitems(items) -> None:
    return None
"""

_PLUGIN_QN = f"{_PROJECT}.pkg.plugin"


@pytest.mark.parametrize("include_tests", [True, False])
def test_pytest_hooks_and_their_callees_are_not_dead(
    tmp_path: Path, include_tests: bool
) -> None:
    ingestor = _index(
        tmp_path,
        {"pkg/__init__.py": "", "pkg/plugin.py": _PLUGIN, "conftest.py": _CONFTEST},
    )
    hooks = {
        f"{_PLUGIN_QN}.{member}"
        for member in (
            "pytest_addoption",
            "pytest_unconfigure",
            "_flush",
            "_Reporter.pytest_runtest_logreport",
        )
    }
    conftest_hook = f"{_PROJECT}.conftest.pytest_collection_modifyitems"
    unused = {f"{_PLUGIN_QN}._unused", f"{_PLUGIN_QN}.outer.pytest_nested"}
    _defined(ingestor, *hooks, conftest_hook, *unused)
    dead = _dead(ingestor, public_roots=False, include_tests=include_tests)
    assert not hooks & dead, sorted(hooks & dead)
    assert conftest_hook not in dead
    # A function nested in another is no plugin attribute pytest could call.
    assert unused <= dead, sorted(unused - dead)
