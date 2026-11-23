"""Which engine indexes each language: the `CGR_INDEX_ENGINE` setting.

Phase 1 of the Rust rewrite (docs/reports/PHASE1_SEAMS.md, section 1). The
value is parsed into an `IndexEngineSelection`, refused at start-up when it is
invalid, and resolved to the engine each language runs on. This build cannot
index any language on Rust, so every selection resolves to Python everywhere.
"""

from __future__ import annotations

from types import MappingProxyType

from loguru import logger

from . import constants as cs
from . import exceptions as ex
from . import logs as ls
from .capture import split_spec
from .types_defs import IndexEngineSelection

_ENGINES = cs.SEPARATOR_COMMA_SPACE.join(cs.IndexEngine)
_LANGUAGES = cs.SEPARATOR_COMMA_SPACE.join(cs.SupportedLanguage)
# The entry of INDEX_ENGINE_DEFAULTS each language falls under: its own, or
# its family's.
_UNIT_OF = {language: unit for unit in cs.INDEX_ENGINE_DEFAULTS for language in unit}
# Languages named on Rust that this process has warned about already: the
# watcher and the MCP server resolve the setting again for every updater.
_warned: set[cs.SupportedLanguage] = set()


def rust_engine_languages() -> frozenset[cs.SupportedLanguage]:
    """The languages this build can index on Rust: none yet.

    From phase 2 this reads the list the engine crate publishes, which names
    whole families only.
    """
    return frozenset()


def _engine(name: str) -> cs.IndexEngine:
    try:
        return cs.IndexEngine(name)
    except ValueError as error:
        raise ValueError(
            ex.INDEX_ENGINE_UNKNOWN_ENGINE.format(engine=name, engines=_ENGINES)
        ) from error


def _language(name: str) -> cs.SupportedLanguage:
    try:
        return cs.SupportedLanguage(name)
    except ValueError as error:
        raise ValueError(
            ex.INDEX_ENGINE_UNKNOWN_LANGUAGE.format(language=name, languages=_LANGUAGES)
        ) from error


def _refuse_split_families(selection: IndexEngineSelection) -> None:
    # Compared as asked, not as resolved: `auto` and `python` are the same
    # engine in this release but not in every one, and a value must not turn
    # invalid when a family's default moves.
    for unit in cs.INDEX_ENGINE_DEFAULTS:
        asked = {
            language: selection.languages.get(language, selection.default)
            for language in cs.SupportedLanguage
            if language in unit
        }
        if len(set(asked.values())) > 1:
            raise ValueError(
                ex.INDEX_ENGINE_FAMILY_SPLIT.format(
                    family=cs.SEPARATOR_COMMA_SPACE.join(asked),
                    asked=cs.SEPARATOR_COMMA_SPACE.join(
                        f"{language}{cs.INDEX_ENGINE_ASSIGN}{engine}"
                        for language, engine in asked.items()
                    ),
                )
            )


def parse_index_engine(spec: str) -> IndexEngineSelection:
    """The selection a `CGR_INDEX_ENGINE` value asks for, read in any case.

    Tokens split as `CGR_CAPTURE`'s do: an engine on its own sets every
    language's, and `LANGUAGE=ENGINE` sets one. Raises ValueError for the
    first token at fault, or for a family the value would split.
    """
    default: str | None = None
    languages: dict[cs.SupportedLanguage, cs.IndexEngine] = {}
    for token in split_spec(spec.lower()):
        name, assign, engine = token.partition(cs.INDEX_ENGINE_ASSIGN)
        if not assign:
            _engine(token)
            if default is not None:
                raise ValueError(
                    ex.INDEX_ENGINE_TWO_DEFAULTS.format(first=default, second=token)
                )
            default = token
            continue
        language = _language(name)
        if not engine:
            raise ValueError(
                ex.INDEX_ENGINE_NO_ENGINE.format(token=token, engines=_ENGINES)
            )
        if language in languages:
            raise ValueError(ex.INDEX_ENGINE_LANGUAGE_TWICE.format(language=name))
        languages[language] = _engine(engine)
    selection = IndexEngineSelection(
        default=cs.IndexEngine.AUTO if default is None else cs.IndexEngine(default),
        languages=MappingProxyType(languages),
    )
    _refuse_split_families(selection)
    return selection


def resolve_index_engines(
    selection: IndexEngineSelection,
) -> dict[cs.SupportedLanguage, cs.IndexEngine]:
    """The engine every language runs on, never `auto`.

    A language runs on the engine asked for when this build can run it there,
    and on Python otherwise. That never stops cgr from starting: Rust asked
    for by name is warned about once per process, and asked for through a
    default (`rust`, or `auto` once a default is Rust) is only noted.
    """
    available = rust_engine_languages()
    engines: dict[cs.SupportedLanguage, cs.IndexEngine] = {}
    by_default: list[cs.SupportedLanguage] = []
    for language in cs.SupportedLanguage:
        named = selection.languages.get(language)
        asked = selection.default if named is None else named
        engine = (
            cs.INDEX_ENGINE_DEFAULTS[_UNIT_OF[language]]
            if asked == cs.IndexEngine.AUTO
            else asked
        )
        if engine == cs.IndexEngine.RUST and language not in available:
            engine = cs.IndexEngine.PYTHON
            if named != cs.IndexEngine.RUST:
                by_default.append(language)
            elif language not in _warned:
                _warned.add(language)
                logger.warning(ls.INDEX_ENGINE_NOT_ON_RUST.format(language=language))
        engines[language] = engine
    if by_default:
        logger.debug(
            ls.INDEX_ENGINE_DEFAULT_NOT_ON_RUST.format(
                languages=cs.SEPARATOR_COMMA_SPACE.join(by_default)
            )
        )
    return engines
