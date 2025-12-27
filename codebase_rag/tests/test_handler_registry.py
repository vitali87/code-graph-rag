from __future__ import annotations

import pytest

from codebase_rag.constants import SupportedLanguage
from codebase_rag.parsers.handlers import get_handler
from codebase_rag.parsers.handlers.base import BaseLanguageHandler
from codebase_rag.parsers.handlers.cpp import CppHandler
from codebase_rag.parsers.handlers.java import JavaHandler
from codebase_rag.parsers.handlers.js_ts import JsTsHandler
from codebase_rag.parsers.handlers.lua import LuaHandler
from codebase_rag.parsers.handlers.rust import RustHandler


class TestGetHandler:
    def test_returns_jsts_handler_for_javascript(self) -> None:
        handler = get_handler(SupportedLanguage.JS)
        assert isinstance(handler, JsTsHandler)

    def test_returns_jsts_handler_for_typescript(self) -> None:
        handler = get_handler(SupportedLanguage.TS)
        assert isinstance(handler, JsTsHandler)

    def test_returns_cpp_handler_for_cpp(self) -> None:
        handler = get_handler(SupportedLanguage.CPP)
        assert isinstance(handler, CppHandler)

    def test_returns_rust_handler_for_rust(self) -> None:
        handler = get_handler(SupportedLanguage.RUST)
        assert isinstance(handler, RustHandler)

    def test_returns_java_handler_for_java(self) -> None:
        handler = get_handler(SupportedLanguage.JAVA)
        assert isinstance(handler, JavaHandler)

    def test_returns_lua_handler_for_lua(self) -> None:
        handler = get_handler(SupportedLanguage.LUA)
        assert isinstance(handler, LuaHandler)

    def test_returns_base_handler_for_python(self) -> None:
        handler = get_handler(SupportedLanguage.PYTHON)
        assert isinstance(handler, BaseLanguageHandler)
        assert type(handler) is BaseLanguageHandler

    def test_returns_base_handler_for_go(self) -> None:
        handler = get_handler(SupportedLanguage.GO)
        assert isinstance(handler, BaseLanguageHandler)
        assert type(handler) is BaseLanguageHandler

    def test_returns_base_handler_for_php(self) -> None:
        handler = get_handler(SupportedLanguage.PHP)
        assert isinstance(handler, BaseLanguageHandler)
        assert type(handler) is BaseLanguageHandler


class TestHandlerCaching:
    def test_same_instance_returned_for_same_language(self) -> None:
        handler1 = get_handler(SupportedLanguage.JS)
        handler2 = get_handler(SupportedLanguage.JS)
        assert handler1 is handler2

    def test_different_instances_for_different_languages(self) -> None:
        js_handler = get_handler(SupportedLanguage.JS)
        cpp_handler = get_handler(SupportedLanguage.CPP)
        assert js_handler is not cpp_handler

    def test_js_and_ts_share_same_handler_type(self) -> None:
        js_handler = get_handler(SupportedLanguage.JS)
        ts_handler = get_handler(SupportedLanguage.TS)
        assert type(js_handler) is type(ts_handler)
        assert js_handler is not ts_handler


class TestHandlerProtocol:
    @pytest.mark.parametrize(
        "language",
        [
            SupportedLanguage.JS,
            SupportedLanguage.TS,
            SupportedLanguage.CPP,
            SupportedLanguage.RUST,
            SupportedLanguage.JAVA,
            SupportedLanguage.LUA,
            SupportedLanguage.PYTHON,
            SupportedLanguage.GO,
            SupportedLanguage.PHP,
        ],
    )
    def test_handler_has_all_protocol_methods(
        self, language: SupportedLanguage
    ) -> None:
        handler = get_handler(language)

        assert hasattr(handler, "is_inside_method_with_object_literals")
        assert hasattr(handler, "is_class_method")
        assert hasattr(handler, "is_export_inside_function")
        assert hasattr(handler, "extract_function_name")
        assert hasattr(handler, "build_function_qualified_name")
        assert hasattr(handler, "is_function_exported")
        assert hasattr(handler, "should_process_as_impl_block")
        assert hasattr(handler, "extract_impl_target")
        assert hasattr(handler, "build_method_qualified_name")
        assert hasattr(handler, "extract_base_class_name")
        assert hasattr(handler, "build_nested_function_qn")

    @pytest.mark.parametrize(
        "language",
        [
            SupportedLanguage.JS,
            SupportedLanguage.TS,
            SupportedLanguage.CPP,
            SupportedLanguage.RUST,
            SupportedLanguage.JAVA,
            SupportedLanguage.LUA,
            SupportedLanguage.PYTHON,
        ],
    )
    def test_handler_methods_are_callable(self, language: SupportedLanguage) -> None:
        handler = get_handler(language)

        assert callable(handler.is_inside_method_with_object_literals)
        assert callable(handler.is_class_method)
        assert callable(handler.is_export_inside_function)
        assert callable(handler.extract_function_name)
        assert callable(handler.build_function_qualified_name)
        assert callable(handler.is_function_exported)
        assert callable(handler.should_process_as_impl_block)
        assert callable(handler.extract_impl_target)
        assert callable(handler.build_method_qualified_name)
        assert callable(handler.extract_base_class_name)
        assert callable(handler.build_nested_function_qn)


class TestHandlerInheritance:
    def test_jsts_handler_extends_base(self) -> None:
        assert issubclass(JsTsHandler, BaseLanguageHandler)

    def test_cpp_handler_extends_base(self) -> None:
        assert issubclass(CppHandler, BaseLanguageHandler)

    def test_rust_handler_extends_base(self) -> None:
        assert issubclass(RustHandler, BaseLanguageHandler)

    def test_java_handler_extends_base(self) -> None:
        assert issubclass(JavaHandler, BaseLanguageHandler)

    def test_lua_handler_extends_base(self) -> None:
        assert issubclass(LuaHandler, BaseLanguageHandler)
