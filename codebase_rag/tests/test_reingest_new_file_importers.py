"""A file CREATED by a scoped re-ingest must re-parse its waiting importers (#1682).

`_affected_caller_keys` finds dependents through the graph's INBOUND edges. A
file the call creates has none yet, so an importer that referenced its path
before it existed -- `main.py` importing `./util` written before `util.ts` --
is never re-parsed, and the resolved IMPORTS edge and the calls into the new
module never appear. A clean index produces them, so the scoped path diverges
for exactly the create-then-import order an agent commonly uses.

`_unresolved_importer_keys` closes that by looking from the TARGET side: the
waiting importer's IMPORTS edge points at an unresolved target named after the
new module, which is findable even with no inbound edge.

SCOPE OF THESE TESTS. They drive the query layer with a stubbed `fetch_all`,
which is what can be verified without a live graph: that the right question is
asked, that the answer is wired into the dependent set, and that a failure
degrades rather than raising. The end-to-end claim -- that a real create-then-
reingest now matches a clean index -- needs the integration suite against
Memgraph and is NOT asserted here. A mock ingestor answers no dependents query
at all, so an end-to-end assertion written against one passes or fails for
reasons unrelated to this change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


class _QueryingIngestor:
    """An ingestor that answers `fetch_all`, which MagicMock does not."""

    def __init__(self, rows: list[dict[str, Any]] | Exception) -> None:
        self._rows = rows
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def fetch_all(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.queries.append((query, params))
        if isinstance(self._rows, Exception):
            raise self._rows
        return self._rows

    # Declared on the CLASS, not served by `__getattr__`: a runtime-checkable
    # Protocol's isinstance inspects the class, so a dynamic attribute makes
    # `hasattr` true while `isinstance(..., QueryProtocol)` stays False -- and
    # the lookup under test silently returns [] for the wrong reason.
    def execute_write(self, query: str, params: dict[str, Any] | None = None) -> None:
        return None

    def __getattr__(self, name: str) -> MagicMock:
        return MagicMock()


def _updater(tmp_path: Path, ingestor: object) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,  # type: ignore[arg-type]
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


class TestModuleNamesOffered:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("util.py", {"util"}),
            ("pkg/util.py", {"util", "pkg.util"}),
            ("a/b/deep.ts", {"deep", "a.b.deep"}),
            # A directory-module entry point is named by its DIRECTORY. Its own
            # stem is never written in an import, so offering it matches
            # nothing, and keeping it in the dotted form yields `pkg.pkg`.
            ("pkg/__init__.py", {"pkg"}),
            ("utils/mod.rs", {"utils"}),
            ("a/b/utils/mod.rs", {"utils", "a.b.utils"}),
            ("components/index.ts", {"components"}),
            # `.mts`/`.cts` are TypeScript's ESM/CJS forms, directory entry
            # points exactly as `.mjs`/`.cjs` are. Omitting them derived
            # `pkg.index` and asked for a name no importer ever writes.
            ("pkg/index.mts", {"pkg"}),
            ("pkg/index.cts", {"pkg"}),
            ("a/b/components/index.mts", {"components", "a.b.components"}),
            ("pkg/index.mjs", {"pkg"}),
            ("pkg/index.cjs", {"pkg"}),
            # Not every `.mts` is an entry point: the rule is the STEM plus the
            # extension, so an ordinary module keeps both its spellings.
            ("pkg/util.mts", {"util", "pkg.util"}),
            # A Rust crate root is named by the manifest, not the directory, so
            # it keeps its own stem rather than being folded into the parent.
            ("src/lib.rs", {"lib", "src.lib"}),
            # The directory-module rule is PER LANGUAGE. `index` and `mod` are
            # ordinary Python module names, and treating every such stem as a
            # directory module leaves them with no importable name at all.
            ("index.py", {"index"}),
            ("mod.py", {"mod"}),
            ("pkg/index.py", {"index", "pkg.index"}),
            # At the repository root the directory-module rule has no
            # directory to apply, so the file is named by its own stem: a
            # sibling writes `./index`, and dropping that left a root entry
            # point with no name at all, so its waiters were never re-parsed.
            ("index.ts", {"index"}),
            ("index.js", {"index"}),
            ("index.mts", {"index"}),
            ("__init__.py", {"__init__"}),
            # A root-level `index.py` was never affected: `.py`'s directory
            # stem is `__init__`, so it is an ordinary module and keeps its
            # name through the other branch entirely. Both are asserted so the
            # per-language distinction stays visible at the root too.
        ],
    )
    def test_both_spellings_are_offered(
        self, tmp_path: Path, key: str, expected: set[str]
    ) -> None:
        # The unresolved edge records whichever spelling the source wrote, so
        # matching only one of them misses half the real cases.
        updater = _updater(tmp_path, _QueryingIngestor([]))
        assert set(updater._module_names_for([key])) == expected

    def test_every_js_ts_module_extension_has_a_directory_stem(self) -> None:
        """The forcing function the parametrised cases cannot provide.

        Those cases enumerate the extensions I thought of, so the next
        extension added to the language set is exactly the one they miss --
        which is how `.mts` and `.cts` came to be absent while `.mjs` and
        `.cjs` were present. `JS_TS_MODULE_EXTENSIONS` is the set cgr's own
        directory-index resolution searches, so anything in it that can be a
        directory entry point must have a stem here.

        `.d.*` declaration files are excluded: `PurePosixPath('index.d.mts')`
        has suffix `.mts`, so they are already covered by their own base
        extension and never reach this map under a `.d.x` key.
        """
        missing = {
            ext
            for ext in cs.JS_TS_MODULE_EXTENSIONS
            if not ext.startswith(".d.")
            and cs.DIRECTORY_MODULE_STEM_BY_EXT.get(ext) != cs.JS_INDEX_STEM
        }
        assert not missing, missing


class TestUnresolvedImporterLookup:
    def test_it_asks_for_the_new_module_by_name(self, tmp_path: Path) -> None:
        ingestor = _QueryingIngestor([{cs.KEY_CALLER_PATH: "main.py"}])
        updater = _updater(tmp_path, ingestor)

        assert updater._unresolved_importer_keys(["util.py"]) == ["main.py"]

        query, params = ingestor.queries[-1]
        assert query == cs.CYPHER_UNRESOLVED_IMPORTER_PATHS
        assert params[cs.CYPHER_PARAM_MODULE_NAMES] == ["util"]
        assert params[cs.KEY_PROJECT_PREFIX] == "proj."

    def test_a_failed_read_degrades_rather_than_raising(self, tmp_path: Path) -> None:
        # The pre-existing dependents lookup degrades the same way: a scoped
        # re-ingest that cannot read the graph should do less, not die.
        updater = _updater(tmp_path, _QueryingIngestor(RuntimeError("no graph")))
        assert updater._unresolved_importer_keys(["util.py"]) == []

    def test_no_present_keys_asks_nothing(self, tmp_path: Path) -> None:
        # A delete-only re-ingest creates no module, so there is no waiting
        # importer to look for and no query worth making.
        ingestor = _QueryingIngestor([{cs.KEY_CALLER_PATH: "main.py"}])
        updater = _updater(tmp_path, ingestor)
        assert updater._unresolved_importer_keys([]) == []
        assert ingestor.queries == []


class TestWiredIntoTheDependentSet:
    def test_a_waiting_importer_becomes_a_dependent(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("from util import helper\n", encoding="utf-8")
        (tmp_path / "util.py").write_text("def helper():\n    return 1\n", "utf-8")
        # Rows come back for BOTH queries; the inbound one finds nothing for a
        # file that did not exist, which is the whole defect.
        ingestor = _QueryingIngestor([{cs.KEY_CALLER_PATH: "main.py"}])
        updater = _updater(tmp_path, ingestor)

        dependents = updater._reingest_dependents(
            present={"util.py": tmp_path / "util.py"}, gone={}
        )
        assert "main.py" in dependents

    def test_the_created_file_is_not_its_own_dependent(self, tmp_path: Path) -> None:
        # The query is answered with the created file itself; it is already
        # being re-parsed, so adding it again would be wrong.
        (tmp_path / "util.py").write_text("def helper():\n    return 1\n", "utf-8")
        ingestor = _QueryingIngestor([{cs.KEY_CALLER_PATH: "util.py"}])
        updater = _updater(tmp_path, ingestor)

        dependents = updater._reingest_dependents(
            present={"util.py": tmp_path / "util.py"}, gone={}
        )
        assert dependents == {}
