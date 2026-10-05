from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.capture import resolve_capture
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_nodes,
    get_relationships,
    run_updater,
)

# Issue #2615: in C and C++ an elaborated type specifier that only USES a
# struct (`struct Table *metatable;`, `struct stat *st`, `sizeof(struct node)`)
# was indexed as a new Class named after the struct and nested under whatever
# held the use. Lua's C source got five `Table` classes, and every
# self-referential list/tree field grew a phantom `node.node`. A bodyless
# specifier that is the type of a declarator is a reference, never a node.

_LIST_H = """struct node { int v; struct node *next; };
int list_len(const struct node *head);
"""

_LIST_C = """#include "list.h"
#include <sys/stat.h>
int list_len(const struct node *head) {
  int n = 0;
  while (head) { n++; head = head->next; }
  return n;
}
long file_size(struct stat *st) { return (long)st->st_size; }
"""

_WRITER_CPP = """#include <sys/uio.h>
#include <sys/stat.h>
#include <cstddef>
namespace ns {
class Writer {
 public:
  explicit Writer(const struct iovec* iov) : cur_(iov) {}
 private:
  const struct iovec* cur_;
  struct stat* st_;
};
size_t Total(const struct iovec* v, int n);
}  // namespace ns
"""

# Lua's shape: `struct Table` is defined once, in lobject.h, and named by
# pointer fields of other structs in two headers.
_LOBJECT_H = """typedef struct Table {
  int flags;
  struct Table *metatable;
} Table;
typedef struct Udata {
  int len;
  struct Table *metatable;
} Udata;
"""

_LSTATE_H = """#include "lobject.h"
typedef struct global_State {
  struct Table *mt[9];
} global_State;
union GCUnion {
  int gc;
  struct Table h;
};
"""


def _write(project: Path, files: dict[str, str]) -> None:
    project.mkdir()
    for name, source in files.items():
        (project / name).write_text(source, encoding="utf-8")


def _short(qn: str) -> str:
    # Drop the `<project>__<hash>.` prefix so assertions read as the file wrote.
    return qn.split(".", 1)[1]


def _nodes(mock_ingestor: MagicMock, label: str) -> dict[str, dict]:
    return {
        _short(c.args[1]["qualified_name"]): c.args[1]
        for c in get_nodes(mock_ingestor, label)
    }


def _of_type_edges(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (_short(c.args[0][2]), _short(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "OF_TYPE")
    }


def _run_with_types(project: Path, mock_ingestor: MagicMock) -> None:
    # Field and Parameter nodes (and their OF_TYPE edges) are opt-in.
    create_and_run_updater(
        project,
        mock_ingestor,
        capture=resolve_capture(["+fields", "+parameters"]),
    )


def test_issue_c_example_has_only_the_defined_struct(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "celab"
    _write(project, {"list.h": _LIST_H, "list.c": _LIST_C})

    run_updater(project, mock_ingestor)

    # Neither the self-referential `struct node *next` (node.node) nor the
    # libc `struct stat *st` parameter (list.stat) is a class of this project.
    assert set(_nodes(mock_ingestor, "Class")) == {"list.h.node"}


def test_issue_cpp_example_has_only_the_defined_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "io"
    _write(project, {"io.cpp": _WRITER_CPP})

    run_updater(project, mock_ingestor)

    assert set(_nodes(mock_ingestor, "Class")) == {"io.ns.Writer"}


def test_struct_named_by_other_structs_fields_is_one_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "luaish"
    _write(project, {"lobject.h": _LOBJECT_H, "lstate.h": _LSTATE_H})

    run_updater(project, mock_ingestor)

    tables = sorted(
        qn for qn in _nodes(mock_ingestor, "Class") if qn.rsplit(".", 1)[-1] == "Table"
    )
    assert tables == ["lobject.Table"]


@pytest.mark.parametrize(
    ("source", "phantom"),
    [
        pytest.param(
            "long f(void) { struct lconv *lc = 0; return (long)lc; }\n",
            "lconv",
            id="local-variable",
        ),
        pytest.param(
            "unsigned long f(void) { return sizeof(struct stat); }\n",
            "stat",
            id="sizeof",
        ),
        pytest.param(
            "long f(void *p) { return (long)((struct iovec *)p)->iov_len; }\n",
            "iovec",
            id="cast",
        ),
        pytest.param(
            "struct tm *now(void);\n",
            "tm",
            id="return-type",
        ),
        pytest.param(
            "int f(union sigval v);\n",
            "sigval",
            id="union-parameter",
        ),
        pytest.param(
            "extern struct foo bar;\n",
            "foo",
            id="extern-declaration",
        ),
        pytest.param(
            "int f(void) { static struct stat st; return 0; }\n",
            "stat",
            id="static-local",
        ),
        pytest.param(
            "int f(p) struct node *p; { return 0; }\n",
            "node",
            id="knr-parameter",
        ),
    ],
)
def test_elaborated_use_positions_add_no_class(
    temp_repo: Path, mock_ingestor: MagicMock, source: str, phantom: str
) -> None:
    project = temp_repo / "uses"
    _write(project, {"u.c": source})

    run_updater(project, mock_ingestor)

    names = {
        props["name"]
        for label in ("Class", "Union")
        for props in _nodes(mock_ingestor, label).values()
    }
    assert phantom not in names


def test_enum_uses_add_no_enum_node(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "enums"
    _write(
        project,
        {
            "e.c": (
                "enum color { RED, GREEN };\n"
                "struct box { enum color c; };\n"
                "int paint(enum color c) { return c; }\n"
            )
        },
    )

    run_updater(project, mock_ingestor)

    # Before the fix: `e.box.color` for the field and `e.color@3` for the
    # parameter, next to the real `e.color`.
    assert set(_nodes(mock_ingestor, "Enum")) == {"e.color"}


def test_cpp_elaborated_class_use_adds_no_nested_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "k"
    _write(
        project,
        {
            "k.cpp": (
                "class K { public: int k; };\n"
                "class H { class K *kp; };\n"
                "int useK(class K *k) { return k->k; }\n"
            )
        },
    )

    _run_with_types(project, mock_ingestor)

    assert set(_nodes(mock_ingestor, "Class")) == {"k.K", "k.H"}
    assert ("k.H.kp", "k.K") in _of_type_edges(mock_ingestor)


def test_cpp_explicit_instantiation_adds_no_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "tinst"
    _write(
        project,
        {
            "t.cpp": (
                "template <typename T> class Box { T t; };\ntemplate class Box<int>;\n"
            )
        },
    )

    run_updater(project, mock_ingestor)

    # `template class Box<int>;` instantiates the template defined above; it
    # declares no type of its own. Before the fix it added `t.Box<int>`.
    assert set(_nodes(mock_ingestor, "Class")) == {"t.Box"}


# Negative tests: what defines a type keeps doing so, and the uses of a type
# the repo defines keep their edges to it.


def test_struct_definitions_are_still_classes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "defs"
    _write(
        project,
        {
            "d.c": (
                "struct point { int x; int y; };\n"
                "struct rect { int w; } origin;\n"
                "union num { int i; float f; };\n"
                "enum dir { UP, DOWN };\n"
            )
        },
    )

    _run_with_types(project, mock_ingestor)

    # `struct rect {...} origin;` has a declarator AND a body: a definition.
    assert set(_nodes(mock_ingestor, "Class")) == {"d.point", "d.rect"}
    assert set(_nodes(mock_ingestor, "Union")) == {"d.num"}
    assert set(_nodes(mock_ingestor, "Enum")) == {"d.dir"}
    fields = {
        (_short(c.args[0][2]), _short(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "HAS_FIELD")
    }
    assert {("d.point", "d.point.x"), ("d.rect", "d.rect.w")} <= fields


def test_forward_declaration_then_definition_is_one_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "fwd"
    _write(
        project,
        {
            "f.h": "struct node;\nint count(struct node *n);\n",
            "f.c": (
                '#include "f.h"\n'
                "struct node { int v; };\n"
                "int count(struct node *n) { return n ? 1 : 0; }\n"
            ),
        },
    )

    run_updater(project, mock_ingestor)

    nodes = _nodes(mock_ingestor, "Class")
    assert list(nodes) == ["f.node"]
    assert nodes["f.node"]["path"] == "f.c"
    assert nodes["f.node"]["start_line"] == 2


def test_forward_declared_only_struct_keeps_its_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "opaque"
    _write(project, {"o.h": "struct handle;\nint use(struct handle *h);\n"})

    run_updater(project, mock_ingestor)

    # No definition anywhere: the forward declaration is the type's only node,
    # as before. The parameter's use adds nothing next to it.
    assert set(_nodes(mock_ingestor, "Class")) == {"o.handle"}


def test_typedefs_are_unchanged(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "tdefs"
    _write(
        project,
        {
            "t.h": (
                "typedef struct TT {\n"
                "  int b;\n"
                "} TT_t;\n"
                "typedef struct opq opq_t;\n"
                "typedef struct lua_State lua_State;\n"
                "struct lua_State { int top; };\n"
            )
        },
    )

    run_updater(project, mock_ingestor)

    nodes = _nodes(mock_ingestor, "Class")
    # A bodied typedef is the definition; a bodyless typedef of a type the
    # repo never defines is C's opaque handle and keeps its node; one whose
    # struct is defined adds nothing beside the definition.
    assert set(nodes) == {"t.TT", "t.opq", "t.lua_State"}
    assert (nodes["t.TT"]["start_line"], nodes["t.TT"]["end_line"]) == (1, 3)
    assert nodes["t.lua_State"]["start_line"] == 6


def test_cpp_class_uses_are_unchanged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "cls"
    _write(
        project,
        {
            "c.cpp": (
                "namespace app {\n"
                "class Engine { public: int rpm; };\n"
                "class Car {\n"
                "  class Wheel;\n"
                "  Engine *engine_;\n"
                "  Engine spare_;\n"
                "};\n"
                "int drive(Engine *e) { return e->rpm; }\n"
                "}  // namespace app\n"
            )
        },
    )

    _run_with_types(project, mock_ingestor)

    # `class Wheel;` in a class body has no declarator: a forward declaration
    # of a type defined nowhere else, so it keeps its node as before.
    assert set(_nodes(mock_ingestor, "Class")) == {
        "c.app.Engine",
        "c.app.Car",
        "c.app.Car.Wheel",
    }
    assert {
        ("c.app.Car.engine_", "c.app.Engine"),
        ("c.app.Car.spare_", "c.app.Engine"),
        ("c.app.drive.0", "c.app.Engine"),
    } <= _of_type_edges(mock_ingestor)


def _links_project(temp_repo: Path, mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    project = temp_repo / "links"
    _write(
        project,
        {
            "lobject.h": _LOBJECT_H,
            "lstate.h": _LSTATE_H,
            "list.c": (
                "struct node { int v; struct node *next; };\n"
                "int push(struct node *head, int v) { return head->v + v; }\n"
            ),
        },
    )
    _run_with_types(project, mock_ingestor)
    return _of_type_edges(mock_ingestor)


def test_uses_of_a_defined_struct_still_link_to_it(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    edges = _links_project(temp_repo, mock_ingestor)

    assert {
        ("list.node.next", "list.node"),
        ("list.push.0", "list.node"),
        ("lobject.Table.metatable", "lobject.Table"),
        ("lobject.Udata.metatable", "lobject.Table"),
    } <= edges


def test_field_types_land_on_the_real_struct_not_a_phantom(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    edges = _links_project(temp_repo, mock_ingestor)

    # Before the fix both fields typed as the phantom `global_State.Table`
    # minted from `struct Table *mt[9];`, not lobject.h's definition.
    assert {
        ("lstate.global_State.mt", "lobject.Table"),
        ("lstate.GCUnion.h", "lobject.Table"),
    } <= edges
    written = {
        _short(c.args[1]["qualified_name"])
        for label in ("Class", "Union", "Enum")
        for c in get_nodes(mock_ingestor, label)
    }
    assert {target for _, target in edges} <= written
