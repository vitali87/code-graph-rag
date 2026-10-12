# Python symbols only a type checker or a runtime dispatcher reaches, found by
# running `cgr dead-code` on this repository:
# - a class declared under `if TYPE_CHECKING:` types `self` in the parser
#   mixins (`_AstAnalyzerDeps`); every `self._x()` resolves to the real
#   implementation, so 23 of the 25 default-mode candidates were its stubs.
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


def _dead(ingestor: _CapturingIngestor, public_roots: bool = True) -> set[str]:
    # Only what the dead-code fetch returns reaches the engine in
    # production, so a rule keyed on any other property would pass here and
    # do nothing against Memgraph. Classes are reported too: a root rule that
    # covers members must not leave their class behind.
    nodes = {key: _node_props(props) for key, props in ingestor.nodes.items()}
    config = default_dead_code_config(include_tests=True, include_classes=True)
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
