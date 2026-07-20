# (H) Incremental cross-language module-qn collision (found via the polyglot
# (H) eval). On a full build, two files that strip to the same module qn
# (H) (shapes.rs / shapes.cpp) are disambiguated -- the second gets its extension
# (H) appended -- because both pass through _disambiguate_module_qn in one run.
# (H) On an INCREMENTAL run that ADDS one of them next to an already-indexed
# (H) sibling, the disambiguator only sees files processed this run, so the added
# (H) file re-claims the bare qn and silently overwrites the existing module
# (H) under the qualified_name uniqueness constraint (issue #652 class). This
# (H) needs the query-capable _StatefulIngestor: the mock harness never
# (H) rehydrates, so the bug is invisible to every other test.
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_MODULE = cs.NodeLabel.MODULE.value

_RS = "pub struct Square;\n\npub fn describe() -> i32 {\n    1\n}\n"
_CPP = "class Square {};\n\nint describe() { return 1; }\n"


def _index(store: _StatefulIngestor, repo: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _shapes_modules(store: _StatefulIngestor) -> dict[str, str]:
    # (H) path -> module qn, for every Module node whose file is a shapes.* file.
    out: dict[str, str] = {}
    for (label, _uid), props in store.nodes.items():
        if label != _MODULE:
            continue
        path = props.get(cs.KEY_PATH)
        qn = props.get(cs.KEY_QUALIFIED_NAME)
        if (
            isinstance(path, str)
            and isinstance(qn, str)
            and Path(path).stem == "shapes"
        ):
            out[path] = qn
    return out


@pytest.fixture
def _needs_rust_cpp() -> None:
    parsers, _ = load_parsers()
    for lang in (cs.SupportedLanguage.RUST, cs.SupportedLanguage.CPP):
        if lang not in parsers:
            pytest.skip(f"{lang.value} parser not available")


@pytest.mark.usefixtures("_needs_rust_cpp")
def test_incremental_add_of_cross_language_sibling_does_not_collide(
    temp_repo: Path,
) -> None:
    (temp_repo / "shapes.rs").write_text(_RS, encoding="utf-8")

    store = _StatefulIngestor()
    _index(store, temp_repo, force=False)

    # (H) rust owns the bare module qn after the first (full) index.
    assert _shapes_modules(store) == {"shapes.rs": "proj.shapes"}

    # (H) Add the C++ sibling and mark it changed past the hash cache so the
    # (H) incremental run re-parses it.
    cache = temp_repo / cs.HASH_CACHE_FILENAME
    future = cache.stat().st_mtime + 10
    cpp = temp_repo / "shapes.cpp"
    cpp.write_text(_CPP, encoding="utf-8")
    os.utime(cpp, (future, future))

    _index(store, temp_repo, force=False)

    modules = _shapes_modules(store)
    # (H) Both files must survive with DISTINCT module qns; pre-fix, shapes.cpp
    # (H) re-claims proj.shapes and overwrites the rust module node.
    assert set(modules) == {"shapes.rs", "shapes.cpp"}, modules
    assert len(set(modules.values())) == 2, (
        f"cross-language module qn collision on incremental add: {modules}"
    )
    assert modules["shapes.rs"] == "proj.shapes", modules
