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

from collections.abc import Iterator
from pathlib import Path

import pytest
from tree_sitter import Node

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


def _functions(node: Node) -> Iterator[Node]:
    """Every def under a node, nested ones included, in document order."""
    for child in node.children:
        if child.type == "function_definition":
            yield child
        yield from _functions(child)


def _local_types(
    tmp_path: Path, body: str, function: str, *, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """The engine's local type map for one function, read directly: what the
    fallback does with an UNBOUND name is its own business, so a test about
    binding asserts on the map, not on the edge the fallback then picks.
    `extra` adds sibling modules to the package."""
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_TWO_CLASSES)
    (repo / "app.py").write_text(_TWO + body)
    for filename, source in (extra or {}).items():
        (repo / filename).write_text(source)
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=_StatefulIngestor(), repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run(force=True)
    tree = parsers[cs.SupportedLanguage.PYTHON].parse((_TWO + body).encode())
    node = next(
        n
        for n in _functions(tree.root_node)
        if (n.child_by_field_name("name").text or b"").decode() == function
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
    assert "a" not in types, types
    assert "b" not in types, types


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


def test_a_rebinding_to_a_non_call_clears_the_earlier_call(tmp_path: Path) -> None:
    """`p = fw(); p = supplied; _n, w = p`: the unpacked value is `supplied`,
    about which nothing is known, so `w` must stay unbound rather than keep
    fw's element (Greptile P2)."""
    types = _local_types(
        tmp_path,
        "def fw() -> tuple[int, Widget]:\n"
        "    return (0, Widget())\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    p = fw()\n"
        "    p = supplied\n"
        "    _n, w = p\n"
        "    return w.render()\n",
        "use",
    )
    assert "w" not in types, types


def test_a_nested_scope_binding_is_not_the_outer_name(tmp_path: Path) -> None:
    """The walk captures every assignment under the function, including a
    nested def's `p = fb()`, which is a different variable; the outer
    unpacking must take the outer `p = fw()` (Greptile P1)."""
    types = _local_types(
        tmp_path,
        "def fw() -> tuple[int, Widget]:\n"
        "    return (0, Widget())\n"
        "\n"
        "def fb() -> tuple[int, Banner]:\n"
        "    return (0, Banner())\n"
        "\n"
        "def use() -> int:\n"
        "    p = fw()\n"
        "    def helper() -> int:\n"
        "        p = fb()\n"
        "        return 0\n"
        "    _n, w = p\n"
        "    return w.render() + helper()\n",
        "use",
    )
    assert types.get("w") == "Widget", types


def test_a_union_inside_an_element_is_not_a_top_level_union(tmp_path: Path) -> None:
    """`-> tuple[Widget | None, Banner]`: the `|` belongs to the first
    element, not to the tuple, so the annotation is one member and `b` is
    `Banner` (Greptile P1, CodeRabbit)."""
    types = _local_types(
        tmp_path,
        "def pair() -> tuple[Widget | None, Banner]:\n"
        "    return (None, Banner())\n"
        "\n"
        "def use() -> int:\n"
        "    w, b = pair()\n"
        "    return b.render()\n",
        "use",
    )
    assert types.get("b") == "Banner", types
    assert types.get("w") == "Widget | None", types


def test_a_module_qualified_free_function_supplies_the_tuple(tmp_path: Path) -> None:
    """`helpers.make_pair()` resolves to a FUNCTION qn; looked up as a method
    it read `helpers` as a class and found nothing (Greptile P1, CodeRabbit)."""
    types = _local_types(
        tmp_path,
        "from . import helpers\n"
        "\n"
        "def use() -> int:\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        "use",
        extra={
            "helpers.py": (
                "from .engine import Widget\n"
                "\n"
                "def make_pair() -> tuple[int, Widget]:\n"
                "    return (0, Widget())\n"
            )
        },
    )
    assert types.get("w") == "Widget", types


_REBINDING_STATEMENTS = {
    "augmented": "    p += supplied\n",
    "for_loop": "    for p in supplied:\n        pass\n",
    "with_as": "    with supplied as p:\n        pass\n",
    # `p` is now a PIECE of something, not fw's tuple, whether that something
    # is a plain value or fw's own result (local review P2).
    "pattern_target": "    p, q = supplied\n",
    "pattern_target_call": "    p, q = fw()\n",
    # tree-sitter parses a with statement's `as (p, q)` as a tuple
    # EXPRESSION, not a pattern (local review P2).
    "with_tuple_target": "    with supplied as (p, q):\n        pass\n",
    "walrus": "    if (p := supplied):\n        pass\n",
    "except_as": "    try:\n        pass\n    except Exception as p:\n        pass\n",
}


@pytest.mark.parametrize("statement", sorted(_REBINDING_STATEMENTS))
def test_a_rebinding_without_an_assignment_node_clears_the_call(
    tmp_path: Path, statement: str
) -> None:
    """`p += x`, `for p in xs`, `with cm as p`: none is an `assignment`
    node, each rebinds `p`, so a later `_n, w = p` is no longer fw's result
    (local review P2)."""
    types = _local_types(
        tmp_path,
        "def fw() -> tuple[int, Widget]:\n"
        "    return (0, Widget())\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    p = fw()\n" + _REBINDING_STATEMENTS[statement] + "    _n, w = p\n"
        "    return w.render()\n",
        "use",
    )
    assert "w" not in types, types


def test_a_local_shadowing_an_imported_module_name_is_the_local(
    tmp_path: Path,
) -> None:
    """`helpers = Maker(); _n, w = helpers.make()`: the receiver is the LOCAL,
    a Maker, not the imported module of the same name (local review P2)."""
    types = _local_types(
        tmp_path,
        "from . import helpers\n"
        "from .factory import Maker\n"
        "\n"
        "def use() -> int:\n"
        "    helpers = Maker()\n"
        "    _n, w = helpers.make()\n"
        "    return w.render()\n",
        "use",
        extra={
            "helpers.py": (
                "from .engine import Banner\n"
                "\n"
                "def make() -> tuple[int, Banner]:\n"
                "    return (0, Banner())\n"
            ),
            "factory.py": (
                "from .engine import Widget\n"
                "\n"
                "class Maker:\n"
                "    def make(self) -> tuple[int, Widget]:\n"
                "        return (0, Widget())\n"
            ),
        },
    )
    assert types.get("w") == "Widget", types


_HELPERS_BANNER = (
    "from .engine import Banner\n"
    "\n"
    "def make_pair() -> tuple[int, Banner]:\n"
    "    return (0, Banner())\n"
)

# body -> the type `w` must get: None when a local binding of the callee's
# name makes the imported module or function unreachable, whatever the
# binding's own type is. Python makes a name assigned ANYWHERE in a body local
# for the whole body, so the binding's position does not matter either.
_UNTYPED_SHADOWS = {
    "local_over_module": (
        "from . import helpers\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    helpers = supplied\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        None,
    ),
    "parameter_over_module": (
        "from . import helpers\n"
        "\n"
        "def use(helpers) -> int:\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        None,
    ),
    "later_local_over_module": (
        "from . import helpers\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    _n, w = helpers.make_pair()\n"
        "    helpers = supplied\n"
        "    return w.render()\n",
        None,
    ),
    "loop_variable_over_module": (
        "from . import helpers\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    for helpers in supplied:\n"
        "        pass\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        None,
    ),
    "local_over_imported_function": (
        "from .helpers import make_pair\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    make_pair = supplied\n"
        "    _n, w = make_pair()\n"
        "    return w.render()\n",
        None,
    ),
    # Every other way a body binds a name (local review P2): each makes
    # `helpers` local for the whole body, so the module is unreachable.
    "with_tuple_over_module": (
        "from . import helpers\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    with supplied as (helpers, x):\n"
        "        pass\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        None,
    ),
    "walrus_over_module": (
        "from . import helpers\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    if (helpers := supplied):\n"
        "        pass\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        None,
    ),
    "except_target_over_module": (
        "from . import helpers\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as helpers:\n"
        "        pass\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        None,
    ),
    "body_import_over_module": (
        "from . import helpers\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    import os as helpers\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        None,
    ),
    # A body-level import of a PROJECT name is a re-import the import map
    # resolves, not a shadow (CodeRabbit); `import os as helpers` above is
    # the external one the map cannot place, and stays a shadow.
    "body_from_import_function_resolves": (
        "def use(supplied) -> int:\n"
        "    from .helpers import make_pair\n"
        "    _n, w = make_pair()\n"
        "    return w.render()\n",
        "Banner",
    ),
    "body_from_import_module_resolves": (
        "def use(supplied) -> int:\n"
        "    from . import helpers\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        "Banner",
    ),
    "body_import_alias_resolves": (
        "def use(supplied) -> int:\n"
        "    from .helpers import make_pair as mp\n"
        "    _n, w = mp()\n"
        "    return w.render()\n",
        "Banner",
    ),
    "nested_def_name_over_module": (
        "from . import helpers\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    def helpers() -> int:\n"
        "        return 0\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        None,
    ),
    "match_capture_over_module": (
        "from . import helpers\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    match supplied:\n"
        "        case [helpers]:\n"
        "            pass\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        None,
    ),
    # A closure reads the enclosing def's local, not the module.
    "enclosing_local_over_module": (
        "from . import helpers\n"
        "\n"
        "def outer(supplied) -> int:\n"
        "    helpers = supplied\n"
        "    def use() -> int:\n"
        "        _n, w = helpers.make_pair()\n"
        "        return w.render()\n"
        "    return use()\n",
        None,
    ),
    # `case helpers.X` is a VALUE pattern: it captures nothing.
    "match_value_pattern_is_not_a_capture": (
        "from . import helpers\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    match supplied:\n"
        "        case helpers.X:\n"
        "            pass\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        "Banner",
    ),
    # A class body's names are not visible to its methods, as Python has it.
    "enclosing_class_attribute_does_not_shadow": (
        "from . import helpers\n"
        "\n"
        "class K:\n"
        "    helpers = 1\n"
        "\n"
        "    def use(self) -> int:\n"
        "        _n, w = helpers.make_pair()\n"
        "        return w.render()\n",
        "Banner",
    ),
    # A nested def's binding is that def's local; the outer `helpers` is
    # still the module.
    "nested_local_does_not_shadow": (
        "from . import helpers\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    def helper() -> int:\n"
        "        helpers = supplied\n"
        "        return 0\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render() + helper()\n",
        "Banner",
    ),
    # `obj.helpers = ...` binds an attribute, not a local named `helpers`.
    "attribute_target_is_not_a_local": (
        "from . import helpers\n"
        "\n"
        "def use(obj) -> int:\n"
        "    obj.helpers = obj\n"
        "    _n, w = helpers.make_pair()\n"
        "    return w.render()\n",
        "Banner",
    ),
    # Known-positive for the function form: the same import, unshadowed.
    "imported_function_unshadowed": (
        "from .helpers import make_pair\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    _n, w = make_pair()\n"
        "    return w.render()\n",
        "Banner",
    ),
}


@pytest.mark.parametrize("shadow", sorted(_UNTYPED_SHADOWS))
def test_an_untyped_local_binding_shadows_an_imported_name(
    tmp_path: Path, shadow: str
) -> None:
    """`helpers = supplied; _n, w = helpers.make_pair()`: `helpers` is the
    untyped local, not the imported module, so `w` gets no type. The earlier
    guard looked only at TYPED locals, and an untyped one was invisible to it
    (Greptile P1). The parameter and later-binding forms are the same rule;
    the function form is the identifier path of the same lookup."""
    body, expected = _UNTYPED_SHADOWS[shadow]
    types = _local_types(tmp_path, body, "use", extra={"helpers.py": _HELPERS_BANNER})
    assert types.get("w") == expected, types


def test_a_nested_scope_unpacking_does_not_type_the_outer_name(
    tmp_path: Path,
) -> None:
    """A nested def's `_n, w = fw()` binds the nested def's `w`; the outer
    `w`, bound to an untyped parameter, must not receive Widget from it
    (CodeRabbit)."""
    types = _local_types(
        tmp_path,
        "def fw() -> tuple[int, Widget]:\n"
        "    return (0, Widget())\n"
        "\n"
        "def use(supplied) -> int:\n"
        "    w = supplied\n"
        "    def helper() -> int:\n"
        "        _n, w = fw()\n"
        "        return w.render()\n"
        "    return w.render() + helper()\n",
        "use",
    )
    assert "w" not in types, types


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
