"""`--engine-preview`: a language on the Rust engine for a test session.

docs/reports/PHASE3_GO.md, section 10 (step 2). The plugin's option sets
`index_engine.PREVIEW` for the session, so `--engine-mode both` compares
every repository holding a previewed language without a test patching the
build's list. While the engine holds none of Go's analysis, that compares
Python with itself and finds no difference. These tests run a session of
their own in a child process, as test_engine_modes.py's do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests import engine_modes

PLUGIN = ("-p", "codebase_rag.tests.engine_modes", "-p", "no:cacheprovider")

# Indexes a small Go repository, as a test of the suite does, and prints the
# languages its updater put on Rust and the selections the scratch indexes of
# `both` were made with.
PREVIEWED = """
from unittest.mock import MagicMock

from codebase_rag import index_engine
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

GO = "package main\\n\\nfunc helper() int { return 1 }\\n\\nfunc main() { helper() }\\n"


def test_go(tmp_path, monkeypatch):
    made = []
    original = GraphUpdater.replica

    def counted(self, ingestor, **kwargs):
        made.append(kwargs.get("index_engine") is None)
        return original(self, ingestor, **kwargs)

    monkeypatch.setattr(GraphUpdater, "replica", counted)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.go").write_text(GO, encoding="utf-8")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=MagicMock(), repo_path=repo, parsers=parsers, queries=queries,
        skip_embeddings=True, state_dir=tmp_path / "state",
    )
    updater.run()
    print(f"\\nPROBE preview={sorted(map(str, index_engine.PREVIEW))}")
    print(f"PROBE rust={sorted(map(str, updater.rust_languages))}")
    print(f"PROBE replicas={made}")
"""


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # This session's own mode sets CGR_INDEX_ENGINE; the session under test
    # starts from none, as CI's does. Go stays on tree-sitter so no index
    # builds the go/types helper.
    for name in (
        engine_modes.INDEX_ENGINE_ENV,
        engine_modes.PARITY_DIFF_DIR_ENV,
        "LOGURU_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CGR_HOME", str(tmp_path / "cgr-home"))
    monkeypatch.setenv("GO_FRONTEND", cs.GoFrontend.TREESITTER.value)


@pytest.fixture
def go_grammar() -> None:
    from codebase_rag.parser_loader import load_parsers

    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.GO not in parsers:
        pytest.skip("go parser not available")


def _run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest_subprocess(*PLUGIN, *args)


def _printed(result: pytest.RunResult) -> list[str]:
    return [
        line.removeprefix("PROBE ")
        for line in result.outlines
        if line.startswith("PROBE ")
    ]


def test_both_compares_a_previewed_language_and_finds_python_equal_to_itself(
    pytester: pytest.Pytester, clean_env: None, go_grammar: None
) -> None:
    pytester.makepyfile(PREVIEWED)

    result = _run(pytester, "-s", "--engine-mode", "both", "--engine-preview", "go")

    result.assert_outcomes(passed=1)
    assert "engine preview: go" in result.outlines
    # Once with every language on Python, once with the session's selection,
    # which the preview put Go on Rust for; and no report.
    assert _printed(result) == [
        "preview=['go']",
        "rust=['go']",
        "replicas=[False, True]",
    ]
    assert not any("engines differ" in line for line in result.outlines)


@pytest.mark.parametrize(
    ("mode", "rust", "replicas"),
    [("python", "[]", "[]"), ("rust", "['go']", "[]")],
)
def test_only_both_compares_and_only_a_selection_asking_for_rust_moves(
    pytester: pytest.Pytester,
    clean_env: None,
    go_grammar: None,
    mode: str,
    rust: str,
    replicas: str,
) -> None:
    # `python` leaves Go on its default, Python, preview or not.
    pytester.makepyfile(PREVIEWED)

    result = _run(pytester, "-s", "--engine-mode", mode, "--engine-preview", "go")

    result.assert_outcomes(passed=1)
    assert _printed(result) == [
        "preview=['go']",
        f"rust={rust}",
        f"replicas={replicas}",
    ]


def test_without_the_option_nothing_is_previewed(
    pytester: pytest.Pytester, clean_env: None, go_grammar: None
) -> None:
    pytester.makepyfile(PREVIEWED)

    result = _run(pytester, "-s", "--engine-mode", "both")

    result.assert_outcomes(passed=1)
    assert not any(line.startswith("engine preview") for line in result.outlines)
    assert _printed(result) == ["preview=[]", "rust=[]", "replicas=[]"]


@pytest.mark.parametrize(
    ("value", "refusal"),
    [
        ("fortran", "'fortran' is not a language"),
        ("c", "this preview names only c."),
    ],
)
def test_a_value_that_is_no_language_or_part_of_a_family_is_refused(
    pytester: pytest.Pytester, clean_env: None, value: str, refusal: str
) -> None:
    pytester.makepyfile("def test_nothing():\n    pass\n")

    result = _run(pytester, "--engine-preview", value)

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert refusal in result.stderr.str()
