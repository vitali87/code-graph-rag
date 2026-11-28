"""`index_engine.PREVIEW`: a language on the Rust engine while its port is
under way.

docs/reports/PHASE3_GO.md, section 10 (step 2). Until Go joins the languages
the build lists, the engine runs only where a preview names Go. The preview
is a module flag, not a setting: the test plugin's `--engine-preview` and the
scripts' `--preview` set it in their own process. Without one, every run is
today's; with one, a selection asking for a previewed language on Rust puts
it there, and while the engine holds none of its analysis Python's passes
still index its files, so the graph is the same.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import index_engine
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.index_engine import (
    asked_index_engines,
    parse_index_engine,
    parse_preview,
    preview_error,
    resolve_index_engines,
    rust_engine_languages,
)
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import _MockIngestor
from codebase_rag.tests.test_graph_updater_index_engine import (
    LANGUAGE_FIXTURES,
    _calls,
    _write,
)

L = cs.SupportedLanguage
E = cs.IndexEngine
_WARNED = (
    "CGR_INDEX_ENGINE asks for go on Rust, which this build cannot index it "
    "on; go indexes on Python"
)


# A fresh interpreter's preview and the languages its build lists.
_DEFAULTS = (
    "from codebase_rag import index_engine\n"
    "print(len(index_engine.PREVIEW), len(index_engine.rust_engine_languages()))\n"
)
_REPO_ROOT = Path(__file__).resolve().parents[2]
# A deadline for the child process, not a performance assertion.
_CHILD_TIMEOUT_SECONDS = 120


@pytest.fixture
def go_previewed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(index_engine, "PREVIEW", frozenset({L.GO}))


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    # Each test starts with no language warned about yet, as a new process does.
    monkeypatch.setattr(index_engine, "_warned", set())
    seen: list[str] = []
    sink = logger.add(
        lambda message: seen.append(message.record["message"]),
        level="WARNING",
        filter=lambda record: record["name"] == index_engine.__name__,
    )
    yield seen
    logger.remove(sink)


class TestThePreview:
    def test_none_unless_asked(self) -> None:
        # In a process of its own: a session with `--engine-preview` has set
        # one in this.
        result = subprocess.run(
            [sys.executable, "-c", _DEFAULTS],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            timeout=_CHILD_TIMEOUT_SECONDS,
            check=False,
        )

        assert result.returncode == 0, result.stderr[-4000:]
        assert result.stdout.split() == ["0", "0"]

    @pytest.mark.usefixtures("no_engine_preview")
    def test_without_one_the_build_lists_nothing(self) -> None:
        assert rust_engine_languages() == frozenset()

    def test_it_is_what_the_build_lists(self, go_previewed: None) -> None:
        assert rust_engine_languages() == frozenset({L.GO})

    def test_a_previewed_language_runs_on_rust_where_asked(
        self, go_previewed: None, warnings: list[str]
    ) -> None:
        named = resolve_index_engines(parse_index_engine("python,go=rust"))
        everything = resolve_index_engines(parse_index_engine("rust"))
        default = resolve_index_engines(parse_index_engine("auto"))

        assert named == {**dict.fromkeys(L, E.PYTHON), L.GO: E.RUST}
        assert everything == named
        # Go's default is still Python: a preview moves no default.
        assert default == dict.fromkeys(L, E.PYTHON)
        assert warnings == []

    @pytest.mark.usefixtures("no_engine_preview")
    def test_without_it_go_on_rust_is_warned_about_and_runs_on_python(
        self, warnings: list[str]
    ) -> None:
        engines = resolve_index_engines(parse_index_engine("go=rust"))

        assert engines == dict.fromkeys(L, E.PYTHON)
        assert warnings == [_WARNED]


class TestTheValue:
    @pytest.mark.parametrize(
        ("spec", "languages"),
        [
            ("go", {L.GO}),
            ("GO", {L.GO}),
            ("", set()),
            ("c,cpp", {L.C, L.CPP}),
            ("javascript; typescript tsx", {L.JS, L.TS, L.TSX}),
            ("go,java", {L.GO, L.JAVA}),
        ],
    )
    def test_it_reads_language_names(self, spec: str, languages: set[L]) -> None:
        assert parse_preview(spec) == languages
        assert preview_error(spec) is None

    @pytest.mark.parametrize(
        ("spec", "refusal"),
        [
            (
                "fortran",
                "'fortran' is not a language; use one of " + ", ".join(L) + ".",
            ),
            (
                "c",
                "c, cpp take one engine together, as they resolve each other's "
                "imports and includes; this preview names only c.",
            ),
            (
                "go,typescript",
                "javascript, tsx, typescript take one engine together, as they "
                "resolve each other's imports and includes; this preview names "
                "only typescript.",
            ),
        ],
    )
    def test_it_refuses_what_is_no_language_or_part_of_a_family(
        self, spec: str, refusal: str
    ) -> None:
        with pytest.raises(ValueError) as caught:
            parse_preview(spec)

        assert str(caught.value) == refusal
        assert preview_error(spec) == refusal


class TestWhatASelectionAsksFor:
    def test_auto_is_this_release_s_default(self) -> None:
        assert asked_index_engines(parse_index_engine("auto")) == {
            language: engine
            for unit, engine in cs.INDEX_ENGINE_DEFAULTS.items()
            for language in sorted(unit)
        }

    def test_rust_is_asked_for_whether_or_not_the_build_has_it(self) -> None:
        asked = asked_index_engines(parse_index_engine("python,go=rust"))

        assert asked == {**dict.fromkeys(L, E.PYTHON), L.GO: E.RUST}
        assert list(asked) == list(L)


def _index(repo: Path, state: Path, spec: str) -> tuple[GraphUpdater, list]:
    parsers, queries = load_parsers()
    ingestor = _MockIngestor()
    updater = GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        skip_embeddings=True,
        state_dir=state,
        index_engine=parse_index_engine(spec),
    )
    updater.run()
    return updater, _calls(ingestor)


@pytest.mark.usefixtures("no_engine_preview")
class TestAnIndexOnThePreview:
    def test_go_is_on_rust_and_indexed_as_python_indexes_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fingerprint_inputs_once_per_session: None,
    ) -> None:
        monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
        repo = _write(tmp_path / "repo", LANGUAGE_FIXTURES[L.GO])
        python, python_calls = _index(repo, tmp_path / "state-python", "python")
        monkeypatch.setattr(index_engine, "PREVIEW", frozenset({L.GO}))

        previewed, previewed_calls = _index(
            repo, tmp_path / "state-go-previewed", "go=rust"
        )

        assert python.rust_languages == frozenset()
        assert previewed.rust_languages == frozenset({L.GO})
        # The engine holds none of Go's analysis yet: Python's passes wrote
        # the same graph, call for call.
        assert previewed_calls == python_calls

    def test_an_updater_resolves_its_selection_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A preview set after an updater was made does not move it: a run
        # never straddles two selections.
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=MagicMock(),
            repo_path=_write(tmp_path / "repo", LANGUAGE_FIXTURES[L.GO]),
            parsers=parsers,
            queries=queries,
            skip_embeddings=True,
            state_dir=tmp_path / "state",
            index_engine=parse_index_engine("go=rust"),
        )
        monkeypatch.setattr(index_engine, "PREVIEW", frozenset({L.GO}))

        assert updater.rust_languages == frozenset()
        assert updater.engines[L.GO] == E.PYTHON
