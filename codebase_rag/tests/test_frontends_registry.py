"""The LanguageFrontend registry + SemanticFacts contract (issue #1178)."""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.parsers.frontends import (
    FRONTENDS,
    LanguageFrontend,
    ResolvedCallSite,
    SemanticFacts,
    empty_facts,
    register_frontend,
)


def test_csharp_frontend_is_registered() -> None:
    frontend = FRONTENDS.get(cs.SupportedLanguage.CSHARP)
    assert frontend is not None
    assert frontend.language == cs.SupportedLanguage.CSHARP
    # The protocol surface is callable; `available()` reflects the real toolchain
    # (False in a dotnet-less environment -> clean degradation to tree-sitter).
    assert isinstance(frontend.available(), bool)
    assert isinstance(frontend, LanguageFrontend)


def test_empty_facts_are_fresh_and_empty() -> None:
    a = empty_facts()
    b = empty_facts()
    assert a.is_empty() and b.is_empty()
    # Never a shared constant: mutating one must not affect a later fresh bundle.
    a.resolved_call_sites[("f.cs", 1, 2, "M")] = ResolvedCallSite("M", "g.cs", 3, 4)
    a.external_sites.add(("f.cs", 5, 6, "N"))
    assert not a.is_empty()
    assert empty_facts().is_empty()
    assert a.resolved_call_sites is not b.resolved_call_sites


def test_register_frontend_returns_the_frontend() -> None:
    class _Fake:
        language = cs.SupportedLanguage.SCALA

        def available(self) -> bool:
            return False

        def applies(self, repo_path: object) -> bool:
            return False

        def run(self, repo_path: object, files: object) -> SemanticFacts:
            return empty_facts()

    fake = _Fake()
    try:
        assert register_frontend(fake) is fake
        assert FRONTENDS[cs.SupportedLanguage.SCALA] is fake
    finally:
        FRONTENDS.pop(cs.SupportedLanguage.SCALA, None)
