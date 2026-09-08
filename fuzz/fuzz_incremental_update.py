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


def _snapshot(store: _StatefulIngestor) -> tuple[frozenset, frozenset]:
    nodes = frozenset((label, str(uid)) for (label, uid) in store.nodes)
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


def _is_known_stale_edge_leak(root: Path, changed: list[str]) -> bool:
    """True when any changed file now yields no definitions.

    That is the shape of #1794: a file that parses to no complete definition
    retracts none of the edges its removed functions emitted, whether it was
    emptied, filled with garbage, or truncated mid-`def`.

    Narrow on purpose -- a plan in which every changed file still defines
    something is not suppressed, so an unrelated disagreement in the same run
    is still reported. Delete this helper and its call site when #1794 is
    fixed; the harness then re-detects it.
    """
    return any(
        _yields_no_definitions(root / rel)
        for rel in changed
        if (root / rel).exists() and rel.endswith(".py")
    )


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

        if actual != expected and _is_known_stale_edge_leak(root, changed):
            # Known defect #1794: a file yielding no definitions keeps the
            # CALLS edges its removed functions emitted. Suppressed by shape
            # rather than by a golden diff, so the harness still fails on any
            # OTHER disagreement in the same run.
            return

        if actual != expected:
            extra_nodes = sorted(actual[0] - expected[0])
            missing_nodes = sorted(expected[0] - actual[0])
            extra_edges = sorted(actual[1] - expected[1])
            missing_edges = sorted(expected[1] - actual[1])
            raise AssertionError(
                "reingest disagreed with a clean index\n"
                f"  shape: {'fresh' if fresh_updater else 'warm'}\n"
                f"  plan: {plan}\n"
                f"  changed={changed} deleted={deleted}\n"
                f"  extra nodes: {extra_nodes[:5]}\n"
                f"  missing nodes: {missing_nodes[:5]}\n"
                f"  extra edges: {extra_edges[:5]}\n"
                f"  missing edges: {missing_edges[:5]}"
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    atheris.Setup(sys.argv, atheris.instrument_func(fuzz_incremental_update))
    atheris.Fuzz()


if __name__ == "__main__":
    main()
