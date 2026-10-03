"""A data file beside a same-named source file is not that module (issue #2463).

`pkg/shapes.txt` next to `pkg/shapes.py`, or trybuild's `tests/ui/x.stderr`
next to `tests/ui/x.rs`: the data file is never parsed, so it records no
module qn. Clearing its state before its re-read fell back to the qn its PATH
would give a module, `proj.pkg.shapes`, which is the source file's own qn,
and swept every definition and class record the source file had just
registered. When the source file is re-parsed in the same run (as a
dependent of a changed file, or because an added sibling put its stem in
flux) and the data file is cleared after it, its re-derived CALLS and
INSTANTIATES edges (and, in Rust, its impl methods, IMPLEMENTS and
INSTANTIATES) were computed against a registry that no longer held them, and
the loss outlived the run: the next sync found nothing changed.

The negative tests pin what must stay: each change on its own, deleting the
data file, a caller in another file, and a real source file's own state still
swept when it goes, including through the path-derived fallback a fresh
updater relies on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict
from evals.cgr_graph import _StatefulIngestor

_SHAPES = (
    "from pkg.helpers import base\n"
    "\n\n"
    "class Square:\n"
    "    def area(self):\n"
    "        return base()\n"
    "\n\n"
    "def make():\n"
    "    return Square().area()\n"
)
PYTHON: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/helpers.py": "def base():\n    return 1\n",
    "pkg/shapes.py": _SHAPES,
    "pkg/shapes.txt": "expected output v1\n",
}
PYTHON_HELPERS_V2 = "def base():\n    return 1\n\n\ndef newer():\n    return 2\n"

RUST: dict[str, str] = {
    "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
    "src/lib.rs": "pub fn helper() -> i32 {\n    1\n}\n",
    "tests/user.rs": (
        "use demo::helper;\n"
        "use std::ops::Deref;\n"
        "\n"
        "struct Wrapper(i32);\n"
        "\n"
        "impl Deref for Wrapper {\n"
        "    type Target = i32;\n"
        "    fn deref(&self) -> &i32 {\n"
        "        &self.0\n"
        "    }\n"
        "}\n"
        "\n"
        "impl Wrapper {\n"
        "    fn build() -> Wrapper {\n"
        "        Wrapper(helper())\n"
        "    }\n"
        "    fn value(&self) -> i32 {\n"
        "        *self.deref()\n"
        "    }\n"
        "}\n"
        "\n"
        "fn main() {\n"
        "    let w = Wrapper::build();\n"
        "    w.value();\n"
        "}\n"
    ),
    "tests/user.stderr": "error: expected output v1\n",
}
RUST_LIB_V2 = (
    "pub fn helper() -> i32 {\n    1\n}\n\npub fn newer() -> i32 {\n    2\n}\n"
)

Snapshot = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]
Edge = tuple[str, ...]
_STRUCTURE = {cs.NodeLabel.FILE.value, cs.NodeLabel.FOLDER.value}


class _Buffered(_StatefulIngestor):
    """Reads see only flushed nodes, as Memgraph's do.

    The emulator is write-through, so the registry rehydration that follows
    the parse read the re-parsed dependent's fresh nodes straight back and
    repaired the sweep; against Memgraph those nodes are still in the batch
    buffer, and the old ones were deleted before the parse.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending: list[tuple[str, PropertyDict]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        self._pending.append((label, properties))

    def flush_all(self) -> None:
        pending, self._pending = self._pending, []
        for label, properties in pending:
            super().ensure_node_batch(label, properties)
        super().flush_all()


def _snapshot(store: _Buffered) -> Snapshot:
    # File and Folder nodes carry absolute paths, which differ between the
    # two temporary trees; everything else is keyed by project-relative qns.
    nodes = frozenset(
        (label, str(uid)) for (label, uid) in store.nodes if label not in _STRUCTURE
    )
    edges = frozenset(
        (str(fl), str(fv), str(rel), str(tl), str(tv))
        for (fl, fv, rel, tl, tv) in store.edges
        if fl not in _STRUCTURE and tl not in _STRUCTURE
    )
    return nodes, edges


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _updater(
    store: _Buffered, repo: Path, language: cs.SupportedLanguage
) -> GraphUpdater:
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"{language} parser not available")
    return GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


def _index(
    store: _Buffered,
    repo: Path,
    language: cs.SupportedLanguage,
    force: bool,
) -> GraphUpdater:
    updater = _updater(store, repo, language)
    updater.run(force=force)
    return updater


def _clean(
    tmp_path: Path, files: dict[str, str], language: cs.SupportedLanguage
) -> Snapshot:
    root = tmp_path / "clean" / "proj"
    _write(root, files)
    store = _Buffered()
    _index(store, root, language, force=True)
    return _snapshot(store)


def _sync_after(
    tmp_path: Path,
    files: dict[str, str],
    edits: dict[str, str | None],
    language: cs.SupportedLanguage,
) -> tuple[Snapshot, Snapshot]:
    """Index `files`, apply `edits` in one change set, sync; and a clean index.

    An edit of None deletes the file. Returns (incremental, clean).
    """
    root = tmp_path / "live" / "proj"
    _write(root, files)
    store = _Buffered()
    _index(store, root, language, force=True)
    for rel, text in edits.items():
        if text is None:
            (root / rel).unlink()
        else:
            _write(root, {rel: text})
    _index(store, root, language, force=False)
    edited = {rel: text for rel, text in {**files, **edits}.items() if text is not None}
    return _snapshot(store), _clean(tmp_path, edited, language)


def _outgoing(snapshot: Snapshot, source_prefix: str) -> set[Edge]:
    return {
        edge
        for edge in snapshot[1]
        if edge[1] == source_prefix or edge[1].startswith(f"{source_prefix}.")
    }


# ---------------------------------------------------------------------------
# The dependent's edges survive a sync that also touches its data sibling
# ---------------------------------------------------------------------------


_MAKE_CALLS_AREA = (
    cs.NodeLabel.FUNCTION.value,
    "proj.pkg.shapes.make",
    cs.RelationshipType.CALLS.value,
    cs.NodeLabel.METHOD.value,
    "proj.pkg.shapes.Square.area",
)
_MAKE_INSTANTIATES_SQUARE = (
    cs.NodeLabel.FUNCTION.value,
    "proj.pkg.shapes.make",
    cs.RelationshipType.INSTANTIATES.value,
    cs.NodeLabel.CLASS.value,
    "proj.pkg.shapes.Square",
)


# `.out` sorts before `.py`, so its state was cleared before the dependent
# re-parsed and it never lost anything; it pins that the order is irrelevant.
@pytest.mark.parametrize("data_suffix", [".txt", ".stderr", ".snap", ".out"])
def test_dependency_and_data_sibling_changed_together_keep_the_dependents_edges(
    tmp_path: Path, data_suffix: str
) -> None:
    files = dict(PYTHON)
    del files["pkg/shapes.txt"]
    data_file = f"pkg/shapes{data_suffix}"
    files[data_file] = "expected output v1\n"

    after, clean = _sync_after(
        tmp_path,
        files,
        {"pkg/helpers.py": PYTHON_HELPERS_V2, data_file: "expected output v2\n"},
        cs.SupportedLanguage.PYTHON,
    )

    make_edges = _outgoing(after, "proj.pkg.shapes.make")
    assert _MAKE_CALLS_AREA in make_edges
    assert _MAKE_INSTANTIATES_SQUARE in make_edges
    assert after == clean


@pytest.mark.parametrize(
    "edits",
    [
        {"pkg/shapes.out": "added\n"},
        {"pkg/shapes.out": "added\n", "pkg/helpers.py": PYTHON_HELPERS_V2},
    ],
    ids=["alone", "with-the-dependency"],
)
def test_an_added_data_sibling_does_not_cost_the_source_its_edges(
    tmp_path: Path, edits: dict[str, str | None]
) -> None:
    # Adding `shapes.out` puts the stem in flux, so the source and the
    # UNCHANGED `shapes.txt` are both re-read, `shapes.txt` after the source.
    after, clean = _sync_after(tmp_path, PYTHON, edits, cs.SupportedLanguage.PYTHON)

    make_edges = _outgoing(after, "proj.pkg.shapes.make")
    assert _MAKE_CALLS_AREA in make_edges
    assert _MAKE_INSTANTIATES_SQUARE in make_edges
    assert after == clean


def test_rust_dependent_beside_its_stderr_snapshot_keeps_impls_and_calls(
    tmp_path: Path,
) -> None:
    # The trybuild layout `dtolnay/anyhow` lost edges on: `tests/user.rs`
    # uses the crate, `tests/user.stderr` is its snapshot, and both the crate
    # and the snapshot change in one sync.
    after, clean = _sync_after(
        tmp_path,
        RUST,
        {"src/lib.rs": RUST_LIB_V2, "tests/user.stderr": "error: v2\n"},
        cs.SupportedLanguage.RUST,
    )

    wanted = _outgoing(clean, "proj.tests.user")
    rels = {edge[2] for edge in wanted}
    assert {
        cs.RelationshipType.DEFINES_METHOD.value,
        cs.RelationshipType.IMPLEMENTS.value,
        cs.RelationshipType.CALLS.value,
    } <= rels, "fixture must give the dependent the edges the issue lost"
    assert _outgoing(after, "proj.tests.user") == wanted
    assert after == clean


@pytest.mark.parametrize("data_file", ["shapes.txt", "shapes.md"])
def test_clearing_a_data_file_keeps_the_same_stem_sources_state(
    tmp_path: Path, data_file: str
) -> None:
    # The watcher and the MCP server hold one updater across events, so a
    # swept registry outlives the event that swept it. A Markdown file is
    # parsed, by the document tier, but records no tree-sitter module either.
    root = tmp_path / "proj"
    _write(root, {**PYTHON, "pkg/shapes.md": "# Shapes\n"})
    updater = _index(_Buffered(), root, cs.SupportedLanguage.PYTHON, force=True)
    processor = updater.factory.definition_processor
    before = set(updater.function_registry.keys())
    owners_before = dict(processor.class_owner_module)
    assert "proj.pkg.shapes.make" in before
    assert "proj.pkg.shapes.Square" in owners_before

    updater.remove_file_from_state(root / "pkg" / data_file)

    assert set(updater.function_registry.keys()) == before
    assert processor.class_owner_module == owners_before
    assert processor.module_qn_to_file_path["proj.pkg.shapes"] == (
        root / "pkg" / "shapes.py"
    )


# ---------------------------------------------------------------------------
# Negative tests: what must stay exactly as it was
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "edits",
    [
        {"pkg/helpers.py": PYTHON_HELPERS_V2},
        {"pkg/shapes.txt": "expected output v2\n"},
        {"pkg/shapes.txt": None},
        {"pkg/shapes.txt": None, "pkg/helpers.py": PYTHON_HELPERS_V2},
        {"pkg/shapes.py": _SHAPES + "\n\ndef extra():\n    return make()\n"},
    ],
    ids=[
        "only-the-dependency",
        "only-the-data-file",
        "data-file-deleted",
        "data-file-deleted-with-dependency",
        "only-the-source-file",
    ],
)
def test_other_change_sets_still_match_a_clean_index(
    tmp_path: Path, edits: dict[str, str | None]
) -> None:
    after, clean = _sync_after(tmp_path, PYTHON, edits, cs.SupportedLanguage.PYTHON)

    assert _outgoing(clean, "proj.pkg.shapes.make")
    assert after == clean


def test_a_caller_elsewhere_keeps_its_edge_into_the_same_stem_source(
    tmp_path: Path,
) -> None:
    # `shapes.py` is not re-parsed here, so the registry rehydration after
    # the parse reads its definitions back from the graph; the caller's edge
    # into it must stay whichever way the data file is handled.
    files = {
        **PYTHON,
        "main.py": "from pkg.shapes import make\n\n\ndef run():\n    return make()\n",
    }
    after, clean = _sync_after(
        tmp_path,
        files,
        {
            "main.py": (
                "from pkg.shapes import make\n\n\ndef run():\n    return make() + 1\n"
            ),
            "pkg/shapes.txt": "expected output v2\n",
        },
        cs.SupportedLanguage.PYTHON,
    )

    assert (
        cs.NodeLabel.FUNCTION.value,
        "proj.main.run",
        cs.RelationshipType.CALLS.value,
        cs.NodeLabel.FUNCTION.value,
        "proj.pkg.shapes.make",
    ) in _outgoing(after, "proj.main.run")
    assert after == clean


def test_deleting_the_source_file_beside_its_data_file_still_removes_it(
    tmp_path: Path,
) -> None:
    after, clean = _sync_after(
        tmp_path,
        PYTHON,
        {"pkg/shapes.py": None, "pkg/shapes.txt": "expected output v2\n"},
        cs.SupportedLanguage.PYTHON,
    )

    assert not {uid for _label, uid in after[0] if uid.startswith("proj.pkg.shapes")}
    assert after == clean


def test_clearing_the_source_file_still_sweeps_its_own_state(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write(root, PYTHON)
    updater = _index(_Buffered(), root, cs.SupportedLanguage.PYTHON, force=True)

    updater.remove_file_from_state(root / "pkg" / "shapes.py")

    left = {qn for qn in updater.function_registry.keys() if ".shapes" in qn}
    assert not left, sorted(left)
    assert "proj.pkg.shapes.Square" not in (
        updater.factory.definition_processor.class_owner_module
    )
    assert "proj.pkg.helpers.base" in updater.function_registry


def test_an_unparsed_source_file_still_falls_back_to_its_path_qn(
    tmp_path: Path,
) -> None:
    # A fresh updater doing a scoped re-ingest has recorded no module for the
    # file yet; the definitions it holds were read back from the graph, and
    # the path-derived qn is the only prefix that reaches them (issue #1719).
    root = tmp_path / "proj"
    _write(root, PYTHON)
    updater = _updater(_Buffered(), root, cs.SupportedLanguage.PYTHON)
    updater.function_registry["proj.pkg.shapes.make"] = cs.NodeLabel.FUNCTION.value
    updater.function_registry["proj.pkg.helpers.base"] = cs.NodeLabel.FUNCTION.value

    updater.remove_file_from_state(root / "pkg" / "shapes.py")

    assert "proj.pkg.shapes.make" not in updater.function_registry
    assert "proj.pkg.helpers.base" in updater.function_registry


@pytest.mark.parametrize("data_file", ["shapes.txt", "shapes.md"])
def test_a_fresh_updater_clearing_a_data_file_keeps_the_graph_read_source_state(
    tmp_path: Path, data_file: str
) -> None:
    # A fresh updater has recorded no module for `shapes.py` either, so no
    # module holds the qn the data file's path derives (the #2586 guard sees
    # no holder). Only the data file being no tree-sitter source keeps the
    # sweep off the definitions read back from the graph.
    root = tmp_path / "proj"
    _write(root, {**PYTHON, "pkg/shapes.md": "# Shapes\n"})
    updater = _updater(_Buffered(), root, cs.SupportedLanguage.PYTHON)
    updater.function_registry["proj.pkg.shapes.make"] = cs.NodeLabel.FUNCTION.value
    assert "proj.pkg.shapes" not in (
        updater.factory.definition_processor.module_qn_to_file_path
    )

    updater.remove_file_from_state(root / "pkg" / data_file)

    assert "proj.pkg.shapes.make" in updater.function_registry
