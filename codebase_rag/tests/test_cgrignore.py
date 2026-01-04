from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.config import CGRIGNORE_FILENAME, load_cgrignore_patterns


def test_returns_empty_when_no_file(temp_repo: Path) -> None:
    result = load_cgrignore_patterns(temp_repo)
    assert result == frozenset()


def test_loads_patterns_from_file(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("vendor\nmy_build\n")

    result = load_cgrignore_patterns(temp_repo)

    assert "vendor" in result
    assert "my_build" in result
    assert len(result) == 2


def test_ignores_comments_and_blank_lines(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("# Comment\n\nvendor\n  # Indented comment\n")

    result = load_cgrignore_patterns(temp_repo)

    assert result == frozenset({"vendor"})


def test_strips_whitespace(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("  vendor  \n\ttemp\t\n")

    result = load_cgrignore_patterns(temp_repo)

    assert "vendor" in result
    assert "temp" in result


def test_returns_empty_on_read_error(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("vendor")

    original_open = Path.open

    def mock_open(self: Path, *args, **kwargs):  # noqa: ANN002, ANN003
        if self.name == CGRIGNORE_FILENAME:
            raise PermissionError("Cannot read")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", mock_open)

    result = load_cgrignore_patterns(temp_repo)
    assert result == frozenset()


def test_handles_duplicates(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("vendor\nvendor\ntemp\n")

    result = load_cgrignore_patterns(temp_repo)

    assert len(result) == 2
