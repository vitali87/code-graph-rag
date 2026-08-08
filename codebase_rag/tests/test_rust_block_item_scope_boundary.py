"""A block-local Rust fn item is out of scope once its block closes.

Such an item registers flat in the enclosing module, so bare-name
resolution reached it from anywhere in that scope and a call written
after the block bound it instead of whatever is really in scope there
(issue #1061).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_flow_edges import _has, _run_flow
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
    # The module's own item keeps the natural qn: it is the one a path from
    # anywhere can name, so the block item takes the span suffix (issue #1037).
    assert (
        "rs_boundary_sibling.src.a.f1",
        "rs_boundary_sibling.src.a.g",
    ) in calls, calls
    assert (
        "rs_boundary_sibling.src.a.f1",
        "rs_boundary_sibling.src.a.g@3",
    ) not in calls, calls
    # The call inside the block still reaches the block's own item, and
    # only it: the natural qn carries a dedup bucket holding the module
    # item too, which must not be fanned out onto.
    assert ("rs_boundary_sibling.src.a.f0", "rs_boundary_sibling.src.a.g@3") in calls, (
        calls
    )
    assert (
        "rs_boundary_sibling.src.a.f0",
        "rs_boundary_sibling.src.a.g",
    ) not in calls, calls


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


def test_block_item_survives_the_module_keyed_resolution_cache(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A file with no `use` anywhere leaves the file-keyed resolution cache
    # armed, and block items make resolution site-dependent: whichever call
    # resolves first would otherwise answer for every later call of that
    # name in the file, block or no block.
    project = temp_repo / "rs_boundary_cache"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod a;\n",
            "src/a.rs": (
                "pub const fn f1() -> u32 {\n"
                "    g()\n"
                "}\n"
                "\n"
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
                "pub const fn g() -> u32 {\n"
                "    9\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    assert (
        "rs_boundary_cache.src.a.f0",
        "rs_boundary_cache.src.a.g@7",
    ) in calls, calls
    assert ("rs_boundary_cache.src.a.f1", "rs_boundary_cache.src.a.g") in calls, calls
    assert (
        "rs_boundary_cache.src.a.f1",
        "rs_boundary_cache.src.a.g@7",
    ) not in calls, calls


def test_block_item_declared_after_the_module_item_keeps_the_boundary(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The module item holds the natural qn wherever it is written, so this
    # reads the same as its sibling above; what it adds is the source order
    # that used to decide the question. The dedup bucket the natural qn
    # carries must not fan a resolved call out onto the other scope's item.
    project = temp_repo / "rs_boundary_order"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod a;\n",
            "src/a.rs": (
                "pub fn g() -> u32 {\n"
                "    9\n"
                "}\n"
                "\n"
                "pub fn f0() -> u32 {\n"
                "    let z = {\n"
                "        fn g() -> u32 {\n"
                "            21\n"
                "        }\n"
                "        g()\n"
                "    };\n"
                "    z\n"
                "}\n"
                "\n"
                "pub fn f1() -> u32 {\n"
                "    g()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    assert ("rs_boundary_order.src.a.f0", "rs_boundary_order.src.a.g@7") in calls, calls
    assert (
        "rs_boundary_order.src.a.f0",
        "rs_boundary_order.src.a.g",
    ) not in calls, calls
    assert ("rs_boundary_order.src.a.f1", "rs_boundary_order.src.a.g") in calls, calls
    assert (
        "rs_boundary_order.src.a.f1",
        "rs_boundary_order.src.a.g@7",
    ) not in calls, calls


def test_flow_edges_still_reach_a_block_item(tmp_path: Path) -> None:
    # The taint walk resolves callees without a call site position, so it
    # cannot be told which side of a block boundary it stands on. Scoping
    # must not answer "outside" there and drop the flow edge.
    edges = _run_flow(
        tmp_path,
        {
            "main.rs": (
                "fn boot() {\n"
                '    let secret = std::env::var("SECRET").unwrap();\n'
                "    let z = {\n"
                "        fn sink(v: String) {\n"
                '            println!("{}", v);\n'
                "        }\n"
                "        sink(secret);\n"
                "        1\n"
                "    };\n"
                "    let _ = z;\n"
                "}\n"
            )
        },
    )
    assert _has(edges, "main.boot", "main.sink"), edges
