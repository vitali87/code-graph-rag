from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.parsers.cpp_frontend import cpp_frontend_available, run_cpp_frontend
from codebase_rag.tests.conftest import get_nodes, get_qualified_names

pytestmark = pytest.mark.skipif(
    not cpp_frontend_available(),
    reason="libclang not available",
)

# C++ type aliases: namespace-scoped `using`/`typedef` and a class-scoped
# member alias. The tree-sitter path emits no Type nodes for these, so the
# frontend adds them (mirroring how Go/Rust type decls become Type nodes).
_HEADER = """
namespace n {

using Meters = double;
typedef int Count;

class Box {
public:
    using Handle = int;
};

}  // namespace n
"""

_SRC = '#include "types.h"\n'


def _write(root: Path) -> None:
    root.mkdir()
    (root / "types.h").write_text(_HEADER, encoding="utf-8")
    (root / "types.cpp").write_text(_SRC, encoding="utf-8")
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "arguments": ["c++", "-std=c++17", str(root / "types.cpp")],
                    "file": str(root / "types.cpp"),
                }
            ]
        ),
        encoding="utf-8",
    )


def test_frontend_emits_type_aliases(temp_repo: Path) -> None:
    root = temp_repo / "typesproj"
    _write(root)

    ingestor = MagicMock()
    run_cpp_frontend(ingestor, root, root.name, root)

    types = get_qualified_names(get_nodes(ingestor, "Type"))
    assert any(q.endswith(".n.Meters") for q in types), f"missing using alias: {types}"
    assert any(q.endswith(".n.Count") for q in types), f"missing typedef: {types}"
    assert any(q.endswith(".n.Box.Handle") for q in types), (
        f"missing class-scoped alias: {types}"
    )

    defines = [
        (c.args[0][0], c.args[0][2], c.args[2][2])
        for c in ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == "DEFINES"
    ]
    # namespace-scoped alias defined by its Module; member alias by its Class.
    assert any(
        src_label == "Module" and child.endswith(".n.Meters")
        for src_label, _, child in defines
    ), f"Module should DEFINE Meters: {defines}"
    assert any(
        src_label == "Class"
        and src_qn.endswith(".n.Box")
        and child.endswith(".n.Box.Handle")
        for src_label, src_qn, child in defines
    ), f"Box should DEFINE Handle: {defines}"
