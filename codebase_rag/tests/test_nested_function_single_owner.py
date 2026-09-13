# A `def inner()` nested inside a method was reached by two passes of the call
# processor: the module-level function walk (no class context) and the class
# walk (class context set). Both resolved inner's calls and both emitted CALLS
# edges for the same call sites (issue #1903). While the two passes agreed the
# duplicate was invisible; once the class pass types `self.parse()` and the
# module pass cannot, the nested function carries a correct edge and a false
# bare-name one. The class pass owns everything scoped inside a class body, so
# the module pass must skip it.
#
# The expected edge counts are taken from a plain method with the SAME body
# rather than hard-coded: `self.M()` deliberately emits a self-dispatch edge in
# addition to the resolved one, so "once" is not the right constant.
from __future__ import annotations

from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

SOURCE = (
    "class Widget:\n"
    "    def render(self) -> str:\n"
    "        return 'w'\n"
    "\n"
    "\n"
    "class Banner:\n"
    "    def render(self) -> str:\n"
    "        return 'b'\n"
    "\n"
    "\n"
    "class Parser:\n"
    "    def parse(self) -> Widget:\n"
    "        return Widget()\n"
    "\n"
    "    def direct(self) -> str:\n"
    "        w = self.parse()\n"
    "        return w.render()\n"
    "\n"
    "    def run(self) -> str:\n"
    "        def inner() -> str:\n"
    "            w = self.parse()\n"
    "            return w.render()\n"
    "\n"
    "        return inner()\n"
)

DIRECT = "repo.app.Parser.direct"
INNER = "repo.app.Parser.run.inner"


def _call_edges(tmp_path: Path) -> Counter[tuple[str, str]]:
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(ingestor=mock, repo_path=root, parsers=parsers, queries=queries).run()
    return Counter(
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == "CALLS"
    )


def _from(edges: Counter[tuple[str, str]], caller: str) -> dict[str, int]:
    return {callee: n for (source, callee), n in edges.items() if source == caller}


def test_a_function_nested_in_a_method_is_walked_by_one_pass(
    tmp_path: Path,
) -> None:
    edges = _call_edges(tmp_path)
    direct, inner = _from(edges, DIRECT), _from(edges, INNER)
    assert direct, edges
    # Same body, same edges, same multiplicity. Before the fix every edge from
    # inner appeared once more than from direct: the module pass's copy.
    assert inner == direct, (inner, direct)


def test_a_function_nested_in_a_method_keeps_its_calls(tmp_path: Path) -> None:
    # Control: the module pass skipping the nested function must not drop its
    # calls, and the class pass must attribute them to the nested node, not to
    # the enclosing method. Not reddened by restoring the old skip (the class
    # pass produced these edges before the fix too); it guards the fix from
    # over-skipping.
    edges = _call_edges(tmp_path)
    assert edges[(INNER, "repo.app.Parser.parse")] >= 1, edges
    assert (INNER, "repo.app.Parser.parse") in edges, edges
    assert ("repo.app.Parser.run", "repo.app.Parser.parse") not in edges, edges
    assert ("repo.app.Parser.run", INNER) in edges, edges


JS_MIXIN = (
    "function bar() { return 1; }\n"
    "const Mixin = (Base) => class extends Base {\n"
    "  m() {\n"
    "    function inner() { return bar(); }\n"
    "    return inner();\n"
    "  }\n"
    "};\n"
)


def test_a_function_nested_in_an_unnamed_js_class_keeps_its_edge(
    tmp_path: Path,
) -> None:
    # The class pass drops a class expression it cannot name, so it never
    # walks this `inner`. The module pass must still walk it, once, or the
    # nested function loses every edge (local review of the #1903 fix).
    parsers, queries = load_parsers()
    if "javascript" not in parsers:
        pytest.skip("javascript parser not available")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.js").write_text(JS_MIXIN, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(ingestor=mock, repo_path=root, parsers=parsers, queries=queries).run()
    edges = Counter(
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == "CALLS"
    )
    assert edges[("repo.app.m.inner", "repo.app.bar")] == 1, edges
