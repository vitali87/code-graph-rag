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
