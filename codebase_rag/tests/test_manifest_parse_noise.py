"""Empty or invalid dependency manifests are not run errors (issue #2568).

Test suites for package managers, bundlers and linters commit fixture
manifests that are empty or broken on purpose. Each one logged an ERROR on
every full index, the level users and CI log scanners read as "the run
failed", although the run succeeded and the file declares nothing. A manifest
whose content cannot be read now logs at DEBUG per file and the run names
them in one WARNING; an empty one stays at DEBUG, since nothing is lost.
"""

from __future__ import annotations

import gc
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.models import Dependency
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import dependency_parser
from codebase_rag.parsers.dependency_parser import parse_dependencies
from codebase_rag.tests.conftest import create_and_run_updater
from evals.cgr_graph import _StatefulIngestor

_NVM_FIXTURE_DIRS = (
    "nested-both",
    "nested-pkg",
    "no-nesting-both",
    "no-nesting-pkg",
)
_NVM_FIXTURE_ROOT = "test/fast/Unit tests/mocks/project_dirs"

_ROOT_PACKAGE_JSON = '{"name": "app", "dependencies": {"left-pad": "^1.3.0"}}\n'

# One broken document per manifest format. The line-based formats (go.mod,
# requirements.txt, Gemfile, pubspec.yaml) accept any text, so the only way
# their content fails is bytes that are not UTF-8.
_NOT_UTF8 = b"\xff\xfe\x00broken\n"
_INVALID_MANIFESTS = pytest.mark.parametrize(
    ("name", "content"),
    [
        ("package.json", b'{"dependencies": {"a": "1"'),
        ("composer.json", b"not json {{{"),
        ("pyproject.toml", b"this is not valid toml [[["),
        ("Cargo.toml", b"[dependencies\nserde = 1"),
        ("App.csproj", b"<Project><ItemGroup></Project>"),
        ("go.mod", _NOT_UTF8),
        ("requirements.txt", _NOT_UTF8),
        ("Gemfile", _NOT_UTF8),
        ("pubspec.yaml", _NOT_UTF8),
    ],
    ids=[
        "package.json",
        "composer.json",
        "pyproject.toml",
        "Cargo.toml",
        "csproj",
        "go.mod",
        "requirements.txt",
        "Gemfile",
        "pubspec.yaml",
    ],
)

# The formats whose parser rejects an empty document; the others read an
# empty file as a manifest with no dependencies and never failed on one.
_EMPTY_MANIFESTS = pytest.mark.parametrize(
    ("name", "content"),
    [
        ("package.json", b""),
        ("package.json", b"  \n\n"),
        ("composer.json", b""),
        ("App.csproj", b""),
    ],
    ids=["package.json", "package.json-whitespace", "composer.json", "csproj"],
)


@pytest.fixture
def records() -> Generator[list[tuple[str, str]], None, None]:
    # Finalize earlier tests' garbage before capturing: ImportProcessor.__del__
    # saves the stdlib cache and logs at DEBUG, which would otherwise land in
    # this capture whenever the cyclic GC happens to run mid-test.
    gc.collect()
    captured: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: captured.append(
            (message.record["level"].name, message.record["message"])
        ),
        level="DEBUG",
    )
    yield captured
    logger.remove(sink_id)


def _at_least_warning(records: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(lvl, msg) for lvl, msg in records if lvl in ("WARNING", "ERROR")]


_MANIFEST_MARKERS = ("manifest", "Error parsing ", "package.json", "Cargo.toml")


def _loud_about_manifests(
    records: list[tuple[str, str]], repo: Path
) -> list[tuple[str, str]]:
    # A run over the mock ingestor also warns about things unrelated to this
    # issue (the first-sync seed prune, #2404), so a run-level check keeps to
    # the lines that concern a manifest: by name, by path or by wording.
    return [
        (lvl, msg)
        for lvl, msg in _at_least_warning(records)
        if str(repo) in msg or any(marker in msg for marker in _MANIFEST_MARKERS)
    ]


def _write(path: Path, content: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


def _depends_on(mock_ingestor: MagicMock) -> set[str]:
    return {
        c.args[2][2]
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == "DEPENDS_ON_EXTERNAL"
    }


class TestPerFileLevel:
    @_EMPTY_MANIFESTS
    def test_an_empty_manifest_logs_nothing_above_debug(
        self,
        tmp_path: Path,
        records: list[tuple[str, str]],
        name: str,
        content: bytes,
    ) -> None:
        manifest = _write(tmp_path / name, content)

        assert parse_dependencies(manifest) == []

        assert _at_least_warning(records) == []
        assert any(lvl == "DEBUG" and str(manifest) in msg for lvl, msg in records), (
            records
        )

    @_INVALID_MANIFESTS
    def test_an_invalid_manifest_logs_at_debug_not_error(
        self,
        tmp_path: Path,
        records: list[tuple[str, str]],
        name: str,
        content: bytes,
    ) -> None:
        manifest = _write(tmp_path / name, content)

        assert parse_dependencies(manifest) == []

        # Per file it is a DEBUG line naming the file and why; the run's one
        # WARNING is the updater's to log, not the parser's.
        assert _at_least_warning(records) == []
        assert any(lvl == "DEBUG" and str(manifest) in msg for lvl, msg in records), (
            records
        )


class TestIndexRun:
    def test_empty_fixture_manifests_log_nothing_above_debug(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        records: list[tuple[str, str]],
    ) -> None:
        # The issue's nvm-sh/nvm layout: four committed 0-byte package.json
        # fixtures beside the project's own manifest.
        _write(temp_repo / "package.json", _ROOT_PACKAGE_JSON)
        for fixture in _NVM_FIXTURE_DIRS:
            _write(temp_repo / _NVM_FIXTURE_ROOT / fixture / "package.json", b"")

        create_and_run_updater(temp_repo, mock_ingestor)

        assert _loud_about_manifests(records, temp_repo) == []

    def test_invalid_fixture_manifests_log_one_warning_and_no_error(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        records: list[tuple[str, str]],
    ) -> None:
        _write(temp_repo / "package.json", _ROOT_PACKAGE_JSON)
        for fixture in _NVM_FIXTURE_DIRS:
            _write(temp_repo / _NVM_FIXTURE_ROOT / fixture / "package.json", b"")
        broken_json = _write(
            temp_repo / "test/fixtures/broken/package.json", b'{"name": '
        )
        broken_toml = _write(
            temp_repo / "test/fixtures/broken/Cargo.toml", b"[dependencies\n"
        )

        create_and_run_updater(temp_repo, mock_ingestor)

        loud = _loud_about_manifests(records, temp_repo)
        assert [lvl for lvl, _msg in loud] == ["WARNING"], loud
        summary = loud[0][1]
        # Counts only the two that lost something: the empty ones declare
        # nothing, so they are not in it.
        assert summary.startswith("2 "), summary
        assert "test/fixtures/broken/" in summary, summary
        # Each one is still listed at DEBUG for whoever wants the detail.
        debug = [msg for lvl, msg in records if lvl == "DEBUG"]
        for manifest in (broken_json, broken_toml):
            assert any(str(manifest) in msg for msg in debug), manifest

    def test_a_valid_root_manifest_still_yields_its_dependencies(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        records: list[tuple[str, str]],
    ) -> None:
        # Negative test: the broken fixtures beside it cost the project's own
        # manifest nothing.
        _write(temp_repo / "package.json", _ROOT_PACKAGE_JSON)
        _write(temp_repo / "test/fixtures/broken/package.json", b"{")
        _write(temp_repo / _NVM_FIXTURE_ROOT / "nested-pkg" / "package.json", b"")

        create_and_run_updater(temp_repo, mock_ingestor)

        assert _depends_on(mock_ingestor) == {"left-pad"}

    def test_a_broken_root_manifest_is_still_named(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        records: list[tuple[str, str]],
    ) -> None:
        # Negative test: the project's own manifest is the one that describes
        # its dependencies, so when it is broken a reader must still be told
        # which file, even among fixtures that sort ahead of it by name.
        root = _write(temp_repo / "package.json", b'{"dependencies": ')
        _write(temp_repo / "a-fixtures/one/package.json", b"{")
        _write(temp_repo / "a-fixtures/two/package.json", b"[")

        create_and_run_updater(temp_repo, mock_ingestor)

        # Named by its absolute path per file, or as the one the run's summary
        # leads with, ahead of `a-fixtures/...`, which sorts first by name.
        loud = _loud_about_manifests(records, temp_repo)
        assert any(
            str(root) in msg or "first: package.json:" in msg for _lvl, msg in loud
        ), loud

    def test_a_clean_repo_logs_nothing_new(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        records: list[tuple[str, str]],
    ) -> None:
        # Negative test: valid manifests of every kind index as before, with
        # no warning, no error and none of the new per-file lines.
        _write(temp_repo / "package.json", _ROOT_PACKAGE_JSON)
        _write(
            temp_repo / "pyproject.toml",
            '[project]\nname = "p"\nversion = "0"\ndependencies = ["requests>=2"]\n',
        )
        _write(temp_repo / "Cargo.toml", '[dependencies]\nserde = "1"\n')
        _write(temp_repo / "m.py", "def f():\n    return 1\n")

        create_and_run_updater(temp_repo, mock_ingestor)

        assert _loud_about_manifests(records, temp_repo) == []
        assert not [msg for _lvl, msg in records if "could not be parsed" in msg]
        assert not [msg for _lvl, msg in records if " is empty" in msg]
        assert _depends_on(mock_ingestor) == {"left-pad", "requests", "serde"}


class TestReingest:
    def test_a_reingest_logs_one_warning_and_no_error(
        self, temp_repo: Path, records: list[tuple[str, str]]
    ) -> None:
        # The watcher and the MCP tool re-parse through `reingest`, which
        # meets manifests the same way and must summarise them the same way.
        _write(temp_repo / "package.json", _ROOT_PACKAGE_JSON)
        _write(temp_repo / "m.py", "def f():\n    return 1\n")
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=_StatefulIngestor(),
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        )
        updater.run(force=True)
        changed = ["test/fixtures/a/package.json", "test/fixtures/b/package.json"]
        for rel in changed:
            _write(temp_repo / rel, b"{")
        records.clear()

        updater.reingest(changed)

        loud = _loud_about_manifests(records, temp_repo)
        assert [lvl for lvl, _msg in loud] == ["WARNING"], loud
        assert loud[0][1].startswith("2 "), loud


class TestNeighbours:
    def test_a_valid_manifest_still_yields_its_dependencies(
        self, tmp_path: Path, records: list[tuple[str, str]]
    ) -> None:
        manifest = _write(tmp_path / "package.json", _ROOT_PACKAGE_JSON)

        assert parse_dependencies(manifest) == [Dependency("left-pad", "^1.3.0")]
        assert records == []

    def test_a_failure_that_is_not_the_files_content_still_logs_an_error(
        self,
        tmp_path: Path,
        records: list[tuple[str, str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Negative test: only a manifest's own content is downgraded. A fault
        # inside the parser is a bug, and the #1070 gate relies on it staying
        # an "Error parsing " line at ERROR.
        def _boom(_path: Path) -> dict:
            raise RuntimeError("parser bug")

        monkeypatch.setattr(dependency_parser, "_load_toml", _boom)
        manifest = _write(tmp_path / "pyproject.toml", "[project]\n")

        assert parse_dependencies(manifest) == []

        errors = [msg for lvl, msg in records if lvl == "ERROR"]
        assert len(errors) == 1, records
        assert errors[0].startswith("Error parsing pyproject.toml ")
        assert "parser bug" in errors[0]


class TestPartialManifest:
    """A manifest that fails part-way keeps what it declared before the
    failure, as every parser did before the shared reader (Greptile, PR
    #2613)."""

    def test_valid_cargo_dependency_survives_a_wrong_type_dev_dependency(
        self, tmp_path: Path, records: list[tuple[str, str]]
    ) -> None:
        manifest = _write(
            tmp_path / "Cargo.toml",
            '[dependencies]\nserde = "1.0"\n\n[dev-dependencies]\nbroken = 1\n',
        )

        assert parse_dependencies(manifest) == [Dependency("serde", "1.0")]
        # A wrong-typed entry is a fault the parser cannot read past, and it
        # is still reported as one.
        errors = [msg for lvl, msg in records if lvl == "ERROR"]
        assert len(errors) == 1, records
        assert errors[0].startswith("Error parsing Cargo.toml ")

    def test_lines_before_bytes_that_are_not_utf8_are_kept(
        self, tmp_path: Path, records: list[tuple[str, str]]
    ) -> None:
        manifest = _write(
            tmp_path / "requirements.txt",
            b"requests==2.31.0\n" + b"x" * 70000 + b"\n\xff\xfe\n",
        )

        parsed = dependency_parser.read_manifest(manifest)

        assert parsed.dependencies[:1] == [Dependency("requests", "==2.31.0")]
        # Still named as unparsable: the rest of the file was not read.
        assert parsed.unparsable is not None
        assert _at_least_warning(records) == []

    def test_a_valid_cargo_manifest_yields_every_entry_and_no_error(
        self, tmp_path: Path, records: list[tuple[str, str]]
    ) -> None:
        manifest = _write(
            tmp_path / "Cargo.toml",
            '[dependencies]\nserde = "1.0"\n\n'
            '[dev-dependencies]\ntokio = { version = "1.38" }\n',
        )

        parsed = dependency_parser.read_manifest(manifest)

        assert parsed.dependencies == [
            Dependency("serde", "1.0"),
            Dependency("tokio", "1.38"),
        ]
        assert parsed.unparsable is None
        assert records == []

    def test_a_manifest_that_fails_to_load_still_yields_nothing(
        self, tmp_path: Path
    ) -> None:
        manifest = _write(tmp_path / "Cargo.toml", b"[dependencies\nserde = 1")

        parsed = dependency_parser.read_manifest(manifest)

        assert parsed.dependencies == []
        assert parsed.unparsable is not None
