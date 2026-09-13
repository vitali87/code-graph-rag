"""`self.method()` and `cls.method()` receivers are typed as the enclosing
class (issue #1901).

The local type map never carried a `self` entry, so a right-hand-side
`self.parse()` had no receiver type, the assigned local stayed untyped, and a
later call on it fell to the bare-name fallback: `w.render()` bound to every
class defining `render`. The map is now seeded with the enclosing class for
Python methods, the way the Rust branch already seeds `self`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

# The decoy defines the colliding NAME; nothing in the fixture ever calls it.
_DECOY_COLLIDES = (
    "class Banner:\n    def render(self) -> str:\n        return 'banner'\n"
)
# Instrument check: with the name gone, no edge can be emitted for any reason.
_DECOY_CONTROL = "class Banner:\n    def paint(self) -> str:\n        return 'banner'\n"

_WIDGET = "class Widget:\n    def render(self) -> str:\n        return 'w'\n"

_SELF_ASSIGN = (
    "from proj.widget import Widget\n\n\n"
    "class Parser:\n"
    "    def parse(self) -> Widget:\n"
    "        return Widget()\n\n"
    "    def run(self) -> str:\n"
    "        w = self.parse()\n"
    "        return w.render()\n"
)
_CLS_ASSIGN = (
    "from proj.widget import Widget\n\n\n"
    "class Parser:\n"
    "    @classmethod\n"
    "    def make(cls) -> Widget:\n"
    "        return Widget()\n\n"
    "    @classmethod\n"
    "    def build(cls) -> str:\n"
    "        w = cls.make()\n"
    "        return w.render()\n"
)
_SELF_SHADOWED = (
    "from proj.widget import Widget\n\n\n"
    "class Parser:\n"
    "    def parse(self) -> Widget:\n"
    "        return Widget()\n\n"
    "    def run(self) -> str:\n"
    "        self = Widget()\n"
    "        return self.render()\n"
)


def _render_targets(repo: Path, caller_suffix: str) -> set[str]:
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    return {
        str(tgt)
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value
        and str(src).endswith(caller_suffix)
        and str(tgt).endswith(".render")
    }


def _build(tmp_path: Path, app: str, decoy: str) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    (repo / "__init__.py").touch()
    (repo / "widget.py").write_text(_WIDGET, encoding="utf-8")
    (repo / "banner.py").write_text(decoy, encoding="utf-8")
    (repo / "app.py").write_text(app, encoding="utf-8")
    return repo


@pytest.mark.parametrize(
    ("app", "caller"),
    [(_SELF_ASSIGN, ".Parser.run"), (_CLS_ASSIGN, ".Parser.build")],
    ids=["self", "cls"],
)
def test_a_local_assigned_from_a_self_call_is_typed(
    tmp_path: Path, app: str, caller: str
) -> None:
    repo = _build(tmp_path, app, _DECOY_COLLIDES)
    assert _render_targets(repo, caller) == {"proj.widget.Widget.render"}, (
        "the receiver assigned from self.parse() was untyped and matched by name"
    )


@pytest.mark.parametrize(
    ("app", "caller"),
    [(_SELF_ASSIGN, ".Parser.run"), (_CLS_ASSIGN, ".Parser.build")],
    ids=["self", "cls"],
)
def test_the_control_cannot_produce_the_decoy_edge(
    tmp_path: Path, app: str, caller: str
) -> None:
    # Validates the instrument: with the decoy's method renamed there is no
    # name to collide with, so the assertion above cannot pass for a wrong
    # reason; the true edge is still there.
    repo = _build(tmp_path, app, _DECOY_CONTROL)
    assert _render_targets(repo, caller) == {"proj.widget.Widget.render"}


def test_a_local_named_self_keeps_its_own_type(tmp_path: Path) -> None:
    # Seeding uses setdefault: a binding of that name in the body wins.
    repo = _build(tmp_path, _SELF_SHADOWED, _DECOY_COLLIDES)
    assert _render_targets(repo, ".Parser.run") == {"proj.widget.Widget.render"}


def test_the_seed_types_the_assigned_local_and_is_not_exported(tmp_path: Path) -> None:
    # The mechanism itself: with the class context, the method's map types
    # `w` from `self.parse()`; `self` and `cls` themselves are NOT left in
    # the map, so `self.m()` calls keep the resolver's own policy for them
    # (concrete sibling over abstract stub). A module-level function's map
    # has none of the three.
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    repo = _build(
        tmp_path,
        _SELF_ASSIGN + "\n\ndef free() -> int:\n    return 1\n",
        _DECOY_CONTROL,
    )
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store, repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run(force=True)
    python = next(p for k, p in parsers.items() if str(k) == "python")
    tree = python.parse((repo / "app.py").read_bytes())
    class_node = next(
        n for n in tree.root_node.children if n.type == "class_definition"
    )
    body = class_node.child_by_field_name("body")
    run_node = next(
        n
        for n in body.children
        if n.type == "function_definition"
        and n.child_by_field_name("name").text == b"run"
    )
    free_node = next(
        n for n in tree.root_node.children if n.type == "function_definition"
    )
    ti = updater.factory.type_inference
    with_context = ti.build_local_variable_type_map(
        run_node, "proj.app", cs.SupportedLanguage.PYTHON, "proj.app.Parser"
    )
    assert with_context.get("w") == "Widget"
    assert "self" not in with_context
    assert "cls" not in with_context
    without_context = ti.build_local_variable_type_map(
        run_node, "proj.app", cs.SupportedLanguage.PYTHON, None
    )
    assert "w" not in without_context, "the seed is what types w"
    free_map = ti.build_local_variable_type_map(
        free_node, "proj.app", cs.SupportedLanguage.PYTHON, None
    )
    assert not {"self", "cls", "w"} & set(free_map)
