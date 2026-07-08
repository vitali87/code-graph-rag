# (H) C++17 nested namespace syntax (`namespace a::b {`) must produce the same
# (H) qualified names as classic nesting (`namespace a { namespace b {`).
# (H) The namespace walk used to keep the literal `a::b` as one qn segment
# (H) while the FQN scope path split it into `a.b`, so one class got TWO nodes
# (H) and out-of-line method callers resolved to the segment variant that the
# (H) methods were not registered under, dropping their CALLS as phantom
# (H) free-function callers (issue #652: the dominant remaining family on
# (H) souffle, which uses `namespace souffle::ast` throughout).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

HDR_MODERN = """
#pragma once
namespace souffle::ast {
class Program {
public:
    int getTypes() const;
};
int toPtr(int x);
}
"""

CPP_MODERN = """
#include "Program.h"
namespace souffle::ast {
int toPtr(int x) { return x; }
int Program::getTypes() const { return toPtr(1); }
}
"""


def _classic(src: str) -> str:
    return src.replace(
        "namespace souffle::ast {", "namespace souffle { namespace ast {"
    ).replace("}\n", "}\n}\n", 1)


def _index(repo: Path, hdr: str, cpp: str, mock_ingestor: MagicMock) -> None:
    (repo / "Program.h").write_text(hdr)
    (repo / "Program.cpp").write_text(cpp)
    run_updater(repo, mock_ingestor)


def _entity_qns(mock_ingestor: MagicMock, project: str) -> set[tuple[str, str]]:
    qns = set()
    for c in mock_ingestor.ensure_node_batch.call_args_list:
        label = str(c.args[0])
        if label in ("Class", "Method", "Function"):
            qn = str(c.args[1]["qualified_name"]).replace(project, "PROJ", 1)
            qns.add((label, qn))
    return qns


def test_modern_and_classic_namespace_nesting_agree(
    tmp_path: Path,
) -> None:
    from codebase_rag.tests.conftest import _MockIngestor, create_and_run_updater

    modern_repo = tmp_path / "modern"
    classic_repo = tmp_path / "classic"
    modern_repo.mkdir()
    classic_repo.mkdir()
    (modern_repo / "Program.h").write_text(HDR_MODERN)
    (modern_repo / "Program.cpp").write_text(CPP_MODERN)
    (classic_repo / "Program.h").write_text(_classic(HDR_MODERN))
    (classic_repo / "Program.cpp").write_text(_classic(CPP_MODERN))

    modern_ing = _MockIngestor()
    create_and_run_updater(modern_repo, modern_ing)
    classic_ing = _MockIngestor()
    create_and_run_updater(classic_repo, classic_ing)

    modern = _entity_qns(modern_ing, "modern")
    classic = _entity_qns(classic_ing, "classic")
    assert modern == classic, modern.symmetric_difference(classic)


def test_out_of_line_caller_binds_under_modern_namespace(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, HDR_MODERN, CPP_MODERN, mock_ingestor)

    node_keys = {
        (str(c.args[0]), c.args[1].get("qualified_name"))
        for c in mock_ingestor.ensure_node_batch.call_args_list
    }
    calls = [
        call
        for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
        if str(call.args[2][2]).endswith(".toPtr")
        and "getTypes" in str(call.args[0][2])
    ]
    assert calls
    for call in calls:
        from_label, _, from_qn = call.args[0]
        assert str(from_label) == cs.NodeLabel.METHOD.value, call.args
        assert (str(from_label), from_qn) in node_keys, call.args
