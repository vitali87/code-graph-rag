from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.import_processor import ImportProcessor
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
    # On an incremental re-index the same module_qn is parsed again;
    # parse_imports resets import_mapping[module_qn] but must also drop the
    # module's php_function_imports, or a `use function` removed from the file
    # lingers and keeps (wrongly) exempting that name from external suppression.
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
