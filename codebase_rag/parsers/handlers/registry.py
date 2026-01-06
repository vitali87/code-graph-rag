from __future__ import annotations

from functools import lru_cache

from ...constants import SupportedLanguage
from .base import BaseLanguageHandler
from .cpp import CppHandler
from .java import JavaHandler
from .js_ts import JsTsHandler
from .lua import LuaHandler
from .protocol import LanguageHandler
from .python import PythonHandler
from .rust import RustHandler

_HANDLERS: dict[SupportedLanguage, type[BaseLanguageHandler]] = {
    SupportedLanguage.PYTHON: PythonHandler,
    SupportedLanguage.JS: JsTsHandler,
    SupportedLanguage.TS: JsTsHandler,
    SupportedLanguage.CPP: CppHandler,
    SupportedLanguage.RUST: RustHandler,
    SupportedLanguage.JAVA: JavaHandler,
    SupportedLanguage.LUA: LuaHandler,
}

_DEFAULT_HANDLER = BaseLanguageHandler


@lru_cache(maxsize=16)
def get_handler(language: SupportedLanguage) -> LanguageHandler:
    handler_class = _HANDLERS.get(language, _DEFAULT_HANDLER)
    return handler_class()
