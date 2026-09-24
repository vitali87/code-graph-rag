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
# Bodied Rust inline modules share their file's path (bot review on PR
# #1967): a path-keyed owner that kept the LAST row rehydrated `alpha.make`
# under `beta`, resolving `Widget` to `beta.Widget`. The clean pass owns the
# definition by the file module, where the two `Widget`s tie and no edge is
# emitted; the incremental run must agree.
_RS = {
    "Cargo.toml": '[package]\nname = "w"\nversion = "0.1.0"\n',
    "src/other.rs": "pub fn x() {}\n",
    "src/lib.rs": (
        "pub mod alpha {\n    pub struct Widget;\n"
        "    pub fn make() -> Widget { Widget }\n}\n"
        "pub mod beta {\n    pub struct Widget;\n}\n"
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
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
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


def test_inline_modules_sharing_a_file_requeue_under_the_file_module(
    tmp_path: Path,
) -> None:
    clean = _clean(tmp_path, _RS)
    assert not any(s == "proj.src.lib.alpha.make" for s, _r, _t in clean)
    assert _incremental(tmp_path, _RS, "src/other.rs") == clean


def test_parameter_of_type_keeps_the_suffixed_modules_own_widget(
    tmp_path: Path,
) -> None:
    clean = _clean(tmp_path, _PY)
    own = "proj.settings.py.Widget"
    assert ("proj.settings.py.use.0", cs.RelationshipType.OF_TYPE.value, own) in clean
    assert _incremental(tmp_path, _PY, "other.py") == clean


class _ModuleRows:
    """A query-capable store answering only the module-path read."""

    def __init__(self, rows: list[dict], *, fail: bool = False) -> None:
        self.rows = rows
        self.reads = 0
        self.fail = fail

    def fetch_all(self, query: str, params: dict | None = None) -> list[dict]:
        # Scoped in the query, not in Python: the shared graph holds every
        # project's modules (bot review on PR #1967).
        assert query == cs.CYPHER_PROJECT_MODULE_PATHS
        assert params == {cs.KEY_PROJECT_NAME: "proj", cs.KEY_PROJECT_PREFIX: "proj."}
        self.reads += 1
        if self.fail:
            raise RuntimeError("transient graph outage")
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
            {
                cs.KEY_QUALIFIED_NAME: "proj.inline",
                cs.KEY_PATH: f"{cs.INLINE_MODULE_PATH_PREFIX}x",
            },
            # A bodied Rust inline module shares its file's path: the FILE
            # module (the shortest name) owns the requeue, whatever the order.
            {cs.KEY_QUALIFIED_NAME: "proj.src.lib.beta", cs.KEY_PATH: "src/lib.rs"},
            {cs.KEY_QUALIFIED_NAME: "proj.src.lib", cs.KEY_PATH: "src/lib.rs"},
            {cs.KEY_QUALIFIED_NAME: "proj.src.lib.alpha", cs.KEY_PATH: "src/lib.rs"},
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
    assert updater._recorded_module_qn("src/lib.rs") == "proj.src.lib"
    assert updater._recorded_module_qn("unknown.py") == "proj.unknown"
    # Read once per rehydration, not per fact.
    assert store.reads == 1


def test_a_failed_module_read_degrades_on_a_full_build_and_aborts_an_incremental_run(
    tmp_path: Path,
) -> None:
    """The posture every read in `_rehydrate_registry_from_graph` takes
    (local review): a full build parsed every file and falls back to the
    bare derivation; an incremental run would requeue under the wrong
    owner, so the outage propagates."""
    import pytest

    store = _ModuleRows([], fail=True)
    updater = GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=tmp_path / "proj",
        parsers={},
        queries={},
    )
    updater._is_full_build = True
    assert updater._recorded_module_qn("settings.py") == "proj.settings"
    updater._module_qns_by_path = None
    updater._is_full_build = False
    with pytest.raises(RuntimeError):
        updater._recorded_module_qn("settings.py")


def test_a_real_file_named_like_an_inline_module_is_not_synthetic() -> None:
    """The synthetic path is `inline_module_<name>` with NO extension.

    Both owner reads skipped any path starting with that prefix, so a real
    file legitimately called `inline_module_widget.py` was dropped from the
    owner map and its type facts requeued under the derived name instead of
    the one the graph records (Copilot, #1935).
    """
    from codebase_rag.graph_updater import _is_inline_module_path

    # Synthetic: the producer writes no extension.
    assert _is_inline_module_path("inline_module_widget") is True
    assert _is_inline_module_path("inline_module_my_mod") is True
    # Real files carrying the same prefix are not synthetic.
    assert _is_inline_module_path("inline_module_widget.py") is False
    assert _is_inline_module_path("pkg/inline_module_a.py") is False
    # The control: an ordinary path is unaffected either way, so the
    # assertions above are not passing on a predicate that always answers
    # False.
    assert _is_inline_module_path("src/lib.rs") is False
    # Matched on the basename, so the answer does not depend on DEPTH.
    # Anchoring on the whole path made an extensionless `inline_module_data`
    # synthetic at the root but not under `pkg/` -- the same name answering
    # two ways (local review, PR #1967). Both are synthetic now; the
    # extension, not the directory, is what separates a real file.
    assert _is_inline_module_path("inline_module_data") is True
    assert _is_inline_module_path("pkg/inline_module_data") is True
    assert _is_inline_module_path("pkg/inline_module_data.py") is False


def test_a_real_inline_module_named_file_is_seeded_so_an_add_cannot_take_its_qn(
    tmp_path: Path,
) -> None:
    """The CONSEQUENCE of narrowing the predicate, not the predicate itself.

    `_seed_module_qns_from_graph` exists so an incremental ADD whose basename
    collides with an already-indexed sibling of another language cannot
    re-claim the bare qn. Skipping every path starting with the inline prefix
    excluded a REAL file called `inline_module_widget.py` from that seed, so
    an added `inline_module_widget.rs` took `proj.inline_module_widget` from
    under it.

    Asserting the predicate returns False does not pin this: reverting the
    suffix clause reddens only the predicate's own unit test (local review,
    PR #1967). This drives the seeded map instead, which is what the
    disambiguator reads.
    """
    repo = tmp_path / "proj"
    files = {
        "other.py": "X = 1\n",
        "inline_module_widget.py": "class Widget:\n    pass\n",
    }
    _write(repo, files)
    store = _StatefulIngestor()
    _updater(repo, store).run(force=True)

    # The real file owns the bare qn after a clean pass.
    updater = _updater(repo, store)
    seeded = updater.factory.definition_processor.module_qn_to_file_path
    updater._seed_module_qns_from_graph({"other.py", "inline_module_widget.py"})
    # The map stores the absolute path of the file owning each qn.
    owner = seeded.get("proj.inline_module_widget")
    assert owner is not None, (
        "a real file named like an inline module must be seeded; skipping it "
        "lets a same-basename ADD re-claim its qualified name"
    )
    assert Path(owner).name == "inline_module_widget.py"
    # The control: an ordinary sibling is seeded the same way, so the
    # assertion above is not passing on a map that simply holds everything
    # regardless of the predicate.
    assert Path(seeded["proj.other"]).name == "other.py"
