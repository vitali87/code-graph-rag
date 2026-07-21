# Cross-language / polyglot ingestion regression guard. Drives the
# evals.polyglot corpus (one file per SupportedLanguage, plus a three-way
# same-basename collision) through a single cgr graph build and asserts the
# cross-language integrity invariants: every available language is
# represented, basename collisions are disambiguated rather than
# overwritten, no semantic edge crosses a language boundary, the recorded
# graph is dangling/orphan free, and the whole thing is deterministic. No
# eval before this one indexed more than one language at a time.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater
from evals.polyglot import (
    EXPECTED_LANGUAGES,
    build_polyglot_corpus,
    cgr_polyglot,
)


def _available_languages() -> frozenset[cs.SupportedLanguage]:
    # Only grade languages whose parser actually loaded, so a missing
    # optional grammar skips that language instead of failing the suite --
    # but any loaded language that drops out of the graph is a real bug.
    parsers, _ = load_parsers()
    return EXPECTED_LANGUAGES & frozenset(parsers)


@pytest.fixture
def polyglot_corpus(temp_repo: Path) -> Path:
    root = temp_repo / "poly"
    build_polyglot_corpus(root)
    return root


def test_every_available_language_is_represented(polyglot_corpus: Path) -> None:
    available = _available_languages()
    report = cgr_polyglot(polyglot_corpus, polyglot_corpus.name)
    dropped = available & report.missing_languages
    assert not dropped, (
        "languages dropped from the polyglot graph: "
        f"{sorted(lang.value for lang in dropped)}"
    )
    # each present language must also contribute at least one definition,
    # not just an empty module node.
    # sorted() so iteration order is deterministic across runs (StrEnum
    # members hash by string, whose order varies with hash randomization).
    empty = [
        lang.value
        for lang in sorted(available)
        if not report.defs_by_language.get(lang)
    ]
    assert not empty, f"languages with no definitions: {sorted(empty)}"


def test_cross_language_basename_collision_is_disambiguated(
    polyglot_corpus: Path,
) -> None:
    available = _available_languages()
    trio = {
        cs.SupportedLanguage.RUST,
        cs.SupportedLanguage.CPP,
        cs.SupportedLanguage.TS,
    }
    if not trio <= available:
        pytest.skip("collision trio needs the rust, cpp and typescript parsers")
    report = cgr_polyglot(polyglot_corpus, polyglot_corpus.name)
    # shapes.rs / shapes.cpp / shapes.ts strip to the same module qn; each
    # must end up with its OWN qn or one silently overwrites the others
    # under the qualified_name uniqueness constraint (issue #652 class).
    # Assert all three files produced a module FIRST: cgr_polyglot keys its
    # path map by qn, so a real collapse would drop a path and a bare
    # uniqueness check could still pass on the survivors (Greptile #824 P1).
    assert set(report.collision_qns) == {"shapes.rs", "shapes.cpp", "shapes.ts"}, (
        f"a colliding-basename file lost its module: {report.collision_qns}"
    )
    qns = list(report.collision_qns.values())
    assert len(qns) == len(set(qns)), (
        f"colliding basenames share a module qn: {report.collision_qns}"
    )


def test_no_edge_crosses_a_language_boundary(polyglot_corpus: Path) -> None:
    report = cgr_polyglot(polyglot_corpus, polyglot_corpus.name)
    # cgr does not resolve references across languages; a CALLS/INHERITS/etc.
    # edge whose endpoints live in different-language modules means a qn from
    # one language bled into another's resolution.
    assert not report.cross_language_edges, (
        f"edges crossing a language boundary: {sorted(report.cross_language_edges)}"
    )


def test_polyglot_graph_is_dangling_and_orphan_free(polyglot_corpus: Path) -> None:
    # create_and_run_updater runs the structural integrity audit (schema,
    # orphans, dangling relationships) over the recorded batches; a
    # violation raises. This proves mixing every language in one build does
    # not produce an edge with a phantom endpoint.
    mock_ingestor = MagicMock()
    mock_ingestor.ensure_node_batch = MagicMock()
    mock_ingestor.ensure_relationship_batch = MagicMock()
    create_and_run_updater(polyglot_corpus, mock_ingestor)


def test_polyglot_report_is_deterministic(
    temp_repo: Path, polyglot_corpus: Path
) -> None:
    # The collision winner is decided by file-processing order; two builds of
    # the same corpus must produce identical qns, or incremental re-index and
    # cross-project references key on a moving target.
    second = temp_repo / "poly_again"
    build_polyglot_corpus(second)
    a = cgr_polyglot(polyglot_corpus, "poly")
    b = cgr_polyglot(second, "poly")
    assert a.modules_by_language == b.modules_by_language
    assert a.collision_qns == b.collision_qns
    assert a.cross_language_edges == b.cross_language_edges
