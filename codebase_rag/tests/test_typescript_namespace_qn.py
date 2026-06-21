# (H) A class declared inside a TypeScript `namespace` must carry the namespace
# (H) in its qualified name (proj...geo.Widget), like a nested function does.
# (H) The class FQN scope walk listed the wrong node type ("namespace_definition"
# (H) instead of the grammar's "internal_module"), so it skipped the namespace
# (H) and produced an unscoped qn that collides with a top-level same-named type.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import KEY_QUALIFIED_NAME, NodeLabel
from codebase_rag.tests.conftest import create_and_run_updater, get_nodes

_TS = """\
export namespace geo {
    export class Widget {
        build(): number { return 1; }
    }
}

export class Widget {
    other(): number { return 2; }
}
"""


def test_typescript_namespace_class_qn_includes_namespace(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "ts_ns"
    project.mkdir()
    (project / "lib.ts").write_text(_TS, encoding="utf-8")
    create_and_run_updater(project, mock_ingestor, skip_if_missing="typescript")

    class_qns = {
        str(node[0][1].get(KEY_QUALIFIED_NAME))
        for node in get_nodes(mock_ingestor, NodeLabel.CLASS)
    }
    # (H) The namespaced class and the top-level class must be distinct nodes.
    assert "ts_ns.lib.geo.Widget" in class_qns, class_qns
    assert "ts_ns.lib.Widget" in class_qns, class_qns
