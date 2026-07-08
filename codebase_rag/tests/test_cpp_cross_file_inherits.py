# (H) A C++ base class written in another header (`class Aggregator : public
# (H) Argument` with Argument defined in Argument.h) used to be anchored to the
# (H) CHILD's own module qn at parse time with no resolution at all, so every
# (H) cross-file INHERITS edge pointed at a phantom the database drops (issue
# (H) #652: 398 on souffle). Base emission is now deferred until every class is
# (H) registered and resolved namespace-scoped across files; a base that
# (H) resolves nowhere (std::exception) emits no edge rather than a lie.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

ARGUMENT_HDR = """
#pragma once
namespace ast {
class Argument {
public:
    virtual ~Argument() = default;
};
}
"""

AGGREGATOR_HDR = """
#pragma once
#include "Argument.h"
namespace ast {
class Aggregator : public Argument {
public:
    int arity() const { return 1; }
};
}
"""

EXTERNAL_BASE_HDR = """
#pragma once
#include <exception>
namespace ast {
class ParseError : public std::exception {
public:
    int code() const { return 2; }
};
}
"""


def _write_fixture(repo: Path) -> None:
    src = repo / "src"
    src.mkdir()
    (src / "Argument.h").write_text(ARGUMENT_HDR)
    (src / "Aggregator.h").write_text(AGGREGATOR_HDR)
    (src / "ParseError.h").write_text(EXTERNAL_BASE_HDR)


def test_cross_header_base_resolves_to_defining_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write_fixture(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    inherits = get_relationships(mock_ingestor, cs.RelationshipType.INHERITS.value)
    aggregator_edges = [
        call for call in inherits if str(call.args[0][2]).endswith(".ast.Aggregator")
    ]
    assert aggregator_edges
    for call in aggregator_edges:
        assert call.args[2][2] == f"{project}.src.Argument.ast.Argument", call.args


def test_unresolvable_external_base_emits_no_phantom_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write_fixture(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    node_keys = {
        (str(c.args[0]), c.args[1].get("qualified_name"))
        for c in mock_ingestor.ensure_node_batch.call_args_list
    }
    inherits = get_relationships(mock_ingestor, cs.RelationshipType.INHERITS.value)
    for call in inherits:
        to_label, _, to_qn = call.args[2]
        assert (str(to_label), to_qn) in node_keys, call.args


def test_same_file_base_still_emits_inherits(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "bank.h").write_text(
        """
#pragma once
class Account {
public:
    virtual ~Account() = default;
};
class Savings : public Account {
public:
    int rate() const { return 3; }
};
"""
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    inherits = get_relationships(mock_ingestor, cs.RelationshipType.INHERITS.value)
    pairs = {(call.args[0][2], call.args[2][2]) for call in inherits}
    assert (f"{project}.bank.Savings", f"{project}.bank.Account") in pairs, pairs
