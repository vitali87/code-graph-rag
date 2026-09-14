"""A recorded nameless function owns its references (issue #1932).

`x: function () {}` in an object literal has no AST name and no arrow
binding name, but the definition pass registers it under the key's name, so
`repo.app.A.m.x` is a real node with its own walk. `_is_unowned_js_scope`
did not know about that third source of ownership -- it asked only for an
AST name or an arrow binding -- so the four reference walkers ALSO descended
into it from the enclosing method, and the same identifier was attributed to
both. A consumer counting references then double-counts one occurrence.

Each shape below pairs the duplicate check with a control that must keep its
edge: a genuinely anonymous callback (`[].map(function () {...})`) gets no
node of its own, so its references MUST still bubble up to the enclosing
method. A fix that simply stopped descending would break that, and the
controls are what makes the difference visible.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_PRELUDE = "function target() { return 1; }\n"


def _reference_edges(tmp_path: Path, source: str) -> Counter[tuple[str, str]]:
    """REFERENCES edges to `target`, by (source qn, target qn)."""
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.JS not in parsers:
        pytest.skip("javascript parser not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.js").write_text(_PRELUDE + source)
    ingestor = MagicMock()
    GraphUpdater(
        ingestor=ingestor, repo_path=repo, parsers=parsers, queries=queries
    ).run(force=True)
    found: Counter[tuple[str, str]] = Counter()
    for call in ingestor.ensure_relationship_batch.call_args_list:
        (_sl, _sk, src), rel, (_tl, _tk, tgt) = call.args[:3]
        if rel == cs.RelationshipType.REFERENCES.value:
            found[(str(src), str(tgt))] += 1
    return found


# One shape per walker that consults _is_unowned_js_scope and is reachable
# from a plain .js fixture: assignment, return and collection. In each, the
# recorded `x` holds the reference, so `A.m` must not also claim it.
#
# The JSX walker takes the same predicate; its behaviour is pinned by
# test_jsx_component_references.py, including the `cell:` arrow that must
# keep bubbling (see the arrow control below).
_RECORDED = {
    "assignment": (
        "class A {\n"
        "  m() {\n"
        "    const h = { x: function () { const f = target; return f; } };\n"
        "    return h.x();\n"
        "  }\n"
        "}\n"
    ),
    "return": (
        "class A {\n"
        "  m() {\n"
        "    const h = { x: function () { return target; } };\n"
        "    return h.x();\n"
        "  }\n"
        "}\n"
    ),
    # Reaches the COLLECTION walker: a shorthand property in a returned
    # object literal. An explicit pair (`{ go: target }`) and an array
    # literal emit nothing, so the shorthand is the one shape that gets
    # there.
    "collection": (
        "class A {\n"
        "  m() {\n"
        "    const h = { x: function () { return { target }; } };\n"
        "    return h.x();\n"
        "  }\n"
        "}\n"
    ),
    # A generator expression is registered under its key like a function
    # expression and has no config-callback consumer, so it is owned too.
    "generator": (
        "class A {\n"
        "  m() {\n"
        "    const h = { x: function* () { const f = target; return f; } };\n"
        "    return h.x();\n"
        "  }\n"
        "}\n"
    ),
}

# The control: a genuinely anonymous function gets no node, so its reference
# MUST bubble to the enclosing method.
_ANONYMOUS = {
    "assignment": (
        "class A {\n"
        "  m() {\n"
        "    return [1].map(function () { const f = target; return f; });\n"
        "  }\n"
        "}\n"
    ),
    "return": (
        "class A {\n"
        "  m() {\n"
        "    return [1].map(function () { return target; });\n"
        "  }\n"
        "}\n"
    ),
    "collection": (
        "class A {\n"
        "  m() {\n"
        "    return [1].map(function () { return { target }; });\n"
        "  }\n"
        "}\n"
    ),
}


@pytest.mark.parametrize("shape", sorted(_RECORDED))
def test_a_recorded_function_does_not_share_its_reference(
    tmp_path: Path, shape: str
) -> None:
    """The recorded node owns the reference; the enclosing method must not
    emit a second copy of it."""
    edges = _reference_edges(tmp_path, _RECORDED[shape])
    assert edges[("repo.app.A.m.x", "repo.app.target")] == 1, edges
    assert edges[("repo.app.A.m", "repo.app.target")] == 0, edges


@pytest.mark.parametrize("shape", sorted(_ANONYMOUS))
def test_an_anonymous_callback_still_bubbles_its_reference(
    tmp_path: Path, shape: str
) -> None:
    """The control. An anonymous function expression gets no caller node, so
    suppressing the descent entirely would drop this edge rather than
    de-duplicate it."""
    edges = _reference_edges(tmp_path, _ANONYMOUS[shape])
    assert edges[("repo.app.A.m", "repo.app.target")] >= 1, edges


_ARROW_IN_A_CONFIG_OBJECT = (
    "class A {\n"
    "  m() {\n"
    "    const cols = [{ cell: () => target }];\n"
    "    return cols;\n"
    "  }\n"
    "}\n"
)


def test_an_arrow_in_a_config_object_still_bubbles(tmp_path: Path) -> None:
    """An arrow that is an object value is registered under its key too, so
    `is_named` alone would claim it. Owning it moves the edge to the arrow's
    own node rather than dropping it -- the target stays reachable -- but
    `test_jsx_component_in_config_callback_is_referenced` asserts the module
    is the edge's SOURCE, and consumers read it that way. This control keeps
    the clause off arrows (greptile-local on #1932, which measured that the
    'reports as dead' reason first given here was false)."""
    edges = _reference_edges(tmp_path, _ARROW_IN_A_CONFIG_OBJECT)
    assert edges[("repo.app.A.m", "repo.app.target")] >= 1, edges
