# Rust `use crate::...` paths were stored raw in the import mapping, so a
# trait imported through them resolved to a phantom external qn
# (crate.flags.Flag) instead of the real project node. The IMPLEMENTS edge
# dangled onto an ExternalModule and the override pass emitted no OVERRIDES
# edges, so dead-code could not expand liveness from a live trait method to
# its implementations (ripgrep: 938 of 1811 candidates; issue #1007).
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable
from unittest.mock import MagicMock

import pytest

from codebase_rag.constants import RelationshipType
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_MAIN_RS = """\
mod flags;

fn main() {
    for flag in crate::flags::defs::FLAGS {
        println!("{}", flag.name_long());
    }
}
"""

_FLAGS_RS = """\
pub(crate) mod defs;

pub trait Flag {
    fn name_long(&self) -> &'static str;
}
"""

_DEFS_RS = """\
use crate::flags::Flag;

pub(crate) struct AfterContext;

impl Flag for AfterContext {
    fn name_long(&self) -> &'static str {
        "after-context"
    }
}

pub(crate) const FLAGS: &[&dyn Flag] = &[&AfterContext];
"""

_SUPER_DEFS_RS = """\
use super::Flag;

pub(crate) struct BeforeContext;

impl Flag for BeforeContext {
    fn name_long(&self) -> &'static str {
        "before-context"
    }
}
"""


def _pairs(mock_ingestor: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {
        (call[0][0][2], call[0][2][2]) for call in get_relationships(mock_ingestor, rel)
    }


def _write(project: Path, files: dict[str, str]) -> None:
    for rel_path, source in files.items():
        target = project / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoding="utf-8", data=source)


def test_crate_path_trait_links_in_src_layout(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "rs_crate_src"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_crate_src"\nversion = "0.1.0"\n',
            "src/main.rs": _MAIN_RS,
            "src/flags.rs": _FLAGS_RS,
            "src/flags/defs.rs": _DEFS_RS,
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    base = "rs_crate_src.src"

    assert (
        f"{base}.flags.defs.AfterContext",
        f"{base}.flags.Flag",
    ) in implements, implements
    assert (
        f"{base}.flags.defs.AfterContext.name_long",
        f"{base}.flags.Flag.name_long",
    ) in overrides, overrides


def test_crate_path_trait_links_without_src_dir(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # ripgrep's core crate layout: the entry point is crates/core/main.rs and
    # there is no src directory, so crate:: must resolve against the entry
    # point's directory, not a literal src segment.
    project = temp_repo / "rs_crate_flat"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_crate_flat"\nversion = "0.1.0"\n',
            "crates/core/main.rs": _MAIN_RS,
            "crates/core/flags.rs": _FLAGS_RS,
            "crates/core/flags/defs.rs": _DEFS_RS,
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    base = "rs_crate_flat.crates.core"

    assert (
        f"{base}.flags.defs.AfterContext",
        f"{base}.flags.Flag",
    ) in implements, implements
    assert (
        f"{base}.flags.defs.AfterContext.name_long",
        f"{base}.flags.Flag.name_long",
    ) in overrides, overrides


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return _pairs(mock_ingestor, RelationshipType.CALLS.value)


def test_inline_mod_super_wildcard_does_not_hijack_same_module_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use super::*;` inside an inline `mod tests` block means "import from
    # THIS file's module", not from the file's parent: super pops the inline
    # module, not the file. Rewriting it against the file qn pointed a live
    # wildcard at the parent module and rebound every bare call in the file
    # to the parent's same-named function.
    project = temp_repo / "rs_inline_super"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_inline_super"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": "pub mod bar;\n\npub fn helper() -> u32 {\n    1\n}\n",
            "src/foo/bar.rs": (
                "pub fn helper() -> u32 {\n"
                "    2\n"
                "}\n\n"
                "pub fn run() -> u32 {\n"
                "    helper()\n"
                "}\n\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n\n"
                "    #[test]\n"
                "    fn t() {\n"
                "        assert_eq!(run(), 2);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_inline_super.src"
    assert (f"{base}.foo.bar.run", f"{base}.foo.bar.helper") in calls, calls
    assert (f"{base}.foo.bar.run", f"{base}.foo.helper") not in calls, calls


def test_crate_path_trait_in_root_file_links(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The crate root MODULE is the entry file (src/main.rs -> proj.src.main),
    # not the src directory: `use crate::Flag` for a trait declared in the
    # entry file must resolve to proj.src.main.Flag, the most common home for
    # a crate's public traits.
    project = temp_repo / "rs_root_trait"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_root_trait"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "mod other;\n\n"
                "pub trait Flag {\n"
                "    fn name_long(&self) -> &'static str;\n"
                "}\n\n"
                "fn main() {\n"
                "    let flags: &[&dyn Flag] = &[&other::Mine];\n"
                "    for flag in flags {\n"
                '        println!("{}", flag.name_long());\n'
                "    }\n"
                "}\n"
            ),
            "src/other.rs": (
                "use crate::Flag;\n\n"
                "pub struct Mine;\n\n"
                "impl Flag for Mine {\n"
                "    fn name_long(&self) -> &'static str {\n"
                '        "mine"\n'
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    base = "rs_root_trait.src"

    assert (f"{base}.other.Mine", f"{base}.main.Flag") in implements, implements
    assert (
        f"{base}.other.Mine.name_long",
        f"{base}.main.Flag.name_long",
    ) in overrides, overrides


def test_crate_import_of_root_file_type_types_receiver(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use crate::Config` for a struct declared in src/main.rs must type the
    # receiver as proj.src.main.Config; a wrong rewrite (proj.src.Config)
    # leaves the type unresolved and the ambiguous name fallback binds an
    # unrelated type's method.
    project = temp_repo / "rs_root_type"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_root_type"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "mod alpha;\n"
                "mod user;\n\n"
                "pub struct Config;\n\n"
                "impl Config {\n"
                "    pub fn apply(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n\n"
                "fn main() {\n"
                '    println!("{}", user::f(Config));\n'
                "}\n"
            ),
            "src/alpha.rs": (
                "pub struct Alpha;\n\n"
                "impl Alpha {\n"
                "    pub fn apply(&self) -> u32 {\n"
                "        2\n"
                "    }\n"
                "}\n"
            ),
            "src/user.rs": (
                "use crate::Config;\n\npub fn f(c: Config) -> u32 {\n    c.apply()\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_root_type.src"
    assert (f"{base}.user.f", f"{base}.main.Config.apply") in calls, calls
    assert (f"{base}.user.f", f"{base}.alpha.Alpha.apply") not in calls, calls


def test_super_import_reaching_crate_root_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # From a depth-1 module, `super::` names the crate root module, which is
    # the ENTRY FILE (src/lib.rs -> proj.src.lib), not the src directory.
    project = temp_repo / "rs_super_root"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_super_root"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod impls;\n\npub trait Tr {\n    fn run(&self) -> u32;\n}\n"
            ),
            "src/impls.rs": (
                "use super::Tr;\n\n"
                "pub struct Mine;\n\n"
                "impl Tr for Mine {\n"
                "    fn run(&self) -> u32 {\n"
                "        3\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    base = "rs_super_root.src"

    assert (f"{base}.impls.Mine", f"{base}.lib.Tr") in implements, implements
    assert (f"{base}.impls.Mine.run", f"{base}.lib.Tr.run") in overrides, overrides


def test_super_sibling_module_path_links_and_imports(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use super::error::Err;` from src/foo.rs reaches the crate ROOT, whose
    # child modules are FILES beside the entry point (src/error.rs ->
    # p.src.error), never children of the entry module qn (p.src.lib.error).
    # The wrong reading loses the trait link and anchors IMPORTS at the
    # crate root instead of the error module.
    project = temp_repo / "rs_super_sibling"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_super_sibling"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod foo;\npub mod error;\n",
            "src/error.rs": ("pub trait Err {\n    fn code(&self) -> u32;\n}\n"),
            "src/foo.rs": (
                "use super::error::Err;\n\n"
                "pub struct F;\n\n"
                "impl Err for F {\n"
                "    fn code(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    imports = _pairs(mock_ingestor, RelationshipType.IMPORTS.value)
    base = "rs_super_sibling.src"

    assert (f"{base}.foo.F", f"{base}.error.Err") in implements, implements
    assert (f"{base}.foo.F.code", f"{base}.error.Err.code") in overrides, overrides
    assert (f"{base}.foo", f"{base}.error") in imports, imports
    assert (f"{base}.foo", f"{base}.lib") not in imports, imports


def test_self_reexport_in_entry_file_keeps_imports_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `pub use self::inner::Tr;` in src/lib.rs: self:: names the crate root
    # module, whose child `inner` is the FILE src/inner.rs.
    project = temp_repo / "rs_self_reexport"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_self_reexport"\nversion = "0.1.0"\n',
            "src/lib.rs": ("pub mod inner;\n\npub use self::inner::Tr;\n"),
            "src/inner.rs": ("pub trait Tr {\n    fn run(&self) -> u32;\n}\n"),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    imports = _pairs(mock_ingestor, RelationshipType.IMPORTS.value)
    base = "rs_self_reexport.src"
    assert (f"{base}.lib", f"{base}.inner") in imports, imports


def test_named_use_inside_inline_mod_does_not_hijack_file_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A NAMED import inside an inline `mod tests` block scopes to that
    # module; leaking it to file scope rebinds the file's own same-named
    # function calls to the imported one.
    project = temp_repo / "rs_inline_named"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_inline_named"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": "pub mod bar;\n\npub fn helper() -> u32 {\n    1\n}\n",
            "src/foo/bar.rs": (
                "pub fn helper() -> u32 {\n"
                "    2\n"
                "}\n\n"
                "pub fn run() -> u32 {\n"
                "    helper()\n"
                "}\n\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use crate::foo::helper;\n\n"
                "    #[test]\n"
                "    fn t() {\n"
                "        assert_eq!(helper(), 1);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_inline_named.src"
    assert (f"{base}.foo.bar.run", f"{base}.foo.bar.helper") in calls, calls
    assert (f"{base}.foo.bar.run", f"{base}.foo.helper") not in calls, calls


def test_bin_crate_root_item_prefers_declaring_entry(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # src/lib.rs + src/main.rs in one package: `use crate::Err` written in a
    # module that main.rs declares belongs to the BIN crate, so the item
    # resolves in src/main.rs, not src/lib.rs.
    project = temp_repo / "rs_bin_lib"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_bin_lib"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod shared;\n",
            "src/shared.rs": "pub fn s() -> u32 {\n    0\n}\n",
            "src/main.rs": (
                "mod cli;\n\n"
                "pub trait Err {\n"
                "    fn code(&self) -> u32;\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/cli.rs": (
                "use crate::Err;\n\n"
                "pub struct F;\n\n"
                "impl Err for F {\n"
                "    fn code(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    base = "rs_bin_lib.src"

    assert (f"{base}.cli.F", f"{base}.main.Err") in implements, implements
    assert (f"{base}.cli.F.code", f"{base}.main.Err.code") in overrides, overrides


def test_root_item_sharing_lowercase_module_name_stays_root_item(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use crate::Err` where the entry file declares trait Err AND a module
    # file err.rs exists: on a case-insensitive filesystem a naive
    # (dir / "Err.rs").is_file() probe matches err.rs and misclassifies the
    # ITEM as a submodule. Type-vs-snake_case module is the normal Rust
    # naming convention, not a contrived collision.
    project = temp_repo / "rs_case_probe"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_case_probe"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod err;\n"
                "pub mod cli;\n\n"
                "pub trait Err {\n"
                "    fn code(&self) -> u32;\n"
                "}\n"
            ),
            "src/err.rs": "pub fn e() -> u32 {\n    0\n}\n",
            "src/cli.rs": (
                "use crate::Err;\n\n"
                "pub struct F;\n\n"
                "impl Err for F {\n"
                "    fn code(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    base = "rs_case_probe.src"

    assert (f"{base}.cli.F", f"{base}.lib.Err") in implements, implements
    assert (f"{base}.cli.F.code", f"{base}.lib.Err.code") in overrides, overrides


def test_src_bin_file_crate_resolves_its_own_root(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # src/bin/tool.rs is its OWN crate root: `use crate::Cmd` in its module
    # tree (src/bin/tool/helper.rs) names the trait in tool.rs, not the lib
    # crate's same-named trait in src/lib.rs.
    project = temp_repo / "rs_bin_target"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_bin_target"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod util;\n\npub trait Cmd {\n    fn run(&self) -> u32;\n}\n"
            ),
            "src/util.rs": "pub fn u() -> u32 {\n    0\n}\n",
            "src/bin/tool.rs": (
                "mod helper;\n\n"
                "pub trait Cmd {\n"
                "    fn run(&self) -> u32;\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/bin/tool/helper.rs": (
                "use crate::Cmd;\n\n"
                "pub struct H;\n\n"
                "impl Cmd for H {\n"
                "    fn run(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    base = "rs_bin_target.src"

    assert (
        f"{base}.bin.tool.helper.H",
        f"{base}.bin.tool.Cmd",
    ) in implements, implements
    assert (
        f"{base}.bin.tool.helper.H",
        f"{base}.lib.Cmd",
    ) not in implements, implements
    assert (
        f"{base}.bin.tool.helper.H.run",
        f"{base}.bin.tool.Cmd.run",
    ) in overrides, overrides


def test_file_level_glob_import_does_not_shadow_local_items(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Rust glob imports NEVER shadow items defined in the importing module;
    # a live wildcard target must not outrank same-module resolution.
    project = temp_repo / "rs_glob_local"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_glob_local"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod util;\npub mod work;\npub mod foo;\n",
            "src/util.rs": "pub fn render() -> u32 {\n    1\n}\n",
            "src/work.rs": (
                "use crate::util::*;\n\n"
                "pub fn render() -> u32 {\n"
                "    2\n"
                "}\n\n"
                "pub fn go() -> u32 {\n"
                "    render()\n"
                "}\n"
            ),
            "src/foo.rs": "pub mod bar;\n\npub fn helper() -> u32 {\n    1\n}\n",
            "src/foo/bar.rs": (
                "use super::*;\n\n"
                "pub fn helper() -> u32 {\n"
                "    2\n"
                "}\n\n"
                "pub fn run() -> u32 {\n"
                "    helper()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_glob_local.src"
    assert (f"{base}.work.go", f"{base}.work.render") in calls, calls
    assert (f"{base}.work.go", f"{base}.util.render") not in calls, calls
    assert (f"{base}.foo.bar.run", f"{base}.foo.bar.helper") in calls, calls
    assert (f"{base}.foo.bar.run", f"{base}.foo.helper") not in calls, calls


def test_attribute_prefixed_mod_declaration_counts_for_entry_choice(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `#[cfg(unix)] mod cli;` on ONE line is the idiomatic spelling; the
    # entry-crate chooser must still see main.rs declaring cli.
    project = temp_repo / "rs_attr_mod"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_attr_mod"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod shared;\n",
            "src/shared.rs": "pub fn s() -> u32 {\n    0\n}\n",
            "src/main.rs": (
                "#[cfg(unix)] mod cli;\n\n"
                "pub trait Err {\n"
                "    fn code(&self) -> u32;\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/cli.rs": (
                "use crate::Err;\n\n"
                "pub struct F;\n\n"
                "impl Err for F {\n"
                "    fn code(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_attr_mod.src"
    assert (f"{base}.cli.F", f"{base}.main.Err") in implements, implements


def test_mapped_but_unregistered_target_falls_back_to_registry(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use crate::Config` where the entry file does NOT declare Config: the
    # rewritten qn (src.main.Config) is unregistered and must not be returned
    # verbatim; registry-backed resolution finds the real declaration.
    project = temp_repo / "rs_unregistered"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_unregistered"\nversion = "0.1.0"\n',
            "src/main.rs": ("mod other;\nmod user;\n\nfn main() {}\n"),
            "src/other.rs": (
                "pub struct Config;\n\n"
                "impl Config {\n"
                "    pub fn apply(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
            "src/user.rs": (
                "use crate::Config;\n\npub fn f(c: Config) -> u32 {\n    c.apply()\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_unregistered.src"
    assert (f"{base}.user.f", f"{base}.other.Config.apply") in calls, calls


def test_shared_module_tie_resolves_item_by_declaring_entry(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # BOTH src/lib.rs and src/main.rs declare `mod cli;` (the file compiles
    # into both crates); `crate::Err` from cli.rs must bind the entry that
    # actually DECLARES Err, not whichever entry a tie-break happens to pick.
    project = temp_repo / "rs_tie_item"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_tie_item"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod cli;\n\npub fn shared() -> u32 {\n    0\n}\n",
            "src/main.rs": (
                "mod cli;\n\n"
                "pub trait Err {\n"
                "    fn code(&self) -> u32;\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/cli.rs": (
                "use crate::Err;\n\n"
                "pub struct F;\n\n"
                "impl Err for F {\n"
                "    fn code(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_tie_item.src"
    assert (f"{base}.cli.F", f"{base}.main.Err") in implements, implements


def test_string_literal_comment_marker_does_not_hide_mod_decl(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A string literal containing "/*" must not swallow the following
    # `mod cli;` declaration when the entry chooser scans main.rs.
    project = temp_repo / "rs_str_marker"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_str_marker"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod shared;\n\npub trait Err {\n    fn code(&self) -> u32;\n}\n"
            ),
            "src/shared.rs": "pub fn s() -> u32 {\n    0\n}\n",
            "src/main.rs": (
                'const PAT: &str = "/*";\n'
                "mod cli;\n"
                "/* a block comment */\n"
                "pub trait Err {\n"
                "    fn code(&self) -> u32;\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/cli.rs": (
                "use crate::Err;\n\n"
                "pub struct F;\n\n"
                "impl Err for F {\n"
                "    fn code(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_str_marker.src"
    assert (f"{base}.cli.F", f"{base}.main.Err") in implements, implements
    assert (f"{base}.cli.F", f"{base}.lib.Err") not in implements, implements


def test_nested_block_comment_hides_mod_decl(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Rust block comments NEST: a `mod cli;` inside an outer comment that
    # also contains an inner comment stays commented out, so lib.rs must not
    # steal cli from the main.rs that really declares it.
    project = temp_repo / "rs_nested_comment"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_nested_comment"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                "/* outer\n"
                "  /* inner */\n"
                "  mod cli;\n"
                "*/\n"
                "pub mod shared;\n\n"
                "pub trait Err {\n"
                "    fn code(&self) -> u32;\n"
                "}\n"
            ),
            "src/shared.rs": "pub fn s() -> u32 {\n    0\n}\n",
            "src/main.rs": (
                "mod cli;\n\n"
                "pub trait Err {\n"
                "    fn code(&self) -> u32;\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/cli.rs": (
                "use crate::Err;\n\n"
                "pub struct F;\n\n"
                "impl Err for F {\n"
                "    fn code(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_nested_comment.src"
    assert (f"{base}.cli.F", f"{base}.main.Err") in implements, implements
    assert (f"{base}.cli.F", f"{base}.lib.Err") not in implements, implements


def test_module_named_main_is_not_a_crate_entry(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # src/app/main.rs here is a plain module DECLARED by app.rs, not a crate
    # entry (verified against rustc: `self::foo` inside app::main is
    # app::main::foo from src/app/main/foo.rs, never the sibling
    # src/app/foo.rs).
    project = temp_repo / "rs_mod_named_main"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_mod_named_main"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod app;\n",
            "src/app.rs": "pub mod main;\npub mod foo;\n",
            "src/app/foo.rs": ("pub trait Sib {\n    fn s(&self) -> u32;\n}\n"),
            "src/app/main.rs": (
                "pub mod foo;\n\n"
                "use self::foo::Sib;\n\n"
                "pub struct M;\n\n"
                "impl Sib for M {\n"
                "    fn s(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
            "src/app/main/foo.rs": ("pub trait Sib {\n    fn s(&self) -> u32;\n}\n"),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_mod_named_main.src"
    assert (
        f"{base}.app.main.M",
        f"{base}.app.main.foo.Sib",
    ) in implements, implements
    assert (f"{base}.app.main.M", f"{base}.app.foo.Sib") not in implements, implements


def test_call_inside_inline_mod_uses_its_own_imports(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A call INSIDE `mod tests` must resolve through the inline module's own
    # imports (its `use crate::foo::helper` shadows the file's helper for
    # code in the mod), while file-level calls stay on the file's items.
    project = temp_repo / "rs_inline_scope_calls"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_inline_scope_calls"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": "pub mod bar;\n\npub fn helper() -> u32 {\n    1\n}\n",
            "src/foo/bar.rs": (
                "pub fn helper() -> u32 {\n"
                "    2\n"
                "}\n\n"
                "pub fn run() -> u32 {\n"
                "    helper()\n"
                "}\n\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use crate::foo::helper;\n\n"
                "    #[test]\n"
                "    fn t() {\n"
                "        assert_eq!(helper(), 1);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_inline_scope_calls.src"
    assert (f"{base}.foo.bar.tests.t", f"{base}.foo.helper") in calls, calls
    assert (f"{base}.foo.bar.tests.t", f"{base}.foo.bar.helper") not in calls, calls
    assert (f"{base}.foo.bar.run", f"{base}.foo.bar.helper") in calls, calls


def test_enum_variant_use_path_keeps_imports_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use crate::color::Color::Red;` has TWO non-module tail segments (type,
    # variant); the IMPORTS edge must still anchor at the color module
    # instead of being dropped.
    project = temp_repo / "rs_variant_use"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_variant_use"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod color;\npub mod paint;\n",
            "src/color.rs": ("pub enum Color {\n    Red,\n    Blue,\n}\n"),
            "src/paint.rs": (
                "use crate::color::Color::Red;\n\n"
                "pub fn pick() -> u32 {\n"
                "    let _ = Red;\n"
                "    0\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    imports = _pairs(mock_ingestor, RelationshipType.IMPORTS.value)
    base = "rs_variant_use.src"
    assert (f"{base}.paint", f"{base}.color") in imports, imports


def test_super_path_trait_links(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    # `use super::Flag;` names the parent module; it must resolve to the
    # importer's parent qn, not externalise as super.Flag.
    project = temp_repo / "rs_super"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_super"\nversion = "0.1.0"\n',
            "src/main.rs": _MAIN_RS,
            "src/flags.rs": _FLAGS_RS,
            "src/flags/defs.rs": _SUPER_DEFS_RS,
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    base = "rs_super.src"

    assert (
        f"{base}.flags.defs.BeforeContext",
        f"{base}.flags.Flag",
    ) in implements, implements
    assert (
        f"{base}.flags.defs.BeforeContext.name_long",
        f"{base}.flags.Flag.name_long",
    ) in overrides, overrides


def test_definitive_lib_module_ignores_main_item_declaration(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Only lib.rs declares `mod user;`, so `crate::` in src/user.rs can ONLY
    # mean the lib crate: a same-named item in the separate bin crate
    # (src/main.rs) must not attract the import via the item tie-break.
    project = temp_repo / "rs_lib_definitive"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_lib_definitive"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod config;\npub mod user;\n\npub use crate::config::Config;\n"
            ),
            "src/config.rs": (
                "pub struct Config;\n\n"
                "impl Config {\n"
                "    pub fn apply(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
            "src/user.rs": (
                "use crate::Config;\n\npub fn f(c: Config) -> u32 {\n    c.apply()\n}\n"
            ),
            "src/main.rs": (
                "pub struct Config;\n\n"
                "impl Config {\n"
                "    pub fn apply(&self) -> u32 {\n"
                "        99\n"
                "    }\n"
                "}\n\n"
                "fn main() {}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_lib_definitive.src"
    assert (f"{base}.user.f", f"{base}.main.Config.apply") not in calls, calls
    imports = _pairs(mock_ingestor, RelationshipType.IMPORTS.value)
    assert (f"{base}.user", f"{base}.main") not in imports, imports


def test_mod_decl_inside_inline_block_does_not_count_for_entry(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `pub mod sys { pub mod unix; }` in lib.rs declares src/sys/unix.rs, a
    # DIFFERENT file from src/unix.rs; the nested `mod unix;` must not make
    # the entry chooser attribute src/unix.rs (declared only by main.rs) to
    # the lib crate.
    project = temp_repo / "rs_nested_mod_decl"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_nested_mod_decl"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod api;\n\n"
                "pub mod sys {\n"
                "    pub mod unix;\n"
                "}\n\n"
                "pub trait Runner {\n"
                "    fn go(&self) -> u32;\n"
                "}\n"
            ),
            "src/api.rs": "pub fn a() -> u32 {\n    0\n}\n",
            "src/sys/unix.rs": "pub fn s() -> u32 {\n    0\n}\n",
            "src/main.rs": (
                "mod unix;\n\n"
                "pub trait Runner {\n"
                "    fn go(&self) -> u32;\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/unix.rs": (
                "use crate::Runner;\n\n"
                "pub struct U;\n\n"
                "impl Runner for U {\n"
                "    fn go(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_nested_mod_decl.src"
    assert (f"{base}.unix.U", f"{base}.main.Runner") in implements, implements
    assert (f"{base}.unix.U", f"{base}.lib.Runner") not in implements, implements


def test_mod_rs_module_dir_with_incidental_main_is_not_crate_root(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # src/foo/ is a module directory in the mod.rs spelling; an incidental
    # src/foo/main.rs is just the module foo::main, so `crate::` inside
    # src/foo/bar.rs must still reach the real crate root at src/.
    project = temp_repo / "rs_modrs_main"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_modrs_main"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod foo;\n\npub trait Tr {\n    fn go(&self) -> u32;\n}\n"
            ),
            "src/foo/mod.rs": "pub mod main;\npub mod bar;\n",
            "src/foo/main.rs": "pub fn m() -> u32 {\n    0\n}\n",
            "src/foo/bar.rs": (
                "use crate::Tr;\n\n"
                "pub struct B;\n\n"
                "impl Tr for B {\n"
                "    fn go(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_modrs_main.src"
    assert (f"{base}.foo.bar.B", f"{base}.lib.Tr") in implements, implements
    assert (f"{base}.foo.bar.B", f"{base}.foo.main.Tr") not in implements, implements


def test_escaped_quote_char_literal_keeps_entry_scan_alive(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # '\'' contains an ESCAPED quote: a lexer that pairs the first following
    # quote closes the literal too early, the orphan quote swallows the rest
    # of the entry file, and main.rs appears to declare nothing.
    project = temp_repo / "rs_char_escape"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_char_escape"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod shared;\n\npub trait Err {\n    fn code(&self) -> u32;\n}\n"
            ),
            "src/shared.rs": "pub fn s() -> u32 {\n    0\n}\n",
            "src/main.rs": (
                "const SPECIALS: [char; 3] = ['\\'', '\"', '\\\\'];\n\n"
                "mod cli;\n\n"
                "pub trait Err {\n"
                "    fn code(&self) -> u32;\n"
                "}\n\n"
                'fn main() {\n    println!("{:?}", SPECIALS);\n}\n'
            ),
            "src/cli.rs": (
                "use crate::Err;\n\n"
                "pub struct F;\n\n"
                "impl Err for F {\n"
                "    fn code(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_char_escape.src"
    assert (f"{base}.cli.F", f"{base}.main.Err") in implements, implements
    assert (f"{base}.cli.F", f"{base}.lib.Err") not in implements, implements


def test_inline_mod_block_does_not_claim_sibling_file_for_crate(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # lib.rs holds an INLINE `pub mod sys { ... }`, which pulls no file into
    # the lib crate; src/sys.rs is declared only by main.rs, so `crate::` in
    # it can only mean the bin crate.
    project = temp_repo / "rs_inline_claim"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_inline_claim"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod sys {\n"
                "    pub fn tick() -> u32 {\n"
                "        0\n"
                "    }\n"
                "}\n\n"
                "pub trait Runner {\n"
                "    fn go(&self) -> u32;\n"
                "}\n"
            ),
            "src/main.rs": (
                "mod sys;\n\n"
                "pub trait Runner {\n"
                "    fn go(&self) -> u32;\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/sys.rs": (
                "use crate::Runner;\n\n"
                "pub struct U;\n\n"
                "impl Runner for U {\n"
                "    fn go(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_inline_claim.src"
    assert (f"{base}.sys.U", f"{base}.main.Runner") in implements, implements
    assert (f"{base}.sys.U", f"{base}.lib.Runner") not in implements, implements


def test_entry_inline_module_wins_over_other_crates_sibling_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # src/inner.rs is definitively in the lib crate, and lib.rs declares sys
    # as an INLINE module: crate::sys::tick must reach src.lib.sys.tick, not
    # the bin crate's src/sys.rs that happens to sit beside the entry.
    project = temp_repo / "rs_inline_wins"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_inline_wins"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod inner;\n\n"
                "pub mod sys {\n"
                "    pub fn tick() -> u32 {\n"
                "        0\n"
                "    }\n"
                "}\n"
            ),
            "src/inner.rs": (
                "use crate::sys::tick;\n\npub fn go() -> u32 {\n    tick()\n}\n"
            ),
            "src/main.rs": (
                'mod sys;\n\nfn main() {\n    println!("{}", sys::tick());\n}\n'
            ),
            "src/sys.rs": "pub fn tick() -> u32 {\n    9\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_inline_wins.src"
    assert (f"{base}.inner.go", f"{base}.lib.sys.tick") in calls, calls
    assert (f"{base}.inner.go", f"{base}.sys.tick") not in calls, calls


def test_macro_rules_body_mod_declaration_counts_for_entry(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A `mod cli;` emitted from a macro body still declares src/cli.rs; the
    # brace-depth filter must not blind the entry scan to it.
    project = temp_repo / "rs_macro_decl"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_macro_decl"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod shared;\n\npub trait Err {\n    fn code(&self) -> u32;\n}\n"
            ),
            "src/shared.rs": "pub fn s() -> u32 {\n    0\n}\n",
            "src/main.rs": (
                "macro_rules! declare { () => { mod cli; }; }\n"
                "declare!();\n\n"
                "pub trait Err {\n"
                "    fn code(&self) -> u32;\n"
                "}\n\n"
                "fn main() {\n    let _ = cli::F;\n}\n"
            ),
            "src/cli.rs": (
                "use crate::Err;\n\n"
                "pub struct F;\n\n"
                "impl Err for F {\n"
                "    fn code(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_macro_decl.src"
    assert (f"{base}.cli.F", f"{base}.main.Err") in implements, implements
    assert (f"{base}.cli.F", f"{base}.lib.Err") not in implements, implements


def test_macro_invocation_body_mod_declaration_counts_for_entry(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The cfg_if! shape used by libc/backtrace/getrandom: platform `mod`
    # declarations live inside a macro invocation's brace body.
    project = temp_repo / "rs_cfgif_decl"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_cfgif_decl"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod shared;\n\npub trait Err {\n    fn code(&self) -> u32;\n}\n"
            ),
            "src/shared.rs": "pub fn s() -> u32 {\n    0\n}\n",
            "src/main.rs": (
                "cfg_if::cfg_if! {\n"
                "    if #[cfg(unix)] {\n"
                "        mod cli;\n"
                "    }\n"
                "}\n\n"
                "pub trait Err {\n"
                "    fn code(&self) -> u32;\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/cli.rs": (
                "use crate::Err;\n\n"
                "pub struct F;\n\n"
                "impl Err for F {\n"
                "    fn code(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_cfgif_decl.src"
    assert (f"{base}.cli.F", f"{base}.main.Err") in implements, implements
    assert (f"{base}.cli.F", f"{base}.lib.Err") not in implements, implements


def test_raw_identifier_mod_declaration_counts_for_entry(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `mod r#type;` declares src/type.rs (windows-core, zerocopy,
    # derive_more all ship this); the raw-identifier prefix must not hide
    # the declaration from the entry scan.
    project = temp_repo / "rs_raw_ident"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_raw_ident"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod shared;\n\npub trait Err {\n    fn code(&self) -> u32;\n}\n"
            ),
            "src/shared.rs": "pub fn s() -> u32 {\n    0\n}\n",
            "src/main.rs": (
                "mod r#type;\n\n"
                "pub trait Err {\n"
                "    fn code(&self) -> u32;\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/type.rs": (
                "use crate::Err;\n\n"
                "pub struct F;\n\n"
                "impl Err for F {\n"
                "    fn code(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_raw_ident.src"
    assert (f"{base}.type.F", f"{base}.main.Err") in implements, implements
    assert (f"{base}.type.F", f"{base}.lib.Err") not in implements, implements


def test_item_pattern_matches_static_mut_extern_fn_and_raw_idents() -> None:
    # log's `static mut LOGGER` and signal-hook-registry's `extern "C" fn`
    # are real entry-file items the tie-break must see.
    from codebase_rag.parsers.import_processor import (
        _RS_ITEM_DECL_PATTERN,
        _RS_MOD_DECL_PATTERN,
        _rs_strip_comments_and_strings,
        _rs_top_level_only,
    )

    source = (
        "static mut LOGGER: u32 = 0;\n"
        'pub unsafe extern "C" fn handler() {}\n'
        "pub mod r#async;\n"
        "pub fn r#type() -> u32 {\n    0\n}\n"
    )
    top = _rs_top_level_only(_rs_strip_comments_and_strings(source))
    items = set(_RS_ITEM_DECL_PATTERN.findall(top))
    assert {"LOGGER", "handler", "type"} <= items, items
    mods = set(_RS_MOD_DECL_PATTERN.findall(top))
    assert "async" in mods, mods


def test_inline_mod_use_of_entry_reexport_resolves_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # lib.rs owns `make` only through `pub use self::inner::make;`, so the
    # inline test mod's `use crate::make` maps to src.lib.make, a qn absent
    # from the registry. The re-exporting module's own import map holds the
    # defining qn one hop away; dropping the edge instead severs the
    # function's only reference and it reads as dead.
    project = temp_repo / "rs_reexport_scope"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_reexport_scope"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod inner;\npub mod work;\npub use self::inner::make;\n"
            ),
            "src/inner.rs": "pub fn make() -> u32 {\n    1\n}\n",
            "src/work.rs": (
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use crate::make;\n\n"
                "    #[test]\n"
                "    fn t() {\n"
                "        assert_eq!(make(), 1);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_reexport_scope.src"
    assert (f"{base}.work.tests.t", f"{base}.inner.make") in calls, calls


def test_function_body_use_shadows_same_module_item(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A `use` inside a function body legally shadows a same-named module
    # item within that function (unlike a module-scoped named use, which
    # would be E0255): go() returns 2, not 1, under cargo.
    project = temp_repo / "rs_fn_body_use"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_fn_body_use"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod inner;\npub mod work;\n",
            "src/inner.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/work.rs": (
                "pub fn helper() -> u32 {\n"
                "    1\n"
                "}\n\n"
                "pub fn go() -> u32 {\n"
                "    use crate::inner::helper;\n"
                "    helper()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_fn_body_use.src"
    assert (f"{base}.work.go", f"{base}.inner.helper") in calls, calls
    assert (f"{base}.work.go", f"{base}.work.helper") not in calls, calls


def test_diverging_fn_body_is_not_macro_transparent() -> None:
    # `fn abort() -> ! {` ends in `!` before the brace, but it opens an item
    # body, not a macro body: a macro invocation's `!` follows the macro
    # NAME. Declarations inside the diverging body must stay invisible.
    from codebase_rag.parsers.import_processor import (
        _RS_ITEM_DECL_PATTERN,
        _RS_MOD_DECL_PATTERN,
        _rs_strip_comments_and_strings,
        _rs_top_level_only,
    )

    source = (
        "fn abort() -> ! {\n"
        "    mod sneaky;\n"
        "    struct Hidden;\n"
        "    loop {}\n"
        "}\n"
        "mod real;\n"
    )
    top = _rs_top_level_only(_rs_strip_comments_and_strings(source))
    mods = set(_RS_MOD_DECL_PATTERN.findall(top))
    assert mods == {"real"}, mods
    items = set(_RS_ITEM_DECL_PATTERN.findall(top))
    assert "sneaky" not in items, items
    assert "Hidden" not in items, items


def test_spaced_pub_visibility_declarations_count() -> None:
    # `pub (crate) mod sub;` compiles: whitespace between `pub` and the
    # visibility parens is legal and must not hide the declaration.
    from codebase_rag.parsers.import_processor import (
        _RS_ITEM_DECL_PATTERN,
        _RS_MOD_DECL_PATTERN,
        _rs_strip_comments_and_strings,
        _rs_top_level_only,
    )

    source = "pub (crate) mod sub;\npub (crate) struct S;\n"
    top = _rs_top_level_only(_rs_strip_comments_and_strings(source))
    mods = set(_RS_MOD_DECL_PATTERN.findall(top))
    assert mods == {"sub"}, mods
    items = set(_RS_ITEM_DECL_PATTERN.findall(top))
    assert {"S", "sub"} <= items, items


def test_method_body_use_keys_under_method_qn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A use inside a METHOD body must store under the method's qn
    # (module.S.run, impl blocks are qn scopes) rather than under the free
    # function sharing its name: keyed at module.run it both rebinds the
    # method's call to the wrong helper and leaks into the free run().
    project = temp_repo / "rs_method_body_use"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_method_body_use"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/foo.rs": (
                "pub struct S;\n\n"
                "impl S {\n"
                "    pub fn run(&self) -> u32 {\n"
                "        use crate::alpha::helper;\n"
                "        helper()\n"
                "    }\n"
                "}\n\n"
                "pub fn helper() -> u32 {\n"
                "    1\n"
                "}\n\n"
                "pub fn run() -> u32 {\n"
                "    helper()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_method_body_use.src"
    assert (f"{base}.foo.S.run", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo.S.run", f"{base}.foo.helper") not in calls, calls
    assert (f"{base}.foo.run", f"{base}.foo.helper") in calls, calls
    assert (f"{base}.foo.run", f"{base}.alpha.helper") not in calls, calls


def test_inline_mod_inside_function_body_keeps_module_scope_key(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Functions are NOT qn scopes: `mod n` declared inside a function body
    # registers its items at module.n, so a use inside that mod must store
    # at module.n too, not module.outer.n where nothing reads it.
    project = temp_repo / "rs_fn_body_mod"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_fn_body_mod"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod alpha;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/foo.rs": (
                "pub fn helper() -> u32 {\n"
                "    1\n"
                "}\n\n"
                "pub fn outer() -> u32 {\n"
                "    mod n {\n"
                "        use crate::alpha::helper;\n\n"
                "        pub fn deep() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "    n::deep()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_fn_body_mod.src"
    assert (f"{base}.foo.n.deep", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo.n.deep", f"{base}.foo.helper") not in calls, calls


def test_nested_fn_body_use_applies_to_nested_fn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A nested fn registers FLAT (module.inner, functions are not scopes),
    # so a use inside its body must key at module.inner for the caller's
    # scope walk to find it.
    project = temp_repo / "rs_nested_fn_use"
    _write(
        project,
        {
            "Cargo.toml": ('[package]\nname = "rs_nested_fn_use"\nversion = "0.1.0"\n'),
            "src/lib.rs": "pub mod alpha;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/foo.rs": (
                "pub fn helper() -> u32 {\n"
                "    1\n"
                "}\n\n"
                "pub fn outer() -> u32 {\n"
                "    fn inner() -> u32 {\n"
                "        use crate::alpha::helper;\n"
                "        helper()\n"
                "    }\n"
                "    inner()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_nested_fn_use.src"
    assert (f"{base}.foo.inner", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo.inner", f"{base}.foo.helper") not in calls, calls


def test_duplicate_method_qn_owns_its_body_use_and_its_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Two traits implemented for the SAME type both name their method `run`,
    # so the second registers as the dedup variant S.run@13. Its body `use`
    # must key on THAT variant, not on the first impl's natural qn: Alpha::run
    # has no use and its bare call binds the same-module foo::other.
    project = temp_repo / "rs_dup_method_use"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_dup_method_use"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod alpha;\npub mod foo;\n",
            "src/alpha.rs": "pub fn other() -> u32 { 2 }\n",
            "src/foo.rs": (
                "pub trait Alpha { fn run(&self) -> u32; }\n"
                "pub trait Beta { fn run(&self) -> u32; }\n\n"
                "pub struct S;\n\n"
                "impl Alpha for S {\n"
                "    fn run(&self) -> u32 {\n"
                "        other()\n"
                "    }\n"
                "}\n\n"
                "impl Beta for S {\n"
                "    fn run(&self) -> u32 {\n"
                "        use crate::alpha::other;\n"
                "        other() + beta_only()\n"
                "    }\n"
                "}\n\n"
                "pub fn other() -> u32 { 1 }\n"
                "pub fn beta_only() -> u32 { 3 }\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_dup_method_use.src"
    assert (f"{base}.foo.S.run", f"{base}.foo.other") in calls, calls
    assert (f"{base}.foo.S.run", f"{base}.alpha.other") not in calls, calls
    # And the caller side keys on the variant too (issue #1014): both methods'
    # calls used to be attributed to the natural qn, leaving the variant with
    # no outgoing edge at all. `beta_only` is called with no `use` in sight, so
    # it pins caller attribution itself rather than the scope-import lookup
    # that `other` alone would resolve through.
    assert (f"{base}.foo.S.run@13", f"{base}.alpha.other") in calls, calls
    assert (f"{base}.foo.S.run@13", f"{base}.foo.beta_only") in calls, calls
    assert (f"{base}.foo.S.run", f"{base}.foo.beta_only") not in calls, calls
    # Attribution moves calls, it does not copy them: a variant that collected
    # BOTH methods' calls would satisfy every assertion above.
    assert (f"{base}.foo.S.run@13", f"{base}.foo.other") not in calls, calls
    scope_uses = updater.factory.import_processor.rust_fn_scope_imports.get(
        f"{base}.foo.S.run@13"
    )
    assert scope_uses == {"other": f"{base}.alpha.other"}, scope_uses


def test_duplicate_method_qn_external_use_does_not_drop_sibling_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The second `run`'s external `use std::cmp::max as pick` must not land
    # on the first `run`'s qn, where the deliberate external-import drop
    # deletes Alpha::run's real edge to the project's foo::pick.
    project = temp_repo / "rs_dup_method_ext_use"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_dup_method_ext_use"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": (
                "pub trait Alpha { fn run(&self) -> u32; }\n"
                "pub trait Beta { fn run(&self) -> u32; }\n\n"
                "pub struct S;\n\n"
                "impl Alpha for S {\n"
                "    fn run(&self) -> u32 {\n"
                "        pick(1, 2)\n"
                "    }\n"
                "}\n\n"
                "impl Beta for S {\n"
                "    fn run(&self) -> u32 {\n"
                "        use std::cmp::max as pick;\n"
                "        pick(1, 2)\n"
                "    }\n"
                "}\n\n"
                "pub fn pick(a: u32, b: u32) -> u32 { if a > b { a } else { b } }\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_dup_method_ext_use.src"
    assert (f"{base}.foo.S.run", f"{base}.foo.pick") in calls, calls


def test_nested_fn_use_does_not_leak_into_same_named_top_level_fn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `other` at file scope registers first, so the nested `other` inside
    # outer() becomes the dedup variant other@8. The nested body's use must
    # key on the variant: the file-scope `other`'s helper() means foo::helper.
    project = temp_repo / "rs_nested_name_clash"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_nested_name_clash"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 { 2 }\n",
            "src/foo.rs": (
                "pub fn helper() -> u32 { 1 }\n\n"
                "pub fn other() -> u32 {\n"
                "    helper()\n"
                "}\n\n"
                "pub fn outer() -> u32 {\n"
                "    fn other() -> u32 {\n"
                "        use crate::alpha::helper;\n"
                "        helper()\n"
                "    }\n"
                "    other()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_nested_name_clash.src"
    assert (f"{base}.foo.other", f"{base}.foo.helper") in calls, calls
    assert (f"{base}.foo.other", f"{base}.alpha.helper") not in calls, calls
    # The nested other (line 8 of foo.rs) keeps its shadowing use.
    assert (f"{base}.foo.other@8", f"{base}.alpha.helper") in calls, calls


def test_impl_nested_in_method_body_use_keys_under_its_own_impl(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An impl block inside a METHOD body registers its methods at
    # module.S1.me1 (class qns collapse every outer non-mod scope), so the
    # use storage key must not climb past the enclosing method and collect
    # the outer impl target into module.S0.S1.me1, where nothing reads it.
    project = temp_repo / "rs_impl_in_method"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_impl_in_method"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod alpha;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 { 2 }\n",
            "src/foo.rs": (
                "pub fn helper() -> u32 { 1 }\n\n"
                "pub struct S0;\n\n"
                "impl S0 {\n"
                "    pub fn me0(&self) -> u32 {\n"
                "        pub struct S1;\n"
                "        impl S1 {\n"
                "            pub fn me1(&self) -> u32 {\n"
                "                use crate::alpha::helper;\n"
                "                helper()\n"
                "            }\n"
                "        }\n"
                "        S1.me1()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_impl_in_method.src"
    assert (f"{base}.foo.S1.me1", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo.S1.me1", f"{base}.foo.helper") not in calls, calls


def test_fn_sharing_inline_mod_name_keeps_module_scope_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `mod run` and `fn run` live in DIFFERENT Rust namespaces but share one
    # cgr qn string: the inline mod's import map must never answer for the
    # same-named function's own bare calls.
    project = temp_repo / "rs_mod_fn_clash"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_mod_fn_clash"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod alpha;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/foo.rs": (
                "pub fn helper() -> u32 {\n"
                "    1\n"
                "}\n\n"
                "pub mod run {\n"
                "    use crate::alpha::helper;\n\n"
                "    pub fn go() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n\n"
                "pub fn run() -> u32 {\n"
                "    helper()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_mod_fn_clash.src"
    assert (f"{base}.foo.run", f"{base}.foo.helper") in calls, calls
    assert (f"{base}.foo.run", f"{base}.alpha.helper") not in calls, calls
    assert (f"{base}.foo.run.go", f"{base}.alpha.helper") in calls, calls


def test_fn_sharing_submodule_name_keeps_its_body_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `pub mod run;` pulls in src/foo/run.rs whose module qn equals the
    # sibling `fn run`'s qn. Parsing run.rs resets its module import map;
    # the function's body use must survive in its own store, and neither
    # may read the other's imports.
    project = temp_repo / "rs_submod_fn_clash"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_submod_fn_clash"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub mod run;\n\n"
                "pub fn helper() -> u32 {\n"
                "    1\n"
                "}\n\n"
                "pub fn run() -> u32 {\n"
                "    use crate::alpha::helper;\n"
                "    helper()\n"
                "}\n"
            ),
            "src/foo/run.rs": (
                "use crate::beta::helper;\n\npub fn go() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_submod_fn_clash.src"
    assert (f"{base}.foo.run", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo.run", f"{base}.beta.helper") not in calls, calls
    assert (f"{base}.foo.run.go", f"{base}.beta.helper") in calls, calls


def test_reparse_of_declaring_file_keeps_submodule_import_map(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A watch-mode re-parse of foo.rs drops the scope keys foo.rs minted.
    # The function-scope key `foo.run` must not be tracked as an
    # import_mapping key, or the cleanup wipes src/foo/run.rs's whole
    # module import map until run.rs itself is re-parsed.
    project = temp_repo / "rs_reparse_clash"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_reparse_clash"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub mod run;\n\n"
                "pub fn run() -> u32 {\n"
                "    use crate::alpha::helper;\n"
                "    helper()\n"
                "}\n"
            ),
            "src/foo/run.rs": (
                "use crate::beta::helper;\n\npub fn go() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    processor = updater.factory.import_processor
    base = "rs_reparse_clash.src"
    module_map = processor.import_mapping.get(f"{base}.foo.run")
    assert module_map == {"helper": f"{base}.beta.helper"}, module_map
    # Simulate the start of a re-parse of foo.rs alone.
    processor._parse_rust_imports({}, f"{base}.foo")
    module_map = processor.import_mapping.get(f"{base}.foo.run")
    assert module_map == {"helper": f"{base}.beta.helper"}, module_map


def test_same_line_struct_does_not_steal_fn_body_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `fn thing` and `struct thing {}` on ONE line: the struct collides on
    # the natural qn and registers as thing@2, but the function kept the
    # natural qn, so its body use must stay on the natural key.
    project = temp_repo / "rs_struct_line_clash"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_struct_line_clash"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/foo.rs": (
                "pub fn helper() -> u32 { 1 }\n"
                "pub fn thing() -> u32 { use crate::alpha::helper; helper() } "
                "pub struct thing {}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_struct_line_clash.src"
    assert (f"{base}.foo.thing", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo.thing", f"{base}.foo.helper") not in calls, calls


def test_two_same_line_impls_keep_first_methods_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Two one-line impls of the same type on ONE line: the FIRST method
    # holds the natural qn S.me, the second becomes S.me@5. The first's
    # body use must resolve by the method's own span, not by guessing
    # from the line number (which matches the second's variant too).
    project = temp_repo / "rs_same_line_impls"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_same_line_impls"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/foo.rs": (
                "pub fn helper() -> u32 { 1 }\n"
                "pub struct S;\n"
                "pub trait A { fn me(&self) -> u32; }\n"
                "pub trait B { fn me(&self) -> u32; }\n"
                "impl A for S { fn me(&self) -> u32 { use crate::alpha::helper; "
                "helper() } } impl B for S { fn me(&self) -> u32 { helper() } }\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_same_line_impls.src"
    assert (f"{base}.foo.S.me", f"{base}.alpha.helper") in calls, calls


def test_unextractable_impl_target_use_does_not_bind_free_fn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `impl A for &S` has no extractable target and its methods are never
    # registered: the method-body use has no caller to serve and must be
    # dropped, not keyed at module.m where a real free fn lives.
    project = temp_repo / "rs_ref_impl_target"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_ref_impl_target"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/foo.rs": (
                "pub fn helper() -> u32 { 1 }\n"
                "pub fn m() -> u32 { helper() }\n"
                "pub struct S;\n"
                "pub trait A { fn m(&self) -> u32; }\n"
                "impl A for &S {\n"
                "    fn m(&self) -> u32 { use crate::alpha::helper; helper() }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ref_impl_target.src"
    assert (f"{base}.foo.m", f"{base}.foo.helper") in calls, calls
    assert (f"{base}.foo.m", f"{base}.alpha.helper") not in calls, calls


def test_assoc_const_block_use_does_not_pollute_file_map(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A use inside an associated-const initializer block scopes to that
    # block alone. No qn scope corresponds to the block, so the mapping is
    # dropped; it must not land in the FILE map, where it would overwrite
    # the file's real import and rebind every bare call, nor lose the
    # file's IMPORTS edges.
    project = temp_repo / "rs_const_block_use"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_const_block_use"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "use crate::beta::helper;\n\n"
                "pub struct S;\n\n"
                "impl S {\n"
                "    pub const C: u32 = { use crate::alpha::helper; helper() };\n"
                "}\n\n"
                "pub fn other() -> u32 {\n"
                "    helper()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_const_block_use.src"
    assert (f"{base}.foo.other", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.foo.other", f"{base}.alpha.helper") not in calls, calls
    assert (f"{base}.foo", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo", f"{base}.beta.helper") not in calls, calls
    imports = _pairs(mock_ingestor, RelationshipType.IMPORTS.value)
    assert (f"{base}.foo", f"{base}.alpha") in imports, imports
    assert (f"{base}.foo", f"{base}.beta") in imports, imports


def test_method_local_mod_does_not_corrupt_same_named_file_mod(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A `mod inner` declared inside an impl method registers its items at
    # module.S.inner.* (the fqn walk keeps the impl segment and skips the
    # function), so its use must key at module.S.inner, not at the
    # file-level `mod inner`'s key, whose real import map it would replace.
    project = temp_repo / "rs_method_local_mod"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_method_local_mod"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub struct S;\n\n"
                "pub mod inner {\n"
                "    use crate::beta::helper;\n\n"
                "    pub fn g2() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n\n"
                "impl S {\n"
                "    pub fn m(&self) -> u32 {\n"
                "        mod inner {\n"
                "            use crate::alpha::helper;\n\n"
                "            pub fn g() -> u32 {\n"
                "                helper()\n"
                "            }\n"
                "        }\n"
                "        inner::g()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_method_local_mod.src"
    assert (f"{base}.foo.inner.g2", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.foo.inner.g2", f"{base}.alpha.helper") not in calls, calls
    assert (f"{base}.foo.S.inner.g", f"{base}.alpha.helper") in calls, calls


def test_method_local_mod_use_reaches_its_own_functions(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The no-competition variant: with no file-level `mod inner`, the
    # method-local mod's use must still bind its own function's call
    # instead of falling through to the file's same-named helper.
    project = temp_repo / "rs_method_local_only"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_method_local_only"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/foo.rs": (
                "pub fn helper() -> u32 {\n"
                "    1\n"
                "}\n\n"
                "pub struct S;\n\n"
                "impl S {\n"
                "    pub fn m(&self) -> u32 {\n"
                "        mod inner {\n"
                "            use crate::alpha::helper;\n\n"
                "            pub fn g() -> u32 {\n"
                "                helper()\n"
                "            }\n"
                "        }\n"
                "        inner::g()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_method_local_only.src"
    assert (f"{base}.foo.S.inner.g", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo.S.inner.g", f"{base}.foo.helper") not in calls, calls


def test_two_impls_method_local_mod_inner_keep_separate_uses(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Two impls of the same type each declare a method-local `mod inner`;
    # both share the effective scope qn foo.S.inner, but their block-local
    # uses import different helpers. Each inner's function must bind through
    # its OWN use, not a merged foo.S.inner import map (#1017 shape 3).
    project = temp_repo / "rs_two_impls_inner"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_two_impls_inner"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub struct S;\n\n"
                "impl S {\n"
                "    pub fn m(&self) -> u32 {\n"
                "        mod inner {\n"
                "            use crate::alpha::helper;\n\n"
                "            pub fn gm() -> u32 {\n"
                "                helper()\n"
                "            }\n"
                "        }\n"
                "        inner::gm()\n"
                "    }\n"
                "}\n\n"
                "impl S {\n"
                "    pub fn n(&self) -> u32 {\n"
                "        mod inner {\n"
                "            use crate::beta::helper;\n\n"
                "            pub fn gn() -> u32 {\n"
                "                helper()\n"
                "            }\n"
                "        }\n"
                "        inner::gn()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_two_impls_inner.src"
    assert (f"{base}.foo.S.inner.gm", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo.S.inner.gn", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.foo.S.inner.gm", f"{base}.beta.helper") not in calls, calls
    assert (f"{base}.foo.S.inner.gn", f"{base}.alpha.helper") not in calls, calls


def test_const_block_mod_function_keeps_first_claimed_span(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A fn inside a mod inside an associated-const initializer is claimed
    # FIRST by the generic function pass (foo.S.inner.g); the impl-method
    # pass also reaches it and must not overwrite that span record with
    # its own Method claim (first claim wins), or the caller reads the
    # file map instead of its mod's own use.
    project = temp_repo / "rs_const_block_mod"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_const_block_mod"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "use crate::beta::helper;\n\n"
                "pub struct S;\n\n"
                "impl S {\n"
                "    pub const C: u32 = {\n"
                "        mod inner {\n"
                "            use crate::alpha::helper;\n\n"
                "            pub const fn g() -> u32 {\n"
                "                helper()\n"
                "            }\n"
                "        }\n"
                "        inner::g()\n"
                "    };\n"
                "}\n\n"
                "pub const fn f() -> u32 {\n"
                "    helper()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_const_block_mod.src"
    assert (f"{base}.foo.S.inner.g", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo.S.g", f"{base}.beta.helper") not in calls, calls


def test_fn_local_mod_sharing_submodule_name_does_not_wipe_its_map(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `mod run { ... }` inside a free fn legally coexists with `pub mod
    # run;` in the same file (at file level the pair would be E0428), and
    # its mods-only key IS src/foo/run.rs's module qn. Storing there would
    # merge two distinct modules' imports, and tracking it for cleanup
    # lets a watch re-parse of foo.rs wipe run.rs's whole import map.
    project = temp_repo / "rs_fn_local_submod"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_fn_local_submod"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub mod run;\n\n"
                "pub fn f() -> u32 {\n"
                "    mod run {\n"
                "        use crate::alpha::helper;\n\n"
                "        pub fn go2() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "    run::go2()\n"
                "}\n"
            ),
            "src/foo/run.rs": (
                "use crate::beta::helper;\n\npub fn go() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_fn_local_submod.src"
    assert (f"{base}.foo.run.go", f"{base}.beta.helper") in calls, calls

    processor = updater.factory.import_processor
    tracked = processor._rust_inline_scope_keys.get(f"{base}.foo", set())
    assert f"{base}.foo.run" not in tracked, tracked
    # Simulate the start of a re-parse of foo.rs alone: run.rs's module
    # import map must survive.
    processor._parse_rust_imports({}, f"{base}.foo")
    module_map = processor.import_mapping.get(f"{base}.foo.run")
    assert module_map == {"helper": f"{base}.beta.helper"}, module_map


def test_trait_const_block_mod_function_keeps_first_claimed_span(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The trait flavour of the impl-path first-claim rule: a fn inside a
    # mod inside a trait const default is claimed first by the function
    # pass (foo.T.inner.g); the trait's class pass must not overwrite the
    # span record with its Method twin. The conftest graph audit is
    # bypassed here: this fixture trips the PRE-EXISTING inline-mod
    # Module-node inconsistency (issue #1018), identical on main and
    # unrelated to the span claim under test.
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    project = temp_repo / "rs_trait_const_mod"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_trait_const_mod"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "use crate::beta::helper;\n\n"
                "pub trait T {\n"
                "    const C: u32 = {\n"
                "        mod inner {\n"
                "            use crate::alpha::helper;\n\n"
                "            pub const fn g() -> u32 {\n"
                "                helper()\n"
                "            }\n"
                "        }\n"
                "        inner::g()\n"
                "    };\n"
                "}\n\n"
                "pub const fn f() -> u32 {\n"
                "    helper()\n"
                "}\n"
            ),
        },
    )
    parsers, queries = load_parsers()
    if "rust" not in parsers:
        import pytest

        pytest.skip("rust parser not available")
    GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
    ).run()

    calls = _calls(mock_ingestor)
    base = "rs_trait_const_mod.src"
    assert (f"{base}.foo.T.inner.g", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo.T.g", f"{base}.beta.helper") not in calls, calls


def test_nested_fn_local_mod_collision_does_not_wipe_submodule_map(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The round-twelve collision one level down: the bodyless `mod run;`
    # lives inside `pub mod outer`, and the fn-local `mod run` inside
    # outer's function collides with src/foo/outer/run.rs's module qn.
    # The scan must anchor at the module scope above the function, not at
    # the file's top level.
    project = temp_repo / "rs_nested_submod_clash"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_nested_submod_clash"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub mod outer {\n"
                "    pub mod run;\n\n"
                "    pub fn f() -> u32 {\n"
                "        mod run {\n"
                "            use crate::alpha::helper;\n\n"
                "            pub fn go2() -> u32 {\n"
                "                helper()\n"
                "            }\n"
                "        }\n"
                "        run::go2()\n"
                "    }\n"
                "}\n"
            ),
            "src/foo/outer/run.rs": (
                "use crate::beta::helper;\n\npub fn go() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    processor = updater.factory.import_processor
    base = "rs_nested_submod_clash.src"
    tracked = processor._rust_inline_scope_keys.get(f"{base}.foo", set())
    assert f"{base}.foo.outer.run" not in tracked, tracked
    processor._parse_rust_imports({}, f"{base}.foo")
    module_map = processor.import_mapping.get(f"{base}.foo.outer.run")
    assert module_map == {"helper": f"{base}.beta.helper"}, module_map


def test_cfg_gated_inline_mod_beside_bodyless_decl_keeps_its_map(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `#[cfg(feature)] pub mod run;` beside `#[cfg(not(feature))] pub mod
    # run { ... }` compiles (the E0428 argument only holds without cfg):
    # the collision drop must be restricted to FUNCTION-local mods, so a
    # legitimate file-level inline mod keeps its import map. The gated
    # file itself is absent here, as in a checkout built without the
    # feature.
    project = temp_repo / "rs_cfg_dup_mod"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_cfg_dup_mod"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                '#[cfg(feature = "ext")]\n'
                "pub mod run;\n\n"
                '#[cfg(not(feature = "ext"))]\n'
                "pub mod run {\n"
                "    use crate::alpha::helper;\n\n"
                "    pub fn go2() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_cfg_dup_mod.src"
    assert (f"{base}.foo.run.go2", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo.run.go2", f"{base}.beta.helper") not in calls, calls
    # The edge must come from the STORED inline-mod map, not from a lucky
    # simple-name fallback.
    module_map = updater.factory.import_processor.import_mapping.get(f"{base}.foo.run")
    assert module_map == {"helper": f"{base}.alpha.helper"}, module_map


def _assert_submodule_map_survives(
    updater: object, file_qn: str, submodule_qn: str, expected: dict[str, str]
) -> None:
    processor = updater.factory.import_processor  # type: ignore[attr-defined]
    tracked = processor._rust_inline_scope_keys.get(file_qn, set())
    assert submodule_qn not in tracked, tracked
    processor._parse_rust_imports({}, file_qn)
    module_map = processor.import_mapping.get(submodule_qn)
    assert module_map == expected, module_map


def test_const_block_local_mod_beside_submodule_does_not_wipe_map(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A mod inside a file-level const initializer BLOCK crosses no
    # function, yet it collides with `pub mod run;` exactly like the
    # fn-local shape: the drop must fire on the bodyless twin plus an
    # existing file, not on a function crossing.
    project = temp_repo / "rs_const_block_submod"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_const_block_submod"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub mod run;\n\n"
                "pub const X: u32 = {\n"
                "    mod run {\n"
                "        use crate::alpha::helper;\n\n"
                "        pub const fn go2() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "    run::go2()\n"
                "};\n"
            ),
            "src/foo/run.rs": (
                "use crate::beta::helper;\n\npub fn go() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_const_block_submod.src"
    _assert_submodule_map_survives(
        updater,
        f"{base}.foo",
        f"{base}.foo.run",
        {"helper": f"{base}.beta.helper"},
    )


def test_two_level_fn_local_mod_collision_does_not_wipe_map(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The collision sits at the CHAIN HEAD: the fn-local `mod run` forges
    # src/foo/run.rs's namespace, so `mod sub` inside it keys at
    # foo.run.sub, colliding with run.rs's own inline `mod sub`. Each mod
    # in the chain must be checked against its own enclosing module
    # scope's bodyless declarations.
    project = temp_repo / "rs_two_level_clash"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_two_level_clash"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub mod run;\n\n"
                "pub fn f() -> u32 {\n"
                "    mod run {\n"
                "        pub fn g() -> u32 {\n"
                "            mod sub {\n"
                "                use crate::alpha::helper;\n\n"
                "                pub fn h() -> u32 {\n"
                "                    helper()\n"
                "                }\n"
                "            }\n"
                "            sub::h()\n"
                "        }\n"
                "    }\n"
                "    run::g()\n"
                "}\n"
            ),
            "src/foo/run.rs": (
                "pub mod sub {\n"
                "    use crate::beta::helper;\n\n"
                "    pub fn hh() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_two_level_clash.src"
    _assert_submodule_map_survives(
        updater,
        f"{base}.foo",
        f"{base}.foo.run.sub",
        {"helper": f"{base}.beta.helper"},
    )


def test_cfg_gated_inline_mod_with_file_present_does_not_wipe_map(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The cfg twin with the gated file PRESENT (a cargo checkout ships
    # every file; only compilation is gated): the inline mod's key IS
    # run.rs's module qn, so its mapping must drop rather than track a
    # foreign module qn for cleanup.
    project = temp_repo / "rs_cfg_dup_mod_file"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_cfg_dup_mod_file"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                '#[cfg(feature = "ext")]\n'
                "pub mod run;\n\n"
                '#[cfg(not(feature = "ext"))]\n'
                "pub mod run {\n"
                "    use crate::alpha::helper;\n\n"
                "    pub fn go2() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n"
            ),
            "src/foo/run.rs": (
                "use crate::beta::helper;\n\npub fn go() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_cfg_dup_mod_file.src"
    _assert_submodule_map_survives(
        updater,
        f"{base}.foo",
        f"{base}.foo.run",
        {"helper": f"{base}.beta.helper"},
    )


def test_entry_file_inline_mod_does_not_collide_with_sibling_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # In src/main.rs an inline `mod run` keys at src.main.run while the
    # declared sibling src/run.rs keys at src.run: DIFFERENT qns, no
    # collision. The twin probe answers a cgr-qn question, not rustc's
    # beside-the-entry module rule, so the inline map must be kept.
    project = temp_repo / "rs_entry_constblock"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_entry_constblock"\nversion = "0.1.0"\n'
            ),
            "src/main.rs": (
                "mod alpha;\n"
                "mod beta;\n"
                "mod run;\n\n"
                "pub const X: u32 = {\n"
                "    mod run {\n"
                "        use crate::beta::helper;\n\n"
                "        pub const fn go2() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "    run::go2()\n"
                "};\n\n"
                "fn main() {\n"
                '    println!("{}", X);\n'
                "}\n"
            ),
            "src/alpha.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/run.rs": (
                "use crate::alpha::helper;\n\npub fn go() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_entry_constblock.src"
    assert (f"{base}.main.run.go2", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.main.run.go2", f"{base}.alpha.helper") not in calls, calls
    module_map = updater.factory.import_processor.import_mapping.get(f"{base}.main.run")
    assert module_map == {"helper": f"{base}.beta.helper"}, module_map


def test_incidental_main_module_file_collision_still_drops(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # src/app/main.rs is the MODULE app::main (declared by src/app.rs),
    # not a crate entry: its submodules live in src/app/main/, so the
    # fn-local `mod sub` DOES collide with src/app/main/sub.rs and must
    # drop, exactly like any non-entry-named file.
    project = temp_repo / "rs_incidental_main"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_incidental_main"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod app;\npub mod beta;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/app.rs": "pub mod main;\n",
            "src/app/main.rs": (
                "pub mod sub;\n\n"
                "pub fn f() -> u32 {\n"
                "    mod sub {\n"
                "        use crate::alpha::helper;\n\n"
                "        pub fn go2() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "    sub::go2()\n"
                "}\n"
            ),
            "src/app/main/sub.rs": (
                "use crate::beta::helper;\n\npub fn go() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_incidental_main.src"
    _assert_submodule_map_survives(
        updater,
        f"{base}.app.main",
        f"{base}.app.main.sub",
        {"helper": f"{base}.beta.helper"},
    )


def test_bare_directory_is_not_a_module_so_inline_mod_keeps_map(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `#[path = "main/sub/unix.rs"] mod sub;` leaves src/main/sub/ a
    # directory with NO mod.rs: under the qn scheme it owns no module qn,
    # so the fn-local `mod sub` collides with nothing and must keep its
    # map (rustc: f() returns beta's value).
    project = temp_repo / "rs_pathattr_dir"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_pathattr_dir"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "mod alpha;\n"
                "mod beta;\n"
                '#[path = "main/sub/unix.rs"]\n'
                "mod sub;\n\n"
                "fn f() -> u32 {\n"
                "    mod sub {\n"
                "        use crate::beta::helper;\n\n"
                "        pub fn go2() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "    sub::go2()\n"
                "}\n\n"
                "fn main() {\n"
                '    println!("{} {}", f(), sub::plat());\n'
                "}\n"
            ),
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/main/sub/unix.rs": "pub fn plat() -> u32 {\n    7\n}\n",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_pathattr_dir.src"
    assert (f"{base}.main.sub.go2", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.main.sub.go2", f"{base}.alpha.helper") not in calls, calls
    module_map = updater.factory.import_processor.import_mapping.get(f"{base}.main.sub")
    assert module_map == {"helper": f"{base}.beta.helper"}, module_map


def test_directory_with_mod_rs_still_collides_and_drops(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The other arm: src/main/sub/mod.rs DOES own the module qn
    # src.main.sub (mod.rs collapses onto its directory), so the fn-local
    # `mod sub` collides and its mapping must drop, keeping mod.rs's own
    # map intact across a re-parse of main.rs.
    project = temp_repo / "rs_modrs_dir_clash"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_modrs_dir_clash"\nversion = "0.1.0"\n'
            ),
            "src/main.rs": (
                "mod alpha;\n"
                "mod beta;\n"
                '#[path = "main/sub/mod.rs"]\n'
                "mod sub;\n\n"
                "fn f() -> u32 {\n"
                "    mod sub {\n"
                "        use crate::alpha::helper;\n\n"
                "        pub fn go2() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "    sub::go2()\n"
                "}\n\n"
                "fn main() {\n"
                '    println!("{}", f());\n'
                "}\n"
            ),
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/main/sub/mod.rs": (
                "use crate::beta::helper;\n\npub fn go() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_modrs_dir_clash.src"
    _assert_submodule_map_survives(
        updater,
        f"{base}.main",
        f"{base}.main.sub",
        {"helper": f"{base}.beta.helper"},
    )


def test_inline_mod_does_not_hijack_same_named_python_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The module-qn scheme is language-agnostic: src/foo/aaa.py owns
    # foo.aaa, the SAME qn as the Rust inline `mod aaa` in foo/mod.rs. No
    # bodyless declaration exists, so file existence alone must trigger
    # the drop, or the Rust map overwrites the Python module's and a
    # Python function's call to its own def rebinds into Rust.
    project = temp_repo / "rs_py_polyglot"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_py_polyglot"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod foo;\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo/mod.rs": (
                "pub fn f() -> u32 {\n"
                "    mod aaa {\n"
                "        use crate::beta::helper;\n\n"
                "        pub fn go2() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "    aaa::go2()\n"
                "}\n"
            ),
            "src/foo/aaa.py": (
                "def helper():\n    return 11\n\n\ndef caller():\n    return helper()\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_py_polyglot.src"
    assert (f"{base}.foo.aaa.caller", f"{base}.foo.aaa.helper") in calls, calls
    assert (f"{base}.foo.aaa.caller", f"{base}.beta.helper") not in calls, calls


def test_bare_directory_with_python_package_still_drops(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # __init__.py collapses onto its directory exactly like mod.rs, so a
    # bare-of-mod-rs directory holding a Python package DOES own the
    # colliding qn: the inline mod's key must drop, or a re-parse of the
    # Rust file wipes the Python package's import map.
    project = temp_repo / "rs_py_pkg_dir"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_py_pkg_dir"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod foo;\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                '#[path = "foo/sub/unix.rs"]\n'
                "pub mod sub;\n\n"
                "pub fn f() -> u32 {\n"
                "    mod sub {\n"
                "        use crate::beta::helper;\n\n"
                "        pub fn go2() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "    sub::go2()\n"
                "}\n"
            ),
            "src/foo/sub/unix.rs": "pub fn plat() -> u32 {\n    7\n}\n",
            "src/foo/sub/__init__.py": (
                "from .other import helper\n\n\ndef caller():\n    return helper()\n"
            ),
            "src/foo/sub/other.py": "def helper():\n    return 11\n",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_py_pkg_dir.src"
    _assert_submodule_map_survives(
        updater,
        f"{base}.foo",
        f"{base}.foo.sub",
        {"helper": f"{base}.foo.sub.other.helper"},
    )


def test_inline_mod_beside_unindexed_directory_keeps_map(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # src/foo/env/mod.rs sits in an IGNORED directory (env is in the
    # walker's ignore patterns), so no module qn is ever registered for
    # it: ownership is the INDEXER's answer, not the filesystem's, and
    # the inline `mod env` must keep its map.
    project = temp_repo / "rs_ignored_dir"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_ignored_dir"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "use crate::alpha::helper;\n\n"
                "pub mod env {\n"
                "    use crate::beta::helper;\n\n"
                "    pub fn go2() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n\n"
                "pub fn f() -> u32 {\n"
                "    helper()\n"
                "}\n"
            ),
            "src/foo/env/mod.rs": "pub fn plat() -> u32 {\n    7\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_ignored_dir.src"
    assert (f"{base}.foo.env.go2", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.foo.env.go2", f"{base}.alpha.helper") not in calls, calls


def test_type_named_file_does_not_swallow_method_local_mod(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # src/foo/Widget.rs owns foo.Widget, but the dropped key would be
    # foo.Widget.inner, which nothing owns: a class-segment prefix is not
    # a directory, and the method-local mod must keep its map.
    project = temp_repo / "rs_type_named_file"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_type_named_file"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "use crate::alpha::helper;\n\n"
                "pub mod Widget;\n\n"
                "pub struct Widget;\n\n"
                "impl Widget {\n"
                "    pub fn m(&self) -> u32 {\n"
                "        mod inner {\n"
                "            use crate::beta::helper;\n\n"
                "            pub fn g() -> u32 {\n"
                "                helper()\n"
                "            }\n"
                "        }\n"
                "        inner::g()\n"
                "    }\n"
                "}\n"
            ),
            "src/foo/Widget.rs": "pub fn plat() -> u32 {\n    7\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_type_named_file.src"
    assert (f"{base}.foo.Widget.inner.g", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.foo.Widget.inner.g", f"{base}.alpha.helper") not in calls, calls


def test_unowned_deeper_chain_keeps_map_below_owned_prefix(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # run.rs owns foo.run, but the key is foo.run.inner and run.rs
    # declares no inline `mod inner`: nothing owns the full key, so
    # dropping it trades a hypothetical collision for a guaranteed wrong
    # edge. The full key decides, not each prefix.
    project = temp_repo / "rs_deep_unowned"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_deep_unowned"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "use crate::alpha::helper;\n\n"
                "pub mod run;\n\n"
                "pub fn f() -> u32 {\n"
                "    mod run {\n"
                "        pub mod inner {\n"
                "            use crate::beta::helper;\n\n"
                "            pub fn h() -> u32 {\n"
                "                helper()\n"
                "            }\n"
                "        }\n"
                "    }\n"
                "    run::inner::h()\n"
                "}\n"
            ),
            "src/foo/run.rs": "pub fn go() -> u32 {\n    1\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_deep_unowned.src"
    assert (f"{base}.foo.run.inner.h", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.foo.run.inner.h", f"{base}.alpha.helper") not in calls, calls


def test_inline_mod_trait_impl_binds_to_imported_trait(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The impl-ingestion pass resolves the trait DURING the file's parse;
    # an inline mod's use must be visible to it then, not only at flush.
    project = temp_repo / "rs_inline_trait"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_inline_trait"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod tra;\npub mod trb;\npub mod foo;\n",
            "src/tra.rs": "pub trait Greet {\n    fn hi(&self) -> u32;\n}\n",
            "src/trb.rs": "pub trait Greet {\n    fn hi(&self) -> u32;\n}\n",
            "src/foo.rs": (
                "pub mod inner {\n"
                "    use crate::trb::Greet;\n\n"
                "    pub struct W;\n\n"
                "    impl Greet for W {\n"
                "        fn hi(&self) -> u32 {\n"
                "            1\n"
                "        }\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    impls = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_inline_trait.src"
    assert (f"{base}.foo.inner.W", f"{base}.trb.Greet") in impls, impls
    assert (f"{base}.foo.inner.W", f"{base}.tra.Greet") not in impls, impls


def test_inline_mod_trait_impl_prefers_import_over_outer_same_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # No cross-file name clash needed: the enclosing FILE defines its own
    # `Greet`, and the inline mod's import must still win inside the mod.
    project = temp_repo / "rs_inline_shadow"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_inline_shadow"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod trb;\npub mod foo;\n",
            "src/trb.rs": "pub trait Greet {\n    fn hi(&self) -> u32;\n}\n",
            "src/foo.rs": (
                "pub trait Greet {\n    fn hi(&self) -> u32;\n}\n\n"
                "pub mod inner {\n"
                "    use crate::trb::Greet;\n\n"
                "    pub struct W;\n\n"
                "    impl Greet for W {\n"
                "        fn hi(&self) -> u32 {\n"
                "            1\n"
                "        }\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    impls = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    base = "rs_inline_shadow.src"
    assert (f"{base}.foo.inner.W", f"{base}.trb.Greet") in impls, impls
    assert (f"{base}.foo.inner.W", f"{base}.foo.Greet") not in impls, impls


def test_inline_mod_trait_impl_resolves_aliased_trait(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use ... as G; impl G for W`: only the import map can expand the
    # alias, so an empty map at parse time externalises a phantom `G`.
    project = temp_repo / "rs_inline_alias"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_inline_alias"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod trb;\npub mod foo;\n",
            "src/trb.rs": "pub trait Greet {\n    fn hi(&self) -> u32;\n}\n",
            "src/foo.rs": (
                "pub mod inner {\n"
                "    use crate::trb::Greet as G;\n\n"
                "    pub struct W;\n\n"
                "    impl G for W {\n"
                "        fn hi(&self) -> u32 {\n"
                "            1\n"
                "        }\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    impls = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    base = "rs_inline_alias.src"
    assert (f"{base}.foo.inner.W", f"{base}.trb.Greet") in impls, impls
    assert (f"{base}.foo.inner.W.hi", f"{base}.trb.Greet.hi") in overrides, overrides


def test_inline_mod_impl_on_imported_type_keeps_overrides(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The Self type is imported too, so the impl's child qn never
    # registers and the deferred pass cannot repair a phantom
    # interface_implementers key: the parse-time trait resolution alone
    # decides whether the OVERRIDES edge exists.
    project = temp_repo / "rs_inline_imported_self"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_inline_imported_self"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod trb;\npub mod foo;\n",
            "src/trb.rs": (
                "pub trait Greet {\n    fn hi(&self) -> u32;\n}\n\npub struct W;\n"
            ),
            "src/foo.rs": (
                "pub mod inner {\n"
                "    use crate::trb::{Greet, W};\n\n"
                "    impl Greet for W {\n"
                "        fn hi(&self) -> u32 {\n"
                "            1\n"
                "        }\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    base = "rs_inline_imported_self.src"
    assert (f"{base}.foo.inner.W.hi", f"{base}.trb.Greet.hi") in overrides, overrides


def test_samefile_pure_mod_beats_fn_local_twin(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # One file, two writers of foo.inner: a module-level `mod inner` and a
    # fn-local twin. Only one key exists (issue #1017); the pure module
    # chain must own it regardless of textual order.
    project = temp_repo / "rs_samefile_purity"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_samefile_purity"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod foo;\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/gamma.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub mod inner {\n"
                "    use crate::beta::helper;\n\n"
                "    pub fn g() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n\n"
                "pub fn f() -> u32 {\n"
                "    mod inner {\n"
                "        use crate::gamma::helper;\n\n"
                "        pub fn g() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "    inner::g()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_samefile_purity.src"
    assert (f"{base}.foo.inner.g", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.foo.inner.g", f"{base}.gamma.helper") not in calls, calls


def test_samefile_impure_twin_callers_keep_their_own_import(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The losing side of the purity arbitration: the fn-local twin's own
    # function (deduplicated to g@13) must still resolve through ITS mod's
    # use, not inherit the pure winner's map through the shared key.
    project = temp_repo / "rs_samefile_twin_callers"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_samefile_twin_callers"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod foo;\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/gamma.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub mod inner {\n"
                "    use crate::beta::helper;\n\n"
                "    pub fn g() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n\n"
                "pub fn f() -> u32 {\n"
                "    mod inner {\n"
                "        use crate::gamma::helper;\n\n"
                "        pub fn g() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "    inner::g()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_samefile_twin_callers.src"
    assert (f"{base}.foo.inner.g", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.foo.inner.g@13", f"{base}.gamma.helper") in calls, calls
    assert (f"{base}.foo.inner.g@13", f"{base}.beta.helper") not in calls, calls


def test_watch_reparse_recommits_inline_mod_map(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The realtime watcher re-parses a file through process_file and then
    # recomputes CALLS repo-wide; it never passes through run(), so the
    # end-of-parse retraction must be re-arbitrated on ITS path too or the
    # inline mod's callers inherit the enclosing file's imports.
    from watchdog.events import FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_watch_inline_mod"
    foo_rs = (
        "use crate::alpha::helper;\n\n"
        "pub mod env {\n"
        "    use crate::beta::helper;\n\n"
        "    pub fn go2() -> u32 {\n"
        "        helper()\n"
        "    }\n"
        "}\n\n"
        "pub fn f() -> u32 {\n"
        "    helper()\n"
        "}\n"
    )
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_watch_inline_mod"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": foo_rs,
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(project / "src" / "foo.rs")))

    calls = _calls(mock_ingestor)
    base = "rs_watch_inline_mod.src"
    assert (f"{base}.foo.env.go2", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.foo.env.go2", f"{base}.alpha.helper") not in calls, calls

    # A later event on an UNRELATED file recomputes calls repo-wide; the
    # recommitted inline map must still be standing.
    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(project / "src" / "alpha.rs")))

    calls = _calls(mock_ingestor)
    assert (f"{base}.foo.env.go2", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.foo.env.go2", f"{base}.alpha.helper") not in calls, calls


def test_initializer_block_use_does_not_hijack_mod_functions(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A `use` inside a static initializer block is scoped to that block
    # alone (at mod level it could not even coexist with a same-named
    # item, E0255), so the mod's functions must keep binding their
    # sibling, not the initializer's import.
    project = temp_repo / "rs_init_block_use"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_init_block_use"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod gamma;\npub mod foo;\n",
            "src/gamma.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub mod inner {\n"
                "    pub static K: u32 = {\n"
                "        use crate::gamma::helper;\n"
                "        helper()\n"
                "    };\n\n"
                "    pub fn helper() -> u32 {\n"
                "        1\n"
                "    }\n\n"
                "    pub fn q() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_init_block_use.src"
    assert (f"{base}.foo.inner.q", f"{base}.foo.inner.helper") in calls, calls
    assert (f"{base}.foo.inner.q", f"{base}.gamma.helper") not in calls, calls
    # The block-scoped use must not land in the mod's import map at all:
    # sibling precedence alone masking it is not enough (drop the sibling
    # and the map entry would resurface the phantom binding).
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.foo.inner")
    assert not mapping or "helper" not in mapping, mapping


def test_initializer_block_use_stores_no_mapping_anywhere(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # With no sibling item, a call next to an initializer block's use is
    # E0425 (the use is scoped to the block alone), at mod level and at
    # file level alike: neither scope's import map may hold the entry.
    project = temp_repo / "rs_init_block_scope"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_init_block_scope"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod gamma;\npub mod foo;\n",
            "src/gamma.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub mod inner {\n"
                "    pub static K: u32 = {\n"
                "        use crate::gamma::helper;\n"
                "        helper()\n"
                "    };\n\n"
                "    pub fn q() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n\n"
                "pub static J: u32 = {\n"
                "    use crate::gamma::helper;\n"
                "    helper()\n"
                "};\n\n"
                "pub fn p() -> u32 {\n"
                "    helper()\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    # Edges cannot be asserted: the generic simple-name fallback binds an
    # otherwise-unresolvable bare call to the sole registered `helper`
    # even with no `use` in the project at all (pre-existing machinery
    # that only fires on rustc-invalid input like this fixture). The
    # import maps are this scoping rule's own contract.
    base = "rs_init_block_scope.src"
    import_mapping = updater.factory.import_processor.import_mapping
    inner_map = import_mapping.get(f"{base}.foo.inner")
    assert not inner_map or "helper" not in inner_map, inner_map
    file_map = import_mapping.get(f"{base}.foo")
    assert not file_map or "helper" not in file_map, file_map


def test_nested_fn_shadows_fn_local_mod_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A fn declared inside g's body shadows the enclosing mod's use for
    # g's calls and for its own recursion; the fanned-out mod use must
    # rank below local items, not above them.
    project = temp_repo / "rs_nested_fn_shadow"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_nested_fn_shadow"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod gamma;\npub mod foo;\n",
            "src/gamma.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                "pub fn wrap() {\n"
                "    mod inner {\n"
                "        use crate::gamma::helper;\n\n"
                "        pub fn g() -> u32 {\n"
                "            fn helper() -> u32 {\n"
                "                helper()\n"
                "            }\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_nested_fn_shadow.src"
    assert (f"{base}.foo.inner.g", f"{base}.foo.inner.helper") in calls, calls
    assert (f"{base}.foo.inner.g", f"{base}.gamma.helper") not in calls, calls
    assert (f"{base}.foo.inner.helper", f"{base}.foo.inner.helper") in calls, calls
    assert (f"{base}.foo.inner.helper", f"{base}.gamma.helper") not in calls, calls


def test_watch_touch_cannot_flip_mod_key_arbitration(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two files write one key (issue #1017): a pure inline-mod chain and
    # a fn-local forgery. On the watch path only the touched file's uses
    # are pending, so arbitration must remember EVERY writer or a no-op
    # touch of the impure file hands it the key.
    from watchdog.events import FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_watch_arbitration"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_watch_arbitration"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod a;\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/gamma.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                '#[cfg(feature = "inline")]\n'
                "pub mod b {\n"
                "    pub mod c {\n"
                "        use crate::beta::helper;\n\n"
                "        pub fn go() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "}\n"
                '#[cfg(not(feature = "inline"))]\n'
                "pub mod b;\n"
            ),
            "src/a/b.rs": (
                "pub fn wrap() {\n"
                "    mod c {\n"
                "        use crate::gamma::helper;\n\n"
                "        pub fn gb() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_watch_arbitration.src"
    assert (f"{base}.a.b.c.go", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.a.b.c.gb", f"{base}.gamma.helper") in calls, calls

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(project / "src" / "a" / "b.rs")))

    # The pure writer's own callers cannot be re-asserted through edges
    # here: the watch delete sweeps every registration under the touched
    # file's qn prefix, taking a.rs's inline-mod functions with it (a
    # pre-existing watch defect, issue #1025). The arbitration's own
    # output contract is the committed key, so pin that, plus the touched
    # file's re-registered caller whose edge recomputes cleanly.
    key = f"{base}.a.b.c"
    mapping = updater.factory.import_processor.import_mapping.get(key)
    assert mapping == {"helper": f"{base}.beta.helper"}, mapping
    calls = _calls(mock_ingestor)
    assert (f"{base}.a.b.c.gb", f"{base}.gamma.helper") in calls, calls

    # A later unrelated event re-arbitrates again; the pure writer must
    # still hold the key.
    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(project / "src" / "gamma.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(key)
    assert mapping == {"helper": f"{base}.beta.helper"}, mapping


def test_watch_delete_of_mod_rs_drops_its_import_state(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A mod.rs file's module qn collapses to its directory; the watch
    # delete must drop the writer under THAT qn or the deleted file keeps
    # voting its inline-mod uses into every later arbitration.
    from watchdog.events import FileDeletedEvent

    import realtime_updater

    project = temp_repo / "rs_modrs_delete"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_modrs_delete"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod a;\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/a/mod.rs": (
                "pub mod c {\n"
                "    use crate::beta::helper;\n\n"
                "    pub fn go() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_modrs_delete.src"
    assert (f"{base}.a.c.go", f"{base}.beta.helper") in calls, calls
    key = f"{base}.a.c"
    assert updater.factory.import_processor.import_mapping.get(key), "not committed"

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    mod_rs = project / "src" / "a" / "mod.rs"
    mod_rs.unlink()
    mock_ingestor.reset_mock()
    handler.dispatch(FileDeletedEvent(str(mod_rs)))

    mapping = updater.factory.import_processor.import_mapping.get(key)
    assert mapping is None, mapping


def test_python_sibling_edit_keeps_rust_import_state(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # src/foo/__init__.py collapses to the same qn as src/foo.rs; a watch
    # event on the Python file must not drop the Rust file's mod-scope
    # writer state, or the arbitration loses its only writer and the
    # committed key vanishes.
    from watchdog.events import FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_pysibling"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_pysibling"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod foo;\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/foo.rs": (
                "pub mod inner {\n"
                "    use crate::beta::helper;\n\n"
                "    pub fn g() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n"
            ),
            "src/foo/__init__.py": "X = 1\n",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_pysibling.src"
    key = f"{base}.foo.inner"
    expected = {"helper": f"{base}.beta.helper"}
    assert updater.factory.import_processor.import_mapping.get(key) == expected

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    # Edge-level assertions are unavailable here: the shared-prefix
    # registration sweep (issue #1025) deregisters foo.rs's inline-mod
    # functions on this event, so pin the arbitration's own output.
    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(project / "src" / "foo" / "__init__.py")))

    mapping = updater.factory.import_processor.import_mapping.get(key)
    assert mapping == expected, mapping


def test_watch_create_of_owned_module_keeps_its_own_imports(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A key committed by a previous arbitration can become a REAL file's
    # module qn (a watch CREATE of the cfg twin's file form). From then on
    # the map is that file's parse output: the arbitration's retraction
    # must not pop it, and no inline writer may recommit over it.
    from watchdog.events import FileCreatedEvent, FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_create_owned"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_create_owned"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                '#[cfg(feature = "inline")]\n'
                "pub mod inner {\n"
                "    use crate::beta::helper;\n\n"
                "    pub fn g() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n"
                '#[cfg(not(feature = "inline"))]\n'
                "pub mod inner;\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_create_owned.src"
    key = f"{base}.foo.inner"
    mapping = updater.factory.import_processor.import_mapping.get(key)
    assert mapping == {"helper": f"{base}.beta.helper"}, mapping

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    inner_rs = project / "src" / "foo" / "inner.rs"
    inner_rs.parent.mkdir(parents=True, exist_ok=True)
    inner_rs.write_text(
        "use crate::alpha::helper as ah;\n\npub fn h() -> u32 {\n    ah()\n}\n",
        encoding="utf-8",
    )
    mock_ingestor.reset_mock()
    handler.dispatch(FileCreatedEvent(str(inner_rs)))

    # Created files emit their CALLS edges in the same cycle since issue
    # #1028; the import map remains the contract THIS test pins.
    mapping = updater.factory.import_processor.import_mapping.get(key)
    assert mapping == {"ah": f"{base}.alpha.helper"}, mapping

    # A later unrelated event re-arbitrates; the owned map must survive.
    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(project / "src" / "alpha.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(key)
    assert mapping == {"ah": f"{base}.alpha.helper"}, mapping


def test_watch_touch_of_mod_rs_beside_same_stem_rs_keeps_sibling_state(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # src/a.rs parses first and keeps the bare qn; src/a/mod.rs is
    # disambiguated to ...a.rs (a stale pre-2018-layout leftover on disk).
    # A watch event on the mod.rs must drop state under the qn ITS parse
    # recorded, not a qn recomputed from the path: the path form collapses
    # to the bare qn, which belongs to a.rs, and a.rs is not re-parsed by
    # this event so nothing would recommit its wiped writer.
    from watchdog.events import FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_modrs_sibling"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_modrs_sibling"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod a;\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/gamma.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "use crate::gamma::helper;\n\n"
                "pub mod inner {\n"
                "    use crate::beta::helper;\n\n"
                "    pub fn g() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n\n"
                "pub fn top() -> u32 {\n    helper()\n}\n"
            ),
            "src/a/mod.rs": "pub fn other() -> u32 {\n    5\n}\n",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_modrs_sibling.src"
    assert (f"{base}.a.inner.g", f"{base}.beta.helper") in calls, calls
    key = f"{base}.a.inner"
    expected = {"helper": f"{base}.beta.helper"}
    assert updater.factory.import_processor.import_mapping.get(key) == expected

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(project / "src" / "a" / "mod.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(key)
    assert mapping == expected, mapping
    calls = _calls(mock_ingestor)
    assert (f"{base}.a.inner.g", f"{base}.beta.helper") in calls, calls


def test_watch_delete_of_disambiguated_mod_rs_drops_its_writer(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The converse direction: src/a.py owns the bare qn, so src/a/mod.rs
    # parses under the disambiguated ...a.rs and its inline mod commits
    # ...a.rs.c. The delete must drop the writer under the qn the parse
    # actually used; keyed on the path form it drops nothing and the
    # deleted file keeps voting its uses into every later arbitration.
    from watchdog.events import FileDeletedEvent, FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_poly_delete"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_poly_delete"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod a;\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/a.py": "X = 1\n",
            "src/a/mod.rs": (
                "pub mod c {\n"
                "    use crate::beta::helper;\n\n"
                "    pub fn go() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_poly_delete.src"
    key = f"{base}.a.rs.c"
    expected = {"helper": f"{base}.beta.helper"}
    assert updater.factory.import_processor.import_mapping.get(key) == expected

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    mod_rs = project / "src" / "a" / "mod.rs"
    mod_rs.unlink()
    mock_ingestor.reset_mock()
    handler.dispatch(FileDeletedEvent(str(mod_rs)))

    mapping = updater.factory.import_processor.import_mapping.get(key)
    assert mapping is None, mapping

    # A later unrelated event re-arbitrates; the dead writer must stay dead.
    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(project / "src" / "beta.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(key)
    assert mapping is None, mapping


def test_initializer_block_use_binds_the_blocks_own_call_at_file_level(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A use inside a const/static initializer block is in scope for that
    # block itself: the call written next to it must bind through it even
    # though the use stores no module-level mapping. Two same-named
    # helpers exist so the simple-name fallback cannot mask a dropped
    # import.
    project = temp_repo / "rs_block_call_file"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_block_call_file"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod a;\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/gamma.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub static J: u32 = {\n"
                "    use crate::gamma::helper;\n"
                "    helper()\n"
                "};\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_call_file.src"
    assert (f"{base}.a", f"{base}.gamma.helper") in calls, calls
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.a")
    assert not mapping or "helper" not in mapping, mapping


def test_enum_discriminant_block_use_binds_the_blocks_own_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An enum discriminant is an expression block outside any function or
    # const/static item. A use inside it is scoped to that block alone: the
    # discriminant's own call must bind through it, while a sibling fn keeps
    # the file-level use and the file import map stays unpolluted (#1016).
    project = temp_repo / "rs_enum_disc_block"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_enum_disc_block"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod a;\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/gamma.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "use crate::beta::helper;\n\n"
                "#[repr(u32)]\n"
                "pub enum E {\n"
                "    A = { use crate::gamma::helper; helper() },\n"
                "}\n\n"
                "pub fn f() -> u32 {\n"
                "    helper()\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_enum_disc_block.src"
    assert (f"{base}.a", f"{base}.gamma.helper") in calls, calls
    assert (f"{base}.a.f", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.a.f", f"{base}.gamma.helper") not in calls, calls
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.a")
    assert mapping and mapping.get("helper") == f"{base}.beta.helper", mapping


def test_initializer_block_use_binds_the_blocks_own_call_in_a_fn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The fn-nested form: a const declared in a function body whose
    # initializer block carries its own use. The call inside the block is
    # attributed to the enclosing function but must still resolve through
    # the block's use, not the function's or file's imports.
    project = temp_repo / "rs_block_call_fn"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_block_call_fn"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod a;\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/gamma.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub fn outer() -> u32 {\n"
                "    const INNER: u32 = {\n"
                "        use crate::gamma::helper;\n"
                "        helper()\n"
                "    };\n"
                "    INNER\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_call_fn.src"
    assert (f"{base}.a.outer", f"{base}.gamma.helper") in calls, calls


def test_initializer_block_use_binds_the_blocks_own_call_at_mod_level(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The mod-level form: a static declared inside an inline mod with a
    # block-scoped use. The block's own call must bind through the use
    # while the mod's import map stays free of it (sibling calls in the
    # mod are E0425 and must not inherit the name).
    project = temp_repo / "rs_block_call_mod"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_block_call_mod"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod a;\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/gamma.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub mod inner {\n"
                "    pub static J: u32 = {\n"
                "        use crate::gamma::helper;\n"
                "        helper()\n"
                "    };\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_call_mod.src"
    assert any(dst == f"{base}.gamma.helper" for _src, dst in calls), calls
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.a.inner")
    assert not mapping or "helper" not in mapping, mapping


def test_nested_fn_own_use_beats_enclosing_block_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A fn declared INSIDE an initializer block is an inner scope: its own
    # body use is more local than the block's, so its call binds beta
    # (rustc evaluates J to 5). The block's own call still binds gamma.
    project = temp_repo / "rs_block_nested_fn"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_block_nested_fn"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod a;\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/gamma.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub static J: u32 = {\n"
                "    use crate::gamma::helper;\n"
                "    const fn f() -> u32 {\n"
                "        use crate::beta::helper;\n"
                "        helper()\n"
                "    }\n"
                "    f() + helper()\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_nested_fn.src"
    assert (f"{base}.a.f", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.a", f"{base}.gamma.helper") in calls, calls


def test_nested_fn_without_own_use_inherits_block_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The asymmetry: a fn nested in the block with NO binding of its own
    # DOES inherit the block's use (rustc evaluates J to 6). The nested-fn
    # exclusion must therefore yield to the block, not drop the call.
    project = temp_repo / "rs_block_inherit_fn"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_block_inherit_fn"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod a;\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/gamma.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub static J: u32 = {\n"
                "    use crate::gamma::helper;\n"
                "    const fn f() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "    f() + helper()\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_inherit_fn.src"
    assert (f"{base}.a.f", f"{base}.gamma.helper") in calls, calls


def test_nested_mod_own_use_beats_enclosing_block_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A mod declared inside the block is a hard name-resolution boundary:
    # its members resolve through ITS import map, never the block's use
    # (rustc evaluates K to 5).
    project = temp_repo / "rs_block_nested_mod"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_block_nested_mod"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod a;\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/gamma.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub static K: u32 = {\n"
                "    use crate::gamma::helper;\n"
                "    mod m {\n"
                "        use crate::beta::helper;\n\n"
                "        pub const fn f() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "    m::f() + helper()\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_nested_mod.src"
    assert (f"{base}.a.m.f", f"{base}.beta.helper") in calls, calls


def test_chained_call_in_block_types_receiver_through_block_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A chained call inside the block must type its receiver through the
    # block's use as well: `make().run()` under `use crate::gamma::make`
    # runs G's method (rustc: V is 2), while the file-level use keeps
    # `top`'s identical expression on B's (top() is 3).
    project = temp_repo / "rs_block_chain"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_block_chain"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod a;\n",
            "src/beta.rs": (
                "pub struct B;\n\n"
                "impl B {\n"
                "    pub const fn run(&self) -> u32 {\n"
                "        3\n"
                "    }\n"
                "}\n\n"
                "pub const fn make() -> B {\n"
                "    B\n"
                "}\n"
            ),
            "src/gamma.rs": (
                "pub struct G;\n\n"
                "impl G {\n"
                "    pub const fn run(&self) -> u32 {\n"
                "        2\n"
                "    }\n"
                "}\n\n"
                "pub const fn make() -> G {\n"
                "    G\n"
                "}\n"
            ),
            "src/a.rs": (
                "use crate::beta::make;\n\n"
                "pub static V: u32 = {\n"
                "    use crate::gamma::make;\n"
                "    make().run()\n"
                "};\n\n"
                "pub const fn top() -> u32 {\n"
                "    make().run()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_chain.src"
    assert (f"{base}.a", f"{base}.gamma.G.run") in calls, calls
    assert (f"{base}.a.top", f"{base}.beta.B.run") in calls, calls


def test_local_item_in_nested_fn_beats_block_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A fn declared inside the block that declares the name ITSELF binds
    # its own item, not the block's use (rustc evaluates J to 10). The
    # nested fn registers flat under the module qn, so the edge lands on
    # the module-level qn the registry assigned it.
    project = temp_repo / "rs_block_local_item"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_block_local_item"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod a;\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/gamma.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub static J: u32 = {\n"
                "    use crate::gamma::helper;\n"
                "    const fn f() -> u32 {\n"
                "        const fn helper() -> u32 {\n"
                "            7\n"
                "        }\n"
                "        helper()\n"
                "    }\n"
                "    f() + helper()\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_local_item.src"
    assert (f"{base}.a.f", f"{base}.a.helper") in calls, calls
    assert (f"{base}.a", f"{base}.gamma.helper") in calls, calls


def test_block_use_shadows_the_files_own_item(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The most common real-world shape: the file defines a helper AND the
    # block imports a same-named one. The block's own call binds the use
    # (rustc evaluates J to 3), not the file's item, because a use
    # shadows outer items for the remainder of its block.
    project = temp_repo / "rs_block_vs_file_item"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_block_vs_file_item"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub const fn helper() -> u32 {\n"
                "    5\n"
                "}\n\n"
                "pub static J: u32 = {\n"
                "    use crate::gamma::helper;\n"
                "    helper()\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_vs_file_item.src"
    assert (f"{base}.a", f"{base}.gamma.helper") in calls, calls
    assert (f"{base}.a", f"{base}.a.helper") not in calls, calls


def test_assoc_base_in_block_resolves_through_block_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `T::assoc().brun()` inside the block must bind BOTH halves through
    # the block's `use crate::beta::T`: the assoc callee and the
    # receiver's method (rustc: the block evaluates through beta's T,
    # `outside` through gamma's).
    project = temp_repo / "rs_block_assoc"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_block_assoc"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod gamma;\npub mod a;\n",
            "src/beta.rs": (
                "pub struct T;\n\n"
                "impl T {\n"
                "    pub const fn assoc() -> T {\n"
                "        T\n"
                "    }\n\n"
                "    pub const fn brun(&self) -> u32 {\n"
                "        2\n"
                "    }\n"
                "}\n"
            ),
            "src/gamma.rs": (
                "pub struct T;\n\n"
                "impl T {\n"
                "    pub const fn assoc() -> T {\n"
                "        T\n"
                "    }\n\n"
                "    pub const fn grun(&self) -> u32 {\n"
                "        3\n"
                "    }\n"
                "}\n"
            ),
            "src/a.rs": (
                "use crate::gamma::T;\n\n"
                "pub static J: u32 = {\n"
                "    use crate::beta::T;\n"
                "    T::assoc().brun()\n"
                "};\n\n"
                "pub fn outside() -> u32 {\n"
                "    T::assoc().grun()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_assoc.src"
    assert (f"{base}.a", f"{base}.beta.T.assoc") in calls, calls
    assert (f"{base}.a", f"{base}.beta.T.brun") in calls, calls
    assert (f"{base}.a.outside", f"{base}.gamma.T.assoc") in calls, calls
    assert (f"{base}.a.outside", f"{base}.gamma.T.grun") in calls, calls


def test_impl_in_nested_fn_does_not_block_the_block_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An impl inside the nested fn declares METHODS, not bare-name items:
    # `g()` in the fn still binds the block's use (rustc evaluates J to
    # 3), never the impl's method or the file's own item.
    project = temp_repo / "rs_block_impl_names"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_block_impl_names"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn g() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub const fn g() -> u32 {\n"
                "    5\n"
                "}\n\n"
                "pub static J: u32 = {\n"
                "    use crate::gamma::g;\n"
                "    const fn f() -> u32 {\n"
                "        struct S;\n"
                "        impl S {\n"
                "            fn g(&self) -> u32 {\n"
                "                99\n"
                "            }\n"
                "        }\n"
                "        g()\n"
                "    }\n"
                "    f()\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_impl_names.src"
    assert (f"{base}.a.f", f"{base}.gamma.g") in calls, calls
    assert (f"{base}.a.f", f"{base}.a.g") not in calls, calls


def test_method_names_in_nested_fn_do_not_steal_the_block_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # With no file twin at all, the over-collected method name must not
    # fabricate an edge onto the inherent method: the bare call binds the
    # block's use (rustc evaluates J to 3).
    project = temp_repo / "rs_block_method_names"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_block_method_names"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn helper() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub static J: u32 = {\n"
                "    use crate::gamma::helper;\n"
                "    const fn f() -> u32 {\n"
                "        struct S;\n"
                "        impl S {\n"
                "            fn helper(&self) -> u32 {\n"
                "                99\n"
                "            }\n"
                "        }\n"
                "        helper()\n"
                "    }\n"
                "    f()\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_method_names.src"
    assert (f"{base}.a.f", f"{base}.gamma.helper") in calls, calls
    assert (f"{base}.a.f", f"{base}.a.S.helper") not in calls, calls


def test_inner_block_item_in_nested_fn_does_not_block_the_block_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An item declared in an INNER block of the nested fn is invisible at
    # the fn's own level: the call after the inner block binds the block
    # use (rustc evaluates J to 3), not the file's item.
    project = temp_repo / "rs_block_inner_item"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_block_inner_item"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn g() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub const fn g() -> u32 {\n"
                "    5\n"
                "}\n\n"
                "pub static J: u32 = {\n"
                "    use crate::gamma::g;\n"
                "    const fn f() -> u32 {\n"
                "        let x = {\n"
                "            const fn g() -> u32 {\n"
                "                7\n"
                "            }\n"
                "            g()\n"
                "        };\n"
                "        x + g()\n"
                "    }\n"
                "    f()\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_inner_item.src"
    assert (f"{base}.a.f", f"{base}.gamma.g") in calls, calls
    # The call INSIDE the inner block binds that block's own item (the
    # registry deduplicates it against the file twin to a @line qn).
    assert any(dst.startswith(f"{base}.a.g@") for _src, dst in calls), calls


def test_inner_block_own_item_beats_block_use_at_block_level(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A plain block is an item scope too: an item declared in a `let`
    # block inside the initializer shadows the initializer's use for the
    # call written in that inner block (rustc evaluates J to 7).
    project = temp_repo / "rs_inner_block_item_a"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_inner_block_item_a"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn g() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub const fn g() -> u32 {\n"
                "    5\n"
                "}\n\n"
                "pub static J: u32 = {\n"
                "    use crate::gamma::g;\n"
                "    let x = {\n"
                "        const fn g() -> u32 {\n"
                "            7\n"
                "        }\n"
                "        g()\n"
                "    };\n"
                "    x\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_inner_block_item_a.src"
    assert not any(dst == f"{base}.gamma.g" for _src, dst in calls), calls
    assert any(dst.startswith(f"{base}.a.g@") for _src, dst in calls), calls
    # And ONLY the block's item: the module's own `g` is a different
    # function that happens to share the natural qn (issue #1061).
    assert not any(dst == f"{base}.a.g" for _src, dst in calls), calls


def test_inner_let_block_item_beats_block_use_in_nested_fn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The same rule one level down: the inner `let` block of a fn nested
    # in the initializer declares its own item; the call inside it binds
    # that item (rustc evaluates J to 7), never the initializer's use.
    project = temp_repo / "rs_inner_block_item_b"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_inner_block_item_b"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn g() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub const fn g() -> u32 {\n"
                "    5\n"
                "}\n\n"
                "pub static J: u32 = {\n"
                "    use crate::gamma::g;\n"
                "    const fn f() -> u32 {\n"
                "        let x = {\n"
                "            const fn g() -> u32 {\n"
                "                7\n"
                "            }\n"
                "            g()\n"
                "        };\n"
                "        x\n"
                "    }\n"
                "    f()\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_inner_block_item_b.src"
    assert not any(dst == f"{base}.gamma.g" for _src, dst in calls), calls
    assert any(dst.startswith(f"{base}.a.g@") for _src, dst in calls), calls


def test_inner_const_initializer_item_beats_block_use_in_nested_fn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # And with the inner block being itself a const initializer: the
    # call inside it binds ITS local item (rustc evaluates J to 7).
    project = temp_repo / "rs_inner_block_item_c"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_inner_block_item_c"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod gamma;\npub mod a;\n",
            "src/gamma.rs": "pub const fn g() -> u32 {\n    3\n}\n",
            "src/a.rs": (
                "pub const fn g() -> u32 {\n"
                "    5\n"
                "}\n\n"
                "pub static J: u32 = {\n"
                "    use crate::gamma::g;\n"
                "    const fn f() -> u32 {\n"
                "        const K: u32 = {\n"
                "            const fn g() -> u32 {\n"
                "                7\n"
                "            }\n"
                "            g()\n"
                "        };\n"
                "        K\n"
                "    }\n"
                "    f()\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_inner_block_item_c.src"
    assert not any(dst == f"{base}.gamma.g" for _src, dst in calls), calls
    assert any(dst.startswith(f"{base}.a.g@") for _src, dst in calls), calls


def test_nested_fn_own_type_use_beats_block_use_for_assoc_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A nested fn's own `use crate::delta::T` shadows the block's
    # `use crate::beta::T` for `T::assoc()` written in the fn body, the
    # same precedence bare calls already honour (rustc-verified).
    project = temp_repo / "rs_block_qual_defers"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_block_qual_defers"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod beta;\npub mod delta;\npub mod a;\n",
            "src/beta.rs": (
                "pub struct T;\n\n"
                "impl T {\n"
                "    pub const fn assoc() -> T {\n"
                "        T\n"
                "    }\n"
                "}\n"
            ),
            "src/delta.rs": (
                "pub struct T;\n\n"
                "impl T {\n"
                "    pub const fn assoc() -> T {\n"
                "        T\n"
                "    }\n"
                "}\n"
            ),
            "src/a.rs": (
                "pub static J: u32 = {\n"
                "    use crate::beta::T;\n"
                "    const fn f() -> u32 {\n"
                "        use crate::delta::T;\n"
                "        let _t = T::assoc();\n"
                "        0\n"
                "    }\n"
                "    f()\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_block_qual_defers.src"
    assert (f"{base}.a.f", f"{base}.delta.T.assoc") in calls, calls
    assert (f"{base}.a.f", f"{base}.beta.T.assoc") not in calls, calls


def test_watch_create_refreshes_rust_path_caches(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The watcher re-parses through process_file without passing through
    # _process_files, so the exact-case directory listings and entry-file
    # declaration caches must be reset per event: a sibling re-parsed
    # after a CREATE must classify `crate::gamma2` against the filesystem
    # that now holds gamma2.rs, not the full run's snapshot.
    from watchdog.events import FileCreatedEvent, FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_watch_cachereset"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_watch_cachereset"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod beta;\npub mod a;\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/a.rs": (
                "use crate::beta::helper;\n\npub fn top() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_watch_cachereset.src"
    calls = _calls(mock_ingestor)
    assert (f"{base}.a.top", f"{base}.beta.helper") in calls, calls

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    gamma2 = project / "src" / "gamma2.rs"
    gamma2.write_text("pub const fn helper2() -> u32 {\n    3\n}\n", encoding="utf-8")
    handler.dispatch(FileCreatedEvent(str(gamma2)))

    (project / "src" / "a.rs").write_text(
        "use crate::gamma2::helper2;\n\npub fn top() -> u32 {\n    helper2()\n}\n",
        encoding="utf-8",
    )
    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(project / "src" / "a.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.a")
    assert mapping == {"helper2": f"{base}.gamma2.helper2"}, mapping
    calls = _calls(mock_ingestor)
    assert (f"{base}.a.top", f"{base}.gamma2.helper2") in calls, calls


def test_explicit_cargo_bin_target_is_its_own_crate_root(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Cargo permits `[[bin]] path = "src/cli.rs"`: such an entry roots its
    # own crate even though it sits in no auto-target location, so its
    # `crate::` paths resolve to the entry file's qn, not the phantom
    # project root.
    project = temp_repo / "rs_explicit_bin"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_explicit_bin"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "src/cli.rs": (
                "pub const fn helper() -> u32 {\n"
                "    2\n"
                "}\n\n"
                "use crate::helper as h;\n\n"
                "pub fn top() -> u32 {\n"
                "    h()\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_explicit_bin.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.cli")
    assert mapping == {"h": f"{base}.cli.helper"}, mapping
    calls = _calls(mock_ingestor)
    assert (f"{base}.cli.top", f"{base}.cli.helper") in calls, calls


def test_directory_named_like_explicit_target_stays_in_its_crate(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An explicit `[[bin]] path = "src/cli.rs"` roots ONLY that file:
    # rustc searches a non-standard root's modules in its CONTAINING
    # directory, so src/cli/ is the LIB crate's `cli` module directory
    # (cargo-verified: `mod sub;` in the bin refuses src/cli/sub.rs with
    # E0583). The lib-side impl must keep its trait link and OVERRIDES.
    project = temp_repo / "rs_dual_target"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_dual_target"\nversion = "0.1.0"\n\n'
                '[lib]\npath = "src/lib.rs"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "src/lib.rs": "pub mod cli;\npub mod flags;\n",
            "src/flags.rs": (
                "pub trait Flag {\n    fn name_long(&self) -> &'static str;\n}\n"
            ),
            "src/cli.rs": "pub mod sub;\n\nfn main() {}\n",
            "src/sub.rs": "pub fn unused_bin_side() {}\n",
            "src/cli/sub.rs": (
                "use crate::flags::Flag;\n\n"
                "pub struct A;\n\n"
                "impl Flag for A {\n"
                "    fn name_long(&self) -> &'static str {\n"
                '        "a"\n'
                "    }\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_dual_target.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.cli.sub")
    assert mapping == {"Flag": f"{base}.flags.Flag"}, mapping
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (f"{base}.cli.sub.A", f"{base}.flags.Flag") in implements, implements
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    assert (
        f"{base}.cli.sub.A.name_long",
        f"{base}.flags.Flag.name_long",
    ) in overrides, overrides


def test_explicit_target_path_with_dot_prefix_matches(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Cargo normalises `path = "./src/cli.rs"`; the matcher must too.
    project = temp_repo / "rs_dot_target"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_dot_target"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "cli"\npath = "./src/cli.rs"\n'
            ),
            "src/cli.rs": (
                "pub const fn helper() -> u32 {\n"
                "    2\n"
                "}\n\n"
                "use crate::helper as h;\n\n"
                "pub fn top() -> u32 {\n"
                "    h()\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_dot_target.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.cli")
    assert mapping == {"h": f"{base}.cli.helper"}, mapping


def test_package_build_key_roots_its_script(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `[package] build = "src/gen.rs"` is an explicit target override too:
    # the build script is its own crate, never a module of the lib.
    project = temp_repo / "rs_build_key"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_build_key"\nversion = "0.1.0"\n'
                'build = "src/gen.rs"\n'
            ),
            "src/lib.rs": "pub fn lib_item() {}\n",
            "src/gen.rs": (
                "pub const fn helper() -> u32 {\n"
                "    2\n"
                "}\n\n"
                "use crate::helper as h;\n\n"
                "pub fn top() -> u32 {\n"
                "    h()\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_build_key.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.gen")
    assert mapping == {"h": f"{base}.gen.helper"}, mapping


def test_watch_storm_delete_and_restore_of_entry_keeps_sibling_maps(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An editor's atomic save or a checkout deletes and recreates the
    # entry file within one debounce window. A sibling re-parsed
    # mid-storm must not bake the transient absence into its import map:
    # deletes keep the stale directory view on purpose, and only a
    # CREATE re-observes the file set.
    from watchdog.events import (
        FileCreatedEvent,
        FileDeletedEvent,
        FileModifiedEvent,
    )

    import realtime_updater

    project = temp_repo / "rs_heal"
    lib_content = "pub mod beta;\npub mod a;\n"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_heal"\nversion = "0.1.0"\n',
            "src/lib.rs": lib_content,
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/a.rs": (
                "use crate::beta::helper;\n\npub fn top() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_heal.src"
    expected = {"helper": f"{base}.beta.helper"}
    assert updater.factory.import_processor.import_mapping.get(f"{base}.a") == (
        expected
    )

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    lib_rs = project / "src" / "lib.rs"
    lib_rs.unlink()
    handler.dispatch(FileDeletedEvent(str(lib_rs)))
    handler.dispatch(FileModifiedEvent(str(project / "src" / "a.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.a")
    assert mapping == expected, mapping

    lib_rs.write_text(lib_content, encoding="utf-8")
    handler.dispatch(FileCreatedEvent(str(lib_rs)))
    handler.dispatch(FileModifiedEvent(str(project / "src" / "a.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.a")
    assert mapping == expected, mapping


def test_watch_create_during_entry_absence_keeps_sibling_maps(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A checkout storm interleaves events: while the entry file is
    # transiently absent, an unrelated CREATE must not re-observe the
    # directory and bake the absence into a sibling's crate root. The
    # CREATE applies its own known delta to the cached listing instead.
    from watchdog.events import (
        FileCreatedEvent,
        FileDeletedEvent,
        FileModifiedEvent,
    )

    import realtime_updater

    project = temp_repo / "rs_storm_interleave"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_storm_interleave"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod beta;\npub mod a;\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/a.rs": (
                "use crate::beta::helper;\n\npub fn top() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_storm_interleave.src"
    expected = {"helper": f"{base}.beta.helper"}
    assert updater.factory.import_processor.import_mapping.get(f"{base}.a") == (
        expected
    )

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    lib_rs = project / "src" / "lib.rs"
    lib_rs.unlink()
    handler.dispatch(FileDeletedEvent(str(lib_rs)))

    other = project / "src" / "other.rs"
    other.write_text("pub fn extra() {}\n", encoding="utf-8")
    handler.dispatch(FileCreatedEvent(str(other)))

    handler.dispatch(FileModifiedEvent(str(project / "src" / "a.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.a")
    assert mapping == expected, mapping


def test_explicit_target_submodules_resolve_beside_the_root(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A crate root resolves `mod sub;` in its CONTAINING directory (the
    # tests/common.rs idiom; cargo-verified for an explicit
    # `[[bin]] path = "src/cli.rs"`): crate::sub::f from the root means
    # src/sub.rs, never a same-named file under src/cli/.
    project = temp_repo / "rs_binroot_beside"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_binroot_beside"\nversion = "0.1.0"\n\n'
                '[lib]\npath = "src/lib.rs"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "src/lib.rs": "pub mod flags;\n",
            "src/flags.rs": "pub fn noop() {}\n",
            "src/cli.rs": (
                "mod sub;\n\nuse crate::sub::f;\n\nfn main() {\n    let _ = f();\n}\n"
            ),
            "src/sub.rs": "pub const fn f() -> u32 {\n    7\n}\n",
            "src/cli/sub.rs": "pub const fn f() -> u32 {\n    9\n}\n",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_binroot_beside.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.cli")
    assert mapping == {"f": f"{base}.sub.f"}, mapping
    calls = _calls(mock_ingestor)
    assert (f"{base}.cli.main", f"{base}.sub.f") in calls, calls


def test_inner_block_use_in_a_fn_scopes_to_its_block(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A use in an INNER block of a fn body is in scope for that block
    # alone (rustc: x binds delta, y binds beta, J is 21): the call
    # inside the block binds its use and the call after it binds the
    # initializer block's, for the qualified and bare shapes alike.
    project = temp_repo / "rs_inner_use_scope"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_inner_use_scope"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod beta;\npub mod delta;\npub mod a;\npub mod b;\n",
            "src/beta.rs": (
                "pub struct T;\n\n"
                "impl T {\n"
                "    pub const fn assoc() -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n\n"
                "pub const fn helper() -> u32 {\n"
                "    1\n"
                "}\n"
            ),
            "src/delta.rs": (
                "pub struct T;\n\n"
                "impl T {\n"
                "    pub const fn assoc() -> u32 {\n"
                "        2\n"
                "    }\n"
                "}\n\n"
                "pub const fn helper() -> u32 {\n"
                "    2\n"
                "}\n"
            ),
            "src/a.rs": (
                "pub static J: u32 = {\n"
                "    use crate::beta::T;\n"
                "    const fn f() -> u32 {\n"
                "        let x = {\n"
                "            use crate::delta::T;\n"
                "            T::assoc()\n"
                "        };\n"
                "        let y = T::assoc();\n"
                "        x * 10 + y\n"
                "    }\n"
                "    f()\n"
                "};\n"
            ),
            "src/b.rs": (
                "pub static K: u32 = {\n"
                "    use crate::beta::helper;\n"
                "    const fn g() -> u32 {\n"
                "        let x = {\n"
                "            use crate::delta::helper;\n"
                "            helper()\n"
                "        };\n"
                "        let y = helper();\n"
                "        x * 10 + y\n"
                "    }\n"
                "    g()\n"
                "};\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_inner_use_scope.src"
    assert (f"{base}.a.f", f"{base}.delta.T.assoc") in calls, calls
    assert (f"{base}.a.f", f"{base}.beta.T.assoc") in calls, calls
    assert (f"{base}.b.g", f"{base}.delta.helper") in calls, calls
    assert (f"{base}.b.g", f"{base}.beta.helper") in calls, calls


def test_touching_one_entry_keeps_the_siblings_declarations(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The entry-declaration cache holds BOTH stems per directory; the
    # event is file-scoped, so touching main.rs must not discard lib.rs's
    # declarations. Mid-storm (lib.rs transiently absent) the discarded
    # entry would rebuild EMPTY and flip a definitive crate attribution
    # to the item tie-break's real-but-wrong answer.
    from watchdog.events import FileDeletedEvent, FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_decl_poison"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_decl_poison"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod a;\n\npub struct Config;\n",
            "src/main.rs": "mod b;\n\npub struct Config;\n\nfn main() {}\n",
            "src/b.rs": "pub fn bee() {}\n",
            "src/a.rs": "use crate::Config;\n\npub fn ay(_c: Config) {}\n",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_decl_poison.src"
    expected = {"Config": f"{base}.lib.Config"}
    assert updater.factory.import_processor.import_mapping.get(f"{base}.a") == (
        expected
    )

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    lib_rs = project / "src" / "lib.rs"
    lib_rs.unlink()
    handler.dispatch(FileDeletedEvent(str(lib_rs)))
    handler.dispatch(FileModifiedEvent(str(project / "src" / "main.rs")))
    handler.dispatch(FileModifiedEvent(str(project / "src" / "a.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.a")
    assert mapping == expected, mapping


def test_self_paths_in_explicit_roots_match_crate_paths(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # In a crate root module `self::` IS `crate::`: an explicit target's
    # `self::sub::g` must resolve to the file beside the root exactly as
    # `crate::sub::f` does.
    project = temp_repo / "rs_self_explicit"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_self_explicit"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "src/cli.rs": (
                "mod sub;\n\nuse self::sub::g;\n\nfn main() {\n    let _ = g();\n}\n"
            ),
            "src/sub.rs": "pub const fn g() -> u32 {\n    7\n}\n",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_self_explicit.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.cli")
    assert mapping == {"g": f"{base}.sub.g"}, mapping
    calls = _calls(mock_ingestor)
    assert (f"{base}.cli.main", f"{base}.sub.g") in calls, calls


def test_explicit_root_own_declarations_outrank_sibling_files(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The entry-declaration priority applies to explicit roots too: an
    # inline `mod sys` in the explicit bin owns crate::sys and self::sys
    # even when the LIB crate has a src/sys.rs beside it (cargo-verified:
    # the bin prints its own 42, never the lib file's value), and the
    # root's own item wins over a sibling file of the same name.
    project = temp_repo / "rs_expl_decls"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_expl_decls"\nversion = "0.1.0"\n\n'
                '[lib]\npath = "src/lib.rs"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "src/lib.rs": "pub mod sys;\npub mod helper;\n",
            "src/sys.rs": "pub const fn f() -> u32 {\n    1\n}\n",
            "src/helper.rs": "pub const fn helper() -> u32 {\n    1\n}\n",
            "src/cli.rs": (
                "mod sys {\n"
                "    pub const fn f() -> u32 {\n"
                "        42\n"
                "    }\n"
                "}\n\n"
                "pub const fn helper() -> u32 {\n"
                "    42\n"
                "}\n\n"
                "use self::sys::f as g2;\n"
                "use crate::helper as h;\n\n"
                "pub fn top() -> u32 {\n"
                "    g2() + h()\n"
                "}\n\n"
                "fn main() {}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_expl_decls.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.cli")
    assert mapping == {
        "g2": f"{base}.cli.sys.f",
        "h": f"{base}.cli.helper",
    }, mapping


def test_entry_modify_racing_its_own_deletion_keeps_declarations(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A storm can MODIFY the entry, then delete it, before a sibling
    # re-parses: the refresh replaces declarations only on a successful
    # read, so the entry's stem is never left absent for the item
    # tie-break to flip a definitive crate attribution.
    from watchdog.events import FileDeletedEvent, FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_decl_poison2"
    _write(
        project,
        {
            "Cargo.toml": ('[package]\nname = "rs_decl_poison2"\nversion = "0.1.0"\n'),
            "src/lib.rs": "pub mod a;\n\npub struct Config;\n",
            "src/main.rs": "mod b;\n\npub struct Config;\n\nfn main() {}\n",
            "src/b.rs": "pub fn bee() {}\n",
            "src/a.rs": "use crate::Config;\n\npub fn ay(_c: Config) {}\n",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_decl_poison2.src"
    expected = {"Config": f"{base}.lib.Config"}
    assert updater.factory.import_processor.import_mapping.get(f"{base}.a") == (
        expected
    )

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    lib_rs = project / "src" / "lib.rs"
    handler.dispatch(FileModifiedEvent(str(lib_rs)))
    lib_rs.unlink()
    handler.dispatch(FileDeletedEvent(str(lib_rs)))
    handler.dispatch(FileModifiedEvent(str(project / "src" / "a.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.a")
    assert mapping == expected, mapping


def test_explicit_stem_does_not_hijack_module_dir_attribution(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An explicit target's stem in the declaration map must not satisfy
    # the top-segment shortcut: src/cli/sub.rs is the LIB crate's module
    # (cargo-verified build), so its crate:: paths resolve through lib.rs
    # and the trait link plus OVERRIDES survive.
    project = temp_repo / "rs_stem_hijack"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_stem_hijack"\nversion = "0.1.0"\n\n'
                '[lib]\npath = "src/lib.rs"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "src/lib.rs": (
                "pub mod cli;\n\npub trait Flag {\n    fn n(&self) -> u32;\n}\n"
            ),
            "src/cli.rs": "pub mod sub;\n\nfn main() {}\n",
            "src/sub.rs": "pub fn bin_side() {}\n",
            "src/cli/sub.rs": (
                "use crate::Flag;\n\n"
                "pub struct A;\n\n"
                "impl Flag for A {\n"
                "    fn n(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_stem_hijack.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.cli.sub")
    assert mapping == {"Flag": f"{base}.lib.Flag"}, mapping
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (f"{base}.cli.sub.A", f"{base}.lib.Flag") in implements, implements
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    assert (f"{base}.cli.sub.A.n", f"{base}.lib.Flag.n") in overrides, overrides


def test_super_onto_explicit_root_keeps_the_module_reading(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The explicit-root attachment applies only when the root itself is
    # asking: `super::other::f` from src/cli/sub.rs (a LIB-crate module)
    # lands on the MODULE cli, whose children live in src/cli/
    # (cargo-verified: the in-crate test binds src/cli/other.rs).
    project = temp_repo / "rs_super_module"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_super_module"\nversion = "0.1.0"\n\n'
                '[lib]\npath = "src/lib.rs"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "src/lib.rs": "pub mod cli;\n",
            "src/cli.rs": "pub mod other;\npub mod sub;\n\nfn main() {}\n",
            "src/other.rs": "pub const fn f() -> u32 {\n    1\n}\n",
            "src/sub.rs": "pub fn s() {}\n",
            "src/cli/other.rs": "pub const fn f() -> u32 {\n    42\n}\n",
            "src/cli/sub.rs": (
                "use super::other::f;\n\npub fn call() -> u32 {\n    f()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_super_module.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.cli.sub")
    assert mapping == {"f": f"{base}.cli.other.f"}, mapping
    calls = _calls(mock_ingestor)
    assert (f"{base}.cli.sub.call", f"{base}.cli.other.f") in calls, calls


def test_super_from_inline_mod_of_explicit_root_attaches_beside(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An inline `mod inner` written in the explicit root has no backing
    # file, so `super::` inside it IS the crate root (cargo-verified: it
    # binds src/sub.rs, printing 7, never the decoy src/cli/sub.rs): the
    # beside-attachment applies exactly as for the root's own paths.
    project = temp_repo / "rs_inline_super"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_inline_super"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "src/cli.rs": (
                "mod sub;\n\n"
                "mod inner {\n"
                "    use super::sub::f;\n\n"
                "    pub const fn c() -> u32 {\n"
                "        f()\n"
                "    }\n"
                "}\n\n"
                "fn main() {\n"
                "    let _ = inner::c();\n"
                "}\n"
            ),
            "src/sub.rs": "pub const fn f() -> u32 {\n    7\n}\n",
            "src/cli/sub.rs": "pub const fn f() -> u32 {\n    99\n}\n",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_inline_super.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.cli.inner")
    assert mapping == {"f": f"{base}.sub.f"}, mapping
    calls = _calls(mock_ingestor)
    assert (f"{base}.cli.inner.c", f"{base}.sub.f") in calls, calls


def test_manifest_repoint_evicts_the_dead_explicit_stem(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The entry-declaration map is DERIVED from the manifest's target
    # set: repointing the bin from cli.rs to tool.rs must evict the dead
    # `cli` stem, or it keeps voting in the declaring scan and crate
    # attribution resolves out of the dead root (cargo-verified: the
    # post-swap tree binds tool.rs's Config).
    from watchdog.events import FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_repoint"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_repoint"\nversion = "0.1.0"\n\n'
                '[lib]\npath = "src/lib.rs"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "src/lib.rs": "pub fn lib_item() {}\n",
            "src/cli.rs": "mod q;\n\npub struct Config;\n\nfn main() {}\n",
            "src/tool.rs": "mod q;\n\npub struct Config;\n\nfn main() {}\n",
            "src/q.rs": "use crate::Config;\n\npub fn ay(_c: Config) {}\n",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_repoint.src"
    assert updater.factory.import_processor.import_mapping.get(f"{base}.q") == {
        "Config": f"{base}.cli.Config"
    }

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    (project / "Cargo.toml").write_text(
        '[package]\nname = "rs_repoint"\nversion = "0.1.0"\n\n'
        '[lib]\npath = "src/lib.rs"\n\n'
        '[[bin]]\nname = "cli"\npath = "src/tool.rs"\n',
        encoding="utf-8",
    )
    handler.dispatch(FileModifiedEvent(str(project / "Cargo.toml")))
    handler.dispatch(FileModifiedEvent(str(project / "src" / "q.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.q")
    assert mapping == {"Config": f"{base}.tool.Config"}, mapping


def test_explicit_only_package_roots_its_submodules(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A package whose ONLY entry is an explicit target still has a crate
    # root: the target's declared submodules resolve their crate:: paths
    # through it (cargo-verified), never through the phantom project
    # root.
    project = temp_repo / "rs_only_expl"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_only_expl"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "src/cli.rs": (
                "mod q;\n\npub struct Config;\n\nfn main() {\n    q::ay(Config);\n}\n"
            ),
            "src/q.rs": "use crate::Config;\n\npub fn ay(_c: Config) {}\n",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_only_expl.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.q")
    assert mapping == {"Config": f"{base}.cli.Config"}, mapping


def test_explicit_only_package_keeps_trait_links_off_decoys(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The same gap at graph level: with a repo-root decoy flags.rs, the
    # phantom root sent IMPLEMENTS/OVERRIDES to the decoy; the explicit
    # root's crate anchors them on the real trait.
    project = temp_repo / "rs_decoyroot"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_decoyroot"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "flags.rs": "pub trait Flag {\n    fn n(&self) -> u32;\n}\n",
            "src/cli.rs": "mod flags;\nmod q;\n\nfn main() {}\n",
            "src/flags.rs": "pub trait Flag {\n    fn n(&self) -> u32;\n}\n",
            "src/q.rs": (
                "use crate::flags::Flag;\n\n"
                "pub struct A;\n\n"
                "impl Flag for A {\n"
                "    fn n(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_decoyroot"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (f"{base}.src.q.A", f"{base}.src.flags.Flag") in implements, implements
    overrides = _pairs(mock_ingestor, RelationshipType.OVERRIDES.value)
    assert (
        f"{base}.src.q.A.n",
        f"{base}.src.flags.Flag.n",
    ) in overrides, overrides


def test_watch_reparse_recomputes_edges_through_fresh_resolutions(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The watch pass deletes EVERY CALLS edge and recomputes them, so the
    # simple-resolution cache must reset with them: a re-parsed file whose
    # use moved to a different target must emit the new edge, not the
    # cached one.
    from watchdog.events import FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_fresh_edges"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_fresh_edges"\nversion = "0.1.0"\n',
            "src/lib.rs": ("pub mod q;\npub mod alt;\n\npub fn helper() {}\n"),
            "src/alt.rs": "pub fn helper() {}\n",
            "src/q.rs": ("use crate::helper;\n\npub fn ay() {\n    helper()\n}\n"),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_fresh_edges.src"
    calls = _calls(mock_ingestor)
    assert (f"{base}.q.ay", f"{base}.lib.helper") in calls, calls

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    (project / "src" / "q.rs").write_text(
        "use crate::alt::helper;\n\npub fn ay() {\n    helper()\n}\n",
        encoding="utf-8",
    )
    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(project / "src" / "q.rs")))

    calls = _calls(mock_ingestor)
    assert (f"{base}.q.ay", f"{base}.alt.helper") in calls, calls
    assert (f"{base}.q.ay", f"{base}.lib.helper") not in calls, calls


def test_explicit_only_fallback_prefers_present_stems(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # In an explicit-only package a module reached only through #[path]
    # has no declaring scan hit; the fallback must pick a stem that is
    # ACTUALLY in the declaration map, so cli.rs's inline `mod flags`
    # wins (cargo prints 111), never the out-of-crate decoy src/flags.rs.
    project = temp_repo / "rs_decoy2"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_decoy2"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "src/cli.rs": (
                '#[path = "q.rs"]\n'
                "mod renamed;\n\n"
                "mod flags {\n"
                "    pub trait Flag {\n"
                "        fn n(&self) -> u32 {\n"
                "            111\n"
                "        }\n"
                "    }\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/q.rs": (
                "use crate::flags::Flag;\n\npub struct A;\n\nimpl Flag for A {}\n"
            ),
            "src/flags.rs": (
                "pub trait Flag {\n    fn n(&self) -> u32 {\n        999\n    }\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_decoy2.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.q")
    assert mapping == {"Flag": f"{base}.cli.flags.Flag"}, mapping
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (f"{base}.q.A", f"{base}.cli.flags.Flag") in implements, implements
    assert (f"{base}.q.A", f"{base}.flags.Flag") not in implements, implements


def test_watch_create_of_second_implementer_drops_sole_impl_edge(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sole-implementer companion edge is deliberately absent once a
    # second implementer exists; the interface-implementer memo must
    # reset with the recompute or the watch keeps emitting the edge a
    # fresh full run would not.
    from watchdog.events import FileCreatedEvent

    import realtime_updater

    project = temp_repo / "rs_iface"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_iface"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod a;\npub mod caller;\n\n"
                "pub trait Tr {\n"
                "    fn m(&self) -> u32;\n"
                "}\n"
            ),
            "src/a.rs": (
                "use crate::Tr;\n\n"
                "pub struct A;\n\n"
                "impl Tr for A {\n"
                "    fn m(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n"
            ),
            "src/caller.rs": (
                "use crate::Tr;\n\npub fn go(t: &dyn Tr) -> u32 {\n    t.m()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_iface.src"
    calls = _calls(mock_ingestor)
    assert (f"{base}.caller.go", f"{base}.a.A.m") in calls, calls

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    b_rs = project / "src" / "b.rs"
    b_rs.write_text(
        "use crate::Tr;\n\n"
        "pub struct B;\n\n"
        "impl Tr for B {\n"
        "    fn m(&self) -> u32 {\n"
        "        2\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    mock_ingestor.reset_mock()
    handler.dispatch(FileCreatedEvent(str(b_rs)))

    calls = _calls(mock_ingestor)
    assert (f"{base}.caller.go", f"{base}.a.A.m") not in calls, calls


def test_unreadable_entry_does_not_hand_the_crate_to_explicit_stems(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A listed but unreadable lib.rs leaves its stem unfilled; the
    # present-stem fallback must not mistake that hole for an
    # explicit-only package and definitively hand q.rs to the bin (whose
    # private inline `mod flags` q.rs can never reach). The safe landing
    # is the filesystem probe, which still finds the real src/flags.rs.
    import os

    project = temp_repo / "rs_unread"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_unread"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "cli"\npath = "src/cli.rs"\n'
            ),
            "src/lib.rs": "pub mod flags;\npub mod q;\n",
            "src/flags.rs": (
                "pub trait Flag {\n    fn n(&self) -> u32 {\n        999\n    }\n}\n"
            ),
            "src/q.rs": (
                "use crate::flags::Flag;\n\npub struct A;\n\nimpl Flag for A {}\n"
            ),
            "src/cli.rs": (
                "mod flags {\n"
                "    pub trait Flag {\n"
                "        fn n(&self) -> u32 {\n"
                "            111\n"
                "        }\n"
                "    }\n"
                "}\n\n"
                "fn main() {}\n"
            ),
        },
    )
    lib_rs = project / "src" / "lib.rs"
    os.chmod(lib_rs, 0)
    if os.access(lib_rs, os.R_OK):
        os.chmod(lib_rs, 0o644)
        pytest.skip("cannot make files unreadable in this environment")
    try:
        updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    finally:
        os.chmod(lib_rs, 0o644)

    base = "rs_unread.src"
    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.q")
    assert mapping == {"Flag": f"{base}.flags.Flag"}, mapping
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (f"{base}.q.A", f"{base}.cli.flags.Flag") not in implements, implements


def test_multi_explicit_stems_stay_ambiguous_not_first_alphabetical(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Two explicit bins, a module neither declares: attribution is
    # genuinely ambiguous, and a dangling phantom (no edge, dead code
    # stays dead) beats a confident edge onto whichever bin sorts first.
    project = temp_repo / "rs_twobins"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_twobins"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "aaa"\npath = "src/aaa.rs"\n\n'
                '[[bin]]\nname = "zzz"\npath = "src/zzz.rs"\n'
            ),
            "src/aaa.rs": (
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        999\n"
                "    }\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/zzz.rs": (
                "mod inner {\n"
                "    pub mod x;\n"
                "}\n\n"
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        111\n"
                "    }\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/inner/x.rs": (
                "use crate::Cfg;\n\npub struct A;\n\nimpl Cfg for A {}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_twobins.src"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (f"{base}.inner.x.A", f"{base}.aaa.Cfg") not in implements, implements


def test_unreadable_explicit_target_keeps_the_ambiguity_phantom(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The read-failure guard covers explicit targets too: with two bins
    # listed and one unreadable (here a DIRECTORY named zzz.rs), the
    # surviving stem must not claim the package definitively; the
    # undeclared module keeps its ambiguity phantom, emitting no
    # IMPLEMENTS edge onto the alphabetically-first bin.
    project = temp_repo / "rs_amb_hole"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_amb_hole"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "aaa"\npath = "src/aaa.rs"\n\n'
                '[[bin]]\nname = "zzz"\npath = "src/zzz.rs"\n'
            ),
            "src/aaa.rs": (
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        999\n"
                "    }\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "src/other.rs": (
                "pub trait Cfg {\n    fn v(&self) -> u32 {\n        5\n    }\n}\n"
            ),
            "src/zzz.rs/placeholder.txt": "not rust\n",
            "src/inner/x.rs": (
                "use crate::Cfg;\n\npub struct A;\n\nimpl Cfg for A {}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_amb_hole.src"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (f"{base}.inner.x.A", f"{base}.aaa.Cfg") not in implements, implements


def test_lone_explicit_target_does_not_claim_an_auto_target_dir(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Auto-target directories hold arbitrarily many crate roots the
    # manifest never lists: tests/bbb.rs is its own crate by location,
    # and tests/common/mod.rs (the Rust Book's shared-helper idiom) is
    # compiled ONLY into the crates that declare it (cargo-verified:
    # bbb's Cfg). The lone explicit target must not claim the directory.
    project = temp_repo / "rs_auto_sibling"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_auto_sibling"\nversion = "0.1.0"\n\n'
                '[lib]\npath = "src/lib.rs"\n\n'
                '[[test]]\nname = "aaa"\npath = "tests/aaa.rs"\n'
            ),
            "src/lib.rs": "pub fn lib_item() {}\n",
            "tests/aaa.rs": (
                "pub trait Cfg {\n    fn v(&self) -> u32 {\n        111\n    }\n}\n"
            ),
            "tests/bbb.rs": (
                "mod common;\n\n"
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        222\n"
                "    }\n"
                "}\n"
            ),
            "tests/common/mod.rs": (
                "use crate::Cfg;\n\npub struct A;\n\nimpl Cfg for A {}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_auto_sibling"
    mapping = updater.factory.import_processor.import_mapping.get(
        f"{base}.tests.common"
    )
    assert mapping != {"Cfg": f"{base}.tests.aaa.Cfg"}, mapping
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (
        f"{base}.tests.common.A",
        f"{base}.tests.aaa.Cfg",
    ) not in implements, implements


def test_build_script_declarations_anchor_its_modules(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # build.rs beside the manifest is cargo's fifth auto crate root: a
    # module it declares belongs to the build-script crate
    # (cargo-verified: the bin prints its own 111 untouched), so the
    # declaring scan must see build.rs's declarations and the lone
    # explicit bin must not claim helper.rs.
    project = temp_repo / "rs_buildedge"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_buildedge"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "cli"\npath = "cli.rs"\n'
            ),
            "build.rs": (
                "mod helper;\n\n"
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        111\n"
                "    }\n"
                "}\n\n"
                "pub fn real_fn() -> u32 {\n"
                "    111\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "helper.rs": (
                "use crate::Cfg;\n"
                "use crate::real_fn;\n\n"
                "pub struct A;\n\n"
                "impl Cfg for A {}\n\n"
                "pub fn read() -> u32 {\n"
                "    real_fn()\n"
                "}\n"
            ),
            "cli.rs": (
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        999\n"
                "    }\n"
                "}\n\n"
                "pub fn real_fn() -> u32 {\n"
                "    999\n"
                "}\n\n"
                "fn main() {}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_buildedge"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (f"{base}.helper.A", f"{base}.build.Cfg") in implements, implements
    assert (f"{base}.helper.A", f"{base}.cli.Cfg") not in implements, implements
    calls = _calls(mock_ingestor)
    assert (f"{base}.helper.read", f"{base}.build.real_fn") in calls, calls
    assert (f"{base}.helper.read", f"{base}.cli.real_fn") not in calls, calls


def test_overridden_build_key_excludes_the_auto_build_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # With `[package] build = "gen.rs"` cargo never compiles build.rs at
    # all (cargo-verified: a garbage build.rs still builds); only the
    # override is the build script, so a module both declare anchors in
    # gen's crate.
    project = temp_repo / "rs_buildkey"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_buildkey"\nversion = "0.1.0"\n'
                'build = "gen.rs"\n\n'
                '[[bin]]\nname = "cli"\npath = "cli.rs"\n'
            ),
            "gen.rs": (
                "mod helper;\n\n"
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        111\n"
                "    }\n"
                "}\n\n"
                "pub fn real_fn() -> u32 {\n"
                "    111\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "build.rs": (
                "mod helper;\n\n"
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        555\n"
                "    }\n"
                "}\n\n"
                "pub fn real_fn() -> u32 {\n"
                "    555\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "helper.rs": (
                "use crate::Cfg;\n"
                "use crate::real_fn;\n\n"
                "pub struct A;\n\n"
                "impl Cfg for A {}\n\n"
                "pub fn read() -> u32 {\n"
                "    real_fn()\n"
                "}\n"
            ),
            "cli.rs": "fn main() {}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_buildkey"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (f"{base}.helper.A", f"{base}.gen.Cfg") in implements, implements
    assert (f"{base}.helper.A", f"{base}.build.Cfg") not in implements, implements


def test_trivial_build_script_does_not_block_the_fallback(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A build.rs holding nothing but fn main() declares nothing and can
    # claim nothing: its empty stem must not disable the explicit-only
    # fallback (cargo-verified: the bin binds its own inline mod flags,
    # never the root decoy).
    project = temp_repo / "rs_wb"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_wb"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "cli"\npath = "cli.rs"\n'
            ),
            "build.rs": "fn main() {}\n",
            "cli.rs": (
                '#[path = "q.rs"]\n'
                "mod renamed;\n\n"
                "mod flags {\n"
                "    pub trait Flag {\n"
                "        fn n(&self) -> u32 {\n"
                "            111\n"
                "        }\n"
                "    }\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "q.rs": (
                "use crate::flags::Flag;\n\npub struct A;\n\nimpl Flag for A {}\n"
            ),
            "flags.rs": (
                "pub trait Flag {\n    fn n(&self) -> u32 {\n        999\n    }\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_wb"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (f"{base}.q.A", f"{base}.cli.flags.Flag") in implements, implements
    assert (f"{base}.q.A", f"{base}.flags.Flag") not in implements, implements


def test_watch_modify_of_build_script_refreshes_its_declarations(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # build.rs is a cached entry now, so a watch MODIFY must re-read its
    # declarations like any other entry: moving `mod helper;` from
    # build.rs to cli.rs moves helper's crate with it.
    from watchdog.events import FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_buildstale"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_buildstale"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "cli"\npath = "cli.rs"\n'
            ),
            "build.rs": (
                "mod helper;\n\n"
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "cli.rs": (
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        2\n"
                "    }\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "helper.rs": ("use crate::Cfg;\n\npub struct A;\n\nimpl Cfg for A {}\n"),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_buildstale"
    assert updater.factory.import_processor.import_mapping.get(f"{base}.helper") == {
        "Cfg": f"{base}.build.Cfg"
    }

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    (project / "build.rs").write_text(
        "pub trait Cfg {\n"
        "    fn v(&self) -> u32 {\n"
        "        1\n"
        "    }\n"
        "}\n\n"
        "fn main() {}\n",
        encoding="utf-8",
    )
    (project / "cli.rs").write_text(
        "mod helper;\n\n"
        "pub trait Cfg {\n"
        "    fn v(&self) -> u32 {\n"
        "        2\n"
        "    }\n"
        "}\n\n"
        "fn main() {}\n",
        encoding="utf-8",
    )
    handler.dispatch(FileModifiedEvent(str(project / "build.rs")))
    handler.dispatch(FileModifiedEvent(str(project / "cli.rs")))
    handler.dispatch(FileModifiedEvent(str(project / "helper.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.helper")
    assert mapping == {"Cfg": f"{base}.cli.Cfg"}, mapping


def test_unreadable_build_script_keeps_the_phantom(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An unreadable build.rs is a hole like an unreadable lib.rs: the
    # lone explicit target must not definitively claim a module only the
    # build script declares.
    import os

    project = temp_repo / "rs_buildhole"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_buildhole"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "cli"\npath = "cli.rs"\n'
            ),
            "build.rs": (
                "mod helper;\n\n"
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        1\n"
                "    }\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "cli.rs": (
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        2\n"
                "    }\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "helper.rs": ("use crate::Cfg;\n\npub struct A;\n\nimpl Cfg for A {}\n"),
        },
    )
    build_rs = project / "build.rs"
    os.chmod(build_rs, 0)
    if os.access(build_rs, os.R_OK):
        os.chmod(build_rs, 0o644)
        pytest.skip("cannot make files unreadable in this environment")
    try:
        create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    finally:
        os.chmod(build_rs, 0o644)

    base = "rs_buildhole"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (f"{base}.helper.A", f"{base}.cli.Cfg") not in implements, implements


def test_watch_modify_of_a_module_named_build_stays_a_module(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cargo only auto-detects build.rs BESIDE Cargo.toml: src/build.rs is
    # an ordinary lib module, and a watch MODIFY of it must not inject a
    # phantom `build` entry stem that flips q.rs's attribution through
    # the item tie-break.
    from watchdog.events import FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_modbuild"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_modbuild"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod q;\npub mod build;\n",
            "src/build.rs": "pub mod q;\n\npub struct Config;\n",
            "src/build/q.rs": "pub fn inner() {}\n",
            "src/q.rs": "use crate::Config;\n\npub fn ay(_c: Config) {}\n",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_modbuild.src"
    expected = {"Config": f"{base}.lib.Config"}
    # lib.rs does not define Config itself: the phantom stem's item
    # tie-break is exactly what would steal the attribution.
    assert updater.factory.import_processor.import_mapping.get(f"{base}.q") == (
        expected
    )

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    handler.dispatch(FileModifiedEvent(str(project / "src" / "build.rs")))
    handler.dispatch(FileModifiedEvent(str(project / "src" / "q.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.q")
    assert mapping == expected, mapping


def test_build_true_means_auto_detection(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `[package] build = true` compiles build.rs exactly like unset
    # (cargo-verified: a garbage build.rs fails the build), so its
    # declarations still anchor helper.rs in the build-script crate.
    project = temp_repo / "rs_btrue"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_btrue"\nversion = "0.1.0"\n'
                "build = true\n\n"
                '[[bin]]\nname = "cli"\npath = "cli.rs"\n'
            ),
            "build.rs": (
                "mod helper;\n\n"
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        111\n"
                "    }\n"
                "}\n\n"
                "pub fn real_fn() -> u32 {\n"
                "    111\n"
                "}\n\n"
                "fn main() {}\n"
            ),
            "helper.rs": (
                "use crate::Cfg;\n"
                "use crate::real_fn;\n\n"
                "pub struct A;\n\n"
                "impl Cfg for A {}\n\n"
                "pub fn read() -> u32 {\n"
                "    real_fn()\n"
                "}\n"
            ),
            "cli.rs": (
                "pub trait Cfg {\n"
                "    fn v(&self) -> u32 {\n"
                "        999\n"
                "    }\n"
                "}\n\n"
                "pub fn real_fn() -> u32 {\n"
                "    999\n"
                "}\n\n"
                "fn main() {}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_btrue"
    implements = _pairs(mock_ingestor, RelationshipType.IMPLEMENTS.value)
    assert (f"{base}.helper.A", f"{base}.build.Cfg") in implements, implements
    assert (f"{base}.helper.A", f"{base}.cli.Cfg") not in implements, implements


def test_watch_modify_standing_in_for_a_coalesced_create_updates_listing(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The debounce layer keeps only the LATEST pending event per path, so
    # a new file written as CREATE then MODIFY within one window reaches
    # processing as a bare MODIFY. A modified file absent from the cached
    # directory listing therefore means the cache predates the file, and
    # the refresh must apply the same event-local delta a CREATE applies,
    # or a sibling's `crate::` rewrite keeps resolving against the
    # pre-create listing.
    from watchdog.events import FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_watch_coalesced_create"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_watch_coalesced_create"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod beta;\npub mod a;\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/a.rs": (
                "use crate::beta::helper;\n\npub fn top() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_watch_coalesced_create.src"

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    gamma2 = project / "src" / "gamma2.rs"
    gamma2.write_text("pub const fn helper2() -> u32 {\n    3\n}\n", encoding="utf-8")
    # MODIFY stands in for the coalesced CREATE.
    handler.dispatch(FileModifiedEvent(str(gamma2)))

    (project / "src" / "a.rs").write_text(
        "use crate::gamma2::helper2;\n\npub fn top() -> u32 {\n    helper2()\n}\n",
        encoding="utf-8",
    )
    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(project / "src" / "a.rs")))

    mapping = updater.factory.import_processor.import_mapping.get(f"{base}.a")
    assert mapping == {"helper2": f"{base}.gamma2.helper2"}, mapping
    calls = _calls(mock_ingestor)
    assert (f"{base}.a.top", f"{base}.gamma2.helper2") in calls, calls


def test_src_bin_main_crate_items_resolve_to_the_entry_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Cargo compiles src/bin/main.rs as a binary named `main` whose crate
    # root is the file itself (cargo-verified with this exact fixture; the
    # unaliased `use crate::helper;` spelling is E0255 in the defining
    # module, so the alias form is the legal self-reference): a `crate::`
    # path to one of the file's own items must land on the entry file's
    # qn, whichever probe of _rust_crate_root answers for the file.
    project = temp_repo / "rs_bin_main_selfref"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_bin_main_selfref"\nversion = "0.1.0"\n'
            ),
            "src/bin/main.rs": (
                "use crate::helper as h;\n\n"
                "pub const fn helper() -> u32 {\n    7\n}\n\n"
                "fn main() {\n    let _ = h();\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    mapping = updater.factory.import_processor.import_mapping.get(
        "rs_bin_main_selfref.src.bin.main"
    )
    assert mapping == {"h": "rs_bin_main_selfref.src.bin.main.helper"}, mapping


def test_watch_modify_of_an_already_deleted_file_leaves_the_listing_alone(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A MODIFY queued before a delete can reach processing after the file
    # is gone. The coalesced-create stand-in must not bake the dead name
    # into the cached listing: like the entry-declaration refresh, which
    # replaces declarations only on a successful read, the listing delta
    # is event-local and applies only while the file is observably there.
    from watchdog.events import FileModifiedEvent

    import realtime_updater

    project = temp_repo / "rs_watch_modify_after_delete"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_watch_modify_after_delete"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod beta;\npub mod a;\n",
            "src/beta.rs": "pub const fn helper() -> u32 {\n    2\n}\n",
            "src/a.rs": (
                "use crate::beta::helper;\n\npub fn top() -> u32 {\n    helper()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    gamma2 = project / "src" / "gamma2.rs"
    gamma2.write_text("pub const fn helper2() -> u32 {\n    3\n}\n", encoding="utf-8")
    gamma2.unlink()
    handler.dispatch(FileModifiedEvent(str(gamma2)))

    listing = updater.factory.import_processor._rust_dir_listing.get(
        str(project / "src")
    )
    assert listing is not None, "the full run should have cached the src listing"
    assert "gamma2.rs" not in listing, sorted(listing)


def test_two_bodied_cfg_twin_mods_keep_separate_uses(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Two mutually-exclusive cfg twins declare a bodied `mod run` in one
    # file; both share the qn foo.run and are indexed unconditionally, but
    # each twin imports a different helper. A twin's function must bind
    # through its OWN mod-body use, not the merged foo.run map (#1017).
    project = temp_repo / "rs_cfg_twin_bodied"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_cfg_twin_bodied"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod beta;\npub mod foo;\n",
            "src/alpha.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    3\n}\n",
            "src/foo.rs": (
                '#[cfg(feature = "ext")]\n'
                "pub mod run {\n"
                "    use crate::alpha::helper;\n\n"
                "    pub fn ga() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n\n"
                '#[cfg(not(feature = "ext"))]\n'
                "pub mod run {\n"
                "    use crate::beta::helper;\n\n"
                "    pub fn gb() -> u32 {\n"
                "        helper()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_cfg_twin_bodied.src"
    assert (f"{base}.foo.run.ga", f"{base}.alpha.helper") in calls, calls
    assert (f"{base}.foo.run.gb", f"{base}.beta.helper") in calls, calls
    assert (f"{base}.foo.run.ga", f"{base}.beta.helper") not in calls, calls
    assert (f"{base}.foo.run.gb", f"{base}.alpha.helper") not in calls, calls


def _module_qns(mock_ingestor: MagicMock) -> set[str]:
    return {
        props["qualified_name"]
        for label, props in (
            c[0] for c in mock_ingestor.ensure_node_batch.call_args_list
        )
        if label == "Module"
    }


def test_inline_mod_in_a_trait_body_has_consistent_module_and_defines_qns(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An inline mod in a trait const initializer keeps the trait scope in its
    # qn (foo.T.inner). Its Module node, the DEFINES from its enclosing module,
    # and the DEFINES to its own functions must all agree, or the graph audit
    # reports an orphan module and a dangling edge (issue #1018).
    project = temp_repo / "rs_inline_mod_trait"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_inline_mod_trait"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": (
                "pub trait T {\n"
                "    const C: u32 = {\n"
                "        mod inner {\n"
                "            pub const fn g() -> u32 {\n                1\n            }\n"
                "        }\n"
                "        inner::g()\n"
                "    };\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    base = "rs_inline_mod_trait.src"
    modules = _module_qns(mock_ingestor)
    assert f"{base}.foo.T.inner" in modules, modules
    assert f"{base}.foo.inner" not in modules, modules
    defines = _pairs(mock_ingestor, RelationshipType.DEFINES.value)
    assert (f"{base}.foo", f"{base}.foo.T.inner") in defines, defines
    assert (f"{base}.foo.T.inner", f"{base}.foo.T.inner.g") in defines, defines


def test_inline_mod_in_an_impl_body_has_consistent_module_and_defines_qns(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The impl-body variant: the items inside key under the impl target
    # (foo.S.inner.g), so the inline Module node keys as foo.S.inner too, and
    # its enclosing-module DEFINES and function DEFINES must agree (issue
    # #1018).
    project = temp_repo / "rs_inline_mod_impl"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_inline_mod_impl"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": (
                "pub struct S;\n\n"
                "impl S {\n"
                "    pub const C: u32 = {\n"
                "        mod inner {\n"
                "            pub const fn g() -> u32 {\n                1\n            }\n"
                "        }\n"
                "        inner::g()\n"
                "    };\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    base = "rs_inline_mod_impl.src"
    modules = _module_qns(mock_ingestor)
    assert f"{base}.foo.S.inner" in modules, modules
    assert f"{base}.foo.inner" not in modules, modules
    defines = _pairs(mock_ingestor, RelationshipType.DEFINES.value)
    assert (f"{base}.foo", f"{base}.foo.S.inner") in defines, defines
    assert (f"{base}.foo.S.inner", f"{base}.foo.S.inner.g") in defines, defines


def test_nested_inline_mods_in_a_trait_body_keep_the_scoped_parent_qn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A mod nested inside another mod inside a trait body: the inner mod's
    # enclosing-module DEFINES must point at the outer mod's SCOPED qn
    # (foo.T.outer), not a mods-only re-walk that drops the trait scope to
    # foo.outer (a node that does not exist), which would dangle the edge
    # (CodeRabbit review on PR #1166).
    project = temp_repo / "rs_nested_inline_mod_trait"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_nested_inline_mod_trait"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": (
                "pub trait T {\n"
                "    const C: u32 = {\n"
                "        mod outer {\n"
                "            pub mod inner {\n"
                "                pub const fn g() -> u32 {\n                    1\n                }\n"
                "            }\n"
                "        }\n"
                "        outer::inner::g()\n"
                "    };\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    base = "rs_nested_inline_mod_trait.src"
    modules = _module_qns(mock_ingestor)
    assert f"{base}.foo.T.outer" in modules, modules
    assert f"{base}.foo.T.outer.inner" in modules, modules
    assert f"{base}.foo.outer" not in modules, modules
    defines = _pairs(mock_ingestor, RelationshipType.DEFINES.value)
    assert (f"{base}.foo", f"{base}.foo.T.outer") in defines, defines
    assert (f"{base}.foo.T.outer", f"{base}.foo.T.outer.inner") in defines, defines
    assert (
        f"{base}.foo.T.outer.inner",
        f"{base}.foo.T.outer.inner.g",
    ) in defines, defines
