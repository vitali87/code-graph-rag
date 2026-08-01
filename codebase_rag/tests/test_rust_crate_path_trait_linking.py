# Rust `use crate::...` paths were stored raw in the import mapping, so a
# trait imported through them resolved to a phantom external qn
# (crate.flags.Flag) instead of the real project node. The IMPLEMENTS edge
# dangled onto an ExternalModule and the override pass emitted no OVERRIDES
# edges, so dead-code could not expand liveness from a live trait method to
# its implementations (ripgrep: 938 of 1811 candidates; issue #1007).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

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
    assert "sneaky" not in items and "Hidden" not in items, items


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


def test_duplicate_method_qn_does_not_steal_body_use(
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
                "        other()\n"
                "    }\n"
                "}\n\n"
                "pub fn other() -> u32 { 1 }\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    base = "rs_dup_method_use.src"
    assert (f"{base}.foo.S.run", f"{base}.foo.other") in calls, calls
    assert (f"{base}.foo.S.run", f"{base}.alpha.other") not in calls, calls
    # Beta::run's use is keyed on its dedup variant, ready for the caller
    # side: the method call pass still attributes Beta::run's calls to the
    # natural qn (issue #1014), so no @13 caller edge exists yet.
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
