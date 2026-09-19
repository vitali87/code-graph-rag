"""An incremental requeue rehydrates the recorded module owner (issue #1935).

The requeue paths rebuilt a pending type fact's owner from the file path with
`base_module_qn`, which runs BEFORE disambiguation and so cannot reproduce a
suffixed module name. With the owner recomputed as the bare name, the
resolver's same-module scope finds the OTHER file's `Widget` instead of the
suffixed module's own, and the graph gains an `OF_TYPE` / `RETURNS` edge a
clean index of the same source never has.

Both fixtures drive an INCREMENTAL run against a store that already holds
the nodes, with a fresh updater over that store (the shape of a real next
run; over an empty store everything is re-parsed and the requeue is never
reached). The file touched is an UNRELATED third one: touching either
same-stem sibling re-parses both (the stem-flux rule, #1569), and the
suffixed module imports nothing, so only a third file leaves it to the
requeue path. The wrong owner then resolves the name through the same-module
scope to the bare module's `Widget`, and the graph carries a second edge.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

# A `.d.ts` beside its `.ts` yields the bare name deterministically (#1720):
# the declaration owns `proj.lib.d.ts`. Its return type and field name ITS
# OWN Widget, resolved through the same-module scope.
_TS = {
    "other.ts": "export const X = 1;\n",
    "lib.ts": "export class Widget {}\nexport function impl(): number { return 1; }\n",
    "lib.d.ts": (
        "declare class Widget {}\n"
        "export declare function make(): Widget;\n"
        "declare class Holder { w: Widget; }\n"
    ),
}
# Two files sharing a stem: the later one in the ascending walk takes the
# extension suffix, so `settings.py` owns `proj.settings.py` and its
# parameter's Widget is its own.
_PY = {
    "other.py": "X = 1\n",
    "settings.c": "struct Widget { int a; };\n",
    "settings.py": "class Widget:\n    pass\n\ndef use(w: Widget) -> int:\n    return 0\n",
}

Edge = tuple[str, str, str]


def _write(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for name, src in files.items():
        (repo / name).write_text(src)


def _updater(repo: Path, store: _StatefulIngestor) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(["+parameters", "+fields"]),
    )


def _typed_edges(store: _StatefulIngestor) -> set[Edge]:
    wanted = {cs.RelationshipType.OF_TYPE.value, cs.RelationshipType.RETURNS.value}
    return {(str(s), r, str(t)) for _sl, s, r, _tl, t in store.edges if r in wanted}


def _clean(tmp_path: Path, files: dict[str, str]) -> set[Edge]:
    repo = tmp_path / "clean" / "proj"
    _write(repo, files)
    store = _StatefulIngestor()
    _updater(repo, store).run(force=True)
    return _typed_edges(store)


def _incremental(tmp_path: Path, files: dict[str, str], touch: str) -> set[Edge]:
    repo = tmp_path / "inc" / "proj"
    _write(repo, files)
    store = _StatefulIngestor()
    _updater(repo, store).run(force=True)
    (repo / touch).write_text(files[touch] + "\n")
    _updater(repo, store).run(force=False)
    return _typed_edges(store)


def test_returns_and_field_of_type_keep_the_declarations_own_widget(
    tmp_path: Path,
) -> None:
    clean = _clean(tmp_path, _TS)
    own = "proj.lib.d.ts.Widget"
    assert ("proj.lib.d.ts.make", cs.RelationshipType.RETURNS.value, own) in clean
    assert ("proj.lib.d.ts.Holder.w", cs.RelationshipType.OF_TYPE.value, own) in clean
    assert _incremental(tmp_path, _TS, "other.ts") == clean


def test_parameter_of_type_keeps_the_suffixed_modules_own_widget(
    tmp_path: Path,
) -> None:
    clean = _clean(tmp_path, _PY)
    own = "proj.settings.py.Widget"
    assert ("proj.settings.py.use.0", cs.RelationshipType.OF_TYPE.value, own) in clean
    assert _incremental(tmp_path, _PY, "other.py") == clean


class _ModuleRows:
    """A query-capable store answering only the module-path read."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.reads = 0

    def fetch_all(self, query: str, params: dict | None = None) -> list[dict]:
        assert query == cs.CYPHER_ALL_MODULE_PATHS_INTERNAL
        self.reads += 1
        return list(self.rows)

    def execute_write(self, query: str, params: dict | None = None) -> None:
        raise AssertionError("no writes expected")


def test_the_recorded_owner_map_is_scoped_to_this_project(tmp_path: Path) -> None:
    """Another project's module may record the SAME relative path; keyed on
    the path alone it would win. Inline modules carry no file and are skipped.
    A path the graph does not hold falls back to the bare derivation."""
    store = _ModuleRows(
        [
            {cs.KEY_QUALIFIED_NAME: "proj.settings.py", cs.KEY_PATH: "settings.py"},
            {cs.KEY_QUALIFIED_NAME: "other.settings", cs.KEY_PATH: "settings.py"},
            {
                cs.KEY_QUALIFIED_NAME: "proj.inline",
                cs.KEY_PATH: f"{cs.INLINE_MODULE_PATH_PREFIX}x",
            },
        ]
    )
    updater = GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=tmp_path / "proj",
        parsers={},
        queries={},
    )
    assert updater.project_name == "proj"
    assert updater._recorded_module_qn("settings.py") == "proj.settings.py"
    assert updater._recorded_module_qn("unknown.py") == "proj.unknown"
    # Read once per rehydration, not per fact.
    assert store.reads == 1
