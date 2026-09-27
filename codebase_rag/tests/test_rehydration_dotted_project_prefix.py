# Every rehydration read is scoped by `qualified_name STARTS WITH
# $project_prefix`; with projects `svc` and `svc.v2` in one graph, `svc.`
# selects `svc.v2`'s rows too, so an incremental run of `svc` registered the
# other project's definitions and modules (issue #1970).
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_MODELS = "class Base:\n    pass\n\n\nclass Child(Base):\n    pass\n\n\ndef helper():\n    return 1\n"


def _updater(root: Path, store: _StatefulIngestor, project: str) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
    )


def _two_projects(tmp_path: Path) -> tuple[_StatefulIngestor, Path]:
    store = _StatefulIngestor()
    for project in ("svc", "svc.v2"):
        root = tmp_path / project
        root.mkdir()
        (root / "api.py").write_text(_MODELS, encoding="utf-8")
        if project == "svc.v2":
            # A file only the longer-named project holds: its rows are the
            # ones a prefix-scoped read admits and nothing else explains.
            (root / "extra.py").write_text("def only_here():\n    return 2\n")
        _updater(root, store, project).run(force=True)
    return store, tmp_path / "svc"


def test_an_incremental_run_rehydrates_its_own_project_only(tmp_path: Path) -> None:
    store, root = _two_projects(tmp_path)
    (root / "caller.py").write_text(
        "from api import helper\n\n\ndef caller():\n    return helper()\n",
        encoding="utf-8",
    )
    updater = _updater(root, store, "svc")
    updater.run()

    assert {path.name for path, _language in updater._parsed_files} == {"caller.py"}
    registry = set(updater.function_registry.keys())
    assert not {qn for qn in registry if qn.startswith("svc.v2.")}
    assert "svc.api.helper" in registry
    assert updater._rehydrated_module_qns == {"svc.api", "svc.caller"}
    inheritance = updater.factory.definition_processor.class_inheritance
    assert inheritance == {"svc.api.Child": ["svc.api.Base"]}
    assert (
        cs.NodeLabel.FUNCTION,
        "svc.caller.caller",
        cs.RelationshipType.CALLS,
        cs.NodeLabel.FUNCTION,
        "svc.api.helper",
    ) in store.edges
    assert "svc.v2.extra.only_here" not in registry
    assert not {
        key
        for key in updater.factory.definition_processor.function_locations
        if str(key[0]).startswith("svc.v2.")
    }
    # The sibling's `api.py` survives this run. Its `extra.py` does not: the
    # orphan prune is prefix ruled and finds no `svc/extra.py` on disk, and
    # a modified `api.py` would take `svc.v2.api` through the module delete
    # too. That is data loss, pre-existing and filed as #1985 with its own
    # test; this PR fixes the reads.
    assert (cs.NodeLabel.FUNCTION, "svc.v2.api.helper") in store.nodes


def test_the_longer_named_project_still_rehydrates_itself(tmp_path: Path) -> None:
    """The control: `svc.v2` owns its own rows under the same rule."""
    store, _root = _two_projects(tmp_path)
    root = tmp_path / "svc.v2"
    (root / "caller.py").write_text(
        "from api import helper\n\n\ndef caller():\n    return helper()\n",
        encoding="utf-8",
    )
    updater = _updater(root, store, "svc.v2")
    updater.run()

    assert {qn for qn in updater.function_registry.keys() if qn.startswith("svc.")} == {
        "svc.v2.api.Base",
        "svc.v2.api.Child",
        "svc.v2.api.helper",
        "svc.v2.caller.caller",
        "svc.v2.extra.only_here",
    }
    assert updater._rehydrated_module_qns == {
        "svc.v2.api",
        "svc.v2.caller",
        "svc.v2.extra",
    }


def test_ownership_degrades_to_the_prefix_when_the_project_list_fails(
    tmp_path: Path,
) -> None:
    """A failed project-list read keeps the previous rule rather than
    dropping every row: the qn under this project's prefix is owned."""
    store, root = _two_projects(tmp_path)
    updater = _updater(root, store, "svc")
    original = store.fetch_all

    def failing(query: str, params=None):  # noqa: ANN001, ANN202
        if (
            query
            == "MATCH (p:Project) RETURN p.name AS name, p.root_path AS root_path ORDER BY p.name"
        ):
            raise RuntimeError("no projects")
        return original(query, params)

    updater.ingestor.fetch_all = failing  # type: ignore[method-assign]
    assert updater._owns("svc.api.helper") is True
    assert updater._owns("svc.v2.api.helper") is True
    assert updater._owns("other.api.helper") is False


def test_the_qn_seed_skips_a_sibling_projects_module(tmp_path: Path) -> None:
    """The seed read is unscoped, and paths are RELATIVE.

    `svc` and `svc.v2` both hold `api.py`, so the sibling's row passes the
    eligible-paths guard and would seed `svc.v2.api` onto THIS project's
    `api.py`, putting a qn another project owns into the disambiguator's map
    (issue #1970).
    """
    store, root = _two_projects(tmp_path)
    (root / "caller.py").write_text("def caller():\n    return 1\n", encoding="utf-8")
    updater = _updater(root, store, "svc")
    updater.run()

    seeded = updater.factory.definition_processor.module_qn_to_file_path
    assert "svc.api" in seeded, seeded
    assert not [qn for qn in seeded if qn.startswith("svc.v2.")], seeded
