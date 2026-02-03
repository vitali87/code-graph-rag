from __future__ import annotations

from pathlib import Path

from codebase_rag.utils.source_extraction import (
    extract_source_lines,
    extract_source_with_fallback,
    validate_source_location,
)


class TestExtractSourceLines:
    def test_extracts_single_line(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\nline2\nline3\n")

        result = extract_source_lines(file_path, 2, 2)

        assert result == "line2"

    def test_extracts_multiple_lines(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\nline2\nline3\nline4\n")

        result = extract_source_lines(file_path, 2, 3)

        assert result == "line2\nline3"

    def test_extracts_all_lines(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\nline2\nline3\n")

        result = extract_source_lines(file_path, 1, 3)

        assert result == "line1\nline2\nline3"

    def test_strips_trailing_whitespace(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="  code  \n  more  \n")

        result = extract_source_lines(file_path, 1, 2)

        assert result == "code  \n  more"

    def test_returns_none_for_nonexistent_file(self, tmp_path: Path) -> None:
        file_path = tmp_path / "nonexistent.py"

        result = extract_source_lines(file_path, 1, 1)

        assert result is None

    def test_returns_none_for_zero_start_line(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\n")

        result = extract_source_lines(file_path, 0, 1)

        assert result is None

    def test_returns_none_for_negative_start_line(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\n")

        result = extract_source_lines(file_path, -1, 1)

        assert result is None

    def test_returns_none_for_zero_end_line(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\n")

        result = extract_source_lines(file_path, 1, 0)

        assert result is None

    def test_returns_none_for_start_greater_than_end(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\nline2\n")

        result = extract_source_lines(file_path, 2, 1)

        assert result is None

    def test_returns_none_when_start_exceeds_file_length(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\nline2\n")

        result = extract_source_lines(file_path, 5, 6)

        assert result is None

    def test_returns_none_when_end_exceeds_file_length(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\nline2\n")

        result = extract_source_lines(file_path, 1, 10)

        assert result is None

    def test_handles_empty_file(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="")

        result = extract_source_lines(file_path, 1, 1)

        assert result is None

    def test_preserves_indentation(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="def func():\n    return 42\n")

        result = extract_source_lines(file_path, 1, 2)

        assert result == "def func():\n    return 42"


class TestExtractSourceWithFallback:
    def test_uses_line_extraction_when_no_ast_extractor(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\nline2\n")

        result = extract_source_with_fallback(file_path, 1, 2)

        assert result == "line1\nline2"

    def test_uses_ast_extractor_when_provided(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\nline2\n")

        def mock_ast_extractor(name: str, path: Path) -> str:
            return f"AST result for {name}"

        result = extract_source_with_fallback(
            file_path, 1, 2, qualified_name="my.func", ast_extractor=mock_ast_extractor
        )

        assert result == "AST result for my.func"

    def test_falls_back_to_lines_when_ast_extractor_returns_none(
        self, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\nline2\n")

        def mock_ast_extractor(name: str, path: Path) -> None:
            return None

        result = extract_source_with_fallback(
            file_path, 1, 2, qualified_name="my.func", ast_extractor=mock_ast_extractor
        )

        assert result == "line1\nline2"

    def test_falls_back_to_lines_when_ast_extractor_raises(
        self, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\nline2\n")

        def mock_ast_extractor(name: str, path: Path) -> str:
            raise RuntimeError("AST extraction failed")

        result = extract_source_with_fallback(
            file_path, 1, 2, qualified_name="my.func", ast_extractor=mock_ast_extractor
        )

        assert result == "line1\nline2"

    def test_skips_ast_when_qualified_name_is_none(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\nline2\n")
        ast_called = False

        def mock_ast_extractor(name: str, path: Path) -> str:
            nonlocal ast_called
            ast_called = True
            return "AST result"

        result = extract_source_with_fallback(
            file_path, 1, 2, qualified_name=None, ast_extractor=mock_ast_extractor
        )

        assert result == "line1\nline2"
        assert ast_called is False

    def test_skips_ast_when_extractor_is_none(self, tmp_path: Path) -> None:
        file_path = tmp_path / "test.py"
        file_path.write_text(encoding="utf-8", data="line1\nline2\n")

        result = extract_source_with_fallback(
            file_path, 1, 2, qualified_name="my.func", ast_extractor=None
        )

        assert result == "line1\nline2"


class TestValidateSourceLocation:
    def test_returns_true_for_valid_location(self) -> None:
        valid, path = validate_source_location("/path/to/file.py", 1, 10)

        assert valid is True
        assert path == Path("/path/to/file.py")

    def test_returns_false_when_file_path_is_none(self) -> None:
        valid, path = validate_source_location(None, 1, 10)

        assert valid is False
        assert path is None

    def test_returns_false_when_start_line_is_none(self) -> None:
        valid, path = validate_source_location("/path/to/file.py", None, 10)

        assert valid is False
        assert path is None

    def test_returns_false_when_end_line_is_none(self) -> None:
        valid, path = validate_source_location("/path/to/file.py", 1, None)

        assert valid is False
        assert path is None

    def test_returns_false_when_all_are_none(self) -> None:
        valid, path = validate_source_location(None, None, None)

        assert valid is False
        assert path is None

    def test_handles_empty_string_path(self) -> None:
        valid, path = validate_source_location("", 1, 10)

        assert valid is False
        assert path is None

    def test_converts_string_to_path(self) -> None:
        valid, path = validate_source_location("relative/path.py", 1, 10)

        assert valid is True
        assert path == Path("relative/path.py")

    def test_handles_windows_style_path(self) -> None:
        valid, path = validate_source_location("C:\\Users\\test\\file.py", 1, 10)

        assert valid is True
        assert path is not None
