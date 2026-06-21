# (H) Covers the L1 eval (evals/cgr_graph.py): cgr emits placeholder MODULE nodes
# (H) for unresolved imports whose path is the dotted import name (e.g.
# (H) "thrift.TTornado"). Those must not be treated as internal import targets when
# (H) scoring IMPORTS, or every "from <pkg>.x import ..." collapses onto them as a
# (H) false positive. Only real in-repo .py modules count as internal.
from __future__ import annotations

from codebase_rag import constants as cs
from evals.cgr_graph import _CapturingIngestor, _to_graph_data

_MODULE = cs.NodeLabel.MODULE.value
_IMPORTS = cs.RelationshipType.IMPORTS.value


def _module(ingestor: _CapturingIngestor, qn: str, path: str) -> None:
    ingestor.ensure_node_batch(
        _MODULE,
        {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_NAME: qn, cs.KEY_PATH: path},
    )


def test_import_placeholder_module_not_scored_as_internal() -> None:
    ingestor = _CapturingIngestor()
    _module(ingestor, "proj.src", "src.py")
    _module(ingestor, "proj.real", "pkg/real.py")
    # (H) Placeholder for an unresolved import: path is the dotted name, not a file.
    _module(ingestor, "proj.placeholder", "proj.placeholder")

    for target in ("proj.real", "proj.placeholder"):
        ingestor.ensure_relationship_batch(
            (_MODULE, cs.KEY_QUALIFIED_NAME, "proj.src"),
            _IMPORTS,
            (_MODULE, cs.KEY_QUALIFIED_NAME, target),
        )

    graph = _to_graph_data(ingestor, "proj")
    import_targets = {e.target_name for e in graph.name_edges if e.rel_type == _IMPORTS}
    assert import_targets == {"pkg/real.py"}, import_targets
