"""A Rust inline mod's node is buffered before any DEFINES edge from it.

Items inside `mod tests { ... }` buffered their `Module -[:DEFINES]->` edge
before the inline Module node itself, which came last. A batch flush in
between wrote the edge while its source did not exist yet, the row was
dropped, and the item sat outside every deletion walk (issue #3000). With a
batch size of 1 a relationship is written as soon as it is buffered, so the
order of the ingestor calls is exactly what decides whether it lands.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater

RSGHOST = """\
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

#[cfg(test)]
mod tests {
    use super::add;

    struct Fixture;

    fn helper() -> i32 {
        add(1, 2)
    }

    #[test]
    fn adds() {
        assert_eq!(helper(), 3);
    }

    mod deeper {
        fn nested() {}
    }
}
"""


def write_crate(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "rsghost"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (root / "src" / "lib.rs").write_text(RSGHOST, encoding="utf-8")


def _index_order(tmp_path: Path) -> tuple[dict[str, int], list[tuple[int, str, str]]]:
    root = tmp_path / "rsghost"
    write_crate(root)
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing="rust")
    module_at: dict[str, int] = {}
    defines: list[tuple[int, str, str]] = []
    for at, (name, args, _kwargs) in enumerate(mock.method_calls):
        if name == "ensure_node_batch" and str(args[0]) == cs.NodeLabel.MODULE:
            module_at.setdefault(str(args[1][cs.KEY_QUALIFIED_NAME]), at)
        elif (
            name == "ensure_relationship_batch"
            and str(args[1]) == cs.RelationshipType.DEFINES
            and str(args[0][0]) == cs.NodeLabel.MODULE
        ):
            defines.append((at, str(args[0][2]), str(args[2][2])))
    return module_at, defines


def test_every_inline_mod_child_follows_its_mod_node(tmp_path: Path) -> None:
    module_at, defines = _index_order(tmp_path)
    inline = {qn for qn in module_at if qn.endswith((".tests", ".tests.deeper"))}
    children = [(at, src, dst) for at, src, dst in defines if src in inline]
    assert {dst.rsplit(".", 1)[1] for _at, _src, dst in children} >= {
        "Fixture",
        "helper",
        "adds",
        "nested",
    }, children
    late = [(src, dst) for at, src, dst in children if at < module_at[src]]
    assert not late, late


def test_the_file_module_still_precedes_its_own_items(tmp_path: Path) -> None:
    # Negative: moving the inline mods first must not put the file Module's
    # own DEFINES (the inline mod, `add`) ahead of the file Module.
    module_at, defines = _index_order(tmp_path)
    file_qn = next(qn for qn in module_at if qn.endswith(".src.lib"))
    own = [(at, dst) for at, src, dst in defines if src == file_qn]
    assert {dst.rsplit(".", 1)[1] for _at, dst in own} >= {"add", "tests"}, own
    assert all(at > module_at[file_qn] for at, _dst in own), own
