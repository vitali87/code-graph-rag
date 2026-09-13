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
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
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
    assert {t for s, t in edges if s.endswith("app.first")} == {
        "proj.engine.Widget.render"
    }
    assert {t for s, t in edges if s.endswith("app.second")} == {
        "proj.engine.Banner.render"
    }


_TWO = (
    "from .engine import Widget, Banner\n"
    "\n"
    "def fw() -> tuple[int, Widget]:\n    return (0, Widget())\n"
    "\n"
    "def fb() -> tuple[int, Banner]:\n    return (0, Banner())\n"
    "\n"
)
_TWO_CLASSES = (
    "class Widget:\n    def render(self) -> int:\n        return 1\n\n"
    "class Banner:\n    def render(self) -> int:\n        return 2\n"
)


def _render_targets(tmp_path: Path, body: str) -> dict[str, set[str]]:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_TWO_CLASSES)
    (repo / "app.py").write_text(_TWO + body)
    out: dict[str, set[str]] = {}
    for src, tgt in _edges(repo, method="render"):
        out.setdefault(src.rsplit(".", 1)[-1], set()).add(tgt.rsplit(".", 2)[-2])
    return out


def test_the_nearest_preceding_binding_wins(tmp_path: Path) -> None:
    """`p = fw(); p = fb(); _n, w = p` unpacks fb's result (local review P1:
    the first binding was taken). A reassignment AFTER the use is ignored."""
    got = _render_targets(
        tmp_path,
        "def nearest() -> int:\n"
        "    p = fw()\n    p = fb()\n    _n, w = p\n    return w.render()\n"
        "\n"
        "def later() -> int:\n"
        "    p = fw()\n    _n, w = p\n    p = fb()\n    return w.render()\n",
    )
    assert got.get("nearest") == {"Banner"}, got
    assert got.get("later") == {"Widget"}, got


def _local_types(tmp_path: Path, body: str, function: str) -> dict[str, str]:
    """The engine's local type map for one function, read directly: what the
    fallback does with an UNBOUND name is its own business, so a test about
    binding asserts on the map, not on the edge the fallback then picks."""
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_TWO_CLASSES)
    (repo / "app.py").write_text(_TWO + body)
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=_StatefulIngestor(), repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run(force=True)
    tree = parsers[cs.SupportedLanguage.PYTHON].parse((_TWO + body).encode())
    node = next(
        n
        for n in tree.root_node.children
        if n.type == "function_definition"
        and (n.child_by_field_name("name").text or b"").decode() == function
    )
    engine = updater.factory.type_inference.python_type_inference
    return engine.build_local_variable_type_map(node, "proj.app")


def test_a_count_mismatch_binds_nothing(tmp_path: Path) -> None:
    """`a, b = three()` cannot be positional; a guess (`b` as the second
    element) would be worse than leaving it unbound."""
    types = _local_types(
        tmp_path,
        "def three() -> tuple[int, Widget, Banner]:\n"
        "    return (0, Widget(), Banner())\n"
        "\n"
        "def mismatch() -> int:\n    a, b = three()\n    return b.render()\n",
        "mismatch",
    )
    assert "a" not in types and "b" not in types, types


def test_optional_and_nested_generics_are_read_through(tmp_path: Path) -> None:
    """`Optional[tuple[int, Banner]]` strips to the tuple; a nested generic
    with its own commas (`dict[str, list[Widget]]`) is ONE position. Read
    from the type map: an edge would not tell a bound `Banner` from the
    fallback happening to pick it."""
    body = (
        "from typing import Optional\n"
        "\n"
        "def opt() -> Optional[tuple[int, Banner]]:\n    return None\n"
        "\n"
        "def nested() -> tuple[int, dict[str, list[Widget]], Banner]:\n"
        "    return (0, {}, Banner())\n"
        "\n"
        "def use_opt() -> int:\n    _n, b = opt()\n    return b.render()\n"
        "\n"
        "def use_nested() -> int:\n    _n, d, b = nested()\n    return b.render()\n"
    )
    assert _local_types(tmp_path / "opt", body, "use_opt").get("b") == "Banner"
    nested = _local_types(tmp_path / "nested", body, "use_nested")
    assert nested.get("b") == "Banner", nested
    assert nested.get("d") == "dict[str, list[Widget]]", nested


def test_an_existing_binding_is_not_overwritten(tmp_path: Path) -> None:
    """A name the earlier passes already typed keeps that type, the same
    first-wins rule `_process_assignment_complex` applies."""
    got = _render_targets(
        tmp_path,
        "def keep() -> int:\n"
        "    w = Widget()\n    _n, w = fb()\n    return w.render()\n",
    )
    assert got.get("keep") == {"Widget"}, got


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
