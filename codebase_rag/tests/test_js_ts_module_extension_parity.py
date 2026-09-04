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


def _index(
    files: dict[str, str], exclude_paths: frozenset[str] | None = None
) -> _StatefulIngestor:
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
        exclude_paths=exclude_paths,
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

    @pytest.mark.parametrize("ext", cs.JS_TS_MODULE_EXTENSIONS)
    def test_a_flat_file_module_resolves(self, ext: str) -> None:
        """`import { go } from './util'` with a FLAT `util<ext>`, no directory.

        Added after a surviving mutant, not by foresight. Every case above
        routes through a directory entry point, so neutering the file branch of
        `_js_module_rel_on_disk` -- the other half of the helper this fix
        delegates to -- left all 12 tests green. The suite was one-dimensional
        on "directory entry point vs flat file" and could not see half the
        component it depends on.
        """
        body = DECL if ext.startswith(".d.") else IMPL
        store = _index(
            {
                "main.ts": "import { go } from './util';\nexport const r = go;\n",
                f"util{ext}": body,
            }
        )

        assert _imports(store) == [
            ("Module", "proj.main", "IMPORTS", "Module", "proj.util")
        ], f"{ext}: {_imports(store)}"

    def test_a_declaration_does_not_yield_to_an_excluded_implementation(self) -> None:
        """A declaration yields only to an implementation that WILL be indexed.

        Greptile P1 on this PR, and correct. `has_implementation_sibling` asked
        the disk "does a same-stem implementation exist", but the question the
        tie-break needs answered is "will one be INDEXED". Those differ exactly
        when the indexer's exclude/unignore policy removes the implementation:
        `foo.ts` excluded, `foo.d.ts` still eligible, and the declaration
        yielded `proj.foo` to a file that never enters the graph. Nothing then
        owns the name imports of `./foo` resolve to -- which is #1720 again,
        reintroduced by the fix for it under a configuration I had not tested.

        Driven end-to-end through `GraphUpdater(exclude_paths=...)` rather than
        against the predicate directly, so it also pins the WIRING: the policy
        has to reach `_disambiguate_module_qn`, and a correct predicate that
        nothing passes the exclude set to would still fail here.
        """
        store = _index(
            {
                "main.ts": "import { go } from './foo';\nexport const r = go;\n",
                "foo.ts": IMPL,
                "foo.d.ts": DECL,
            },
            exclude_paths=frozenset({"foo.ts"}),
        )

        modules = _modules(store)
        # The precondition: the exclude really took, so a green result below
        # cannot come from the implementation having been indexed after all.
        assert "proj.foo.ts" not in modules, modules
        assert "proj.foo" in modules, modules

    def test_a_declaration_still_yields_when_the_implementation_is_indexed(
        self,
    ) -> None:
        """The control for the test above: same fixture, no exclude.

        Without this, "declarations never yield" would satisfy the exclusion
        test, and the walk-order fix in `test_qn_walk_order_parity.py` would be
        the only thing holding the other direction.
        """
        store = _index(
            {
                "main.ts": "import { go } from './foo';\nexport const r = go;\n",
                "foo.ts": IMPL,
                "foo.d.ts": DECL,
            }
        )

        modules = _modules(store)
        assert "proj.foo" in modules, modules
        assert "proj.foo.d.ts" in modules, modules

    def test_an_import_of_a_directory_with_no_entry_point_binds_nothing(self) -> None:
        store = _index({"main.ts": MAIN, "pkg/other.ts": IMPL})

        assert _imports(store) == [], _imports(store)

    def test_the_split_still_asks_the_filesystem(self) -> None:
        """The control with teeth: over-stripping must bind the WRONG module.

        The previous control asserted only that an unresolvable import produces
        no edge, and a resolver that dropped the last segment unconditionally
        satisfied that too -- `proj.pkg.missing` is no more bindable than
        `proj.pkg.missing.go`, so both readings emit nothing and the test could
        not tell them apart. It passed against the over-stripping mutant.

        This fixture makes the two readings disagree about a real target. A
        Python package `pkg/__init__.py` registers the module qn `proj.pkg`,
        while `pkg/` holds no JS/TS entry point at all. So:

          asking the disk  `pkg` names no JS module -> keep `proj.pkg.go`
                           -> matches nothing -> no edge, correctly
          over-stripping   -> `proj.pkg` -> IS a known module -> emits an
                           IMPORTS edge from a TypeScript file to a Python
                           package

        The failure the split exists to prevent is a wrong edge, not a missing
        one, so the assertion has to be able to see a wrong edge.
        """
        store = _index(
            {
                "main.ts": MAIN,
                "pkg/__init__.py": "def go():\n    return 1\n",
            }
        )

        assert "proj.pkg" in _modules(store), _modules(store)
        assert _imports(store) == [], _imports(store)
