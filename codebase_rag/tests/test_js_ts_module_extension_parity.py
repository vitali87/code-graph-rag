"""A named import must bind through EVERY JS/TS module extension (#1720).

`_resolve_js_internal_module` decides where a named import's module ends and
its symbol begins: given the deferred target `proj.pkg.go` it must return
`proj.pkg` so the alias map can redirect that to the real `proj.pkg.index`
Module node. It made that decision by probing the filesystem against a
RESTATED list of four extensions -- `.js`, `.ts`, `.jsx`, `.tsx` -- rather than
against `JS_TS_MODULE_EXTENSIONS`, which is the set every other part of the
JS/TS path already uses.

So for a module written in any of the other six forms the probe found nothing,
the symbol segment was never stripped, and `proj.pkg.go` was carried forward to
verification, where it matches no Module node and is dropped as an unverifiable
internal target. The import edge simply vanishes.

This is the same defect shape as the `.mts`/`.cts` omission from
`DIRECTORY_MODULE_STEM_BY_EXT` found in the #1682 review: a subset of a
canonical set, restated at a second site, silently drifting from it. The fix
there and here is the same -- ask the one helper that owns the question.

Scope note, because #1720 framed this as a `.d.ts` problem: declarations are
how it was FOUND, not the extent of it. `.mjs`, `.cjs`, `.mts` and `.cts` are
ordinary implementation files with no declaration involvement and they were
equally broken, which is why the parametrisation below covers the whole set
rather than the three `.d.*` forms.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

# A named import through a directory entry point: the shape that needs the
# module/symbol split to be made correctly.
MAIN = "import { go } from './pkg';\nexport const r = go;\n"
IMPL = "export function go() { return 1; }\n"
DECL = "export declare function go(): number;\n"


def _index(files: dict[str, str]) -> _StatefulIngestor:
    root = Path(tempfile.mkdtemp()) / "proj"
    root.mkdir(parents=True)
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


def _imports(store: _StatefulIngestor) -> list[tuple[Any, ...]]:
    return [edge for edge in store.edges if edge[2] == "IMPORTS"]


def _modules(store: _StatefulIngestor) -> list[str]:
    return sorted(
        props[cs.KEY_QUALIFIED_NAME]
        for (label, _uid), props in store.nodes.items()
        if label == cs.NodeLabel.MODULE.value
    )


class TestEveryModuleExtensionBindsANamedImport:
    @pytest.mark.parametrize("ext", cs.JS_TS_MODULE_EXTENSIONS)
    def test_a_directory_entry_point_resolves(self, ext: str) -> None:
        """`import { go } from './pkg'` with `pkg/index<ext>` as the entry point.

        Asserts the edge's TARGET, not merely that some edge exists: carrying
        the unstripped `proj.pkg.go` forward produces no edge at all, but a
        different wrong split could produce an edge to the wrong node, and
        "an edge exists" cannot tell those apart.
        """
        body = DECL if ext.startswith(".d.") else IMPL
        store = _index({"main.ts": MAIN, f"pkg/index{ext}": body})

        assert "proj.pkg.index" in _modules(store), _modules(store)
        assert _imports(store) == [
            ("Module", "proj.main", "IMPORTS", "Module", "proj.pkg.index")
        ], f"{ext}: {_imports(store)}"

    def test_an_import_of_a_directory_with_no_entry_point_binds_nothing(self) -> None:
        """The control that stops the rule collapsing into "always strip".

        If the module/symbol split stopped consulting the filesystem and simply
        dropped the last segment, every parametrisation above would pass and
        the resolver would invent module targets for directories that hold no
        entry point at all. Here `pkg/` exists but has no `index.*`, so there
        is no module to bind to and no edge may be emitted.
        """
        store = _index({"main.ts": MAIN, "pkg/other.ts": IMPL})

        assert _imports(store) == [], _imports(store)
