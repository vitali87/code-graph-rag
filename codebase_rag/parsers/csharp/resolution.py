"""C#'s share of the name-based callee resolution: a qualified path read the
way C# reads it, through the namespaces and `using` directives of the file it
is written in, the type a `using` alias in a `new` names, and among
same-named types the one C# name lookup reaches first.

`CallResolver` owns what every language shares (the order the stages run in,
the resolution cache and the verdict each answer carries, the simple-name
fallback whose candidates this ranks) and hands C#'s part to the
`CSharpResolver` it builds. A C# member call itself is typed by the C#
engine (`CSharpTypeInferenceEngine`), which the resolver bridges to."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol

if TYPE_CHECKING:
    from ..call_site import CallSite
    from ..import_processor import ImportProcessor
    from ..type_inference import TypeInferenceEngine


def _split_csharp_qualified_path(path: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    generic_depth = 0
    for char in path:
        if char == cs.CHAR_ANGLE_OPEN:
            generic_depth += 1
        elif char == cs.CHAR_ANGLE_CLOSE and generic_depth:
            generic_depth -= 1
        if char == cs.SEPARATOR_DOT and generic_depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


class CSharpResolverHost(Protocol):
    """What C# resolution calls on the `CallResolver` that builds it: the
    inherited-method lookup every language shares, for a member a qualified
    path names on a type that only inherits it."""

    def _resolve_inherited_method(
        self, class_qn: str, method_name: str, own_members: bool = True
    ) -> tuple[str, str] | None: ...


class CSharpResolver:
    """Binds C# qualified paths and ranks same-named C# types by C# lookup.

    Built and owned by `CallResolver`. The registry, the import processor
    (whose `CSharpImporter` records each file's declared namespaces) and the
    type inference engine (whose C# engine reads a type name in its file)
    are the resolver's own, held by reference and read as they stand when a
    call resolves."""

    __slots__ = (
        "_host",
        "function_registry",
        "import_processor",
        "type_inference",
    )

    def __init__(
        self,
        host: CSharpResolverHost,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
    ) -> None:
        self._host = host
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference

    def reset_resolution_memos(self) -> None:
        # The C# chained-receiver memo (issue #1800) is a resolution cache
        # like the rest and expires with them: it is keyed by byte span, and a
        # re-parsed file's spans name different code. Reached through the
        # PRIVATE attribute so a project with no C# does not build an engine
        # just to clear an empty dict.
        if (csharp := self.type_inference._csharp_type_inference) is not None:
            csharp._call_memo.clear()

    def resolve_csharp_method_call(
        self,
        call_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        return self.type_inference.csharp_type_inference.resolve_csharp_method_call(
            call_node, local_var_types, module_qn, caller_qn
        )

    def resolve_csharp_qualified_path(
        self, call: CallSite
    ) -> tuple[bool, tuple[str, str] | None]:
        if call.language != cs.SupportedLanguage.CSHARP:
            return False, None
        call_name, module_qn = call.call_name, call.module_qn
        if cs.SEPARATOR_DOUBLE_COLON in call_name or cs.SEPARATOR_DOT in call_name:
            result, decided = self._csharp_qualified_lookup(
                call_name, module_qn, call.local_var_types
            )
            if result is not None:
                return True, result
            if decided or cs.SEPARATOR_DOUBLE_COLON in call_name:
                return True, None
        # `new W(1)` under `using W = Zeta.Widget;` names no registered type
        # by its written name, so the trie has no `W` to offer; the type
        # lookup expands the alias to its target (issue #2002).
        if call.constructing and (
            type_qn := self.type_inference.csharp_type_inference._type_name_to_qn(
                call_name, module_qn
            )
        ):
            return True, (self.function_registry[type_qn], type_qn)
        return False, None

    def resolve_csharp_qualified_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        language: cs.SupportedLanguage | None,
    ) -> tuple[bool, tuple[str, str] | None]:
        # Decided once the path's C# reading binds it or rules it out; a
        # path it cannot place is left to the probes after it.
        if language != cs.SupportedLanguage.CSHARP:
            return False, None
        result, decided = self._csharp_qualified_lookup(
            call_name, module_qn, local_var_types
        )
        return result is not None or decided, result

    def csharp_qualified_path_binds(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        language: cs.SupportedLanguage | None,
    ) -> bool:
        # A path C#'s qualified reading binds starts at a namespace or a
        # type, so its receiver is never a value.
        if language != cs.SupportedLanguage.CSHARP:
            return False
        result, _ = self._csharp_qualified_lookup(call_name, module_qn, local_var_types)
        return result is not None

    def _csharp_dotted_path_shadowed(
        self, call_name: str, local_var_types: dict[str, str] | None
    ) -> bool:
        return bool(
            local_var_types
            and cs.SEPARATOR_DOT in call_name
            and cs.SEPARATOR_DOUBLE_COLON not in call_name
            and not call_name.startswith(f"global{cs.SEPARATOR_DOT}")
            and call_name.split(cs.SEPARATOR_DOT, 1)[0] in local_var_types
        )

    def _csharp_qualified_lookup(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> tuple[tuple[str, str] | None, bool]:
        if self._csharp_dotted_path_shadowed(call_name, local_var_types):
            return None, False
        path = call_name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
        global_prefix = f"global{cs.SEPARATOR_DOT}"
        is_global = path.startswith(global_prefix)
        if path.startswith(global_prefix):
            path = path[len(global_prefix) :]
        parts = _split_csharp_qualified_path(path)
        if len(parts) < 2 or any(not part for part in parts):
            return None, False

        type_inference = self.type_inference.csharp_type_inference
        for cut in range(len(parts), 0, -1):
            type_path = cs.SEPARATOR_DOT.join(parts[:cut])
            if is_global:
                type_path = f"global{cs.SEPARATOR_DOUBLE_COLON}{type_path}"
            class_qn = type_inference._qualified_type_name_to_qn(type_path, module_qn)
            if class_qn is None:
                continue
            if cut == len(parts):
                return (cs.NodeLabel.CLASS, class_qn), True
            # A namespace-qualified type owns the path even when the member is
            # missing; an unqualified value receiver may still be a field chain.
            return self._csharp_type_member(class_qn, parts[cut:]), True
        if (
            not is_global
            and cs.SEPARATOR_DOUBLE_COLON not in call_name
            and self._csharp_path_starts_with_imported_namespace(parts, module_qn)
        ):
            return None, True
        return None, False

    def _csharp_type_member(
        self, class_qn: str, member_parts: list[str]
    ) -> tuple[str, str] | None:
        # The member a qualified path names on a resolved type: a registered
        # method directly, else a single-segment name inherited from a base.
        member_name = cs.SEPARATOR_DOT.join(member_parts).split(cs.CHAR_ANGLE_OPEN, 1)[
            0
        ]
        method_qn = f"{class_qn}{cs.SEPARATOR_DOT}{member_name}"
        if method_qn in self.function_registry:
            return self.function_registry[method_qn], method_qn
        if cs.SEPARATOR_DOT not in member_name:
            return self._host._resolve_inherited_method(class_qn, member_name)
        return None

    def _csharp_path_starts_with_imported_namespace(
        self, parts: list[str], module_qn: str
    ) -> bool:
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        namespaces = {
            namespace
            for module_namespaces in self.import_processor.csharp.csharp_module_namespaces.values()
            for namespace in module_namespaces
        }
        for cut in range(len(parts), 0, -1):
            prefix = cs.SEPARATOR_DOT.join(parts[:cut])
            local_name, separator, remainder = prefix.partition(cs.SEPARATOR_DOT)
            imported = import_map.get(local_name)
            if imported is None:
                continue
            expanded = f"{imported}{separator}{remainder}" if separator else imported
            if expanded in namespaces:
                return True
        return False

    def csharp_prefer_imported(
        self,
        candidates: list[str],
        module_qn: str,
        call_name: str,
        language: cs.SupportedLanguage | None,
    ) -> list[str]:
        # Same-named C# types in two namespaces tied on import distance and
        # broke on qn order, so `using Zeta;` + `new Widget()` bound
        # `Other.Widget` (issue #2001). C# name lookup searches the caller's
        # own namespace, innermost first, before any `using`, so a candidate
        # there wins outright; otherwise the candidates whose
        # owning type sits DIRECTLY in a namespace (or, for `using static`,
        # a type) the file imports: C# does not import nested namespaces.
        if language != cs.SupportedLanguage.CSHARP or len(candidates) <= 1:
            return candidates
        containers, import_keys, bare_members = self._csharp_import_keys(
            candidates, call_name
        )
        enclosing = self._csharp_enclosing_namespaces(module_qn)
        # A namespace holds types, not members: the caller's namespace makes
        # `App.Widget` nameable, never `App.Widget.S` callable bare. Only a
        # `using static` does that, which the import preference below checks
        # .
        local = [
            qn
            for qn in candidates
            if containers.get(qn) in enclosing and qn not in bare_members
        ]
        if local:
            innermost = max(len(containers[qn]) for qn in local)
            return [qn for qn in local if len(containers[qn]) == innermost]
        # Only a directive whose local name is its target's own last segment
        # makes names available unqualified (`using Zeta;`, `using static
        # Zeta.Widget;`); an alias (`using Alias = Other;`) does not.
        imported = {
            target
            for local_name, target in self.import_processor.import_mapping.get(
                module_qn, {}
            ).items()
            if local_name == target.rpartition(cs.SEPARATOR_DOT)[2]
        }
        preferred = [qn for qn in candidates if import_keys.get(qn) in imported]
        return preferred or candidates

    def _csharp_import_keys(
        self, candidates: list[str], call_name: str
    ) -> tuple[dict[str, str], dict[str, str], set[str]]:
        """(candidate -> namespace holding its type, candidate -> what an
         import must name to make it available, the bare-called members).

         A type, or a member reached through its type (`Widget.S()`), is
         imported by a namespace directive naming the type's namespace; a BARE
         member (`S()`) only by a `using static` naming its declaring type
        .
        """
        declared = self.type_inference.facts.csharp_class_namespaced
        bare = cs.SEPARATOR_DOT not in call_name
        containers: dict[str, str] = {}
        import_keys: dict[str, str] = {}
        bare_members: set[str] = set()
        for qn in candidates:
            owner = qn
            while owner and owner not in declared:
                owner = owner.rpartition(cs.SEPARATOR_DOT)[0]
            if not owner:
                continue
            containers[qn] = declared[owner].rpartition(cs.SEPARATOR_DOT)[0]
            if bare and owner != qn:
                bare_members.add(qn)
            import_keys[qn] = declared[owner] if qn in bare_members else containers[qn]
        return containers, import_keys, bare_members

    def _csharp_enclosing_namespaces(self, module_qn: str) -> set[str]:
        """Every namespace enclosing the file's call sites, innermost included.

         Only known when the file declares ONE nested chain; with sibling
         namespaces (`App` and `Other` in one file) neither is local to every
         call site, so no local precedence applies rather than a guessed one
        .
        """
        declared_here = sorted(
            self.import_processor.csharp.csharp_module_namespaces.get(module_qn, {}),
            key=len,
        )
        if not declared_here or not all(
            declared_here[-1] == ns
            or declared_here[-1].startswith(f"{ns}{cs.SEPARATOR_DOT}")
            for ns in declared_here
        ):
            return set()
        parts = declared_here[-1].split(cs.SEPARATOR_DOT)
        return {cs.SEPARATOR_DOT.join(parts[:cut]) for cut in range(1, len(parts) + 1)}
