import importlib
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
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
                logger.debug(ls.BUILDING_BINDINGS, lang=lang_name)
                result = subprocess.run(
                    [sys.executable, cs.SETUP_PY, cs.BUILD_EXT_CMD, cs.INPLACE_FLAG],
                    check=False,
                    cwd=str(submodule_path),
                    capture_output=True,
                    text=True,
                )

                if result.returncode != 0:
                    logger.debug(
                        ls.BUILD_FAILED,
                        lang=lang_name,
                        stdout=result.stdout,
                        stderr=result.stderr,
                    )
                    return None
                logger.debug(ls.BUILD_SUCCESS, lang=lang_name)

            logger.debug(ls.IMPORTING_MODULE, module=module_name)
            module = importlib.import_module(module_name)

            language_attrs: list[str] = [
                cs.QUERY_LANGUAGE,
                f"{cs.LANG_ATTR_PREFIX}{lang_name}",
                f"{cs.LANG_ATTR_PREFIX}{lang_name.replace('-', '_')}",
            ]

            for attr_name in language_attrs:
                if hasattr(module, attr_name):
                    logger.debug(
                        ls.LOADED_FROM_SUBMODULE, lang=lang_name, attr=attr_name
                    )
                    loader: LanguageLoader = getattr(module, attr_name)
                    return loader

            logger.debug(ls.NO_LANG_ATTR, module=module_name, available=dir(module))

        finally:
            if python_bindings_str in sys.path:
                sys.path.remove(python_bindings_str)

    except Exception as e:
        logger.debug(ls.SUBMODULE_LOAD_FAILED, lang=lang_name, error=e)

    return None


def _try_import_language(
    module_path: str, attr_name: str, lang_name: cs.SupportedLanguage
) -> LanguageLoader:
    # AttributeError covers a pip package too old to export the requested
    # grammar variant (tree_sitter_typescript without language_tsx); fall
    # back rather than crash parser init.
    try:
        module = importlib.import_module(module_path)
        loader: LanguageLoader = getattr(module, attr_name)
        return loader
    except (ImportError, AttributeError):
        return _try_load_from_submodule(lang_name)


def _language_imports() -> list[LanguageImport]:
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
        # Same pip package ships both grammar variants; .tsx needs the tsx
        # one or JSX parses as an ERROR forest. The submodule name is TSX on
        # purpose: no grammars/tree-sitter-tsx exists, so a too-old pip
        # package leaves TSX unavailable, not bound to the wrong grammar.
        LanguageImport(
            cs.SupportedLanguage.TSX,
            cs.TreeSitterModule.TS,
            cs.LANG_ATTR_TSX,
            cs.SupportedLanguage.TSX,
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
            cs.SupportedLanguage.C,
            cs.TreeSitterModule.C,
            cs.QUERY_LANGUAGE,
            cs.SupportedLanguage.C,
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
        LanguageImport(
            cs.SupportedLanguage.PHP,
            cs.TreeSitterModule.PHP,
            cs.LANG_ATTR_PHP,
            cs.SupportedLanguage.PHP,
        ),
        LanguageImport(
            cs.SupportedLanguage.CSHARP,
            cs.TreeSitterModule.CSHARP,
            cs.QUERY_LANGUAGE,
            cs.SupportedLanguage.CSHARP,
        ),
        LanguageImport(
            cs.SupportedLanguage.DART,
            cs.TreeSitterModule.DART,
            cs.QUERY_LANGUAGE,
            cs.SupportedLanguage.DART,
        ),
    ]

    return language_imports


_IMPORT_SPECS: dict[cs.SupportedLanguage, LanguageImport] = {
    lang_import.lang_key: lang_import for lang_import in _language_imports()
}

_loader_cache: dict[cs.SupportedLanguage, LanguageLoader] = {}


def _get_language_library(lang_name: cs.SupportedLanguage) -> LanguageLoader:
    # One grammar module import per language, on first use, cached for the
    # process (issue #68: importing all 14 grammars up front made every
    # startup pay for languages the repo does not contain).
    if lang_name in _loader_cache:
        return _loader_cache[lang_name]
    lang_import = _IMPORT_SPECS.get(lang_name)
    if lang_import is not None:
        loader = _try_import_language(
            lang_import.module_path,
            lang_import.attr_name,
            lang_import.submodule_name,
        )
    else:
        loader = _try_load_from_submodule(lang_name)
    _loader_cache[lang_name] = loader
    return loader


def _build_query_pattern(node_types: tuple[str, ...], capture_name: str) -> str:
    return " ".join([f"({node_type}) @{capture_name}" for node_type in node_types])


def _get_locals_pattern(lang_name: cs.SupportedLanguage) -> str | None:
    match lang_name:
        case cs.SupportedLanguage.JS:
            return cs.JS_LOCALS_PATTERN
        case cs.SupportedLanguage.TS | cs.SupportedLanguage.TSX:
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
        logger.debug(ls.LOCALS_QUERY_FAILED, lang=lang_name, error=e)
        return None


def _create_highlights_query(
    language: Language, lang_name: cs.SupportedLanguage
) -> Query | None:
    query_str = ""

    # TSX shares the TypeScript grammar for highlights
    query_lang_name = (
        cs.SupportedLanguage.TS if lang_name == cs.SupportedLanguage.TSX else lang_name
    )

    try:
        module_name = (
            f"{cs.TREE_SITTER_MODULE_PREFIX}{query_lang_name.replace('-', '_')}"
        )
        module = importlib.import_module(module_name)
        if hasattr(module, "HIGHLIGHTS_QUERY"):
            query_str = module.HIGHLIGHTS_QUERY
    except Exception as e:
        logger.debug(
            f"Failed to load standard highlights query for {query_lang_name}: {e}"
        )

    try:
        fallback_path = (
            Path(__file__).parent / "queries" / "highlights" / f"{query_lang_name}.scm"
        )
        if fallback_path.exists():
            custom_queries = fallback_path.read_text(encoding="utf-8")
            query_str = (
                query_str + "\n" + custom_queries if query_str else custom_queries
            )

        if query_str:
            return Query(language, query_str)
    except Exception as e:
        logger.debug(
            f"Failed to load fallback highlights query for {query_lang_name}: {e}"
        )

    return None


COMBINED_FUNC_CLASS_QUERIES: dict[cs.SupportedLanguage, Query | None] = {}
COMBINED_FUNC_CLASS_IMPORT_QUERIES: dict[cs.SupportedLanguage, Query | None] = {}


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

    combined_fc_pattern = f"{function_patterns} {class_patterns}".strip()
    try:
        COMBINED_FUNC_CLASS_QUERIES[lang_name] = (
            Query(language, combined_fc_pattern) if combined_fc_pattern else None
        )
    except Exception:
        COMBINED_FUNC_CLASS_QUERIES[lang_name] = None

    combined_fci_pattern = f"{function_patterns} {class_patterns} {combined_import_patterns} {call_patterns}".strip()
    try:
        COMBINED_FUNC_CLASS_IMPORT_QUERIES[lang_name] = (
            Query(language, combined_fci_pattern) if combined_fci_pattern else None
        )
    except Exception:
        COMBINED_FUNC_CLASS_IMPORT_QUERIES[lang_name] = None

    return LanguageQueries(
        functions=_create_optional_query(language, function_patterns),
        classes=_create_optional_query(language, class_patterns),
        calls=_create_optional_query(language, call_patterns),
        imports=_create_optional_query(language, combined_import_patterns),
        locals=_create_locals_query(language, lang_name),
        highlights=_create_highlights_query(language, lang_name),
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
    lang_lib = _get_language_library(lang_name)
    if not lang_lib:
        logger.debug(ls.LIB_NOT_AVAILABLE, lang=lang_name)
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
        # query compilation can fail AFTER the parser insert; drop the orphan
        # so the store never exposes a parser without its queries
        parsers.pop(lang_name, None)
        logger.warning(ls.GRAMMAR_LOAD_FAILED.format(lang=lang_name, error=e))
        return False


class _LazyLanguageView[V](Mapping[cs.SupportedLanguage, V]):
    # A read-only Mapping over the store's per-language dict; a missing key
    # triggers a one-time grammar load (issue #68). Mapping derives get,
    # keys, values, items, and __contains__ from the three methods below,
    # so laziness needs no dict-override tricks: membership and get load on
    # demand through __getitem__, and full-view operations (iteration, len,
    # and everything derived from them) probe every spec first so no
    # consumer ever sees a partial availability picture.
    __slots__ = ("_data", "_ensure", "_probe_all")

    def __init__(
        self,
        data: dict[cs.SupportedLanguage, V],
        ensure: Callable[[object], bool],
        probe_all: Callable[[], None],
    ) -> None:
        self._data = data
        self._ensure = ensure
        self._probe_all = probe_all

    def __getitem__(self, lang_name: cs.SupportedLanguage) -> V:
        if lang_name not in self._data:
            self._ensure(lang_name)
        return self._data[lang_name]

    def __iter__(self) -> Iterator[cs.SupportedLanguage]:
        self._probe_all()
        return iter(self._data)

    def __len__(self) -> int:
        self._probe_all()
        return len(self._data)


class _LazyGrammarStore:
    # Process-wide cache: grammars load once per interpreter, not once per
    # load_parsers() call (which previously recompiled EVERY language's
    # query set on every call). The lock serializes loads: without it a
    # thread probing a language MID-LOAD would see it in _attempted but not
    # yet in the dict and wrongly report it unavailable (PR #802 review).
    def __init__(self) -> None:
        self._parser_data: dict[cs.SupportedLanguage, Parser] = {}
        self._query_data: dict[cs.SupportedLanguage, LanguageQueries] = {}
        self._attempted: set[object] = set()
        self._lock = threading.Lock()
        self.parsers: Mapping[cs.SupportedLanguage, Parser] = _LazyLanguageView(
            self._parser_data, self._ensure, self._probe_all
        )
        self.queries: Mapping[cs.SupportedLanguage, LanguageQueries] = (
            _LazyLanguageView(self._query_data, self._ensure, self._probe_all)
        )

    def _ensure(self, lang_name: object) -> bool:
        # the lock-free fast path must see BOTH twin entries: the loader
        # writes the parser before the queries, so a lone parser entry means
        # mid-load or failed-load, not a completed language
        if lang_name in self._parser_data and lang_name in self._query_data:
            return True
        with self._lock:
            if lang_name in self._attempted:
                return lang_name in self._parser_data and lang_name in self._query_data
            self._attempted.add(lang_name)
            if not isinstance(lang_name, str):
                return False
            try:
                lang = cs.SupportedLanguage(lang_name)
            except ValueError:
                return False
            spec = LANGUAGE_SPECS.get(lang)
            if spec is None:
                return False
            return _process_language(
                lang, deepcopy(spec), self._parser_data, self._query_data
            )

    def _probe_all(self) -> None:
        for lang_key in LANGUAGE_SPECS:
            self._ensure(lang_key)
        if not self._parser_data:
            raise RuntimeError(ex.NO_LANGUAGES)


_store: _LazyGrammarStore | None = None
_store_lock = threading.Lock()


def _reset_parser_cache() -> None:
    # Test hook: discard every cached grammar so laziness can be observed
    # from a clean slate.
    global _store
    with _store_lock:
        _store = None


def load_parsers() -> tuple[
    Mapping[cs.SupportedLanguage, Parser],
    Mapping[cs.SupportedLanguage, LanguageQueries],
]:
    global _store
    store = _store
    if store is None:
        with _store_lock:
            if _store is None:
                _store = _LazyGrammarStore()
                logger.info(ls.PARSERS_LAZY_READY)
            store = _store
    return store.parsers, store.queries
