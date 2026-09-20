"""A deleted C# file's method return types must leave with its registry rows.

`TypeInferenceEngine.csharp_method_return_types` records `(type, arity)` per
C# method qualified name. `remove_file_from_state` pruned the registry and,
since #1752, the general return-type map, but never this one: after
`run()`, deleting `A.cs` and removing its state, the registry row for
`proj.A.N.K.Self` was gone while the map still held `('K', 0)` (issue #1753).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import _MockIngestor

A_CS = "namespace N\n{\n    public class K\n    {\n        public K Self() { return this; }\n    }\n}\n"
B_CS = "namespace N\n{\n    public class L\n    {\n        public L Twin() { return this; }\n    }\n}\n"


def _create_graph_updater(root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.CSHARP not in parsers:
        pytest.skip("csharp parser not available")
    return GraphUpdater(
        ingestor=_MockIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


def test_a_deleted_csharp_files_return_types_are_forgotten(temp_repo: Path) -> None:
    root = temp_repo / "proj"
    root.mkdir()
    (root / "A.cs").write_text(A_CS, encoding="utf-8")
    (root / "B.cs").write_text(B_CS, encoding="utf-8")
    updater = _create_graph_updater(root)
    updater.run()
    recorded = updater.factory.type_inference.csharp_method_return_types
    self_qn = next((qn for qn in recorded if qn.endswith("K.Self")), None)
    twin_qn = next((qn for qn in recorded if qn.endswith("L.Twin")), None)
    assert self_qn, f"fixture guard: A.cs recorded nothing: {recorded}"
    assert twin_qn, f"fixture guard: B.cs recorded nothing: {recorded}"

    (root / "A.cs").unlink()
    updater.remove_file_from_state(root / "A.cs")

    assert self_qn not in recorded, (
        f"the deleted file's return-type entry survived remove_file_from_state: {recorded}"
    )
    assert twin_qn in recorded, (
        "a sibling's entry was swept along with the deleted file's"
    )
