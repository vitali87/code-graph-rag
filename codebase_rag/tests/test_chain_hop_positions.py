"""Each hop of a method chain is recorded where its name is written.

A member call's tree-sitter node spans its receiver, so every hop of
`new TB().Put("a").Other("b").Put("c").Build()` took the chain's start as
its site: callers showed line 9 for a call on line 11, and the two `Put`
hops shared one (line, col) and merged into one CALLS edge (issue #3166).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_CSHARP = """\
class TB {
    public TB Put(string s) { return this; }
    public TB Other(string s) { return this; }
    public int Build() { return 1; }
}

class Top {
    static int Make() {
        return new TB()
            .Put("a")
            .Other("b")
            .Put("c")
            .Build();
    }

    static int Bare() {
        return Helper(
            1);
    }

    static int Helper(int n) { return n; }
}
"""
_JAVA = """\
class TB {
    TB put(String s) { return this; }
    TB other(String s) { return this; }
    int build() { return 1; }
}

class Top {
    int make() {
        return new TB()
            .put("a")
            .other("b")
            .put("c")
            .build();
    }
}
"""
_PYTHON = """\
class TB:
    def put(self, s: str) -> "TB":
        return self


def make() -> "TB":
    return (
        TB()
        .put("a")
    )
"""
_TYPESCRIPT = """\
class TB {
  put(s: string): TB { return this; }
}

export function make(): TB {
  return new TB()
    .put("a");
}
"""
_RUST = """\
pub struct TB;

impl TB {
    pub fn put(self, s: &str) -> TB { self }
}

pub fn make() -> TB {
    TB
        .put("a")
}
"""

_Sites = list[tuple[str, str, int, int]]


def _sites(root: Path, files: dict[str, str], grammar: str) -> _Sites:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing=grammar)
    out: _Sites = []
    for rel in (cs.RelationshipType.CALLS, cs.RelationshipType.INSTANTIATES):
        for c in get_relationships(mock, rel):
            props = c.kwargs.get("properties") or {}
            callee = str(c.args[2][2]).rsplit(".", 1)[1]
            out.append(
                (str(rel), callee, int(props[cs.KEY_LINE]), int(props[cs.KEY_COL]))
            )
    return sorted(out)


@pytest.mark.parametrize(
    ("files", "grammar", "expected"),
    [
        (
            {"Chain.cs": _CSHARP},
            "c_sharp",
            [
                ("Put(string)", 10, 13),
                ("Other(string)", 11, 13),
                ("Put(string)", 12, 13),
                ("Build", 13, 13),
            ],
        ),
        (
            {"Top.java": _JAVA},
            "java",
            [
                ("put(String)", 10, 13),
                ("other(String)", 11, 13),
                ("put(String)", 12, 13),
                ("build()", 13, 13),
            ],
        ),
    ],
    ids=["csharp", "java"],
)
def test_every_hop_has_its_own_site(
    tmp_path: Path, files: dict[str, str], grammar: str, expected: list
) -> None:
    sites = _sites(tmp_path / grammar, files, grammar)
    calls = sorted(
        (callee, line, col)
        for rel, callee, line, col in sites
        if rel == cs.RelationshipType.CALLS and callee != "Helper(int)"
    )
    assert calls == sorted(expected), sites


@pytest.mark.parametrize(
    ("files", "grammar", "site"),
    [
        ({"chain.py": _PYTHON}, "python", ("put", 9, 9)),
        ({"chain.ts": _TYPESCRIPT}, "typescript", ("put", 7, 5)),
        (
            {
                "Cargo.toml": '[package]\nname = "ch"\nversion = "0.1.0"\n',
                "src/lib.rs": _RUST,
            },
            "rust",
            ("put", 9, 9),
        ),
    ],
    ids=["python", "typescript", "rust"],
)
def test_a_hop_on_its_own_line_is_sited_there(
    tmp_path: Path, files: dict[str, str], grammar: str, site: tuple[str, int, int]
) -> None:
    sites = _sites(tmp_path / grammar, files, grammar)
    calls = [(c, line, col) for rel, c, line, col in sites if rel == "CALLS"]
    assert site in calls, sites


def test_bare_calls_and_constructions_keep_their_start(tmp_path: Path) -> None:
    # Negatives: a bare call has no receiver, so its site still starts at its
    # name, and `new TB()` still starts at `new`.
    sites = _sites(tmp_path / "cs", {"Chain.cs": _CSHARP}, "c_sharp")
    assert ("CALLS", "Helper(int)", 17, 15) in sites, sites
    assert any(
        rel == "INSTANTIATES" and (line, col) == (9, 15) for rel, _c, line, col in sites
    ), sites
