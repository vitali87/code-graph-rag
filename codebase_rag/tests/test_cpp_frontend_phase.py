# Each C++ frontend mode runs at its own place in the pipeline (#1178).
#
# LIBCLANG emits its own definition nodes and reports the files it covered, so
# it must run before the definition pass, which skips them. HYBRID attributes
# macro uses to the tree-sitter spans that pass produces, so it must run after.
# An inverted order raises nothing: HYBRID would find no spans and attribute
# nothing, and the graph would quietly miss every macro edge.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.updater import frontends
from evals.cgr_graph import StatefulIngestor

SOURCE = "int area(int w, int h) { return w * h; }\n"


def _definitions_seen_by_the_frontend(
    root: Path, mode: cs.CppFrontend, monkeypatch: pytest.MonkeyPatch
) -> bool:
    (root / "shapes.cpp").write_text(SOURCE, encoding="utf-8")
    monkeypatch.setattr(gu.settings, "CPP_FRONTEND", mode)
    monkeypatch.setattr(frontends, "cpp_frontend_available", lambda: True)
    monkeypatch.setattr(frontends, "find_compile_commands", lambda _repo: root)
    seen: list[bool] = []

    def libclang(*_args: object, function_registry: object, **_kw: object) -> set:
        seen.append(any(qn.endswith(".area") for qn, _ in function_registry.items()))
        return set()

    def hybrid(
        *_args: object, function_registry: object, **_kw: object
    ) -> tuple[list, list]:
        seen.append(any(qn.endswith(".area") for qn, _ in function_registry.items()))
        return [], []

    monkeypatch.setattr(frontends, "run_cpp_frontend", libclang)
    monkeypatch.setattr(frontends, "run_cpp_frontend_hybrid", hybrid)
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.CPP not in parsers:
        pytest.skip("cpp parser not available")
    GraphUpdater(
        ingestor=StatefulIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        skip_embeddings=True,
    ).run(force=True)
    assert len(seen) == 1, seen
    return seen[0]


def test_libclang_runs_before_the_definition_pass(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not _definitions_seen_by_the_frontend(
        temp_repo, cs.CppFrontend.LIBCLANG, monkeypatch
    )


def test_hybrid_runs_after_the_definition_pass(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _definitions_seen_by_the_frontend(
        temp_repo, cs.CppFrontend.HYBRID, monkeypatch
    )
