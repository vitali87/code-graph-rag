"""A deleted file's method return types must leave with its registry rows.

`TypeInferenceEngine.method_return_types` records the return type of C++,
Rust and Dart free functions, of every language's methods, and of Go receiver
methods, under the definition's qualified name. `remove_file_from_state`
pruned the registry and, since #1668, the Go free-function map for a deleted
file, but never this one: on a reused updater a deleted file's entries
persisted, and a Go method keyed by its receiver's module could not even be
reached by a prefix sweep (issue #1738).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import _MockIngestor

TYPES_GO = "package pkg\n\ntype A struct{}\n\ntype B struct{}\n"
# `Clone` is keyed by A's module (`proj.pkg.types.A.Clone`): no prefix of
# methods.go matches it, so only span-record ownership can find it.
METHODS_GO = "package pkg\n\nfunc (a A) Clone() A { return a }\n"
OTHER_GO = "package pkg\n\nfunc (b B) Twin() B { return b }\n"


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


def _project(root: Path) -> GraphUpdater:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "types.go").write_text(TYPES_GO, encoding="utf-8")
    (root / "pkg" / "methods.go").write_text(METHODS_GO, encoding="utf-8")
    (root / "pkg" / "other.go").write_text(OTHER_GO, encoding="utf-8")
    updater = _updater(root)
    updater.run()
    recorded = updater.factory.type_inference.method_return_types
    assert recorded.get("proj.pkg.types.A.Clone") == "A", (
        f"fixture guard: the first run did not record Clone's return type: {recorded}"
    )
    assert recorded.get("proj.pkg.types.B.Twin") == "B", recorded
    return updater


def test_a_deleted_go_files_method_return_types_are_forgotten(
    temp_repo: Path,
) -> None:
    """Delete `methods.go` on a reused updater: `Clone`'s entry must go.

    The entry sits under `types.go`'s prefix, which this event does not
    touch, so this is exactly the qn a prefix sweep cannot reach; the span
    records name `methods.go` as the registering module.
    """
    root = temp_repo / "proj"
    updater = _project(root)
    recorded = updater.factory.type_inference.method_return_types

    (root / "pkg" / "methods.go").unlink()
    updater.remove_file_from_state(root / "pkg" / "methods.go")

    assert "proj.pkg.types.A.Clone" not in recorded, (
        "the deleted file's return-type entry survived remove_file_from_state"
    )
    # The control: another file's method under the SAME receiver module
    # stays; only the deleted file's rows go.
    assert recorded.get("proj.pkg.types.B.Twin") == "B", (
        "a sibling's entry was swept along with the deleted file's"
    )


def test_a_method_another_file_owns_survives_its_receiver_modules_delete(
    temp_repo: Path,
) -> None:
    """Delete `types.go`: the methods keyed under it stay, as their rows do.

    Both methods sit under `proj.pkg.types` although `methods.go` and
    `other.go` declare them, and neither of those files is re-parsed by
    this event. The registry keeps their rows for exactly that reason (the
    span records name the declaring file, so they are foreign to this
    delete), and the return-type map must agree with the registry rather
    than sweep by prefix alone.
    """
    root = temp_repo / "proj"
    updater = _project(root)
    recorded = updater.factory.type_inference.method_return_types

    (root / "pkg" / "types.go").unlink()
    updater.remove_file_from_state(root / "pkg" / "types.go")

    assert "proj.pkg.types.A.Clone" in updater.function_registry, (
        "fixture guard: the registry swept a row another file owns"
    )
    assert recorded.get("proj.pkg.types.A.Clone") == "A", (
        f"the map swept by prefix what the registry kept by ownership: {recorded}"
    )
    assert recorded.get("proj.pkg.types.B.Twin") == "B", recorded


def test_a_reparse_records_the_new_return_type(temp_repo: Path) -> None:
    """The point of forgetting: a re-parse must not be shadowed by the old."""
    root = temp_repo / "proj"
    updater = _project(root)
    recorded = updater.factory.type_inference.method_return_types

    (root / "pkg" / "methods.go").write_text(
        "package pkg\n\nfunc (a A) Clone() B { return B{} }\n", encoding="utf-8"
    )
    updater.remove_file_from_state(root / "pkg" / "methods.go")
    assert "proj.pkg.types.A.Clone" not in recorded
    updater.run()

    assert recorded.get("proj.pkg.types.A.Clone") == "B", recorded


def test_an_entry_whose_registry_row_is_already_gone_is_still_forgotten(
    temp_repo: Path,
) -> None:
    """Ownership comes from the span records, not the registry sweep.

    A receiver method is keyed under `types.go`, so no prefix of
    `methods.go` matches it, and with its registry row already removed it
    never enters the sweep's removal set either. Only the span record that
    names `methods.go` as the registering module can still find it (#1752
    review).
    """
    root = temp_repo / "proj"
    updater = _project(root)
    recorded = updater.factory.type_inference.method_return_types
    del updater.function_registry["proj.pkg.types.A.Clone"]

    (root / "pkg" / "methods.go").unlink()
    updater.remove_file_from_state(root / "pkg" / "methods.go")

    assert "proj.pkg.types.A.Clone" not in recorded, (
        f"the orphaned entry outlived its registry row and its file: {recorded}"
    )
    assert recorded.get("proj.pkg.types.B.Twin") == "B", recorded


def test_a_qn_shared_with_a_survivor_is_forgotten_rather_than_left_stale(
    temp_repo: Path,
) -> None:
    """Two files declaring the same receiver method share one map slot.

    The map holds one value per qn, and the later-parsed file wrote it. When
    that file is deleted the survivor is not re-parsed, so keeping the entry
    keeps the DELETED definition's return type under the survivor's name;
    forgetting it costs a lookup until the survivor's next parse (#1752
    review).
    """
    root = temp_repo / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "types.go").write_text(TYPES_GO, encoding="utf-8")
    (root / "pkg" / "methods.go").write_text(METHODS_GO, encoding="utf-8")
    # Parsed after methods.go, so its return type is the one recorded.
    (root / "pkg" / "other.go").write_text(
        "package pkg\n\nfunc (a A) Clone() B { return B{} }\n", encoding="utf-8"
    )
    updater = _updater(root)
    updater.run()
    recorded = updater.factory.type_inference.method_return_types
    assert recorded.get("proj.pkg.types.A.Clone") == "B", (
        f"fixture guard: the later file did not write the shared slot: {recorded}"
    )

    (root / "pkg" / "other.go").unlink()
    updater.remove_file_from_state(root / "pkg" / "other.go")

    assert "proj.pkg.types.A.Clone" not in recorded, (
        f"the deleted definition's return type survives under the survivor: {recorded}"
    )


def test_an_entry_keyed_by_its_duplicate_name_is_forgotten_too(temp_repo: Path) -> None:
    """Some writers key the map by the registry's `@line` name itself.

    Deferred Rust functions and duplicate C++ out-of-class methods record
    their return type under the `@line` name, so the ownership comparison
    normalises the map key as well as the span record (#1752 review). The
    Go fixture plants such a key for the deleted file's method.
    """
    root = temp_repo / "proj"
    updater = _project(root)
    recorded = updater.factory.type_inference.method_return_types
    planted = f"proj.pkg.types.A.Clone{cs.DUP_QN_MARKER}3"
    recorded[planted] = "A"

    (root / "pkg" / "methods.go").unlink()
    updater.remove_file_from_state(root / "pkg" / "methods.go")

    assert planted not in recorded, (
        f"an entry keyed by the deleted method's duplicate name survived: {recorded}"
    )
    assert recorded.get("proj.pkg.types.B.Twin") == "B", recorded
