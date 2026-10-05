from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.parsers.cpp_frontend import (
    cpp_frontend_available,
    run_cpp_frontend,
    run_cpp_frontend_hybrid,
)
from codebase_rag.tests.conftest import get_nodes, get_relationships
from codebase_rag.types_defs import PropertyDict

pytestmark = pytest.mark.skipif(
    not cpp_frontend_available(),
    reason="libclang not available",
)

# Issue #2943: a compile database entry's paths are relative to its own
# `directory` (the JSON Compilation Database spec), which is how
# `bear -- make` writes them. The libclang pass resolved them against the
# PROCESS cwd instead, so indexing with --repo-path from anywhere else failed
# every TU to load and silently dropped every libclang fact.
_GEOMETRY_H = """\
#ifndef GEOMETRY_H
#define GEOMETRY_H
#define AREA(w, h) ((w) * (h))
int rect_area(int w, int h);
#endif
"""

_SCALE_H = "#define SCALE 2\n"

_GEOMETRY_SRC = """\
#include "geometry.h"
#include "scale.h"
int rect_area(int w, int h) { return AREA(w, h) * SCALE; }
"""


def _entry(directory: Path, source: str, *flags: str) -> dict[str, str | list[str]]:
    return {
        "directory": str(directory),
        "file": source,
        "arguments": ["c++", "-std=c++17", *flags, "-c", source],
    }


def _write_geometry(root: Path, relative: bool) -> None:
    # `-Iinc` is relative too: scale.h is only reachable through it.
    root.mkdir()
    (root / "inc").mkdir()
    (root / "geometry.h").write_text(_GEOMETRY_H, encoding="utf-8")
    (root / "inc" / "scale.h").write_text(_SCALE_H, encoding="utf-8")
    (root / "geometry.cpp").write_text(_GEOMETRY_SRC, encoding="utf-8")
    if relative:
        entry = _entry(root, "geometry.cpp", "-Iinc")
    else:
        entry = _entry(root, str(root / "geometry.cpp"), f"-I{root / 'inc'}")
    (root / "compile_commands.json").write_text(json.dumps([entry]), encoding="utf-8")


def _functions(ingestor: MagicMock) -> dict[str, PropertyDict]:
    return {
        c.args[1][cs.KEY_QUALIFIED_NAME]: c.args[1]
        for c in get_nodes(ingestor, cs.NodeLabel.FUNCTION)
    }


def _imports(ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(ingestor, cs.RelationshipType.IMPORTS)
    }


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # cgr invoked with --repo-path from a directory unrelated to the repo
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


@pytest.mark.parametrize("relative", [True, False], ids=["bear-style", "absolute"])
def test_hybrid_relative_compdb_entry_indexes_from_another_cwd(
    temp_repo: Path, elsewhere: Path, relative: bool
) -> None:
    root = temp_repo / "cppcwd"
    _write_geometry(root, relative)
    ingestor = MagicMock()
    pending, _ = run_cpp_frontend_hybrid(ingestor, root, root.name, root)

    functions = _functions(ingestor)
    assert "cppcwd.geometry.h.AREA" in functions, sorted(functions)
    # reached only through the relative -Iinc
    assert "cppcwd.inc.scale.SCALE" in functions, sorted(functions)
    assert functions["cppcwd.geometry.h.AREA"][cs.KEY_ABSOLUTE_PATH] == (
        (root / "geometry.h").resolve().as_posix()
    )
    assert {p.callee_qn for p in pending} == {
        "cppcwd.geometry.h.AREA",
        "cppcwd.inc.scale.SCALE",
    }, pending
    assert all(p.rel_path == "geometry.cpp" for p in pending), pending
    imports = _imports(ingestor)
    assert ("cppcwd.geometry", "cppcwd.geometry.h") in imports, sorted(imports)
    assert ("cppcwd.geometry", "cppcwd.inc.scale") in imports, sorted(imports)
    module_paths = {
        c.args[1][cs.KEY_QUALIFIED_NAME]: c.args[1][cs.KEY_ABSOLUTE_PATH]
        for c in get_nodes(ingestor, cs.NodeLabel.MODULE)
    }
    assert module_paths["cppcwd.geometry"] == (
        (root / "geometry.cpp").resolve().as_posix()
    )


def test_libclang_relative_compdb_entry_indexes_from_another_cwd(
    temp_repo: Path, elsewhere: Path
) -> None:
    root = temp_repo / "cpppure"
    _write_geometry(root, relative=True)
    ingestor = MagicMock()
    covered = run_cpp_frontend(ingestor, root, root.name, root)

    assert "geometry.cpp" in covered, sorted(covered)
    functions = _functions(ingestor)
    assert "cpppure.geometry.rect_area" in functions, sorted(functions)
    assert functions["cpppure.geometry.rect_area"][cs.KEY_ABSOLUTE_PATH] == (
        (root / "geometry.cpp").resolve().as_posix()
    )
    calls = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(ingestor, cs.RelationshipType.CALLS)
    }
    assert ("cpppure.geometry.rect_area", "cpppure.geometry.h.AREA") in calls, sorted(
        calls
    )


def test_same_relative_name_in_two_directories_stays_two_files(
    temp_repo: Path, elsewhere: Path
) -> None:
    # Each TU spells its own files `main.cpp` and `./local.h`: resolved per
    # entry directory, a's include must not be taken for b's.
    root = temp_repo / "twodirs"
    root.mkdir()
    entries = []
    for name in ("a", "b"):
        sub = root / name
        sub.mkdir()
        (sub / "local.h").write_text(
            f"#define {name.upper()}_LIMIT 1\n", encoding="utf-8"
        )
        (sub / "main.cpp").write_text(
            f'#include "local.h"\nint {name}_main() {{ return {name.upper()}_LIMIT; }}\n',
            encoding="utf-8",
        )
        entries.append(_entry(sub, "main.cpp"))
    (root / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
    ingestor = MagicMock()
    pending, _ = run_cpp_frontend_hybrid(ingestor, root, root.name, root)

    imports = _imports(ingestor)
    assert ("twodirs.a.main", "twodirs.a.local") in imports, sorted(imports)
    assert ("twodirs.b.main", "twodirs.b.local") in imports, sorted(imports)
    assert ("twodirs.b.main", "twodirs.a.local") not in imports, sorted(imports)
    assert {(p.rel_path, p.callee_qn) for p in pending} == {
        ("a/main.cpp", "twodirs.a.local.A_LIMIT"),
        ("b/main.cpp", "twodirs.b.local.B_LIMIT"),
    }, pending


def test_unloadable_translation_unit_is_reported_not_silent(
    temp_repo: Path, elsewhere: Path
) -> None:
    # A TU that genuinely cannot load (its source is gone) still skips, but
    # says which file it skipped instead of reporting success.
    root = temp_repo / "cppgone"
    _write_geometry(root, relative=True)
    (root / "geometry.cpp").unlink()
    messages: list[str] = []
    handler = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        pending, _ = run_cpp_frontend_hybrid(MagicMock(), root, root.name, root)
    finally:
        logger.remove(handler)
    assert pending == []
    assert any("geometry.cpp" in m for m in messages), messages
