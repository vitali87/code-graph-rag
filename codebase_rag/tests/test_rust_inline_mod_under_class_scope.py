"""An inline `mod` under a trait or impl body needs ONE qn scheme.

The module ingestion pass names such a module by mod segments only
(`foo.inner`) while the function fqn walk keeps the class segment
(`foo.T.inner.g`). The Module node, its DEFINES edge, and the functions inside
it then sit under divergent qns, so the graph audit reports both an orphan
Module node and a dangling DEFINES edge (issue #1018).
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _write,
    create_and_run_updater,
)

CARGO = '[package]\nname = "{name}"\nversion = "0.1.0"\n'

TRAIT_CONST = """pub trait T {
    const C: u32 = {
        mod inner {
            pub const fn g() -> u32 { 1 }
        }
        inner::g()
    };
}
"""

TRAIT_DEFAULT_METHOD = """pub trait T {
    fn run(&self) -> u32 {
        mod inner {
            pub fn g() -> u32 { 1 }
        }
        inner::g()
    }
}
"""

IMPL_BODY = """pub struct S;

impl S {
    pub fn run(&self) -> u32 {
        mod inner {
            pub fn g() -> u32 { 1 }
        }
        inner::g()
    }
}
"""


def _module_qns(mock_ingestor: MagicMock) -> set[str]:
    return {
        call[0][1]["qualified_name"]
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Module"
    }


def _function_qns(mock_ingestor: MagicMock) -> set[str]:
    return {
        call[0][1]["qualified_name"]
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] in ("Function", "Method")
    }


def _index(temp_repo: Path, mock_ingestor: MagicMock, name: str, foo: str) -> None:
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": foo,
        },
    )
    # create_and_run_updater runs the graph audit, which fails on the orphan
    # Module node and the dangling DEFINES edge this issue produces.
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")


def test_inline_mod_in_a_trait_const_initializer(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    name = "rs_mod_trait_const"
    _index(temp_repo, mock_ingestor, name, TRAIT_CONST)

    modules = _module_qns(mock_ingestor)
    functions = _function_qns(mock_ingestor)
    inner = {qn for qn in modules if qn.endswith(".inner")}
    assert len(inner) == 1, modules
    holder = next(iter(inner))
    assert any(qn.startswith(f"{holder}.") for qn in functions), (holder, functions)


def test_inline_mod_in_a_trait_default_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    name = "rs_mod_trait_method"
    _index(temp_repo, mock_ingestor, name, TRAIT_DEFAULT_METHOD)

    modules = _module_qns(mock_ingestor)
    functions = _function_qns(mock_ingestor)
    inner = {qn for qn in modules if qn.endswith(".inner")}
    assert len(inner) == 1, modules
    holder = next(iter(inner))
    assert any(qn.startswith(f"{holder}.") for qn in functions), (holder, functions)


def test_inline_mod_in_an_impl_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    name = "rs_mod_impl_method"
    _index(temp_repo, mock_ingestor, name, IMPL_BODY)

    modules = _module_qns(mock_ingestor)
    functions = _function_qns(mock_ingestor)
    inner = {qn for qn in modules if qn.endswith(".inner")}
    assert len(inner) == 1, modules
    holder = next(iter(inner))
    assert any(qn.startswith(f"{holder}.") for qn in functions), (holder, functions)
