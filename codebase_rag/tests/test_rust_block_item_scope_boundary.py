"""A block-local Rust fn item is out of scope once its block closes.

Such an item registers flat in the enclosing module, so bare-name
resolution reached it from anywhere in that scope and a call written
after the block bound it instead of whatever is really in scope there
(issue #1061).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)


def test_call_after_the_block_does_not_bind_the_block_item(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The mod-level `use` is what is in scope at `z + g()`; the block's own
    # `g` died with its block. No same-named twin exists here, so the block
    # item holds the natural qn and a name lookup answers with it.
    project = temp_repo / "rs_boundary_use"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn g() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub mod inner {\n"
                "    use crate::gamma::g;\n"
                "\n"
                "    pub const fn f0() -> u32 {\n"
                "        let z = {\n"
                "            const fn g() -> u32 {\n"
                "                21\n"
                "            }\n"
                "            0\n"
                "        };\n"
                "        z + g()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_boundary_use.src.a.inner.f0"
    assert (caller, "rs_boundary_use.src.gamma.g") in calls, calls
    assert (caller, "rs_boundary_use.src.a.inner.g") not in calls, calls


def test_a_sibling_function_does_not_bind_another_block_item(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The block item is not a module item: a different function in the same
    # file never sees it, and the module's own same-named item is what its
    # call binds.
    project = temp_repo / "rs_boundary_sibling"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod a;\n",
            "src/a.rs": (
                "pub const fn f0() -> u32 {\n"
                "    let z = {\n"
                "        const fn g() -> u32 {\n"
                "            21\n"
                "        }\n"
                "        g()\n"
                "    };\n"
                "    z\n"
                "}\n"
                "\n"
                "pub const fn f1() -> u32 {\n"
                "    g()\n"
                "}\n"
                "\n"
                "pub const fn g() -> u32 {\n"
                "    9\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    # The block item registered first and took the natural qn, so the
    # module's own item is the one carrying a span suffix here.
    assert (
        "rs_boundary_sibling.src.a.f1",
        "rs_boundary_sibling.src.a.g@15",
    ) in calls, calls
    assert (
        "rs_boundary_sibling.src.a.f1",
        "rs_boundary_sibling.src.a.g",
    ) not in calls, calls
    # The call inside the block still reaches the block's own item.
    assert ("rs_boundary_sibling.src.a.f0", "rs_boundary_sibling.src.a.g") in calls, (
        calls
    )


def test_a_nested_function_inside_the_block_sees_the_block_item(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Scope is where the call is WRITTEN, not which function owns it: a fn
    # declared inside the block is inside the block, so its body binds the
    # block's item even though it is a caller of its own.
    project = temp_repo / "rs_boundary_nested"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod a;\n",
            "src/a.rs": (
                "pub const fn g() -> u32 {\n"
                "    9\n"
                "}\n"
                "\n"
                "pub fn f0() -> u32 {\n"
                "    let z = {\n"
                "        const fn g() -> u32 {\n"
                "            21\n"
                "        }\n"
                "        fn inner() -> u32 {\n"
                "            g()\n"
                "        }\n"
                "        inner()\n"
                "    };\n"
                "    z\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_boundary_nested.src.a.inner"
    assert (caller, "rs_boundary_nested.src.a.g@7") in calls, calls
    assert (caller, "rs_boundary_nested.src.a.g") not in calls, calls
