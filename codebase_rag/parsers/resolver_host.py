"""What several languages' resolvers call back into on the `CallResolver`
that builds them, declared once for their host Protocols to take.

A language's resolver narrows a question or vetoes an answer, and hands
what every language resolves alike back to the resolver: the language a
module is written in, a module's parsed tree, a written type to its class,
a class's bases in lookup order and its own or inherited method, the
re-export walk, the direct-import probe, and the cache record of an answer
with how surely it bound. This module imports no resolver, so each can
import it; a language's host Protocol extends `ResolverHost` and adds only
what that language alone calls."""

from typing import Protocol

from tree_sitter import Node

from .. import constants as cs


class ResolverHost(Protocol):
    """The resolution every language shares, which a language's stage hands
    a narrowed question to or falls back on. `verdict` is how surely the
    answer being built binds its callee: a language stage that binds by name
    alone marks it a guess, and the cache record keeps it beside the
    answer, so a cache hit reports what the miss did."""

    verdict: str

    def module_language(self, qualified_name: str) -> cs.SupportedLanguage | None: ...

    def cached_module_root(self, module_qn: str) -> Node | None: ...

    def remember_cacheable(
        self, cache_key: tuple[str, str, bool] | None, result: tuple[str, str] | None
    ) -> None: ...

    def resolve_class_qn_from_type(
        self, var_type: str, import_map: dict[str, str], module_qn: str
    ) -> str: ...

    def mro(self, class_qn: str) -> list[str]: ...

    def try_resolve_method(
        self, class_qn: str, method_name: str, separator: str = cs.SEPARATOR_DOT
    ) -> tuple[str, str] | None: ...

    def resolve_inherited_method(
        self, class_qn: str, method_name: str, own_members: bool = True
    ) -> tuple[str, str] | None: ...

    def follow_reexports(self, class_qn: str) -> str: ...

    def try_resolve_direct_import(
        self,
        call_name: str,
        import_map: dict[str, str],
        language: cs.SupportedLanguage | None = None,
        module_qn: str | None = None,
    ) -> tuple[str, str] | None: ...
