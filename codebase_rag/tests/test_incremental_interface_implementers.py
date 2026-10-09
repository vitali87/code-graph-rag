"""An incremental run keeps the implementers of unchanged Java classes.

A call typed to an interface with one first-party implementer also binds
the implementation (`_emit_interface_sole_impls`), read from
`interface_implementers`, which only the classes ingested in the current run
fill. A comment edit to the caller re-parsed it alone, the implementer was
unknown, and the fan-out edges were gone (gson: 8 edges) (issue #3256).
"""

from __future__ import annotations

import os
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_FILES = {
    "src/p/Strategy.java": (
        "package p;\n\npublic interface Strategy {\n    String name(String f);\n}\n"
    ),
    "src/p/Policy.java": (
        "package p;\n\npublic enum Policy implements Strategy {\n    UPPER {\n"
        "        @Override public String name(String f) { return f.toUpperCase(); }\n"
        "    },\n    LOWER {\n"
        "        @Override public String name(String f) { return f.toLowerCase(); }\n"
        "    };\n\n    public String name(String f) { return f; }\n}\n"
    ),
    "src/p/Factory.java": (
        "package p;\n\npublic class Factory {\n    private final Strategy strategy;\n\n"
        "    public Factory(Strategy s) { this.strategy = s; }\n\n"
        "    public String names(String f) {\n        return strategy.name(f);\n    }\n}\n"
    ),
}
_EDIT = (
    "        return strategy.name(f);",
    "        // comment-only edit\n        return strategy.name(f);",
)


def _index(store: _StatefulIngestor, root: Path, force: bool) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run(force=force)
    return updater


def _callees(store: _StatefulIngestor) -> set[str]:
    return {
        str(edge[4])
        for edge in store.edges
        if edge[2] == cs.RelationshipType.CALLS.value
        and str(edge[1]).startswith("proj.src.p.Factory.Factory.names")
    }


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _edit(root: Path, rel: str, text: str) -> None:
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    (root / rel).write_text(text, encoding="utf-8")
    os.utime(root / rel, (cache_mtime + 1, cache_mtime + 1))


def _fresh_of(temp_repo: Path, files: dict[str, str]) -> set[str]:
    clean = temp_repo / "clean" / "proj"
    _write(clean, files)
    store = _StatefulIngestor()
    _index(store, clean, force=True)
    return _callees(store)


def test_a_reparsed_caller_keeps_its_sole_implementer_edges(temp_repo: Path) -> None:
    root = temp_repo / "proj"
    _write(root, _FILES)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    fresh = _callees(store)
    # The interface method, the enum's own and both constant bodies.
    assert len(fresh) == 4, fresh
    assert any(qn.startswith("proj.src.p.Policy.Policy.name") for qn in fresh)

    _edit(root, "src/p/Factory.java", _FILES["src/p/Factory.java"].replace(*_EDIT))
    _index(store, root, force=False)

    assert _callees(store) == fresh


def test_an_implementer_that_drops_the_interface_loses_its_edges(
    temp_repo: Path,
) -> None:
    # Negative: a re-parsed class's own relations win over the stored ones.
    # `Policy` no longer implements `Strategy`, so the call keeps only the
    # interface method, as a fresh index of the same tree gives.
    root = temp_repo / "proj"
    _write(root, _FILES)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    edited = {
        "src/p/Policy.java": _FILES["src/p/Policy.java"].replace(
            " implements Strategy", ""
        ),
        "src/p/Factory.java": _FILES["src/p/Factory.java"].replace(*_EDIT),
    }
    for rel, text in edited.items():
        _edit(root, rel, text)
    _index(store, root, force=False)

    assert _callees(store) == _fresh_of(temp_repo, {**_FILES, **edited})
    assert not any(
        qn.startswith("proj.src.p.Policy.Policy.name") for qn in _callees(store)
    )


def test_a_second_implementer_ends_the_sole_fan_out(temp_repo: Path) -> None:
    # Negative: an implementer read back from the graph still counts toward
    # "sole". With `Other` added, `Strategy` has two implementers, one stored
    # and one parsed this run, and the call binds the interface method only.
    root = temp_repo / "proj"
    _write(root, _FILES)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    other = {
        "src/p/Other.java": (
            "package p;\n\npublic class Other implements Strategy {\n"
            "    public String name(String f) { return f; }\n}\n"
        ),
    }
    _write(root, other)
    _edit(root, "src/p/Factory.java", _FILES["src/p/Factory.java"].replace(*_EDIT))
    _index(store, root, force=False)

    edited = {
        **_FILES,
        **other,
        "src/p/Factory.java": _FILES["src/p/Factory.java"].replace(*_EDIT),
    }
    assert _callees(store) == _fresh_of(temp_repo, edited)


def test_restore_variant_reads_only_a_line_or_line_column_suffix() -> None:
    # `natural@line` and `natural@line_col` rejoin the natural qn's bucket;
    # anything else (no marker, a non-numeric suffix) is left alone.
    from codebase_rag.function_registry import FunctionRegistryTrie

    registry = FunctionRegistryTrie()
    for qn in ("p.C.m@8", "p.C.m@11_4", "p.C.n", "p.C.o@x"):
        registry.restore_variant(qn)

    assert registry.variants("p.C.m") == ["p.C.m", "p.C.m@8", "p.C.m@11_4"]
    assert registry.variants("p.C.n") == ["p.C.n"]
    assert registry.variants("p.C.o") == ["p.C.o"]
