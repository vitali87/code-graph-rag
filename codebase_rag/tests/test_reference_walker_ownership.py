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


# The walkers that consult _is_unowned_js_scope and are reachable from a
# recorded function: assignment and return. In each, the recorded `x` holds
# the reference, so `A.m` must not also claim it.
#
# The collection and JSX walkers take the same predicate and so are fixed by
# the same change, but no fixture here reaches them: a returned array literal
# emits no REFERENCES edge even directly in a method, and JSX needs a .jsx
# tree. Rather than assert a shape the code does not produce, they are left
# uncovered and named here.
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
}

# The control: a genuinely anonymous function gets no node, so its reference
# MUST bubble to the enclosing method. No `collection` entry: a returned array
# literal emits no REFERENCES edge even directly in a method, so there is no
# existing behaviour for a control to protect.
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
