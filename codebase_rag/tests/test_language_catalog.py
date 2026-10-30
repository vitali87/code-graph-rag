"""The entries `cgr language list-languages` is built from: the order and
support of the tree-sitter languages, the install hint per extra, what a
frontend's "active" means, and the column the language names keep."""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.language.catalog import (
    FrontendEntry,
    LanguageEntry,
    _frontend_entry,
    install_hints,
    languages_table,
    tree_sitter_languages,
)
from codebase_rag.language_spec import LANGUAGE_SPECS


def _entry(
    name: str, tier: cs.LanguageTier, installed: bool, extensions: tuple[str, ...] = ()
) -> LanguageEntry:
    return LanguageEntry(name, extensions, tier, cs.LanguageSupport.FULL, installed)


def test_fully_supported_languages_are_listed_before_the_rest() -> None:
    entries = tree_sitter_languages()
    supports = [entry.support for entry in entries]
    first_partial = supports.index(cs.LanguageSupport.IN_DEVELOPMENT)
    assert cs.LanguageSupport.FULL not in supports[first_partial:]
    assert len(entries) == len(LANGUAGE_SPECS)
    assert {entry.tier for entry in entries} == {cs.LanguageTier.TREE_SITTER}


def test_a_tree_sitter_entry_carries_its_specs_extensions() -> None:
    (python,) = [e for e in tree_sitter_languages() if e.name == "Python"]
    assert python.extensions == tuple(
        LANGUAGE_SPECS[cs.SupportedLanguage.PYTHON].file_extensions
    )
    assert python.support == cs.LanguageSupport.FULL


def test_no_hint_when_every_tier_is_installed() -> None:
    entries = [
        _entry("Python", cs.LanguageTier.TREE_SITTER, installed=True),
        _entry("Ruby", cs.LanguageTier.AST_GREP, installed=True),
    ]
    assert install_hints(entries) == []


def test_tiers_sharing_an_extra_share_one_hint() -> None:
    entries = [
        _entry("Zig", cs.LanguageTier.TREE_SITTER, installed=False),
        _entry("Markdown", cs.LanguageTier.DOCUMENT, installed=False),
        _entry("Ruby", cs.LanguageTier.AST_GREP, installed=True),
    ]
    assert install_hints(entries) == [
        cs.LANG_INSTALL_HINT.format(
            tier=f"{cs.LanguageTier.TREE_SITTER}, {cs.LanguageTier.DOCUMENT}",
            extra=cs.TIER_EXTRAS[cs.LanguageTier.TREE_SITTER],
        )
    ]


def test_each_missing_extra_gets_its_own_hint() -> None:
    entries = [
        _entry("Zig", cs.LanguageTier.TREE_SITTER, installed=False),
        _entry("Ruby", cs.LanguageTier.AST_GREP, installed=False),
    ]
    hints = install_hints(entries)
    assert len(hints) == 2
    assert any(cs.TIER_EXTRAS[cs.LanguageTier.AST_GREP] in hint for hint in hints)


class _Probes:
    """Counts how often the resolver and the toolchain probe were asked."""

    def __init__(self, resolved: cs.GoFrontend, toolchain: bool) -> None:
        self.resolved = resolved
        self.toolchain = toolchain
        self.resolves = 0
        self.probes = 0

    def resolve(self) -> cs.GoFrontend:
        self.resolves += 1
        return self.resolved

    def toolchain_found(self) -> bool:
        self.probes += 1
        return self.toolchain


def _go_entry(configured: cs.GoFrontend, probes: _Probes) -> FrontendEntry:
    return _frontend_entry(
        cs.FrontendName.GO_TYPES,
        (cs.SupportedLanguage.GO,),
        cs.SETTING_GO_FRONTEND,
        configured,
        cs.GoFrontend.TREESITTER,
        probes.resolve,
        probes.toolchain_found,
    )


def test_a_disabled_frontend_probes_its_toolchain_and_is_never_active() -> None:
    probes = _Probes(resolved=cs.GoFrontend.TREESITTER, toolchain=True)
    entry = _go_entry(cs.GoFrontend.TREESITTER, probes)
    assert entry.toolchain_found is True
    assert entry.active is False
    assert (probes.probes, probes.resolves) == (1, 0)
    assert entry.setting == cs.LANG_SETTING_FMT.format(
        name=cs.SETTING_GO_FRONTEND, value=cs.GoFrontend.TREESITTER.value
    )


@pytest.mark.parametrize(
    ("resolved", "active"),
    [(cs.GoFrontend.GOTYPES, True), (cs.GoFrontend.TREESITTER, False)],
)
def test_an_enabled_frontend_is_whatever_its_resolver_answers(
    resolved: cs.GoFrontend, active: bool
) -> None:
    probes = _Probes(resolved=resolved, toolchain=not active)
    entry = _go_entry(cs.GoFrontend.GOTYPES, probes)
    assert (entry.active, entry.toolchain_found) == (active, active)
    # The resolver probed the toolchain itself: no second probe.
    assert (probes.probes, probes.resolves) == (0, 1)
    assert entry.languages == ("Go",)


def test_the_language_column_is_as_wide_as_the_widest_name_and_never_wraps() -> None:
    entries = [
        _entry("C", cs.LanguageTier.TREE_SITTER, True, (".c", ".h")),
        _entry("Objective-Something", cs.LanguageTier.AST_GREP, False, (".m",)),
    ]
    table = languages_table(entries, width=50)
    language, extensions, *_ = table.columns
    assert language.width == len("Objective-Something")
    assert language.no_wrap
    # The extensions column never narrows below its widest single extension.
    assert extensions.width is not None
    assert extensions.width >= len("Extensions")
    assert table.row_count == 2
