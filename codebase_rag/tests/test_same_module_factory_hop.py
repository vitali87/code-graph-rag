"""A same-module `Foo.create().bar()` keeps the factory's return type.

The import-prefix fold treats `X.named()` on a class of this module as a Dart
named constructor and types the chain as `X`. That is Dart-only: elsewhere
`Foo.create()` is a static factory whose recorded return type the chain must
follow, and folding it to `Foo` bound `.bar()` to `Foo.bar` (bot review).
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

SOURCE = (
    "class Other { bar() { return 1; } }\n"
    "class Foo {\n"
    "  static create(): Other { return new Other(); }\n"
    "  bar() { return 2; }\n"
    "}\n"
    "function use() { return Foo.create().bar(); }\n"
)


def test_a_factory_hop_is_not_folded_to_its_class(tmp_path: Path) -> None:
    """TypeScript: `Foo.create()` is a static factory, so `.bar()` is not
    `Foo.bar`; the fold used to drop the `create()` hop and bind it there."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.ts").write_text(SOURCE, encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    GraphUpdater(ingestor=store, repo_path=root, parsers=parsers, queries=queries).run(
        force=True
    )
    calls = {
        str(target)
        for _sl, source, rel, _tl, target in store.edges
        if rel == cs.RelationshipType.CALLS.value and str(source).endswith(".use")
    }
    assert "proj.app.Foo.bar" not in calls, sorted(calls)
