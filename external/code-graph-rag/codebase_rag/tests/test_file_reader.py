from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import Tool

from codebase_rag import constants as cs
from codebase_rag.schemas import FileReadResult
from codebase_rag.tools.file_reader import (
    FileReader,
    create_file_reader_tool,
)

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def temp_project_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def file_reader(temp_project_root: Path) -> FileReader:
    return FileReader(str(temp_project_root))


@pytest.fixture
def sample_text_file(temp_project_root: Path) -> Path:
    file_path = temp_project_root / "sample.txt"
    file_path.write_text("Hello, World!\nLine 2\nLine 3", encoding="utf-8")
    return file_path


@pytest.fixture
def sample_python_file(temp_project_root: Path) -> Path:
    file_path = temp_project_root / "sample.py"
    file_path.write_text("def hello():\n    return 'Hello'", encoding="utf-8")
    return file_path


class TestFileReaderInit:
    def test_init_resolves_project_root(self, temp_project_root: Path) -> None:
        reader = FileReader(str(temp_project_root))
        assert reader.project_root == temp_project_root.resolve()

    def test_init_with_relative_path(self) -> None:
        reader = FileReader(".")
        assert reader.project_root == Path(".").resolve()

    def test_binary_extensions_set(self) -> None:
        assert ".pdf" in cs.BINARY_EXTENSIONS
        assert ".png" in cs.BINARY_EXTENSIONS
        assert ".jpg" in cs.BINARY_EXTENSIONS
        assert ".jpeg" in cs.BINARY_EXTENSIONS


class TestFileReadResult:
    def test_success_result(self) -> None:
        result = FileReadResult(file_path="test.txt", content="Hello")
        assert result.file_path == "test.txt"
        assert result.content == "Hello"
        assert result.error_message is None

    def test_error_result(self) -> None:
        result = FileReadResult(file_path="test.txt", error_message="File not found")
        assert result.file_path == "test.txt"
        assert result.content is None
        assert result.error_message == "File not found"


class TestReadFile:
    async def test_read_existing_text_file(
        self, file_reader: FileReader, sample_text_file: Path
    ) -> None:
        result = await file_reader.read_file("sample.txt")
        assert result.content == "Hello, World!\nLine 2\nLine 3"
        assert result.error_message is None

    async def test_read_python_file(
        self, file_reader: FileReader, sample_python_file: Path
    ) -> None:
        result = await file_reader.read_file("sample.py")
        assert "def hello():" in str(result.content)
        assert result.error_message is None

    async def test_read_nonexistent_file(self, file_reader: FileReader) -> None:
        result = await file_reader.read_file("nonexistent.txt")
        assert result.content is None
        assert result.error_message is not None
        assert "not found" in result.error_message.lower()

    async def test_read_file_outside_root(self, file_reader: FileReader) -> None:
        result = await file_reader.read_file("../../../etc/passwd")
        assert result.content is None
        assert result.error_message is not None
        assert "security" in result.error_message.lower()

    async def test_read_binary_pdf_file(
        self, file_reader: FileReader, temp_project_root: Path
    ) -> None:
        pdf_file = temp_project_root / "document.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 binary content")
        result = await file_reader.read_file("document.pdf")
        assert result.content is None
        assert result.error_message is not None
        assert "binary" in result.error_message.lower()

    async def test_read_binary_png_file(
        self, file_reader: FileReader, temp_project_root: Path
    ) -> None:
        png_file = temp_project_root / "image.png"
        png_file.write_bytes(b"\x89PNG\r\n\x1a\n binary content")
        result = await file_reader.read_file("image.png")
        assert result.content is None
        assert result.error_message is not None
        assert "binary" in result.error_message.lower()

    async def test_read_file_with_unicode(
        self, file_reader: FileReader, temp_project_root: Path
    ) -> None:
        unicode_file = temp_project_root / "unicode.txt"
        unicode_file.write_text("Hello 世界\nПривет мир", encoding="utf-8")
        result = await file_reader.read_file("unicode.txt")
        assert "Hello 世界" in str(result.content)
        assert "Привет мир" in str(result.content)
        assert result.error_message is None

    async def test_read_empty_file(
        self, file_reader: FileReader, temp_project_root: Path
    ) -> None:
        empty_file = temp_project_root / "empty.txt"
        empty_file.write_text("", encoding="utf-8")
        result = await file_reader.read_file("empty.txt")
        assert result.content == ""
        assert result.error_message is None

    async def test_read_file_in_subdirectory(
        self, file_reader: FileReader, temp_project_root: Path
    ) -> None:
        subdir = temp_project_root / "subdir"
        subdir.mkdir()
        nested_file = subdir / "nested.txt"
        nested_file.write_text("Nested content", encoding="utf-8")
        result = await file_reader.read_file("subdir/nested.txt")
        assert result.content == "Nested content"
        assert result.error_message is None

    async def test_read_directory_returns_error(
        self, file_reader: FileReader, temp_project_root: Path
    ) -> None:
        subdir = temp_project_root / "subdir"
        subdir.mkdir()
        result = await file_reader.read_file("subdir")
        assert result.content is None
        assert result.error_message is not None


class TestCreateFileReaderTool:
    def test_creates_tool_instance(self, file_reader: FileReader) -> None:
        tool = create_file_reader_tool(file_reader)
        assert isinstance(tool, Tool)

    def test_tool_has_description(self, file_reader: FileReader) -> None:
        tool = create_file_reader_tool(file_reader)
        assert tool.description is not None
        assert "read" in tool.description.lower()

    async def test_tool_function_returns_content(
        self, file_reader: FileReader, sample_text_file: Path
    ) -> None:
        tool = create_file_reader_tool(file_reader)
        result = await tool.function(file_path="sample.txt")
        assert "Hello, World!" in result

    async def test_tool_function_returns_error_string(
        self, file_reader: FileReader
    ) -> None:
        tool = create_file_reader_tool(file_reader)
        result = await tool.function(file_path="nonexistent.txt")
        assert "Error:" in result
