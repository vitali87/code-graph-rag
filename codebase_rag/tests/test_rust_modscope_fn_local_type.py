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
