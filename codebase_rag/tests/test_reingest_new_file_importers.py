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

SCOPE OF THESE TESTS. `TestUnresolvedImporterLookup` drives the query layer with
a stubbed `fetch_all`: that the right question is asked, that the answer is
wired into the dependent set, and that a failure degrades rather than raising.
A MagicMock cannot show more, so an end-to-end assertion written against one
passes or fails for reasons unrelated to the code.

`TestUnresolvedSpecifierWaiters` runs end to end against the stateful in-memory
double, covering BOTH halves of the mechanism: the persisted-row path (#1682,
`import util` records the external-looking target `util`) and the recorded-
specifier path (#1714, a relative `./index` records no row at all). That became
possible only once the double answered these two queries. It previously fell
through to `case _: return []` for both, which reads as "no waiters" and is
indistinguishable from a lookup that never ran -- which is why this file could
originally cover the query layer alone.

Still NOT asserted here: that a real scoped re-ingest against Memgraph produces
the same graph as a clean index. The double is faithful to the queries cgr
issues, not to the database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor


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

        # Selected by identity rather than by position: the lookup now issues
        # a second query for specifier-recorded waiters (issue #1714), so
        # `queries[-1]` names that one and would assert nothing about this.
        params = next(
            p for q, p in ingestor.queries if q == cs.CYPHER_UNRESOLVED_IMPORTER_PATHS
        )
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


class TestUnresolvedSpecifierWaiters:
    """The relative JS/TS case the row-based lookup cannot see (issue #1714).

    `main.js` importing a missing `./index` persists NO IMPORTS edge -- it is
    dropped at flush as an unverifiable internal target -- so the target-side
    query has no row to match. Measured on the parent branch:

        main.js  import { go } from './index'  ->  IMPORTS edges: []
        main.py  import util                   ->  [('proj.main', 'util')]

    The importer records the literal specifier on its Module node instead.
    Driven through a real index against the stateful double rather than a
    stubbed `fetch_all`, because the claim is that the property is WRITTEN
    during a normal parse, which a stub cannot show.
    """

    @staticmethod
    def _index(root: Path, files: dict[str, str]) -> _StatefulIngestor:
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        store = _StatefulIngestor()
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        ).run(force=True)
        return store

    @staticmethod
    def _module(store: _StatefulIngestor, path: str) -> dict[str, Any]:
        return next(
            dict(props)
            for (label, _uid), props in store.nodes.items()
            if label == cs.NodeLabel.MODULE.value and props.get(cs.KEY_PATH) == path
        )

    @staticmethod
    def _updater_on(root: Path, store: _StatefulIngestor) -> GraphUpdater:
        parsers, queries = load_parsers()
        return GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        )

    def test_the_dropped_specifier_is_recorded_on_the_importer(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root, {"main.js": "import { go } from './index';\nexport const r = go;\n"}
        )

        assert self._module(store, "main.js")[cs.KEY_UNRESOLVED_SPECIFIERS] == [
            "./index"
        ]
        # The premise, asserted rather than assumed: there is genuinely no
        # IMPORTS row for the existing lookup to find.
        assert not [edge for edge in store.edges if edge[2] == "IMPORTS"]

    def test_a_specifier_whose_target_exists_is_not_recorded(
        self, tmp_path: Path
    ) -> None:
        """The control that stops this from nominating everything for ever.

        A probe that recorded every relative specifier would pass the test
        above and still be useless, so this drives the same import with the
        target present.
        """
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root,
            {
                "main.js": "import { go } from './index';\nexport const r = go;\n",
                "index.js": "export function go() { return 1; }\n",
            },
        )

        assert self._module(store, "main.js")[cs.KEY_UNRESOLVED_SPECIFIERS] == []

    def test_the_waiter_is_nominated_when_the_file_is_created(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root, {"main.js": "import { go } from './index';\nexport const r = go;\n"}
        )
        (root / "index.js").write_text(
            "export function go() { return 1; }\n", encoding="utf-8"
        )

        keys = self._updater_on(root, store)._unresolved_importer_keys(["index.js"])

        assert keys == ["main.js"]

    def test_a_directory_entry_point_satisfies_the_directory_specifier(
        self, tmp_path: Path
    ) -> None:
        # `./pkg` is satisfied by creating `pkg/index.js`, which is the form a
        # bare directory import resolves to.
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root, {"main.js": "import { go } from './pkg';\nexport const r = go;\n"}
        )
        (root / "pkg").mkdir()
        (root / "pkg" / "index.js").write_text(
            "export function go() { return 1; }\n", encoding="utf-8"
        )

        keys = self._updater_on(root, store)._unresolved_importer_keys(["pkg/index.js"])

        assert keys == ["main.js"]

    def test_an_unrelated_creation_does_not_nominate_the_waiter(
        self, tmp_path: Path
    ) -> None:
        """The specifier must be MATCHED, not merely present.

        Without this, a lookup that returned every module carrying any
        unresolved specifier would pass every test above.
        """
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root, {"main.js": "import { go } from './index';\nexport const r = go;\n"}
        )
        (root / "other.js").write_text("export const x = 1;\n", encoding="utf-8")

        keys = self._updater_on(root, store)._unresolved_importer_keys(["other.js"])

        assert keys == []

    def test_a_specifier_is_resolved_against_the_importers_own_directory(
        self, tmp_path: Path
    ) -> None:
        """`./index` in `sub/main.js` means `sub/index.js`, not `index.js`.

        A match on the specifier text alone would bind the root file, which is
        why the resolution happens against `importer.path` rather than in the
        query.
        """
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root,
            {"sub/main.js": ("import { go } from './index';\nexport const r = go;\n")},
        )
        (root / "index.js").write_text("export const x = 1;\n", encoding="utf-8")
        updater = self._updater_on(root, store)

        assert updater._unresolved_importer_keys(["index.js"]) == []
        assert updater._unresolved_importer_keys(["sub/index.js"]) == ["sub/main.js"]

    def test_a_python_waiter_is_found_through_its_persisted_unresolved_edge(
        self, tmp_path: Path
    ) -> None:
        """The #1682 row path, driven end to end rather than through a stub.

        This was previously unassertable: the stateful double did not answer
        `CYPHER_UNRESOLVED_IMPORTER_PATHS` and fell through to `[]`, so the
        lookup reported no waiters for reasons unrelated to the code. With the
        double answering it, the two halves of the mechanism can be told
        apart -- this one keeps a persisted IMPORTS row (`import util` records
        the external-looking target `util`), where the JS/TS cases above keep
        none at all.
        """
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root, {"main.py": "import util\n\ndef run():\n    return util.helper()\n"}
        )
        # The premise: an unresolved row exists here, unlike the JS/TS case.
        assert [edge for edge in store.edges if edge[2] == "IMPORTS"]

        (root / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")

        keys = self._updater_on(root, store)._unresolved_importer_keys(["util.py"])

        assert keys == ["main.py"]

    def test_a_resolved_import_is_not_reported_as_a_waiter(
        self, tmp_path: Path
    ) -> None:
        """The control for the row path: only UNRESOLVED targets count.

        Without it, a double that returned every importer of anything would
        satisfy the test above.
        """
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root,
            {
                "main.py": "import util\n\ndef run():\n    return util.helper()\n",
                "util.py": "def helper():\n    return 1\n",
            },
        )

        keys = self._updater_on(root, store)._unresolved_importer_keys(["util.py"])

        assert keys == []

    def test_a_specifier_written_with_an_explicit_extension_still_matches(
        self, tmp_path: Path
    ) -> None:
        """`./foo.js` records the extension; the created file arrives as
        `foo.js`. Normalising only one side left them as `foo` and `foo.js`,
        which never met, so the importer was never re-parsed (raised on the
        #1717 review and reproduced before fixing)."""
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root, {"main.js": "import { go } from './foo.js';\nexport const r = go;\n"}
        )
        assert self._module(store, "main.js")[cs.KEY_UNRESOLVED_SPECIFIERS] == [
            "./foo.js"
        ]
        (root / "foo.js").write_text(
            "export function go() { return 1; }\n", encoding="utf-8"
        )

        keys = self._updater_on(root, store)._unresolved_importer_keys(["foo.js"])

        assert keys == ["main.js"]

    def test_a_declaration_file_normalises_past_both_of_its_suffixes(
        self, tmp_path: Path
    ) -> None:
        """`index.d.ts` must reduce to `index`, not `index.d`.

        A single-suffix strip leaves `index.d`, so a created declaration entry
        point would not satisfy `./pkg` either. Found while fixing the
        explicit-extension case rather than reported.

        SCOPE. This asserts NOMINATION -- that the waiting importer is selected
        for re-parsing -- which is the mechanism this file covers. It does not
        assert that the resulting IMPORTS edge lands, and for a declaration
        file today it does not: `base_module_qn` indexes `pkg/index.d.ts` as
        `proj.pkg.index.d` while the resolver looks for `proj.pkg`, so the edge
        is dropped downstream. That is issue #1720, pre-existing and far wider
        than this file (it affects every `.d.ts` in every project). Stated here
        because a reader would otherwise take a green test as proof that
        declaration entry points work end to end.
        """
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root, {"main.ts": "import { go } from './pkg';\nexport const r = go;\n"}
        )
        (root / "pkg").mkdir()
        (root / "pkg" / "index.d.ts").write_text(
            "export declare function go(): number;\n", encoding="utf-8"
        )

        keys = self._updater_on(root, store)._unresolved_importer_keys(
            ["pkg/index.d.ts"]
        )

        assert keys == ["main.ts"]

    def test_a_dotted_basename_is_not_cut_in_half(self, tmp_path: Path) -> None:
        """`./foo.test` must not lose `.test`, which is not a module extension.

        The fallback used to strip whatever suffix a path carried, so the
        specifier became `foo` while the created `foo.test.ts` became
        `foo.test` -- the same asymmetry the normalisation exists to remove,
        reintroduced by the branch that handles everything else (raised on
        #1717).
        """
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root,
            {"main.ts": "import { go } from './foo.test';\nexport const r = go;\n"},
        )
        (root / "foo.test.ts").write_text(
            "export function go() { return 1; }\n", encoding="utf-8"
        )

        keys = self._updater_on(root, store)._unresolved_importer_keys(["foo.test.ts"])

        assert keys == ["main.ts"]

    def test_a_commonjs_require_of_a_missing_target_is_recorded(
        self, tmp_path: Path
    ) -> None:
        """`require('./util')` drops its edge exactly as `import` does.

        The recording hung off the import-statement branch alone, so CommonJS
        importers were never nominated -- and `require` is the older half of
        the JS corpus, not an edge case (raised on #1717).
        """
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root,
            {
                "main.js": "const { go } = require('./util');\nmodule.exports = { go };\n"
            },
        )

        assert self._module(store, "main.js")[cs.KEY_UNRESOLVED_SPECIFIERS] == [
            "./util"
        ]

        (root / "util.js").write_text(
            "function go() { return 1; }\nmodule.exports = { go };\n",
            encoding="utf-8",
        )
        keys = self._updater_on(root, store)._unresolved_importer_keys(["util.js"])

        assert keys == ["main.js"]

    def test_a_re_export_from_a_missing_target_is_recorded(
        self, tmp_path: Path
    ) -> None:
        """`export ... from './util'` takes its own parse branch."""
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(root, {"main.ts": "export { go } from './util';\n"})

        assert self._module(store, "main.ts")[cs.KEY_UNRESOLVED_SPECIFIERS] == [
            "./util"
        ]

        (root / "util.ts").write_text(
            "export function go(): number { return 1; }\n", encoding="utf-8"
        )
        keys = self._updater_on(root, store)._unresolved_importer_keys(["util.ts"])

        assert keys == ["main.ts"]

    def test_a_bare_require_of_a_package_is_not_recorded(self, tmp_path: Path) -> None:
        """The control for the two above: only RELATIVE specifiers qualify.

        A bare `require('lodash')` becomes a real edge to an ExternalModule,
        which the row-based lookup already finds; recording it here would
        nominate the importer on every unrelated creation.
        """
        root = tmp_path / "proj"
        root.mkdir()
        store = self._index(
            root,
            {"main.js": "const _ = require('lodash');\nmodule.exports = { _ };\n"},
        )

        assert self._module(store, "main.js")[cs.KEY_UNRESOLVED_SPECIFIERS] == []

    def test_a_reused_updater_clears_a_specifier_whose_target_now_exists(
        self, tmp_path: Path
    ) -> None:
        """Recording is only half of it; a stale specifier nominates for ever.

        The updater is REUSED across both runs, which is the watcher's shape
        and the only one that exercises the clearing at all: a fresh updater
        builds a fresh ImportProcessor whose specifier map starts empty, so it
        writes `[]` whether or not the previous entry is ever retracted.
        Measured -- with a fresh updater here, deleting the retraction leaves
        every test in this file green.

        The property must go EMPTY rather than merely stop matching, because
        `SET n += props` can update a property but never remove one.
        """
        root = tmp_path / "proj"
        root.mkdir()
        (root / "main.js").write_text(
            "import { go } from './index';\nexport const r = go;\n", encoding="utf-8"
        )
        store = _StatefulIngestor()
        updater = self._updater_on(root, store)
        updater.run(force=True)
        assert self._module(store, "main.js")[cs.KEY_UNRESOLVED_SPECIFIERS] == [
            "./index"
        ]

        (root / "index.js").write_text(
            "export function go() { return 1; }\n", encoding="utf-8"
        )
        updater.run(force=True)

        assert self._module(store, "main.js")[cs.KEY_UNRESOLVED_SPECIFIERS] == []
        assert updater._unresolved_importer_keys(["index.js"]) == []
