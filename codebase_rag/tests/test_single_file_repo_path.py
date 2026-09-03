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

    def test_single_file_run_does_not_hide_a_sibling_edit_from_the_next_run(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        """The merged cache must not be stamped with this run's instant.

        The merge keeps the previous run's hashes for siblings. Restamping the
        cache to now would assert those hashes were observed now, so a sibling
        edited BEFORE the single-file run satisfies `file_mtime <=
        cache_mtime` while its cached hash still matches -- and the next
        project run reports "already in sync" and never indexes the edit.

        Ordering matters: the edit must precede the single-file run. An edit
        made afterwards is newer than any stamp and is safe either way, so a
        test written that way passes on the broken code too.
        """
        repo = self._three_module_project(tmp_path)
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
        ).run()

        (repo / "module_b.py").write_text(
            "def renamed_b():\n    pass\n", encoding="utf-8"
        )
        GraphUpdater(
            ingestor=_MockIngestor(),
            repo_path=repo / "module_a.py",
            parsers=parsers,
            queries=queries,
        ).run()

        third = _MockIngestor()
        GraphUpdater(
            ingestor=third,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
        ).run()

        assert "scoped.module_b.renamed_b" in get_node_names(third, "Function"), (
            "the sibling's edit never reached the graph: the single-file run "
            "stamped the merged cache with its own instant, so the project "
            "run treated the edited file as already in sync"
        )

    def test_single_file_run_keeps_lombok_stale_siblings_in_the_cache(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        """The merge must read the cache as found, not after the stale pop.

        `_process_files` pops `_delombok_stale_keys` from `old_hashes` before
        the merge sees it. A single-file run does not commit the delombok
        state, so it must not act on that pop either: merging over the popped
        dict DROPS those siblings from the cache, and a dropped key takes the
        `is_new` path next run, skipping delete-before-reingest.

        The stale set is DERIVED, not assigned: `_collect_delombok_state`
        recomputes both delombok fields from the on-disk state file during
        `run`, so setting the attributes on the updater beforehand is
        overwritten and the test would pass on broken code too. Planting a
        state file that names a key the current (empty) overlay lacks is what
        genuinely makes `_delombok_state_changed` true and puts that key in
        `_delombok_stale_keys`.
        """
        repo = self._three_module_project(tmp_path)
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
        ).run()

        (repo / cs.DELOMBOK_STATE_FILENAME).write_text(
            json.dumps({"identity": "stale", "keys": ["module_b.py"], "lombok": "x"}),
            encoding="utf-8",
        )

        second = GraphUpdater(
            ingestor=_MockIngestor(),
            repo_path=repo / "module_a.py",
            parsers=parsers,
            queries=queries,
        )
        second.run()
        assert second._delombok_stale_keys == {"module_b.py"}, (
            "fixture guard: the planted state file must make module_b.py "
            "stale, or this test cannot see the defect it exists for"
        )

        cache = json.loads((repo / cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8"))
        assert "module_b.py" in cache, (
            "a Lombok-stale sibling was dropped from the cache by a run that "
            "does not commit the delombok state; the next run then treats it "
            "as new and skips delete-before-reingest"
        )

    @pytest.mark.parametrize("force", [False, True])
    def test_single_file_run_keeps_sibling_hashes_whatever_force_says(
        self, tmp_path: Path, mock_ingestor: MagicMock, force: bool
    ) -> None:
        """`force` must not empty the snapshot the single-file merge reads.

        `force=True` sets `old_hashes = {}` so the run RE-PARSES what it
        walked. A single-file run walked one file, so that says nothing about
        the siblings it never looked at -- but the merge snapshot was taken
        from `old_hashes`, so the commit persisted a cache holding only the
        target and erased every sibling's hash.

        Parametrised rather than written for `force=True` alone: the unforced
        case is the control. It passed before this fix and must keep passing,
        which is what shows the repair is about `force` specifically and not a
        blanket change to how the merge reads the cache.
        """
        repo = self._three_module_project(tmp_path)
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
        ).run()

        before = json.loads((repo / cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8"))
        assert "module_b.py" in before, (
            "fixture guard: the project run must have cached the sibling, or "
            "this test cannot observe it being erased"
        )

        GraphUpdater(
            ingestor=_MockIngestor(),
            repo_path=repo / "module_a.py",
            parsers=parsers,
            queries=queries,
        ).run(force=force)

        cache = json.loads((repo / cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8"))
        assert "module_b.py" in cache, (
            f"run(force={force}) on a single file erased the sibling's hash: "
            "the next project run then treats every sibling as new, skipping "
            "delete-before-reingest and duplicating their entities"
        )
        assert "module_a.py" in cache, (
            "the run's own file must still be recorded; the merge must add to "
            "the previous cache, not be replaced by it"
        )

    @pytest.mark.parametrize(
        ("force", "drop_cache"),
        [(False, False), (True, False), (False, True)],
        ids=["cache-names-target", "forced", "cacheless"],
    )
    def test_single_file_run_deletes_the_target_before_reingest(
        self,
        tmp_path: Path,
        mock_ingestor: MagicMock,
        force: bool,
        drop_cache: bool,
    ) -> None:
        """An already-indexed target must be cleared before it is re-parsed.

        `force=True` empties `old_hashes`, so the hash cache can no longer say
        whether the target is already in the graph. With `preexisting_paths`
        left empty on this path, `is_new` came out True for a file the
        previous parse had indexed, so the run skipped
        `_delete_module_entities` and stacked a second parse on top of the
        first -- a renamed-away function keeping its old node and its inbound
        CALLS/REFERENCES edges beside the fresh ones.

        Asserts the DELETE is issued rather than checking the cache: the cache
        is written identically either way, so a cache assertion is satisfied
        by both the working and the broken code.

        The deciding axis is whether the cache can NAME THE TARGET, and
        `force` is only one of two ways to make it not: a missing or evicted
        cache leaves `old_hashes` empty exactly as `force` does. So the cases
        are (a) the cache names it -- the control, which reaches the delete
        through `old_hashes` and must keep doing so; (b) `force` empties it;
        (c) the cache is gone, which is reachable WITHOUT `force` on a fresh
        clone of an already-indexed repo, since the cache lives in the working
        tree and the graph does not. Parametrising on `force` alone left (c)
        invisible and a guard covering only `force` passed (#1619 review,
        round 4).
        """
        repo = self._three_module_project(tmp_path)
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
        ).run()

        # A real second run queries a graph that still holds the first run's
        # modules; the fake sink has to say so or nothing is "pre-existing"
        # and the test could not tell the two paths apart.
        cache_path = repo / cs.HASH_CACHE_FILENAME
        assert cache_path.is_file(), (
            "fixture guard: the project run must have written a hash cache, "
            "or the cacheless case below would be indistinguishable from it"
        )
        if drop_cache:
            cache_path.unlink()

        second = _MockIngestor()
        second.fetch_all.return_value = [
            {cs.KEY_PATH: "module_a.py"},
            {cs.KEY_PATH: "module_b.py"},
        ]
        GraphUpdater(
            ingestor=second,
            repo_path=repo / "module_a.py",
            parsers=parsers,
            queries=queries,
        ).run(force=force)

        deleted = {
            call.args[1][cs.KEY_PATH]
            for call in second.execute_write.call_args_list
            if call.args and call.args[0] == cs.CYPHER_DELETE_MODULE
        }
        assert "module_a.py" in deleted, (
            f"run(force={force}) with drop_cache={drop_cache} re-parsed an "
            "already-indexed file without deleting its previous entities "
            "first, so the old parse's nodes and edges survive alongside the "
            "new ones"
        )
