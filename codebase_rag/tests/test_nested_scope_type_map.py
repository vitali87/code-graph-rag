"""A nested body's bindings belong to that body, not the function around it.

Issue #1922. `build_local_variable_type_map` collects assignments from the
whole subtree, so a name bound inside a nested def or class body was
recorded in the ENCLOSING function's map. An outer receiver of the same
name then resolved its method call by the inner binding's class.

Python's scoping makes the two bindings genuinely separate: `inner`'s `v`
is local to `inner` and cannot be the same object as `outer`'s. A class
body is the same for the enclosing function -- names bound there are
attributes of the class, reached as `Holder.v`, never as a bare `v` in the
function around it.

Asserted on the type map rather than on the edge, because the bare-name
fallback resolves an unbound receiver by method name and can pick the right
class by coincidence, which would make an edge-level assertion pass for
both the fixed and the broken engine.

`_traverse_single_pass` now filters the assignments it processes to the
analysed scope, which closes the leak for a bare name: that pass was the
only writer that could put one in the enclosing map. The attribute and
property passes that run after it write only `self.`-prefixed keys
(`_infer_instance_attributes_from_init` guards on `PY_SELF_PREFIX`, and
both property passes build their key with it), so they cannot produce the
bare `v` these tests assert on, and `_collect_local_aliases` already stops
at a nested def or class of its own accord.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "proj"

_CLASSES = (
    "class Widget:\n    def render(self) -> int:\n        return 1\n\n"
    "class Banner:\n    def render(self) -> int:\n        return 2\n"
)
_HEADER = "from .engine import Widget, Banner\n\n"


def _functions(node: Node) -> Iterator[Node]:
    for child in node.children:
        if child.type == cs.TS_PY_FUNCTION_DEFINITION:
            yield child
        yield from _functions(child)


def _local_types(tmp_path: Path, body: str, function: str) -> dict[str, str]:
    repo = tmp_path / PROJECT
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_CLASSES)
    source = _HEADER + body
    (repo / "app.py").write_text(source)
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=_StatefulIngestor(), repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run(force=True)
    tree = parsers[cs.SupportedLanguage.PYTHON].parse(source.encode())
    node = next(
        candidate
        for candidate in _functions(tree.root_node)
        if (candidate.child_by_field_name("name").text or b"").decode() == function
    )
    engine = updater.factory.type_inference.python_type_inference
    return engine.build_local_variable_type_map(node, f"{PROJECT}.app")


def test_a_nested_defs_binding_does_not_type_the_outer_name(
    tmp_path: Path,
) -> None:
    """`outer`'s `v` is a parameter of unknown type; `inner`'s is a Banner
    local. They are different objects and the outer map must not claim the
    inner one's type."""
    types = _local_types(
        tmp_path,
        "def outer(v) -> int:\n"
        "    def inner() -> int:\n"
        "        v = Banner()\n"
        "        return v.render()\n"
        "    return v.render()\n",
        "outer",
    )

    assert "v" not in types


def test_a_nested_class_bodys_binding_does_not_type_the_outer_name(
    tmp_path: Path,
) -> None:
    """A class body binds attributes, reached as `Holder.v`. A bare `v` in
    the function around it is a different name entirely."""
    types = _local_types(
        tmp_path,
        "def outer(v) -> int:\n"
        "    class Holder:\n"
        "        v = Banner()\n"
        "    return v.render()\n",
        "outer",
    )

    assert "v" not in types


def test_the_functions_own_binding_still_types_it(tmp_path: Path) -> None:
    """The control: the filter must remove only what a nested body bound.

    Without this, the tests above could be satisfied by a change that
    stopped typing locals altogether.
    """
    types = _local_types(
        tmp_path,
        "def outer() -> int:\n    v = Banner()\n    return v.render()\n",
        "outer",
    )

    assert types["v"] == "Banner"


def test_a_nested_def_still_types_its_own_binding(tmp_path: Path) -> None:
    """The inner function's own map keeps the binding the outer one loses;
    the name is local to `inner`, which is exactly the point."""
    types = _local_types(
        tmp_path,
        "def outer(v) -> int:\n"
        "    def inner() -> int:\n"
        "        v = Banner()\n"
        "        return v.render()\n"
        "    return v.render()\n",
        "inner",
    )

    assert types["v"] == "Banner"


def test_a_nonlocal_rebinding_does_type_the_outer_name(tmp_path: Path) -> None:
    """`nonlocal` is the exception the scope filter must admit.

    It makes the nested assignment rebind the ENCLOSING function's name
    rather than create a local of its own, so after `inner()` runs the
    outer `v` really does hold the nested binding's value. Filtering it out
    left the outer map holding the earlier type (Greptile, #1922).
    """
    types = _local_types(
        tmp_path,
        "def outer() -> int:\n"
        "    v = Widget()\n"
        "    def inner() -> None:\n"
        "        nonlocal v\n"
        "        v = Banner()\n"
        "    inner()\n"
        "    return v.render()\n",
        "outer",
    )

    assert types["v"] == "Banner"


def test_a_global_rebinding_does_not_type_the_outer_name(tmp_path: Path) -> None:
    """`global` is NOT that exception: it binds the module's name, and the
    enclosing function's local of the same name is untouched.

    `origin/main` types the outer `v` as `Banner` here, which is wrong for
    the same reason the nested-def case is.
    """
    types = _local_types(
        tmp_path,
        "def outer(v) -> int:\n"
        "    def inner() -> None:\n"
        "        global v\n"
        "        v = Banner()\n"
        "    inner()\n"
        "    return v.render()\n",
        "outer",
    )

    assert "v" not in types


def test_a_nonlocal_unpacking_types_the_outer_names(tmp_path: Path) -> None:
    """The unpacking pass applies the same `nonlocal` exception as the
    value passes (CodeRabbit, #1922).

    `_process_assignment_unpacking` filters on scope exactly as
    `_traverse_single_pass` does, so without the exception a nested
    `nonlocal a, b; a, b = pair()` was admitted by the assignment filter
    and then dropped by the unpacking filter. The simple and complex
    passes cannot bind tuple targets, so nothing typed the names at all.
    """
    types = _local_types(
        tmp_path,
        "def pair() -> tuple[Widget, Banner]:\n"
        "    return (Widget(), Banner())\n"
        "\n"
        "def outer() -> int:\n"
        "    a = None\n"
        "    b = None\n"
        "    def inner() -> None:\n"
        "        nonlocal a, b\n"
        "        a, b = pair()\n"
        "    inner()\n"
        "    return b.render()\n",
        "outer",
    )

    assert types["a"] == "Widget"
    assert types["b"] == "Banner"


def test_an_unrelated_nested_unpacking_still_types_nothing(tmp_path: Path) -> None:
    """The control: without a `nonlocal`, a nested unpacking binds that
    body's own locals and must not reach the enclosing map, or the
    exception above would have been achieved by dropping the filter.
    """
    types = _local_types(
        tmp_path,
        "def pair() -> tuple[Widget, Banner]:\n"
        "    return (Widget(), Banner())\n"
        "\n"
        "def outer(a, b) -> int:\n"
        "    def inner() -> None:\n"
        "        a, b = pair()\n"
        "    inner()\n"
        "    return b.render()\n",
        "outer",
    )

    assert "a" not in types
    assert "b" not in types
