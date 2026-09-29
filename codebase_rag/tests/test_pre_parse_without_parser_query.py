"""Pre-parsing skips a changed file whose language has no parser query.

`GraphUpdater` takes `parsers` and `queries` separately. A language present
in the first but missing from the second, or queried without a parser, must
leave its file to the normal per-file path, not abort the incremental run
before stale nodes are removed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_ENTRY = (Path("m.py"), "m.py", True, b"def f():\n    return 1\n")


@pytest.fixture
def python_parsers() -> tuple[dict, dict]:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python grammar unavailable")
    return (
        {cs.SupportedLanguage.PYTHON: parsers[cs.SupportedLanguage.PYTHON]},
        {cs.SupportedLanguage.PYTHON: queries[cs.SupportedLanguage.PYTHON]},
    )


def _updater(tmp_path: Path, parsers: dict, queries: dict) -> GraphUpdater:
    return GraphUpdater(
        ingestor=MagicMock(),
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


def test_a_language_without_queries_is_skipped(
    tmp_path: Path, python_parsers: tuple[dict, dict]
) -> None:
    parsers, _queries = python_parsers
    updater = _updater(tmp_path, parsers, {})
    assert updater._pre_parse_changed_files([_ENTRY]) == {}


def test_queries_without_a_parser_are_skipped(
    tmp_path: Path, python_parsers: tuple[dict, dict]
) -> None:
    parsers, queries = python_parsers
    partial = {
        lang: {k: v for k, v in q.items() if k != cs.KEY_PARSER}
        for lang, q in queries.items()
    }
    updater = _updater(tmp_path, parsers, partial)
    assert updater._pre_parse_changed_files([_ENTRY]) == {}


def test_a_complete_language_is_still_pre_parsed(
    tmp_path: Path, python_parsers: tuple[dict, dict]
) -> None:
    # Negative: the guard must not skip a language that has everything.
    parsers, queries = python_parsers
    updater = _updater(tmp_path, parsers, queries)
    assert Path("m.py") in updater._pre_parse_changed_files([_ENTRY])


def test_the_definition_pass_skips_a_language_without_a_parser(
    tmp_path: Path, python_parsers: tuple[dict, dict]
) -> None:
    parsers, queries = python_parsers
    partial = {
        lang: {k: v for k, v in q.items() if k != cs.KEY_PARSER}
        for lang, q in queries.items()
    }
    processor = _updater(tmp_path, parsers, partial).factory.definition_processor
    assert (
        processor._parse_file_root(
            tmp_path / "m.py", cs.SupportedLanguage.PYTHON, partial, _ENTRY[3], None
        )
        is None
    )
