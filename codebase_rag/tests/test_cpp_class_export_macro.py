"""Issue #2840: a macro between `class` and the name does not rename the class.

`class SPDLOG_API Logger {...}`, the export/visibility idiom of spdlog, Qt
(`Q_CORE_EXPORT`), googletest (`GTEST_API_`) and abseil (`ABSL_DLL`), has no
grammar production without the macro's #define, so tree-sitter read it as a
function named `Logger` returning `class SPDLOG_API`: the class was named
after the macro, its methods were lost or became free functions, and every
class so marked in one file collapsed into one node (spdlog's `logger.h`: 46
Functions, 0 Classes).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

SOURCE = """\
#define SPDLOG_API __attribute__((visibility("default")))
#define MYAPI

class SPDLOG_API Logger {
public:
    void info() { write(); }
    void write() {}
};

class MYAPI Alpha { public: int a() { return 1; } };
class MYAPI Beta { public: int b() { return 2; } };

class Base { public: virtual int v() { return 0; } };
struct MYAPI Point : public Base { int x() { return v(); } };

class PlainClass {
public:
    void run() {}
};

struct Pair { int first; };
struct Pair make_pair() { struct Pair p; return p; }

class alignas(8) Aligned { public: int z() { return 3; } };
"""


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("macro") / "cppmac"
    _write(root, "a.cpp", SOURCE)
    return _index(root, MagicMock())


def _labels(graph: RecordedGraph) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        qn.removeprefix(prefix): str(props.get("label"))
        for qn, props in graph.nodes.items()
        if qn.startswith(prefix)
    }


def _edges(graph: RecordedGraph, rel: str) -> set[tuple[str, str]]:
    prefix = f"{graph.project}."
    return {
        (src.removeprefix(prefix), dst.removeprefix(prefix))
        for src, kind, dst, _props in graph.edges
        if kind == rel
    }


@pytest.mark.parametrize(
    ("qn", "label"),
    [
        ("a.Logger", "Class"),
        ("a.Logger.info", "Method"),
        ("a.Logger.write", "Method"),
        ("a.Alpha", "Class"),
        ("a.Alpha.a", "Method"),
        ("a.Beta", "Class"),
        ("a.Beta.b", "Method"),
        ("a.Point", "Class"),
        ("a.Point.x", "Method"),
    ],
)
def test_a_macro_prefixed_class_keeps_its_name_and_members(
    graph: RecordedGraph, qn: str, label: str
) -> None:
    assert _labels(graph).get(qn) == label


def test_no_node_is_named_after_the_macro(graph: RecordedGraph) -> None:
    assert not [qn for qn in _labels(graph) if "SPDLOG_API" in qn or "MYAPI" in qn]


def test_the_class_body_calls_and_bases_are_kept(graph: RecordedGraph) -> None:
    assert ("a.Logger.info", "a.Logger.write") in _edges(graph, "CALLS")
    assert ("a.Point", "a.Base") in _edges(graph, "INHERITS")


# Negative: what must not change.


@pytest.mark.parametrize(
    ("qn", "label"),
    [
        ("a.PlainClass", "Class"),
        ("a.PlainClass.run", "Method"),
        ("a.make_pair", "Function"),
        ("a.Aligned", "Class"),
        ("a.Aligned.z", "Method"),
    ],
    ids=[
        "plain-class",
        "plain-method",
        "function-returning-a-struct",
        "alignas",
        "alignas-member",
    ],
)
def test_well_formed_code_is_read_as_before(
    graph: RecordedGraph, qn: str, label: str
) -> None:
    assert _labels(graph).get(qn) == label
