# C++ stack-object constructions in declaration form (issue #871). Two
# shapes emit no construction edges today: `Point origin(10, 10)` (a plain
# init_declarator with an argument list) and the most-vexing-parse misparse
# `FlutterWindow window(project);`, which tree-sitter reads as a FUNCTION
# DECLARATION named `window` returning FlutterWindow. The misparse also
# mints a phantom Function node. Recovery is conservative: a declarator
# argument list counts as a construction only when every "parameter" is a
# bare type-less identifier and at least one names an in-scope local or
# parameter of the enclosing function, so genuine local prototypes
# (`int helper(int);`, `FlutterWindow maker(SomeType);`) keep working.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

MAIN_CPP = """
class FlutterWindow {
 public:
  FlutterWindow(int project);
  ~FlutterWindow();
  bool Create();
};

FlutterWindow::FlutterWindow(int project) {}
FlutterWindow::~FlutterWindow() {}
bool FlutterWindow::Create() { return true; }

class Point {
 public:
  Point(int x, int y) {}
};

class SomeType {
 public:
  SomeType() {}
};

int wWinMain() {
  int project = 1;
  FlutterWindow window(project);
  Point origin(10, 10);
  if (!window.Create()) {
    return 1;
  }
  return 0;
}

int prototypes() {
  int helper(int);
  FlutterWindow maker(SomeType);
  FlutterWindow factory();
  return 0;
}

int laterShadow() {
  FlutterWindow protoA(SomeType);
  int SomeType = 1;
  return SomeType;
}

int nestedShadow() {
  FlutterWindow protoB(SomeType);
  if (1) {
    int SomeType = 2;
  }
  return 0;
}

int conditionInit() {
  if (int cproject = 1) {
    FlutterWindow cwindow(cproject);
  }
  return 0;
}

int lambdaScope() {
  auto g = [](int lproject) {
    FlutterWindow lwindow(lproject);
    return 0;
  };
  return g(1);
}

int lambdaCapture() {
  int kproject = 1;
  auto h = [kproject]() {
    FlutterWindow kwindow(kproject);
    return 0;
  };
  return h();
}
"""


@pytest.fixture
def cpp_vexing_project(temp_repo: Path) -> Path:
    root = temp_repo / "vexing"
    root.mkdir()
    (root / "main.cpp").write_text(MAIN_CPP, encoding="utf-8")
    return root


def _rels(mock_ingestor: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == rel
    }


def _functions(mock_ingestor: MagicMock) -> set[str]:
    return {
        str(c.args[1][cs.KEY_QUALIFIED_NAME])
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == cs.NodeLabel.FUNCTION.value
    }


def _has(edges: set[tuple[str, str]], src: str, dst: str) -> bool:
    return any(s.endswith(src) and d.endswith(dst) for s, d in edges)


def test_vexing_parse_mints_no_phantom_function(
    cpp_vexing_project: Path, mock_ingestor: MagicMock
):
    run_updater(cpp_vexing_project, mock_ingestor)
    functions = _functions(mock_ingestor)
    assert not any(qn.endswith(".main.window") for qn in functions), sorted(functions)


def test_vexing_parse_emits_construction_calls(
    cpp_vexing_project: Path, mock_ingestor: MagicMock
):
    # `FlutterWindow window(project);` runs the ctor now and the dtor at end
    # of lifetime; both get the redirect, like an explicit construction.
    run_updater(cpp_vexing_project, mock_ingestor)
    calls = _rels(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert _has(calls, ".main.wWinMain", ".FlutterWindow.FlutterWindow"), sorted(calls)
    assert _has(calls, ".main.wWinMain", ".FlutterWindow.~FlutterWindow"), sorted(calls)


def test_vexing_parse_emits_instantiates(
    cpp_vexing_project: Path, mock_ingestor: MagicMock
):
    run_updater(cpp_vexing_project, mock_ingestor)
    inst = _rels(mock_ingestor, cs.RelationshipType.INSTANTIATES.value)
    assert _has(inst, ".main.wWinMain", ".main.FlutterWindow"), sorted(inst)


def test_init_declarator_construction_emits_ctor_call(
    cpp_vexing_project: Path, mock_ingestor: MagicMock
):
    # `Point origin(10, 10)` parses correctly yet emitted no edges either:
    # a declaration-shaped construction never reached the ctor redirect.
    run_updater(cpp_vexing_project, mock_ingestor)
    calls = _rels(mock_ingestor, cs.RelationshipType.CALLS.value)
    inst = _rels(mock_ingestor, cs.RelationshipType.INSTANTIATES.value)
    assert _has(calls, ".main.wWinMain", ".Point.Point"), sorted(calls)
    assert _has(inst, ".main.wWinMain", ".main.Point"), sorted(inst)


def test_genuine_local_prototypes_are_not_constructions(
    cpp_vexing_project: Path, mock_ingestor: MagicMock
):
    # `int helper(int);` (typed parameter), `FlutterWindow maker(SomeType);`
    # (a real type, no such local) and `FlutterWindow factory();` (the
    # standard says function declaration) all stay declarations: no ctor
    # call, no construction edges from `prototypes`.
    run_updater(cpp_vexing_project, mock_ingestor)
    calls = _rels(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert not _has(calls, ".main.prototypes", ".FlutterWindow.FlutterWindow"), sorted(
        calls
    )
    assert not _has(calls, ".main.prototypes", ".SomeType.SomeType"), sorted(calls)


def test_condition_init_name_counts_as_evidence(
    cpp_vexing_project: Path, mock_ingestor: MagicMock
):
    # An if/switch condition declaration (`if (int cproject = 1)`) nests
    # inside condition_clause, not as a direct ancestor child, yet its name
    # is in scope for the branch body: the construction must be recognised
    # (Greptile round 3).
    run_updater(cpp_vexing_project, mock_ingestor)
    functions = _functions(mock_ingestor)
    assert not any(qn.endswith(".main.cwindow") for qn in functions), sorted(functions)
    calls = _rels(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert _has(calls, ".main.conditionInit", ".FlutterWindow.FlutterWindow"), sorted(
        calls
    )


def test_lambda_parameter_counts_as_evidence(
    cpp_vexing_project: Path, mock_ingestor: MagicMock
):
    # A candidate inside a lambda body takes evidence from the lambda's OWN
    # scope, which includes its parameters (Greptile round 4).
    run_updater(cpp_vexing_project, mock_ingestor)
    functions = _functions(mock_ingestor)
    assert not any(qn.endswith(".lwindow") for qn in functions), sorted(functions)
    calls = _rels(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert any(
        d.endswith(".FlutterWindow.FlutterWindow")
        and ".lambdaScope" in s
        for s, d in calls
    ), sorted(calls)


def test_lambda_capture_counts_as_evidence(
    cpp_vexing_project: Path, mock_ingestor: MagicMock
):
    # A captured value (`[kproject]() { ... }`) is in scope inside the
    # lambda body just like a parameter (Greptile round 5).
    run_updater(cpp_vexing_project, mock_ingestor)
    functions = _functions(mock_ingestor)
    assert not any(qn.endswith(".kwindow") for qn in functions), sorted(functions)
    calls = _rels(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert any(
        d.endswith(".FlutterWindow.FlutterWindow") and ".lambdaCapture" in s
        for s, d in calls
    ), sorted(calls)


def test_out_of_scope_names_keep_prototypes(
    cpp_vexing_project: Path, mock_ingestor: MagicMock
):
    # At `FlutterWindow protoA(SomeType);` name lookup finds only the TYPE
    # SomeType: the same-named local declared LATER (laterShadow) or inside
    # a sibling nested block (nestedShadow) is not visible there, so both
    # stay genuine prototypes (Greptile/CodeRabbit round 1: whole-function
    # name collection reclassified them as constructions).
    run_updater(cpp_vexing_project, mock_ingestor)
    calls = _rels(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert not _has(calls, ".main.laterShadow", ".FlutterWindow.FlutterWindow"), sorted(
        calls
    )
    assert not _has(calls, ".main.nestedShadow", ".FlutterWindow.FlutterWindow"), (
        sorted(calls)
    )
