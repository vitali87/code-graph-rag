from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.import_processor import (
    ImportProcessor,
    _php_use_clause_binding,
)
from evals.cgr_graph import _capture


def _make(root: Path) -> None:
    # enum_value is declared `namespace App\Support` but lives in a file whose
    # path is Collections/functions.php, so cgr registers it by file path
    # (proj.Collections.functions.enum_value), NOT by namespace. This is the
    # pervasive laravel global-helper layout (Illuminate/Collections/functions.php
    # declares namespace Illuminate\Support).
    collections = root / "Collections"
    collections.mkdir(parents=True, exist_ok=True)
    (collections / "functions.php").write_text(
        "<?php\n"
        "namespace App\\Support;\n"
        "if (! function_exists('App\\\\Support\\\\enum_value')) {\n"
        "    function enum_value($value) { return $value; }\n"
        "}\n",
        encoding="utf-8",
    )
    db = root / "Database"
    db.mkdir(parents=True, exist_ok=True)
    (db / "Connection.php").write_text(
        "<?php\n"
        "namespace App\\Database;\n"
        "use function App\\Support\\enum_value;\n"
        "class Connection {\n"
        "    public function from($table) {\n"
        "        return enum_value($table);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )


def test_use_function_import_call_resolves_across_namespace_path_mismatch(
    tmp_path: Path,
) -> None:
    # A bare call to a function brought in by `use function A\B\c` must resolve
    # to the registered first-party function even though the PHP namespace path
    # never matches cgr's file-path qualified name. Before the fix, the namespace
    # target (App.Support.enum_value) matched no node and was misclassified as an
    # external import, suppressing the simple-name trie fallback and dropping the
    # call. A bare call without `use function` already resolved via the trie.
    _make(tmp_path)
    ingestor = _capture(tmp_path, "proj")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }
    assert (
        "proj.Database.Connection.Connection.from",
        "proj.Collections.functions.enum_value",
    ) in calls


def test_reparse_clears_stale_php_function_imports(tmp_path: Path) -> None:
    # On incremental re-index the same module_qn is parsed again; parse_imports
    # resets import_mapping[module_qn] but must also drop the module's
    # php_function_imports, or a `use function` removed from the file lingers
    # and keeps wrongly exempting that name from external suppression.
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PHP not in parsers:
        pytest.skip("php tree-sitter grammar not installed")
    php = parsers[cs.SupportedLanguage.PHP]
    processor = ImportProcessor(tmp_path, "proj")

    with_import = php.parse(
        b"<?php\nnamespace App\\Db;\nuse function App\\Support\\enum_value;\n"
    ).root_node
    processor.parse_imports(with_import, "proj.mod", cs.SupportedLanguage.PHP, queries)
    assert "enum_value" in processor.php_function_imports.get("proj.mod", set())

    without_import = php.parse(b"<?php\nnamespace App\\Db;\n").root_node
    processor.parse_imports(
        without_import, "proj.mod", cs.SupportedLanguage.PHP, queries
    )
    assert "enum_value" not in processor.php_function_imports.get("proj.mod", set())


def test_class_alias_survives_a_same_named_const_and_reparse(tmp_path: Path) -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PHP not in parsers:
        pytest.skip("php tree-sitter grammar not installed")
    php = parsers[cs.SupportedLanguage.PHP]
    processor = ImportProcessor(tmp_path, "proj")
    both = php.parse(
        b"<?php\nnamespace App;\n"
        b"use Vendor\\Widget as Target;\n"
        b"use const Settings\\TARGET as Target;\n"
        b"use function Helpers\\build as Target;\n"
    ).root_node
    processor.parse_imports(both, "proj.mod", cs.SupportedLanguage.PHP, queries)
    assert processor.php_class_imports["proj.mod"]["Target"] == "Vendor.Widget"
    assert "Target" in processor.php_const_imports["proj.mod"]
    assert "Target" in processor.php_function_imports["proj.mod"]

    cleared = php.parse(b"<?php\nnamespace App;\n").root_node
    processor.parse_imports(cleared, "proj.mod", cs.SupportedLanguage.PHP, queries)
    assert "proj.mod" not in processor.php_class_imports


def test_same_alias_construction_follows_the_class(tmp_path: Path) -> None:
    # The const is recorded after the class, so the shared import map no
    # longer points at Widget. The function alias is recorded before the
    # class. Neither may send `new` to the current-namespace decoy.
    root = tmp_path / "proj"
    files = {
        "src/Decoy.php": "<?php\nnamespace App;\nclass Target { public function __construct() {} }\nclass Maker { public function __construct() {} }\n",
        "src/Widget.php": "<?php\nnamespace Vendor;\nclass Widget { public function __construct() {} }\nclass Maker { public function __construct() {} }\n",
        "src/Caller.php": (
            "<?php\nnamespace App;\n"
            "use Vendor\\Widget as Target;\n"
            "use const Settings\\TARGET as Target;\n"
            "use function Helpers\\build as Maker;\n"
            "use Vendor\\Maker as Maker;\n"
            "class Caller { public function make(): void {"
            " new Target(); new target(); new Maker(); new maker(); } }\n"
        ),
    }
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    ingestor = _capture(root, "proj")
    edges = {
        (str(src), rel, str(dst))
        for _fl, src, rel, _tl, dst in ingestor.rels
        if rel in {"INSTANTIATES", "CALLS"}
    }
    caller = "proj.src.Caller.Caller.make"
    widget = "proj.src.Widget.Widget"
    maker = "proj.src.Widget.Maker"
    assert (caller, "INSTANTIATES", widget) in edges
    assert (caller, "CALLS", f"{widget}.__construct") in edges
    assert (caller, "INSTANTIATES", maker) in edges
    assert (caller, "CALLS", f"{maker}.__construct") in edges
    assert not any(dst.endswith(".Decoy.Target") for _src, _rel, dst in edges)
    assert not any(dst.endswith(".Decoy.Maker") for _src, _rel, dst in edges)


def _use_clauses(source: bytes) -> list[Node]:
    parsers, _ = load_parsers()
    if cs.SupportedLanguage.PHP not in parsers:
        pytest.skip("php tree-sitter grammar not installed")
    tree = parsers[cs.SupportedLanguage.PHP].parse(source)
    clauses: list[Node] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_PHP_NAMESPACE_USE_CLAUSE:
            clauses.append(node)
        stack.extend(reversed(node.children))
    return clauses


def test_use_clause_binding_reads_path_and_local_name() -> None:
    aliased, plain = _use_clauses(b"<?php\nuse A\\B as C;\nuse A\\D;\n")
    assert _php_use_clause_binding(aliased) == ("A.B", "C")
    assert _php_use_clause_binding(plain) == ("A.D", "D")


def test_use_clause_without_a_qualified_name_binds_nothing() -> None:
    # `use Foo;` parses to a bare `name`, which the import map has never bound.
    (clause,) = _use_clauses(b"<?php\nuse Foo;\n")
    assert _php_use_clause_binding(clause) is None


def test_use_clause_with_an_empty_qualified_name_binds_nothing() -> None:
    qualified_name = MagicMock()
    qualified_name.type = cs.TS_PHP_QUALIFIED_NAME
    qualified_name.text = b""
    clause = MagicMock()
    clause.named_children = [qualified_name]
    assert _php_use_clause_binding(clause) is None
