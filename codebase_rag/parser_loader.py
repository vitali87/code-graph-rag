import importlib
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

from loguru import logger
from tree_sitter import Language, Parser, Query

from .constants import (
    BINDINGS_DIR,
    BUILD_EXT_CMD,
    CAPTURE_CALL,
    CAPTURE_CLASS,
    CAPTURE_FUNCTION,
    CAPTURE_IMPORT,
    CAPTURE_IMPORT_FROM,
    ERR_NO_LANGUAGES,
    GRAMMARS_DIR,
    INPLACE_FLAG,
    JS_LOCALS_PATTERN,
    LANG_ATTR_PREFIX,
    LANG_ATTR_TYPESCRIPT,
    LOG_BUILD_FAILED,
    LOG_BUILD_SUCCESS,
    LOG_BUILDING_BINDINGS,
    LOG_GRAMMAR_LOAD_FAILED,
    LOG_GRAMMAR_LOADED,
    LOG_IMPORTING_MODULE,
    LOG_INITIALIZED_PARSERS,
    LOG_LIB_NOT_AVAILABLE,
    LOG_LOADED_FROM_SUBMODULE,
    LOG_LOCALS_QUERY_FAILED,
    LOG_NO_LANG_ATTR,
    LOG_SUBMODULE_LOAD_FAILED,
    QUERY_LANGUAGE,
    SETUP_PY,
    TREE_SITTER_MODULE_PREFIX,
    TREE_SITTER_PREFIX,
    TS_LOCALS_PATTERN,
    SupportedLanguage,
)
from .language_config import LANGUAGE_CONFIGS
from .types_defs import LanguageImport, LanguageLoader, LanguageQueries


def _try_load_from_submodule(lang_name: SupportedLanguage) -> LanguageLoader:
    submodule_path = Path(GRAMMARS_DIR) / f"{TREE_SITTER_PREFIX}{lang_name}"
    python_bindings_path = submodule_path / BINDINGS_DIR / SupportedLanguage.PYTHON

    if not python_bindings_path.exists():
        return None

    python_bindings_str = str(python_bindings_path)
    try:
        if python_bindings_str not in sys.path:
            sys.path.insert(0, python_bindings_str)

        try:
            module_name = f"{TREE_SITTER_MODULE_PREFIX}{lang_name.replace('-', '_')}"

            setup_py_path = submodule_path / SETUP_PY
            if setup_py_path.exists():
                logger.debug(LOG_BUILDING_BINDINGS.format(lang=lang_name))
                result = subprocess.run(
                    [sys.executable, SETUP_PY, BUILD_EXT_CMD, INPLACE_FLAG],
                    cwd=str(submodule_path),
                    capture_output=True,
                    text=True,
                )

                if result.returncode != 0:
                    logger.debug(
                        LOG_BUILD_FAILED.format(
                            lang=lang_name, stdout=result.stdout, stderr=result.stderr
                        )
                    )
                    return None
                logger.debug(LOG_BUILD_SUCCESS.format(lang=lang_name))

            logger.debug(LOG_IMPORTING_MODULE.format(module=module_name))
            module = importlib.import_module(module_name)

            language_attrs: list[str] = [
                QUERY_LANGUAGE,
                f"{LANG_ATTR_PREFIX}{lang_name}",
                f"{LANG_ATTR_PREFIX}{lang_name.replace('-', '_')}",
            ]

            for attr_name in language_attrs:
                if hasattr(module, attr_name):
                    logger.debug(
                        LOG_LOADED_FROM_SUBMODULE.format(lang=lang_name, attr=attr_name)
                    )
                    loader: LanguageLoader = getattr(module, attr_name)
                    return loader

            logger.debug(
                LOG_NO_LANG_ATTR.format(module=module_name, available=dir(module))
            )

        finally:
            if python_bindings_str in sys.path:
                sys.path.remove(python_bindings_str)

    except Exception as e:
        logger.debug(LOG_SUBMODULE_LOAD_FAILED.format(lang=lang_name, error=e))

    return None


def _try_import_language(
    module_path: str, attr_name: str, lang_name: SupportedLanguage
) -> LanguageLoader:
    try:
        module = importlib.import_module(module_path)
        loader: LanguageLoader = getattr(module, attr_name)
        return loader
    except ImportError:
        return _try_load_from_submodule(lang_name)


def _import_language_loaders() -> dict[SupportedLanguage, LanguageLoader]:
    language_imports: list[LanguageImport] = [
        LanguageImport(
            SupportedLanguage.PYTHON,
            "tree_sitter_python",
            QUERY_LANGUAGE,
            SupportedLanguage.PYTHON,
        ),
        LanguageImport(
            SupportedLanguage.JS,
            "tree_sitter_javascript",
            QUERY_LANGUAGE,
            SupportedLanguage.JS,
        ),
        LanguageImport(
            SupportedLanguage.TS,
            "tree_sitter_typescript",
            LANG_ATTR_TYPESCRIPT,
            SupportedLanguage.TS,
        ),
        LanguageImport(
            SupportedLanguage.RUST,
            "tree_sitter_rust",
            QUERY_LANGUAGE,
            SupportedLanguage.RUST,
        ),
        LanguageImport(
            SupportedLanguage.GO, "tree_sitter_go", QUERY_LANGUAGE, SupportedLanguage.GO
        ),
        LanguageImport(
            SupportedLanguage.SCALA,
            "tree_sitter_scala",
            QUERY_LANGUAGE,
            SupportedLanguage.SCALA,
        ),
        LanguageImport(
            SupportedLanguage.JAVA,
            "tree_sitter_java",
            QUERY_LANGUAGE,
            SupportedLanguage.JAVA,
        ),
        LanguageImport(
            SupportedLanguage.CPP,
            "tree_sitter_cpp",
            QUERY_LANGUAGE,
            SupportedLanguage.CPP,
        ),
        LanguageImport(
            SupportedLanguage.LUA,
            "tree_sitter_lua",
            QUERY_LANGUAGE,
            SupportedLanguage.LUA,
        ),
    ]

    loaders: dict[SupportedLanguage, LanguageLoader] = {
        lang_import.lang_key: _try_import_language(
            lang_import.module_path,
            lang_import.attr_name,
            lang_import.submodule_name,
        )
        for lang_import in language_imports
    }
    for lang_key in LANGUAGE_CONFIGS:
        lang_name = SupportedLanguage(lang_key)
        if lang_name not in loaders or loaders[lang_name] is None:
            loaders[lang_name] = _try_load_from_submodule(lang_name)

    return loaders


_language_loaders = _import_language_loaders()

LANGUAGE_LIBRARIES: dict[SupportedLanguage, LanguageLoader] = _language_loaders


def _build_query_pattern(node_types: tuple[str, ...], capture_name: str) -> str:
    return " ".join([f"({node_type}) @{capture_name}" for node_type in node_types])


def _get_locals_pattern(lang_name: SupportedLanguage) -> str | None:
    match lang_name:
        case SupportedLanguage.JS:
            return JS_LOCALS_PATTERN
        case SupportedLanguage.TS:
            return TS_LOCALS_PATTERN
        case _:
            return None


def load_parsers() -> tuple[
    dict[SupportedLanguage, Parser], dict[SupportedLanguage, LanguageQueries]
]:
    parsers: dict[SupportedLanguage, Parser] = {}
    queries: dict[SupportedLanguage, LanguageQueries] = {}
    available_languages: list[SupportedLanguage] = []

    configs = deepcopy(LANGUAGE_CONFIGS)

    for lang_key, lang_config in configs.items():
        lang_name = SupportedLanguage(lang_key)
        lang_lib = LANGUAGE_LIBRARIES.get(lang_name)
        if not lang_lib:
            logger.debug(LOG_LIB_NOT_AVAILABLE.format(lang=lang_name))
            continue

        try:
            language = Language(lang_lib())
            parser = Parser(language)
            parsers[lang_name] = parser

            function_patterns = lang_config.function_query or _build_query_pattern(
                lang_config.function_node_types, CAPTURE_FUNCTION
            )
            class_patterns = lang_config.class_query or _build_query_pattern(
                lang_config.class_node_types, CAPTURE_CLASS
            )
            call_patterns = lang_config.call_query or _build_query_pattern(
                lang_config.call_node_types, CAPTURE_CALL
            )

            import_patterns = _build_query_pattern(
                lang_config.import_node_types, CAPTURE_IMPORT
            )
            import_from_patterns = _build_query_pattern(
                lang_config.import_from_node_types, CAPTURE_IMPORT_FROM
            )

            all_import_patterns: list[str] = []
            if import_patterns.strip():
                all_import_patterns.append(import_patterns)
            if import_from_patterns.strip() and import_from_patterns != import_patterns:
                all_import_patterns.append(import_from_patterns)
            combined_import_patterns = " ".join(all_import_patterns)

            locals_query: Query | None = None
            if locals_pattern := _get_locals_pattern(lang_name):
                try:
                    locals_query = Query(language, locals_pattern)
                except Exception as e:
                    logger.debug(
                        LOG_LOCALS_QUERY_FAILED.format(lang=lang_name, error=e)
                    )

            queries[lang_name] = LanguageQueries(
                functions=Query(language, function_patterns)
                if function_patterns
                else None,
                classes=Query(language, class_patterns) if class_patterns else None,
                calls=Query(language, call_patterns) if call_patterns else None,
                imports=Query(language, combined_import_patterns)
                if combined_import_patterns
                else None,
                locals=locals_query,
                config=lang_config,
                language=language,
                parser=parser,
            )

            available_languages.append(lang_name)
            logger.success(LOG_GRAMMAR_LOADED.format(lang=lang_name))
        except Exception as e:
            logger.warning(LOG_GRAMMAR_LOAD_FAILED.format(lang=lang_name, error=e))

    if not available_languages:
        raise RuntimeError(ERR_NO_LANGUAGES)

    logger.info(
        LOG_INITIALIZED_PARSERS.format(languages=", ".join(available_languages))
    )
    return parsers, queries
