"""A Rust workspace that lists its root package as `"."` indexes (#3167).

`members = ["."]` is how a root package joins its own workspace
(rust-lang/mdBook does it). The member patterns were expanded with
`Path.glob`, which on Python 3.12 raises `IndexError` for `"."` and
`AttributeError` for `"./"`, neither caught: the first external trait impl
(`impl Default`) aborted the whole sync before any CALLS edge was written.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_ROOT_LIB = """\
use member_a::helper;
use member_b::other;

pub struct W;
impl Default for W {
    fn default() -> Self { W }
}

pub fn run() -> u32 { helper() + other() }
"""


def _member(root: Path, rel: str, name: str, body: str) -> None:
    (root / rel / "src").mkdir(parents=True)
    (root / rel / "Cargo.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (root / rel / "src" / "lib.rs").write_text(body, encoding="utf-8")


def _calls(root: Path, members: str) -> set[tuple[str, str]]:
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "rsws"\nversion = "0.1.0"\nedition = "2021"\n\n'
        f"[workspace]\nmembers = {members}\n",
        encoding="utf-8",
    )
    (root / "src" / "lib.rs").write_text(_ROOT_LIB, encoding="utf-8")
    _member(root, "crates/a", "member_a", "pub fn helper() -> u32 { 1 }\n")
    _member(root, "crates/b", "member_b", "pub fn other() -> u32 { 2 }\n")
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing="rust")
    return {
        (
            str(c.args[0][2]).split(".", 1)[1],
            str(c.args[2][2]).split(".", 1)[1],
        )
        for c in get_relationships(mock, cs.RelationshipType.CALLS)
    }


@pytest.mark.parametrize(
    "members",
    [
        '["."]',
        '["./"]',
        '[".", "crates/*"]',
        '["./", "./crates/a", "crates/b/"]',
    ],
    ids=["dot", "dot-slash", "dot-and-glob", "dot-slash-prefixed-members"],
)
def test_a_root_member_does_not_abort_the_sync(tmp_path: Path, members: str) -> None:
    messages: list[str] = []
    sink = logger.add(messages.append, level="DEBUG", format="{message}")
    try:
        calls = _calls(tmp_path / "rsws", members)
    finally:
        logger.remove(sink)
    assert ("src.lib.run", "crates.a.src.lib.helper") in calls, calls
    # `.` and `./` name the root, so nothing is left unexpanded.
    assert not [m for m in messages if "could not be expanded" in m], messages


def test_the_other_members_still_map_to_their_crates(tmp_path: Path) -> None:
    # Negative: the `./`-prefixed and trailing-slash members are still
    # expanded, so both `use` heads reach their member crate.
    calls = _calls(tmp_path / "rsws", '["./", "./crates/a", "crates/b/"]')
    assert {
        ("src.lib.run", "crates.a.src.lib.helper"),
        ("src.lib.run", "crates.b.src.lib.other"),
    } <= (calls), calls


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (".", ""),
        ("./", ""),
        (".//", ""),
        ("./crates/a", "crates/a"),
        ("crates/b/", "crates/b"),
        ("crates/*", "crates/*"),
    ],
)
def test_a_member_pattern_is_read_relative_to_the_root(
    pattern: str, expected: str
) -> None:
    from codebase_rag.parsers.import_processor import _rust_member_pattern

    assert _rust_member_pattern(pattern) == expected


def test_a_member_the_expansion_cannot_handle_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Whatever a future Python raises for a pattern costs that member only:
    # the sync completes and the other member is still mapped.
    original = Path.glob

    def glob(self: Path, pattern: str) -> Iterator[Path]:
        if pattern == "crates/a":
            raise RuntimeError("unexpandable")
        return original(self, pattern)

    monkeypatch.setattr(Path, "glob", glob)
    calls = _calls(tmp_path / "rsws", '[".", "crates/a", "crates/b"]')
    assert ("src.lib.run", "crates.b.src.lib.other") in calls, calls
