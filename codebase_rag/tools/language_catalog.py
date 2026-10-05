"""What `cgr language list-languages` shows (issue #2421).

Every language cgr parses, across its three tiers, read from each tier's own
registry: the tree-sitter `LANGUAGE_SPECS`, the ast-grep tier's pattern
configs and the document tier's extensions. Alongside them, the optional
compiler frontends as indexing would resolve them under the current settings.

The command imports this lazily: it loads the parsers package, which the CLI
must not pay for at start-up.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import StrEnum
from typing import NamedTuple

from loguru import logger
from rich.cells import cell_len
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .. import constants as cs
from .. import logs as ls
from ..config import settings
from ..language_spec import LANGUAGE_SPECS, LanguageSpec
from ..parser_loader import grammar_installed
from ..parsers import (
    cpp_frontend,
    csharp_frontend,
    go_frontend,
    java_frontend,
    py_frontend,
)
from ..parsers.ast_grep_tier import (
    structural_tier_config_names,
    structural_tier_languages,
)
from ..parsers.document_tier import DOCUMENT_EXTENSIONS, document_tier_available
from ..utils.dependencies import has_ast_grep


class LanguageEntry(NamedTuple):
    name: str
    extensions: tuple[str, ...]
    tier: cs.LanguageTier
    support: cs.LanguageSupport
    installed: bool


class FrontendEntry(NamedTuple):
    name: cs.FrontendName
    languages: tuple[str, ...]
    setting: str
    toolchain_found: bool
    active: bool


def _display_name(lang: cs.SupportedLanguage) -> str:
    # A language added with `add-grammar` has a spec but no metadata.
    meta = cs.LANGUAGE_METADATA.get(lang)
    return meta.display_name if meta is not None else str(lang)


def _tree_sitter_support(lang: cs.SupportedLanguage) -> cs.LanguageSupport:
    # Only metadata can vouch for full support; a spec written by
    # `add-grammar` has auto-detected node types and nothing hand-written.
    meta = cs.LANGUAGE_METADATA.get(lang)
    if meta is not None and meta.status == cs.LanguageStatus.FULL:
        return cs.LanguageSupport.FULL
    return cs.LanguageSupport.IN_DEVELOPMENT


def _tree_sitter_specs() -> list[tuple[cs.SupportedLanguage, LanguageSpec]]:
    # Fully supported first, then by key: the order of the generated support
    # matrix in the README and docs.
    return sorted(
        LANGUAGE_SPECS.items(),
        key=lambda item: (
            _tree_sitter_support(item[0]) != cs.LanguageSupport.FULL,
            str(item[0]),
        ),
    )


def tree_sitter_languages() -> list[LanguageEntry]:
    return [
        LanguageEntry(
            name=_display_name(lang),
            extensions=tuple(spec.file_extensions),
            tier=cs.LanguageTier.TREE_SITTER,
            support=_tree_sitter_support(lang),
            installed=grammar_installed(lang),
        )
        for lang, spec in _tree_sitter_specs()
    ]


def _ast_grep_entry(
    name: str, extensions: tuple[str, ...], installed: bool
) -> LanguageEntry:
    return LanguageEntry(
        # configs spell the name in lower case (`language: ruby`)
        name=name[:1].upper() + name[1:],
        extensions=extensions,
        tier=cs.LanguageTier.AST_GREP,
        support=cs.LanguageSupport.STRUCTURAL,
        installed=installed,
    )


def ast_grep_languages() -> list[LanguageEntry]:
    try:
        languages = structural_tier_languages()
    except ImportError:
        # No PyYAML, which the [ast-grep] extra installs: the tier cannot run,
        # but its languages are still listed, unavailable, so the install
        # hint names the extra that adds them (Greptile review of PR 2508).
        # Their extensions are in the configs this cannot read.
        return [
            _ast_grep_entry(name, (), installed=False)
            for name in structural_tier_config_names()
        ]
    except Exception as exc:  # noqa: BLE001
        # AstGrepTier disables itself on the same failure, so leaving these
        # out is accurate; the other tiers must still be listed.
        logger.warning(ls.LANG_LIST_AST_GREP_UNREADABLE.format(error=exc))
        return []
    installed = has_ast_grep()
    return [
        _ast_grep_entry(name, extensions, installed)
        for name, extensions in languages.items()
    ]


def document_languages() -> list[LanguageEntry]:
    return [
        LanguageEntry(
            name=cs.DOCUMENT_TIER_LANGUAGE,
            extensions=tuple(sorted(DOCUMENT_EXTENSIONS)),
            tier=cs.LanguageTier.DOCUMENT,
            support=cs.LanguageSupport.HEADINGS,
            installed=document_tier_available(),
        )
    ]


def language_catalog() -> list[LanguageEntry]:
    return [*tree_sitter_languages(), *ast_grep_languages(), *document_languages()]


def _frontend_entry[Mode: StrEnum](
    name: cs.FrontendName,
    languages: Iterable[cs.SupportedLanguage],
    setting: str,
    configured: Mode,
    disabled: Mode,
    resolve: Callable[[], Mode],
    toolchain_found: Callable[[], bool],
) -> FrontendEntry:
    shown_setting = cs.LANG_SETTING_FMT.format(name=setting, value=configured.value)
    names = tuple(_display_name(lang) for lang in languages)
    if configured == disabled:
        # Off by setting: the resolver would not look at the toolchain, so
        # probe it here, or the listing could not say whether turning the
        # frontend on would work.
        return FrontendEntry(name, names, shown_setting, toolchain_found(), False)
    # Indexing asks the resolver, so its answer is what "active" means. With
    # the frontend enabled it probes the toolchain itself, and it lands on
    # the disabled mode exactly when that probe fails; probing again would
    # start javac's two JVMs twice for one answer.
    active = resolve() != disabled
    return FrontendEntry(name, names, shown_setting, active, active)


def frontend_catalog() -> list[FrontendEntry]:
    return [
        _frontend_entry(
            cs.FrontendName.LIBCLANG,
            sorted(cs.C_FAMILY_LANGUAGES),
            cs.SETTING_CPP_FRONTEND,
            settings.CPP_FRONTEND,
            cs.CppFrontend.TREESITTER,
            cpp_frontend.resolve_cpp_frontend,
            cpp_frontend.cpp_frontend_available,
        ),
        _frontend_entry(
            cs.FrontendName.GO_TYPES,
            (cs.SupportedLanguage.GO,),
            cs.SETTING_GO_FRONTEND,
            settings.GO_FRONTEND,
            cs.GoFrontend.TREESITTER,
            go_frontend.resolve_go_frontend,
            go_frontend.go_frontend_available,
        ),
        _frontend_entry(
            cs.FrontendName.ROSLYN,
            (cs.SupportedLanguage.CSHARP,),
            cs.SETTING_CSHARP_FRONTEND,
            settings.CSHARP_FRONTEND,
            cs.CSharpFrontend.TREESITTER,
            csharp_frontend.resolve_csharp_frontend,
            csharp_frontend.csharp_frontend_available,
        ),
        _frontend_entry(
            cs.FrontendName.JAVAC,
            (cs.SupportedLanguage.JAVA,),
            cs.SETTING_JAVA_FRONTEND,
            settings.JAVA_FRONTEND,
            cs.JavaFrontend.HEURISTIC,
            java_frontend.resolve_java_frontend,
            java_frontend.java_frontend_available,
        ),
        _frontend_entry(
            cs.FrontendName.JEDI,
            (cs.SupportedLanguage.PYTHON,),
            cs.SETTING_PYTHON_FRONTEND,
            settings.PYTHON_FRONTEND,
            cs.PythonFrontend.HEURISTIC,
            py_frontend.resolve_python_frontend,
            py_frontend.python_frontend_available,
        ),
    ]


def _widest(cells: Iterable[str]) -> int:
    return max((cell_len(cell) for cell in cells), default=0)


def _yes_no(value: bool) -> Text:
    if value:
        return Text(cs.LANG_TABLE_YES, style=cs.Color.GREEN)
    return Text(cs.LANG_TABLE_NO, style=cs.Color.RED)


def _new_table(title: str, caption: str | None = None) -> Table:
    return Table(
        title=title,
        caption=caption,
        caption_justify="left",
        show_header=True,
        header_style=f"bold {cs.Color.MAGENTA}",
    )


def _cell_padding(columns: int) -> int:
    # one space either side of every cell, plus a border between and around
    return 2 * columns + columns + 1


def languages_table(entries: list[LanguageEntry], width: int) -> Table:
    names = [entry.name for entry in entries]
    extensions = [cs.LANG_TABLE_SEPARATOR.join(entry.extensions) for entry in entries]
    secondary = (
        (cs.LANG_TABLE_COL_TIER, [entry.tier.value for entry in entries]),
        (cs.LANG_TABLE_COL_SUPPORT, [entry.support.value for entry in entries]),
        (cs.LANG_TABLE_COL_INSTALLED, [cs.LANG_TABLE_YES, cs.LANG_TABLE_NO]),
    )
    # Language and Extensions get fixed widths, sized here, because Rich
    # otherwise shares a narrow terminal out by shrinking the widest columns,
    # which squeezed the names to nothing (issue #2421). A fixed column is
    # left alone while the others can still give way, so on a narrow
    # terminal Tier, Support and Installed fold instead. The name is never
    # wrapped; the extensions wrap between entries, never inside one.
    name_width = _widest([cs.LANG_TABLE_COL_LANGUAGE, *names])
    extensions_floor = _widest(
        [
            cs.LANG_TABLE_COL_EXTENSIONS,
            # an extension plus the comma that follows it
            *(
                ext + cs.LANG_TABLE_SEPARATOR.strip()
                for entry in entries
                for ext in entry.extensions
            ),
        ]
    )
    room = (
        width
        - _cell_padding(2 + len(secondary))
        - name_width
        - sum(_widest([header, *values]) for header, values in secondary)
    )
    extensions_width = max(
        extensions_floor,
        min(_widest([cs.LANG_TABLE_COL_EXTENSIONS, *extensions]), room),
    )

    table = _new_table(cs.LANG_TABLE_TITLE, cs.LANG_SUPPORT_LEGEND)
    table.add_column(
        cs.LANG_TABLE_COL_LANGUAGE,
        style=cs.Color.CYAN,
        no_wrap=True,
        width=name_width,
    )
    table.add_column(
        cs.LANG_TABLE_COL_EXTENSIONS,
        style=cs.Color.GREEN,
        width=extensions_width,
    )
    for header, _ in secondary:
        table.add_column(header, overflow="fold")
    for entry, extension_list in zip(entries, extensions, strict=True):
        table.add_row(
            entry.name,
            extension_list,
            entry.tier.value,
            entry.support.value,
            _yes_no(entry.installed),
        )
    return table


def install_hints(entries: list[LanguageEntry]) -> list[str]:
    tiers_by_extra: dict[str, list[str]] = {}
    for tier in cs.LanguageTier:
        if any(entry.tier == tier and not entry.installed for entry in entries):
            tiers_by_extra.setdefault(cs.TIER_EXTRAS[tier], []).append(tier.value)
    return [
        cs.LANG_INSTALL_HINT.format(
            tier=cs.LANG_TABLE_SEPARATOR.join(tiers), extra=extra
        )
        for extra, tiers in tiers_by_extra.items()
    ]


def _node_mappings(spec: LanguageSpec) -> list[tuple[cs.NodeMappingKind, str]]:
    return [
        (kind, cs.LANG_TABLE_SEPARATOR.join(types) or cs.LANG_TABLE_PLACEHOLDER)
        for kind, types in (
            (cs.NodeMappingKind.FUNCTIONS, spec.function_node_types),
            (cs.NodeMappingKind.CLASSES, spec.class_node_types),
            (cs.NodeMappingKind.MODULES, spec.module_node_types),
            (cs.NodeMappingKind.CALLS, spec.call_node_types),
        )
    ]


def node_types_table() -> Table:
    specs = _tree_sitter_specs()
    table = _new_table(cs.LANG_NODE_TABLE_TITLE)
    table.add_column(
        cs.LANG_TABLE_COL_LANGUAGE,
        style=cs.Color.CYAN,
        no_wrap=True,
        min_width=_widest(_display_name(lang) for lang, _ in specs),
    )
    table.add_column(cs.LANG_TABLE_COL_NODE_KIND, no_wrap=True)
    # the only column that gives way: node types wrap between entries and
    # fold, never truncate, when one is wider than what is left
    table.add_column(
        cs.LANG_TABLE_COL_NODE_TYPES, style=cs.Color.YELLOW, overflow="fold"
    )
    for lang, spec in specs:
        for index, (kind, node_types) in enumerate(_node_mappings(spec)):
            # the name heads its block only, so each block reads as one row
            table.add_row(_display_name(lang) if index == 0 else "", kind, node_types)
        table.add_section()
    return table


def frontends_table(entries: list[FrontendEntry]) -> Table:
    table = _new_table(cs.LANG_FRONTEND_TABLE_TITLE, cs.LANG_FRONTEND_LEGEND)
    for header, style in (
        (cs.LANG_TABLE_COL_FRONTEND, cs.Color.CYAN),
        (cs.LANG_TABLE_COL_LANGUAGES, None),
        (cs.LANG_TABLE_COL_TOOLCHAIN, None),
        (cs.LANG_TABLE_COL_SETTING, None),
        (cs.LANG_TABLE_COL_ACTIVE, None),
    ):
        table.add_column(header, style=style, overflow="fold")
    for entry in entries:
        toolchain = (
            Text(cs.LANG_TOOLCHAIN_FOUND, style=cs.Color.GREEN)
            if entry.toolchain_found
            else Text(cs.LANG_TOOLCHAIN_MISSING, style=cs.Color.RED)
        )
        table.add_row(
            entry.name.value,
            cs.LANG_TABLE_SEPARATOR.join(entry.languages),
            toolchain,
            entry.setting,
            _yes_no(entry.active),
        )
    return table


def print_language_catalog(console: Console, verbose: bool) -> None:
    entries = language_catalog()
    console.print(languages_table(entries, console.width))
    for hint in install_hints(entries):
        # Text, not a markup string: the extra's name is in [brackets]
        console.print(Text(hint, style=cs.Color.YELLOW))
    console.print()
    console.print(frontends_table(frontend_catalog()))
    if verbose:
        console.print()
        console.print(node_types_table())
