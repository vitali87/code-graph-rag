from __future__ import annotations

import re
from collections import defaultdict, deque

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..types_defs import FunctionRegistryTrieProtocol, NodeType
from .import_processor import ImportProcessor
from .py import resolve_class_name
from .type_inference import TypeInferenceEngine

_SEPARATOR_PATTERN = re.compile(r"[.:]|::")
_SEARCH_NAME_CACHE: dict[str, str] = {}
_CHAINED_METHOD_PATTERN = re.compile(r"\.([^.()]+)$")
_QN_SPLIT_CACHE: dict[str, tuple[list[str], int]] = {}


class CallResolver:
    __slots__ = (
        "function_registry",
        "import_processor",
        "type_inference",
        "class_inheritance",
        "type_aliases",
        "_simple_resolution_cache",
        "_wildcard_cache",
        "_protocol_impl_cache",
        "_field_bindings",
        "_field_to_classes",
        "_subclass_map_cache",
        "_protocol_classes_cache",
        "_struct_impl_cache",
    )

    def __init__(
        self,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        class_inheritance: dict[str, list[str]],
        type_aliases: dict[str, str] | None = None,
    ) -> None:
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        self.class_inheritance = class_inheritance
        # (H) C++ typedef/using alias -> underlying bare type, consulted when a
        # (H) receiver type name is mapped to a class (empty for other languages).
        self.type_aliases = type_aliases if type_aliases is not None else {}
        self._simple_resolution_cache: dict[
            tuple[str, str], tuple[str, str] | None
        ] = {}
        self._wildcard_cache: dict[int, list[tuple[str, str]]] = {}
        self._protocol_impl_cache: dict[str, str] | None = None
        self._field_bindings: dict[tuple[str, str], set[str]] = {}
        self._field_to_classes: dict[str, set[str]] = {}
        self._subclass_map_cache: dict[str, set[str]] | None = None
        self._protocol_classes_cache: set[str] | None = None
        self._struct_impl_cache: dict[str, set[str]] = {}

    def record_callable_field_binding(
        self, class_qn: str, field: str, func_qn: str
    ) -> None:
        # (H) A NamedTuple/dataclass field holding a function reference: every
        # (H) function bound to it at any construction site is a possible callee
        # (H) when the field is invoked. Recording all of them is a sound call
        # (H) graph (each runs for its own configuration), so recall is complete.
        self._field_bindings.setdefault((class_qn, field), set()).add(func_qn)
        self._field_to_classes.setdefault(field, set()).add(class_qn)

    def callable_field_targets(
        self, field: str, recv_type: str | None = None
    ) -> set[str]:
        classes = self._field_to_classes.get(field)
        if not classes:
            return set()
        if recv_type:
            simple = recv_type.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            matched = [
                qn
                for qn in classes
                if qn == recv_type or qn.rsplit(cs.SEPARATOR_DOT, 1)[-1] == simple
            ]
            if len(matched) == 1:
                return self._field_bindings.get((matched[0], field), set())
        # (H) Receiver type unknown or ambiguous: only resolve when exactly one
        # (H) class declares this callable field, so the targets are unambiguous.
        if len(classes) == 1:
            return self._field_bindings.get((next(iter(classes)), field), set())
        return set()

    def _resolve_class_qn_from_type(
        self, var_type: str, import_map: dict[str, str], module_qn: str
    ) -> str:
        var_type = self._strip_optional(var_type)
        if cs.SEPARATOR_DOT in var_type:
            return self._follow_reexports(var_type)
        if var_type in import_map:
            return self._follow_reexports(import_map[var_type])
        return self._resolve_class_name(var_type, module_qn) or ""

    def _strip_optional(self, var_type: str) -> str:
        # (H) An Optional annotation (X | None) names a single concrete class; reduce it
        # (H) so attribute/operator resolution can find that class. Genuine multi-type
        # (H) unions stay unresolved (ambiguous).
        if cs.PY_UNION_SEPARATOR not in var_type:
            return var_type
        non_none = [
            member
            for part in var_type.split(cs.PY_UNION_SEPARATOR)
            if (member := part.strip()) and member != cs.PY_NONE
        ]
        return non_none[0] if len(non_none) == 1 else var_type

    def _follow_reexports(self, class_qn: str) -> str:
        # (H) `from .pkg import Cls` records the importer's name against the re-export
        # (H) module (pkg.Cls), not the class's real definition (pkg.mod.Cls), so a
        # (H) class_qn that is not itself registered may be a re-export. Follow the
        # (H) module's own import map one hop at a time until a registered class is
        # (H) reached, guarding against cycles.
        seen: set[str] = set()
        current = class_qn
        while (
            current
            and current not in seen
            and current not in self.function_registry
            and cs.SEPARATOR_DOT in current
        ):
            seen.add(current)
            module_qn, _, name = current.rpartition(cs.SEPARATOR_DOT)
            following = self.import_processor.import_mapping.get(module_qn, {}).get(
                name
            )
            if not following or following == current:
                break
            current = following
        return current

    def _try_resolve_method(
        self, class_qn: str, method_name: str, separator: str = cs.SEPARATOR_DOT
    ) -> tuple[str, str] | None:
        method_qn = f"{class_qn}{separator}{method_name}"
        if method_qn in self.function_registry:
            return self.function_registry[method_qn], method_qn
        return self._resolve_inherited_method(class_qn, method_name)

    def resolve_function_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
    ) -> tuple[str, str] | None:
        return self._redirect_protocol_method(
            self._resolve_function_call(
                call_name, module_qn, local_var_types, class_context
            )
        )

    def _protocol_impl_map(self) -> dict[str, str]:
        # (H) A Protocol stub never runs; the concrete implementer does. Map each
        # (H) XxxProtocol to a unique non-Protocol class named Xxx (the suffix
        # (H) convention disambiguates the real impl from test mocks or other
        # (H) structural conformers, which structural matching alone cannot).
        if self._protocol_impl_cache is not None:
            return self._protocol_impl_cache
        sep = cs.SEPARATOR_DOT
        protocols: set[str] = set()
        classes_by_simple: dict[str, list[str]] = defaultdict(list)
        for qn, bases in self.class_inheritance.items():
            classes_by_simple[qn.rsplit(sep, 1)[-1]].append(qn)
            if any(base.rsplit(sep, 1)[-1] == cs.PY_PROTOCOL for base in bases):
                protocols.add(qn)
        impl: dict[str, str] = {}
        for protocol_qn in protocols:
            simple = protocol_qn.rsplit(sep, 1)[-1]
            if simple == cs.PY_PROTOCOL or not simple.endswith(cs.PY_PROTOCOL):
                continue
            base_name = simple[: -len(cs.PY_PROTOCOL)]
            candidates = [
                qn for qn in classes_by_simple.get(base_name, []) if qn not in protocols
            ]
            if len(candidates) == 1:
                impl[protocol_qn] = candidates[0]
        self._protocol_impl_cache = impl
        return impl

    def _protocol_classes(self) -> set[str]:
        if self._protocol_classes_cache is None:
            sep = cs.SEPARATOR_DOT
            self._protocol_classes_cache = {
                qn
                for qn, bases in self.class_inheritance.items()
                if any(base.rsplit(sep, 1)[-1] == cs.PY_PROTOCOL for base in bases)
            }
        return self._protocol_classes_cache

    def protocol_dispatch_targets(self, callee_qn: str) -> set[tuple[str, str]]:
        # (H) A call resolved to a Protocol stub method (P.M) never runs the stub: the
        # (H) runtime receiver is some conformer, so the sound call graph emits an edge
        # (H) to M on every non-Protocol class that defines it. Gating on the resolved
        # (H) target being a Protocol method keeps this from firing on ordinary calls.
        class_qn, sep, method_name = callee_qn.rpartition(cs.SEPARATOR_DOT)
        if not sep or class_qn not in self._protocol_classes():
            return set()
        protocols = self._protocol_classes()
        targets: set[tuple[str, str]] = set()
        for qn in self.function_registry.find_ending_with(method_name):
            definer, dot, name = qn.rpartition(cs.SEPARATOR_DOT)
            if dot and name == method_name and definer not in protocols:
                targets.add((self.function_registry[qn], qn))
        return targets

    def _redirect_protocol_method(
        self, result: tuple[str, str] | None
    ) -> tuple[str, str] | None:
        if result is None:
            return result
        class_qn, sep, method_name = result[1].rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return result
        impl_qn = self._protocol_impl_map().get(class_qn)
        if impl_qn is None:
            return result
        redirected = f"{impl_qn}{cs.SEPARATOR_DOT}{method_name}"
        if redirected in self.function_registry:
            return self.function_registry[redirected], redirected
        return result

    def _resolve_function_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
    ) -> tuple[str, str] | None:
        use_cache = not local_var_types
        if use_cache:
            cache_key = (call_name, module_qn)
            if cache_key in self._simple_resolution_cache:
                return self._simple_resolution_cache[cache_key]

        if result := self._try_resolve_iife(call_name, module_qn):
            return result

        if self._is_super_call(call_name):
            return self._resolve_super_call(call_name, class_context)

        if cs.SEPARATOR_DOT in call_name and self._is_method_chain(call_name):
            return self._resolve_chained_call(call_name, module_qn, local_var_types)

        if result := self._try_resolve_via_imports(
            call_name, module_qn, local_var_types
        ):
            if use_cache:
                self._simple_resolution_cache[cache_key] = result
            return result

        if result := self._try_resolve_same_module(call_name, module_qn):
            if use_cache:
                self._simple_resolution_cache[cache_key] = result
            return result

        if class_context and (
            result := self._resolve_self_sibling_method(call_name, class_context)
        ):
            return result

        # (H) A bare name explicitly imported from outside the project binds to that
        # (H) external symbol. Since precise import / same-module resolution above
        # (H) already failed, the symbol is unindexed; do NOT let the simple-name
        # (H) trie fallback rebind it to an unrelated first-party symbol of the same
        # (H) name. (The instantiation eval caught `from evals import GraphData;
        # (H) GraphData()` being resolved to codebase_rag's own GraphData class.)
        if cs.SEPARATOR_DOT not in call_name and self._is_external_import(
            call_name, module_qn
        ):
            if use_cache:
                self._simple_resolution_cache[cache_key] = None
            return None

        # (H) A member call `obj.method` whose receiver has a KNOWN inferred type that is
        # (H) not a first-party class is a call on an external object (e.g. a
        # (H) `std::string`). Precise local-type resolution above already failed, so the
        # (H) method lives on the external type; do NOT let the simple-name trie fallback
        # (H) rebind it to an unrelated first-party method of the same name. Untyped
        # (H) receivers keep the fallback (their type is unknown, not known-external).
        if self._receiver_type_is_external(call_name, module_qn, local_var_types):
            if use_cache:
                self._simple_resolution_cache[cache_key] = None
            return None

        result = self._try_resolve_via_trie(call_name, module_qn)
        if use_cache:
            self._simple_resolution_cache[cache_key] = result
        return result

    def _is_external_import(self, call_name: str, module_qn: str) -> bool:
        # (H) True when call_name is imported in module_qn from a module outside the
        # (H) project. First-party imports are written either project-prefixed
        # (H) (`from proj.w import X`) or bare (`from utils.helpers import X`, where
        # (H) the registered node is `proj.utils.helpers.X`); both are first-party
        # (H) and left to the trie fallback. Only a target that is neither rooted at
        # (H) the project nor registered under the project prefix is external, so
        # (H) this suppresses cross-project fuzzy rebinds without dropping real
        # (H) first-party calls.
        import_map = self.import_processor.import_mapping.get(module_qn)
        if not import_map:
            return False
        target = import_map.get(call_name)
        if not target:
            return False
        # (H) A PHP `use function A\B\c` target is a namespace path, which never
        # (H) matches cgr's file-path qualified name (a global helper declares
        # (H) `namespace Illuminate\Support` from Collections/functions.php). Treating
        # (H) it as external would suppress the simple-name trie fallback that a bare
        # (H) PHP call already relies on, dropping the call; leave it to the trie.
        # (H) LIMITATION: cgr qualifies PHP functions by file path and does not track
        # (H) the `namespace` declaration anywhere, so a genuinely external
        # (H) `use function Vendor\pkg\helper` cannot be told apart from a
        # (H) path-mismatched first-party one; both defer to the trie, exactly as a
        # (H) bare `helper()` call already does. Precise first-party-vs-external
        # (H) disambiguation would require systemic PHP namespace tracking.
        php_imports = self.import_processor.php_function_imports.get(module_qn)
        if php_imports and call_name in php_imports:
            return False
        # (H) Only dotted absolute-path imports (Python/Java `pkg.mod.Name`) are
        # (H) judged here. Rust/C++ record relative or `::`-separated targets
        # (H) (`super::b::helper`) that never carry the project prefix and rely on
        # (H) the trie fallback to resolve, so they must not be mistaken external.
        if cs.SEPARATOR_DOT not in target or cs.SEPARATOR_DOUBLE_COLON in target:
            return False
        project_root = module_qn.split(cs.SEPARATOR_DOT, 1)[0]
        if target.split(cs.SEPARATOR_DOT, 1)[0] == project_root:
            return False
        return f"{project_root}{cs.SEPARATOR_DOT}{target}" not in self.function_registry

    def _receiver_type_is_external(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> bool:
        # (H) True only for a two-part dotted member call `obj.method` whose `obj` has an
        # (H) inferred local type that is known to be external. The receiver type is
        # (H) external when it resolves to nothing, or to a qn that is neither registered
        # (H) nor rooted at the project (a `std::string` -> `std.string`). In that case
        # (H) the method lives on the external type, so the simple-name trie fallback must
        # (H) not rebind it to a same-named first-party method. An untyped receiver (obj
        # (H) absent from the map) or a project-rooted type is left alone: its method may
        # (H) still be resolved by the fallback (e.g. a cross-file imported-class call the
        # (H) precise path missed), so only a provably external type is suppressed.
        if not local_var_types or cs.SEPARATOR_DOT not in call_name:
            return False
        parts = call_name.split(cs.SEPARATOR_DOT)
        if len(parts) != 2:
            return False
        var_type = local_var_types.get(parts[0])
        if var_type is None:
            return False
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        class_qn = self._resolve_class_qn_from_type(var_type, import_map, module_qn)
        if not class_qn:
            return True
        # (H) First-party class qns may be written without the project prefix (a bare
        # (H) `from models.user import User` resolves to `models.user.User` while the
        # (H) registry stores `proj.models.user.User`), so check both the qn as-is and
        # (H) the project-prefixed form before judging a type external -- mirrors
        # (H) _is_external_import. A project-rooted qn is always treated as first-party.
        project_root = module_qn.split(cs.SEPARATOR_DOT, 1)[0]
        if class_qn.split(cs.SEPARATOR_DOT, 1)[0] == project_root:
            return False
        return (
            class_qn not in self.function_registry
            and f"{project_root}{cs.SEPARATOR_DOT}{class_qn}"
            not in self.function_registry
        )

    def _try_resolve_iife(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        if not call_name:
            return None
        if not (
            call_name.startswith(cs.IIFE_FUNC_PREFIX)
            or call_name.startswith(cs.IIFE_ARROW_PREFIX)
        ):
            return None
        iife_qn = f"{module_qn}.{call_name}"
        if iife_qn in self.function_registry:
            return self.function_registry[iife_qn], iife_qn
        return None

    def _is_super_call(self, call_name: str) -> bool:
        return (
            call_name == cs.KEYWORD_SUPER
            or call_name.startswith(f"{cs.KEYWORD_SUPER}.")
            or call_name.startswith(f"{cs.KEYWORD_SUPER}()")
        )

    def _try_resolve_via_imports(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        if module_qn not in self.import_processor.import_mapping:
            return None

        import_map = self.import_processor.import_mapping[module_qn]

        if result := self._try_resolve_direct_import(call_name, import_map):
            return result

        if result := self._try_resolve_qualified_call(
            call_name, import_map, module_qn, local_var_types
        ):
            return result

        return self._try_resolve_wildcard_imports(call_name, import_map)

    def _try_resolve_direct_import(
        self, call_name: str, import_map: dict[str, str]
    ) -> tuple[str, str] | None:
        if call_name not in import_map:
            return None
        imported_qn = import_map[call_name]
        if imported_qn in self.function_registry:
            logger.debug(ls.CALL_DIRECT_IMPORT, call_name=call_name, qn=imported_qn)
            return self.function_registry[imported_qn], imported_qn
        return None

    def _try_resolve_qualified_call(
        self,
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        if cs.SEPARATOR_DOUBLE_COLON in call_name:
            separator = cs.SEPARATOR_DOUBLE_COLON
        elif cs.SEPARATOR_COLON in call_name:
            separator = cs.SEPARATOR_COLON
        elif cs.SEPARATOR_DOT in call_name:
            separator = cs.SEPARATOR_DOT
        else:
            return None

        parts = call_name.split(separator)

        if len(parts) == 2:
            if result := self._resolve_two_part_call(
                parts, call_name, separator, import_map, module_qn, local_var_types
            ):
                return result

        if len(parts) >= 3 and parts[0] == cs.KEYWORD_SELF:
            return self._resolve_self_attribute_call(
                parts, call_name, import_map, module_qn, local_var_types
            )

        return self._resolve_multi_part_call(
            parts, call_name, import_map, module_qn, local_var_types
        )

    def _has_separator(self, call_name: str) -> bool:
        return (
            cs.SEPARATOR_DOT in call_name
            or cs.SEPARATOR_DOUBLE_COLON in call_name
            or cs.SEPARATOR_COLON in call_name
        )

    def _get_separator(self, call_name: str) -> str:
        if cs.SEPARATOR_DOUBLE_COLON in call_name:
            return cs.SEPARATOR_DOUBLE_COLON
        if cs.SEPARATOR_COLON in call_name:
            return cs.SEPARATOR_COLON
        return cs.SEPARATOR_DOT

    def _try_resolve_wildcard_imports(
        self, call_name: str, import_map: dict[str, str]
    ) -> tuple[str, str] | None:
        map_id = id(import_map)
        if map_id not in self._wildcard_cache:
            self._wildcard_cache[map_id] = (
                [(k, v) for k, v in import_map.items() if k[0] == "*"]
                if import_map
                else []
            )
        wildcards = self._wildcard_cache[map_id]
        if not wildcards:
            return None
        for _, imported_qn in wildcards:
            if result := self._try_wildcard_qns(call_name, imported_qn):
                return result
        return None

    def _try_wildcard_qns(
        self, call_name: str, imported_qn: str
    ) -> tuple[str, str] | None:
        potential_qns = []
        if cs.SEPARATOR_DOUBLE_COLON not in imported_qn:
            potential_qns.append(f"{imported_qn}.{call_name}")
        potential_qns.append(f"{imported_qn}{cs.SEPARATOR_DOUBLE_COLON}{call_name}")

        for wildcard_qn in potential_qns:
            if wildcard_qn in self.function_registry:
                logger.debug(ls.CALL_WILDCARD, call_name=call_name, qn=wildcard_qn)
                return self.function_registry[wildcard_qn], wildcard_qn
        return None

    def _try_resolve_same_module(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        same_module_func_qn = f"{module_qn}.{call_name}"
        if same_module_func_qn in self.function_registry:
            logger.debug(
                ls.CALL_SAME_MODULE, call_name=call_name, qn=same_module_func_qn
            )
            return self.function_registry[same_module_func_qn], same_module_func_qn
        return None

    def _try_resolve_via_trie(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        search_name = _SEARCH_NAME_CACHE.get(call_name)
        if search_name is None:
            search_name = _SEPARATOR_PATTERN.split(call_name)[-1]
            _SEARCH_NAME_CACHE[call_name] = search_name
        possible_matches = self.function_registry.find_ending_with(search_name)
        if not possible_matches:
            logger.debug(ls.CALL_UNRESOLVED, call_name=call_name)
            return None

        if len(possible_matches) == 1:
            best_candidate_qn = possible_matches[0]
        else:
            caller_parts = module_qn.split(cs.SEPARATOR_DOT)
            caller_len = len(caller_parts)
            caller_parent_prefix = (
                cs.SEPARATOR_DOT.join(caller_parts[:-1]) + cs.SEPARATOR_DOT
                if caller_len > 1
                else ""
            )
            best_candidate_qn = min(
                possible_matches,
                key=lambda qn: (
                    # (H) An @abstractmethod stub never runs when a concrete override
                    # (H) exists, so prefer concrete candidates over abstract ones
                    # (H) even when the abstract stub is closer by import distance.
                    self.function_registry.is_abstract(qn),
                    self._import_distance_fast(
                        qn, caller_parts, caller_len, caller_parent_prefix
                    ),
                    qn,
                ),
            )
        logger.debug(ls.CALL_TRIE_FALLBACK, call_name=call_name, qn=best_candidate_qn)
        return self.function_registry[best_candidate_qn], best_candidate_qn

    def _resolve_two_part_call(
        self,
        parts: list[str],
        call_name: str,
        separator: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        object_name, method_name = parts

        if result := self._try_resolve_via_local_type(
            object_name,
            method_name,
            separator,
            call_name,
            import_map,
            module_qn,
            local_var_types,
        ):
            return result

        if result := self._try_resolve_via_import(
            object_name, method_name, separator, call_name, import_map
        ):
            return result

        return self._try_resolve_module_method(method_name, call_name, module_qn)

    def _try_resolve_via_local_type(
        self,
        object_name: str,
        method_name: str,
        separator: str,
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        if not local_var_types or object_name not in local_var_types:
            return None

        var_type = local_var_types[object_name]

        if class_qn := self._resolve_class_qn_from_type(
            var_type, import_map, module_qn
        ):
            if result := self._try_method_on_class(
                class_qn, method_name, separator, call_name, object_name, var_type
            ):
                return result

        if var_type in cs.JS_BUILTIN_TYPES:
            return (
                cs.NodeLabel.FUNCTION,
                f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}{var_type}{cs.SEPARATOR_PROTOTYPE}{method_name}",
            )
        return None

    def _try_method_on_class(
        self,
        class_qn: str,
        method_name: str,
        separator: str,
        call_name: str,
        object_name: str,
        var_type: str,
    ) -> tuple[str, str] | None:
        method_qn = f"{class_qn}{separator}{method_name}"
        if method_qn in self.function_registry:
            logger.debug(
                ls.CALL_TYPE_INFERRED,
                call_name=call_name,
                method_qn=method_qn,
                obj=object_name,
                var_type=var_type,
            )
            return self.function_registry[method_qn], method_qn

        if inherited := self._resolve_inherited_method(class_qn, method_name):
            logger.debug(
                ls.CALL_TYPE_INFERRED_INHERITED,
                call_name=call_name,
                method_qn=inherited[1],
                obj=object_name,
                var_type=var_type,
            )
            return inherited
        return None

    def _try_resolve_via_import(
        self,
        object_name: str,
        method_name: str,
        separator: str,
        call_name: str,
        import_map: dict[str, str],
    ) -> tuple[str, str] | None:
        if object_name not in import_map:
            return None

        class_qn = self._resolve_imported_class_qn(
            import_map[object_name], object_name, method_name, separator
        )

        registry_separator = (
            separator if separator == cs.SEPARATOR_COLON else cs.SEPARATOR_DOT
        )
        method_qn = f"{class_qn}{registry_separator}{method_name}"

        if method_qn in self.function_registry:
            logger.debug(
                ls.CALL_IMPORT_STATIC, call_name=call_name, method_qn=method_qn
            )
            return self.function_registry[method_qn], method_qn
        return None

    def _resolve_imported_class_qn(
        self,
        class_qn: str,
        object_name: str,
        method_name: str,
        separator: str,
    ) -> str:
        if cs.SEPARATOR_DOUBLE_COLON in class_qn:
            class_qn = self._resolve_rust_class_qn(class_qn)

        potential_class_qn = f"{class_qn}.{object_name}"
        test_method_qn = f"{potential_class_qn}{separator}{method_name}"
        if test_method_qn in self.function_registry:
            return potential_class_qn
        return class_qn

    def _resolve_rust_class_qn(self, class_qn: str) -> str:
        rust_parts = class_qn.split(cs.SEPARATOR_DOUBLE_COLON)
        class_name = rust_parts[-1]

        matching_qns = self.function_registry.find_ending_with(class_name)
        return next(
            (
                qn
                for qn in matching_qns
                if self.function_registry.get(qn) == NodeType.CLASS
            ),
            class_qn,
        )

    def _try_resolve_module_method(
        self, method_name: str, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        method_qn = f"{module_qn}.{method_name}"
        if method_qn in self.function_registry:
            logger.debug(
                ls.CALL_OBJECT_METHOD, call_name=call_name, method_qn=method_qn
            )
            return self.function_registry[method_qn], method_qn
        return None

    def _resolve_self_attribute_call(
        self,
        parts: list[str],
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        attribute_ref = cs.SEPARATOR_DOT.join(parts[:-1])
        method_name = parts[-1]

        if local_var_types and attribute_ref in local_var_types:
            var_type = local_var_types[attribute_ref]
            if class_qn := self._resolve_class_qn_from_type(
                var_type, import_map, module_qn
            ):
                method_qn = f"{class_qn}.{method_name}"
                if method_qn in self.function_registry:
                    logger.debug(
                        ls.CALL_INSTANCE_ATTR,
                        call_name=call_name,
                        method_qn=method_qn,
                        attr_ref=attribute_ref,
                        var_type=var_type,
                    )
                    return self.function_registry[method_qn], method_qn

                if inherited_method := self._resolve_inherited_method(
                    class_qn, method_name
                ):
                    logger.debug(
                        ls.CALL_INSTANCE_ATTR_INHERITED,
                        call_name=call_name,
                        method_qn=inherited_method[1],
                        attr_ref=attribute_ref,
                        var_type=var_type,
                    )
                    return inherited_method

        return None

    def _resolve_multi_part_call(
        self,
        parts: list[str],
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        class_name = parts[0]
        method_name = cs.SEPARATOR_DOT.join(parts[1:])

        if class_name in import_map:
            class_qn = import_map[class_name]
            method_qn = f"{class_qn}.{method_name}"
            if method_qn in self.function_registry:
                logger.debug(
                    ls.CALL_IMPORT_QUALIFIED,
                    call_name=call_name,
                    method_qn=method_qn,
                )
                return self.function_registry[method_qn], method_qn

        if local_var_types and class_name in local_var_types:
            var_type = local_var_types[class_name]
            if class_qn := self._resolve_class_qn_from_type(
                var_type, import_map, module_qn
            ):
                method_qn = f"{class_qn}.{method_name}"
                if method_qn in self.function_registry:
                    logger.debug(
                        ls.CALL_INSTANCE_QUALIFIED,
                        call_name=call_name,
                        method_qn=method_qn,
                        class_name=class_name,
                        var_type=var_type,
                    )
                    return self.function_registry[method_qn], method_qn

                if inherited_method := self._resolve_inherited_method(
                    class_qn, method_name
                ):
                    logger.debug(
                        ls.CALL_INSTANCE_INHERITED,
                        call_name=call_name,
                        method_qn=inherited_method[1],
                        class_name=class_name,
                        var_type=var_type,
                    )
                    return inherited_method

        return None

    def operator_dunder_targets(
        self,
        operand_text: str,
        dunder: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> set[tuple[str, str]]:
        # (H) Operator syntax dispatches to a dunder on the operand's type. Resolve only
        # (H) when the operand type is known; never via the name-only trie fallback, so a
        # (H) builtin container does not borrow a first-party dunder. A Protocol-typed
        # (H) operand dispatches to the dunder on each structural implementer (which may
        # (H) define the dunder even when the Protocol stub does not, e.g. __len__).
        if not local_var_types or not (var_type := local_var_types.get(operand_text)):
            return set()
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        class_qn = self._resolve_class_qn_from_type(var_type, import_map, module_qn)
        if not class_qn:
            return set()
        if class_qn in self._protocol_classes():
            # (H) Naming convention (XxxProtocol -> Xxx) is robust when it applies;
            # (H) structural conformance covers protocols whose implementer is named
            # (H) differently. Union both so neither gap drops a concrete target.
            classes = set(self._protocol_structural_implementers(class_qn))
            if named_impl := self._protocol_impl_map().get(class_qn):
                classes.add(named_impl)
        else:
            classes = {class_qn}
        targets: set[tuple[str, str]] = set()
        for candidate in classes:
            if resolved := self._try_resolve_method(candidate, dunder):
                targets.add(resolved)
        return targets

    def _protocol_structural_implementers(self, protocol_qn: str) -> set[str]:
        # (H) Classes that define every method declared on the Protocol (own or
        # (H) inherited). Used to dispatch operator dunders to the concrete type when the
        # (H) Protocol/implementer names don't follow the XxxProtocol convention.
        if protocol_qn in self._struct_impl_cache:
            return self._struct_impl_cache[protocol_qn]
        sep = cs.SEPARATOR_DOT
        protocol_methods = {
            qn.rsplit(sep, 1)[-1]
            for qn, node_type in self.function_registry.find_with_prefix(protocol_qn)
            if node_type == NodeType.METHOD and qn.rsplit(sep, 1)[0] == protocol_qn
        }
        result: set[str] = set()
        if protocol_methods:
            protocols = self._protocol_classes()
            for candidate in self.class_inheritance:
                if candidate in protocols:
                    continue
                if all(
                    self._try_resolve_method(candidate, method)
                    for method in protocol_methods
                ):
                    result.add(candidate)
        self._struct_impl_cache[protocol_qn] = result
        return result

    def resolve_builtin_call(self, call_name: str) -> tuple[str, str] | None:
        if call_name in cs.JS_BUILTIN_PATTERNS:
            return (cs.NodeLabel.FUNCTION, f"{cs.BUILTIN_PREFIX}.{call_name}")

        for suffix, method in cs.JS_FUNCTION_PROTOTYPE_SUFFIXES.items():
            if call_name.endswith(suffix):
                return (
                    cs.NodeLabel.FUNCTION,
                    f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}Function{cs.SEPARATOR_PROTOTYPE}{method}",
                )

        if cs.SEPARATOR_PROTOTYPE in call_name and (
            call_name.endswith(cs.JS_SUFFIX_CALL)
            or call_name.endswith(cs.JS_SUFFIX_APPLY)
        ):
            base_call = call_name.rsplit(cs.SEPARATOR_DOT, 1)[0]
            return (cs.NodeLabel.FUNCTION, base_call)

        return None

    def resolve_cpp_operator_call(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        if not call_name.startswith(cs.OPERATOR_PREFIX):
            return None

        if call_name in cs.CPP_OPERATORS:
            return (cs.NodeLabel.FUNCTION, cs.CPP_OPERATORS[call_name])

        if possible_matches := self.function_registry.find_ending_with(call_name):
            same_module_ops = [
                qn
                for qn in possible_matches
                if qn.startswith(module_qn) and call_name in qn
            ]
            candidates = same_module_ops or possible_matches
            candidates.sort(key=lambda qn: (len(qn), qn))
            best = candidates[0]
            return (self.function_registry[best], best)

        return None

    def _is_method_chain(self, call_name: str) -> bool:
        if cs.CHAR_PAREN_OPEN not in call_name or cs.CHAR_PAREN_CLOSE not in call_name:
            return False
        parts = call_name.split(cs.SEPARATOR_DOT)
        method_calls = sum(
            cs.CHAR_PAREN_OPEN in part and cs.CHAR_PAREN_CLOSE in part for part in parts
        )
        return method_calls >= 1 and len(parts) >= 2

    def _resolve_chained_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> tuple[str, str] | None:
        match = _CHAINED_METHOD_PATTERN.search(call_name)
        if not match:
            return None

        final_method = match[1]

        object_expr = call_name[: match.start()]

        if (
            object_type
            := self.type_inference.python_type_inference._infer_expression_return_type(
                object_expr, module_qn, local_var_types
            )
        ):
            full_object_type = object_type
            if cs.SEPARATOR_DOT not in object_type:
                if resolved_class := self._resolve_class_name(object_type, module_qn):
                    full_object_type = resolved_class

            method_qn = f"{full_object_type}.{final_method}"

            if method_qn in self.function_registry:
                logger.debug(
                    ls.CALL_CHAINED,
                    call_name=call_name,
                    method_qn=method_qn,
                    obj_expr=object_expr,
                    obj_type=object_type,
                )
                return self.function_registry[method_qn], method_qn

            if inherited_method := self._resolve_inherited_method(
                full_object_type, final_method
            ):
                logger.debug(
                    ls.CALL_CHAINED_INHERITED,
                    call_name=call_name,
                    method_qn=inherited_method[1],
                    obj_expr=object_expr,
                    obj_type=object_type,
                )
                return inherited_method

        return None

    def _resolve_super_call(
        self, call_name: str, class_context: str | None = None
    ) -> tuple[str, str] | None:
        match call_name:
            case _ if call_name == cs.KEYWORD_SUPER:
                method_name = cs.KEYWORD_CONSTRUCTOR
            case _ if cs.SEPARATOR_DOT in call_name:
                method_name = call_name.split(cs.SEPARATOR_DOT, 1)[1]
            case _:
                return None

        current_class_qn = class_context
        if not current_class_qn:
            logger.debug(ls.CALL_SUPER_NO_CONTEXT, call_name=call_name)
            return None

        if current_class_qn not in self.class_inheritance:
            logger.debug(ls.CALL_SUPER_NO_INHERITANCE, class_qn=current_class_qn)
            return None

        parent_classes = self.class_inheritance[current_class_qn]
        if not parent_classes:
            logger.debug(ls.CALL_SUPER_NO_PARENTS, class_qn=current_class_qn)
            return None

        if result := self._resolve_inherited_method(current_class_qn, method_name):
            callee_type, parent_method_qn = result
            logger.debug(
                ls.CALL_SUPER_RESOLVED,
                call_name=call_name,
                method_qn=parent_method_qn,
            )
            return callee_type, parent_method_qn

        logger.debug(
            ls.CALL_SUPER_UNRESOLVED,
            call_name=call_name,
            class_qn=current_class_qn,
        )
        return None

    def _resolve_self_sibling_method(
        self, call_name: str, class_context: str
    ) -> tuple[str, str] | None:
        # (H) self.method() in a mixin may call a method defined on a SIBLING mixin
        # (H) (neither is the other's base); both are combined into a concrete class.
        # (H) Resolve through the concrete subclasses' MRO and accept the target only
        # (H) when it is unambiguous, so an unrelated same-named method cannot win.
        parts = call_name.split(cs.SEPARATOR_DOT)
        if len(parts) != 2 or parts[0] != cs.KEYWORD_SELF:
            return None
        method_name = parts[1]
        candidates: set[str] = set()
        for subclass_qn in self._concrete_subclasses(class_context):
            candidates |= self._mro_method_qns(subclass_qn, method_name)
        if not candidates:
            return None
        # (H) An @abstractmethod stub never runs when a concrete sibling implements the
        # (H) method, so prefer concrete candidates; resolve only when unambiguous.
        chosen = {
            qn for qn in candidates if not self.function_registry.is_abstract(qn)
        } or candidates
        if len(chosen) != 1:
            return None
        method_qn = next(iter(chosen))
        logger.debug(
            ls.CALL_INSTANCE_ATTR_INHERITED,
            call_name=call_name,
            method_qn=method_qn,
            attr_ref=cs.KEYWORD_SELF,
            var_type=class_context,
        )
        return self.function_registry[method_qn], method_qn

    def _mro_method_qns(self, class_qn: str, method_name: str) -> set[str]:
        results: set[str] = set()
        visited: set[str] = set()
        queue: deque[str] = deque([class_qn])
        while queue:
            current = self._follow_reexports(queue.popleft())
            if current in visited:
                continue
            visited.add(current)
            method_qn = f"{current}.{method_name}"
            if method_qn in self.function_registry:
                results.add(method_qn)
            queue.extend(self.class_inheritance.get(current, ()))
        return results

    def _subclass_map(self) -> dict[str, set[str]]:
        if self._subclass_map_cache is None:
            mapping: dict[str, set[str]] = defaultdict(set)
            for subclass_qn, bases in self.class_inheritance.items():
                for base in bases:
                    mapping[self._follow_reexports(base)].add(subclass_qn)
            self._subclass_map_cache = mapping
        return self._subclass_map_cache

    def _concrete_subclasses(self, class_qn: str) -> set[str]:
        subclass_map = self._subclass_map()
        found: set[str] = set()
        stack = list(subclass_map.get(class_qn, ()))
        while stack:
            current = stack.pop()
            if current in found:
                continue
            found.add(current)
            stack.extend(subclass_map.get(current, ()))
        return found

    def _resolve_inherited_method(
        self, class_qn: str, method_name: str
    ) -> tuple[str, str] | None:
        if class_qn not in self.class_inheritance:
            return None

        bfs_queue = deque(self.class_inheritance.get(class_qn, []))
        visited = set(bfs_queue)

        while bfs_queue:
            # (H) Base classes are recorded by the name the subclass imported, which
            # (H) may be a package re-export (class_ingest.ClassIngestMixin) rather than
            # (H) the real definition (class_ingest.mixin.ClassIngestMixin); follow the
            # (H) re-export so the inherited method qn matches the registry.
            parent_class_qn = self._follow_reexports(bfs_queue.popleft())
            parent_method_qn = f"{parent_class_qn}.{method_name}"

            if parent_method_qn in self.function_registry:
                return (
                    self.function_registry[parent_method_qn],
                    parent_method_qn,
                )

            if parent_class_qn in self.class_inheritance:
                for grandparent_qn in self.class_inheritance[parent_class_qn]:
                    if grandparent_qn not in visited:
                        visited.add(grandparent_qn)
                        bfs_queue.append(grandparent_qn)

        return None

    def _calculate_import_distance(
        self, candidate_qn: str, caller_module_qn: str
    ) -> int:
        caller_parts = caller_module_qn.split(cs.SEPARATOR_DOT)
        candidate_parts = candidate_qn.split(cs.SEPARATOR_DOT)

        common_prefix = 0
        for i in range(min(len(caller_parts), len(candidate_parts))):
            if caller_parts[i] == candidate_parts[i]:
                common_prefix += 1
            else:
                break

        base_distance = max(len(caller_parts), len(candidate_parts)) - common_prefix

        if candidate_qn.startswith(
            cs.SEPARATOR_DOT.join(caller_parts[:-1]) + cs.SEPARATOR_DOT
        ):
            base_distance -= 1

        return base_distance

    def _import_distance_fast(
        self,
        candidate_qn: str,
        caller_parts: list[str],
        caller_len: int,
        caller_parent_prefix: str,
    ) -> int:
        if candidate_qn in _QN_SPLIT_CACHE:
            candidate_parts, candidate_len = _QN_SPLIT_CACHE[candidate_qn]
        else:
            candidate_parts = candidate_qn.split(cs.SEPARATOR_DOT)
            candidate_len = len(candidate_parts)
            _QN_SPLIT_CACHE[candidate_qn] = (candidate_parts, candidate_len)
        common_prefix = 0
        for i in range(min(caller_len, candidate_len)):
            if caller_parts[i] == candidate_parts[i]:
                common_prefix += 1
            else:
                break
        base_distance = max(caller_len, candidate_len) - common_prefix
        if caller_parent_prefix and candidate_qn.startswith(caller_parent_prefix):
            base_distance -= 1
        return base_distance

    def _dealias_type(self, type_name: str) -> str:
        # (H) Follow C++ typedef/using aliases (`typedef Mutex MutexAlias;`) to the
        # (H) underlying class name so an alias'd receiver resolves like the class it
        # (H) names. Bounded against an alias cycle; a no-op when the name is not an
        # (H) alias (and always, for languages with no aliases collected).
        seen: set[str] = set()
        while type_name in self.type_aliases and type_name not in seen:
            seen.add(type_name)
            type_name = self.type_aliases[type_name]
        return type_name

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        return resolve_class_name(
            self._dealias_type(class_name),
            module_qn,
            self.import_processor,
            self.function_registry,
        )

    def resolve_java_method_call(
        self,
        call_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        java_engine = self.type_inference.java_type_inference

        result = java_engine.resolve_java_method_call(
            call_node, local_var_types, module_qn
        )

        if result:
            call_text = (
                call_node.text.decode(cs.ENCODING_UTF8)
                if call_node.text
                else cs.TEXT_UNKNOWN
            )
            logger.debug(
                ls.CALL_JAVA_RESOLVED, call_text=call_text, method_qn=result[1]
            )

        return result
