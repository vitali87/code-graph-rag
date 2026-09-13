from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_MODELS = (
    "class Base:\n    pass\n\n"
    "class Alternate:\n    pass\n\n"
    "class Child(Base, Alternate):\n    pass\n\n"
    "def helper():\n    return 1\n"
)


def _updater(root: Path, store: _StatefulIngestor) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=root.name,
    )


@pytest.fixture
def rehydrated_updater(tmp_path: Path) -> GraphUpdater:
    store = _StatefulIngestor()
    for project in ("proj", "proj_extra"):
        root = tmp_path / project
        root.mkdir()
        (root / "models.py").write_text(_MODELS, encoding="utf-8")
        _updater(root, store).run(force=True)

    root = tmp_path / "proj"
    (root / "caller.py").write_text(
        "from models import helper\n\ndef caller():\n    return helper()\n",
        encoding="utf-8",
    )
    updater = _updater(root, store)
    updater.run()
    assert {path.name for path, _language in updater._parsed_files} == {"caller.py"}
    assert (cs.NodeLabel.FUNCTION, "proj_extra.models.helper") in store.nodes
    assert (
        cs.NodeLabel.FUNCTION,
        "proj.caller.caller",
        cs.RelationshipType.CALLS,
        cs.NodeLabel.FUNCTION,
        "proj.models.helper",
    ) in store.edges
    return updater


def test_incremental_registry_contains_only_the_requested_project(
    rehydrated_updater: GraphUpdater,
) -> None:
    assert set(rehydrated_updater.function_registry.keys()) == {
        "proj.models.Base",
        "proj.models.Alternate",
        "proj.models.Child",
        "proj.models.helper",
        "proj.caller.caller",
    }


def test_incremental_modules_contain_only_the_requested_project(
    rehydrated_updater: GraphUpdater,
) -> None:
    assert rehydrated_updater._rehydrated_module_qns == {
        "proj.models",
        "proj.caller",
    }


def test_incremental_inheritance_contains_only_the_requested_project(
    rehydrated_updater: GraphUpdater,
) -> None:
    assert rehydrated_updater.factory.definition_processor.class_inheritance == {
        "proj.models.Child": ["proj.models.Base", "proj.models.Alternate"],
    }


def test_inheritance_scope_applies_to_child_not_base() -> None:
    store = _StatefulIngestor()
    for qualified_name in ("proj.Child", "other.Base", "other.Child", "proj.Base"):
        store.ensure_node_batch(
            cs.NodeLabel.CLASS, {cs.KEY_QUALIFIED_NAME: qualified_name}
        )
    for child, base in (("proj.Child", "other.Base"), ("other.Child", "proj.Base")):
        store.ensure_relationship_batch(
            (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, child),
            cs.RelationshipType.INHERITS,
            (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, base),
            {cs.KEY_BASE_INDEX: 0},
        )

    assert store.fetch_all(
        cs.CYPHER_ALL_INHERITS, {cs.KEY_PROJECT_PREFIX: "proj."}
    ) == [
        {
            cs.KEY_CHILD_QN: "proj.Child",
            cs.KEY_BASE_QN: "other.Base",
            cs.KEY_BASE_INDEX: 0,
        }
    ]
