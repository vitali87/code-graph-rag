"""The providers package registers the LiteLLM provider only where
pydantic-ai's LiteLLM support can be imported, asked of the import system
without loading it."""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
from collections.abc import Callable

import pytest
from loguru import logger

import codebase_rag.providers as providers_package
from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.providers import _litellm_importable
from codebase_rag.providers.base import PROVIDER_REGISTRY
from codebase_rag.providers.litellm import LiteLLMProvider

_REAL_FIND_SPEC = importlib.util.find_spec


def _find_spec_answering(
    answer: Callable[[], importlib.machinery.ModuleSpec | None],
) -> Callable[..., importlib.machinery.ModuleSpec | None]:
    """`find_spec` that answers `answer()` for the LiteLLM support module and
    the real lookup for every other name."""

    def find_spec(
        name: str, package: str | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if name == cs.PYDANTIC_AI_LITELLM_MODULE:
            return answer()
        return _REAL_FIND_SPEC(name, package)

    return find_spec


def _missing_parent() -> importlib.machinery.ModuleSpec | None:
    # What find_spec raises for a dotted name whose parent package is absent.
    raise ModuleNotFoundError(cs.PYDANTIC_AI_LITELLM_MODULE)


def test_an_installed_support_module_is_importable() -> None:
    assert _REAL_FIND_SPEC(cs.PYDANTIC_AI_LITELLM_MODULE) is not None
    assert _litellm_importable() is True


def test_a_module_the_import_system_cannot_find_is_not_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", _find_spec_answering(lambda: None))
    assert _litellm_importable() is False


def test_a_missing_parent_package_reads_as_not_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        importlib.util, "find_spec", _find_spec_answering(_missing_parent)
    )
    assert _litellm_importable() is False


def test_importing_the_package_registers_the_litellm_provider() -> None:
    assert PROVIDER_REGISTRY[cs.Provider.LITELLM_PROXY] is LiteLLMProvider


def test_without_litellm_support_the_package_registers_no_litellm_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(PROVIDER_REGISTRY, cs.Provider.LITELLM_PROXY)
    monkeypatch.setattr(importlib.util, "find_spec", _find_spec_answering(lambda: None))
    messages: list[str] = []
    sink = logger.add(messages.append, level="DEBUG", format="{message}")
    try:
        importlib.reload(providers_package)
    finally:
        logger.remove(sink)

    assert cs.Provider.LITELLM_PROXY not in PROVIDER_REGISTRY
    assert [m.strip() for m in messages] == [ls.LITELLM_PROVIDER_UNAVAILABLE]
