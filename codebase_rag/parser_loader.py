from collections.abc import Callable
from typing import Any

from loguru import logger
from tree_sitter import Language, Parser

from .language_config import LANGUAGE_CONFIGS

# Define a type for the language library loaders
LanguageLoader = Callable[[], object] | None


# Import available Tree-sitter languages and correctly type them as Optional
def _import_language_loaders() -> dict[str, LanguageLoader]:
    """Import language loaders with proper error handling and typing."""
    loaders: dict[str, LanguageLoader] = {}

    try:
        from tree_sitter_python import language as python_language_so

        loaders["python"] = python_language_so
    except ImportError:
        loaders["python"] = None

    try:
        from tree_sitter_javascript import language as javascript_language_so

        loaders["javascript"] = javascript_language_so
    except ImportError:
        loaders["javascript"] = None

    try:
        from tree_sitter_typescript import language_typescript as typescript_language_so

        loaders["typescript"] = typescript_language_so
    except ImportError:
        loaders["typescript"] = None

    try:
        from tree_sitter_rust import language as rust_language_so

        loaders["rust"] = rust_language_so
    except ImportError:
        loaders["rust"] = None

    try:
        from tree_sitter_go import language as go_language_so

        loaders["go"] = go_language_so
    except ImportError:
        loaders["go"] = None

    try:
        from tree_sitter_scala import language as scala_language_so

        loaders["scala"] = scala_language_so
    except ImportError:
        loaders["scala"] = None

    try:
        from tree_sitter_java import language as java_language_so

        loaders["java"] = java_language_so
    except ImportError:
        loaders["java"] = None

    try:
        from tree_sitter_cpp import language as cpp_language_so

        loaders["cpp"] = cpp_language_so
    except ImportError:
        loaders["cpp"] = None

    return loaders


_language_loaders = _import_language_loaders()


LANGUAGE_LIBRARIES: dict[str, LanguageLoader | None] = _language_loaders


def load_parsers() -> tuple[dict[str, Parser], dict[str, Any]]:
    """Loads all available Tree-sitter parsers and compiles their queries."""
    parsers: dict[str, Parser] = {}
    queries: dict[str, Any] = {}
    available_languages = []

    for lang_name, lang_config in LANGUAGE_CONFIGS.items():
        if lang_lib := LANGUAGE_LIBRARIES.get(lang_name):
            try:
                language = Language(lang_lib())
                parser = Parser(language)

                parsers[lang_name] = parser

                # Compile queries
                function_patterns = " ".join(
                    [
                        f"({node_type}) @function"
                        for node_type in lang_config.function_node_types
                    ]
                )
                class_patterns = " ".join(
                    [
                        f"({node_type}) @class"
                        for node_type in lang_config.class_node_types
                    ]
                )
                call_patterns = " ".join(
                    [
                        f"({node_type}) @call"
                        for node_type in lang_config.call_node_types
                    ]
                )

                queries[lang_name] = {
                    "functions": language.query(function_patterns),
                    "classes": language.query(class_patterns),
                    "calls": language.query(call_patterns) if call_patterns else None,
                    "config": lang_config,
                }

                available_languages.append(lang_name)
                logger.success(f"Successfully loaded {lang_name} grammar.")
            except Exception as e:
                logger.warning(f"Failed to load {lang_name} grammar: {e}")
        else:
            logger.debug(f"Tree-sitter library for {lang_name} not available.")

    if not available_languages:
        raise RuntimeError("No Tree-sitter languages available.")

    logger.info(f"Initialized parsers for: {', '.join(available_languages)}")
    return parsers, queries
