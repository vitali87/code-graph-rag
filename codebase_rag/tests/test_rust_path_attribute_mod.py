"""A `#[path]` attribute redirects a Rust mod declaration to another file.

The qn scheme is path-derived, so the target keys under where the FILE
sits, while every declaration-side resolution used to compute the
name-derived spelling: a qn no module owns. Both consumers now read the
attribute, so a `#[cfg(test)]` gate reaches the file it names and a crate
path through the declared name lands on that file too (issue #1035).
"""

import time
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.parsers.import_processor import (
    RustEntryDecls,
    _rs_entry_decls_of,
    _rs_strip_comments_and_strings,
    _rs_top_level_only,
)
from codebase_rag.tests.test_rust_cfg_test_mod_declarations import _declared_gates
from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)


def test_path_attribute_gate_names_the_target_file_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `#[path = "support/helpers.rs"] mod helpers;` in src/lib.rs backs
    # src/support/helpers.rs, which indexes as src.support.helpers. The
    # name-derived src.helpers owns nothing, so recording it leaves the
    # gate inert and `fixture` reads as production code.
    project = temp_repo / "rs_path_attr"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_path_attr"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "#[cfg(test)]\n"
                '#[path = "support/helpers.rs"]\n'
                "mod helpers;\n"
                "\n"
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
            ),
            "src/support/helpers.rs": ("pub(crate) fn fixture() -> i32 {\n    7\n}\n"),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    gates = _declared_gates(mock_ingestor, "rs_path_attr.src.lib")
    assert "rs_path_attr.src.support.helpers" in gates, gates


def test_path_attribute_target_beside_a_plain_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The path is relative to the DIRECTORY holding the declaring file,
    # never to a directory named after it, so a plain (non mod-rs) file
    # redirects its declaration into its own parent directory.
    project = temp_repo / "rs_path_attr_plain"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_plain"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": (
                "#[cfg(test)]\n"
                '#[path = "fixtures/rig.rs"]\n'
                "mod rig;\n"
                "\n"
                "pub fn run() -> i32 {\n"
                "    1\n"
                "}\n"
            ),
            "src/fixtures/rig.rs": "pub(crate) fn build() -> i32 {\n    2\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    gates = _declared_gates(mock_ingestor, "rs_path_attr_plain.src.engine")
    assert "rs_path_attr_plain.src.fixtures.rig" in gates, gates


def test_declaration_without_a_path_attribute_is_untouched(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The ordinary spelling still resolves through the relative-path
    # machinery; only a redirected declaration takes the file-derived qn.
    project = temp_repo / "rs_path_attr_none"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_none"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "#[cfg(test)]\nmod helpers;\n\npub fn add() -> i32 {\n    1\n}\n",
            "src/helpers.rs": "pub(crate) fn fixture() -> i32 {\n    7\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    gates = _declared_gates(mock_ingestor, "rs_path_attr_none.src.lib")
    assert "rs_path_attr_none.src.helpers" in gates, gates
    assert not any(cs.SEPARATOR_DOT + "support" in gate for gate in gates), gates


def test_crate_path_resolves_through_a_redirected_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `crate::helpers::fixture()` names the module by its DECLARED name
    # while the module keys under the redirected file, so the crate path
    # has to land on the file's qn to reach anything at all.
    project = temp_repo / "rs_path_attr_call"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_call"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                '#[path = "support/helpers.rs"]\n'
                "mod helpers;\n"
                "pub mod a;\n"
                "pub mod decoy;\n"
            ),
            "src/support/helpers.rs": "pub(crate) fn fixture() -> i32 {\n    7\n}\n",
            "src/decoy.rs": "pub fn fixture() -> i32 {\n    1\n}\n",
            "src/a.rs": ("pub fn run() -> i32 {\n    crate::helpers::fixture()\n}\n"),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    assert (
        "rs_path_attr_call.src.a.run",
        "rs_path_attr_call.src.support.helpers.fixture",
    ) in calls, calls
    assert (
        "rs_path_attr_call.src.a.run",
        "rs_path_attr_call.src.decoy.fixture",
    ) not in calls, calls


def test_crate_path_resolves_a_redirect_declared_outside_an_entry_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The redirect sits in src/engine.rs, an ordinary module file, so only
    # the crate entry's declarations used to be consulted and the tail of
    # the path attached by name. src/engine/rig.rs is present and declared
    # by nobody, which is exactly where the name-derived spelling lands
    # (issue #1065); rustc compiles src/fixtures/rig.rs.
    project = temp_repo / "rs_path_attr_deep"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_deep"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod engine;\npub mod a;\n",
            "src/engine.rs": ('#[path = "fixtures/rig.rs"]\npub mod rig;\n'),
            "src/fixtures/rig.rs": "pub fn build() -> i32 {\n    2\n}\n",
            "src/engine/rig.rs": "pub fn build() -> i32 {\n    9\n}\n",
            "src/a.rs": ("pub fn go() -> i32 {\n    crate::engine::rig::build()\n}\n"),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_deep.src.a.go"
    assert (caller, "rs_path_attr_deep.src.fixtures.rig.build") in calls, calls
    assert (caller, "rs_path_attr_deep.src.engine.rig.build") not in calls, calls


def test_a_relative_path_resolves_a_redirect_declared_beside_it(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `super::rig` from a sibling module reaches the same declaration by a
    # different route, and that route never touched the entry map at all.
    project = temp_repo / "rs_path_attr_super"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_super"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": (
                '#[path = "fixtures/rig.rs"]\npub mod rig;\npub mod a;\n'
            ),
            "src/fixtures/rig.rs": "pub fn build() -> i32 {\n    2\n}\n",
            "src/engine/rig.rs": "pub fn build() -> i32 {\n    9\n}\n",
            "src/engine/a.rs": ("pub fn go() -> i32 {\n    super::rig::build()\n}\n"),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_super.src.engine.a.go"
    assert (caller, "rs_path_attr_super.src.fixtures.rig.build") in calls, calls
    assert (caller, "rs_path_attr_super.src.engine.rig.build") not in calls, calls


def test_a_redirect_onto_a_mod_rs_is_not_read_off_its_sibling_shadow(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A redirect naming `platform/mod.rs` backs the DIRECTORY, and the qn
    # scheme drops the `mod` segment, so `src.platform` looks exactly like a
    # plain file module. Continuing the walk from the sibling `src/platform.rs`
    # reads an undeclared shadow, whose own redirect then sends `inner` to a
    # third file rustc never compiles into this path.
    project = temp_repo / "rs_path_attr_modrs"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_modrs"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod engine;\npub mod a;\n",
            "src/engine.rs": '#[path = "platform/mod.rs"]\npub mod platform;\n',
            "src/platform/mod.rs": "pub mod inner;\n",
            "src/platform/inner.rs": "pub fn go() -> i32 {\n    1\n}\n",
            "src/platform.rs": '#[path = "elsewhere/inner.rs"]\npub mod inner;\n',
            "src/elsewhere/inner.rs": "pub fn other() -> i32 {\n    9\n}\n",
            "src/a.rs": (
                "pub fn run() -> i32 {\n    crate::engine::platform::inner::go()\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_modrs.src.a.run"
    assert (caller, "rs_path_attr_modrs.src.platform.inner.go") in calls, calls
    assert not [
        pair for pair in calls if pair[0] == caller and "elsewhere" in pair[1]
    ], calls


def test_super_inside_a_redirect_target_climbs_the_module_tree(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `#[path]` moves the FILE, not the module's place in the tree. `rig` is
    # `crate::engine::rig`, so `super` is `engine` and `super::sib` is backed
    # by src/engine/sib.rs. Popping a segment off the file's own qn lands on
    # src/fixtures instead, where an undeclared shadow answers (issue #1083).
    project = temp_repo / "rs_path_attr_super_target"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_super_target"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": '#[path = "fixtures/rig.rs"]\npub mod rig;\npub mod sib;\n',
            "src/engine/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/fixtures/sib.rs": "pub fn run() -> i32 {\n    9\n}\n",
            "src/fixtures/rig.rs": (
                "pub fn build() -> i32 {\n    super::sib::run()\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_super_target.src.fixtures.rig.build"
    assert (caller, "rs_path_attr_super_target.src.engine.sib.run") in calls, calls
    assert (caller, "rs_path_attr_super_target.src.fixtures.sib.run") not in calls, (
        calls
    )


def test_super_from_an_ordinary_module_still_climbs_its_own_directory(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The redirect map must claim only files a redirect names. A plain module
    # in the same directory keeps the physical climb, which for it IS the
    # module tree.
    project = temp_repo / "rs_path_attr_super_plain"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_super_plain"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": "pub mod rig;\npub mod sib;\n",
            "src/engine/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/engine/rig.rs": (
                "pub fn build() -> i32 {\n    super::sib::run()\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_super_plain.src.engine.rig.build"
    assert (caller, "rs_path_attr_super_plain.src.engine.sib.run") in calls, calls


def test_a_module_declared_where_it_sits_keeps_its_own_super(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # rustc compiles a file into every tree that declares it, and `helper` is
    # declared by its own neighbour as well as aliased from `a`. The
    # neighbour's spelling is the one `super::` inside it counts from, so an
    # alias written anywhere else must not move it.
    project = temp_repo / "rs_path_attr_super_alias"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_super_alias"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod a;\npub mod b;\n",
            "src/b.rs": "pub mod helper;\npub mod sib;\n",
            "src/b/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/b/helper.rs": "pub fn build() -> i32 {\n    super::sib::run()\n}\n",
            "src/a.rs": '#[path = "b/helper.rs"]\npub mod aliased;\npub mod sib;\n',
            "src/a/sib.rs": "pub fn run() -> i32 {\n    9\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_super_alias.src.b.helper.build"
    assert (caller, "rs_path_attr_super_alias.src.b.sib.run") in calls, calls
    assert (caller, "rs_path_attr_super_alias.src.a.sib.run") not in calls, calls


def test_super_super_from_a_redirect_target_climbs_declarer_by_declarer(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `b` is `crate::a::b` and `a` is `crate::a`, both moved, so the two
    # climbs are one hop each through the declaring module and land on the
    # crate root. Popping segments off the declarer's own path instead stops
    # inside src/fx, where a shadow answers.
    project = temp_repo / "rs_path_attr_super_nested"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_super_nested"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": '#[path = "fx/a.rs"]\npub mod a;\npub mod top;\n',
            "src/fx/a.rs": '#[path = "b.rs"]\npub mod b;\n',
            "src/fx/b.rs": ("pub fn build() -> i32 {\n    super::super::top::f()\n}\n"),
            "src/top.rs": "pub fn f() -> i32 {\n    1\n}\n",
            "src/fx/top.rs": "pub fn f() -> i32 {\n    9\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_super_nested.src.fx.b.build"
    assert (caller, "rs_path_attr_super_nested.src.top.f") in calls, calls
    assert (caller, "rs_path_attr_super_nested.src.fx.top.f") not in calls, calls


def test_super_from_an_inline_mod_inside_a_redirect_target_climbs_the_tree(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `#[cfg(test)] mod tests { use super::super::*; }` is the shape a
    # `#[path]`-ed fixture file invites. The inline mod keeps its own place,
    # so the first climb is the file's module and only the second is moved.
    project = temp_repo / "rs_path_attr_super_inline"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_super_inline"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": '#[path = "fixtures/rig.rs"]\npub mod rig;\npub mod sib;\n',
            "src/engine/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/fixtures/sib.rs": "pub fn run() -> i32 {\n    9\n}\n",
            "src/fixtures/rig.rs": (
                "pub mod inner {\n"
                "    use super::super::sib::run;\n"
                "\n"
                "    pub fn build() -> i32 {\n"
                "        run()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_super_inline.src.fixtures.rig.inner.build"
    assert (caller, "rs_path_attr_super_inline.src.engine.sib.run") in calls, calls
    assert (caller, "rs_path_attr_super_inline.src.fixtures.sib.run") not in calls, (
        calls
    )


def test_a_redirect_under_cargos_src_bin_still_moves_its_target(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `bin` is a build-output ignore EXCEPT directly under src/, where cargo
    # keeps first-party binaries. The graph holds those files, so the sweep
    # that reads their declarations has to walk there too.
    project = temp_repo / "rs_path_attr_super_bin"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_super_bin"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub fn lib_root() -> i32 {\n    0\n}\n",
            "src/bin/tool.rs": (
                '#[path = "fx/rig.rs"]\nmod rig;\nmod sib;\nfn main() {}\n'
            ),
            "src/bin/tool/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/bin/fx/sib.rs": "pub fn run() -> i32 {\n    9\n}\n",
            "src/bin/fx/rig.rs": (
                "pub fn build() -> i32 {\n    super::sib::run()\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_super_bin.src.bin.fx.rig.build"
    assert (caller, "rs_path_attr_super_bin.src.bin.tool.sib.run") in calls, calls
    assert (caller, "rs_path_attr_super_bin.src.bin.fx.sib.run") not in calls, calls


def test_two_declarers_of_one_target_resolve_the_same_way_everywhere(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A helper shared by two modules compiles into both trees, and the graph
    # keys it once, so `super::` in it has to pick one. The pick is the walk
    # order, which is sorted precisely so a developer machine and CI agree.
    project = temp_repo / "rs_path_attr_super_dup"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_super_dup"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod alpha;\npub mod omega;\n",
            "src/alpha.rs": (
                '#[path = "fx/shared.rs"]\npub mod shared;\n'
                "pub fn helper() -> i32 {\n    1\n}\n"
            ),
            "src/omega.rs": (
                '#[path = "fx/shared.rs"]\npub mod shared;\n'
                "pub fn helper() -> i32 {\n    2\n}\n"
            ),
            "src/fx/shared.rs": "pub fn build() -> i32 {\n    super::helper()\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_super_dup.src.fx.shared.build"
    assert (caller, "rs_path_attr_super_dup.src.alpha.helper") in calls, calls
    assert (caller, "rs_path_attr_super_dup.src.omega.helper") not in calls, calls


def test_a_new_redirect_moves_its_target_for_the_watcher(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The realtime watcher re-parses without entering _process_files, so the
    # per-run reset never fires. An edit that adds or drops a redirect
    # changes who declares the target, and a stale map keeps `super::` in
    # that file pointing where it no longer belongs.
    project = temp_repo / "rs_path_attr_super_watch"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_super_watch"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": "pub mod sib;\n",
            "src/engine/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/fixtures/rig.rs": (
                "pub fn build() -> i32 {\n    super::sib::run()\n}\n"
            ),
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    processor = updater.factory.import_processor
    target = "rs_path_attr_super_watch.src.fixtures.rig"
    assert processor._rust_logical_parent(target) is None
    engine = project / "src" / "engine.rs"
    engine.write_text(
        '#[path = "fixtures/rig.rs"]\npub mod rig;\npub mod sib;\n', encoding="utf-8"
    )
    processor.refresh_rust_path_caches_for(engine, created=False)
    assert (
        processor._rust_logical_parent(target) == "rs_path_attr_super_watch.src.engine"
    )


def test_a_commented_out_redirect_does_not_hijack_a_live_declaration(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The declaration scan reads comment-and-string-blanked source so that
    # no comment or literal can present a declaration. The `#[path]` value
    # has to survive that blanking to be read at all, and keeping it must
    # not reopen the door: an attribute inside a comment is not one.
    project = temp_repo / "rs_path_attr_comment"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_comment"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                "/* previous layout:\n"
                '#[path = "decoy.rs"]\n'
                "mod helpers;\n"
                "*/\n"
                "mod helpers;\n"
                "pub mod decoy;\n"
                "pub mod a;\n"
            ),
            "src/helpers.rs": "pub fn fixture() -> i32 {\n    7\n}\n",
            "src/decoy.rs": "pub fn fixture() -> i32 {\n    1\n}\n",
            "src/a.rs": "pub fn run() -> i32 {\n    crate::helpers::fixture()\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_comment.src.a.run"
    assert (caller, "rs_path_attr_comment.src.helpers.fixture") in calls, calls
    assert (caller, "rs_path_attr_comment.src.decoy.fixture") not in calls, calls


def test_a_redirect_inside_a_string_literal_is_not_read(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A raw string may hold whole lines of Rust; none of it is code.
    project = temp_repo / "rs_path_attr_string"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_string"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                'pub const SAMPLE: &str = r#"\n'
                '#[path = "decoy.rs"]\n'
                "mod helpers;\n"
                '"#;\n'
                "mod helpers;\n"
                "pub mod decoy;\n"
                "pub mod a;\n"
            ),
            "src/helpers.rs": "pub fn fixture() -> i32 {\n    7\n}\n",
            "src/decoy.rs": "pub fn fixture() -> i32 {\n    1\n}\n",
            "src/a.rs": "pub fn run() -> i32 {\n    crate::helpers::fixture()\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_string.src.a.run"
    assert (caller, "rs_path_attr_string.src.helpers.fixture") in calls, calls
    assert (caller, "rs_path_attr_string.src.decoy.fixture") not in calls, calls


def test_a_redirect_declared_inside_a_macro_body_is_read(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Platform modules are routinely declared inside `cfg_if!`, and the
    # declaration scan keeps depth-0 macro bodies for exactly that reason,
    # so a redirect written there is a real one.
    project = temp_repo / "rs_path_attr_macro"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_macro"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                "pub mod a;\n"
                "cfg_if! {\n"
                '    #[path = "support/helpers.rs"]\n'
                "    mod helpers;\n"
                "}\n"
            ),
            "src/support/helpers.rs": "pub fn fixture() -> i32 {\n    7\n}\n",
            "src/decoy.rs": "pub fn fixture() -> i32 {\n    1\n}\n",
            "src/a.rs": "pub fn run() -> i32 {\n    crate::helpers::fixture()\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_macro.src.a.run"
    assert (
        caller,
        "rs_path_attr_macro.src.support.helpers.fixture",
    ) in calls, calls


def test_an_absolute_redirect_claims_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An absolute path names a file outside the indexed tree, so both the
    # gate and the crate path stand down rather than inventing a spelling.
    project = temp_repo / "rs_path_attr_abs"
    _write(
        project,
        {
            "Cargo.toml": ('[package]\nname = "rs_path_attr_abs"\nversion = "0.1.0"\n'),
            "src/lib.rs": (
                "#[cfg(test)]\n"
                '#[path = "/nowhere/helpers.rs"]\n'
                "mod helpers;\n"
                "\n"
                "pub fn add() -> i32 {\n    1\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    gates = _declared_gates(mock_ingestor, "rs_path_attr_abs.src.lib")
    assert not any("nowhere" in gate for gate in gates), gates


def _scan_cost_per_char(source: str) -> tuple[float, RustEntryDecls]:
    """Best-of-three seconds per scanned character, and what was found."""
    top_level = _rs_top_level_only(_rs_strip_comments_and_strings(source))
    best = None
    for _ in range(3):
        start = time.perf_counter()
        decls = _rs_entry_decls_of(top_level)
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    assert best is not None
    return best / len(top_level), decls


def test_the_declaration_scan_stays_linear_in_blank_source() -> None:
    # Every declaration pattern opened `^\s*`, and `\s` matches newlines, so
    # under MULTILINE each line start rescanned the whole run of whitespace
    # after it. Comment blanking turns a licence header or a doc block into
    # exactly that run, and every crate entry file is scanned in full with no
    # prescan in front of it (issue #1087).
    #
    # The comparison is against DENSE source through the same code path, so
    # the machine's speed cancels and what is left is the property itself:
    # whether blanked text costs more per character than real code. It did,
    # by about 59000x; it now costs about 4x, and no absolute wall-clock
    # budget has to be guessed for the slowest CI worker.
    blank = "pub mod a;\n" + "// a line of an ordinary doc block\n" * 20000
    dense = "pub mod a;\n" + "pub fn f(a: i32) -> i32 { a + 1 }\n" * 20000
    blank_cost, decls = _scan_cost_per_char(blank)
    dense_cost, _ = _scan_cost_per_char(dense)
    assert "a" in decls.mods, decls
    assert blank_cost < dense_cost * 50, (
        f"blank {blank_cost * 1e9:.1f}ns/char vs dense {dense_cost * 1e9:.1f}ns/char"
    )


def test_an_attribute_on_its_own_line_still_reaches_its_declaration() -> None:
    # The leading whitespace is what crosses lines wrongly; the whitespace
    # BETWEEN an attribute and the declaration it decorates has to keep
    # crossing them, since that spelling is the idiomatic one.
    source = (
        '#[cfg(unix)]\n\n    \n#[path = "sys/unix.rs"]\n    pub(crate) mod platform;\n'
    )
    decls = _rs_entry_decls_of(
        _rs_top_level_only(_rs_strip_comments_and_strings(source))
    )
    assert "platform" in decls.mods, decls
    assert decls.redirects == {"platform": "sys/unix.rs"}, decls.redirects


def test_two_cfg_variants_of_one_name_claim_no_single_target() -> None:
    # Platform modules are declared once per cfg under a single name, each
    # redirected somewhere else. Exactly one compiles and nothing in a
    # static scan knows which, so the name keeps no redirect rather than
    # the one that happens to be written last.
    source = (
        "#[cfg(unix)]\n"
        '#[path = "sys/unix.rs"]\n'
        "mod platform;\n"
        "#[cfg(windows)]\n"
        '#[path = "sys/windows.rs"]\n'
        "mod platform;\n"
    )
    decls = _rs_entry_decls_of(
        _rs_top_level_only(_rs_strip_comments_and_strings(source))
    )
    assert "platform" in decls.mods, decls
    assert decls.redirects == {}, decls.redirects


def test_two_cfg_variants_gate_both_of_their_targets(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The gate side sees each declaration on its own, and both really are
    # gated, so both targets are recorded whichever one compiles.
    project = temp_repo / "rs_path_attr_cfg_gate"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_cfg_gate"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                "#[cfg(test)]\n"
                '#[path = "sys/unix.rs"]\n'
                "mod platform;\n"
                "#[cfg(test)]\n"
                '#[path = "sys/windows.rs"]\n'
                "mod other;\n"
            ),
            "src/sys/unix.rs": "pub fn boot() -> i32 {\n    1\n}\n",
            "src/sys/windows.rs": "pub fn boot() -> i32 {\n    2\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    gates = _declared_gates(mock_ingestor, "rs_path_attr_cfg_gate.src.lib")
    assert "rs_path_attr_cfg_gate.src.sys.unix" in gates, gates
    assert "rs_path_attr_cfg_gate.src.sys.windows" in gates, gates


def test_a_drive_qualified_redirect_claims_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `C:/...` is absolute on Windows and carries no separator this check
    # would otherwise notice.
    project = temp_repo / "rs_path_attr_drive"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_drive"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                "#[cfg(test)]\n"
                '#[path = "C:/outside/helpers.rs"]\n'
                "mod helpers;\n"
                "\n"
                "pub fn add() -> i32 {\n    1\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    gates = _declared_gates(mock_ingestor, "rs_path_attr_drive.src.lib")
    assert not any("outside" in gate or "C:" in gate for gate in gates), gates


def test_redirect_inside_an_inline_module_gates_the_file_it_names(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Inside an inline block the path counts from the declaring file's
    # directory plus the inline chain, so the gate still has to name the
    # file the indexer keyed the module by.
    project = temp_repo / "rs_path_attr_inline"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_inline"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                "pub mod outer {\n"
                "    #[cfg(test)]\n"
                '    #[path = "redirected.rs"]\n'
                "    mod child;\n"
                "}\n"
            ),
            "src/outer/redirected.rs": ("pub(crate) fn fixture() -> i32 {\n    7\n}\n"),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    gates = _declared_gates(mock_ingestor, "rs_path_attr_inline.src.lib")
    assert "rs_path_attr_inline.src.outer.redirected" in gates, gates


def test_a_super_call_from_an_inline_mod_resolves_through_the_written_path(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The same climb as the `use` above, written at the CALL instead. The
    # resolver used to decline every relative path from a caller nested below
    # the file module, because an inline mod and an impl block are one shape in
    # qn space, and the enclosing-scope fallback then matched the tail by NAME
    # against the file's own physical ancestors -- landing on the undeclared
    # shadow that happens to sit there (issue #1086).
    project = temp_repo / "rs_inline_super_call"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_inline_super_call"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": '#[path = "fixtures/rig.rs"]\npub mod rig;\npub mod sib;\n',
            "src/engine/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/fixtures/sib.rs": "pub fn run() -> i32 {\n    9\n}\n",
            "src/fixtures/rig.rs": (
                "pub mod inner {\n"
                "    pub fn build() -> i32 {\n"
                "        super::super::sib::run()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_inline_super_call.src.fixtures.rig.inner.build"
    assert (caller, "rs_inline_super_call.src.engine.sib.run") in calls, calls
    assert (caller, "rs_inline_super_call.src.fixtures.sib.run") not in calls, calls


def test_a_super_call_from_a_cfg_test_mod_resolves_through_the_written_path(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `#[cfg(test)] mod tests { ... super::super::... }` inside a `#[path]`-ed
    # fixture file is the shape that reaches this most often (issue #1086). The
    # gate makes no difference to the climb: `tests` is an inline mod like any
    # other, so the first hop is the file's module and only the second is moved.
    project = temp_repo / "rs_cfg_test_super_call"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_cfg_test_super_call"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": '#[path = "fixtures/rig.rs"]\npub mod rig;\npub mod sib;\n',
            "src/engine/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/fixtures/sib.rs": "pub fn run() -> i32 {\n    9\n}\n",
            "src/fixtures/rig.rs": (
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    pub fn check() -> i32 {\n"
                "        super::super::sib::run()\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_cfg_test_super_call.src.fixtures.rig.tests.check"
    assert (caller, "rs_cfg_test_super_call.src.engine.sib.run") in calls, calls
    assert (caller, "rs_cfg_test_super_call.src.fixtures.sib.run") not in calls, calls
