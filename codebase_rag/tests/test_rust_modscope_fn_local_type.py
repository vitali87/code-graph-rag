"""A Rust local takes the return type of a helper reached through a module.

`let td = tmpdir();` types `td` from `tmpdir`'s return type, so `td.path()`
binds to `TempDir::path`. That worked only for a top-level fn of the same
file or one imported by name. A helper defined inside an inline module (the
`#[cfg(test)] mod tests { fn tmpdir() ... }` shape) and a module-qualified
call (`util::tmpdir()`, `crate::util::tmpdir()`, `inner::make()`) left the
local untyped, and the method fell through to a name-only match on whichever
`path` sorted first (issue #2942: 358 of ripgrep's 910 wrong edges).

Every fixture keeps a decoy `path` method in `aaa.rs`, which sorts first, so
a name-only fallback lands on it: only a typed local gives the edges
asserted here. The fixtures pass `cargo check --tests`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_CARGO = '[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'

_DECOY_RS = """\
pub struct Ignore;
impl Ignore {
    pub fn path(&self) -> u8 { 0 }
}
"""

_UTIL_RS = """\
pub struct TempDir;
impl TempDir {
    pub fn path(&self) -> u8 { 1 }
}
pub fn tmpdir() -> TempDir { TempDir }
"""

_LIB_RS = "pub mod aaa;\npub mod util;\npub mod walk;\n"


def _index(temp_repo: Path, mock_ingestor: MagicMock, name: str, walk_rs: str) -> str:
    project = temp_repo / name
    files = {
        "Cargo.toml": _CARGO.format(name=name),
        "src/lib.rs": _LIB_RS,
        "src/aaa.rs": _DECOY_RS,
        "src/util.rs": _UTIL_RS,
        "src/walk.rs": walk_rs,
    }
    for rel_path, source in files.items():
        target = project / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    return f"{name}.src"


def _path_targets(mock_ingestor: MagicMock, caller: str) -> dict[str, str]:
    # Resolution of each `.path` method the caller is bound to.
    targets: dict[str, str] = {}
    for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value):
        src, dst = str(call.args[0][2]), str(call.args[2][2])
        if src != caller or not dst.endswith(".path"):
            continue
        props = call.kwargs.get("properties") or (
            call.args[3] if len(call.args) > 3 else {}
        )
        targets[dst] = str((props or {}).get(cs.KEY_RESOLUTION))
    return targets


def test_helper_inside_inline_test_module_types_the_local(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_tests",
        """\
#[cfg(test)]
mod tests {
    use crate::util::TempDir;

    fn tmpdir() -> TempDir { TempDir }

    #[test]
    fn walks() {
        let td = tmpdir();
        assert_eq!(td.path(), 1);
    }
}
""",
    )
    targets = _path_targets(mock_ingestor, f"{base}.walk.tests.walks")
    assert targets == {f"{base}.util.TempDir.path": cs.EdgeResolution.EXACT}, targets


def test_use_inside_inline_module_types_the_local(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The `use` binding the helper lives in the inline module's own scope.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_use",
        """\
#[cfg(test)]
mod tests {
    use crate::util::tmpdir;

    #[test]
    fn walks() {
        let td = tmpdir();
        assert_eq!(td.path(), 1);
    }
}
""",
    )
    targets = _path_targets(mock_ingestor, f"{base}.walk.tests.walks")
    assert targets == {f"{base}.util.TempDir.path": cs.EdgeResolution.EXACT}, targets


def test_module_qualified_calls_type_the_local(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_qual",
        """\
use crate::util;

pub fn imported_module() -> u8 {
    let td = util::tmpdir();
    td.path()
}

pub fn crate_path() -> u8 {
    let td = crate::util::tmpdir();
    td.path()
}

mod inner {
    pub struct Maker;
    impl Maker {
        pub fn path(&self) -> u8 { 2 }
    }
    pub fn make() -> Maker { Maker }
}

pub fn sibling_inline_module() -> u8 {
    let m = inner::make();
    m.path()
}
""",
    )
    temp_path = {f"{base}.util.TempDir.path": cs.EdgeResolution.EXACT}
    for caller in ("imported_module", "crate_path"):
        targets = _path_targets(mock_ingestor, f"{base}.walk.{caller}")
        assert targets == temp_path, (caller, targets)
    targets = _path_targets(mock_ingestor, f"{base}.walk.sibling_inline_module")
    assert targets == {f"{base}.walk.inner.Maker.path": cs.EdgeResolution.EXACT}, (
        targets
    )


def test_inline_module_helper_shadows_only_inside_its_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The file's own `tmpdir` returns `Ignore`; the test module's returns
    # `TempDir`. Each call site gets the one in its own scope.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_shadow",
        """\
use crate::aaa::Ignore;

fn tmpdir() -> Ignore { Ignore }

pub fn top() -> u8 {
    let td = tmpdir();
    td.path()
}

#[cfg(test)]
mod tests {
    use crate::util::TempDir;

    fn tmpdir() -> TempDir { TempDir }

    #[test]
    fn walks() {
        let td = tmpdir();
        assert_eq!(td.path(), 1);
    }
}
""",
    )
    top = _path_targets(mock_ingestor, f"{base}.walk.top")
    assert top == {f"{base}.aaa.Ignore.path": cs.EdgeResolution.EXACT}, top
    walks = _path_targets(mock_ingestor, f"{base}.walk.tests.walks")
    assert walks == {f"{base}.util.TempDir.path": cs.EdgeResolution.EXACT}, walks


def test_self_and_super_paths_count_from_the_inline_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `self::` names the inline module the call is written in, and each
    # `super::` one module up from it, not from the file module, whose own
    # `make` returns the decoy.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_selfsuper",
        """\
use crate::aaa::Ignore;

fn make() -> Ignore { Ignore }

pub mod outer {
    pub struct Maker;
    impl Maker {
        pub fn path(&self) -> u8 { 2 }
    }
    pub fn make() -> Maker { Maker }

    pub fn here() -> u8 {
        let m = self::make();
        m.path()
    }

    pub mod inner {
        pub fn up() -> u8 {
            let m = super::make();
            m.path()
        }

        pub mod deeper {
            pub fn two_up() -> u8 {
                let m = super::super::make();
                m.path()
            }
        }
    }
}
""",
    )
    maker_path = {f"{base}.walk.outer.Maker.path": cs.EdgeResolution.EXACT}
    for caller in ("here", "inner.up", "inner.deeper.two_up"):
        targets = _path_targets(mock_ingestor, f"{base}.walk.outer.{caller}")
        assert targets == maker_path, (caller, targets)


def test_helper_return_type_is_read_in_the_helpers_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The caller has its own `TempDir` in scope; `tmpdir` returns util's.
    # The return type names the type where `tmpdir` is written.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_owner",
        """\
use crate::util;
use crate::util::tmpdir;

mod other {
    pub struct TempDir;
    impl TempDir {
        pub fn path(&self) -> u8 { 3 }
    }
}

use other::TempDir;

pub fn qualified() -> u8 {
    let _own: Option<TempDir> = None;
    let td = util::tmpdir();
    td.path()
}

pub fn imported() -> u8 {
    let td = tmpdir();
    td.path()
}
""",
    )
    temp_path = {f"{base}.util.TempDir.path": cs.EdgeResolution.EXACT}
    for caller in ("qualified", "imported"):
        targets = _path_targets(mock_ingestor, f"{base}.walk.{caller}")
        assert targets == temp_path, (caller, targets)


def test_inner_module_reexport_shadows_an_outer_same_named_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Inside `tests`, `util` is its own child module, which re-exports the
    # crate's `tmpdir`; the file's `util::tmpdir` returning `Wrong` is
    # shadowed there.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_reexport",
        """\
mod util {
    pub struct Wrong;
    impl Wrong {
        pub fn path(&self) -> u8 { 4 }
    }
    pub fn tmpdir() -> Wrong { Wrong }
}

pub fn top() -> u8 {
    let td = util::tmpdir();
    td.path()
}

mod tests {
    mod util {
        pub use crate::util::tmpdir;
    }

    pub fn walks() -> u8 {
        let td = util::tmpdir();
        td.path()
    }
}
""",
    )
    top = _path_targets(mock_ingestor, f"{base}.walk.top")
    assert top == {f"{base}.walk.util.Wrong.path": cs.EdgeResolution.EXACT}, top
    walks = _path_targets(mock_ingestor, f"{base}.walk.tests.walks")
    assert walks == {f"{base}.util.TempDir.path": cs.EdgeResolution.EXACT}, walks


def test_uses_in_bodies_and_blocks_shadow_module_level_bindings(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Each caller reaches util's `tmpdir` through a `use` in a fn body or
    # block around the call, which shadows the file's own `tmpdir` and its
    # `util` alias for the decoy module: the caller's own body, an inner
    # block of it, an enclosing fn's body, a const initializer around the
    # fn, and an aliased body `use` under an inline module's alias.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_bodyuse",
        """\
use crate::aaa::Ignore;

mod wrong {
    pub fn tmpdir() -> crate::aaa::Ignore { crate::aaa::Ignore }
}

use self::wrong as util;

fn tmpdir() -> Ignore { Ignore }

pub fn bare() -> u8 {
    use crate::util::tmpdir;
    let td = tmpdir();
    td.path()
}

pub fn qualified() -> u8 {
    use crate::util;
    let td = util::tmpdir();
    td.path()
}

pub fn inner_block() -> u8 {
    {
        use crate::util::tmpdir;
        let td = tmpdir();
        td.path()
    }
}

pub fn outer() -> u8 {
    use crate::util;
    fn nested() -> u8 {
        let td = util::tmpdir();
        td.path()
    }
    nested()
}

pub const IN_CONST: fn() -> u8 = {
    use crate::util;
    fn in_const() -> u8 {
        let td = util::tmpdir();
        td.path()
    }
    in_const
};

pub mod tests {
    use super::wrong as util;

    pub fn aliased() -> u8 {
        use crate::util as util;
        let td = util::tmpdir();
        td.path()
    }
}
""",
    )
    temp_path = {f"{base}.util.TempDir.path": cs.EdgeResolution.EXACT}
    for caller in (
        "bare",
        "qualified",
        "inner_block",
        "nested",
        "in_const",
        "tests.aliased",
    ):
        targets = _path_targets(mock_ingestor, f"{base}.walk.{caller}")
        assert targets == temp_path, (caller, targets)


def test_nested_function_looks_names_up_in_its_enclosing_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A fn declared in another fn's body is still in `tests`: `self::`, a
    # bare name and a child module count from there, and `super::` from the
    # file, whose own `make` returns the decoy.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_nested",
        """\
use crate::aaa::Ignore;

fn make() -> Ignore { Ignore }

pub mod tests {
    pub struct Maker;
    impl Maker {
        pub fn path(&self) -> u8 { 2 }
    }
    pub fn make() -> Maker { Maker }

    pub mod inner {
        pub fn make() -> super::Maker { super::Maker }
    }

    pub fn outer() -> u8 {
        fn via_self() -> u8 {
            let m = self::make();
            m.path()
        }
        fn bare() -> u8 {
            let m = make();
            m.path()
        }
        fn child() -> u8 {
            let m = inner::make();
            m.path()
        }
        fn via_super() -> u8 {
            let m = super::make();
            m.path()
        }
        via_self() + bare() + child() + via_super()
    }
}
""",
    )
    maker_path = {f"{base}.walk.tests.Maker.path": cs.EdgeResolution.EXACT}
    for caller in ("via_self", "bare", "child"):
        targets = _path_targets(mock_ingestor, f"{base}.walk.tests.{caller}")
        assert targets == maker_path, (caller, targets)
    targets = _path_targets(mock_ingestor, f"{base}.walk.tests.via_super")
    assert targets == {f"{base}.aaa.Ignore.path": cs.EdgeResolution.EXACT}, targets


def test_inner_helper_with_no_return_type_still_shadows_an_outer_one(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The test module's own `tmpdir` returns `()`, so it records no return
    # type; the file's `tmpdir` returning the decoy must not stand in for
    # it, whether the call names it bare or through a module path.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_unit",
        """\
use crate::aaa::Ignore;

fn tmpdir() -> Ignore { Ignore }

mod here {
    pub fn tmpdir() -> crate::aaa::Ignore { crate::aaa::Ignore }
}

pub trait Pathy {
    fn path(&self) -> u8;
}

impl Pathy for () {
    fn path(&self) -> u8 { 5 }
}

pub mod tests {
    use super::Pathy;

    fn tmpdir() {}

    mod here {
        pub fn tmpdir() {}
    }

    pub fn bare() -> u8 {
        let td = tmpdir();
        td.path()
    }

    pub fn qualified() -> u8 {
        let td = here::tmpdir();
        td.path()
    }
}
""",
    )
    decoy = f"{base}.aaa.Ignore.path"
    for caller in ("bare", "qualified"):
        targets = _path_targets(mock_ingestor, f"{base}.walk.tests.{caller}")
        assert targets.get(decoy) != cs.EdgeResolution.EXACT, (caller, targets)


def test_external_module_call_leaves_the_local_untyped(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `tempfile::tempdir()` names no first-party module: nothing may type
    # `td` as the project's own TempDir.
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_ext",
        """\
pub fn external() -> u8 {
    let td = tempfile::tempdir();
    td.path()
}
""",
    )
    targets = _path_targets(mock_ingestor, f"{base}.walk.external")
    assert f"{base}.util.TempDir.path" not in targets, targets


# A `use` of a toolchain crate's fn names no project fn, even when the project
# has one of the same name: matching it by name typed the local as the
# project's TempDir (Greptile, PR #3010).
_SHADOWED_STD_USE = {
    "body": """\
use crate::util::TempDir;

pub fn vars() -> TempDir { TempDir }

pub fn external() -> u8 {
    use std::env::vars;
    let td = vars();
    td.path()
}
""",
    "module": """\
mod helpers {
    pub fn vars() -> crate::util::TempDir { crate::util::TempDir }
}

use std::env::vars;

pub fn external() -> u8 {
    let td = vars();
    td.path()
}
""",
}


def test_a_std_use_never_takes_a_same_named_project_fns_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    for scope, walk_rs in _SHADOWED_STD_USE.items():
        mock_ingestor.reset_mock()
        base = _index(temp_repo, mock_ingestor, f"rs_modfn_std_{scope}", walk_rs)
        targets = _path_targets(mock_ingestor, f"{base}.walk.external")
        assert f"{base}.util.TempDir.path" not in targets, (scope, targets)


def test_every_use_of_a_block_reaches_its_nested_fn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Each `use` of an initializer block is its own entry: keeping only the
    # last one per block lost `use crate::util;`, and the nested fn's
    # `util::tmpdir()` fell to the file's `util` alias for the decoy module
    # (Greptile, PR #3010).
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_blockuses",
        """\
mod wrong {
    pub fn tmpdir() -> crate::aaa::Ignore { crate::aaa::Ignore }
}

use self::wrong as util;

pub const IN_CONST: fn() -> u8 = {
    use crate::util;
    use crate::aaa;
    fn in_const() -> u8 {
        let td = util::tmpdir();
        td.path()
    }
    in_const
};
""",
    )
    targets = _path_targets(mock_ingestor, f"{base}.walk.in_const")
    assert targets == {f"{base}.util.TempDir.path": cs.EdgeResolution.EXACT}, targets


def test_a_helper_s_outside_crate_return_type_is_not_the_caller_s(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `helpers::open()` returns the `File` its module imports from std; the
    # caller's own `File` shares the name only, so `f` is untyped and
    # `f.path()` is no exact call to it (CodeRabbit, PR #3010).
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_modfn_extret",
        """\
mod helpers {
    use std::fs::File;
    pub fn open() -> File { unimplemented!() }
}

pub struct File;
impl File {
    pub fn path(&self) -> u8 { 2 }
}

pub fn external() -> u8 {
    let f = helpers::open();
    f.path()
}
""",
    )
    targets = _path_targets(mock_ingestor, f"{base}.walk.external")
    assert targets.get(f"{base}.walk.File.path") != cs.EdgeResolution.EXACT, targets
