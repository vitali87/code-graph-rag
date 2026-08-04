"""A Rust trait-impl method overrides the trait ITS OWN block names (#1076).

The override pass rebuilt the relationship from the method's qualified name:
it split off the last segment for the method name and walked the implemented
traits in sorted order, taking the first that declared a matching method. Two
traits declaring one name broke that walk twice over.

The dedup variant `S.run@13` kept its `@13` suffix in the derived method name,
so the lookup for `Beta.run@13` missed and the second method got no OVERRIDES
edge at all, leaving it unreachable from every root. And the surviving edge
landed on whichever trait sorted first rather than the one written above the
method, so reversing the two impl blocks pointed it at the wrong trait.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _pairs,
    _write,
    create_and_run_updater,
)
from codebase_rag.types_defs import RelationshipType

_TRAITS = "pub trait Alpha { fn run(&self) -> u32; }\npub trait Beta { fn run(&self) -> u32; }\n\npub struct S;\n\n"

# Alpha first, so the block order and the alphabetical order AGREE. The bug
# hides here for the natural qn, which is why the reversed fixture below
# exists: this one pins only the variant.
_ALPHA_FIRST = _TRAITS + (
    "impl Alpha for S {\n"
    "    fn run(&self) -> u32 {\n"
    "        1\n"
    "    }\n"
    "}\n\n"
    "impl Beta for S {\n"
    "    fn run(&self) -> u32 {\n"
    "        2\n"
    "    }\n"
    "}\n"
)

# Beta first, so block order and alphabetical order DISAGREE. `S.run` is
# Beta's method and `S.run@13` is Alpha's, the opposite of the fixture above.
_BETA_FIRST = _TRAITS + (
    "impl Beta for S {\n"
    "    fn run(&self) -> u32 {\n"
    "        1\n"
    "    }\n"
    "}\n\n"
    "impl Alpha for S {\n"
    "    fn run(&self) -> u32 {\n"
    "        2\n"
    "    }\n"
    "}\n"
)


def _overrides(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)


def _run(temp_repo: Path, mock_ingestor: MagicMock, name: str, source: str) -> str:
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": f'[package]\nname = "{name}"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": source,
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    return f"{name}.src.foo"


def test_each_trait_impl_method_overrides_its_own_trait(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    base = _run(temp_repo, mock_ingestor, "rs_impl_override", _ALPHA_FIRST)
    overrides = _overrides(mock_ingestor)
    assert (f"{base}.S.run", f"{base}.Alpha.run") in overrides, overrides
    # The variant had no OVERRIDES edge of any kind: the derived method name
    # kept the `@13` suffix, so no trait declared a match.
    assert (f"{base}.S.run@13", f"{base}.Beta.run") in overrides, overrides
    assert (f"{base}.S.run@13", f"{base}.Alpha.run") not in overrides, overrides
    assert (f"{base}.S.run", f"{base}.Beta.run") not in overrides, overrides


def test_block_order_not_trait_name_decides_the_override_target(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Same two traits, impl blocks swapped. Every edge here is the mirror of
    # the test above, so an implementation that reads the sorted trait list
    # instead of the impl block passes that one and fails this one.
    base = _run(temp_repo, mock_ingestor, "rs_impl_override_rev", _BETA_FIRST)
    overrides = _overrides(mock_ingestor)
    assert (f"{base}.S.run", f"{base}.Beta.run") in overrides, overrides
    assert (f"{base}.S.run@13", f"{base}.Alpha.run") in overrides, overrides
    assert (f"{base}.S.run", f"{base}.Alpha.run") not in overrides, overrides
    assert (f"{base}.S.run@13", f"{base}.Beta.run") not in overrides, overrides
