# Header free-function prototypes (issue #893). An out-of-class METHOD
# definition reattaches to the header class, so a member gets one node; a
# free function does not: the header prototype mints its own Function node
# (`proj.utils.h.FreeHelper`) beside the definition's
# (`proj.utils.FreeHelper`). Calls bind to the definition, so the
# prototype node has zero incoming edges and reports dead forever.
# Prototype registration is now deferred and dropped when a bodied
# definition of the same namespace-qualified function registers anywhere,
# mirroring the forward-declared-class machinery; a prototype-only
# function (defined outside the parsed tree) keeps its node.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

UTILS_H = """
int FreeHelper(int x);
int OnlyProto(int x);
static int TuLocal(int x);

namespace {
int AnonLocal(int x);
}

namespace alpha {
int Scoped(int x);
}
"""

UTILS_CPP = """
#include "utils.h"
int FreeHelper(int x) { return x; }
int TuLocal(int x) { return x; }
int AnonLocal(int x) { return x; }

namespace beta {
int Scoped(int x) { return x; }
}
"""

MAIN_CPP = """
#include "utils.h"
int main() {
  return FreeHelper(2);
}
"""


@pytest.fixture
def cpp_proto_project(temp_repo: Path) -> Path:
    root = temp_repo / "proto"
    root.mkdir()
    (root / "utils.h").write_text(UTILS_H, encoding="utf-8")
    (root / "utils.cpp").write_text(UTILS_CPP, encoding="utf-8")
    (root / "main.cpp").write_text(MAIN_CPP, encoding="utf-8")
    return root


def _functions(mock_ingestor: MagicMock) -> set[str]:
    return {
        str(c.args[1][cs.KEY_QUALIFIED_NAME])
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == cs.NodeLabel.FUNCTION.value
    }


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == cs.RelationshipType.CALLS.value
    }


def test_prototype_beside_definition_is_not_minted(
    cpp_proto_project: Path, mock_ingestor: MagicMock
):
    run_updater(cpp_proto_project, mock_ingestor)
    functions = _functions(mock_ingestor)
    assert not any(qn.endswith(".utils.h.FreeHelper") for qn in functions), sorted(
        functions
    )
    assert any(qn.endswith(".utils.FreeHelper") for qn in functions), sorted(functions)


def test_call_still_binds_to_the_definition(
    cpp_proto_project: Path, mock_ingestor: MagicMock
):
    run_updater(cpp_proto_project, mock_ingestor)
    calls = _calls(mock_ingestor)
    assert any(
        s.endswith(".main.main") and d.endswith(".utils.FreeHelper") for s, d in calls
    ), sorted(calls)


def test_prototype_only_function_keeps_its_node(
    cpp_proto_project: Path, mock_ingestor: MagicMock
):
    # OnlyProto has no parsed definition (an external TU could provide it);
    # dropping its only node would erase the symbol entirely.
    run_updater(cpp_proto_project, mock_ingestor)
    functions = _functions(mock_ingestor)
    assert any(qn.endswith(".utils.h.OnlyProto") for qn in functions), sorted(functions)


def test_static_prototype_is_never_deduped(
    cpp_proto_project: Path, mock_ingestor: MagicMock
):
    # `static int TuLocal(int);` has INTERNAL linkage: each translation
    # unit owns a separate function, so a definition registered from any
    # other module is not its definition and must not drop it (Greptile
    # round 2).
    run_updater(cpp_proto_project, mock_ingestor)
    functions = _functions(mock_ingestor)
    assert any(qn.endswith(".utils.h.TuLocal") for qn in functions), sorted(functions)


def test_anonymous_namespace_prototype_is_never_deduped(
    cpp_proto_project: Path, mock_ingestor: MagicMock
):
    # A function inside an anonymous namespace is translation-unit-local
    # even without `static`, so a same-named definition in another module
    # is not its definition (Greptile round 3).
    run_updater(cpp_proto_project, mock_ingestor)
    functions = _functions(mock_ingestor)
    assert any(qn.endswith(".utils.h.AnonLocal") for qn in functions), sorted(functions)


def test_namespace_mismatch_keeps_the_prototype(
    cpp_proto_project: Path, mock_ingestor: MagicMock
):
    # alpha::Scoped is prototyped, only beta::Scoped is defined: different
    # namespace-qualified functions, so the prototype survives.
    run_updater(cpp_proto_project, mock_ingestor)
    functions = _functions(mock_ingestor)
    assert any(qn.endswith(".utils.h.alpha.Scoped") for qn in functions), sorted(
        functions
    )
