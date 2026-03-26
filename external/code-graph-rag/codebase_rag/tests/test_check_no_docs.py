from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

from scripts.check_no_docs import (
    _has_allowed_marker,
    check_file,
    check_module_docstring,
    find_comment_start,
)


class TestFindCommentStart:
    def test_simple_comment(self) -> None:
        assert find_comment_start("x = 1  # comment") == 7

    def test_comment_at_start(self) -> None:
        assert find_comment_start("# full line comment") == 0

    def test_no_comment(self) -> None:
        assert find_comment_start("x = 1") is None

    def test_hash_in_single_quoted_string(self) -> None:
        assert find_comment_start("x = 'has # inside'") is None

    def test_hash_in_double_quoted_string(self) -> None:
        assert find_comment_start('x = "has # inside"') is None

    def test_comment_after_string_with_hash(self) -> None:
        result = find_comment_start("x = 'has # inside'  # real comment")
        assert result == 20

    def test_escaped_quote_in_string(self) -> None:
        assert find_comment_start(r"x = 'it\'s # here'") is None

    def test_mixed_quotes(self) -> None:
        assert find_comment_start('x = "it\'s # here"') is None

    def test_empty_string(self) -> None:
        assert find_comment_start("x = ''  # comment") == 8

    def test_multiple_strings(self) -> None:
        result = find_comment_start("x = 'a' + 'b'  # comment")
        assert result == 15


class TestHasAllowedMarker:
    def test_h_marker(self) -> None:
        assert _has_allowed_marker("# (H) this is allowed") is True

    def test_type_marker(self) -> None:
        assert _has_allowed_marker("# type: ignore") is True

    def test_noqa_marker(self) -> None:
        assert _has_allowed_marker("# noqa: E501") is True

    def test_pyright_marker(self) -> None:
        assert _has_allowed_marker("# pyright: ignore") is True

    def test_ty_marker(self) -> None:
        assert _has_allowed_marker("# ty: ignore") is True

    def test_protoc_marker(self) -> None:
        assert _has_allowed_marker("# @@protoc_insertion_point") is True

    def test_no_marker(self) -> None:
        assert _has_allowed_marker("# regular comment") is False

    def test_partial_match_not_allowed(self) -> None:
        assert _has_allowed_marker("# types are cool") is False


class TestCheckFile:
    def test_file_with_no_comments(self) -> None:
        content = "x = 1\ny = 2\n"
        with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(content)
            f.flush()
            errors = check_file(f.name)
        Path(f.name).unlink()
        assert errors == []

    def test_file_with_allowed_comment(self) -> None:
        content = "x = 1  # (H) allowed comment\n"
        with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(content)
            f.flush()
            errors = check_file(f.name)
        Path(f.name).unlink()
        assert errors == []

    def test_file_with_disallowed_comment(self) -> None:
        content = "x = 1  # bad comment\n"
        with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(content)
            f.flush()
            errors = check_file(f.name)
        Path(f.name).unlink()
        assert len(errors) == 1
        assert "bad comment" in errors[0]

    def test_shebang_and_module_docstring_detected(self) -> None:
        content = '#!/usr/bin/env python3\n"""Module docstring."""\nx = 1  # bad\n'
        with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(content)
            f.flush()
            errors = check_file(f.name)
        Path(f.name).unlink()
        assert len(errors) == 2
        assert any("module-level docstring found" in e for e in errors)
        assert any("bad" in e for e in errors)

    def test_multiline_string_not_treated_as_comment(self) -> None:
        content = 'x = """\nhas # inside\n"""\ny = 1  # bad\n'
        with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(content)
            f.flush()
            errors = check_file(f.name)
        Path(f.name).unlink()
        assert len(errors) == 1
        assert "bad" in errors[0]

    def test_type_ignore_comment_allowed(self) -> None:
        content = "x: int = 'bad'  # type: ignore\n"
        with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(content)
            f.flush()
            errors = check_file(f.name)
        Path(f.name).unlink()
        assert errors == []

    def test_noqa_comment_allowed(self) -> None:
        content = "from module import *  # noqa: F403\n"
        with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(content)
            f.flush()
            errors = check_file(f.name)
        Path(f.name).unlink()
        assert errors == []

    def test_multiple_errors_reported(self) -> None:
        content = "x = 1  # first bad\ny = 2  # second bad\n"
        with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(content)
            f.flush()
            errors = check_file(f.name)
        Path(f.name).unlink()
        assert len(errors) == 2


class TestCheckModuleDocstring:
    def test_no_docstring(self) -> None:
        lines = ["import os\n", "x = 1\n"]
        assert check_module_docstring("test.py", lines) is None

    def test_double_quote_docstring(self) -> None:
        lines = ['"""Module docstring."""\n', "x = 1\n"]
        result = check_module_docstring("test.py", lines)
        assert result is not None
        assert "module-level docstring found" in result

    def test_single_quote_docstring(self) -> None:
        lines = ["'''Module docstring.'''\n", "x = 1\n"]
        result = check_module_docstring("test.py", lines)
        assert result is not None
        assert "module-level docstring found" in result

    def test_empty_lines_before_code(self) -> None:
        lines = ["\n", "\n", "import os\n"]
        assert check_module_docstring("test.py", lines) is None

    def test_empty_lines_before_docstring(self) -> None:
        lines = ["\n", "\n", '"""Docstring."""\n']
        result = check_module_docstring("test.py", lines)
        assert result is not None
        assert "module-level docstring found" in result

    def test_shebang_then_docstring(self) -> None:
        lines = ["#!/usr/bin/env python3\n", '"""Docstring."""\n']
        result = check_module_docstring("test.py", lines)
        assert result is not None
        assert "test.py:2" in result
        assert "module-level docstring found" in result

    def test_shebang_then_code(self) -> None:
        lines = ["#!/usr/bin/env python3\n", "import os\n"]
        assert check_module_docstring("test.py", lines) is None

    def test_file_with_module_docstring_detected(self) -> None:
        content = '"""Module docstring."""\nx = 1\n'
        with NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(content)
            f.flush()
            errors = check_file(f.name)
        Path(f.name).unlink()
        assert len(errors) == 1
        assert "module-level docstring found" in errors[0]
