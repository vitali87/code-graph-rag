from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.parsers.cpp_frontend import cpp_frontend_available, run_cpp_frontend
from codebase_rag.tests.conftest import get_nodes, get_qualified_names, run_updater

pytestmark = pytest.mark.skipif(
    not cpp_frontend_available(),
    reason="libclang not available",
)

# A macro-free C++ corpus: a namespaced class declared in a header with
# in-class declarations + one inline method, its out-of-line definitions in
# the .cpp, a free-function prototype in the header, and free-function
# definitions in the .cpp. Macro-free so the tree-sitter path parses it
# correctly and its qualified names are the ground truth the libclang
# frontend must reproduce exactly (the issue #46 acceptance test).
HEADER = """
namespace geo {

class Shape {
public:
    Shape(double x);
    virtual ~Shape();
    double area() const;
    virtual void describe();
    int inline_helper() { return 7; }
};

int free_proto(int n);

static int static_proto(int n);

}  // namespace geo
"""

SRC = """
#include "geometry.h"

namespace geo {

Shape::Shape(double x) {}
Shape::~Shape() {}
double Shape::area() const { return 1.0; }
void Shape::describe() {}

int free_proto(int n) { return n + 1; }

int only_in_cpp(int a) { return a; }

int static_proto(int n) { return n; }

}  // namespace geo
"""

_LABELS = ("Class", "Function", "Method")


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "geometry.h").write_text(HEADER, encoding="utf-8")
    (root / "geometry.cpp").write_text(SRC, encoding="utf-8")
    compile_commands = [
        {
            "directory": str(root),
            "arguments": ["c++", "-std=c++17", str(root / "geometry.cpp")],
            "file": str(root / "geometry.cpp"),
        }
    ]
    (root / "compile_commands.json").write_text(
        json.dumps(compile_commands), encoding="utf-8"
    )


def _qns_by_label(ingestor: MagicMock) -> dict[str, set[str]]:
    return {label: get_qualified_names(get_nodes(ingestor, label)) for label in _LABELS}


def test_frontend_qns_match_tree_sitter(temp_repo: Path) -> None:
    root = temp_repo / "geomproj"
    _write_project(root)

    ts_ingestor = MagicMock()
    run_updater(root, ts_ingestor)
    ts_qns = _qns_by_label(ts_ingestor)

    fe_ingestor = MagicMock()
    run_cpp_frontend(fe_ingestor, root, root.name, root)
    fe_qns = _qns_by_label(fe_ingestor)

    assert fe_qns == ts_qns, (
        f"frontend/tree-sitter qn mismatch:\n"
        f"  frontend only: { {k: fe_qns[k] - ts_qns[k] for k in _LABELS} }\n"
        f"  tree-sitter only: { {k: ts_qns[k] - fe_qns[k] for k in _LABELS} }"
    )


def _write_cpp_project(root: Path, header_name: str, header: str, src: str) -> None:
    root.mkdir()
    cpp_name = f"{Path(header_name).stem}.cpp"
    (root / header_name).write_text(header, encoding="utf-8")
    (root / cpp_name).write_text(src, encoding="utf-8")
    compile_commands = [
        {
            "directory": str(root),
            "arguments": ["c++", "-std=c++17", str(root / cpp_name)],
            "file": str(root / cpp_name),
        }
    ]
    (root / "compile_commands.json").write_text(
        json.dumps(compile_commands), encoding="utf-8"
    )


# A macro that tree-sitter cannot expand: `struct WIDGET_API Widget` is
# mis-parsed (WIDGET_API is read as the type), so cgr loses the `Widget`
# class entirely. libclang expands the macro and recovers it with its true
# multi-line span. This is the whole reason the frontend exists.
_MACRO_HEADER = """
#define WIDGET_API

namespace ui {

struct WIDGET_API Widget {
    int handle;
    void show();
    void hide();
};

}  // namespace ui
"""

_MACRO_SRC = """
#include "widget.h"
namespace ui {
void Widget::show() {}
void Widget::hide() {}
}
"""


def test_frontend_recovers_macro_mangled_class(temp_repo: Path) -> None:
    root = temp_repo / "macroproj"
    _write_cpp_project(root, "widget.h", _MACRO_HEADER, _MACRO_SRC)

    ts_ingestor = MagicMock()
    run_updater(root, ts_ingestor)
    ts_classes = get_qualified_names(get_nodes(ts_ingestor, "Class"))

    fe_ingestor = MagicMock()
    run_cpp_frontend(fe_ingestor, root, root.name, root)
    fe_class_nodes = get_nodes(fe_ingestor, "Class")
    fe_classes = get_qualified_names(fe_class_nodes)

    # tree-sitter loses Widget to the macro; the frontend recovers it.
    assert not any(q.endswith(".ui.Widget") for q in ts_classes), (
        f"expected tree-sitter to mis-parse Widget, got {ts_classes}"
    )
    assert any(q.endswith(".ui.Widget") for q in fe_classes), (
        f"frontend did not recover Widget: {fe_classes}"
    )

    widget = next(
        c[0][1] for c in fe_class_nodes if c[0][1]["qualified_name"].endswith(".Widget")
    )
    assert widget["end_line"] > widget["start_line"], (
        f"expected a real multi-line span for Widget, got {widget}"
    )


_INHERIT_HEADER = """
namespace geo {

class Base {
public:
    virtual void run();
};

class Derived : public Base {
public:
    void run();
    Derived operator+(const Derived& o) const;
};

}  // namespace geo
"""

_INHERIT_SRC = """
#include "shapes.h"
namespace geo {
void Base::run() {}
void Derived::run() {}
Derived Derived::operator+(const Derived& o) const { return *this; }
}
"""


def test_frontend_emits_inheritance_and_operator(temp_repo: Path) -> None:
    root = temp_repo / "shapesproj"
    _write_cpp_project(root, "shapes.h", _INHERIT_HEADER, _INHERIT_SRC)

    fe_ingestor = MagicMock()
    run_cpp_frontend(fe_ingestor, root, root.name, root)

    methods = get_qualified_names(get_nodes(fe_ingestor, "Method"))
    assert any(q.endswith(".geo.Derived.operator_plus") for q in methods), (
        f"operator+ not converted: {sorted(methods)}"
    )

    inherits = [
        (c.args[0][2], c.args[2][2])
        for c in fe_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == "INHERITS"
    ]
    assert any(
        src.endswith(".geo.Derived") and dst.endswith(".Base") for src, dst in inherits
    ), f"expected Derived INHERITS Base, got {inherits}"
