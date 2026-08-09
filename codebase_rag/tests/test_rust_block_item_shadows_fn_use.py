"""A nested block's own fn item outranks the enclosing function's body use.

`use crate::gamma::g;` at the top of a function body binds `g` for the
rest of that body, but a nested block declaring its own `const fn g` is
an item scope: calls written inside it bind the block's item, and calls
after the block keep binding the use (issue #1026).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)


def test_block_item_shadows_the_enclosing_function_body_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "rs_block_item"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn g() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub const fn f0() -> u32 {\n"
                "    use crate::gamma::g;\n"
                "    let z = {\n"
                "        const fn g() -> u32 {\n"
                "            21\n"
                "        }\n"
                "        g()\n"
                "    };\n"
                "    z\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_block_item.src.a.f0"
    assert (caller, "rs_block_item.src.a.g") in calls, calls
    assert (caller, "rs_block_item.src.gamma.g") not in calls, calls


def test_block_item_binds_over_a_same_named_module_item(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Standing the body use down is only half the answer: the block's own
    # item is what the call binds, and a same-named item at module level is
    # what a name-keyed lookup reaches for instead. Both are registered flat
    # in the same module, so only the block item's own span tells them apart.
    project = temp_repo / "rs_block_item_twin"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn g() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub const fn g() -> u32 {\n"
                "    9\n"
                "}\n"
                "\n"
                "pub const fn f0() -> u32 {\n"
                "    use crate::gamma::g;\n"
                "    let z = {\n"
                "        const fn g() -> u32 {\n"
                "            21\n"
                "        }\n"
                "        g()\n"
                "    };\n"
                "    z\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_block_item_twin.src.a.f0"
    # The block item registered under a span-suffixed variant: the module's
    # own item is the one a path can name, so it keeps the natural qn.
    assert (caller, "rs_block_item_twin.src.a.g@8") in calls, calls
    assert (caller, "rs_block_item_twin.src.a.g") not in calls, calls
    assert (caller, "rs_block_item_twin.src.gamma.g") not in calls, calls


def test_block_item_binds_over_a_same_named_inline_mod_item(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The same contest one scope deeper, where the twin is the enclosing
    # inline mod's own item and the scope walk, not the same-module probe,
    # is what a name lookup would answer from.
    project = temp_repo / "rs_block_item_mod"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn g() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub mod inner {\n"
                "    pub const fn g() -> u32 {\n"
                "        7\n"
                "    }\n"
                "\n"
                "    pub const fn f0() -> u32 {\n"
                "        use crate::gamma::g;\n"
                "        let z = {\n"
                "            const fn g() -> u32 {\n"
                "                21\n"
                "            }\n"
                "            g()\n"
                "        };\n"
                "        z\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_block_item_mod.src.a.inner.f0"
    assert (caller, "rs_block_item_mod.src.a.inner.g@9") in calls, calls
    assert (caller, "rs_block_item_mod.src.a.inner.g") not in calls, calls
    assert (caller, "rs_block_item_mod.src.gamma.g") not in calls, calls


def test_function_body_use_still_binds_outside_the_declaring_block(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The item scope is the block, not the whole body: a call written after
    # the block closes is out of the item's reach and binds the use again.
    project = temp_repo / "rs_block_item_after"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn g() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub const fn f0() -> u32 {\n"
                "    use crate::gamma::g;\n"
                "    let z = {\n"
                "        const fn g() -> u32 {\n"
                "            21\n"
                "        }\n"
                "        0\n"
                "    };\n"
                "    z + g()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_block_item_after.src.a.f0"
    assert (caller, "rs_block_item_after.src.gamma.g") in calls, calls


def test_block_item_of_another_name_leaves_the_body_use_alone(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An item scope only shadows the names it declares; a call inside the
    # block naming something else still binds the enclosing body's use.
    project = temp_repo / "rs_block_item_other"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn g() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub const fn f0() -> u32 {\n"
                "    use crate::gamma::g;\n"
                "    let z = {\n"
                "        const fn h() -> u32 {\n"
                "            21\n"
                "        }\n"
                "        g() + h()\n"
                "    };\n"
                "    z\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_block_item_other.src.a.f0"
    assert (caller, "rs_block_item_other.src.gamma.g") in calls, calls
