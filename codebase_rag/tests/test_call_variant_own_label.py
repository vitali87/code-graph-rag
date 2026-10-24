"""A CALLS edge to a same-named variant carries that variant's own label.

A Scala method that builds an anonymous object (`new Setup { override def
report(...) = self.report(...) }`) registers the object's `report` as a
Function in the class's name space, so the class's own `report` became the
Method variant `report@9`. The call fanned out to every variant under the
label of the one it resolved to, so `self.report(msg)` was written as
`Function -> Function report@9`: an endpoint that does not exist, which the
flush drops, and the outer method lost its caller (issue #2848).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_CALLS = cs.RelationshipType.CALLS.value
_METHOD = cs.NodeLabel.METHOD.value
_FUNCTION = cs.NodeLabel.FUNCTION.value

_ISSUE = """\
package demo

trait Setup {
  def report(msg: String): Unit
}

class Parser {
  self =>
  def report(msg: String): Unit = println(msg)

  def make(): Setup =
    new Setup {
      override def report(msg: String): Unit = self.report(msg)
    }
}
"""

_LOCAL_DEF = """\
package demo

class Q {
  def helper(): Int = 1

  def run(): Int = {
    def helper(): Int = 2
    helper()
  }

  def other(): Int = helper()
}
"""


def _index(tmp_path: Path, files: dict[str, str]) -> _StatefulIngestor:
    root = tmp_path / "p"
    root.mkdir()
    for name, src in files.items():
        (root / name).write_text(src, encoding="utf-8")
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.SCALA not in parsers:
        pytest.skip("scala parser not available")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="p",
    ).run(force=True)
    return store


def _calls(store: _StatefulIngestor) -> set[tuple[str, str, str, str]]:
    return {
        (str(e[0]), str(e[1]), str(e[3]), str(e[4]))
        for e in store.keyed_edges
        if e[2] == _CALLS
    }


def test_the_outer_method_keeps_its_self_caller(tmp_path: Path) -> None:
    store = _index(tmp_path, {"M.scala": _ISSUE})
    outer = (_METHOD, "p.M.Parser.report@9")
    assert outer in store.nodes, sorted(store.nodes)
    assert (_FUNCTION, "p.M.Parser.report", *outer) in _calls(store), _calls(store)


@pytest.mark.parametrize("files", [{"M.scala": _ISSUE}, {"Q.scala": _LOCAL_DEF}])
def test_every_call_names_a_node_that_exists(
    tmp_path: Path, files: dict[str, str]
) -> None:
    store = _index(tmp_path, files)
    phantoms = {edge for edge in _calls(store) if (edge[2], edge[3]) not in store.nodes}
    assert phantoms == set(), (phantoms, sorted(store.nodes))


def test_a_single_definition_keeps_its_label(tmp_path: Path) -> None:
    # Negative: with one definition per name nothing changes.
    src = "package demo\n\nclass R {\n  def a(): Int = b()\n  def b(): Int = 1\n}\n"
    store = _index(tmp_path, {"R.scala": src})
    assert (_METHOD, "p.R.R.a", _METHOD, "p.R.R.b") in _calls(store), _calls(store)
