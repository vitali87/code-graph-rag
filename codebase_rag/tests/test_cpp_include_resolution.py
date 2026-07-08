# (H) C++ #include resolution and its downstream effects (issue #652).
# (H) The naive resolver rooted every include at the project and stripped the
# (H) extension, so `#include "Directive.h"` from Directive.cpp mapped the stem
# (H) to the .cpp's OWN module qn (the header's real qn is the disambiguated
# (H) Directive.h). That poisoned the import map: IMPORTS edges pointed at
# (H) phantom modules (3.9k on souffle), and resolve_class_name's import-map
# (H) step returned a MODULE qn when asked for the class `Directive`, so every
# (H) out-of-line method's calls were attributed to a phantom free-function
# (H) caller (11k dangling CALLS on souffle).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

HDR = """
#pragma once
namespace souffle {
class Directive {
public:
    void print() const;
};
int helper(int x);
}
"""

CPP = """
#include "Directive.h"
namespace souffle {

int helper(int x) { return x + 1; }

void Directive::print() const {
    helper(1);
}

}
"""


def _write_same_stem_fixture(repo: Path) -> None:
    (repo / "Directive.h").write_text(HDR)
    (repo / "Directive.cpp").write_text(CPP)


def _node_keys(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    keys = set()
    for c in mock_ingestor.ensure_node_batch.call_args_list:
        props = c.args[1]
        key = props.get("qualified_name") or props.get("path") or props.get("name")
        keys.add((str(c.args[0]), key))
    return keys


def test_out_of_line_method_calls_attribute_to_method_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write_same_stem_fixture(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    keys = _node_keys(mock_ingestor)
    helper_calls = [
        call
        for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
        if str(call.args[2][2]).endswith(".helper") and ".print" in str(call.args[0][2])
    ]
    assert helper_calls
    for call in helper_calls:
        from_label, _, from_qn = call.args[0]
        assert str(from_label) == cs.NodeLabel.METHOD.value, call.args
        assert ".Directive.print" in str(from_qn), call.args
        assert (str(from_label), from_qn) in keys, call.args


def test_include_imports_edge_targets_real_header_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write_same_stem_fixture(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    keys = _node_keys(mock_ingestor)
    cpp_module = f"{temp_repo.name}.Directive"
    header_module = f"{temp_repo.name}.Directive.h"
    import_targets = {
        str(call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.IMPORTS.value)
        if str(call.args[0][2]) == cpp_module
    }
    assert header_module in import_targets, import_targets
    # (H) The self-import phantom (stem resolved to the includer's own qn) must
    # (H) be gone.
    assert cpp_module not in import_targets, import_targets
    assert (cs.NodeLabel.MODULE.value, header_module) in keys


def test_subdir_include_resolves_via_path_suffix(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) souffle-style layout: includes are written relative to an -I root
    # (H) ("ast/Directive.h"), not the includer or the repo root.
    (temp_repo / "src" / "ast").mkdir(parents=True)
    (temp_repo / "src" / "ast" / "Directive.h").write_text(HDR)
    (temp_repo / "src" / "ast" / "Directive.cpp").write_text(
        CPP.replace('#include "Directive.h"', '#include "ast/Directive.h"')
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    cpp_module = f"{project}.src.ast.Directive"
    header_module = f"{project}.src.ast.Directive.h"
    import_targets = {
        str(call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.IMPORTS.value)
        if str(call.args[0][2]) == cpp_module
    }
    assert header_module in import_targets, import_targets
