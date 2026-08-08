"""`function_locations` must not survive a module's re-parse (issue #1019).

The map is keyed by `(module_qn, start_line, start_col)` and was never cleared
between runs of a reused GraphUpdater. A function renamed in place keeps its
start position, so a second run reads the dead qn and files the enclosing
method's body `use` under it; the live qn then resolves through the file map and
binds the wrong helper.

The codebase defends exactly this invariant elsewhere -- `reset_rust_path_caches`
and `reset_js_receiver_bindings` exist so a reused updater's second run stays
correct -- so it is pinned here the same way.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_rust_crate_path_trait_linking import _calls, _write

CARGO = '[package]\nname = "{name}"\nversion = "0.1.0"\n'


def _method_source(method_name: str, tail: str = "") -> str:
    return (
        "use crate::beta::helper;\n"
        "\n"
        "pub struct S;\n"
        "\n"
        "impl S {\n"
        f"    pub fn {method_name}(&self) -> u32 {{\n"
        "        use crate::alpha::helper;\n"
        "        helper()\n"
        "    }\n"
        "}\n"
        "\n"
        "pub fn f() -> u32 {\n"
        "    helper()\n"
        "}\n"
        f"{tail}"
    )


def _corpus(name: str, method_name: str) -> dict[str, str]:
    return {
        "Cargo.toml": CARGO.format(name=name),
        "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
        "src/alpha.rs": "pub fn helper() -> u32 {\n    1\n}\n",
        "src/beta.rs": "pub fn helper() -> u32 {\n    2\n}\n",
        "src/foo.rs": _method_source(method_name),
    }


def _reused_updater(project: Path, mock_ingestor: MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    if "rust" not in parsers:
        pytest.skip("rust parser not available")
    return GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
    )


def test_renamed_method_does_not_resolve_through_the_dead_qn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    name = "rs_reuse_rename"
    project = temp_repo / name
    _write(project, _corpus(name, "run"))

    updater = _reused_updater(project, mock_ingestor)
    updater.run()

    # Renamed in place: same start line and column, so the stale span key
    # still matches and hands back FunctionLocation(qualified_name=...run).
    _write(project, {"src/foo.rs": _method_source("go")})
    mock_ingestor.reset_mock()
    updater.run()

    calls = _calls(mock_ingestor)
    live = f"{name}.src.foo.S.go"
    assert (live, f"{name}.src.alpha.helper") in calls, calls
    assert (live, f"{name}.src.beta.helper") not in calls, calls


def test_function_locations_hold_no_entry_for_the_dead_qn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    name = "rs_reuse_map"
    project = temp_repo / name
    _write(project, _corpus(name, "run"))

    updater = _reused_updater(project, mock_ingestor)
    updater.run()

    _write(project, {"src/foo.rs": _method_source("go")})
    updater.run()

    located = {
        location.qualified_name
        for location in updater.factory.definition_processor.function_locations.values()
    }
    assert f"{name}.src.foo.S.go" in located, located
    assert f"{name}.src.foo.S.run" not in located, located


def test_unchanged_module_keeps_resolving_across_runs(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Control: purging on re-parse must not lose a module whose functions
    # kept their names. The trailing item makes the file re-parse; without a
    # content change the run is skipped as in-sync and records nothing.
    name = "rs_reuse_stable"
    project = temp_repo / name
    _write(project, _corpus(name, "run"))

    updater = _reused_updater(project, mock_ingestor)
    updater.run()

    _write(
        project,
        {"src/foo.rs": _method_source("run", "\npub fn added() -> u32 {\n    3\n}\n")},
    )
    mock_ingestor.reset_mock()
    updater.run()

    calls = _calls(mock_ingestor)
    live = f"{name}.src.foo.S.run"
    assert (live, f"{name}.src.alpha.helper") in calls, calls
    assert (f"{name}.src.foo.f", f"{name}.src.beta.helper") in calls, calls
