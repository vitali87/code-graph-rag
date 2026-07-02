from pathlib import Path

from evals.cgr_graph import _capture


def _make(root: Path) -> None:
    # (H) enum_value is declared `namespace App\Support` but lives in a file whose
    # (H) path is Collections/functions.php, so cgr registers it by file path
    # (H) (proj.Collections.functions.enum_value), NOT by namespace. This is the
    # (H) pervasive laravel global-helper layout (Illuminate/Collections/functions.php
    # (H) declares namespace Illuminate\Support).
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
    # (H) A bare call to a function brought in by `use function A\B\c` must resolve
    # (H) to the registered first-party function even though the PHP namespace path
    # (H) never matches cgr's file-path qualified name. Before the fix, the namespace
    # (H) target (App.Support.enum_value) matched no node and was misclassified as an
    # (H) external import, suppressing the simple-name trie fallback and dropping the
    # (H) call. A bare call without `use function` already resolved via the trie.
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
