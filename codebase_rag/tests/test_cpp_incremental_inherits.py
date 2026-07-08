# (H) Incremental runs rehydrate the function registry from the graph but used
# (H) to leave simple_name_lookup (and the qn->file map behind _is_cpp_defined)
# (H) empty for unchanged files. Deferred C++ INHERITS resolution runs after
# (H) rehydration, so a re-parsed file inheriting from a class in an UNCHANGED
# (H) header could not find the base and the edge silently disappeared from the
# (H) graph on every incremental update (PR #663 review finding).
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

BASE_HDR = """
#pragma once
namespace ast {
class Base {
public:
    virtual ~Base() = default;
};
}
"""

DERIVED_HDR = """
#pragma once
#include "Base.h"
namespace ast {
class Derived : public Base {
public:
    int arity() const { return 1; }
};
}
"""


def _index(store: _StatefulIngestor, repo: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store, repo_path=repo, parsers=parsers, queries=queries
    ).run(force=force)


def _inherits_edges(store: _StatefulIngestor) -> set[tuple[str, str]]:
    return {
        (str(from_val), str(to_val))
        for from_label, from_val, rel_type, to_label, to_val in store.edges
        if rel_type == cs.RelationshipType.INHERITS.value
    }


def test_incremental_reparse_keeps_cross_header_inherits(temp_repo: Path) -> None:
    (temp_repo / "Base.h").write_text(BASE_HDR)
    (temp_repo / "Derived.h").write_text(DERIVED_HDR)
    project = temp_repo.name
    expected = (
        f"{project}.Derived.ast.Derived",
        f"{project}.Base.ast.Base",
    )

    store = _StatefulIngestor()
    _index(store, temp_repo, force=True)
    assert expected in _inherits_edges(store)

    # (H) A trailing comment changes the hash but not the AST, so only
    # (H) Derived.h re-parses; Base.h stays rehydration-only.
    derived = temp_repo / "Derived.h"
    derived.write_text(DERIVED_HDR + "// touched\n")
    _index(store, temp_repo, force=False)
    assert expected in _inherits_edges(store)
