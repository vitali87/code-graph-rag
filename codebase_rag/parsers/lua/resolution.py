"""Lua's share of the name-based callee resolution: the member a table holds
under either of the two spellings a Lua call and its definition may use, in
the table a `require` binds or in one the caller's own module defines.

`CallResolver` owns what every language shares (the order the stages run in,
the resolution cache and the verdict each answer carries, the method lookup
and the package-member search these spellings feed) and hands Lua's part to
the `LuaResolver` it builds and the functions here."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...types_defs import FunctionRegistryTrieProtocol
from . import utils as lua_utils

if TYPE_CHECKING:
    from ..call_site import CallSite


def lua_member_spellings(
    language: cs.SupportedLanguage | None,
    class_qn: str,
    method_name: str,
    separator: str,
) -> tuple[str, ...]:
    # A Lua instance reaches its class's members under either spelling:
    # `acc:deposit()` is `Account:deposit`, `s.area(s)` and `s:twice()`
    # may name a member defined with the other separator. Empty for any
    # other language, whose member has the one spelling the call writes.
    if language != cs.SupportedLanguage.LUA:
        return ()
    return lua_utils.member_spellings(class_qn, method_name, separator)


def lua_package_methods(
    language: cs.SupportedLanguage | None,
    members: Iterable[tuple[str, cs.NodeLabel]],
    member_depth: int,
    member_name: str,
) -> list[str]:
    # A Lua module's returned table holds `M.add` one segment below
    # the file, like a Go file's function, but its colon-method
    # `M:method` is that one segment itself (issue #2481).
    if language != cs.SupportedLanguage.LUA:
        return []
    return [
        qn
        for qn, _ in members
        if qn.count(cs.SEPARATOR_DOT) == member_depth - 1
        and qn.rsplit(cs.SEPARATOR_DOT, 1)[-1].partition(cs.LUA_METHOD_SEPARATOR)[2]
        == member_name
    ]


class LuaResolverHost(Protocol):
    """What Lua resolution calls on the `CallResolver` that builds it: the
    cache bookkeeping that records an answer with its verdict, and the
    package-member search every language shares, which finds the one table
    of a required module that holds a member."""

    def _remember_cacheable(
        self, cache_key: tuple[str, str, bool] | None, result: tuple[str, str] | None
    ) -> None: ...

    def _try_resolve_package_member(
        self,
        package_qn: str,
        member_name: str,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str] | None: ...


class LuaResolver:
    """Binds Lua calls to the table members they name.

    Built and owned by `CallResolver`. The registry is the resolver's own,
    held by reference and read as it stands when a call resolves."""

    __slots__ = ("_host", "function_registry")

    def __init__(
        self, host: LuaResolverHost, function_registry: FunctionRegistryTrieProtocol
    ) -> None:
        self._host = host
        self.function_registry = function_registry

    def resolve_lua_same_module_member(
        self, call: CallSite, cache_key: tuple[str, str, bool] | None
    ) -> tuple[bool, tuple[str, str] | None]:
        if result := self.lua_same_module_member(
            call.call_name, call.module_qn, call.language
        ):
            self._host._remember_cacheable(cache_key, result)
            return True, result
        return False, None

    def lua_same_module_member(
        self, call_name: str, module_qn: str, language: cs.SupportedLanguage | None
    ) -> tuple[str, str] | None:
        if language != cs.SupportedLanguage.LUA:
            return None
        if (split := lua_utils.split_member_call(call_name)) is None:
            return None
        table_path, separator, member = split
        return self._lua_table_member(
            f"{module_qn}{cs.SEPARATOR_DOT}{table_path}", member, separator, call_name
        )

    def resolve_lua_module_member(
        self,
        imported_qn: str,
        alias: str,
        member: str,
        separator: str,
        call_name: str,
        language: cs.SupportedLanguage | None,
    ) -> tuple[bool, tuple[str, str] | None]:
        # Decided for every Lua call through a `require` alias: the table the
        # module returns is all the alias binds.
        if language != cs.SupportedLanguage.LUA:
            return False, None
        return True, self._try_resolve_lua_module_member(
            imported_qn, alias, member, separator, call_name
        )

    def _try_resolve_lua_module_member(
        self,
        imported_qn: str,
        alias: str,
        member: str,
        separator: str,
        call_name: str,
    ) -> tuple[str, str] | None:
        # `require` binds the table a module returns. The module usually names
        # it after the alias (`local Storage = {}` in storage/Storage.lua);
        # otherwise (`local M = {}`) the package-member search finds the one
        # table holding the member. Both spellings throughout: `util:method()`
        # reaches `M:method`, `util.add()` reaches `M.add` (issue #2481).
        for owner_qn in (f"{imported_qn}{cs.SEPARATOR_DOT}{alias}", imported_qn):
            if result := self._lua_table_member(owner_qn, member, separator, call_name):
                return result
        return self._host._try_resolve_package_member(
            imported_qn, member, cs.SupportedLanguage.LUA
        )

    def _lua_table_member(
        self, owner_qn: str, member: str, separator: str, call_name: str
    ) -> tuple[str, str] | None:
        for qn in lua_utils.member_spellings(owner_qn, member, separator):
            if qn in self.function_registry:
                logger.debug(ls.CALL_LUA_TABLE_MEMBER, call_name=call_name, qn=qn)
                return self.function_registry[qn], qn
        return None
