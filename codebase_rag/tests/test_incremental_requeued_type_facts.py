"""An incremental sync leaves unchanged definitions' type edges as they are.

The annotations of every unchanged definition are requeued from the graph
and resolved again, but that file's imports are not loaded on such a run.
A name its import bound (`from matcher.core import Match`) then fell back to
the nearest same-named type (`printer.jsont.Match`), and the sync added a
wrong RETURNS / ACCEPTS edge beside the right one, even when it only added
an unrelated file (issue #3007).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_TYPE_RELS = {cs.RelationshipType.RETURNS.value, cs.RelationshipType.ACCEPTS.value}

_Edges = set[tuple[str, str]]

_PYTHON = {
    "matcher/__init__.py": "",
    "matcher/core.py": "class Match:\n    start = 0\n",
    "printer/__init__.py": "",
    "printer/jsont.py": 'class Match:\n    text = b""\n',
    "printer/util.py": (
        "from matcher.core import Match\n\n\n"
        "def trim(line: Match) -> Match:\n    return line\n\n\n"
        "def pair(line: Match, later: Later) -> None:\n    return None\n"
    ),
}
_TYPESCRIPT = {
    "src/matcher/core.ts": "export class Match { start = 0; }\n",
    "src/printer/jsont.ts": 'export class Match { text = ""; }\n',
    "src/printer/util.ts": (
        'import { Match } from "../matcher/core";\n\n'
        "export function trim(line: Match): Match {\n  return line;\n}\n"
    ),
}
_RUST = {
    "Cargo.toml": '[package]\nname = "rsacc"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": "pub mod matcher;\npub mod printer;\n",
    "src/matcher.rs": "pub struct Match;\n",
    "src/printer/mod.rs": "pub mod jsont;\npub mod util;\n",
    "src/printer/jsont.rs": "pub struct Match;\n",
    "src/printer/util.rs": (
        "use crate::matcher::Match;\n\npub fn trim(line: Match) -> Match {\n    line\n}\n"
    ),
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _sync(root: Path, store: _StatefulIngestor, force: bool) -> _Edges:
    parsers, queries = load_parsers()
    GraphUpdater(ingestor=store, repo_path=root, parsers=parsers, queries=queries).run(
        force=force
    )
    return {
        (f"{src.split('.', 1)[1]} {rel}", str(dst).split(".", 1)[1])
        for _, src, rel, _, dst in store.edges
        if rel in _TYPE_RELS
    }


def _fresh(root: Path, files: dict[str, str]) -> _Edges:
    _write(root, files)
    return _sync(root, _StatefulIngestor(), force=True)


@pytest.mark.parametrize(
    ("files", "extra", "grammar"),
    [
        (_PYTHON, ("printer/other.py", "def unrelated():\n    return 1\n"), "python"),
        (
            _TYPESCRIPT,
            ("src/printer/other.ts", "export function unrelated() {}\n"),
            "typescript",
        ),
        (_RUST, ("src/zz_unrelated.rs", ""), "rust"),
    ],
    ids=["python", "typescript", "rust"],
)
def test_adding_an_unrelated_file_leaves_type_edges_as_fresh(
    tmp_path: Path, files: dict[str, str], extra: tuple[str, str], grammar: str
) -> None:
    if grammar not in load_parsers()[0]:
        pytest.skip(f"{grammar} parser not available")
    root = tmp_path / "repo"
    _write(root, files)
    store = _StatefulIngestor()
    fresh = _sync(root, store, force=True)
    assert any("jsont" not in dst and "trim" in src for src, dst in fresh), fresh

    _write(root, dict([extra]))
    after = _sync(root, store, force=False)

    assert {e for e in after if "trim" in e[0]} == {
        e for e in fresh if "trim" in e[0]
    }, after
    assert not [e for e in after if "jsont" in e[1]], after


def test_an_unchanged_annotation_still_takes_a_type_a_new_file_adds(
    tmp_path: Path,
) -> None:
    # Negative (issue #1527 kept): `pair(..., later: Later)` names a type no
    # file defines yet, and adding it binds the new edge on an incremental run.
    root = tmp_path / "repo"
    _write(root, _PYTHON)
    store = _StatefulIngestor()
    _sync(root, store, force=True)

    _write(root, {"printer/later.py": "class Later:\n    pass\n"})
    after = _sync(root, store, force=False)

    accepts = {dst for src, dst in after if src == "printer.util.pair ACCEPTS"}
    assert "printer.later.Later" in accepts, after


def test_a_partly_bound_annotation_keeps_its_bound_name(tmp_path: Path) -> None:
    # The same run: the imported `Match` beside the new `Later` keeps its one
    # edge instead of gaining the nearer `printer.jsont.Match`.
    root = tmp_path / "repo"
    _write(root, _PYTHON)
    store = _StatefulIngestor()
    _sync(root, store, force=True)

    _write(root, {"printer/later.py": "class Later:\n    pass\n"})
    after = _sync(root, store, force=False)

    accepts = {dst for src, dst in after if src == "printer.util.pair ACCEPTS"}
    assert accepts == {"matcher.core.Match", "printer.later.Later"}, after


def test_a_qualified_name_is_not_bound_by_a_same_named_target(
    tmp_path: Path,
) -> None:
    # `route` accepts `crate::matcher::Match` and returns `crate::later::Match`,
    # which no file defines yet. The ACCEPTS edge to `matcher::Match` must not
    # count the other path as bound: adding `later.rs` gives `route` the
    # RETURNS edge a fresh index of the same files has.
    if "rust" not in load_parsers()[0]:
        pytest.skip("rust parser not available")
    files = {
        **_RUST,
        "src/printer/util.rs": (
            "pub fn route(m: crate::matcher::Match) -> crate::later::Match {\n"
            "    todo!()\n}\n"
        ),
    }
    root = tmp_path / "repo"
    _write(root, files)
    store = _StatefulIngestor()
    _sync(root, store, force=True)

    added = {
        "src/lib.rs": "pub mod later;\npub mod matcher;\npub mod printer;\n",
        "src/later.rs": "pub struct Match;\n",
    }
    _write(root, added)
    after = _sync(root, store, force=False)

    fresh_root = tmp_path / "fresh"
    _write(fresh_root, {**files, **added})
    fresh = _sync(fresh_root, _StatefulIngestor(), force=True)
    assert ("src.printer.util.route RETURNS", "src.later.Match") in fresh, fresh
    assert {e for e in after if "route" in e[0]} == {
        e for e in fresh if "route" in e[0]
    }, after


_ALIASED = {
    "matcher/__init__.py": "",
    "matcher/core.py": "class Match:\n    start = 0\n",
    "printer/__init__.py": "",
    "printer/jsont.py": 'class Bar:\n    text = b""\n',
    "printer/util.py": (
        "from matcher.core import Match as Bar\n\n\n"
        "def trim(line: Bar) -> Bar:\n    return line\n"
    ),
}


def test_an_aliased_import_does_not_gain_a_same_named_type(tmp_path: Path) -> None:
    # `from matcher.core import Match as Bar`: the edge reaches a target the
    # written `Bar` does not spell, so the requeue resolves `Bar` again, and
    # without the file's imports reached printer.jsont's own `Bar` (#3007
    # review). The requeue loads them, so the sync leaves the edges as a
    # fresh index has them.
    root = tmp_path / "repo"
    _write(root, _ALIASED)
    store = _StatefulIngestor()
    fresh = _sync(root, store, force=True)
    assert ("printer.util.trim RETURNS", "matcher.core.Match") in fresh, fresh

    _write(root, {"printer/other.py": "def unrelated():\n    return 1\n"})
    after = _sync(root, store, force=False)

    assert {e for e in after if "trim" in e[0]} == {
        e for e in fresh if "trim" in e[0]
    }, after


def test_an_aliased_import_does_not_gain_a_type_the_sync_adds(
    tmp_path: Path,
) -> None:
    # The same alias, with printer.jsont's `Bar` added by the sync itself: no
    # unchanged file's type, so only the importing file's own imports tell
    # the requeue that `Bar` is matcher.core's `Match` (#3007 review).
    files = {k: v for k, v in _ALIASED.items() if k != "printer/jsont.py"}
    root = tmp_path / "repo"
    _write(root, files)
    store = _StatefulIngestor()
    _sync(root, store, force=True)

    _write(root, {"printer/jsont.py": _ALIASED["printer/jsont.py"]})
    after = _sync(root, store, force=False)

    fresh = _fresh(tmp_path / "fresh", _ALIASED)
    assert ("printer.util.trim RETURNS", "matcher.core.Match") in fresh, fresh
    assert after == fresh, after


def test_deleting_one_of_two_same_named_types_binds_the_other(
    tmp_path: Path,
) -> None:
    # `f(x: Foo)` imports no `Foo`, and two packages define one, equally near,
    # so neither gets the edge. Deleting one leaves a unique `Foo`, which a
    # fresh index binds; the unchanged annotation must bind it too, though
    # the type it now reaches is in a file the sync left alone (#3007 review).
    files = {
        "alpha/__init__.py": "",
        "alpha/models.py": "class Foo:\n    pass\n",
        "beta/__init__.py": "",
        "beta/models.py": "class Foo:\n    pass\n",
        "app/__init__.py": "",
        "app/use.py": "def f(x: Foo) -> Foo:\n    return x\n",
    }
    root = tmp_path / "repo"
    _write(root, files)
    store = _StatefulIngestor()
    assert not _sync(root, store, force=True)

    (root / "beta/models.py").unlink()
    after = _sync(root, store, force=False)

    del files["beta/models.py"]
    fresh = _fresh(tmp_path / "fresh", files)
    assert ("app.use.f ACCEPTS", "alpha.models.Foo") in fresh, fresh
    assert after == fresh, after


def test_deleting_the_imported_type_falls_back_as_fresh(tmp_path: Path) -> None:
    # `from matcher.core import Match` stays while matcher/core.py goes: a
    # fresh index falls through the dead import to printer.jsont's `Match`,
    # an unchanged file's type, and the sync must too (#3007 review).
    root = tmp_path / "repo"
    _write(root, _PYTHON)
    store = _StatefulIngestor()
    _sync(root, store, force=True)

    (root / "matcher/core.py").unlink()
    after = _sync(root, store, force=False)

    files = {k: v for k, v in _PYTHON.items() if k != "matcher/core.py"}
    fresh = _fresh(tmp_path / "fresh", files)
    assert ("printer.util.trim RETURNS", "printer.jsont.Match") in fresh, fresh
    assert after == fresh, after
