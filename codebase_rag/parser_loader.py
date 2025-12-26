import importlib
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

from loguru import logger
from tree_sitter import Language, Parser, Query

from . import constants as cs
from . import exceptions as ex
from . import logs as ls
from .language_spec import LANGUAGE_SPECS, LanguageSpec
from .types_defs import LanguageImport, LanguageLoader, LanguageQueries


def _try_load_from_submodule(lang_name: cs.SupportedLanguage) -> LanguageLoader:
    submodule_path = Path(cs.GRAMMARS_DIR) / f"{cs.TREE_SITTER_PREFIX}{lang_name}"
    python_bindings_path = (
        submodule_path / cs.BINDINGS_DIR / cs.SupportedLanguage.PYTHON
    )

    if not python_bindings_path.exists():
        return None

    python_bindings_str = str(python_bindings_path)
    try:
        if python_bindings_str not in sys.path:
            sys.path.insert(0, python_bindings_str)

        try:
            module_name = f"{cs.TREE_SITTER_MODULE_PREFIX}{lang_name.replace('-', '_')}"

            setup_py_path = submodule_path / cs.SETUP_PY
            if setup_py_path.exists():
                logger.debug(ls.BUILDING_BINDINGS.format(lang=lang_name))
                result = subprocess.run(
                    [sys.executable, cs.SETUP_PY, cs.BUILD_EXT_CMD, cs.INPLACE_FLAG],
                    check=False,
                    cwd=str(submodule_path),
                    capture_output=True,
                    text=True,
                )

                if result.returncode != 0:
                    logger.debug(
                        ls.BUILD_FAILED.format(
                            lang=lang_name, stdout=result.stdout, stderr=result.stderr
                        )
                    )
                    return None
                logger.debug(ls.BUILD_SUCCESS.format(lang=lang_name))

            logger.debug(ls.IMPORTING_MODULE.format(module=module_name))
            module = importlib.import_module(module_name)

            language_attrs: list[str] = [
                cs.QUERY_LANGUAGE,
                f"{cs.LANG_ATTR_PREFIX}{lang_name}",
                f"{cs.LANG_ATTR_PREFIX}{lang_name.replace('-', '_')}",
            ]

            for attr_name in language_attrs:
                if hasattr(module, attr_name):
                    logger.debug(
                        ls.LOADED_FROM_SUBMODULE.format(lang=lang_name, attr=attr_name)
                    )
                    loader: LanguageLoader = getattr(module, attr_name)
                    return loader

            logger.debug(
                ls.NO_LANG_ATTR.format(module=module_name, available=dir(module))
            )

        finally:
            if python_bindings_str in sys.path:
                sys.path.remove(python_bindings_str)

    except Exception as e:
        logger.debug(ls.SUBMODULE_LOAD_FAILED.format(lang=lang_name, error=e))

    return None


def _try_import_language(
    module_path: str, attr_name: str, lang_name: cs.SupportedLanguage
) -> LanguageLoader:
    try:
        module = importlib.import_module(module_path)
        loader: LanguageLoader = getattr(module, attr_name)
        return loader
    except ImportError:
        return _try_load_from_submodule(lang_name)


def _import_language_loaders() -> dict[cs.SupportedLanguage, LanguageLoader]:
    language_imports: list[LanguageImport] = [
        LanguageImport(
            cs.SupportedLanguage.PYTHON,
            cs.TreeSitterModule.PYTHON,
            cs.QUERY_LANGUAGE,
            cs.SupportedLanguage.PYTHON,
        ),
        LanguageImport(
            cs.SupportedLanguage.JS,
            cs.TreeSitterModule.JS,
            cs.QUERY_LANGUAGE,
            cs.SupportedLanguage.JS,
        ),
        LanguageImport(
            cs.SupportedLanguage.TS,
            cs.TreeSitterModule.TS,
            cs.LANG_ATTR_TYPESCRIPT,
            cs.SupportedLanguage.TS,
        ),
        LanguageImport(
            cs.SupportedLanguage.RUST,
            cs.TreeSitterModule.RUST,
            cs.QUERY_LANGUAGE,
            cs.SupportedLanguage.RUST,
        ),
        LanguageImport(
            cs.SupportedLanguage.GO,
            cs.TreeSitterModule.GO,
            cs.QUERY_LANGUAGE,
            cs.SupportedLanguage.GO,
        ),
        LanguageImport(
            cs.SupportedLanguage.SCALA,
            cs.TreeSitterModule.SCALA,
            cs.QUERY_LANGUAGE,
            cs.SupportedLanguage.SCALA,
        ),
        LanguageImport(
            cs.SupportedLanguage.JAVA,
            cs.TreeSitterModule.JAVA,
            cs.QUERY_LANGUAGE,
            cs.SupportedLanguage.JAVA,
        ),
        LanguageImport(
            cs.SupportedLanguage.CPP,
            cs.TreeSitterModule.CPP,
            cs.QUERY_LANGUAGE,
            cs.SupportedLanguage.CPP,
        ),
        LanguageImport(
            cs.SupportedLanguage.LUA,
            cs.TreeSitterModule.LUA,
            cs.QUERY_LANGUAGE,
            cs.SupportedLanguage.LUA,
        ),
    ]

    loaders: dict[cs.SupportedLanguage, LanguageLoader] = {
        lang_import.lang_key: _try_import_language(
            lang_import.module_path,
            lang_import.attr_name,
            lang_import.submodule_name,
        )
        for lang_import in language_imports
    }
    for lang_key in LANGUAGE_SPECS:
        lang_name = cs.SupportedLanguage(lang_key)
        if lang_name not in loaders or loaders[lang_name] is None:
            loaders[lang_name] = _try_load_from_submodule(lang_name)

    return loaders


_language_loaders = _import_language_loaders()

LANGUAGE_LIBRARIES: dict[cs.SupportedLanguage, LanguageLoader] = _language_loaders


def _build_query_pattern(node_types: tuple[str, ...], capture_name: str) -> str:
    return " ".join([f"({node_type}) @{capture_name}" for node_type in node_types])


def _get_locals_pattern(lang_name: cs.SupportedLanguage) -> str | None:
    match lang_name:
        case cs.SupportedLanguage.JS:
            return cs.JS_LOCALS_PATTERN
        case cs.SupportedLanguage.TS:
            return cs.TS_LOCALS_PATTERN
        case _:
            return None


def _build_combined_import_pattern(lang_config: LanguageSpec) -> str:
    import_patterns = _build_query_pattern(
        lang_config.import_node_types, cs.CAPTURE_IMPORT
    )
    import_from_patterns = _build_query_pattern(
        lang_config.import_from_node_types, cs.CAPTURE_IMPORT_FROM
    )

    all_patterns: list[str] = []
    if import_patterns.strip():
        all_patterns.append(import_patterns)
    if import_from_patterns.strip() and import_from_patterns != import_patterns:
        all_patterns.append(import_from_patterns)
    return " ".join(all_patterns)


def _create_optional_query(language: Language, pattern: str | None) -> Query | None:
    return Query(language, pattern) if pattern else None


def _create_locals_query(
    language: Language, lang_name: cs.SupportedLanguage
) -> Query | None:
    locals_pattern = _get_locals_pattern(lang_name)
    if not locals_pattern:
        return None
    try:
        return Query(language, locals_pattern)
    except Exception as e:
        logger.debug(ls.LOCALS_QUERY_FAILED.format(lang=lang_name, error=e))
        return None


def _create_language_queries(
    language: Language,
    parser: Parser,
    lang_config: LanguageSpec,
    lang_name: cs.SupportedLanguage,
) -> LanguageQueries:
    function_patterns = lang_config.function_query or _build_query_pattern(
        lang_config.function_node_types, cs.CAPTURE_FUNCTION
    )
    class_patterns = lang_config.class_query or _build_query_pattern(
        lang_config.class_node_types, cs.CAPTURE_CLASS
    )
    call_patterns = lang_config.call_query or _build_query_pattern(
        lang_config.call_node_types, cs.CAPTURE_CALL
    )
    combined_import_patterns = _build_combined_import_pattern(lang_config)

    return LanguageQueries(
        functions=_create_optional_query(language, function_patterns),
        classes=_create_optional_query(language, class_patterns),
        calls=_create_optional_query(language, call_patterns),
        imports=_create_optional_query(language, combined_import_patterns),
        locals=_create_locals_query(language, lang_name),
        config=lang_config,
        language=language,
        parser=parser,
    )


def _process_language(
    lang_name: cs.SupportedLanguage,
    lang_config: LanguageSpec,
    parsers: dict[cs.SupportedLanguage, Parser],
    queries: dict[cs.SupportedLanguage, LanguageQueries],
) -> bool:
    lang_lib = LANGUAGE_LIBRARIES.get(lang_name)
    if not lang_lib:
        logger.debug(ls.LIB_NOT_AVAILABLE.format(lang=lang_name))
        return False

    try:
        language = Language(lang_lib())
        parser = Parser(language)
        parsers[lang_name] = parser
        queries[lang_name] = _create_language_queries(
            language, parser, lang_config, lang_name
        )
        logger.success(ls.GRAMMAR_LOADED.format(lang=lang_name))
        return True
    except Exception as e:
        logger.warning(ls.GRAMMAR_LOAD_FAILED.format(lang=lang_name, error=e))
        return False


def load_parsers() -> tuple[
    dict[cs.SupportedLanguage, Parser], dict[cs.SupportedLanguage, LanguageQueries]
]:
    parsers: dict[cs.SupportedLanguage, Parser] = {}
    queries: dict[cs.SupportedLanguage, LanguageQueries] = {}
    available_languages: list[cs.SupportedLanguage] = []

    for lang_key, lang_config in deepcopy(LANGUAGE_SPECS).items():
        lang_name = cs.SupportedLanguage(lang_key)
        if _process_language(lang_name, lang_config, parsers, queries):
            available_languages.append(lang_name)

    if not available_languages:
        raise RuntimeError(ex.NO_LANGUAGES)

    logger.info(ls.INITIALIZED_PARSERS.format(languages=", ".join(available_languages)))
    return parsers, queries
