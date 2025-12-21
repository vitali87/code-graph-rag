from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import Tool

from codebase_rag.tools.file_writer import (
    FileCreationResult,
    FileWriter,
    create_file_writer_tool,
)

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def temp_project_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def file_writer(temp_project_root: Path) -> FileWriter:
    return FileWriter(str(temp_project_root))


class TestFileWriterInit:
    def test_init_resolves_project_root(self, temp_project_root: Path) -> None:
        writer = FileWriter(str(temp_project_root))
        assert writer.project_root == temp_project_root.resolve()

    def test_init_with_relative_path(self) -> None:
        writer = FileWriter(".")
        assert writer.project_root == Path(".").resolve()


class TestFileCreationResult:
    def test_success_result(self) -> None:
        result = FileCreationResult(file_path="test.txt")
        assert result.file_path == "test.txt"
        assert result.success is True
        assert result.error_message is None

    def test_error_result(self) -> None:
        result = FileCreationResult(
            file_path="test.txt", success=False, error_message="Error occurred"
        )
        assert result.file_path == "test.txt"
        assert result.success is False
        assert result.error_message == "Error occurred"


class TestCreateFile:
    async def test_create_new_file(
        self, file_writer: FileWriter, temp_project_root: Path
    ) -> None:
        result = await file_writer.create_file("new_file.txt", "Hello, World!")
        assert result.success is True
        assert result.error_message is None
        created_file = temp_project_root / "new_file.txt"
        assert created_file.exists()
        assert created_file.read_text(encoding="utf-8") == "Hello, World!"

    async def test_create_file_in_subdirectory(
        self, file_writer: FileWriter, temp_project_root: Path
    ) -> None:
        result = await file_writer.create_file(
            "subdir/nested/file.txt", "Nested content"
        )
        assert result.success is True
        created_file = temp_project_root / "subdir" / "nested" / "file.txt"
        assert created_file.exists()
        assert created_file.read_text(encoding="utf-8") == "Nested content"

    async def test_overwrite_existing_file(
        self, file_writer: FileWriter, temp_project_root: Path
    ) -> None:
        existing_file = temp_project_root / "existing.txt"
        existing_file.write_text("Old content", encoding="utf-8")
        result = await file_writer.create_file("existing.txt", "New content")
        assert result.success is True
        assert existing_file.read_text(encoding="utf-8") == "New content"

    async def test_create_file_outside_root(self, file_writer: FileWriter) -> None:
        result = await file_writer.create_file("../../../tmp/malicious.txt", "content")
        assert result.success is False
        assert result.error_message is not None
        assert "security" in result.error_message.lower()

    async def test_create_file_with_unicode_content(
        self, file_writer: FileWriter, temp_project_root: Path
    ) -> None:
        unicode_content = "Hello 世界\nПривет мир\n🎉 Emoji"
        result = await file_writer.create_file("unicode.txt", unicode_content)
        assert result.success is True
        created_file = temp_project_root / "unicode.txt"
        assert created_file.read_text(encoding="utf-8") == unicode_content

    async def test_create_empty_file(
        self, file_writer: FileWriter, temp_project_root: Path
    ) -> None:
        result = await file_writer.create_file("empty.txt", "")
        assert result.success is True
        created_file = temp_project_root / "empty.txt"
        assert created_file.exists()
        assert created_file.read_text(encoding="utf-8") == ""

    async def test_create_file_with_special_characters_in_name(
        self, file_writer: FileWriter, temp_project_root: Path
    ) -> None:
        result = await file_writer.create_file("file-with_special.chars.txt", "content")
        assert result.success is True
        created_file = temp_project_root / "file-with_special.chars.txt"
        assert created_file.exists()

    async def test_create_file_multiline_content(
        self, file_writer: FileWriter, temp_project_root: Path
    ) -> None:
        multiline = "Line 1\nLine 2\nLine 3\n"
        result = await file_writer.create_file("multiline.txt", multiline)
        assert result.success is True
        created_file = temp_project_root / "multiline.txt"
        assert created_file.read_text(encoding="utf-8") == multiline


class TestCreateFileWriterTool:
    def test_creates_tool_instance(self, file_writer: FileWriter) -> None:
        tool = create_file_writer_tool(file_writer)
        assert isinstance(tool, Tool)

    def test_tool_has_description(self, file_writer: FileWriter) -> None:
        tool = create_file_writer_tool(file_writer)
        assert tool.description is not None
        assert "create" in tool.description.lower()

    def test_tool_requires_approval(self, file_writer: FileWriter) -> None:
        tool = create_file_writer_tool(file_writer)
        assert tool.requires_approval is True

    async def test_tool_function_creates_file(
        self, file_writer: FileWriter, temp_project_root: Path
    ) -> None:
        tool = create_file_writer_tool(file_writer)
        result = await tool.function(file_path="tool_test.txt", content="Tool content")
        assert result.success is True
        created_file = temp_project_root / "tool_test.txt"
        assert created_file.exists()
