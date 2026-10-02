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

import shutil
import sys
import tempfile
from pathlib import Path

import atheris
from loguru import logger

with atheris.instrument_imports():
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

# Kinds 0-6 rewrite a file in place; 7-9 put source at a path the first index
# never saw: a new importer, a rename, a move into a new directory.
EDIT_KINDS = 10
# Kinds that need no original on disk, so they still apply after an earlier
# edit in the plan removed the file.
_CREATES_FROM_NOTHING = frozenset({5, 7})

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
        # keyed_edges, not edges: the endpoint view collapses every site
        # between one pair of nodes into a single entry, so adding or losing a
        # call site left this snapshot identical. The site is part of the
        # identity the production store MERGEs on, so it is part of the
        # identity being compared.
        (str(fl), str(fv), str(rel), str(tl), str(tv), repr(site))
        + tuple(sorted(f"{k}={v}" for k, v in store.props_for(e).items()))
        for e in store.keyed_edges
        for (fl, fv, rel, tl, tv, site) in [e]
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
    elif kind == 6:  # append a new definition and a call to it
        path.write_text(
            original + "\n\n\ndef added():\n    return helper_missing()\n",
            encoding="utf-8",
        )
    elif kind == 7:  # a new sibling that imports this module
        new = path.with_name(f"{path.stem}_new.py")
        module = _module_name(rel)
        new.write_text(
            f"import {module}\n\n\ndef added_caller():\n"
            f"    return {module}.helper()\n{blob}",
            encoding="utf-8",
        )
        return new.relative_to(root).as_posix(), None
    else:  # rename in place (8) or move into a new subdirectory (9)
        if kind == 8:
            new = path.with_name(f"{path.stem}_renamed.py")
        else:
            new = path.parent / "moved" / path.name
            new.parent.mkdir(exist_ok=True)
        path.replace(new)
        return new.relative_to(root).as_posix(), rel
    return rel, None


def _module_name(rel: str) -> str:
    parts = rel.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


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
            fdp.ConsumeIntInRange(0, EDIT_KINDS - 1),
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
            if not (root / rel).exists() and kind not in _CREATES_FROM_NOTHING:
                # Already removed or moved by an earlier edit in this plan.
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

        if actual != expected:
            # Printed in full, not truncated: an earlier `[:5]` hid half of
            # #1798's delta. Nothing is filtered as a known defect: #1794,
            # #1798 and #1799 are fixed, and their reproducers are seeds.
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
    # Every input runs two full indexes, whose INFO/DEBUG output made the batch
    # log hundreds of megabytes; findings surface as raised exceptions.
    logger.disable("codebase_rag")
    atheris.Setup(sys.argv, atheris.instrument_func(fuzz_incremental_update))
    atheris.Fuzz()


if __name__ == "__main__":
    main()
