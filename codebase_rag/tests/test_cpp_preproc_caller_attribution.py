# (H) Methods declared inside preprocessor conditionals (#ifdef/#else within a
# (H) class body, souffle's BTreeDelete.h pattern) distort the AST enough that
# (H) Pass 3's structural caller-qn walk diverged from the qn Pass 2 registered
# (H) (dropping the enclosing template class from the chain), so every call
# (H) inside them was attributed to a phantom caller the database drops (issue
# (H) #652). The definition pass now records each ingested function's location
# (H) and call attribution reuses that record instead of re-deriving.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

ENGINE_HDR = """
#pragma once
#include <vector>

namespace ns {
namespace detail {

template <typename T>
class engine {
public:
    class iterator;

    struct node {
        int size() const { return 1; }

#ifdef IS_PARALLEL
        void split(node** root, int idx, std::vector<node*>& locked) {
            grow(root);
            int s = size();
        }
#else
        void split(node** root, int idx) {
            grow(root);
            int s = size();
        }
#endif

        void grow(node** root) {
            split(root, 0);
        }
    };
};

}  // namespace detail
}  // namespace ns
"""


def _node_keys(mock_ingestor: MagicMock) -> set[tuple[str, str | None]]:
    return {
        (str(c.args[0]), c.args[1].get("qualified_name"))
        for c in mock_ingestor.ensure_node_batch.call_args_list
    }


def test_calls_inside_preproc_methods_attach_to_real_nodes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "engine.h").write_text(ENGINE_HDR)
    run_updater(temp_repo, mock_ingestor)

    node_keys = _node_keys(mock_ingestor)
    calls = get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert calls
    for call in calls:
        from_label, _, from_qn = call.args[0]
        assert (str(from_label), from_qn) in node_keys, call.args


def test_preproc_method_callers_keep_full_class_chain(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "engine.h").write_text(ENGINE_HDR)
    run_updater(temp_repo, mock_ingestor)

    grow_calls = [
        call
        for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
        if str(call.args[2][2]).endswith(".grow")
    ]
    assert grow_calls
    for call in grow_calls:
        assert ".engine.node.split" in str(call.args[0][2]), call.args
