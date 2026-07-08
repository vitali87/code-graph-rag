# (H) IMPORTS edges were emitted straight from the import map's parse-time
# (H) guesses, so an internal-looking target that maps to no real module (a
# (H) broken import, a directory, a crate path resolved from the wrong root, a
# (H) specifier with an explicit .js extension, a C++20 module declaration
# (H) registering itself) produced an edge the database silently drops (issue
# (H) #652: 51 across the fixture suite). Emission is now deferred until every
# (H) file is parsed and verified against the real module qns; an internal
# (H) target that resolves nowhere emits no edge.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater


def _import_targets(mock_ingestor: MagicMock, from_qn: str) -> set[str]:
    return {
        call.args[2][2]
        for call in get_relationships(mock_ingestor, cs.RelationshipType.IMPORTS.value)
        if call.args[0][2] == from_qn
    }


def _node_keys(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0]), c.args[1].get("qualified_name"))
        for c in mock_ingestor.ensure_node_batch.call_args_list
    }


def _assert_no_dangling_imports(mock_ingestor: MagicMock) -> None:
    node_keys = _node_keys(mock_ingestor)
    for call in get_relationships(mock_ingestor, cs.RelationshipType.IMPORTS.value):
        to_label, _, to_qn = call.args[2]
        assert (str(to_label), to_qn) in node_keys, call.args


def test_python_broken_import_emits_no_phantom_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    pkg = temp_repo / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "real.py").write_text("VALUE = 1\n")
    (pkg / "main.py").write_text(
        "from app.real import VALUE\nfrom app.missing_module import Thing\n"
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    targets = _import_targets(mock_ingestor, f"{project}.app.main")
    assert f"{project}.app.real" in targets, targets
    _assert_no_dangling_imports(mock_ingestor)


def test_js_explicit_extension_resolves_to_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "b.js").write_text("export function createB() { return 2; }\n")
    (temp_repo / "a.js").write_text(
        "import { createB } from './b.js';\nexport function runA() { return createB(); }\n"
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    targets = _import_targets(mock_ingestor, f"{project}.a")
    assert f"{project}.b" in targets, targets
    _assert_no_dangling_imports(mock_ingestor)

    # (H) The item mapping must also drop the extension or calls to createB
    # (H) resolve against a phantom qn.
    calls = {
        (call.args[0][2], call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
    }
    assert (f"{project}.a.runA", f"{project}.b.createB") in calls, calls


def test_js_directory_import_resolves_to_index_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    shared = temp_repo / "shared"
    shared.mkdir()
    (shared / "index.js").write_text("export const config = {};\n")
    (temp_repo / "app.js").write_text("import { config } from './shared';\n")
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    targets = _import_targets(mock_ingestor, f"{project}.app")
    assert f"{project}.shared.index" in targets, targets
    _assert_no_dangling_imports(mock_ingestor)


def test_rust_crate_import_resolves_to_real_module_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    src = temp_repo / "src"
    (src / "utils").mkdir(parents=True)
    (src / "lib.rs").write_text("pub mod utils;\n")
    (src / "utils" / "mod.rs").write_text("pub fn helper() -> i32 { 42 }\n")
    (temp_repo / "tool.rs").write_text(
        "use crate::utils::helper;\nfn main() { let _ = helper(); }\n"
    )
    run_updater(temp_repo, mock_ingestor)

    _assert_no_dangling_imports(mock_ingestor)


def test_cpp_module_declarations_emit_no_self_import(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "my_export.cppm").write_text(
        """
export module my_export_module;

export int answer() { return 42; }
"""
    )
    run_updater(temp_repo, mock_ingestor)

    _assert_no_dangling_imports(mock_ingestor)
    project = temp_repo.name
    targets = _import_targets(mock_ingestor, f"{project}.my_export")
    assert f"{project}.my_export_module" not in targets, targets


def test_cpp_module_impl_without_interface_emits_no_phantom(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "orphan_impl.cpp").write_text(
        """
module lonely_module;

int helper() { return 1; }
"""
    )
    run_updater(temp_repo, mock_ingestor)

    node_keys = _node_keys(mock_ingestor)
    for call in get_relationships(mock_ingestor, cs.RelationshipType.IMPLEMENTS.value):
        to_label, _, to_qn = call.args[2]
        assert (str(to_label), to_qn) in node_keys, call.args


def test_cpp_module_impl_with_interface_still_links(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "math.cppm").write_text(
        """
export module math_module;

export int add(int a, int b) { return a + b; }
"""
    )
    (temp_repo / "math_impl.cpp").write_text(
        """
module math_module;

int internal_helper() { return 7; }
"""
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    implements = {
        (call.args[0][2], call.args[2][2])
        for call in get_relationships(
            mock_ingestor, cs.RelationshipType.IMPLEMENTS.value
        )
    }
    assert (
        f"{project}.math_module{cs.CPP_IMPL_SUFFIX}",
        f"{project}.math_module",
    ) in implements, implements


def test_js_destructured_require_of_missing_module_emits_no_phantom(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) The CommonJS destructuring fallback emitted its IMPORTS edges
    # (H) directly, bypassing deferred verification entirely.
    utils = temp_repo / "src" / "utils"
    utils.mkdir(parents=True)
    (utils / "helpers.js").write_text("module.exports = { helper: () => {} };\n")
    (temp_repo / "main.js").write_text(
        "const { helper, validator } = require('./src/utils/helpers');\n"
        "const { api: apiClient, db: database } = require('./src/services');\n"
        "helper();\n"
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    targets = _import_targets(mock_ingestor, f"{project}.main")
    assert f"{project}.src.utils.helpers" in targets, targets
    _assert_no_dangling_imports(mock_ingestor)


def test_java_inner_class_never_self_implements(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `static class Entry implements Map.Entry<K, V>`: the parse-time
    # (H) resolution can land on the inner class ITSELF (the only registered
    # (H) qn ending in Entry); a self-IMPLEMENTS is never real.
    (temp_repo / "SimpleHashMap.java").write_text(
        """
import java.util.Map;

public class SimpleHashMap<K, V> {
    static class Entry<K, V> implements Map.Entry<K, V> {
        K key;
        V value;

        public K getKey() { return key; }
        public V getValue() { return value; }
        public V setValue(V value) { this.value = value; return value; }
    }
}
"""
    )
    run_updater(temp_repo, mock_ingestor, skip_if_missing="java")

    for rel_type in (cs.RelationshipType.IMPLEMENTS, cs.RelationshipType.INHERITS):
        for call in get_relationships(mock_ingestor, rel_type.value):
            assert call.args[0][2] != call.args[2][2], call.args


def test_lua_require_of_missing_module_emits_no_phantom(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "real.lua").write_text("local M = {}\nreturn M\n")
    (temp_repo / "main.lua").write_text(
        'local real = require("real")\nlocal gone = require("storage")\n'
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    targets = _import_targets(mock_ingestor, f"{project}.main")
    assert f"{project}.real" in targets, targets
    _assert_no_dangling_imports(mock_ingestor)
