"""Which engine indexes each language: the `CGR_INDEX_ENGINE` setting.

Phase 1 of the Rust rewrite (docs/reports/PHASE1_SEAMS.md, section 1). The
value is parsed into an `IndexEngineSelection`, refused at start-up when it is
invalid, and resolved to the engine each language runs on. This build lists
no language the Rust engine indexes, so every selection resolves to Python
everywhere, unless a preview names one.

A preview (`PREVIEW`, docs/reports/PHASE3_GO.md, section 10) runs a language
on the Rust engine while its port is under way, before the build lists it:
a selection asking for it on Rust then gets it there. It is a module flag,
not a setting, as `engine_frame.ENABLED` is: the test plugin's
`--engine-preview` and the scripts' `--preview` set it, in their own process
only, so nothing a user or a gating cell runs ever reaches it. While the
engine holds none of a previewed language's analysis, Python's passes still
index its files, and a preview compares Python with itself.
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

# The languages a preview runs on the Rust engine: none unless a test session
# or a script asks (`parse_preview`). Whole families only.
PREVIEW: frozenset[cs.SupportedLanguage] = frozenset()


def rust_engine_languages() -> frozenset[cs.SupportedLanguage]:
    """The languages this build can index on Rust: those a preview names.

    Once a language's port is done this also reads the list the engine crate
    publishes (docs/reports/PHASE3_GO.md, section 10), which names whole
    families only.
    """
    return PREVIEW


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


def parse_preview(spec: str) -> frozenset[cs.SupportedLanguage]:
    """The languages a preview value names, read in any case and split as
    `CGR_INDEX_ENGINE`'s tokens are. Raises ValueError for a name that is no
    language, or for part of a family, which takes one engine together."""
    languages = frozenset(_language(name) for name in split_spec(spec.lower()))
    for unit in cs.INDEX_ENGINE_DEFAULTS:
        if languages & unit and not unit <= languages:
            raise ValueError(
                ex.INDEX_ENGINE_PREVIEW_SPLIT.format(
                    family=cs.SEPARATOR_COMMA_SPACE.join(sorted(unit)),
                    asked=cs.SEPARATOR_COMMA_SPACE.join(sorted(languages & unit)),
                )
            )
    return languages


def preview_error(spec: str) -> str | None:
    """Why `parse_preview` refuses `spec`, or None when it reads."""
    try:
        parse_preview(spec)
    except ValueError as error:
        return str(error)
    return None


def index_engine_error(spec: str) -> str | None:
    """Why `parse_index_engine` refuses `spec`, or None when it reads: for
    a caller that reports the refusal itself, such as a test marker."""
    try:
        parse_index_engine(spec)
    except ValueError as error:
        return str(error)
    return None


def default_index_engine() -> IndexEngineSelection:
    """The selection the `CGR_INDEX_ENGINE` setting asks for, read now.

    What `GraphUpdater` reads when it is given none, as `capture` reads
    `CGR_CAPTURE` through `default_capture()`.
    """
    # Imported here: `config` validates the setting with `parse_index_engine`.
    from .config import settings

    return parse_index_engine(settings.INDEX_ENGINE)


def asked_index_engines(
    selection: IndexEngineSelection,
) -> dict[cs.SupportedLanguage, cs.IndexEngine]:
    """The engine `selection` asks for each language, `auto` read as this
    release's default, whether or not this build can run it there."""
    engines: dict[cs.SupportedLanguage, cs.IndexEngine] = {}
    for language in cs.SupportedLanguage:
        asked = selection.languages.get(language, selection.default)
        engines[language] = (
            cs.INDEX_ENGINE_DEFAULTS[_UNIT_OF[language]]
            if asked == cs.IndexEngine.AUTO
            else asked
        )
    return engines


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
    for language, asked in asked_index_engines(selection).items():
        engines[language] = asked
        if asked != cs.IndexEngine.RUST or language in available:
            continue
        engines[language] = cs.IndexEngine.PYTHON
        if selection.languages.get(language) != cs.IndexEngine.RUST:
            by_default.append(language)
        elif language not in _warned:
            _warned.add(language)
            logger.warning(ls.INDEX_ENGINE_NOT_ON_RUST.format(language=language))
    if by_default:
        logger.debug(
            ls.INDEX_ENGINE_DEFAULT_NOT_ON_RUST.format(
                languages=cs.SEPARATOR_COMMA_SPACE.join(by_default)
            )
        )
    return engines
