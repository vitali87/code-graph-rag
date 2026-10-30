"""Julia's share of the name-based callee resolution: the module scopes a
caller's imports bind in, where a nested `module` block's own `using`
shadows the file's, and the member a selected import (`using .api: alpha`)
names in its source module.

`CallResolver` owns what every language shares (the order the stages run in,
the resolution cache and the verdict each answer carries, the direct-import
probe each scope is asked through) and hands Julia's part to the
`JuliaResolver` it builds and the function here."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...types_defs import FunctionRegistryTrieProtocol
from ..resolver_host import ResolverHost

if TYPE_CHECKING:
    from ..call_site import CallSite
    from ..import_processor import ImportProcessor


def julia_caller_dependent(call: CallSite) -> bool:
    # A Julia caller inside a nested `module` block: the block's own `using`
    # shadows the file-level bindings, so the caller's answer is its own.
    module_qn, caller_qn = call.module_qn, call.caller_qn
    return (
        call.language == cs.SupportedLanguage.JULIA
        and caller_qn is not None
        and caller_qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}")
        and cs.SEPARATOR_DOT in caller_qn[len(module_qn) + 1 :]
    )


def _julia_caller_scopes(caller_qn: str, module_qn: str) -> list[str]:
    # The caller's module scopes innermost first (the file module last).
    if not caller_qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}"):
        return [module_qn]
    parts = caller_qn[len(module_qn) + 1 :].split(cs.SEPARATOR_DOT)
    scopes = [module_qn]
    prefix = module_qn
    for part in parts[:-1]:
        prefix = f"{prefix}{cs.SEPARATOR_DOT}{part}"
        scopes.insert(0, prefix)
    return scopes


class JuliaResolver:
    """Binds Julia calls through the `using`s of the scopes they are made in.

    Built and owned by `CallResolver`. The registry and the import processor
    (whose import map is keyed by module scope, nested `module` blocks
    included, and whose `JuliaImporter` knows each file's declared modules)
    are the resolver's own, held by reference and read as they stand when a
    call resolves."""

    __slots__ = ("_host", "function_registry", "import_processor")

    def __init__(
        self,
        host: ResolverHost,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
    ) -> None:
        self._host = host
        self.function_registry = function_registry
        self.import_processor = import_processor

    def resolve_julia_scoped_import(
        self,
        call_name: str,
        module_qn: str,
        language: cs.SupportedLanguage | None,
        caller_qn: str | None,
    ) -> tuple[str, str] | None:
        if language != cs.SupportedLanguage.JULIA or not caller_qn:
            return None
        # A `using` inside a Julia `module` block binds only that block,
        # so a nested module's imports shadow the file-level ones: walk
        # the caller's module scopes innermost first.
        for scope in _julia_caller_scopes(caller_qn, module_qn):
            import_map = self.import_processor.import_mapping.get(scope)
            if not import_map:
                continue
            if result := self._host.try_resolve_direct_import(
                call_name, import_map, language, scope
            ):
                return result
        return None

    def julia_selected_import_member(
        self, imported_qn: str, call_name: str
    ) -> tuple[str, str] | None:
        # A Julia selected import (`using .api: alpha`) binds the local
        # name to the source MODULE; resolve the member exactly before
        # the name trie, or a same-named function soaks the edge.
        for member in self.import_processor.julia.julia_member_qn_candidates(
            imported_qn, call_name
        ):
            if member in self.function_registry:
                logger.debug(ls.CALL_DIRECT_IMPORT, call_name=call_name, qn=member)
                return self.function_registry[member], member
        return None
