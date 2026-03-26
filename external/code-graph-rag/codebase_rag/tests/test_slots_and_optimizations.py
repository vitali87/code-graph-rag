from __future__ import annotations

import pytest

from codebase_rag.parsers.dependency_parser import (
    CargoTomlParser,
    ComposerJsonParser,
    CsprojParser,
    DependencyParser,
    GemfileParser,
    GoModParser,
    PackageJsonParser,
    PyProjectTomlParser,
    RequirementsTxtParser,
)
from codebase_rag.parsers.handlers.base import BaseLanguageHandler
from codebase_rag.parsers.handlers.cpp import CppHandler
from codebase_rag.parsers.handlers.java import JavaHandler
from codebase_rag.parsers.handlers.js_ts import JsTsHandler
from codebase_rag.parsers.handlers.lua import LuaHandler
from codebase_rag.parsers.handlers.protocol import LanguageHandler
from codebase_rag.parsers.handlers.python import PythonHandler
from codebase_rag.parsers.handlers.rust import RustHandler
from codebase_rag.parsers.stdlib_extractor import StdlibExtractor
from codebase_rag.parsers.utils import _cached_decode_bytes


class TestHandlerSlots:
    @pytest.mark.parametrize(
        "handler_cls",
        [
            BaseLanguageHandler,
            PythonHandler,
            JavaHandler,
            JsTsHandler,
            CppHandler,
            RustHandler,
            LuaHandler,
        ],
    )
    def test_handler_has_slots(self, handler_cls: type) -> None:
        assert hasattr(handler_cls, "__slots__")

    @pytest.mark.parametrize(
        "handler_cls",
        [
            BaseLanguageHandler,
            PythonHandler,
            JavaHandler,
            JsTsHandler,
            CppHandler,
            RustHandler,
            LuaHandler,
        ],
    )
    def test_handler_no_instance_dict(self, handler_cls: type) -> None:
        instance = handler_cls()
        assert not hasattr(instance, "__dict__")

    def test_protocol_has_slots(self) -> None:
        assert hasattr(LanguageHandler, "__slots__")


class TestDependencyParserSlots:
    @pytest.mark.parametrize(
        "parser_cls",
        [
            DependencyParser,
            PyProjectTomlParser,
            RequirementsTxtParser,
            PackageJsonParser,
            CargoTomlParser,
            GoModParser,
            GemfileParser,
            ComposerJsonParser,
            CsprojParser,
        ],
    )
    def test_parser_has_slots(self, parser_cls: type) -> None:
        assert hasattr(parser_cls, "__slots__")

    @pytest.mark.parametrize(
        "parser_cls",
        [
            DependencyParser,
            PyProjectTomlParser,
            RequirementsTxtParser,
            PackageJsonParser,
            CargoTomlParser,
            GoModParser,
            GemfileParser,
            ComposerJsonParser,
            CsprojParser,
        ],
    )
    def test_parser_no_instance_dict(self, parser_cls: type) -> None:
        instance = parser_cls()
        assert not hasattr(instance, "__dict__")


class TestStdlibExtractorSlots:
    def test_has_slots(self) -> None:
        assert hasattr(StdlibExtractor, "__slots__")
        assert "function_registry" in StdlibExtractor.__slots__
        assert "repo_path" in StdlibExtractor.__slots__
        assert "project_name" in StdlibExtractor.__slots__

    def test_no_instance_dict(self) -> None:
        extractor = StdlibExtractor()
        assert not hasattr(extractor, "__dict__")


class TestCachedDecodeBytes:
    def test_cache_maxsize(self) -> None:
        cache_info = _cached_decode_bytes.cache_info()
        assert cache_info.maxsize == 50000

    def test_decode_bytes(self) -> None:
        result = _cached_decode_bytes(b"hello world")
        assert result == "hello world"

    def test_decode_caches(self) -> None:
        _cached_decode_bytes.cache_clear()
        _cached_decode_bytes(b"test_cache")
        _cached_decode_bytes(b"test_cache")
        info = _cached_decode_bytes.cache_info()
        assert info.hits >= 1
