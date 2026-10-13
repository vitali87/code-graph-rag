"""A bare specifier a `baseUrl` resolves is a first-party import.

With `"baseUrl": "src"`, TypeScript resolves `"lib/format"` against `src/`.
cgr recognised such a specifier as first-party but kept its module qn the
bare path, so the import pointed at an ExternalModule `lib.format` and no
cross-file call got an edge (issue #2808). Create React App projects and
many webpack/Angular codebases import this way everywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_CALLS = cs.RelationshipType.CALLS.value
_IMPORTS = cs.RelationshipType.IMPORTS.value

_FORMAT = 'export function formatPrice(n: number) { return "$" + n.toFixed(2); }\n'
_PRICE_TAG = (
    'import { formatPrice } from "lib/format";\n'
    "export function PriceTag(p: { n: number }) { return formatPrice(p.n); }\n"
)
_APP = (
    'import { PriceTag } from "components/PriceTag";\n'
    'import { useState } from "react";\n'
    "export function App() { useState(0); return PriceTag({ n: 1 }); }\n"
)


def _index(root: Path, files: dict[str, str]) -> _StatefulIngestor:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="tsb",
    ).run(force=True)
    return store


def _edges(store: _StatefulIngestor, rel: str) -> set[tuple[str, str, str]]:
    return {(str(e[1]), str(e[3]), str(e[4])) for e in store.keyed_edges if e[2] == rel}


def _issue(config: str = "tsconfig.json", base: str = "src") -> dict[str, str]:
    return {
        config: f'{{ "compilerOptions": {{ "baseUrl": "{base}", "jsx": "react-jsx" }} }}\n',
        "src/lib/format.ts": _FORMAT,
        "src/components/PriceTag.tsx": _PRICE_TAG,
        "src/App.tsx": _APP,
    }


@pytest.mark.parametrize("config", ["tsconfig.json", "jsconfig.json"])
def test_a_base_url_import_names_the_project_module(
    tmp_path: Path, config: str
) -> None:
    store = _index(tmp_path / "tsb", _issue(config))
    imports = _edges(store, _IMPORTS)
    assert ("tsb.src.components.PriceTag", "Module", "tsb.src.lib.format") in imports
    assert ("tsb.src.App", "Module", "tsb.src.components.PriceTag") in imports


def test_calls_through_base_url_imports_resolve(tmp_path: Path) -> None:
    calls = _edges(_index(tmp_path / "tsb", _issue()), _CALLS)
    assert (
        "tsb.src.components.PriceTag.PriceTag",
        "Function",
        "tsb.src.lib.format.formatPrice",
    ) in calls, calls
    assert (
        "tsb.src.App.App",
        "Function",
        "tsb.src.components.PriceTag.PriceTag",
    ) in calls, calls


def test_a_base_url_beside_paths_resolves_too(tmp_path: Path) -> None:
    files = _issue(base=".")
    files["tsconfig.json"] = (
        '{ "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["./src/*"] } } }\n'
    )
    files["src/components/PriceTag.tsx"] = _PRICE_TAG.replace(
        '"lib/format"', '"src/lib/format"'
    )
    calls = _edges(_index(tmp_path / "tsb", files), _CALLS)
    assert (
        "tsb.src.components.PriceTag.PriceTag",
        "Function",
        "tsb.src.lib.format.formatPrice",
    ) in calls, calls


def test_a_package_no_file_backs_stays_external(tmp_path: Path) -> None:
    # Negatives: `react` names no file under `src/`, so it is still the npm
    # package, and without a `baseUrl` a bare specifier is never first-party.
    store = _index(tmp_path / "tsb", _issue())
    imports = _edges(store, _IMPORTS)
    assert ("tsb.src.App", "ExternalModule", "react") in imports, imports
    files = _issue()
    files["tsconfig.json"] = '{ "compilerOptions": { "jsx": "react-jsx" } }\n'
    imports = _edges(_index(tmp_path / "nobase", files), _IMPORTS)
    assert not any(
        target.startswith("tsb.") for _, _, target in imports if "lib" in target
    ), imports
