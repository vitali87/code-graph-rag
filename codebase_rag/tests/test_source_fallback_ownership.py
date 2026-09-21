from pathlib import Path
from typing import Literal
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import logs as ls
from codebase_rag.cypher_queries import CYPHER_LIST_PROJECTS
from codebase_rag.tools.code_retrieval import CodeRetriever
from codebase_rag.tools.semantic_search import get_function_source_code
from codebase_rag.utils.path_utils import (
    absolute_path_within_project_root,
    project_root_for_qualified_name,
)
from codebase_rag.utils.source_extraction import extract_source_lines

SOURCE = "def get_user(user_id):\n    return user_id\n"
QUALIFIED_NAME = "service.users.src.handlers.get_user"
RELATIVE_PATH = "src/handlers.py"
Reader = Literal["snippet", "function"]


def _make_source_ingestor(
    target: Path | None,
    roots: dict[str, str | None],
    relative_path: str = RELATIVE_PATH,
) -> MagicMock:
    node_rows = [
        {
            "qualified_name": QUALIFIED_NAME,
            "name": "get_user",
            "path": relative_path,
            "absolute_path": str(target) if target is not None else None,
            "start": 1,
            "end": 2,
            "start_line": 1,
            "end_line": 2,
            "docstring": None,
        }
    ]
    roots_rows = [{"name": name, "root_path": root} for name, root in roots.items()]
    ingestor = MagicMock()
    ingestor.fetch_all.side_effect = lambda query, params=None: (
        roots_rows if query == CYPHER_LIST_PROJECTS else node_rows
    )
    return ingestor


async def _read_source(
    reader: Reader, current_repo: Path, ingestor: MagicMock
) -> str | None:
    if reader == "snippet":
        result = await CodeRetriever(str(current_repo), ingestor).find_code_snippet(
            QUALIFIED_NAME
        )
        return result.source_code.strip() if result.found else None
    return get_function_source_code(ingestor, node_id=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["snippet", "function"])
class TestFallbackOwnership:
    @pytest.mark.parametrize("absolute_kind", ["stale", "absent", "outside-root"])
    async def test_foreign_source_does_not_read_colliding_local_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reader: Reader,
        absolute_kind: str,
    ) -> None:
        current_repo = tmp_path / "orders"
        collision = current_repo / RELATIVE_PATH
        collision.parent.mkdir(parents=True)
        collision.write_text(SOURCE, encoding="utf-8", newline="\n")
        other_repo = tmp_path / "users"
        other_repo.mkdir()
        monkeypatch.chdir(current_repo)
        target = other_repo / RELATIVE_PATH
        if absolute_kind == "absent":
            target = None
        elif absolute_kind == "outside-root":
            target = collision
        roots = {"service": str(current_repo), "service.users": str(other_repo)}

        result = await _read_source(
            reader, current_repo, _make_source_ingestor(target, roots)
        )

        assert result is None

    @pytest.mark.parametrize("absolute_kind", ["stale", "absent", "outside-root"])
    @pytest.mark.parametrize("local_collision", [False, True])
    async def test_foreign_relative_source_is_resolved_from_its_indexed_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reader: Reader,
        absolute_kind: str,
        local_collision: bool,
    ) -> None:
        current_repo = tmp_path / "orders"
        current_repo.mkdir()
        collision = current_repo / RELATIVE_PATH
        if local_collision:
            collision.parent.mkdir(parents=True)
            collision.write_text("wrong source\nwrong source\n", encoding="utf-8")
        other_repo = tmp_path / "users"
        source = other_repo / RELATIVE_PATH
        source.parent.mkdir(parents=True)
        source.write_text(SOURCE, encoding="utf-8", newline="\n")
        monkeypatch.chdir(current_repo)
        target = tmp_path / "old-users" / RELATIVE_PATH
        if absolute_kind == "absent":
            target = None
        elif absolute_kind == "outside-root":
            target = collision
        roots = {"service": str(current_repo), "service.users": str(other_repo)}

        result = await _read_source(
            reader, current_repo, _make_source_ingestor(target, roots)
        )

        assert result == SOURCE.strip()

    async def test_stale_node_path_can_fall_back_inside_its_current_project_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reader: Reader,
    ) -> None:
        current_repo = tmp_path / "users"
        local = current_repo / RELATIVE_PATH
        local.parent.mkdir(parents=True)
        local.write_text(SOURCE, encoding="utf-8", newline="\n")
        monkeypatch.chdir(current_repo)
        stale = tmp_path / "old-users" / RELATIVE_PATH
        roots = {"service.users": str(current_repo)}

        result = await _read_source(
            reader, current_repo, _make_source_ingestor(stale, roots)
        )

        assert result == SOURCE.strip()

    @pytest.mark.parametrize("roots", [{}, {"service.users": None}])
    async def test_unknown_legacy_root_keeps_relative_fallback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reader: Reader,
        roots: dict[str, str | None],
    ) -> None:
        local = tmp_path / RELATIVE_PATH
        local.parent.mkdir(parents=True)
        local.write_text(SOURCE, encoding="utf-8", newline="\n")
        monkeypatch.chdir(tmp_path)

        result = await _read_source(
            reader, tmp_path, _make_source_ingestor(None, roots)
        )

        assert result == SOURCE.strip()

    async def test_foreign_absolute_source_still_wins_over_local_collision(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reader: Reader,
    ) -> None:
        current_repo = tmp_path / "orders"
        collision = current_repo / RELATIVE_PATH
        collision.parent.mkdir(parents=True)
        collision.write_text("wrong source\nwrong source\n", encoding="utf-8")
        foreign = tmp_path / "users" / RELATIVE_PATH
        foreign.parent.mkdir(parents=True)
        foreign.write_text(SOURCE, encoding="utf-8", newline="\n")
        monkeypatch.chdir(current_repo)
        roots = {
            "service": str(current_repo),
            "service.users": str(tmp_path / "users"),
        }

        result = await _read_source(
            reader, current_repo, _make_source_ingestor(foreign, roots)
        )

        assert result == SOURCE.strip()

    async def test_stale_project_root_is_not_assumed_to_have_moved_to_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reader: Reader,
    ) -> None:
        local = tmp_path / RELATIVE_PATH
        local.parent.mkdir(parents=True)
        local.write_text(SOURCE, encoding="utf-8", newline="\n")
        monkeypatch.chdir(tmp_path)
        stale_root = tmp_path / "no-longer-exists"

        result = await _read_source(
            reader,
            tmp_path,
            _make_source_ingestor(
                stale_root / RELATIVE_PATH, {"service.users": str(stale_root)}
            ),
        )

        assert result is None

    async def test_relative_traversal_cannot_escape_known_project_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reader: Reader,
    ) -> None:
        current_repo = tmp_path / "users"
        current_repo.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text(SOURCE, encoding="utf-8", newline="\n")
        monkeypatch.chdir(current_repo)
        roots = {"service.users": str(current_repo)}

        result = await _read_source(
            reader, current_repo, _make_source_ingestor(None, roots, "../outside.py")
        )

        assert result is None

    async def test_fallback_symlink_cannot_escape_known_project_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reader: Reader,
    ) -> None:
        current_repo = tmp_path / "users"
        link = current_repo / RELATIVE_PATH
        link.parent.mkdir(parents=True)
        outside = tmp_path / "outside.py"
        outside.write_text(SOURCE, encoding="utf-8", newline="\n")
        try:
            link.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
        monkeypatch.chdir(current_repo)
        roots = {"service.users": str(current_repo)}

        result = await _read_source(
            reader, current_repo, _make_source_ingestor(None, roots)
        )

        assert result is None

    async def test_fallback_reuses_cached_project_roots(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reader: Reader,
    ) -> None:
        local = tmp_path / RELATIVE_PATH
        local.parent.mkdir(parents=True)
        local.write_text(SOURCE, encoding="utf-8", newline="\n")
        monkeypatch.chdir(tmp_path)
        ingestor = _make_source_ingestor(None, {"service.users": str(tmp_path)})
        if reader == "snippet":
            retriever = CodeRetriever(str(tmp_path), ingestor)
            for _ in range(3):
                result = await retriever.find_code_snippet(QUALIFIED_NAME)
                assert result.found
                assert result.source_code == SOURCE
        else:
            roots_cache: dict[str, dict[str, str | None]] = {}
            for _ in range(3):
                assert (
                    get_function_source_code(
                        ingestor, node_id=1, roots_cache=roots_cache
                    )
                    == SOURCE.strip()
                )

        roots_calls = [
            call
            for call in ingestor.fetch_all.call_args_list
            if call.args[0] == CYPHER_LIST_PROJECTS
        ]
        assert len(roots_calls) == 1


@pytest.mark.parametrize("include_parent", [False, True])
def test_exact_project_name_uses_its_own_root(
    tmp_path: Path, include_parent: bool
) -> None:
    owner_root = tmp_path / "users"
    roots = {"service.users": str(owner_root)}
    if include_parent:
        roots["service"] = str(tmp_path)

    assert project_root_for_qualified_name("service.users", roots) == owner_root
    assert absolute_path_within_project_root(
        "service.users", str(owner_root / RELATIVE_PATH), roots
    )
    assert not absolute_path_within_project_root(
        "service.users", str(tmp_path / RELATIVE_PATH), roots
    )


def test_exact_project_name_without_root_keeps_legacy_behavior(tmp_path: Path) -> None:
    roots = {"service": str(tmp_path / "parent"), "service.users": None}

    assert project_root_for_qualified_name("service.users", roots) is None
    assert absolute_path_within_project_root(
        "service.users", str(tmp_path / RELATIVE_PATH), roots
    )


def test_project_name_requires_a_segment_boundary(tmp_path: Path) -> None:
    roots = {"service.users": str(tmp_path / "users")}

    assert project_root_for_qualified_name("service.users2", roots) is None
    roots["service"] = str(tmp_path)
    assert project_root_for_qualified_name("service.users2", roots) == tmp_path


@pytest.mark.parametrize("fallback_kind", ["missing", "directory"])
def test_function_source_rejects_non_file_fallback(
    tmp_path: Path, fallback_kind: str
) -> None:
    target = tmp_path / RELATIVE_PATH
    if fallback_kind == "directory":
        target.mkdir(parents=True)
    ingestor = _make_source_ingestor(None, {"service.users": str(tmp_path)})

    with (
        patch("codebase_rag.tools.semantic_search.logger") as mock_logger,
        patch(
            "codebase_rag.utils.source_extraction.extract_source_lines",
            wraps=extract_source_lines,
        ) as mock_extract,
    ):
        assert get_function_source_code(ingestor, node_id=1) is None

    mock_extract.assert_not_called()
    mock_logger.warning.assert_called_once_with(
        ls.SEMANTIC_INVALID_LOCATION.format(id=1)
    )
    mock_logger.error.assert_not_called()
