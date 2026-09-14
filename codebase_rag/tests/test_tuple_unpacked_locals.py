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
from codebase_rag.parsers.py.type_inference import PythonTypeInferenceEngine
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


def _nodes_under(node: Node) -> Iterator[Node]:
    """Every node under `node`, itself included."""
    yield node
    for child in node.children:
        yield from _nodes_under(child)


def _type_inference_engine(tmp_path: Path) -> PythonTypeInferenceEngine:
    """A built analyzer, for calling one pass in isolation."""
    repo = tmp_path / "engine_only"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_TWO_CLASSES)
    (repo / "app.py").write_text(_TWO)
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=_StatefulIngestor(), repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run(force=True)
    return updater.factory.type_inference.python_type_inference


def _local_types(
    tmp_path: Path,
    body: str,
    function: str,
    *,
    package: str = "",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """The engine's local type map for one function, read directly: what the
    fallback does with an UNBOUND name is its own business, so a test about
    binding asserts on the map, not on the edge the fallback then picks.
    `package` nests the module one level down (`proj.pkg.app`), for a rule
    that reads the module qn's depth.
    `extra` adds sibling modules beside `app.py`."""
    repo = tmp_path / "proj"
    module_dir = repo / package if package else repo
    module_dir.mkdir(parents=True)
    (repo / "__init__.py").touch()
    (module_dir / "__init__.py").touch()
    (module_dir / "engine.py").write_text(_TWO_CLASSES)
    (module_dir / "app.py").write_text(_TWO + body)
    for filename, source in (extra or {}).items():
        (module_dir / filename).write_text(source)
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
    module_qn = ".".join(part for part in ("proj", package, "app") if part)
    return engine.build_local_variable_type_map(node, module_qn)


def test_a_rebound_typed_parameter_does_not_unpack_its_annotation(
    tmp_path: Path,
) -> None:
    """A typed parameter rebound in the body is the same defect as the
    annotated local below, and must behave the same way (greptile-local).

    The parameter's type is seeded into the map before any assignment pass
    runs, so there is no annotated assignment node for the annotation pass
    to reason about; the supersession test therefore lives at the unpack
    site, where `_defining_call` already tests the bindings before it.
    """
    types = _local_types(
        tmp_path,
        "def opaque(n):\n    return n\n"
        "\n"
        "def use(p: tuple[int, Banner]) -> int:\n"
        "    p = opaque(1)\n"
        "    _n, b = p\n"
        "    return b.render()\n",
        "use",
    )

    assert "b" not in types


def test_an_unpack_before_a_later_rebinding_still_reads_the_annotation(
    tmp_path: Path,
) -> None:
    """The name held the annotated value AT the unpack; a rebinding further
    down does not reach back (Greptile, #1919).

    The local type map is flat per function, so it cannot say "Banner until
    line 5, unknown after". An earlier cut of this fix cleared the map
    entry whenever the name was rebound anywhere later, which untyped the
    uses before the rebinding too.
    """
    types = _local_types(
        tmp_path,
        "def opaque(n):\n    return n\n"
        "\n"
        "def use() -> int:\n"
        "    p: tuple[int, Banner] = opaque(0)\n"
        "    _n, b = p\n"
        "    r = b.render()\n"
        "    p = opaque(1)\n"
        "    return r\n",
        "use",
    )

    assert types.get("b") == "Banner"


def test_a_reassignment_the_engine_cannot_read_drops_the_annotation(
    tmp_path: Path,
) -> None:
    """`p: tuple[...] = ...` then `p = opaque()`: the annotation describes
    the FIRST value, and the name has since been rebound to something the
    engine cannot type. Keeping the tuple text would unpack the old shape
    into names the new value never produced (Greptile P1, #1919).

    The clearing rule for unpacked calls already says a later binding to
    anything but a whole-target call clears the earlier one; an annotation
    is not exempt from it.
    """
    types = _local_types(
        tmp_path,
        "def opaque(n):\n    return n\n"
        "\n"
        "def use() -> int:\n"
        "    p: tuple[int, Banner] = opaque(0)\n"
        "    p = opaque(1)\n"
        "    _n, b = p\n"
        "    return b.render()\n",
        "use",
    )

    assert "b" not in types


def test_an_unreassigned_annotation_still_binds(tmp_path: Path) -> None:
    """The control for the test above: without the reassignment the same
    annotation must still type the unpacked name, or the fix above would
    have been achieved by simply never reading annotations."""
    types = _local_types(
        tmp_path,
        "def opaque(n):\n    return n\n"
        "\n"
        "def use() -> int:\n"
        "    p: tuple[int, Banner] = opaque(0)\n"
        "    _n, b = p\n"
        "    return b.render()\n",
        "use",
    )

    assert types["b"] == "Banner"


def test_the_annotation_pass_reads_only_the_analysed_scope(
    tmp_path: Path,
) -> None:
    """This pass records an annotation only from the body being analysed.

    Asserted on the pass rather than on the final map, because the value
    passes that run before it leak a nested binding regardless: a plain
    `v = Banner()` inside a nested def reaches the enclosing map on `main`
    too, with no annotation anywhere (issue #1922). So the map after a full
    build cannot tell this pass's scope rule from that defect, and a test
    asserting `"v" not in types` would fail on `main` as well -- it would
    be measuring #1922, not this change.

    The unpacking pass filters on `_scope_of(assignment) == caller.id`;
    this checks the annotation pass does the same, by giving it a nested
    annotated assignment and an empty map of its own.
    """
    source = (
        "def outer(v) -> int:\n"
        "    def inner() -> int:\n"
        "        v: Banner = Banner()\n"
        "        return v.render()\n"
        "    return v.render()\n"
    )
    parsers, _queries = load_parsers()
    tree = parsers[cs.SupportedLanguage.PYTHON].parse(source.encode())
    outer = next(
        node
        for node in _functions(tree.root_node)
        if (node.child_by_field_name("name").text or b"").decode() == "outer"
    )
    annotated = [
        node
        for node in _nodes_under(outer)
        if node.type == cs.TS_PY_ASSIGNMENT
        and node.child_by_field_name(cs.TS_FIELD_TYPE) is not None
    ]
    assert annotated, "fixture must contain the nested annotated assignment"
    engine = _type_inference_engine(tmp_path)
    types: dict[str, str] = {}

    engine._process_assignment_annotation(outer, annotated, types, "proj.app")

    assert types == {}


def test_an_annotation_in_the_analysed_scope_still_binds(tmp_path: Path) -> None:
    """The control for the scope filter: an annotation in the function
    being analysed must keep binding."""
    types = _local_types(
        tmp_path,
        "def outer() -> int:\n    v: Banner = Banner()\n    return v.render()\n",
        "outer",
    )

    assert types["v"] == "Banner"


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


def test_a_tuple_typed_parameter_unpacks_positionally(tmp_path: Path) -> None:
    """`def use(p: tuple[int, Widget]): _n, w = p` -- the tuple type is on the
    NAME, not on a call (issue #1900). The same split applies to a stored
    `tuple[...]` type string."""
    types = _local_types(
        tmp_path,
        "def use(p: tuple[int, Banner]) -> int:\n    _n, w = p\n    return w.render()\n",
        "use",
    )
    assert types.get("w") == "Banner", types


def test_a_tuple_typed_local_unpacks_positionally(tmp_path: Path) -> None:
    """An annotated local, not a parameter: `q: tuple[Widget, Banner] = ...`."""
    types = _local_types(
        tmp_path,
        "def use(p: tuple[int, Banner]) -> int:\n"
        "    q: tuple[Widget, Banner] = (Widget(), Banner())\n"
        "    w, b = q\n"
        "    return b.render()\n",
        "use",
    )
    assert types.get("w") == "Widget", types
    assert types.get("b") == "Banner", types


def test_a_tuple_typed_local_assigned_from_an_untyped_call_unpacks(
    tmp_path: Path,
) -> None:
    """`q: tuple[int, Banner] = untyped()`: the defining call is found but
    says nothing, so the name's own annotation must be consulted next -- the
    commonest annotated-local shape, and the one a call-or-name choice
    skipped (local review P1)."""
    types = _local_types(
        tmp_path,
        "def untyped():\n"
        "    return (0, Banner())\n"
        "\n"
        "def use() -> int:\n"
        "    q: tuple[int, Banner] = untyped()\n"
        "    _n, w = q\n"
        "    return w.render()\n",
        "use",
    )
    assert types.get("w") == "Banner", types


def test_a_declaration_covers_only_the_assignment_it_was_made_for(
    tmp_path: Path,
) -> None:
    """`q: T` declares the name for the assignment that follows it, and for
    that one only (Greptile, #1919).

    After `q = opaque(0); q = opaque(1)` the name holds the second call's
    result, which the declaration never described. Treating every later
    assignment as covered by the declaration let a stale tuple shape unpack
    into a name the value no longer had.
    """
    types = _local_types(
        tmp_path,
        "def opaque(n):\n    return n\n"
        "\n"
        "def use() -> int:\n"
        "    q: tuple[int, Banner]\n"
        "    q = opaque(0)\n"
        "    q = opaque(1)\n"
        "    _n, b = q\n"
        "    return b.render()\n",
        "use",
    )

    assert "b" not in types


def test_a_declared_but_unassigned_name_carries_its_annotation(
    tmp_path: Path,
) -> None:
    """`q: tuple[int, Banner]` with no value has no right-hand side at all;
    the annotation is the only thing that can type it (local review P2)."""
    types = _local_types(
        tmp_path,
        "def untyped():\n"
        "    return (0, Banner())\n"
        "\n"
        "def use() -> int:\n"
        "    q: tuple[int, Banner]\n"
        "    q = untyped()\n"
        "    _n, w = q\n"
        "    return w.render()\n",
        "use",
    )
    assert types.get("q") == "tuple[int, Banner]", types
    assert types.get("w") == "Banner", types


def test_the_value_outranks_the_annotation_and_the_annotation_fills_the_gap(
    tmp_path: Path,
) -> None:
    """Precedence, pinned on the map: `x: Banner = Widget()` stays `Widget`
    (as before #1900), `y: Banner = opaque(1)` becomes `Banner`, and a quoted
    forward reference loses its quotes. An annotation that pre-empted the
    value regressed `x` to `Banner` and `z` to `'Banner'` (local review P1).
    `opaque` returns its parameter, so its body infers nothing: a callee that
    merely lacks an annotation is still read from its `return`."""
    types = _local_types(
        tmp_path,
        "def opaque(raw):\n"
        "    return raw\n"
        "\n"
        "def use() -> int:\n"
        "    x: Banner = Widget()\n"
        "    y: Banner = opaque(1)\n"
        "    z: 'Banner' = opaque(1)\n"
        "    return x.render() + y.render() + z.render()\n",
        "use",
    )
    assert (types.get("x"), types.get("y"), types.get("z")) == (
        "Widget",
        "Banner",
        "Banner",
    ), types


def test_a_typed_container_local_still_types_its_loop_variable(
    tmp_path: Path,
) -> None:
    """`items: List[Widget] = load()` with `load -> list[Widget]`: the value's
    `list[Widget]` is what a `for` loop can unwrap; the raw `List[Widget]`
    is not. The base tree bound `w` to `Widget`; the annotation-first rule
    lost it (local review P1, regression)."""
    types = _local_types(
        tmp_path,
        "from typing import List\n"
        "\n"
        "def load() -> list[Widget]:\n"
        "    return [Widget()]\n"
        "\n"
        "def use() -> int:\n"
        "    items: List[Widget] = load()\n"
        "    for w in items:\n"
        "        w.render()\n"
        "    return 0\n",
        "use",
    )
    assert types.get("w") == "Widget", types


def test_self_inside_a_tuple_on_a_name_keeps_its_text(tmp_path: Path) -> None:
    """`p: tuple[int, Self]` on a method's parameter: the stored text has no
    owner, so `Self` must stay `Self` rather than resolve the MODULE qn as if
    it were a method's and bind `w` to the package (local review P2). Needs
    a module three parts deep, or the wrong rule returns nothing anyway."""
    types = _local_types(
        tmp_path,
        "from typing import Self\n"
        "\n"
        "class Host:\n"
        "    def use(self, p: tuple[int, Self]) -> int:\n"
        "        _n, w = p\n"
        "        return 0\n",
        "use",
        package="pkg",
    )
    assert types.get("w") == "Self", types


_OPAQUE = "def opaque(raw):\n    return raw\n\n"

# annotation -> (import line, the type the map must hold). Each read the way
# a return annotation is; the raw text would be unreadable downstream.
_ANNOTATED_LOCAL_FORMS = {
    "optional": ("Optional[Banner]", "from typing import Optional\n"),
    "optional_quoted": ("Optional['Banner']", "from typing import Optional\n"),
    "pipe_none": ("Banner | None", ""),
    "quoted": ("'Banner'", ""),
}


@pytest.mark.parametrize("form", sorted(_ANNOTATED_LOCAL_FORMS))
def test_an_annotated_local_reads_like_a_return_annotation(
    tmp_path: Path, form: str
) -> None:
    """`y: Optional[Banner] = opaque(1); y.render()`: the map must hold
    `Banner`, not the raw `Optional[Banner]` the resolver cannot read, which
    cost `y` the fallback edge it had while untyped (local review P1). The
    edge is pinned too, since the map alone cannot show the resolver reads
    the value. The plain `quoted` form is also satisfied by the raw text
    with its quotes stripped, so no mutation reddens it alone: it pins the
    form, the other three pin the normalisation."""
    annotation, imports = _ANNOTATED_LOCAL_FORMS[form]
    body = (
        imports + _OPAQUE + "def use() -> int:\n"
        f"    y: {annotation} = opaque(1)\n"
        "    return y.render()\n"
    )
    assert _local_types(tmp_path, body, "use").get("y") == "Banner"
    edge_root = tmp_path / "edge"
    edge_root.mkdir()
    assert _render_targets(edge_root, body) == {"use": {"Banner"}}


def test_an_annotated_container_local_types_its_loop_variable(
    tmp_path: Path,
) -> None:
    """`items: List[Widget] = opaque(1)`: the value says nothing, so the
    annotation must supply the `list[Widget]` marker a loop unwraps, not the
    raw `List[Widget]` that typed `w` as `List[Widget]` (local review P1)."""
    body = (
        "from typing import List\n" + _OPAQUE + "def use() -> int:\n"
        "    items: List[Widget] = opaque(1)\n"
        "    for w in items:\n"
        "        w.render()\n"
        "    return 0\n"
    )
    types = _local_types(tmp_path, body, "use")
    assert types.get("items") == "list[Widget]", types
    assert types.get("w") == "Widget", types


def test_an_unreadable_annotation_types_nothing(tmp_path: Path) -> None:
    """`d: dict[str, Widget] = opaque(1); for k in d`: neither the resolver
    nor the loop pass can read a dict annotation, so `d` stays untyped, as
    on the base, rather than typing `k` as `dict[str, Widget]`."""
    body = (
        _OPAQUE + "def use() -> int:\n"
        "    d: dict[str, Widget] = opaque(1)\n"
        "    for k in d:\n"
        "        k.render()\n"
        "    return 0\n"
    )
    types = _local_types(tmp_path, body, "use")
    assert "d" not in types, types
    assert "k" not in types, types


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
