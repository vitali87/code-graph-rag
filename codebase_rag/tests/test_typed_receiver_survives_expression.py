"""A receiver assigned from an expression keeps its type.

Issue #1868. `_collect_local_aliases` recorded an alias only when the
assignment's right-hand side was a bare name or attribute. Every other
expression left the variable UNTYPED, and a later `var.method()` then fell
back to matching the bare method name against every class in the project that
defines it -- emitting a CALLS edge to each.

The shapes below are the common ones for a typed receiver: `a or default`,
`a if cond else b`, and `base / "sub"` (the pathlib idiom). All three appear
in this repo's own `codebase_rag/trace`, where they produced false CALLS
edges from `cli.pull_cmd` into five unrelated `*FrameResolver.resolve`
methods.

Every test here pairs the collision fixture with a CONTROL in which the
decoy class defines `compute()` instead of `resolve()`. Without the control
a green result is unreadable: it would look identical whether the edge was
correctly suppressed or the harness simply produced no edges at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

# The decoy defines a method whose NAME collides with pathlib.Path.resolve.
# Nothing in the fixture ever calls it.
_DECOY_COLLIDES = "class Engine:\n    def resolve(self) -> int:\n        return 1\n"
# Same class, method renamed: the bare-name fallback has nothing to match, so
# a correctly-behaving indexer and a broken one both emit no edge. This is the
# instrument check, not a behaviour assertion.
_DECOY_CONTROL = "class Engine:\n    def compute(self) -> int:\n        return 1\n"

_HEAD = "from pathlib import Path\n\ndef run(target: Path | None, base: Path) -> str:\n"

_BODIES = {
    "or_expression": (
        "    resolved = target or Path('x')\n    return resolved.resolve().as_posix()\n"
    ),
    "ternary": (
        "    resolved = target if target else Path('x')\n"
        "    return resolved.resolve().as_posix()\n"
    ),
    "path_division": (
        "    resolved = base / 'sub'\n    return resolved.resolve().as_posix()\n"
    ),
}


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
def test_an_expression_receiver_does_not_call_every_same_named_method(
    tmp_path: Path, shape: str
) -> None:
    """The defect: the receiver's type is lost and the call matches by name."""
    repo = _build(tmp_path, _BODIES[shape], _DECOY_COLLIDES)

    assert not _decoy_resolve_edges(repo), (
        f"a {shape} receiver is a pathlib.Path, but .resolve() was linked to "
        f"Engine.resolve, which nothing calls"
    )


@pytest.mark.parametrize("shape", sorted(_BODIES))
def test_the_control_cannot_produce_the_edge_it_is_checking_for(
    tmp_path: Path, shape: str
) -> None:
    """Validates the instrument rather than the code under test.

    With the decoy's method renamed there is no name to collide with, so no
    edge can be emitted for any reason. If this ever fails, the fixture is
    matching something other than the collision and the test above proves
    nothing.
    """
    repo = _build(tmp_path, _BODIES[shape], _DECOY_CONTROL)

    assert not _decoy_resolve_edges(repo)


def test_the_fixture_can_go_red(tmp_path: Path) -> None:
    """A known-positive: the bare-name fallback DOES fire when the type is
    genuinely unknowable, so the assertions above are not vacuously green.

    `_make()` has no return annotation and returns a decoy instance, so there
    is no type to recover and matching by name is the only option left. An
    edge here is expected and correct; its presence proves the harness can
    observe an Engine.resolve edge when one is emitted.
    """
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_DECOY_COLLIDES)
    (repo / "app.py").write_text(
        "from engine import Engine\n"
        "\n"
        "def _make():\n"
        "    return Engine()\n"
        "\n"
        "def run():\n"
        "    thing = _make()\n"
        "    return thing.resolve()\n"
    )

    assert _decoy_resolve_edges(repo), (
        "the harness never observes an Engine.resolve edge, so the assertions "
        "that no such edge exists are not evidence of anything"
    )
