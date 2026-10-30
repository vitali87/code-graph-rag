"""The table a pass looks a language up in to find the collaborator that
handles it."""

from __future__ import annotations

from collections.abc import Mapping

from .. import constants as cs


def by_language[T](
    collaborators: Mapping[cs.SupportedLanguage | frozenset[cs.SupportedLanguage], T],
) -> dict[cs.SupportedLanguage, T]:
    """Each language's collaborator, keyed by single language.

    A family key (`cs.JS_TS_LANGUAGES`) hands every language in it the same
    collaborator, so one written for the family is named once rather than
    once per language."""
    return {
        language: collaborator
        for key, collaborator in collaborators.items()
        for language in (key if isinstance(key, frozenset) else (key,))
    }
