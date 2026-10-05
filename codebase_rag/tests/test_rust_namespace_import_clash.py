"""Two file-level `use`s of one name both keep their binding (issue #2462).

Rust keeps types, values and macros in separate namespaces, so
`use mycrate::Error;` (the crate's error type) and `use thiserror::Error;`
(the derive macro behind `#[derive(Error)]`) are legal side by side. The
import map has one slot per name, and the later `use` simply took it: the
`mycrate::Error` IMPORTS row vanished, and `Error` in a type position fell
back to a project-wide name lookup that bound an unrelated `struct Error;`
in a `tests/ui` fixture.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_CARGO = (
    '[package]\nname = "mycrate"\nversion = "0.1.0"\nedition = "2021"\n'
    '[dev-dependencies]\nthiserror = "1"\n'
)
_LIB = "pub struct Error;\n\npub fn fail() -> Error {\n    Error\n}\n"
# An unrelated compile-fail fixture: the wrong-but-nearest `Error` a name
# fallback from `tests/app.rs` would pick.
_FIXTURE = "struct Error;\n\nfn main() {\n    let _ = Error;\n}\n"
_APP_BODY = (
    "#[derive(Error, Debug)]\n"
    '#[error("wrapped")]\n'
    "struct Wrapped;\n"
    "\n"
    "fn make() -> Error {\n"
    "    fail()\n"
    "}\n"
    "\n"
    "fn take(_e: Error) {}\n"
)

_APP = "rsns.tests.app"
_LIB_QN = "rsns.src.lib"
_TYPE_QN = "rsns.src.lib.Error"
_DECOY_QN = "rsns.tests.ui.fixture.Error"


def _write(project: Path, files: dict[str, str]) -> None:
    for rel_path, source in files.items():
        target = project / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoding="utf-8", data=source)


def _index(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    uses: str,
    extra: dict[str, str] | None = None,
) -> GraphUpdater:
    project = temp_repo / "rsns"
    _write(
        project,
        {
            "Cargo.toml": _CARGO,
            "src/lib.rs": _LIB,
            "tests/ui/fixture.rs": _FIXTURE,
            "tests/app.rs": f"{uses}\n{_APP_BODY}",
            **(extra or {}),
        },
    )
    return create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")


def _imports(mock_ingestor: MagicMock, module_qn: str) -> list[tuple[str, str, int]]:
    # (target module, imported_name, line) per IMPORTS edge, duplicates kept:
    # one row per binding the file writes.
    rows: list[tuple[str, str, int]] = []
    for c in get_relationships(mock_ingestor, cs.RelationshipType.IMPORTS):
        if c.args[0][2] != module_qn:
            continue
        site = c.kwargs.get("properties") or {}
        rows.append(
            (c.args[2][2], site.get(cs.KEY_IMPORTED_NAME), site.get(cs.KEY_LINE))
        )
    return sorted(rows)


def _targets(mock_ingestor: MagicMock, rel: str, source_qn: str) -> set[str]:
    return {
        c.args[2][2]
        for c in get_relationships(mock_ingestor, rel)
        if c.args[0][2] == source_qn
    }


def _slot(updater: GraphUpdater, module_qn: str, name: str) -> str | None:
    return updater.factory.import_processor.import_mapping.get(module_qn, {}).get(name)


# --- the reported shape -------------------------------------------------------


def test_derive_macro_use_does_not_evict_the_type_import(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        "use mycrate::{fail, Error};\nuse thiserror::Error;\n",
    )
    assert _imports(mock_ingestor, _APP) == [
        (_LIB_QN, "Error", 1),
        (_LIB_QN, "fail", 1),
        ("thiserror", "Error", 2),
    ]
    assert _targets(mock_ingestor, cs.RelationshipType.RETURNS, f"{_APP}.make") == {
        _TYPE_QN
    }
    assert _targets(mock_ingestor, cs.RelationshipType.ACCEPTS, f"{_APP}.take") == {
        _TYPE_QN
    }


def test_derive_macro_use_written_first_keeps_its_imports_row(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The opposite order already bound the type (the later `use` won the
    # slot), but the macro's own row vanished instead.
    _index(
        temp_repo,
        mock_ingestor,
        "use thiserror::Error;\nuse mycrate::{fail, Error};\n",
    )
    assert _imports(mock_ingestor, _APP) == [
        (_LIB_QN, "Error", 2),
        (_LIB_QN, "fail", 2),
        ("thiserror", "Error", 1),
    ]
    assert _targets(mock_ingestor, cs.RelationshipType.RETURNS, f"{_APP}.make") == {
        _TYPE_QN
    }


def test_crate_under_test_type_import_survives_a_later_macro_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The dtolnay/anyhow shape: an integration test imports the crate's own
    # `Error` through the package name, then thiserror's derive. Fresh and
    # incremental syncs disagreed on which fixture `Error` the fallback hit.
    project = temp_repo / "anyhowish"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "anyhow"\nversion = "1.0.0"\nedition = "2018"\n'
                '[dev-dependencies]\nthiserror = "1"\n'
            ),
            "src/lib.rs": (
                "pub struct Error;\n\npub trait Context {}\n\n"
                "pub type Result<T> = core::result::Result<T, Error>;\n"
            ),
            "tests/ui/no-impl.rs": "struct Error;\n\nfn main() {}\n",
            "tests/test_context.rs": (
                "use anyhow::{Context, Error, Result};\n"
                "use thiserror::Error;\n"
                "\n"
                "struct Dropped;\n"
                "\n"
                "#[derive(Error, Debug)]\n"
                '#[error("high level")]\n'
                "struct HighLevel;\n"
                "\n"
                "fn make_chain() -> (Error, Dropped) {\n"
                "    unimplemented!()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    returns = _targets(
        mock_ingestor,
        cs.RelationshipType.RETURNS,
        "anyhowish.tests.test_context.make_chain",
    )
    assert "anyhowish.src.lib.Error" in returns, returns
    assert "anyhowish.tests.ui.no-impl.Error" not in returns, returns
    assert ("anyhowish.src.lib", "Error", 1) in _imports(
        mock_ingestor, "anyhowish.tests.test_context"
    )


def test_two_external_same_name_uses_keep_both_imports_rows(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `std::fmt::Display` (the trait) beside `derive_more::Display` (its
    # derive): the file imports both, so both rows belong in the graph.
    _index(
        temp_repo,
        mock_ingestor,
        "use mycrate::fail;\nuse std::fmt::Display;\nuse derive_more::Display;\n",
    )
    rows = _imports(mock_ingestor, _APP)
    assert ("std::fmt", "Display", 2) in rows, rows
    assert ("derive_more", "Display", 3) in rows, rows


# --- what must not change ------------------------------------------------------


def test_without_the_macro_use_the_type_import_is_unchanged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    updater = _index(temp_repo, mock_ingestor, "use mycrate::{fail, Error};\n")
    assert _imports(mock_ingestor, _APP) == [
        (_LIB_QN, "Error", 1),
        (_LIB_QN, "fail", 1),
    ]
    assert _slot(updater, _APP, "Error") == _TYPE_QN
    assert _targets(mock_ingestor, cs.RelationshipType.RETURNS, f"{_APP}.make") == {
        _TYPE_QN
    }


def test_two_external_bindings_keep_the_later_one_in_the_slot(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Neither binding names a project item, so there is nothing to prefer:
    # the later `use` keeps the slot exactly as before.
    updater = _index(
        temp_repo,
        mock_ingestor,
        "use mycrate::fail;\nuse std::fmt::Display;\nuse derive_more::Display;\n",
    )
    assert _slot(updater, _APP, "Display") == "derive_more::Display"


def test_two_project_bindings_keep_the_later_one_in_the_slot(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # cfg-exclusive twins naming two project items: both are first-party, so
    # the slot still goes to the later `use`, as it always did.
    updater = _index(
        temp_repo,
        mock_ingestor,
        (
            "use mycrate::fail;\n"
            "#[cfg(test)]\nuse mycrate::mock::Clock;\n"
            "#[cfg(not(test))]\nuse mycrate::real::Clock;\n"
        ),
        extra={
            "src/lib.rs": f"pub mod mock;\npub mod real;\n{_LIB}",
            "src/mock.rs": "pub struct Clock;\n",
            "src/real.rs": "pub struct Clock;\n",
        },
    )
    assert _slot(updater, _APP, "Clock") == "rsns.src.real.Clock"


def test_self_module_binding_still_yields_to_a_later_external_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A `{self}` module binding stays reachable through its own map (issue
    # #1054), so a later value `use` of the name takes the shared slot even
    # when it names an external crate.
    updater = _index(
        temp_repo,
        mock_ingestor,
        "use mycrate::fail;\nuse mycrate::io::{self};\nuse other::io;\n",
        extra={
            "src/lib.rs": f"pub mod io;\n{_LIB}",
            "src/io.rs": "pub fn open() {}\n",
        },
    )
    assert _slot(updater, _APP, "io") == "other::io"
    processor = updater.factory.import_processor
    assert processor.rust_self_module_imports[_APP]["io"] == "rsns.src.io"


def test_repeated_use_of_one_path_writes_one_imports_row(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # cfg twins of the SAME path bind one item: one row, not two.
    _index(
        temp_repo,
        mock_ingestor,
        (
            "use mycrate::fail;\n"
            '#[cfg(feature = "a")]\nuse mycrate::Error;\n'
            '#[cfg(not(feature = "a"))]\nuse mycrate::Error;\n'
        ),
    )
    rows = [row for row in _imports(mock_ingestor, _APP) if row[1] == "Error"]
    assert len(rows) == 1, rows
    assert rows[0][0] == _LIB_QN


def test_function_scope_macro_use_leaves_the_file_binding_alone(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A `use` inside a fn body lives in that function's own map; the
    # file-level slot keeps the file's binding as before.
    updater = _index(
        temp_repo,
        mock_ingestor,
        "use mycrate::{fail, Error};\n\nfn local() {\n    use thiserror::Error;\n}\n",
    )
    assert _slot(updater, _APP, "Error") == _TYPE_QN
    assert _targets(mock_ingestor, cs.RelationshipType.RETURNS, f"{_APP}.make") == {
        _TYPE_QN
    }
