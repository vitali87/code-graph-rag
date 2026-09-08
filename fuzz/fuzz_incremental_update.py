"""Fuzz the incremental re-ingest path against a clean index.

`GraphUpdater.reingest` re-parses a named set of files plus their one-level
dependents and restores every other edge verbatim. The bugs this path has
produced (#1567-#1570, #1584, #1665) were not crashes: the updater ran to
completion and left a graph that disagreed with what a full index of the same
tree would have produced -- a stale edge, a module that kept a deleted file's
functions, a dependent that was never re-resolved.

So the oracle is differential rather than "does not raise":

    graph after run(force=True) + reingest(edits) == graph after a clean index

which is the same property `codebase_rag/tests/test_reingest.py` asserts over a
handful of hand-written edits. Here the edit sequence is fuzzer-chosen --
truncate, splice, rewrite, delete, recreate -- so it reaches shapes no
hand-written list covers, in particular a file deleted and restored between
passes and an edit that empties a module another file imports from.

Both updater shapes are fuzzed, since they take different code paths: `warm`
reuses the updater that built the graph (the watcher's shape), `fresh` builds a
new one over the populated store (the MCP tool's shape, which must read the
registry back out of the graph first).

Run locally (Linux; atheris does not build against Apple Clang):

    uv run --extra fuzz python fuzz/fuzz_incremental_update.py -max_total_time=60
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import atheris

with atheris.instrument_imports():
    from codebase_rag import constants as cs
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers
    from evals.cgr_graph import _StatefulIngestor

PROJECT = "proj"

# A small tree with a real dependency chain: main -> app -> util, plus a module
# that nothing imports. Edits to `util` must reach `app` and `main` through the
# dependent walk; edits to `unrelated` must reach nothing.
FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper():\n    return 1\n\n\ndef other():\n    return 2\n",
    "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper()\n",
    "pkg/unrelated.py": "def alone():\n    return 3\n",
    "main.py": "from pkg.app import run\n\n\ndef main():\n    main_body = run()\n",
}

EDITABLE = tuple(sorted(FIXTURE))

_PARSERS, _QUERIES = load_parsers()


def _materialise(root: Path) -> None:
    for rel, text in FIXTURE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _updater(store: _StatefulIngestor, root: Path) -> GraphUpdater:
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=_PARSERS,
        queries=_QUERIES,
        project_name=PROJECT,
    )


def _freeze(value: object) -> object:
    """A hashable, order-stable rendering of a property value."""
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((str(k), _freeze(v)) for k, v in value.items()))
    return str(value)


def _snapshot(store: _StatefulIngestor) -> tuple[frozenset, frozenset]:
    """Nodes and edges, both carrying their properties.

    Node PROPERTIES are part of the comparison, not just node identity: a
    reingest that leaves a stale `end_line` or a dropped docstring on an
    otherwise correct node changes no edge and no uid, so an identity-only
    snapshot would call the two graphs equal. Values are frozen through
    `_freeze` because several properties are lists.
    """
    nodes = frozenset(
        (str(label), str(uid))
        + tuple(sorted((str(k), _freeze(v)) for k, v in (props or {}).items()))
        for (label, uid), props in store.nodes.items()
    )
    edges = frozenset(
        (str(fl), str(fv), str(rel), str(tl), str(tv))
        + tuple(sorted(f"{k}={v}" for k, v in store.edge_props.get(e, {}).items()))
        for e in store.edges
        for (fl, fv, rel, tl, tv) in [e]
    )
    return nodes, edges


def _apply_edit(
    root: Path, rel: str, kind: int, blob: str
) -> tuple[str | None, str | None]:
    """Mutate one file. Returns (changed_rel, deleted_rel)."""
    path = root / rel
    original = FIXTURE[rel]

    if kind == 0:  # truncate mid-file, the watcher's mid-write case
        cut = len(original) // 2
        path.write_text(original[:cut], encoding="utf-8")
    elif kind == 1:  # splice fuzzer bytes into the middle
        cut = len(original) // 2
        path.write_text(original[:cut] + blob + original[cut:], encoding="utf-8")
    elif kind == 2:  # replace wholesale
        path.write_text(blob, encoding="utf-8")
    elif kind == 3:  # empty the module but keep the file
        path.write_text("", encoding="utf-8")
    elif kind == 4:  # delete
        path.unlink(missing_ok=True)
        return None, rel
    elif kind == 5:  # delete then recreate: the atomic-save race
        path.unlink(missing_ok=True)
        path.write_text(
            original + "\n\n\ndef added():\n    return 4\n", encoding="utf-8"
        )
    else:  # append a new definition and a call to it
        path.write_text(
            original + "\n\n\ndef added():\n    return helper_missing()\n",
            encoding="utf-8",
        )
    return rel, None


def _yields_no_definitions(path: Path) -> bool:
    """True when the file parses to no complete function or class definition.

    Asked of the PARSER rather than by grepping for `def`, because the
    triggering shape includes a file truncated mid-definition: `def ` is
    present as text while the grammar yields no complete definition node.
    """
    try:
        source = path.read_bytes()
    except OSError:
        return False
    parser = _PARSERS.get(cs.SupportedLanguage.PYTHON)
    if parser is None:
        return False
    tree = parser.parse(source)
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in ("function_definition", "class_definition"):
            return False
        stack.extend(node.children)
    return True


def _module_prefixes_without_definitions(root: Path, changed: list[str]) -> set[str]:
    """Qualified-name prefixes of the changed files that define nothing now."""
    prefixes = set()
    for rel in changed:
        path = root / rel
        if not rel.endswith(".py") or not path.exists():
            continue
        if not _yields_no_definitions(path):
            continue
        parts = rel[: -len(".py")].split("/")
        if parts[-1] == "__init__":
            parts.pop()
        prefixes.add(".".join([PROJECT, *parts]))
    return prefixes


def _demoted_directories(root: Path, deleted: list[str]) -> set[str]:
    """Directories whose `__init__.py` this plan removed and that still exist.

    That is the precondition for #1798, and it is what makes the suppression
    correlated rather than label-shaped: without it, ANY Package/Folder
    mismatch anywhere in the graph would be discarded.
    """
    dirs = set()
    for rel in deleted:
        if Path(rel).name != "__init__.py":
            continue
        parent = str(Path(rel).parent)
        if parent in (".", ""):
            continue
        if (root / parent).is_dir():
            dirs.add(parent)
    return dirs


def _package_demotion_residue(
    root: Path,
    deleted: list[str],
    extra_nodes: set,
    missing_nodes: set,
    extra_edges: set,
    missing_edges: set,
) -> None:
    """Discard the parts of the delta that #1798 explains, in place.

    Deleting a package's `__init__.py` should retract the directory's
    `Package` identity. It does not, which surfaces as a `Package` node a
    clean index does not have (mislabelled when the `Folder` is missing too,
    duplicated when it is present as well), plus containment edges anchored
    to the wrong one of the two.

    Every row removed here must name one of the directories that actually
    lost an `__init__.py` in THIS plan. An earlier version filtered on the
    node label and the (source-label, relation) pair alone, which discarded
    an unrelated `Package`/`Folder` mismatch just as happily -- a suppression
    that cannot fail is not a suppression.
    """
    dirs = _demoted_directories(root, deleted)
    if not dirs:
        return

    # A directory appears as `proj.pkg` on the Package side and as an
    # absolute path on the Folder side, so match either spelling. Both the
    # resolved and unresolved paths are included: the graph stores whatever
    # the updater saw, and on macOS a temp dir under /var resolves to
    # /private/var, so comparing one spelling silently matches nothing.
    qualified = {f"{PROJECT}.{d.replace('/', '.')}" for d in dirs}
    absolute = {str(root / d) for d in dirs}
    absolute |= {str((root / d).resolve()) for d in dirs}
    names = qualified | absolute

    for node in {n for n in extra_nodes if n[0] == "Package" and n[1] in names}:
        extra_nodes.discard(node)
    for node in {n for n in missing_nodes if n[0] == "Folder" and n[1] in names}:
        missing_nodes.discard(node)

    containment = {
        ("Package", "CONTAINS_FILE"),
        ("Package", "CONTAINS_MODULE"),
        ("Folder", "CONTAINS_FILE"),
        ("Folder", "CONTAINS_MODULE"),
        ("Project", "CONTAINS_PACKAGE"),
        ("Project", "CONTAINS_FOLDER"),
    }
    for edges in (extra_edges, missing_edges):
        for edge in {
            e
            for e in edges
            if (e[0], e[2]) in containment
            # The directory is the SOURCE of what it contains and the TARGET
            # of the project's edge, so check whichever end names it.
            and (str(e[1]) in names or str(e[4]) in names)
        }:
            edges.discard(edge)


def _stale_edge_residue(root: Path, changed: list[str], extra_edges: set) -> None:
    """Discard the parts of the delta that #1794 explains, in place.

    A file that parses to no complete definition keeps the edges its removed
    definitions emitted, so an extra edge whose SOURCE lives in such a file's
    module is explained.
    """
    prefixes = _module_prefixes_without_definitions(root, changed)
    if not prefixes:
        return
    for edge in {
        e
        for e in extra_edges
        if any(
            str(e[1]) == prefix or str(e[1]).startswith(f"{prefix}.")
            for prefix in prefixes
        )
    }:
        extra_edges.discard(edge)


def _resurrected_file_residue(
    root: Path,
    deleted: list[str],
    extra_nodes: set,
    missing_nodes: set,
    extra_edges: set,
    missing_edges: set,
) -> None:
    """Discard the parts of the delta that #1799 explains, in place.

    A path named in `deleted` whose file still exists is retracted anyway and
    never re-indexed, so the graph loses that file's File and definition
    nodes and downgrades its importers to a phantom ExternalModule.

    Correlated to the specific resurrected paths, like the #1798 filter: only
    rows naming those files, their module prefix, or the ExternalModule that
    replaced them are removed.
    """
    resurrected = [
        rel for rel in deleted if rel.endswith(".py") and (root / rel).exists()
    ]
    if not resurrected:
        return

    paths = {str(root / rel) for rel in resurrected}
    paths |= {str((root / rel).resolve()) for rel in resurrected}
    prefixes = set()
    stems = set()
    for rel in resurrected:
        parts = rel[: -len(".py")].split("/")
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            stems.add(parts[-1])
            prefixes.add(".".join([PROJECT, *parts]))

    def _theirs(value: object) -> bool:
        text = str(value)
        if text in paths or text in stems:
            return True
        return any(text == pre or text.startswith(f"{pre}.") for pre in prefixes)

    for node in {n for n in missing_nodes if _theirs(n[1])}:
        missing_nodes.discard(node)
    # The phantom ExternalModule stands in for the module that was dropped.
    for node in {n for n in extra_nodes if n[0] == "ExternalModule" and _theirs(n[1])}:
        extra_nodes.discard(node)
    for edges in (extra_edges, missing_edges):
        for edge in {e for e in edges if _theirs(e[1]) or _theirs(e[4])}:
            edges.discard(edge)


def _is_only_known_defects(
    root: Path,
    changed: list[str],
    deleted: list[str],
    actual: tuple[frozenset, frozenset],
    expected: tuple[frozenset, frozenset],
) -> bool:
    """True when everything in the delta is explained by #1794 or #1798.

    Subtractive rather than a disjunction of whole-delta matchers: one edit
    plan can trigger BOTH defects at once (truncate a file mid-`def` while
    deleting an `__init__.py`), and an `A or B` test matches neither because
    each sees the other's rows as foreign. Each helper removes only the rows
    its own defect explains; whatever survives is a genuine finding and fails
    the run, so this stays as strict as the per-defect matchers it replaces.

    Delete both helpers and this one when #1794 and #1798 are fixed; the
    harness then re-detects them.
    """
    extra_nodes = set(actual[0] - expected[0])
    missing_nodes = set(expected[0] - actual[0])
    extra_edges = set(actual[1] - expected[1])
    missing_edges = set(expected[1] - actual[1])

    _package_demotion_residue(
        root, deleted, extra_nodes, missing_nodes, extra_edges, missing_edges
    )
    _stale_edge_residue(root, changed, extra_edges)
    _resurrected_file_residue(
        root, deleted, extra_nodes, missing_nodes, extra_edges, missing_edges
    )

    return not (extra_nodes or missing_nodes or extra_edges or missing_edges)


def _clean_index(root: Path) -> tuple[frozenset, frozenset]:
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    return _snapshot(store)


def fuzz_incremental_update(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    fresh_updater = fdp.ConsumeBool()
    edit_count = fdp.ConsumeIntInRange(1, 3)

    plan = [
        (
            EDITABLE[fdp.ConsumeIntInRange(0, len(EDITABLE) - 1)],
            fdp.ConsumeIntInRange(0, 6),
        )
        for _ in range(edit_count)
    ]
    blob = fdp.ConsumeUnicodeNoSurrogates(512)

    tmp = Path(tempfile.mkdtemp(prefix="cgr-fuzz-"))
    try:
        root = tmp / PROJECT
        root.mkdir()
        _materialise(root)

        store = _StatefulIngestor()
        updater = _updater(store, root)
        updater.run(force=True)

        changed: list[str] = []
        deleted: list[str] = []
        for rel, kind in plan:
            if not (root / rel).exists() and kind != 5:
                # Already removed by an earlier edit in this plan.
                continue
            edited, removed = _apply_edit(root, rel, kind, blob)
            if edited and edited not in changed:
                changed.append(edited)
            if removed and removed not in deleted:
                deleted.append(removed)

        if not changed and not deleted:
            return

        if fresh_updater:
            updater = _updater(store, root)
        updater.reingest(changed, deleted=deleted)

        actual = _snapshot(store)
        expected = _clean_index(root)

        if actual != expected and _is_only_known_defects(
            root, changed, deleted, actual, expected
        ):
            # Known defects #1794 and #1798, each matched on its exact
            # delta rather than on the plan's shape.
            # #1794: a file yielding no definitions keeps the
            # CALLS edges its removed functions emitted. Suppressed by shape
            # rather than by a golden diff, so the harness still fails on any
            # OTHER disagreement in the same run.
            return

        if actual != expected:
            # Printed in full, not truncated: an earlier `[:5]` hid the
            # `Project -CONTAINS_PACKAGE->` half of #1798's delta and sent a
            # suppression predicate that "obviously matched" back for another
            # build cycle.
            extra_nodes = sorted(actual[0] - expected[0])
            missing_nodes = sorted(expected[0] - actual[0])
            extra_edges = sorted(actual[1] - expected[1])
            missing_edges = sorted(expected[1] - actual[1])
            raise AssertionError(
                "reingest disagreed with a clean index\n"
                f"  shape: {'fresh' if fresh_updater else 'warm'}\n"
                f"  plan: {plan}\n"
                f"  changed={changed} deleted={deleted}\n"
                f"  extra nodes: {extra_nodes}\n"
                f"  missing nodes: {missing_nodes}\n"
                f"  extra edges: {extra_edges}\n"
                f"  missing edges: {missing_edges}"
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    atheris.Setup(sys.argv, atheris.instrument_func(fuzz_incremental_update))
    atheris.Fuzz()


if __name__ == "__main__":
    main()
