# A `global using static N.T;` in one file puts T's members in bare-call scope
# for every file of the compilation. An incremental run re-parses only CHANGED
# files, so a fresh updater never saw the directive in an unchanged
# `Usings.cs`: the edited caller's bare `Twice(3)` found no scope to resolve
# through and, with the name-wide fallback closed to bare calls (#2005), lost
# the CALLS edge the clean index emits (CodeRabbit, PR #2036).
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.checkout_state import state_file
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

FIXTURE: dict[str, str] = {
    "Helpers.cs": (
        "namespace Helpers;\npublic static class MathHelpers {\n"
        "    public static int Twice(int x) => x * 2;\n}\n"
    ),
    "Usings.cs": "global using static Helpers.MathHelpers;\n",
    "App.cs": (
        "namespace App;\npublic class A {\n"
        "    public int Run() { return Twice(3); }\n}\n"
    ),
}


def _index(store: _StatefulIngestor, repo: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.CSHARP not in parsers:
        pytest.skip("c_sharp parser not available")
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _calls(store: _StatefulIngestor) -> set[tuple[str, str]]:
    return {
        (str(fv), str(tv))
        for (_fl, fv, rel, _tl, tv) in store.edges
        if rel == cs.RelationshipType.CALLS
    }


def _twice_call(calls: set[tuple[str, str]]) -> bool:
    return any(
        src.endswith("App.A.Run") and ".MathHelpers.Twice" in dst for src, dst in calls
    )


def test_an_unchanged_files_global_static_import_scopes_an_edited_caller(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    root.mkdir()
    for rel, text in FIXTURE.items():
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    assert _twice_call(_calls(store)), _calls(store)

    # Past the hash cache's mtime, so only App.cs re-parses (see
    # test_cpp_incremental_out_of_class_method._touch_after_cache).
    app = root / "App.cs"
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    app.write_text(app.read_text(encoding="utf-8") + "// touched\n")
    os.utime(app, (cache_mtime + 1, cache_mtime + 1))
    _index(store, root, force=False)

    assert _twice_call(_calls(store)), _calls(store)
