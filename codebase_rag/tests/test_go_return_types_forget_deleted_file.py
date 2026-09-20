"""A deleted Go file's return-type entries must leave with its registry rows.

`TypeInferenceEngine.go_function_return_types` records the first return type
of every Go free function, and `_go_free_fn_return_type` answers a sibling
file's call through a lazily rebuilt `(package, name)` index over it.
`GraphUpdater.remove_file_from_state` pruned every other registry for a deleted
file but never this map, so on a reused updater a deleted file's entries
persisted; the index is filled with `setdefault`, so a surviving or new
sibling defining a free function of the same name lost to the stale entry and
its return type was never recorded (issue #1668).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import _MockIngestor

A_GO = "package pkg\n\ntype A struct{}\n\nfunc New() *A { return &A{} }\n"
USER_GO = "package pkg\n\nfunc Use() {\n\tx := New()\n\t_ = x\n}\n"


def _updater(root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    if "go" not in {str(k) for k in parsers}:
        pytest.skip("go parser not available")
    return GraphUpdater(
        ingestor=_MockIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


def test_a_deleted_go_files_return_types_are_forgotten(temp_repo: Path) -> None:
    """Delete `a.go` on a reused updater: its `New` entry must go.

    The second half is the sibling case the issue names, staged so that the
    size check alone cannot rescue it: one entry removed and one added leaves
    the map the same size, so an index that is only rebuilt on a size change
    would keep serving the deleted file's type. The drop has to invalidate the
    index itself.
    """
    root = temp_repo / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.go").write_text(A_GO, encoding="utf-8")
    (root / "pkg" / "user.go").write_text(USER_GO, encoding="utf-8")
    updater = _updater(root)
    updater.run()

    engine = updater.factory.type_inference
    assert engine.go_function_return_types.get("proj.pkg.a.New") == "A", (
        "fixture guard: the first run did not record New's return type: "
        f"{engine.go_function_return_types}"
    )
    # Prime the package index the way a call from user.go does.
    assert engine._go_free_fn_return_type("New", "proj.pkg.user") == "A"

    (root / "pkg" / "a.go").unlink()
    updater.remove_file_from_state(root / "pkg" / "a.go")

    assert "proj.pkg.a.New" not in engine.go_function_return_types, (
        "the deleted file's return-type entry survived remove_file_from_state"
    )

    # A new sibling takes the name BEFORE any lookup: the map is back to the
    # size the index was built at, so a drop that only popped the entry and
    # left the size sentinel alone would serve the stale index here. No
    # intermediate lookup, deliberately -- one would rebuild the index on the
    # size change and hide exactly that (#1668 local review).
    engine.go_function_return_types["proj.pkg.c.New"] = "C"
    assert len(engine.go_function_return_types) == engine._go_free_fn_index_size or (
        engine._go_free_fn_index_size == -1
    ), "fixture guard: the delete-then-add must land on the indexed size"
    assert engine._go_free_fn_return_type("New", "proj.pkg.user") == "C", (
        "the sibling's return type lost to the deleted file's stale entry"
    )


def test_a_lookup_after_the_delete_no_longer_answers(temp_repo: Path) -> None:
    """With no sibling, the package index must answer None, not the stale type."""
    root = temp_repo / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.go").write_text(A_GO, encoding="utf-8")
    (root / "pkg" / "user.go").write_text(USER_GO, encoding="utf-8")
    updater = _updater(root)
    updater.run()
    engine = updater.factory.type_inference
    assert engine._go_free_fn_return_type("New", "proj.pkg.user") == "A"

    updater.remove_file_from_state(root / "pkg" / "a.go")

    assert engine._go_free_fn_return_type("New", "proj.pkg.user") is None, (
        "the package index still answers from the deleted file's entry"
    )


def test_another_files_entries_survive_the_delete(temp_repo: Path) -> None:
    """The control: only the deleted file's entries go, not the package's."""
    root = temp_repo / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.go").write_text(A_GO, encoding="utf-8")
    (root / "pkg" / "b.go").write_text(
        "package pkg\n\ntype B struct{}\n\nfunc Make() *B { return &B{} }\n",
        encoding="utf-8",
    )
    updater = _updater(root)
    updater.run()
    engine = updater.factory.type_inference
    assert engine.go_function_return_types.get("proj.pkg.b.Make") == "B"

    updater.remove_file_from_state(root / "pkg" / "a.go")

    assert "proj.pkg.a.New" not in engine.go_function_return_types
    assert engine.go_function_return_types.get("proj.pkg.b.Make") == "B", (
        "a sibling's entry was swept along with the deleted file's"
    )
    assert engine._go_free_fn_return_type("Make", "proj.pkg.a") == "B"
