import shutil
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from evals import constants as ec
from evals.retrieval import (
    cgr_call_edges,
    first_party_property_names,
    first_party_symbols,
    grep_call_edges,
    oracle_call_edges,
    parse_py_trees,
    score_retrieval,
)
from evals.types_defs import NameEdge, NodeKey

_CALLS = cs.RelationshipType.CALLS.value
_MODULE = cs.NodeLabel.MODULE.value

_RG = shutil.which(ec.RG_BIN)
needs_rg = pytest.mark.skipif(_RG is None, reason="ripgrep not installed")

# (H) core.py genuinely CALLS helper(), instantiates Widget(), and calls w.run();
# (H) build() is defined but never called, so it is a caller, never a callee.
_CORE = """\
def helper():
    return 1


class Widget:
    def run(self):
        return helper()


def build():
    helper()
    w = Widget()
    w.run()
    return w
"""

# (H) uses.py only imports and aliases helper/Widget; it never calls them, so a
# (H) name-based grep over-includes it while the call oracle does not.
_USES = """\
from pkg.core import Widget, helper

ALIAS = helper
VALUE = 2
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(_CORE, encoding="utf-8")
    (pkg / "uses.py").write_text(_USES, encoding="utf-8")
    return tmp_path


def _edge(file: str, name: str) -> NameEdge:
    return NameEdge(_CALLS, NodeKey(_MODULE, file, ec.MODULE_START_LINE), name)


def test_oracle_captures_first_party_calls(repo: Path) -> None:
    trees, _files = parse_py_trees(repo)
    fp = first_party_symbols(trees)
    oracle = oracle_call_edges(trees, fp)

    assert _edge("pkg/core.py", "helper") in oracle
    assert _edge("pkg/core.py", "Widget") in oracle
    assert _edge("pkg/core.py", "run") in oracle
    # (H) build is defined but never called -> never a callee edge.
    assert _edge("pkg/core.py", "build") not in oracle
    # (H) uses.py references symbols but calls none of them.
    assert not any(e.source.file == "pkg/uses.py" for e in oracle)


# (H) A property is invoked by a bare attribute read (getter, Load) or write
# (H) (setter, Store) with no parens; both are descriptor-method calls cgr emits a
# (H) CALLS edge for, so the oracle counts both. A `del` (deleter) is not a
# (H) retrieval call, and a bare method reference (no parens) is not a call.
_PROPS = """\
class Model:
    @property
    def related_model(self):
        return 1

    @cached_property
    def output_field(self):
        return 2

    @property
    def slot(self):
        return 4

    @slot.setter
    def slot(self, v):
        self._slot = v

    @property
    def gone(self):
        return 5

    def plain(self):
        return 3

    def user(self):
        a = self.related_model
        b = self.output_field
        cb = self.plain
        return a, b, cb

    def writer(self, x):
        self.slot = x
        del self.gone
"""


def test_oracle_captures_property_access_as_calls(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "m.py").write_text(_PROPS, encoding="utf-8")
    trees, _files = parse_py_trees(tmp_path)
    fp = first_party_symbols(trees)
    props = first_party_property_names(trees)

    assert props == {"related_model", "output_field", "slot", "gone"}
    oracle = oracle_call_edges(trees, fp, props)
    # (H) getter read (Load) and setter write (Store) both count.
    assert _edge("pkg/m.py", "related_model") in oracle
    assert _edge("pkg/m.py", "output_field") in oracle
    assert _edge("pkg/m.py", "slot") in oracle
    # (H) `cb = self.plain` is a bound-method reference, not a call.
    assert _edge("pkg/m.py", "plain") not in oracle
    # (H) `del self.gone` (Del) is the only access of gone and is not a call.
    assert _edge("pkg/m.py", "gone") not in oracle


@needs_rg
def test_grep_name_overincludes_vs_oracle(repo: Path) -> None:
    trees, files = parse_py_trees(repo)
    fp = first_party_symbols(trees)
    oracle = oracle_call_edges(trees, fp)
    grep_name = grep_call_edges(repo, fp, files, ec.GrepMode.NAME)

    # (H) bare import/alias of helper in uses.py is a grep false positive.
    assert _edge("pkg/uses.py", "helper") in grep_name
    assert _edge("pkg/uses.py", "helper") not in oracle
    # (H) build's definition site mentions its name though it is never called.
    assert _edge("pkg/core.py", "build") in grep_name
    assert _edge("pkg/core.py", "build") not in oracle


@needs_rg
def test_grep_call_excludes_bare_reference_but_flags_def_site(repo: Path) -> None:
    trees, files = parse_py_trees(repo)
    fp = first_party_symbols(trees)
    grep_call = grep_call_edges(repo, fp, files, ec.GrepMode.CALL)

    # (H) `def build():` matches NAME( -> grep cannot tell a def from a call.
    assert _edge("pkg/core.py", "build") in grep_call
    # (H) `ALIAS = helper` is not followed by ( -> the call-pattern excludes it.
    assert _edge("pkg/uses.py", "helper") not in grep_call


def test_score_retrieval_computes_prf() -> None:
    e1, e2, e3 = _edge("a.py", "f"), _edge("a.py", "g"), _edge("b.py", "h")
    oracle = {e1, e2, e3}
    retrieved = {e1, e2, _edge("c.py", "x")}  # (H) tp=2, fp=1, fn=1
    result = score_retrieval([(ec.RetrievalCondition.GRAPH.value, retrieved)], oracle)
    row = next(
        r for r in result.rows if r["label"] == ec.RetrievalCondition.GRAPH.value
    )
    assert (row["tp"], row["fp"], row["fn"]) == (2, 1, 1)
    assert row["precision"] == round(2 / 3, ec.ROUND_DIGITS)
    assert row["recall"] == round(2 / 3, ec.ROUND_DIGITS)


@needs_rg
def test_grep_preserves_colon_in_path(repo: Path) -> None:
    # (H) a .py file whose name contains a colon must keep its full path; the
    # (H) ripgrep output separator must not be confused with a path colon.
    (repo / "pkg" / "od:d.py").write_text(
        "from pkg.core import helper\n\nhelper()\n", encoding="utf-8"
    )
    trees, files = parse_py_trees(repo)
    fp = first_party_symbols(trees)
    grep_name = grep_call_edges(repo, fp, files, ec.GrepMode.NAME)

    assert _edge("pkg/od:d.py", "helper") in grep_name


def test_cgr_call_edges_smoke(repo: Path) -> None:
    trees, _files = parse_py_trees(repo)
    fp = first_party_symbols(trees)
    cgr = cgr_call_edges(repo, repo.name, fp)

    assert isinstance(cgr, set)
    # (H) cgr resolves the intra-module first-party call helper() in core.py.
    assert _edge("pkg/core.py", "helper") in cgr
