"""A receiver written inline at the call site follows the same rules as one
assigned to a variable.

Issue #1893. #1870 typed a variable from the expression it was assigned
(`r = base / "sub"; r.resolve()`), but a receiver that never becomes a variable
-- `(self.base / self.srcs[i]).resolve()` -- reached call resolution as a bare
string, matched nothing, and fell back to the bare method name, emitting a
CALLS edge to every class defining `resolve`. Every shape here is one from
`codebase_rag/trace/sourcemap.py`, where five unrelated frame resolvers
define `resolve` -- except two, pinned below as strict expected failures with
their real causes, which this change does not reach.

Every defect test is paired with a control whose decoy method is renamed, so
a green result means the edge was suppressed rather than the harness seeing
nothing; a known-positive pins that the harness can observe the decoy edge.
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
    "from pathlib import Path\n"
    "\n"
    "class Mapper:\n"
    "    def __init__(self, base: Path, sources: list[str]) -> None:\n"
    "        self.base = base\n"
    "        self.sources = sources\n"
    "\n"
)

# Each body is a method on Mapper whose receiver is an inline expression.
_BODIES = {
    # sourcemap.py:143
    "paren_div_subscript": (
        "    def at(self, i: int) -> str:\n"
        "        return (self.base / self.sources[i]).resolve().as_posix()\n"
    ),
    "paren_div_name": (
        "    def joined(self, source: str) -> str:\n"
        "        return (self.base / source).resolve().as_posix()\n"
    ),
    "paren_or": (
        "    def pick(self, override: Path | None) -> str:\n"
        "        return (override or self.base).resolve().as_posix()\n"
    ),
}


# sourcemap.py:225, as a whole module: `inner` is a TUPLE-UNPACKED local
# (`_ol, _oc, inner = parsed`, from a call annotated
# `-> tuple[int, int, SourceMap] | None`). Tuple unpacking from an annotated
# return is never typed, so `inner.base_dir` has no type and the fallback
# still fires (#1896). Pinned strict so it flips when unpacking is typed.
_TUPLE_UNPACKED_SRC = (
    "from dataclasses import dataclass\n"
    "from pathlib import Path\n"
    "\n"
    "@dataclass\n"
    "class SourceMap:\n"
    "    sources: list[str]\n"
    "    base_dir: Path\n"
    "\n"
    "def _parse_section(raw: object) -> tuple[int, int, SourceMap] | None:\n"
    "    return None\n"
    "\n"
    "def from_sections(sections: list[object]) -> list[str]:\n"
    "    out: list[str] = []\n"
    "    for raw in sections:\n"
    "        parsed = _parse_section(raw)\n"
    "        if parsed is None:\n"
    "            return out\n"
    "        _ol, _oc, inner = parsed\n"
    "        for source in inner.sources:\n"
    "            out.append((inner.base_dir / source).resolve().as_posix())\n"
    "    return out\n"
)

# sourcemap.py:285. The left operand is an ATTRIBUTE of an external class
# (`js_path.parent` on a `Path`), and nothing in the graph knows what a Path's
# attributes are, so no honest rule can type it; the assignment path has the
# same gap. Pinned strict so the day stub knowledge lands, this flips and the
# shape joins _BODIES rather than staying an undocumented limitation.
_EXTERNAL_ATTRIBUTE_BODY = (
    "    def sibling(self, js_path: Path, relative: str) -> Path:\n"
    "        return (js_path.parent / relative).resolve()\n"
)


def _decoy_resolve_edges(repo: Path) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    return {
        (str(src), str(tgt))
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value and "Engine.resolve" in str(tgt)
    }


def _build(tmp_path: Path, body: str, decoy: str) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(decoy)
    (repo / "app.py").write_text(_HEAD + body)
    return repo


@pytest.mark.parametrize("shape", sorted(_BODIES))
def test_an_inline_receiver_does_not_call_every_same_named_method(
    tmp_path: Path, shape: str
) -> None:
    repo = _build(tmp_path, _BODIES[shape], _DECOY_COLLIDES)

    assert not _decoy_resolve_edges(repo), (
        f"an inline {shape} receiver is a pathlib.Path, but .resolve() was linked "
        f"to Engine.resolve, which nothing calls"
    )


@pytest.mark.parametrize("shape", sorted(_BODIES))
def test_the_control_cannot_produce_the_edge_it_is_checking_for(
    tmp_path: Path, shape: str
) -> None:
    repo = _build(tmp_path, _BODIES[shape], _DECOY_CONTROL)

    assert not _decoy_resolve_edges(repo)


@pytest.mark.xfail(
    strict=True,
    reason="an attribute of an external class has no known type (no stubs); "
    "the bare-name fallback still fires -- see the note on _EXTERNAL_ATTRIBUTE_BODY",
)
def test_an_attribute_of_an_external_receiver_is_still_matched_by_name(
    tmp_path: Path,
) -> None:
    repo = _build(tmp_path, _EXTERNAL_ATTRIBUTE_BODY, _DECOY_COLLIDES)

    assert not _decoy_resolve_edges(repo)


@pytest.mark.xfail(
    strict=True,
    reason="a tuple-unpacked local is never typed, so the receiver has no type "
    "and the bare-name fallback still fires -- see _TUPLE_UNPACKED_SRC (#1896)",
)
def test_a_tuple_unpacked_receiver_is_still_matched_by_name(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_DECOY_COLLIDES)
    (repo / "app.py").write_text(_TUPLE_UNPACKED_SRC)

    assert not _decoy_resolve_edges(repo)


def test_an_inline_operator_on_a_project_class_reaches_its_dunder(
    tmp_path: Path,
) -> None:
    """The positive side: `(factory / config).run()` with
    `Factory.__truediv__ -> Product` calls `run` as Product inherits it from
    `Base` -- not `Factory.run`. Inheritance is deliberate: a direct registry
    lookup on `Product.run` would find nothing, so this pins that the inline
    path dispatches through the same inherited-method resolution a variable
    receiver gets (local review P2)."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(
        "class Config:\n    pass\n\n"
        "class Base:\n    def run(self) -> int:\n        return 1\n\n"
        "class Product(Base):\n    pass\n\n"
        "class Factory:\n"
        "    def run(self) -> int:\n        return 0\n"
        "    def __truediv__(self, config: Config) -> Product:\n"
        "        return Product()\n"
    )
    (repo / "app.py").write_text(
        "from .engine import Factory, Config\n\n"
        "def exercise(factory: Factory, config: Config) -> int:\n"
        "    return (factory / config).run()\n"
    )
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    calls = {
        str(tgt)
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value and str(src).endswith("app.exercise")
    }
    assert any(t.endswith("engine.Base.run") for t in calls), calls
    assert not any(t.endswith("engine.Factory.run") for t in calls), calls


def test_the_fixture_can_go_red(tmp_path: Path) -> None:
    """Known-positive: the same inline shape with UNTYPED operands has no
    type to recover, so it still matches by name -- the harness demonstrably
    observes an Engine.resolve edge, and the defect tests above are
    distinguishable from a harness that sees nothing."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_DECOY_COLLIDES)
    # The failing shape exactly, minus the annotation that makes it typed.
    (repo / "app.py").write_text(
        "class Mapper:\n"
        "    def __init__(self, base, sources):\n"
        "        self.base = base\n"
        "        self.sources = sources\n"
        "\n"
        "    def at(self, i):\n"
        "        return (self.base / self.sources[i]).resolve()\n"
    )

    assert _decoy_resolve_edges(repo), (
        "the harness never observes an Engine.resolve edge, so the assertions "
        "that no such edge exists are not evidence of anything"
    )
