import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import (
    _MockIngestor,
    get_node_names,
    get_relationships,
    run_updater,
)


@pytest.fixture
def cpp_single_file(temp_repo: Path) -> Path:
    test_file = temp_repo / "cmGlobalFastbuildGenerator.cxx"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <map>
#include <set>
#include <string>

static std::map<std::string, std::string> const compilerIdToFastbuildFamily = {
    {"GNU", "gcc"},
    {"Clang", "clang"},
};

static std::set<std::string> const supportedLanguages = {
    "C",
    "CXX",
};

template <class T>
T generateAlias(std::string const& name) { return T(); }

static void helperFunc() {}

class FastbuildTarget {
public:
    void GenerateAliases();
};

void FastbuildTarget::GenerateAliases() {
    auto alias = generateAlias("test");
}

void freeFunction() {
    helperFunc();
}
""",
    )
    return test_file


@pytest.fixture
def ran_single_file_updater(cpp_single_file: Path, mock_ingestor: MagicMock) -> None:
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_single_file,
        parsers=parsers,
        queries=queries,
    )
    updater.run()


def test_single_file_repo_path_produces_graph(
    ran_single_file_updater: None,
    mock_ingestor: MagicMock,
) -> None:
    functions = get_node_names(mock_ingestor, "Function")
    methods = get_node_names(mock_ingestor, "Method")
    classes = get_node_names(mock_ingestor, "Class")

    assert any("generateAlias" in qn for qn in functions)
    assert any("helperFunc" in qn for qn in functions)
    assert any("freeFunction" in qn for qn in functions)

    assert any("GenerateAliases" in qn for qn in methods)
    assert any("FastbuildTarget" in qn for qn in classes)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")
    assert len(defines_rels) >= 3

    calls_rels = get_relationships(mock_ingestor, "CALLS")
    assert len(calls_rels) >= 1


def test_single_file_repo_path_static_functions(
    ran_single_file_updater: None,
    mock_ingestor: MagicMock,
) -> None:
    functions = get_node_names(mock_ingestor, "Function")

    assert any("helperFunc" in qn for qn in functions), (
        f"Static function helperFunc not found. Functions: {functions}"
    )

    assert any("generateAlias" in qn for qn in functions), (
        f"Template function generateAlias not found. Functions: {functions}"
    )


def test_single_file_repo_path_out_of_class_methods(
    ran_single_file_updater: None,
    mock_ingestor: MagicMock,
) -> None:
    methods = get_node_names(mock_ingestor, "Method")
    defines_method_rels = get_relationships(mock_ingestor, "DEFINES_METHOD")

    assert any("GenerateAliases" in qn for qn in methods), (
        f"Out-of-class method GenerateAliases not found. Methods: {methods}"
    )
    assert len(defines_method_rels) >= 1


def test_directory_repo_path_still_works(
    temp_repo: Path,
    mock_ingestor: MagicMock,
) -> None:
    project = temp_repo / "normal_project"
    project.mkdir()
    (project / "main.cpp").write_text(
        encoding="utf-8",
        data="""
void doStuff() {}
int main() { doStuff(); return 0; }
""",
    )

    run_updater(project, mock_ingestor)

    functions = get_node_names(mock_ingestor, "Function")
    assert any("doStuff" in qn for qn in functions)
    assert any("main" in qn for qn in functions)


class TestSingleFileRunScope:
    """A single-file run must speak only for the file it walked (#1619).

    `GraphUpdater(repo_path=<a file>)` walks exactly one file, but the
    reconciliation that follows treats the walked set as the project's whole
    set. So the run deletes every module the hash cache names, replaces the
    cache with its single entry, and writes an EMPTY directory-mtime record
    (single-file collection returns before the walk that populates it, so
    there is nothing to truncate -- the previous project walk's record is
    wiped outright).

    The principle is already stated in `run()`, where the exclusion-state
    stamp is guarded by `if self._single_file is None` because "a single-file
    run walks one file and cannot speak for the project's exclusion set".
    These tests extend it to deletions, the hash cache and the mtime record.

    Latent today: `cli.py` and `mcp/tools.py` always pass a directory. The
    constructor accepts a file without complaint, so the next caller wanting a
    targeted re-index would silently drop the rest of the repository.
    """

    @staticmethod
    def _three_module_project(tmp_path: Path) -> Path:
        repo = tmp_path / "scoped"
        repo.mkdir()
        (repo / "__init__.py").touch()
        (repo / "module_a.py").write_text(
            "class Alpha:\n    def greet(self):\n        return 1\n", encoding="utf-8"
        )
        (repo / "module_b.py").write_text("def func_b():\n    pass\n", encoding="utf-8")
        return repo

    @staticmethod
    def _swept_module_keys(ingestor: MagicMock) -> set[str]:
        """Modules deleted and NOT re-added -- i.e. genuinely swept.

        `CYPHER_DELETE_MODULE` serves two different purposes, so counting the
        query alone would flag correct behaviour. The reconciliation path
        issues it to sweep a file that is gone; the changed-file path issues
        it for a file that still exists, to clear the previous parse's
        entities immediately before `_process_single_file` re-adds them
        (graph_updater.py:2731). Only the first is a deletion, and the
        difference is visible in whether a Module node is written afterwards.
        """
        deleted = {
            call.args[1][cs.KEY_PATH]
            for call in ingestor.execute_write.call_args_list
            if call.args and call.args[0] == cs.CYPHER_DELETE_MODULE
        }
        readded = {
            call[0][1][cs.KEY_PATH]
            for call in ingestor.ensure_node_batch.call_args_list
            if call[0][0] == cs.NodeLabel.MODULE and cs.KEY_PATH in call[0][1]
        }
        return deleted - readded

    def _build_then_single_file(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> tuple[Path, MagicMock]:
        """Full build, then a single-file run on module_a with a fresh ingestor.

        The second ingestor must satisfy `QueryProtocol`: the deletion is
        gated on `isinstance(self.ingestor, QueryProtocol)`, so a plain
        `MagicMock` makes the defect silently invisible rather than absent.
        """
        repo = self._three_module_project(tmp_path)
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
        ).run()

        second = _MockIngestor()
        GraphUpdater(
            ingestor=second,
            repo_path=repo / "module_a.py",
            parsers=parsers,
            queries=queries,
        ).run()
        return repo, second

    def test_single_file_run_deletes_no_sibling_modules(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        _repo, second = self._build_then_single_file(tmp_path, mock_ingestor)
        assert self._swept_module_keys(second) == set(), (
            "a single-file run reconciled the whole project and swept every "
            "module the cache named, including the one it was asked to index"
        )

    def test_single_file_run_keeps_sibling_hash_cache_entries(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        repo, _second = self._build_then_single_file(tmp_path, mock_ingestor)
        cache = json.loads((repo / cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8"))
        assert set(cache) == {"__init__.py", "module_a.py", "module_b.py"}, (
            "the cache must keep every sibling's hash; replacing it with the "
            "one walked file makes the next full run treat the rest as new"
        )

    def test_single_file_run_leaves_the_directory_mtime_record_alone(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        """Skipped, not merged: this run collected no directory mtimes at all.

        Distinct from the hash cache, where the single walked file DID yield a
        fresh hash worth recording. Here there is nothing to merge, so the
        only correct action is to leave the previous walk's record untouched.
        """
        repo = self._three_module_project(tmp_path)
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
        ).run()
        mtimes_path = repo / cs.DIR_MTIMES_FILENAME
        before = json.loads(mtimes_path.read_text(encoding="utf-8"))
        assert before, "fixture guard: the full build must record at least one"

        GraphUpdater(
            ingestor=_MockIngestor(),
            repo_path=repo / "module_a.py",
            parsers=parsers,
            queries=queries,
        ).run()

        assert json.loads(mtimes_path.read_text(encoding="utf-8")) == before, (
            "a single-file run records no directory mtimes, so writing its "
            "empty collection wipes the project's record wholesale"
        )
