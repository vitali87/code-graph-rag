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


def test_a_nested_class_body_binding_does_not_type_the_outer_name(
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


def test_a_mixed_nonlocal_unpacking_binds_only_the_declared_name(
    tmp_path: Path,
) -> None:
    """`nonlocal a` then `a, b = pair()`: `a` is the enclosing function's,
    `b` is a local of the nested body (Greptile, #1922).

    Admitting the assignment for `a`'s sake and then binding every target
    leaked `b` outward. `origin/main` binds neither, so this was a
    regression introduced by the nonlocal exception, not a pre-existing
    gap.
    """
    types = _local_types(
        tmp_path,
        "def pair() -> tuple[Widget, Banner]:\n"
        "    return (Widget(), Banner())\n"
        "\n"
        "def outer() -> int:\n"
        "    a = None\n"
        "    def inner() -> None:\n"
        "        nonlocal a\n"
        "        a, b = pair()\n"
        "        return b\n"
        "    inner()\n"
        "    return a.render()\n",
        "outer",
    )

    assert types["a"] == "Widget"
    assert "b" not in types


_THREE_LEVEL = (
    "def outer() -> int:\n"
    "    v = Widget()\n"
    "    def middle() -> None:\n"
    "        v = Banner()\n"
    "        def inner() -> None:\n"
    "            nonlocal v\n"
    "            v = Splash()\n"
    "        inner()\n"
    "    middle()\n"
    "    return v.render()\n"
)


def test_a_nonlocal_binds_the_nearest_enclosing_binder(tmp_path: Path) -> None:
    """With three levels, `nonlocal` rebinds the NEAREST enclosing function
    that binds the name, not every ancestor.

    `middle` binds `v`, so `inner`'s `nonlocal v` rebinds middle's `v` and
    outer's `v` is untouched. Collecting every descendant's declarations
    flat let inner's `Gamma()` overwrite outer's inferred type, so the
    outer name reported the wrong class (Greptile, PR #1928).
    """
    assert _local_types(tmp_path, _THREE_LEVEL, "outer")["v"] == "Widget"


def test_the_nearest_binder_still_receives_the_nonlocal(tmp_path: Path) -> None:
    """The other half of the rule: middle OWNS the rebinding, so its own
    map must hold the innermost type. Asserting only outer's value would
    pass for an implementation that dropped the declaration entirely."""
    assert _local_types(tmp_path, _THREE_LEVEL, "middle")["v"] == "Splash"


def test_a_nonlocal_still_reaches_past_a_scope_that_does_not_bind_it(
    tmp_path: Path,
) -> None:
    """The control for the test above: when the intervening function does
    NOT bind the name, the declaration reaches the outer one as before, so
    the ownership rule cannot be satisfied by refusing every nested case."""
    types = _local_types(
        tmp_path,
        "def outer() -> int:\n"
        "    v = Widget()\n"
        "    def middle() -> None:\n"
        "        def inner() -> None:\n"
        "            nonlocal v\n"
        "            v = Banner()\n"
        "        inner()\n"
        "    middle()\n"
        "    return v.render()\n",
        "outer",
    )

    assert types["v"] == "Banner"


def test_a_nonlocal_does_not_admit_a_deeper_scope_that_does_not_declare_it(
    tmp_path: Path,
) -> None:
    """`middle`'s `nonlocal v` rebinds outer's `v`, but `inner` declares
    nothing, so its `v = Banner()` is inner's own local. Admission is per
    binding: the declaration must sit in the binding's OWN body (#2124).
    `middle` deliberately does not assign `v`; if it did, a later `Widget`
    would hide the leaked `Banner`."""
    types = _local_types(
        tmp_path,
        "def outer() -> int:\n"
        "    v = Widget()\n"
        "    def middle() -> None:\n"
        "        nonlocal v\n"
        "        def inner() -> int:\n"
        "            v = Banner()\n"
        "            return v.render()\n"
        "        v.render()\n"
        "    return v.render()\n",
        "outer",
    )

    assert types["v"] == "Widget"


def test_a_nested_for_target_does_not_type_the_outer_name(tmp_path: Path) -> None:
    types = _local_types(
        tmp_path,
        "def outer() -> int:\n"
        "    v = Widget()\n"
        "    def inner() -> None:\n"
        "        for v in [Banner()]:\n"
        "            pass\n"
        "    return v.render()\n",
        "outer",
    )

    assert types["v"] == "Widget"


def test_a_nonlocal_for_target_does_type_the_outer_name(tmp_path: Path) -> None:
    """The `nonlocal` exception covers a `for` target as it covers an
    assignment: the control that keeps the test above from passing for an
    implementation that drops every nested loop."""
    types = _local_types(
        tmp_path,
        "def outer() -> int:\n"
        "    v = Widget()\n"
        "    def inner() -> None:\n"
        "        nonlocal v\n"
        "        for v in [Banner()]:\n"
        "            pass\n"
        "    inner()\n"
        "    return v.render()\n",
        "outer",
    )

    assert types["v"] == "Banner"


def test_a_comprehension_variable_does_not_type_the_functions_own_name(
    tmp_path: Path,
) -> None:
    """A comprehension has its own scope in Python 3, so its `v` is not the
    function's `v`, and it cannot declare `nonlocal` (#2124)."""
    types = _local_types(
        tmp_path,
        "def outer() -> int:\n"
        "    v = Widget()\n"
        "    xs = [v for v in [Banner()]]\n"
        "    return v.render()\n",
        "outer",
    )

    assert types["v"] == "Widget"


def test_a_comprehension_variable_is_still_typed_for_its_own_body(
    tmp_path: Path,
) -> None:
    """The control: with no function binding of the same name, the
    comprehension's variable keeps typing calls inside its own body
    (`[w.render() for w in ...]`), which reads the function's map."""
    types = _local_types(
        tmp_path,
        "def outer() -> list[int]:\n    return [w.render() for w in [Banner()]]\n",
        "outer",
    )

    assert types["w"] == "Banner"


def test_a_parameter_is_the_nearest_binder_of_a_nonlocal(tmp_path: Path) -> None:
    """`middle(v: Banner)` binds `v` as a PARAMETER, so `inner`'s `nonlocal v`
    names middle's parameter and outer's `v` keeps its own type. The owner
    check read body bindings only, skipped middle, and admitted inner's
    `for v in [Banner()]` into outer's map (bot review)."""
    body = (
        "def outer():\n"
        "    v = Widget()\n"
        "    def middle(v: Banner):\n"
        "        def inner():\n"
        "            nonlocal v\n"
        "            for v in [Banner()]:\n"
        "                pass\n"
        "        inner()\n"
        "    middle(Banner())\n"
        "    return v\n"
    )
    assert _local_types(tmp_path, body, "outer")["v"] == "Widget"


def test_a_forwarding_nonlocal_does_not_own_the_name(tmp_path: Path) -> None:
    """`middle` declares `nonlocal v` and assigns it, so middle FORWARDS
    outer's `v`; `inner`'s `nonlocal v` therefore reaches outer too, and
    inner's rebinding types outer's name (bot review)."""
    body = (
        "def outer():\n"
        "    v = Widget()\n"
        "    def middle():\n"
        "        nonlocal v\n"
        "        v = Widget()\n"
        "        def inner():\n"
        "            nonlocal v\n"
        "            v = Banner()\n"
        "        inner()\n"
        "    middle()\n"
        "    return v\n"
    )
    assert _local_types(tmp_path, body, "outer")["v"] == "Banner"
