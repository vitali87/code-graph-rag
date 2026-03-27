"""Tests for C++ cross-file out-of-class method resolution (issue #496).

When a class is declared in a header (.h) and methods are implemented
out-of-class in a source file (.cpp) using ``ClassName::method`` syntax,
the Method nodes must link back to the correct Class node via
DEFINES_METHOD edges -- not to a phantom class constructed from the
.cpp module's qualified name.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.constants import SEPARATOR_DOT
from codebase_rag.tests.conftest import (
    get_nodes,
    get_relationships,
    run_updater,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_method_qns(mock_ingestor: MagicMock) -> set[str]:
    """Return all Method qualified names recorded in the ingestor."""
    return {call[0][1]["qualified_name"] for call in get_nodes(mock_ingestor, "Method")}


def _get_class_qns(mock_ingestor: MagicMock) -> set[str]:
    """Return all Class qualified names recorded in the ingestor."""
    return {call[0][1]["qualified_name"] for call in get_nodes(mock_ingestor, "Class")}


def _get_defines_method_edges(
    mock_ingestor: MagicMock,
) -> list[tuple[str, str]]:
    """Return ``(class_qn, method_qn)`` pairs from DEFINES_METHOD rels."""
    edges: list[tuple[str, str]] = []
    for rel in get_relationships(mock_ingestor, "DEFINES_METHOD"):
        class_qn = rel.args[0][2]
        method_qn = rel.args[2][2]
        edges.append((class_qn, method_qn))
    return edges


def _method_names_for_class(mock_ingestor: MagicMock, class_name: str) -> set[str]:
    """Method simple-names linked via DEFINES_METHOD to *class_name*."""
    names: set[str] = set()
    for class_qn, method_qn in _get_defines_method_edges(mock_ingestor):
        parts = class_qn.split(SEPARATOR_DOT)
        if class_name in parts:
            names.add(method_qn.split(SEPARATOR_DOT)[-1])
    return names


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cpp_cross_file_project(temp_repo: Path) -> Path:
    project = temp_repo / "cpp_cross_file"
    project.mkdir()
    return project


# ---------------------------------------------------------------------------
# Test: basic header + source cross-file methods
# ---------------------------------------------------------------------------


def test_header_source_method_resolution(
    cpp_cross_file_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Class in .h, implementations in .cpp -- methods must link to .h class."""
    include = cpp_cross_file_project / "include"
    include.mkdir()
    src = cpp_cross_file_project / "src"
    src.mkdir()

    (include / "Calculator.h").write_text(
        encoding="utf-8",
        data="""\
#pragma once

class Calculator {
public:
    int add(int a, int b);
    int subtract(int a, int b);
    double divide(int a, int b);
};
""",
    )

    (src / "Calculator.cpp").write_text(
        encoding="utf-8",
        data="""\
#include "Calculator.h"

int Calculator::add(int a, int b) {
    return a + b;
}

int Calculator::subtract(int a, int b) {
    return a - b;
}

double Calculator::divide(int a, int b) {
    if (b == 0) return 0;
    return static_cast<double>(a) / b;
}
""",
    )

    run_updater(cpp_cross_file_project, mock_ingestor)

    # The class should exist in the header module.
    class_qns = _get_class_qns(mock_ingestor)
    header_class = [qn for qn in class_qns if "include" in qn and "Calculator" in qn]
    assert header_class, (
        f"Expected a Calculator class in include/, got classes: {class_qns}"
    )

    # All three out-of-class methods should have DEFINES_METHOD edges
    # pointing to the *header* class, not to a phantom class in src/.
    edges = _get_defines_method_edges(mock_ingestor)
    header_class_qn = header_class[0]
    methods_linked_to_header = {
        mq.split(SEPARATOR_DOT)[-1] for cq, mq in edges if cq == header_class_qn
    }

    assert "add" in methods_linked_to_header, (
        f"'add' not linked to header class. Edges: {edges}"
    )
    assert "subtract" in methods_linked_to_header, (
        f"'subtract' not linked to header class. Edges: {edges}"
    )
    assert "divide" in methods_linked_to_header, (
        f"'divide' not linked to header class. Edges: {edges}"
    )

    # There should be NO orphan Method nodes (methods whose container_qn
    # uses the .cpp module instead of the .h module).
    method_qns = _get_method_qns(mock_ingestor)
    orphan_methods = {
        qn
        for qn in method_qns
        if "src.Calculator" in qn and "Calculator.Calculator" in qn
    }
    assert not orphan_methods, (
        f"Found orphan methods with .cpp module QN: {orphan_methods}"
    )


# ---------------------------------------------------------------------------
# Test: multiple source files implementing one header class
# ---------------------------------------------------------------------------


def test_multiple_source_files_one_class(
    cpp_cross_file_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Two .cpp files implement methods of one class declared in .h."""
    include = cpp_cross_file_project / "include"
    include.mkdir()
    src = cpp_cross_file_project / "src"
    src.mkdir()

    (include / "Engine.h").write_text(
        encoding="utf-8",
        data="""\
#pragma once

class Engine {
public:
    void start();
    void stop();
    void accelerate(int speed);
    void brake();
};
""",
    )

    (src / "engine_control.cpp").write_text(
        encoding="utf-8",
        data="""\
#include "Engine.h"

void Engine::start() { /* ... */ }
void Engine::stop() { /* ... */ }
""",
    )

    (src / "engine_movement.cpp").write_text(
        encoding="utf-8",
        data="""\
#include "Engine.h"

void Engine::accelerate(int speed) { /* ... */ }
void Engine::brake() { /* ... */ }
""",
    )

    run_updater(cpp_cross_file_project, mock_ingestor)

    class_qns = _get_class_qns(mock_ingestor)
    header_classes = [qn for qn in class_qns if "include" in qn and "Engine" in qn]
    assert header_classes, f"Expected Engine class in include/, got: {class_qns}"
    header_class_qn = header_classes[0]

    edges = _get_defines_method_edges(mock_ingestor)
    methods_linked = {
        mq.split(SEPARATOR_DOT)[-1] for cq, mq in edges if cq == header_class_qn
    }

    for method_name in ("start", "stop", "accelerate", "brake"):
        assert method_name in methods_linked, (
            f"'{method_name}' not linked to header Engine class. "
            f"Linked methods: {methods_linked}"
        )


# ---------------------------------------------------------------------------
# Test: constructor and destructor out-of-class across files
# ---------------------------------------------------------------------------


def test_cross_file_constructor_destructor(
    cpp_cross_file_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Constructors and destructors implemented in .cpp link to .h class."""
    include = cpp_cross_file_project / "include"
    include.mkdir()
    src = cpp_cross_file_project / "src"
    src.mkdir()

    (include / "Resource.h").write_text(
        encoding="utf-8",
        data="""\
#pragma once

class Resource {
public:
    Resource();
    Resource(int size);
    ~Resource();
    void reset();
private:
    int* data_;
};
""",
    )

    (src / "Resource.cpp").write_text(
        encoding="utf-8",
        data="""\
#include "Resource.h"

Resource::Resource() : data_(nullptr) {}

Resource::Resource(int size) {
    data_ = new int[size];
}

Resource::~Resource() {
    delete[] data_;
}

void Resource::reset() {
    delete[] data_;
    data_ = nullptr;
}
""",
    )

    run_updater(cpp_cross_file_project, mock_ingestor)

    class_qns = _get_class_qns(mock_ingestor)
    header_classes = [qn for qn in class_qns if "include" in qn and "Resource" in qn]
    assert header_classes, f"Expected Resource class in include/, got: {class_qns}"
    header_class_qn = header_classes[0]

    edges = _get_defines_method_edges(mock_ingestor)
    methods_linked = {
        mq.split(SEPARATOR_DOT)[-1] for cq, mq in edges if cq == header_class_qn
    }

    assert "Resource" in methods_linked, (
        f"Constructor not linked to header class. Methods: {methods_linked}"
    )
    assert "~Resource" in methods_linked, (
        f"Destructor not linked to header class. Methods: {methods_linked}"
    )
    assert "reset" in methods_linked, (
        f"'reset' not linked to header class. Methods: {methods_linked}"
    )


# ---------------------------------------------------------------------------
# Test: nested namespace cross-file methods
# ---------------------------------------------------------------------------


def test_nested_namespace_cross_file(
    cpp_cross_file_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Class inside nested namespaces, methods implemented in separate .cpp."""
    include = cpp_cross_file_project / "include"
    include.mkdir()
    src = cpp_cross_file_project / "src"
    src.mkdir()

    (include / "Logger.h").write_text(
        encoding="utf-8",
        data="""\
#pragma once

namespace app {
namespace logging {

class Logger {
public:
    void info(const char* msg);
    void error(const char* msg);
};

}  // namespace logging
}  // namespace app
""",
    )

    (src / "Logger.cpp").write_text(
        encoding="utf-8",
        data="""\
#include "Logger.h"

namespace app {
namespace logging {

void Logger::info(const char* msg) { /* ... */ }
void Logger::error(const char* msg) { /* ... */ }

}  // namespace logging
}  // namespace app
""",
    )

    run_updater(cpp_cross_file_project, mock_ingestor)

    class_qns = _get_class_qns(mock_ingestor)
    header_classes = [qn for qn in class_qns if "include" in qn and "Logger" in qn]
    assert header_classes, f"Expected Logger class in include/, got: {class_qns}"
    header_class_qn = header_classes[0]

    edges = _get_defines_method_edges(mock_ingestor)
    methods_linked = {
        mq.split(SEPARATOR_DOT)[-1] for cq, mq in edges if cq == header_class_qn
    }

    assert "info" in methods_linked, (
        f"'info' not linked to header Logger. Methods: {methods_linked}"
    )
    assert "error" in methods_linked, (
        f"'error' not linked to header Logger. Methods: {methods_linked}"
    )


# ---------------------------------------------------------------------------
# Test: no orphan methods remain (aggregate check)
# ---------------------------------------------------------------------------


def test_no_orphan_methods_across_files(
    cpp_cross_file_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Every Method node must have at least one incoming DEFINES_METHOD edge."""
    include = cpp_cross_file_project / "include"
    include.mkdir()
    src = cpp_cross_file_project / "src"
    src.mkdir()

    (include / "Widget.h").write_text(
        encoding="utf-8",
        data="""\
#pragma once

class Widget {
public:
    void draw();
    void resize(int w, int h);
    void hide();
};
""",
    )

    (src / "Widget.cpp").write_text(
        encoding="utf-8",
        data="""\
#include "Widget.h"

void Widget::draw() { /* ... */ }
void Widget::resize(int w, int h) { /* ... */ }
void Widget::hide() { /* ... */ }
""",
    )

    run_updater(cpp_cross_file_project, mock_ingestor)

    method_qns = _get_method_qns(mock_ingestor)
    edges = _get_defines_method_edges(mock_ingestor)
    methods_with_edges = {mq for _, mq in edges}

    orphans = method_qns - methods_with_edges
    # Filter to only methods belonging to Widget (other methods from inline
    # definitions always have edges).
    widget_orphans = {qn for qn in orphans if "Widget" in qn}
    assert not widget_orphans, (
        f"Found orphan Widget Method nodes with no DEFINES_METHOD edge: "
        f"{widget_orphans}"
    )


# ---------------------------------------------------------------------------
# Test: same-file out-of-class still works (regression)
# ---------------------------------------------------------------------------


def test_same_file_out_of_class_still_works(
    cpp_cross_file_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """When class and implementations are in the same .cpp, nothing breaks."""
    (cpp_cross_file_project / "single.cpp").write_text(
        encoding="utf-8",
        data="""\
class Foo {
public:
    void bar();
    int baz(int x);
};

void Foo::bar() { /* ... */ }
int Foo::baz(int x) { return x; }
""",
    )

    run_updater(cpp_cross_file_project, mock_ingestor)

    method_names = _method_names_for_class(mock_ingestor, "Foo")
    assert "bar" in method_names, f"Expected 'bar', got: {method_names}"
    assert "baz" in method_names, f"Expected 'baz', got: {method_names}"
