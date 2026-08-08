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

from codebase_rag.constants import DUP_QN_MARKER
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


def test_inherent_method_sharing_a_trait_method_name_overrides_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `impl S` is inherent: it implements no trait, so its `run` overrides
    # nothing even though the trait impl above declares that name. Dropping the
    # dedup suffix for EVERY method, rather than only where an impl block
    # vouches for it, hands the ancestry walk a name it could not match before
    # and invents this edge. An OVERRIDES edge expands dead-code liveness, so
    # the invented one revives an unused inherent method.
    base = _run(
        temp_repo,
        mock_ingestor,
        "rs_impl_inherent",
        _TRAITS.replace("pub trait Beta { fn run(&self) -> u32; }\n", "")
        + (
            "impl Alpha for S {\n"
            "    fn run(&self) -> u32 { 1 }\n"
            "}\n\n"
            "impl S {\n"
            "    pub fn run(&self) -> u32 { 2 }\n"
            "}\n"
        ),
    )
    overrides = _overrides(mock_ingestor)
    assert (f"{base}.S.run", f"{base}.Alpha.run") in overrides, overrides
    assert not [
        pair for pair in overrides if pair[0].startswith(f"{base}.S.run{DUP_QN_MARKER}")
    ], overrides


def test_inherent_impl_written_first_still_overrides_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Same two blocks, inherent one FIRST, so the inherent method takes the
    # natural qn and the trait method takes the variant. The trait binding is
    # recorded only for the variant, and absence from that map used to send the
    # inherent method to the ancestry walk, which found the one trait `S`
    # implements and linked it. Rust has no inheritance: a method outside an
    # `impl Trait for` block overrides nothing, whatever it is named (#1078).
    base = _run(
        temp_repo,
        mock_ingestor,
        "rs_impl_inherent_first",
        _TRAITS.replace("pub trait Beta { fn run(&self) -> u32; }\n", "")
        + (
            "impl S {\n"
            "    pub fn run(&self) -> u32 { 2 }\n"
            "}\n\n"
            "impl Alpha for S {\n"
            "    fn run(&self) -> u32 { 1 }\n"
            "}\n"
        ),
    )
    overrides = _overrides(mock_ingestor)
    # The trait method's `fn` sits on line 10, so it holds the variant here.
    assert (
        f"{base}.S.run{DUP_QN_MARKER}10",
        f"{base}.Alpha.run",
    ) in overrides, overrides
    # Nothing at all from the natural qn, not merely no edge to Alpha: a gate
    # that silenced some other trait too would satisfy the narrower check.
    assert not [pair for pair in overrides if pair[0] == f"{base}.S.run"], overrides


def test_a_scoped_generic_trait_impl_is_not_mistaken_for_inherent(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `impl b::Base<u32> for S` is a trait impl, but the trait NAME extractor
    # reads nothing off a generic wrapped around a scoped path and returns
    # None, exactly as it does for a genuinely inherent block. Classifying by
    # that would bar a real trait impl's methods from ever overriding, and this
    # spelling is ordinary: std::ops::Add, serde::de::Visitor.
    # `Alpha` names `Base` as a supertrait, which is what puts `Base` in S's
    # ancestry: the scoped-generic block alone records no implementer, a
    # separate pre-existing gap this test does not speak to.
    project = temp_repo / "rs_scoped_generic_impl"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_scoped_generic_impl"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod b;\npub mod foo;\n",
            "src/b.rs": "pub trait Base<T> { fn run(&self) -> T; }\n",
            "src/foo.rs": (
                "pub trait Alpha: crate::b::Base<u32> { fn go(&self); }\n\n"
                "pub struct S;\n\n"
                "impl crate::b::Base<u32> for S {\n"
                "    fn run(&self) -> u32 { 1 }\n"
                "}\n\n"
                "impl Alpha for S {\n"
                "    fn go(&self) {}\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    overrides = _overrides(mock_ingestor)
    base = "rs_scoped_generic_impl.src"
    assert (f"{base}.foo.S.run", f"{base}.b.Base.run") in overrides, overrides
    assert (f"{base}.foo.S.go", f"{base}.foo.Alpha.go") in overrides, overrides


def test_a_generic_scoped_trait_impl_implements_the_trait_it_names(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The block above needed a supertrait to reach `Base` at all: on its own,
    # `impl crate::b::Base<u32> for S` read as no trait impl whatsoever, so no
    # IMPLEMENTS edge was queued and no implementer recorded (issue #1080).
    # The decoy sits in `a` and the real target in `z`, so the project-wide
    # simple-name sweep the old resolution ends in picks the wrong one and
    # only reading the WRITTEN path lands on `z`.
    project = temp_repo / "rs_scoped_generic_implements"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_scoped_generic_implements"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod a;\npub mod z;\npub mod foo;\n",
            "src/a.rs": "pub trait Base<T> { fn run(&self) -> T; }\n",
            "src/z.rs": "pub trait Base<T> { fn run(&self) -> T; }\n",
            "src/foo.rs": (
                "pub struct S;\n\n"
                "impl crate::z::Base<u32> for S {\n"
                "    fn run(&self) -> u32 { 1 }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    base = "rs_scoped_generic_implements.src"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _overrides(mock_ingestor)
    assert (f"{base}.foo.S", f"{base}.z.Base") in implements, implements
    assert (f"{base}.foo.S", f"{base}.a.Base") not in implements, implements
    assert (f"{base}.foo.S.run", f"{base}.z.Base.run") in overrides, overrides
    assert (f"{base}.foo.S.run", f"{base}.a.Base.run") not in overrides, overrides


def test_a_crate_path_to_a_re_exported_trait_keeps_its_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `pub use inner::Base;` in the crate root makes `crate::Base` legal, but
    # the trait registers where it is DECLARED. Reading the written path alone
    # lands on the entry module, which holds no such node, so the exact answer
    # has to fall back to the name-anchored one rather than emit nothing.
    project = temp_repo / "rs_reexported_trait"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_reexported_trait"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod inner;\npub mod foo;\npub use inner::Base;\n",
            "src/inner.rs": "pub trait Base { fn run(&self) -> u32; }\n",
            "src/foo.rs": (
                "pub struct S;\n\n"
                "impl crate::Base for S {\n"
                "    fn run(&self) -> u32 { 1 }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    base = "rs_reexported_trait.src"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _overrides(mock_ingestor)
    assert (f"{base}.foo.S", f"{base}.inner.Base") in implements, implements
    assert (f"{base}.foo.S.run", f"{base}.inner.Base.run") in overrides, overrides


def test_a_plain_scoped_trait_impl_implements_the_trait_it_names(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Without the generic wrapper the trait NAME does come out, but only as
    # the bare `Base`, whose resolution ends in a project-wide sweep any
    # same-named trait can answer. The decoy is deliberately in `a` and the
    # real target in `z`, so the sweep's own ordering picks the wrong one and
    # only the written path lands on the trait the block names.
    project = temp_repo / "rs_scoped_plain_implements"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_scoped_plain_implements"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod a;\npub mod z;\npub mod foo;\n",
            "src/a.rs": "pub trait Base { fn run(&self) -> u32; }\n",
            "src/z.rs": "pub trait Base { fn run(&self) -> u32; }\n",
            "src/foo.rs": (
                "pub struct S;\n\n"
                "impl crate::z::Base for S {\n"
                "    fn run(&self) -> u32 { 1 }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    base = "rs_scoped_plain_implements.src"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _overrides(mock_ingestor)
    assert (f"{base}.foo.S", f"{base}.z.Base") in implements, implements
    assert (f"{base}.foo.S", f"{base}.a.Base") not in implements, implements
    assert (f"{base}.foo.S.run", f"{base}.z.Base.run") in overrides, overrides
    assert (f"{base}.foo.S.run", f"{base}.a.Base.run") not in overrides, overrides


def test_a_re_exported_trait_resolves_through_the_binding_not_a_sweep(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `crate::Base` is exact about where to look, and the crate root's own
    # `pub use` says where the trait is declared. Falling back to the simple
    # name instead hands the choice to a project-wide sweep, which the decoy
    # in `a` answers first.
    project = temp_repo / "rs_reexport_decoy_trait"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_reexport_decoy_trait"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                "pub mod a;\npub mod inner;\npub mod foo;\npub use inner::Base;\n"
            ),
            "src/a.rs": "pub trait Base { fn run(&self) -> u32; }\n",
            "src/inner.rs": "pub trait Base { fn run(&self) -> u32; }\n",
            "src/foo.rs": (
                "pub struct S;\n\n"
                "impl crate::Base for S {\n"
                "    fn run(&self) -> u32 { 1 }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    base = "rs_reexport_decoy_trait.src"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _overrides(mock_ingestor)
    assert (f"{base}.foo.S", f"{base}.inner.Base") in implements, implements
    assert (f"{base}.foo.S", f"{base}.a.Base") not in implements, implements
    assert (f"{base}.foo.S.run", f"{base}.inner.Base.run") in overrides, overrides
    assert (f"{base}.foo.S.run", f"{base}.a.Base.run") not in overrides, overrides


def test_a_re_export_of_a_re_export_still_reaches_the_declaring_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A facade module that itself re-exports is the usual reason a crate root
    # can say `pub use facade::Base`. Following one binding and stopping at an
    # unregistered qn hands the rest of the chain back to the sweep.
    project = temp_repo / "rs_reexport_chain_trait"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_reexport_chain_trait"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                "pub mod a;\npub mod facade;\npub mod inner;\npub mod foo;\n"
                "pub use facade::Base;\n"
            ),
            "src/a.rs": "pub trait Base { fn run(&self) -> u32; }\n",
            "src/facade.rs": "pub use crate::inner::Base;\n",
            "src/inner.rs": "pub trait Base { fn run(&self) -> u32; }\n",
            "src/foo.rs": (
                "pub struct S;\n\n"
                "impl crate::Base for S {\n"
                "    fn run(&self) -> u32 { 1 }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    base = "rs_reexport_chain_trait.src"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _overrides(mock_ingestor)
    assert (f"{base}.foo.S", f"{base}.inner.Base") in implements, implements
    assert (f"{base}.foo.S", f"{base}.a.Base") not in implements, implements
    assert (f"{base}.foo.S.run", f"{base}.inner.Base.run") in overrides, overrides
    assert (f"{base}.foo.S.run", f"{base}.a.Base.run") not in overrides, overrides


def test_a_locally_bound_module_head_names_the_trait_it_was_bound_to(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use crate::z as alias;` then `impl alias::Base` is the same written
    # path one indirection on, and the binding says exactly which module the
    # head means. Testing the expansion only for externality throws that away
    # and hands a first-party spelling back to the sweep, where the decoy in
    # `a` answers first.
    project = temp_repo / "rs_alias_scoped_implements"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_alias_scoped_implements"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod a;\npub mod z;\npub mod foo;\n",
            "src/a.rs": "pub trait Base { fn run(&self) -> u32; }\n",
            "src/z.rs": "pub trait Base { fn run(&self) -> u32; }\n",
            "src/foo.rs": (
                "use crate::z as alias;\n\n"
                "pub struct S;\n\n"
                "impl alias::Base for S {\n"
                "    fn run(&self) -> u32 { 1 }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    base = "rs_alias_scoped_implements.src"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _overrides(mock_ingestor)
    assert (f"{base}.foo.S", f"{base}.z.Base") in implements, implements
    assert (f"{base}.foo.S", f"{base}.a.Base") not in implements, implements
    assert (f"{base}.foo.S.run", f"{base}.z.Base.run") in overrides, overrides
    assert (f"{base}.foo.S.run", f"{base}.a.Base.run") not in overrides, overrides


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
