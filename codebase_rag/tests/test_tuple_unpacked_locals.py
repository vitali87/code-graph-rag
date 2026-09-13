"""A tuple-unpacked local takes its element's type from the right-hand side.

Issue #1896. `_ol, _oc, inner = parsed` bound nothing: the assignment walk
typed only a single-name target, so `inner` had no type, `inner.base_dir` had
none either, and a call on it fell to the bare-name fallback -- the real shape
of `codebase_rag/trace/sourcemap.py:225`, which #1898 could only pin as a
strict expected failure.

Every defect test pairs with a control whose decoy method is renamed, so a
green result means the edge was suppressed rather than the harness seeing
nothing; a positive pins that the element type is USED, not merely absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_DECOY_COLLIDES = "class Engine:\n    def resolve(self) -> int:\n        return 1\n"
_DECOY_CONTROL = "class Engine:\n    def compute(self) -> int:\n        return 1\n"

_HEAD = (
    "from dataclasses import dataclass\n"
    "from pathlib import Path\n"
    "\n"
    "@dataclass\n"
    "class SourceMap:\n"
    "    sources: list[str]\n"
    "    base_dir: Path\n"
    "\n"
)

_SHAPES = {
    # sourcemap.py:225 as written: unpack a call annotated with a tuple.
    "unpack_annotated_call": (
        "def _parse_section(raw: object) -> tuple[int, int, SourceMap] | None:\n"
        "    return None\n"
        "\n"
        "def from_sections(raw: object) -> str:\n"
        "    parsed = _parse_section(raw)\n"
        "    if parsed is None:\n"
        "        return ''\n"
        "    _ol, _oc, inner = parsed\n"
        "    return (inner.base_dir / 'x').resolve().as_posix()\n"
    ),
    # The intermediate variable elided: unpack the call directly.
    "unpack_call_directly": (
        "def _parse_section(raw: object) -> tuple[int, int, SourceMap]:\n"
        "    return (0, 0, SourceMap([], Path('.')))\n"
        "\n"
        "def from_sections(raw: object) -> str:\n"
        "    _ol, _oc, inner = _parse_section(raw)\n"
        "    return (inner.base_dir / 'x').resolve().as_posix()\n"
    ),
    # Parenthesised target list.
    "unpack_tuple_pattern": (
        "def _parse_section(raw: object) -> tuple[int, SourceMap]:\n"
        "    return (0, SourceMap([], Path('.')))\n"
        "\n"
        "def from_sections(raw: object) -> str:\n"
        "    (_ol, inner) = _parse_section(raw)\n"
        "    return (inner.base_dir / 'x').resolve().as_posix()\n"
    ),
}


def _edges(repo: Path, *, method: str) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store, repo_path=repo, parsers=parsers, queries=queries
    ).run(force=True)
    return {
        (str(src), str(tgt))
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value and str(tgt).endswith(method)
    }


def _build(tmp_path: Path, body: str, decoy: str) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(decoy)
    (repo / "app.py").write_text(_HEAD + body)
    return repo


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_an_unpacked_local_does_not_call_every_same_named_method(
    tmp_path: Path, shape: str
) -> None:
    repo = _build(tmp_path, _SHAPES[shape], _DECOY_COLLIDES)

    assert not _edges(repo, method="Engine.resolve"), (
        f"{shape}: `inner` is a SourceMap whose base_dir is a Path, but "
        f".resolve() was linked to Engine.resolve, which nothing calls"
    )


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_the_control_cannot_produce_the_edge_it_is_checking_for(
    tmp_path: Path, shape: str
) -> None:
    repo = _build(tmp_path, _SHAPES[shape], _DECOY_CONTROL)

    assert not _edges(repo, method="Engine.resolve")


def test_the_element_type_is_used_not_merely_absent(tmp_path: Path) -> None:
    """The positive: a method on the unpacked element's class resolves to
    that class. Absence of a false edge alone would also be satisfied by an
    element left untyped whose call happens to match nothing."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(
        "class Widget:\n    def render(self) -> int:\n        return 1\n\n"
        "class Other:\n    def render(self) -> int:\n        return 2\n"
    )
    (repo / "app.py").write_text(
        "from .engine import Widget\n"
        "\n"
        "def _pair() -> tuple[int, Widget]:\n"
        "    return (0, Widget())\n"
        "\n"
        "def run() -> int:\n"
        "    _n, widget = _pair()\n"
        "    return widget.render()\n"
    )
    calls = {t for _s, t in _edges(repo, method="render")}
    assert any(t.endswith("engine.Widget.render") for t in calls), calls
    assert not any(t.endswith("engine.Other.render") for t in calls), calls


def test_a_heterogeneous_tuple_binds_each_position(tmp_path: Path) -> None:
    """Two element classes with the same method name: each target gets its
    own position's class, not the first, not both."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(
        "class Widget:\n    def render(self) -> int:\n        return 1\n\n"
        "class Banner:\n    def render(self) -> int:\n        return 2\n"
    )
    (repo / "app.py").write_text(
        "from .engine import Widget, Banner\n"
        "\n"
        "def _pair() -> tuple[Widget, Banner]:\n"
        "    return (Widget(), Banner())\n"
        "\n"
        "def first() -> int:\n"
        "    widget, _banner = _pair()\n"
        "    return widget.render()\n"
        "\n"
        "def second() -> int:\n"
        "    _widget, banner = _pair()\n"
        "    return banner.render()\n"
    )
    edges = _edges(repo, method="render")
    assert {t for s, t in edges if s.endswith("app.first")} == {"proj.engine.Widget.render"}
    assert {t for s, t in edges if s.endswith("app.second")} == {"proj.engine.Banner.render"}


def test_the_fixture_can_go_red(tmp_path: Path) -> None:
    """Known-positive: unpacking from an UNANNOTATED call recovers nothing,
    so the bare-name fallback still fires and the harness observes the
    decoy edge."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_DECOY_COLLIDES)
    (repo / "app.py").write_text(
        "def _parse_section(raw):\n"
        "    return raw\n"
        "\n"
        "def from_sections(raw):\n"
        "    _ol, _oc, inner = _parse_section(raw)\n"
        "    return (inner.base_dir / 'x').resolve()\n"
    )

    assert _edges(repo, method="Engine.resolve"), (
        "the harness never observes an Engine.resolve edge, so the assertions "
        "that no such edge exists are not evidence of anything"
    )
