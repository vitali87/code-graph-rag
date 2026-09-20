# IMPORTS edges were emitted straight from the import map's parse-time
# guesses, so an internal-looking target that maps to no real module (a
# broken import, a directory, a crate path resolved from the wrong root, a
# specifier with an explicit .js extension, a C++20 module declaration
# registering itself) produced an edge the database silently drops (issue
# #652: 51 across the fixture suite). Emission is now deferred until every
# file is parsed and verified against the real module qns; an internal
# target that resolves nowhere emits no edge.
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

    # The item mapping must also drop the extension or calls to createB
    # resolve against a phantom qn.
    calls = {
        (call.args[0][2], call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
    }
    assert (f"{project}.a.runA", f"{project}.b.createB") in calls, calls


def test_js_bare_package_named_like_extension_keeps_its_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A bare npm package can legitimately END in .js (p5.js, highlight.js);
    # extension stripping applies to relative file paths only, or the
    # external package qn silently loses its real name.
    (temp_repo / "sketch.js").write_text(
        "import p5 from 'p5.js';\nexport const sketch = new p5();\n"
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    targets = _import_targets(mock_ingestor, f"{project}.sketch")
    assert "p5.js" in targets, targets
    assert "p5" not in targets, targets


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


def test_a_global_module_fragment_include_keeps_its_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """The include comes BEFORE the declaration, so the declaration displaces it.

    A global module fragment is legal C++20 and puts includes first:

        module;
        #include <foo.h>
        export module foo;

    `<foo.h>` binds the local name `foo`, then `export module foo;` overwrites
    that binding. The shadowed-include sweep only recorded displacements made
    by a later INCLUDE, so nothing recorded this one: the declaration's own
    target is skipped by the deferred loop as a self-import, and the header
    got no edge at all. Measured before the fix -- `IMPORTS from proj.m` was
    empty (#1758 review).

    The declaration now records what it displaced, exactly as an include
    does. Asserts both halves: the header keeps its edge AND the declaration
    still emits no self-import, since recording the displacement must not
    reintroduce the thing the guard exists to suppress.
    """
    (temp_repo / "m.cpp").write_text(
        """
module;
#include <foo.h>
export module foo;

int use() { return 2; }
"""
    )
    run_updater(temp_repo, mock_ingestor)

    _assert_no_dangling_imports(mock_ingestor)
    project = temp_repo.name
    targets = _import_targets(mock_ingestor, f"{project}.m")
    assert "std.foo.h" in targets, (
        "an include in the global module fragment lost its edge when the "
        f"module declaration overwrote its local binding: {targets}"
    )
    assert f"{project}.foo" not in targets, (
        f"the module declaration's own qn was emitted as an import: {targets}"
    )


def test_a_quoted_include_keeps_its_edge_beside_a_same_named_declaration(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """The declaration guard must not swallow a real quoted include (#1758 review).

    The guard skips a displaced binding that `export module X;` registered,
    so it cannot rebuild the self-import. The worry is that a QUOTED include
    resolving to the same qn would be suppressed with it, silently losing a
    real dependency edge.

    It cannot, and this pins why: a quoted include resolves to the qn of the
    FILE it names, so `#include "foo.h"` beside `foo.cpp` yields `proj.foo.h`
    -- the header's own module -- while the declaration registered
    `proj.foo`. Different qns, so the guard never matches the include. If
    include resolution ever started stripping the extension, the two would
    collide and this test goes red.
    """
    (temp_repo / "foo.cpp").write_text("int helper() { return 1; }\n")
    (temp_repo / "foo.h").write_text("int helper();\n")
    sub = temp_repo / "sub"
    sub.mkdir()
    (sub / "foo.h").write_text("int other();\n")
    (temp_repo / "m.cpp").write_text(
        """
export module foo;
#include "foo.h"
#include "sub/foo.h"

int use() { return 2; }
"""
    )
    run_updater(temp_repo, mock_ingestor)

    _assert_no_dangling_imports(mock_ingestor)
    project = temp_repo.name
    targets = _import_targets(mock_ingestor, f"{project}.m")
    assert f"{project}.foo" not in targets, (
        f"the module declaration's own qn was emitted as an import: {targets}"
    )
    assert {f"{project}.foo.h", f"{project}.sub.foo"} <= targets, (
        "a quoted include that shares a name with the module declaration lost "
        f"its edge to the declaration guard: {targets}"
    )


def test_a_module_declaration_shadowed_by_an_include_emits_no_self_import(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`export module foo;` beside `#include <foo.h>`, with a real `proj.foo`.

    The declaration registers `foo -> proj.foo` and the include then takes
    the same local name, so the declaration's binding is DISPLACED. The
    shadowed-include sweep re-emits displaced bindings to keep an include's
    edge alive (#1758), and a declaration caught in that sweep rebuilds
    exactly the self-import the test above forbids: the main edge loop
    filters it via `_cpp_declaration_mappings`, so the sweep must too.
    """
    (temp_repo / "foo.cpp").write_text("int helper() { return 1; }\n")
    sub = temp_repo / "sub"
    sub.mkdir()
    # A SECOND include binding the same local name `foo`, so the first one is
    # genuinely displaced and only the sweep can carry its edge. Without it
    # the surviving-edge assertion below is satisfied by the main edge loop
    # (the winning include is in `import_mapping` either way) and a guard
    # that dropped everything in the sweep would still pass (#1758 review).
    (temp_repo / "m.cpp").write_text(
        """
export module foo;
#include <foo.h>
#include <sub/foo.h>

int use() { return 2; }
"""
    )
    run_updater(temp_repo, mock_ingestor)

    _assert_no_dangling_imports(mock_ingestor)
    project = temp_repo.name
    targets = _import_targets(mock_ingestor, f"{project}.m")
    assert f"{project}.foo" not in targets, (
        "the shadowed-include sweep re-emitted a module declaration's own qn "
        f"as an IMPORTS edge: {targets}"
    )
    # Both includes keep their edge. The displaced one reaches the graph ONLY
    # through the sweep, so this fails if the guard suppresses too much --
    # which the single-include version of this test could not detect.
    assert {"std.foo.h", "std.sub.foo.h"} <= targets, (
        "an include's edge was lost with the declaration; the guard must drop "
        f"the declaration binding only: {targets}"
    )


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
    # The CommonJS destructuring fallback emitted its IMPORTS edges
    # directly, bypassing deferred verification entirely.
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
    # `static class Entry implements Map.Entry<K, V>`: the parse-time
    # resolution can land on the inner class ITSELF (the only registered
    # qn ending in Entry); a self-IMPLEMENTS is never real.
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


def test_python_from_package_import_submodule_targets_the_submodule(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `from thrift.transport import TTransport` under a src-root layout
    # imports the SUBMODULE TTransport.py; the stdlib extractor treats
    # the trailing name as an item and strips it, so suffix verification
    # anchored the edge at the package __init__ instead of the real
    # module file (thrift lib/py: 17 such edges). The flush must verify
    # the FULL dotted name as a module first.
    src = temp_repo / "src"
    transport = src / "transport"
    transport.mkdir(parents=True)
    (transport / "__init__.py").write_text("")
    (transport / "TTransport.py").write_text(
        "class TTransportBase(object):\n    pass\n"
    )
    (src / "user.py").write_text(
        "from " + temp_repo.name + ".transport import TTransport\n"
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    targets = _import_targets(mock_ingestor, f"{project}.src.user")
    assert f"{project}.src.transport.TTransport" in targets, targets
    assert f"{project}.src.transport.__init__" not in targets, targets


def test_python_import_suffix_match_ignores_other_language_modules(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # thrift lib/py: src/protocol/__init__.py AND src/ext/protocol.h both
    # yield module qns ending `.protocol`, so the language-blind suffix
    # match went ambiguous and dropped the edge for a Python import that
    # can only target the Python module. Candidates are tie-broken by the
    # importing language's file extensions.
    src = temp_repo / "src"
    proto = src / "protocol"
    ext = src / "ext"
    proto.mkdir(parents=True)
    ext.mkdir()
    (proto / "__init__.py").write_text("")
    (ext / "protocol.h").write_text("struct TProtocol { int dummy; };\n")
    (src / "TBinaryProtocol.py").write_text(
        "class Accelerated(object):\n"
        "    def __init__(self):\n"
        "        try:\n"
        "            from " + temp_repo.name + ".protocol import fastbinary\n"
        "        except ImportError:\n"
        "            pass\n"
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    targets = _import_targets(mock_ingestor, f"{project}.src.TBinaryProtocol")
    assert f"{project}.src.protocol" in targets, targets
