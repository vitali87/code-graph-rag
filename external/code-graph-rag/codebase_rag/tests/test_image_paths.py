from pathlib import Path

import pytest

from codebase_rag.main import (
    _find_image_paths,
    _get_path_variants,
    _handle_chat_images,
    _replace_path_in_question,
)


class TestFindImagePaths:
    def test_finds_png_path(self) -> None:
        question = "What is in this image /home/user/screenshot.png please analyze"
        result = _find_image_paths(question)
        assert result == [Path("/home/user/screenshot.png")]

    def test_finds_jpg_path(self) -> None:
        question = "Look at /tmp/photo.jpg"
        result = _find_image_paths(question)
        assert result == [Path("/tmp/photo.jpg")]

    def test_finds_jpeg_path(self) -> None:
        question = "Check /var/images/pic.jpeg"
        result = _find_image_paths(question)
        assert result == [Path("/var/images/pic.jpeg")]

    def test_finds_gif_path(self) -> None:
        question = "Analyze /home/user/animation.gif"
        result = _find_image_paths(question)
        assert result == [Path("/home/user/animation.gif")]

    def test_finds_multiple_images(self) -> None:
        question = "Compare /img/a.png and /img/b.jpg"
        result = _find_image_paths(question)
        assert result == [Path("/img/a.png"), Path("/img/b.jpg")]

    def test_case_insensitive_extension(self) -> None:
        question = "Look at /path/IMAGE.PNG and /path/photo.JPG"
        result = _find_image_paths(question)
        assert len(result) == 2
        assert Path("/path/IMAGE.PNG") in result
        assert Path("/path/photo.JPG") in result

    def test_ignores_relative_paths(self) -> None:
        question = "Check images/photo.png and ./local/pic.jpg"
        result = _find_image_paths(question)
        assert result == []

    def test_ignores_non_image_extensions(self) -> None:
        question = "Look at /path/document.pdf and /path/code.py"
        result = _find_image_paths(question)
        assert result == []

    def test_empty_question(self) -> None:
        result = _find_image_paths("")
        assert result == []

    def test_no_paths(self) -> None:
        question = "What is the meaning of life?"
        result = _find_image_paths(question)
        assert result == []

    def test_handles_quoted_paths(self) -> None:
        question = 'Look at "/path/with spaces/image.png"'
        result = _find_image_paths(question)
        assert result == [Path("/path/with spaces/image.png")]


class TestGetPathVariants:
    def test_returns_four_variants(self) -> None:
        result = _get_path_variants("/path/to/file.png")
        assert len(result) == 4

    def test_includes_escaped_spaces(self) -> None:
        result = _get_path_variants("/path/with spaces/file.png")
        assert r"/path/with\ spaces/file.png" in result

    def test_includes_single_quoted(self) -> None:
        result = _get_path_variants("/path/to/file.png")
        assert "'/path/to/file.png'" in result

    def test_includes_double_quoted(self) -> None:
        result = _get_path_variants("/path/to/file.png")
        assert '"/path/to/file.png"' in result

    def test_includes_original(self) -> None:
        path = "/path/to/file.png"
        result = _get_path_variants(path)
        assert path in result


class TestReplacePathInQuestion:
    def test_replaces_simple_path(self) -> None:
        question = "Look at /old/path.png please"
        result = _replace_path_in_question(question, "/old/path.png", "/new/path.png")
        assert result == "Look at /new/path.png please"

    def test_replaces_quoted_path(self) -> None:
        question = "Look at '/old/path.png' please"
        result = _replace_path_in_question(question, "/old/path.png", "/new/path.png")
        assert result == "Look at '/new/path.png' please"

    def test_replaces_double_quoted_path(self) -> None:
        question = 'Look at "/old/path.png" please'
        result = _replace_path_in_question(question, "/old/path.png", "/new/path.png")
        assert result == 'Look at "/new/path.png" please'

    def test_returns_original_if_not_found(self) -> None:
        question = "No path here"
        result = _replace_path_in_question(question, "/missing.png", "/new.png")
        assert result == question


class TestHandleChatImages:
    @pytest.fixture
    def temp_project(self, tmp_path: Path) -> Path:
        return tmp_path

    @pytest.fixture
    def temp_image(self, tmp_path: Path) -> Path:
        img_path = tmp_path / "test_image.png"
        img_path.write_bytes(b"fake png content")
        return img_path

    def test_no_images_returns_unchanged(self, temp_project: Path) -> None:
        question = "What is 2 + 2?"
        result = _handle_chat_images(question, temp_project)
        assert result == question

    def test_copies_image_to_tmp(self, temp_project: Path, temp_image: Path) -> None:
        question = f"Look at {temp_image}"
        result = _handle_chat_images(question, temp_project)

        assert ".tmp" in result
        assert "test_image.png" in result

        tmp_dir = temp_project / ".tmp"
        assert tmp_dir.exists()
        copied_files = list(tmp_dir.glob("*test_image.png"))
        assert len(copied_files) == 1

    def test_handles_nonexistent_image(self, temp_project: Path) -> None:
        question = "Look at /nonexistent/image.png"
        result = _handle_chat_images(question, temp_project)
        assert result == question

    def test_handles_multiple_images(self, temp_project: Path) -> None:
        img1 = temp_project / "img1.png"
        img2 = temp_project / "img2.jpg"
        img1.write_bytes(b"png1")
        img2.write_bytes(b"jpg2")

        question = f"Compare {img1} and {img2}"
        result = _handle_chat_images(question, temp_project)

        assert ".tmp" in result
        assert "img1.png" in result
        assert "img2.jpg" in result
