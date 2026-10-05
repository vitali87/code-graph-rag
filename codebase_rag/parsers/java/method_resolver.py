from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Sequence
from functools import cached_property
from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...decorators import depth_guard, recursion_guard
from ...types_defs import (
    ASTNode,
    JavaCandidateLookups,
    JavaOverloadRank,
    JavaSupertypes,
    NodeType,
)
from ..utils import module_qn_for_entity, safe_decode_text
from .utils import (
    extract_class_info,
    extract_method_call_info,
    extract_method_info,
    get_class_context_from_qn,
    java_written_supertype_count,
    method_reference_receiver_text,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ...types_defs import (
        ASTCacheProtocol,
        FunctionRegistryTrieProtocol,
        SimpleNameLookup,
    )
    from ..import_processor import ImportProcessor

# The registry kinds a Java type declaration takes; records and annotation
# types register as CLASS.
_JAVA_TYPE_NODE_TYPES = (NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM)
# The declarations that name every supertype they have. An enum, a record and
# an annotation type also extend a JDK type nobody wrote down.
_JAVA_FULLY_DECLARED_TYPES = frozenset(
    {cs.TS_CLASS_DECLARATION, cs.TS_INTERFACE_DECLARATION}
)
_NO_SUPERTYPES = JavaSupertypes({}, frozenset())


def _java_signature_arity(qn_or_member: str) -> int | None:
    # Top-level parameter count of a signatured Java method name
    # (`resolve(A,B,Map<K, V>)` -> 3, `create()` -> 0); None if unsignatured.
    # Generic commas (`Map<K, V>`) are nested, so only depth-0 commas separate
    # parameters. Picks the arity-matching overload for a call.
    open_idx = qn_or_member.find(cs.CHAR_PAREN_OPEN)
    if open_idx < 0:
        return None
    inner = qn_or_member[open_idx + 1 : qn_or_member.rfind(cs.CHAR_PAREN_CLOSE)]
    if not inner.strip():
        return 0
    depth = 0
    count = 1
    for ch in inner:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


def _java_param_type_names(qn: str) -> list[str]:
    # Simple parameter type names from a signatured method qn
    # (`isX(Class<?>,String)` -> ['Class', 'String']): generics and package/scope
    # stripped so they compare with inferred argument-type simple names.
    return [_simple_type_name(p) for p in _java_param_type_texts(qn)]


def _java_param_type_texts(qn: str) -> list[str]:
    # The parameter types of a signatured method qn as written; only depth-0
    # commas separate them (`m(Map<K, V>,int)` -> ['Map<K, V>', 'int']).
    open_idx = qn.find(cs.CHAR_PAREN_OPEN)
    close_idx = qn.rfind(cs.CHAR_PAREN_CLOSE)
    if open_idx < 0 or close_idx <= open_idx:
        return []
    inner = qn[open_idx + 1 : close_idx]
    if not inner.strip():
        return []
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in inner:
        if ch in "<([":
            depth += 1
            cur += ch
        elif ch in ">)]":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts]


def _simple_type_name(type_text: str) -> str:
    # Type arguments go and array dimensions stay: `List<String>[]` is an
    # array, which no `List` parameter takes. A varargs `pkg.T...` reads
    # `T...`: its dots are no package separator, and stripping them as one
    # left an empty name no argument could match.
    text = _erase_type_arguments(type_text)
    varargs = cs.JAVA_VARARGS_SUFFIX if text.endswith(cs.JAVA_VARARGS_SUFFIX) else ""
    text = text.removesuffix(cs.JAVA_VARARGS_SUFFIX)
    element = _element_type_text(text)
    dims = text[len(element) :]
    return f"{element.rsplit(cs.SEPARATOR_DOT, 1)[-1]}{dims}{varargs}"


def _erase_type_arguments(type_text: str) -> str:
    depth = 0
    kept: list[str] = []
    for ch in type_text:
        if ch == cs.CHAR_ANGLE_OPEN:
            depth += 1
        elif ch == cs.CHAR_ANGLE_CLOSE:
            depth -= 1
        elif depth == 0 and not ch.isspace():
            kept.append(ch)
    return "".join(kept)


def _element_type_text(type_text: str) -> str:
    # What an array or varargs type holds (`pkg.Shape[][]` -> `pkg.Shape`).
    text = _erase_type_arguments(type_text).removesuffix(cs.JAVA_VARARGS_SUFFIX)
    while text.endswith(cs.JAVA_ARRAY_SUFFIX):
        text = text.removesuffix(cs.JAVA_ARRAY_SUFFIX)
    return text


def _pick_declared_overload(
    declarations: list[ASTNode], param_types: tuple[str, ...]
) -> ASTNode | None:
    # Without a signature to match, the first declaration is all there is. With
    # one, prefer the declaration whose parameter types match it: overloads may
    # differ in return type, and the caller has already chosen which one it is.
    if not declarations:
        return None
    if not param_types:
        return declarations[0]
    for declaration in declarations:
        declared = tuple(
            _simple_type_name(text)
            for text in extract_method_info(declaration)[cs.KEY_PARAMETERS]
        )
        if declared == param_types:
            return declaration
    return declarations[0]


def _overload_rank(
    qn: str,
    arg_types: tuple[str | None, ...],
    supertypes: Sequence[JavaSupertypes] = (),
    lookups: JavaCandidateLookups | None = None,
) -> JavaOverloadRank | None:
    # How well a candidate fits the KNOWN argument types, or None when some
    # argument provably cannot reach its parameter. Unknown args (None) are
    # wildcards and cost nothing. Summing lets the MOST SPECIFIC applicable
    # overload win, so take(Integer) beats take(Object) for an int argument,
    # the way the language resolves it. `supertypes[i]` is what argument i
    # widens to. A parameter typed by one of the candidate's type variables
    # takes what a reference type can stand for, within a bound this does not
    # read: it is unproven, never ruled out. A parameter whose simple name
    # matches the argument's but names another type (`java.awt.List` for a
    # `java.util.List`) is no exact match; it may still be a supertype.
    params = _java_param_type_names(qn)
    if len(params) != len(arg_types):
        return None
    declaration = _CandidateDeclaration(qn, lookups)
    unproven = conversions = distance = 0
    for index, (at, pt) in enumerate(zip(arg_types, params, strict=True)):
        if at is None:
            continue
        known = supertypes[index] if index < len(supertypes) else _NO_SUPERTYPES
        rank = _candidate_argument_rank(
            _simple_type_name(at), pt, index, known, declaration
        )
        if rank is None:
            return None
        conversion, depth = rank
        if conversion == cs.JAVA_RANK_UNPROVEN:
            unproven += 1
        else:
            conversions += conversion
            distance += depth
    return JavaOverloadRank(unproven, conversions, distance)


class _CandidateDeclaration:
    # What ranking reads from one candidate's declaration. Each read walks the
    # declaring file, so it happens at most once per candidate, and only when
    # some argument's rank turns on it.

    def __init__(self, qn: str, lookups: JavaCandidateLookups | None) -> None:
        self._qn = qn
        self._lookups = lookups

    @cached_property
    def type_variables(self) -> frozenset[str]:
        return self._lookups.type_variables(self._qn) if self._lookups else frozenset()

    @cached_property
    def parameter_types(self) -> tuple[str | None, ...]:
        return self._lookups.parameter_types(self._qn) if self._lookups else ()

    @cached_property
    def written_types(self) -> tuple[str, ...]:
        return tuple(
            _element_type_text(text) for text in _java_param_type_texts(self._qn)
        )

    def parameter_identity(self, index: int) -> tuple[str | None, str | None]:
        # Parameter `index` as the declaring file resolves it (None where
        # unsure) and as written.
        declared, written = self.parameter_types, self.written_types
        return (
            declared[index] if index < len(declared) else None,
            written[index] if index < len(written) else None,
        )

    def takes_argument_type(self, index: int, known: JavaSupertypes) -> bool:
        # Parameter `index`, sharing the argument's simple name, names the
        # argument's very type.
        return (
            _same_type(known.qualified, known.written, *self.parameter_identity(index))
            is True
        )

    def supertypes_for(
        self, index: int, param_type: str, known: JavaSupertypes
    ) -> JavaSupertypes:
        # `known` as it bears on parameter `index`: a supertype the walk
        # reached under the parameter's simple name counts only as far as it
        # is the type the parameter names.
        name = _element_type_text(param_type)
        reached = [
            (ref, depth)
            for ref, depth in known.refs.items()
            if _simple_type_name(ref) == name
        ]
        if not reached:
            return known
        declared, written = self.parameter_identity(index)
        verdicts = [
            (
                _same_type(_certain_ref(ref, known.project), ref, declared, written),
                depth,
            )
            for ref, depth in reached
        ]
        return _narrowed(known, name, verdicts)


def _certain_ref(ref: str, project: frozenset[str]) -> str | None:
    # A walked supertype whose identity is settled: a project type, or a JDK
    # type named in full.
    if ref in project or ref.startswith(cs.JAVA_STDLIB_PREFIXES):
        return ref
    return None


def _same_type(
    qualified: str | None,
    written: str | None,
    declared: str | None,
    declared_written: str | None,
) -> bool | None:
    # Whether two types sharing a simple name are one: each as resolved
    # (None where unsure) and as written. A project type is the other only
    # when both resolve to it; a JDK one keeps the benefit of the doubt a
    # wildcard or java.lang import gives it. None when nothing settles it.
    if qualified and declared:
        return qualified == declared
    settled = qualified or declared
    if settled and not settled.startswith(cs.JAVA_STDLIB_PREFIXES):
        return None
    if settled or (written is not None and written == declared_written):
        return True
    return None


def _narrowed(
    known: JavaSupertypes, name: str, verdicts: list[tuple[bool | None, int]]
) -> JavaSupertypes:
    # `known` with `name` reached at the nearest supertype that is the
    # parameter's type. When none may be, the parameter is out of reach of a
    # complete walk; when some may, it stays unproven.
    depths = {key: depth for key, depth in known.depths.items() if key != name}
    unreachable = known.unreachable - {name}
    if proven := [depth for same, depth in verdicts if same]:
        depths[name] = min(proven)
    elif known.complete and all(same is False for same, _ in verdicts):
        unreachable |= {name}
    return known._replace(depths=depths, unreachable=unreachable)


def _candidate_argument_rank(
    arg_type: str,
    param_type: str,
    index: int,
    known: JavaSupertypes,
    declaration: _CandidateDeclaration,
) -> tuple[int, int] | None:
    # One known argument against its parameter, or None when it provably
    # cannot reach it (see `_overload_rank`).
    known = declaration.supertypes_for(index, param_type, known)
    if (rank := _argument_rank(arg_type, param_type, known)) is None:
        return _type_variable_rank(arg_type, param_type, declaration.type_variables)
    if (
        rank[0] == cs.JAVA_RANK_EXACT
        and (known.qualified or known.written)
        and not declaration.takes_argument_type(index, known)
    ):
        return cs.JAVA_RANK_UNPROVEN, 0
    return rank


def _type_variable_rank(
    arg_type: str, param_type: str, type_variables: frozenset[str]
) -> tuple[int, int] | None:
    # An argument no conversion reaches stays possible only for a parameter
    # typed by one of the candidate's type variables, and only unproven.
    if _element_type_text(param_type) in type_variables and _type_variable_accepts(
        arg_type, param_type
    ):
        return cs.JAVA_RANK_UNPROVEN, 0
    return None


def _type_variable_accepts(arg_type: str, param_type: str) -> bool:
    # A type variable stands for a reference type: `T` takes any argument, a
    # primitive boxed; `T[]` takes a `String[]` or an `int[][]` (T = int[]),
    # never an `int[]` or a `String`; `T...` also takes one element.
    if param_type.endswith(cs.JAVA_VARARGS_SUFFIX):
        element = param_type.removesuffix(cs.JAVA_VARARGS_SUFFIX)
        return _type_variable_accepts(
            arg_type, f"{element}{cs.JAVA_ARRAY_SUFFIX}"
        ) or _type_variable_accepts(arg_type, element)
    if not (param_dims := _array_dimensions(param_type)):
        return True
    arg_dims = _array_dimensions(arg_type)
    if arg_dims != param_dims:
        return arg_dims > param_dims
    return _element_type_text(arg_type) not in cs.JAVA_BOXED_TYPES


def _array_dimensions(type_text: str) -> int:
    text = _erase_type_arguments(type_text)
    return text.count(cs.JAVA_ARRAY_SUFFIX) + text.endswith(cs.JAVA_VARARGS_SUFFIX)


def _argument_rank(
    arg_type: str, param_type: str, supertypes: JavaSupertypes = _NO_SUPERTYPES
) -> tuple[int, int] | None:
    # The conversion the language would apply, ranked by preference (JLS 5.3),
    # with how far up the argument's hierarchy a widening reference
    # conversion reaches.
    if param_type.endswith(cs.JAVA_VARARGS_SUFFIX) and arg_type.endswith(
        cs.JAVA_ARRAY_SUFFIX
    ):
        # A `T...` parameter takes a `T[]` argument as is (JLS 15.12.2.2).
        param_type = (
            f"{param_type.removesuffix(cs.JAVA_VARARGS_SUFFIX)}{cs.JAVA_ARRAY_SUFFIX}"
        )
    if arg_type == param_type:
        return cs.JAVA_RANK_EXACT, 0
    if arg_type.endswith(cs.JAVA_ARRAY_SUFFIX):
        return _array_rank(arg_type, param_type, supertypes)
    if param_type in cs.JAVA_WIDENING_PRIMITIVES.get(arg_type, ()):
        return cs.JAVA_RANK_WIDENED, 0
    boxed = cs.JAVA_BOXED_TYPES.get(arg_type, arg_type)
    if param_type == boxed:
        return cs.JAVA_RANK_BOXED, 0
    if param_type in cs.JAVA_REFERENCE_SUPERTYPES.get(boxed, ()):
        return cs.JAVA_RANK_SUPERTYPE, 0
    if param_type == cs.JAVA_TYPE_OBJECT_NAME:
        return cs.JAVA_RANK_OBJECT, 0
    if (depth := supertypes.depths.get(param_type)) is not None:
        return cs.JAVA_RANK_SUPERTYPE, depth
    # A primitive, a boxed type, String and Object have no supertypes beyond
    # the ones checked above, so no other parameter can take them, and no
    # other type converts to a primitive. Any other type may reach the
    # parameter through a supertype nobody indexed.
    if (
        boxed in cs.JAVA_REFERENCE_SUPERTYPES
        or arg_type == cs.JAVA_TYPE_OBJECT_NAME
        or param_type in cs.JAVA_BOXED_TYPES
        or param_type in supertypes.unreachable
    ):
        return None
    return cs.JAVA_RANK_UNPROVEN, 0


def _array_rank(
    arg_type: str, param_type: str, supertypes: JavaSupertypes
) -> tuple[int, int] | None:
    # An array widens to an array of a supertype of its element, or else to
    # Object, Cloneable and Serializable alone; a primitive element neither
    # widens nor boxes (JLS 4.10.3).
    if not param_type.endswith(cs.JAVA_ARRAY_SUFFIX):
        if param_type == cs.JAVA_TYPE_OBJECT_NAME:
            return cs.JAVA_RANK_OBJECT, 0
        if param_type in cs.JAVA_ARRAY_SUPERTYPES:
            return cs.JAVA_RANK_SUPERTYPE, 1
        return None
    arg_element = arg_type.removesuffix(cs.JAVA_ARRAY_SUFFIX)
    param_element = param_type.removesuffix(cs.JAVA_ARRAY_SUFFIX)
    if arg_element in cs.JAVA_BOXED_TYPES or param_element in cs.JAVA_BOXED_TYPES:
        return None
    return _argument_rank(arg_element, param_element, supertypes)


def _callable_visible_to_caller(
    entity_type: str, qn: str, caller_qn: str | None
) -> bool:
    # A Java FUNCTION entry is a method declared inside another method's body, i.e.
    # an anonymous/local class method, only visible lexically. The unqualified
    # module-wide fallback must not let a call OUTSIDE that anon bind to it (M.use()
    # -> an anon-local helper() elsewhere). Accept a FUNCTION only when the caller is
    # lexically within its owning scope (owner qn prefixes the caller). METHOD and
    # CONSTRUCTOR entries are top-level and stay module-visible.
    if entity_type != cs.ENTITY_FUNCTION:
        return True
    if not caller_qn:
        return False
    owner = qn.split(cs.CHAR_PAREN_OPEN, maxsplit=1)[0].rsplit(cs.SEPARATOR_DOT, 1)[0]
    return caller_qn == owner or caller_qn.startswith(f"{owner}{cs.SEPARATOR_DOT}")


def _ranked(
    matches: Sequence[tuple[str, str]],
    arg_types: tuple[str | None, ...],
    supertypes: Sequence[JavaSupertypes] = (),
    lookups: JavaCandidateLookups | None = None,
) -> list[tuple[JavaOverloadRank, tuple[str, str]]]:
    # The candidates that can take the argument types, with how well they
    # fit, in declaration order.
    return [
        (rank, match)
        for match in matches
        if (rank := _overload_rank(match[1], arg_types, supertypes, lookups))
        is not None
    ]


def _ranked_best(
    matches: Sequence[tuple[str, str]],
    arg_types: tuple[str | None, ...],
    supertypes: Sequence[JavaSupertypes] = (),
    lookups: JavaCandidateLookups | None = None,
) -> list[tuple[str, str]]:
    # The candidates the argument types rank best, in declaration order;
    # empty when no candidate can take them.
    ranked = _ranked(matches, arg_types, supertypes, lookups)
    if not ranked:
        return []
    best = min(rank for rank, _ in ranked)
    return [match for rank, match in ranked if rank == best]


def _best_overloads(
    matches: Sequence[tuple[str, str]],
    arg_count: int | None,
    arg_types: tuple[str | None, ...],
    supertypes: Sequence[JavaSupertypes] = (),
    lookups: JavaCandidateLookups | None = None,
) -> list[tuple[str, str]]:
    # The same-name candidates nothing tells apart from the best one, in
    # declaration order: prefer an argument-TYPE match (resolves same-arity
    # overloads like isX(String) vs isX(Class)), then an argument-COUNT match,
    # then the first. A type match implies an arity match, so it is the most
    # specific. More than one left means declaration order alone would choose.
    if len(matches) > 1 and any(at is not None for at in arg_types):
        if best := _ranked_best(matches, arg_types, supertypes, lookups):
            return best
    if len(matches) > 1 and arg_count is not None:
        if same_arity := [
            match for match in matches if _java_signature_arity(match[1]) == arg_count
        ]:
            return same_arity
    return list(matches[:1])


def _overload_contenders(
    matches: Sequence[tuple[str, str]],
    arg_count: int | None,
    arg_types: tuple[str | None, ...],
    supertypes: Sequence[JavaSupertypes] = (),
    lookups: JavaCandidateLookups | None = None,
) -> list[tuple[str, str]]:
    # The best candidates plus every one an unproven conversion could make
    # javac prefer to them, in declaration order. More than one means the
    # pick rests on what the argument types cannot show.
    if len(matches) > 1 and any(at is not None for at in arg_types):
        if ranked := _ranked(matches, arg_types, supertypes, lookups):
            best = min(rank for rank, _ in ranked)
            return [
                match
                for rank, match in ranked
                if rank == best
                or _may_outrank(
                    rank, best, _is_variable_arity_call(match[1], arg_types)
                )
            ]
    return _best_overloads(matches, arg_count, ())


def _may_outrank(
    rank: JavaOverloadRank, best: JavaOverloadRank, variable_arity: bool
) -> bool:
    # An unproven conversion may hold, and then be as specific as a widening
    # one step up the hierarchy. A candidate that, so counted, ranks no worse
    # than the best could be the overload javac picks. Beside a best that is
    # itself unproven, any applicable candidate could be. A variable-arity
    # call is weighed only when no other candidate applies (JLS 15.12.2.4),
    # so it never displaces a best that provably does.
    if not rank.unproven:
        return False
    if best.unproven:
        return True
    if variable_arity:
        return False
    best_case = JavaOverloadRank(
        0,
        rank.conversions + rank.unproven * cs.JAVA_RANK_SUPERTYPE,
        rank.distance + rank.unproven,
    )
    return best_case <= best


def _is_variable_arity_call(qn: str, arg_types: tuple[str | None, ...]) -> bool:
    # A `T...` parameter given one element, not a `T[]`.
    params = _java_param_type_names(qn)
    last = arg_types[-1] if arg_types else None
    return (
        bool(params)
        and params[-1].endswith(cs.JAVA_VARARGS_SUFFIX)
        and len(params) == len(arg_types)
        and last is not None
        and not _simple_type_name(last).endswith(cs.JAVA_ARRAY_SUFFIX)
    )


def _pick_overload(
    matches: Sequence[tuple[str, str]],
    arg_count: int | None,
    arg_types: tuple[str | None, ...],
    lookups: JavaCandidateLookups | None = None,
) -> tuple[str, str] | None:
    # Ties keep declaration order, so the choice stays deterministic.
    best = _best_overloads(matches, arg_count, arg_types, lookups=lookups)
    return best[0] if best else None


def _java_getter_return_type(method_lower: str) -> str | None:
    # A getter's likely type from its name: getName -> String, getId -> long,
    # getSize/getLength -> int.
    if cs.JAVA_NAME_PATTERN in method_lower:
        return cs.JAVA_TYPE_STRING_FQN
    if cs.JAVA_ID_PATTERN in method_lower:
        return cs.JAVA_TYPE_LONG
    if cs.JAVA_SIZE_PATTERN in method_lower or cs.JAVA_LENGTH_PATTERN in method_lower:
        return cs.JAVA_TYPE_INT
    return None


def _java_factory_return_type(method_call: str) -> str | None:
    # A qualified create/new factory's likely product from its method name.
    parts = method_call.split(cs.SEPARATOR_DOT)
    if len(parts) < 2:
        return None
    method_name_lower = parts[-1].lower()
    if cs.JAVA_USER_PATTERN in method_name_lower:
        return cs.JAVA_HEURISTIC_USER
    if cs.JAVA_ORDER_PATTERN in method_name_lower:
        return cs.JAVA_HEURISTIC_ORDER
    return None


class JavaMethodResolverMixin:
    __slots__ = ()
    import_processor: ImportProcessor
    function_registry: FunctionRegistryTrieProtocol
    project_name: str
    module_qn_to_file_path: dict[str, Path]
    ast_cache: ASTCacheProtocol
    class_inheritance: dict[str, list[str]]
    simple_name_lookup: SimpleNameLookup
    _fqn_to_module_qn: dict[str, list[str]]
    # The overload family each pick of the call being resolved was weighed
    # in, by the picked method: the call's ties come from the same family.
    _family_picks: dict[str, list[tuple[str, str]]]

    @abstractmethod
    def _resolve_java_type_name(self, type_name: str, module_qn: str) -> str: ...

    @abstractmethod
    def _infer_java_type_from_expression(
        self,
        expr_node: ASTNode,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None: ...

    @abstractmethod
    def _imported_class_qn(self, target: str, type_name: str) -> str: ...

    @abstractmethod
    def _rank_module_candidates(
        self, candidates: list[str], class_qn: str, current_module_qn: str | None
    ) -> list[str]: ...

    @abstractmethod
    def _find_registry_entries_under(
        self, prefix: str
    ) -> Iterable[tuple[str, str]]: ...

    @abstractmethod
    def _get_superclass_name(self, class_qn: str) -> str | None: ...

    @abstractmethod
    def _get_implemented_interfaces(self, class_qn: str) -> list[str]: ...

    @abstractmethod
    def _get_current_class_name(self, module_qn: str) -> str | None: ...

    @abstractmethod
    def _lookup_variable_type(self, var_name: str, module_qn: str) -> str | None: ...

    @abstractmethod
    def _lookup_java_field_type(
        self, class_type: str, field_name: str, module_qn: str
    ) -> str | None: ...

    @abstractmethod
    def _find_containing_java_class(self, node: ASTNode) -> ASTNode | None: ...

    def _java_this_type(
        self, context_node: ASTNode | None, module_qn: str
    ) -> str | None:
        # Inside a method-body anonymous class `this` is the anon (its base
        # type), which the lexical named-class walk misses; prefer that, then
        # the lexical containing class (precise in multi-class files); fall
        # back to the first class under the module otherwise.
        if anon_base := self._enclosing_anon_base_qn(context_node, module_qn):
            return anon_base
        if lexical := self._lexical_class_qn(context_node, module_qn):
            return lexical
        return next(
            (
                str(qn)
                for qn, entity_type in self.function_registry.find_with_prefix(
                    module_qn
                )
                if entity_type == NodeType.CLASS
            ),
            None,
        )

    def _java_super_type(
        self, context_node: ASTNode | None, module_qn: str
    ) -> str | None:
        # The lexical class's parent when available; otherwise the first class
        # under the module that has a parent.
        if (lexical := self._lexical_class_qn(context_node, module_qn)) and (
            parent_qn := self._find_parent_class(lexical)
        ):
            return parent_qn
        for qn, entity_type in self.function_registry.find_with_prefix(module_qn):
            if entity_type == NodeType.CLASS and (
                parent_qn := self._find_parent_class(qn)
            ):
                return parent_qn
        return None

    def _resolve_java_object_type(
        self,
        object_ref: str,
        local_var_types: dict[str, str],
        module_qn: str,
        context_node: ASTNode | None = None,
    ) -> str | None:
        if object_ref in local_var_types:
            return local_var_types[object_ref]

        # 'this' reference: inside a method-body anonymous class `this` is the anon
        # (its base type), which the lexical named-class walk misses; prefer that,
        # then the lexical containing class (precise in multi-class files); fall back
        # to the first class under the module otherwise.
        if object_ref == cs.JAVA_KEYWORD_THIS:
            return self._java_this_type(context_node, module_qn)

        if object_ref == cs.JAVA_KEYWORD_SUPER:
            return self._java_super_type(context_node, module_qn)

        import_map = self.import_processor.import_mapping.get(module_qn)
        if import_map is not None and object_ref in import_map:
            return self._imported_class_qn(import_map[object_ref], object_ref)

        simple_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{object_ref}"
        if (
            simple_class_qn in self.function_registry
            and self.function_registry[simple_class_qn] == NodeType.CLASS
        ):
            return simple_class_qn

        # A nested class referenced by its simple name as a static receiver base
        # (`Checker.INSTANCE...`, gson's `AccessChecker.INSTANCE`) has qn
        # `module.Outer.Nested`, which the direct check above misses; use the
        # nested-aware type resolver so the static-field access chain resolves.
        nested_qn = self._resolve_java_type_name(object_ref, module_qn)
        if nested_qn != object_ref and self.function_registry.get(nested_qn) in (
            NodeType.CLASS,
            NodeType.INTERFACE,
            NodeType.ENUM,
        ):
            return nested_qn

        # An unqualified class-name receiver for a static call (`T.make()`) defined in
        # a sibling file: imports and the current module were checked above, so the
        # remaining unqualified case is a same-package class.
        if sibling_class_qn := self._resolve_sibling_class_qn(object_ref, module_qn):
            return sibling_class_qn

        # A receiver like `obj.engine` (field access on a typed variable) is not a
        # single name: resolve the base, then walk each field's declared type across
        # classes so `obj.engine.start()` and deeper chains resolve to a method.
        if cs.SEPARATOR_DOT in object_ref:
            return self._resolve_field_access_chain_type(
                object_ref, local_var_types, module_qn, context_node
            )

        return None

    def _lexical_class_qn(
        self, context_node: ASTNode | None, module_qn: str
    ) -> str | None:
        if context_node is None:
            return None
        if not (class_node := self._find_containing_java_class(context_node)):
            return None
        if not (class_name := extract_class_info(class_node).get(cs.FIELD_NAME)):
            return None
        return self._resolve_java_type_name(class_name, module_qn)

    def _enclosing_anon_base_qn(
        self, context_node: ASTNode | None, module_qn: str
    ) -> str | None:
        # If `context_node` sits inside a method-body anonymous class
        # (`new Base(){ ... }`) before any named class, return the anon's base type
        # qn: an unqualified call inside the anon is `this.m()`, dispatched on the
        # anon (its base), not the enclosing named class. None otherwise.
        if context_node is None:
            return None
        named = (
            cs.TS_CLASS_DECLARATION,
            cs.TS_INTERFACE_DECLARATION,
            cs.TS_ENUM_DECLARATION,
            cs.TS_RECORD_DECLARATION,
        )
        current = context_node.parent
        while current is not None:
            if current.type in named:
                return None
            if current.type == cs.TS_CLASS_BODY:
                parent = current.parent
                if (
                    parent is not None
                    and parent.type == cs.TS_OBJECT_CREATION_EXPRESSION
                    and (type_node := parent.child_by_field_name(cs.FIELD_TYPE))
                    is not None
                    and type_node.text is not None
                ):
                    base = type_node.text.decode(cs.ENCODING_UTF8).split(
                        cs.CHAR_ANGLE_OPEN, 1
                    )[0]
                    return self._resolve_java_type_name(base, module_qn)
                return None
            current = current.parent
        return None

    def _resolve_field_access_chain_type(
        self,
        object_ref: str,
        local_var_types: dict[str, str],
        module_qn: str,
        context_node: ASTNode | None = None,
    ) -> str | None:
        parts = object_ref.split(cs.SEPARATOR_DOT)
        if len(parts) < 2:
            return None

        current_type = self._resolve_java_object_type(
            parts[0], local_var_types, module_qn, context_node
        )
        if not current_type:
            return None

        for field_name in parts[1:]:
            next_type = self._lookup_java_field_type(
                current_type, field_name, module_qn
            )
            if not next_type:
                return None
            current_type = next_type

        return current_type

    def _find_parent_class(self, class_qn: str) -> str | None:
        parent_classes = self.class_inheritance.get(class_qn, [])
        return parent_classes[0] if parent_classes else None

    def _resolve_sibling_class_qn(self, class_name: str, module_qn: str) -> str | None:
        # Resolve a bare class name to a registered Class/Interface in a SIBLING file
        # of the same package (directory), so an unqualified same-package reference
        # resolves without an import. A bare receiver with no import is only valid for
        # the current package in Java, so a class in another package is NOT a match:
        # linking it would be a wrong cross-package edge, so leave the receiver
        # unresolved instead.
        if not (candidate_modules := self._fqn_to_module_qn.get(class_name)):
            return None
        if not (current_file := self.module_qn_to_file_path.get(module_qn)):
            return None
        current_dir = current_file.parent
        for candidate_module in candidate_modules:
            candidate_qn = f"{candidate_module}{cs.SEPARATOR_DOT}{class_name}"
            if candidate_qn not in self.function_registry or self.function_registry[
                candidate_qn
            ] not in (NodeType.CLASS, NodeType.INTERFACE):
                continue
            candidate_file = self.module_qn_to_file_path.get(candidate_module)
            if candidate_file and candidate_file.parent == current_dir:
                return candidate_qn
        return None

    def _resolve_static_or_local_method(
        self,
        method_name: str,
        module_qn: str,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        matches = [
            (entity_type, qn)
            for qn, entity_type in self.function_registry.find_with_prefix(module_qn)
            if entity_type in cs.JAVA_CALLABLE_ENTITY_TYPES
            and qn.split(cs.CHAR_PAREN_OPEN)[0].endswith(
                f"{cs.SEPARATOR_DOT}{method_name}"
            )
            and _callable_visible_to_caller(entity_type, qn, caller_qn)
        ]
        return _pick_overload(matches, arg_count, arg_types, self._candidate_lookups)

    def _resolve_unqualified_java_call(
        self,
        call_node: ASTNode,
        method_name: str,
        module_qn: str,
        arg_count: int,
        arg_types: tuple[str | None, ...],
        caller_qn: str | None,
    ) -> tuple[str, str] | None:
        logger.debug(ls.JAVA_RESOLVING_STATIC, method=method_name)
        # An unqualified call `m(...)` is `this.m(...)`. Inside a method-body
        # anonymous class (`new Base(){ read(){ m(); } }`), `this` is the anon,
        # so bind against the anon's base type FIRST: `_lexical_class_qn` only
        # sees the enclosing NAMED class and would mis-bind an inherited call to
        # a same-named method there. Then the enclosing class hierarchy; the bare
        # module-wide scan is the last resort (it ignores lexical scope).
        if (anon_base_qn := self._enclosing_anon_base_qn(call_node, module_qn)) and (
            result := self._resolve_instance_method(
                anon_base_qn, method_name, module_qn, arg_count, arg_types
            )
        ):
            logger.debug(ls.JAVA_FOUND_STATIC, result=result)
            return result
        if (enclosing_qn := self._lexical_class_qn(call_node, module_qn)) and (
            result := self._resolve_instance_method(
                enclosing_qn, method_name, module_qn, arg_count, arg_types
            )
        ):
            logger.debug(ls.JAVA_FOUND_STATIC, result=result)
            return result
        result = self._resolve_static_or_local_method(
            method_name, module_qn, arg_count, arg_types, caller_qn
        )
        if result:
            logger.debug(ls.JAVA_FOUND_STATIC, result=result)
        else:
            logger.debug(ls.JAVA_STATIC_NOT_FOUND, method=method_name)
        return result

    def _resolve_instance_method(
        self,
        object_type: str,
        method_name: str,
        module_qn: str,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
    ) -> tuple[str, str] | None:
        resolved_type = self._resolve_java_type_name(object_type, module_qn)

        # The family needs the receiver's registered class, which a
        # package-private type in a sibling file only has through the package.
        if method_result := self._pick_from_overload_family(
            self._java_type_ref(object_type, module_qn),
            method_name,
            module_qn,
            arg_types,
        ):
            return method_result

        if method_result := self._find_method_with_any_signature(
            resolved_type, method_name, module_qn, arg_count, arg_types
        ):
            return method_result

        if inherited_result := self._find_inherited_method(
            resolved_type, method_name, module_qn, arg_count, arg_types
        ):
            return inherited_result

        return self._find_interface_method(
            resolved_type, method_name, module_qn, arg_count, arg_types
        )

    def _find_method_with_any_signature(
        self,
        class_qn: str,
        method_name: str,
        current_module_qn: str | None = None,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
    ) -> tuple[str, str] | None:
        if class_qn:
            if result := self._search_method_in_class(
                class_qn, method_name, arg_count, arg_types
            ):
                return result

        if class_qn and not class_qn.startswith(self.project_name):
            return self._search_method_in_alternate_modules(
                class_qn, method_name, current_module_qn, arg_count, arg_types
            )

        return None

    def _search_method_in_class(
        self,
        class_qn: str,
        method_name: str,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
    ) -> tuple[str, str] | None:
        return _pick_overload(
            self._methods_named(class_qn, method_name),
            arg_count,
            arg_types,
            self._candidate_lookups,
        )

    def _methods_named(self, class_qn: str, method_name: str) -> list[tuple[str, str]]:
        # Every overload of `method_name` declared directly on `class_qn`; a
        # nested class's member carries an extra segment and never matches.
        matches: list[tuple[str, str]] = []
        for qn, method_type in self._find_registry_entries_under(class_qn):
            if qn == class_qn:
                continue
            suffix = qn[len(class_qn) :]
            if not suffix.startswith(cs.SEPARATOR_DOT):
                continue
            member = suffix[1:]
            if self._is_matching_method(member, method_name):
                matches.append((method_type, qn))
        return matches

    def java_method_reference_targets(
        self,
        receiver: ASTNode,
        method_name: str,
        local_var_types: dict[str, str] | None,
        module_qn: str,
    ) -> list[tuple[str, str]]:
        # The receiver is typed exactly as a call's `object` is, so one the call
        # path cannot type (a lambda parameter, the JDK's `System.out`) binds
        # nothing instead of a same-named first-party method found by name.
        if receiver.type in (cs.TS_METHOD_INVOCATION, cs.TS_OBJECT_CREATION_EXPRESSION):
            receiver_type = self._infer_java_type_from_expression(
                receiver, module_qn, local_var_types
            )
        elif receiver_text := method_reference_receiver_text(receiver):
            receiver_type = self._resolve_java_object_type(
                receiver_text, local_var_types or {}, module_qn, receiver
            )
        else:
            receiver_type = None
        if not receiver_type or not (
            first := self._resolve_instance_method(
                receiver_type, method_name, module_qn
            )
        ):
            return []
        # Which overload a reference denotes is decided by the functional
        # interface it is assigned to, a type the parser never sees (the C#
        # method-group shape), so the whole family visible on the receiver is
        # referenced rather than whichever overload the lookup met first.
        declaring_qn = (
            first[1].split(cs.CHAR_PAREN_OPEN, 1)[0].rpartition(cs.SEPARATOR_DOT)[0]
        )
        return self._overload_family(declaring_qn, method_name) or [first]

    def _overload_family(
        self, class_qn: str, method_name: str
    ) -> list[tuple[str, str]]:
        # The overloads of `method_name` a call or a method reference through
        # `class_qn` can reach: those it declares plus those it inherits up
        # the superclass chain. A parent overload with the signature of one a
        # subclass declares is overridden by it, so it is not a separate
        # candidate. Simple type
        # names match first, then the types each file resolves them to:
        # `m(java.awt.List)` does not override `m(java.util.List)`. Two
        # overloads of ONE class can match by simple names
        # (`of(javax...TypeVariable)`, `of(java.lang.reflect.TypeVariable)`);
        # neither overrides the other, so both stay.
        family: list[tuple[str, str]] = []
        below: dict[tuple[str, ...], list[tuple[str, str]]] = {}
        seen: set[str] = set()
        current: str | None = class_qn
        while current and current not in seen:
            seen.add(current)
            declared = [
                (entry, tuple(_java_param_type_names(entry[1])))
                for entry in self._methods_named(current, method_name)
            ]
            family.extend(
                entry
                for entry, sig in declared
                if not any(
                    self._same_parameter_types(entry[1], current, qn, owner)
                    for qn, owner in below.get(sig, ())
                )
            )
            for entry, sig in declared:
                below.setdefault(sig, []).append((entry[1], current))
            current = self._find_parent_class(current)
        return family

    def _same_parameter_types(
        self, qn: str, class_qn: str, other_qn: str, other_class_qn: str
    ) -> bool:
        return self._parameter_types(qn, class_qn) == self._parameter_types(
            other_qn, other_class_qn
        )

    def _parameter_types(self, qn: str, class_qn: str) -> list[str]:
        # The types a method's parameters name, as its own file resolves them;
        # a varargs parameter is its array type.
        module_qn = module_qn_for_entity(class_qn, self.module_qn_to_file_path)
        types: list[str] = []
        for text in _java_param_type_texts(qn):
            erased = _erase_type_arguments(text)
            element = _element_type_text(erased)
            dims = erased[len(element) :].replace(
                cs.JAVA_VARARGS_SUFFIX, cs.JAVA_ARRAY_SUFFIX
            )
            if module_qn and cs.SEPARATOR_DOT not in element:
                element = self._java_type_ref(element, module_qn)
            types.append(f"{element}{dims}")
        return types

    def _pick_from_overload_family(
        self,
        class_qn: str,
        method_name: str,
        module_qn: str,
        arg_types: tuple[str | None, ...],
    ) -> tuple[str, str] | None:
        # Overload resolution weighs the methods a class declares together
        # with those it inherits (JLS 15.12.2.1): beside an inherited
        # Base.conv(Shape,int), Hier.conv(Circle,int) is one candidate of two,
        # not the end of a lookup that stops at the first class declaring the
        # name. When the argument types rank no candidate, the class-first
        # lookup decides as before.
        if not any(at is not None for at in arg_types):
            return None
        family = self._overload_family(class_qn, method_name)
        if len(family) < 2:
            return None
        best = _ranked_best(
            family,
            arg_types,
            self._argument_supertypes(family, arg_types, module_qn),
            self._candidate_lookups,
        )
        if not best:
            return None
        self._family_picks[best[0][1]] = family
        return best[0]

    def java_overload_ties(
        self,
        call_node: ASTNode,
        method_qn: str,
        local_var_types: dict[str, str] | None,
        module_qn: str,
    ) -> list[tuple[str, str]]:
        # The overloads the call's argument types cannot tell apart from
        # `method_qn`, the target it was bound to: declaration order alone chose
        # among them. Empty when the types, or a lone candidate, decided.
        unsignatured = method_qn.split(cs.CHAR_PAREN_OPEN, 1)[0]
        declaring_qn, _, method_name = unsignatured.rpartition(cs.SEPARATOR_DOT)
        if not declaring_qn or not (call_info := extract_method_call_info(call_node)):
            return []
        arg_count = call_info[cs.FIELD_ARGUMENTS]
        # A pick weighed among the receiver's own and inherited overloads is
        # tied within that family; the declaring class's alone can miss an
        # overload the receiver adds. Read before inferring the argument
        # types, whose nested calls resolve picks of their own.
        family = self._family_picks.get(method_qn) or self._overload_family(
            declaring_qn, method_name
        )
        # Inferring argument types can resolve nested calls, so it waits until
        # a second candidate of the call's arity makes it worth the cost.
        if sum(_java_signature_arity(qn) == arg_count for _, qn in family) < 2:
            return []
        arg_types = self._infer_arg_types(call_node, local_var_types or {}, module_qn)
        tied = _overload_contenders(
            family,
            arg_count,
            arg_types,
            self._argument_supertypes(family, arg_types, module_qn),
            self._candidate_lookups,
        )
        if all(qn != method_qn for _, qn in tied):
            if _java_signature_arity(method_qn) != arg_count or all(
                at is None for at in arg_types
            ):
                return []
            # A lookup blind to some argument type bound an overload the types
            # rank below another: neither is certain.
            tied = [entry for entry in family if entry[1] == method_qn] + tied
        return tied if len(tied) > 1 else []

    def _argument_supertypes(
        self,
        family: Sequence[tuple[str, str]],
        arg_types: tuple[str | None, ...],
        module_qn: str,
    ) -> tuple[JavaSupertypes, ...]:
        # Each argument's supertypes, walked only when some candidate's
        # parameter could need them: one equal to the argument's own type, or
        # Object, never does, and a primitive, boxed or String argument has no
        # supertypes beyond the fixed ones. An array walks its element type.
        params = [_java_param_type_names(qn) for _, qn in family]
        supertypes: list[JavaSupertypes] = []
        for index, arg_type in enumerate(arg_types):
            if arg_type is None:
                supertypes.append(_NO_SUPERTYPES)
                continue
            simple = _simple_type_name(arg_type)
            element = _element_type_text(simple)
            if (
                cs.JAVA_BOXED_TYPES.get(element, element)
                in cs.JAVA_REFERENCE_SUPERTYPES
            ):
                supertypes.append(_NO_SUPERTYPES)
                continue
            at_index = [p[index] for p in params if len(p) == len(arg_types)]
            qualified, written = (
                (
                    self._qualified_type(arg_type, module_qn),
                    _element_type_text(arg_type),
                )
                if simple in at_index
                else (None, None)
            )
            others = frozenset(
                _element_type_text(p)
                for p in at_index
                if p not in (simple, cs.JAVA_TYPE_OBJECT_NAME)
            )
            walked = (
                self._java_supertypes(_element_type_text(arg_type), module_qn, others)
                if others
                else _NO_SUPERTYPES
            )
            supertypes.append(walked._replace(qualified=qualified, written=written))
        return tuple(supertypes)

    @property
    def _candidate_lookups(self) -> JavaCandidateLookups:
        return JavaCandidateLookups(
            self._java_type_variables, self._qualified_parameter_types
        )

    def _qualified_parameter_types(self, method_qn: str) -> tuple[str | None, ...]:
        class_qn = method_qn.split(cs.CHAR_PAREN_OPEN, 1)[0].rpartition(
            cs.SEPARATOR_DOT
        )[0]
        if not (
            module_qn := module_qn_for_entity(class_qn, self.module_qn_to_file_path)
        ):
            return ()
        return tuple(
            self._qualified_type(text, module_qn)
            for text in _java_param_type_texts(method_qn)
        )

    def _qualified_type(self, type_text: str, module_qn: str) -> str | None:
        # The type a name denotes, when that is certain: a registered project
        # type, or a JDK type by its full name. A scoped name resolves through
        # its enclosing type (`Outer.Inner`, `Map.Entry` with `Map` imported);
        # a dotted name the lookups do not place stays unknown.
        ref = self._java_member_type_ref(_element_type_text(type_text), module_qn)
        if self.function_registry.get(ref) in _JAVA_TYPE_NODE_TYPES or ref.startswith(
            cs.JAVA_STDLIB_PREFIXES
        ):
            return ref
        return None

    def _java_supertypes(
        self, type_name: str, module_qn: str, parameters: frozenset[str]
    ) -> JavaSupertypes:
        # The supertypes a value of this static type widens to (JLS 4.10), by
        # simple name, each at its distance up the hierarchy. A project type
        # walks its declared superclass and interfaces, a JDK type the table
        # of common ones. A parameter type the walk misses is ruled out only
        # when nothing is hidden: every type on the walk declares all its
        # supertypes, or a JDK argument meets a project parameter, which no
        # JDK type can extend. Otherwise it stays unproven.
        root = self._java_type_ref(type_name, module_qn)
        depths: dict[str, int] = {}
        refs: dict[str, int] = {}
        frontier = [root]
        seen = set(frontier)
        complete = True
        depth = 0
        while frontier:
            depth += 1
            parents: list[str] = []
            for ref in frontier:
                direct, declared = self._java_direct_supertypes(ref)
                complete = complete and declared
                parents.extend(direct)
            frontier = []
            for parent in parents:
                depths.setdefault(_simple_type_name(parent), depth)
                refs.setdefault(parent, depth)
                if parent not in seen:
                    seen.add(parent)
                    frontier.append(parent)
        missed = parameters - depths.keys()
        walked = JavaSupertypes(
            depths,
            frozenset(),
            refs=refs,
            project=frozenset(
                ref
                for ref in refs
                if self.function_registry.get(ref) in _JAVA_TYPE_NODE_TYPES
            ),
            complete=complete,
        )
        if complete:
            return walked._replace(unreachable=missed)
        if self._is_jdk_type(root):
            return walked._replace(
                unreachable=frozenset(p for p in missed if self._names_project_type(p))
            )
        return walked

    def _is_jdk_type(self, type_ref: str) -> bool:
        # Named in full in a JDK package, and no project type carries the
        # name: a JDK class never extends a type this project declares, while
        # another type from outside the index may (a plugin built against it).
        return (
            type_ref.startswith(cs.JAVA_STDLIB_PREFIXES)
            and self.function_registry.get(type_ref) not in _JAVA_TYPE_NODE_TYPES
            and not self._names_project_type(_simple_type_name(type_ref))
        )

    def _names_project_type(self, simple_name: str) -> bool:
        return any(
            self.function_registry.get(qn) in _JAVA_TYPE_NODE_TYPES
            for qn in self.simple_name_lookup.get(simple_name, ())
        )

    def _java_direct_supertypes(self, type_ref: str) -> tuple[list[str], bool]:
        # A type's direct supertypes, and whether they are all of them.
        if self.function_registry.get(type_ref) not in _JAVA_TYPE_NODE_TYPES or not (
            owner := module_qn_for_entity(type_ref, self.module_qn_to_file_path)
        ):
            return list(
                cs.JAVA_LIBRARY_SUPERTYPES.get(_simple_type_name(type_ref), ())
            ), False
        # class_inheritance holds a class's superclass and an interface's
        # superinterfaces, resolved against the whole repo; the interfaces a
        # class, enum or record implements are read from its declaration.
        parents = list(self.class_inheritance.get(type_ref, ()))
        if (declaration := self._java_type_declaration(type_ref, owner)) is None:
            return parents, False
        parents.extend(
            self._java_member_type_ref(name, owner)
            for name in extract_class_info(declaration)[cs.FIELD_INTERFACES]
        )
        # A supertype written in a form neither reader names (`@A Base`) is
        # one the walk never sees: the list is whole only when it holds as
        # many as the declaration writes.
        return parents, (
            declaration.type in _JAVA_FULLY_DECLARED_TYPES
            and len(parents) == java_written_supertype_count(declaration)
        )

    def _java_member_type_ref(self, type_name: str, module_qn: str) -> str:
        # `_java_type_ref`, which leaves a dotted name as written, plus a
        # member type named through its enclosing one (`Outer.Inner`): the
        # registered type `Outer` names here, then `.Inner`.
        ref = self._java_type_ref(type_name, module_qn)
        outer, dot, member = ref.partition(cs.SEPARATOR_DOT)
        if not dot or self.function_registry.get(ref) in _JAVA_TYPE_NODE_TYPES:
            return ref
        nested = f"{self._java_type_ref(outer, module_qn)}{cs.SEPARATOR_DOT}{member}"
        if self.function_registry.get(
            nested
        ) in _JAVA_TYPE_NODE_TYPES or nested.startswith(cs.JAVA_STDLIB_PREFIXES):
            return nested
        return ref

    def _java_type_variables(self, method_qn: str) -> frozenset[str]:
        # The type variables a method's parameters may name: its own and
        # those of the classes around its declaration.
        class_qn = method_qn.split(cs.CHAR_PAREN_OPEN, 1)[0].rpartition(
            cs.SEPARATOR_DOT
        )[0]
        if (
            not (owner := module_qn_for_entity(class_qn, self.module_qn_to_file_path))
            or (scope := self._java_type_declaration(class_qn, owner)) is None
        ):
            return frozenset()
        names: set[str] = set()
        if (method := self._declared_method(scope, method_qn)) is not None:
            names.update(extract_method_info(method)[cs.KEY_TYPE_PARAMETERS])
        while scope is not None:
            if scope.type in cs.JAVA_CLASS_NODE_TYPES:
                names.update(extract_class_info(scope)[cs.KEY_TYPE_PARAMETERS])
            scope = scope.parent
        return frozenset(names)

    @staticmethod
    def _declared_method(declaration: ASTNode, method_qn: str) -> ASTNode | None:
        # The method or constructor of a type declaration that `method_qn`
        # names, matched by name and parameter types.
        if (body := declaration.child_by_field_name(cs.FIELD_BODY)) is None:
            return None
        name = method_qn.split(cs.CHAR_PAREN_OPEN, 1)[0].rpartition(cs.SEPARATOR_DOT)[2]
        params = _java_param_type_names(method_qn)
        members = list(body.children)
        members.extend(
            member
            for child in body.children
            if child.type == cs.TS_JAVA_ENUM_BODY_DECLARATIONS
            for member in child.children
        )
        for member in members:
            if member.type not in cs.JAVA_METHOD_NODE_TYPES:
                continue
            info = extract_method_info(member)
            if (
                info[cs.KEY_NAME] == name
                and [_simple_type_name(text) for text in info[cs.KEY_PARAMETERS]]
                == params
            ):
                return member
        return None

    def _java_type_ref(self, type_name: str, module_qn: str) -> str:
        # The registered project type `type_name` names from `module_qn`, or
        # the name itself for a JDK or other external type.
        base = type_name.split(cs.CHAR_ANGLE_OPEN, 1)[0].strip()
        resolved = self._resolve_java_type_name(base, module_qn)
        if self.function_registry.get(resolved) in _JAVA_TYPE_NODE_TYPES:
            return resolved
        if resolved == base and (
            sibling := self._same_package_type_qn(base, module_qn)
        ):
            return sibling
        return resolved

    def _same_package_type_qn(self, type_name: str, module_qn: str) -> str | None:
        # A package-private top-level type declared in a sibling file (`class
        # Square` inside Names.java) is visible to its whole package without an
        # import, yet no file is named after it, so the per-file lookups miss
        # it. Used only when exactly one sibling file declares the name.
        if not (current_file := self.module_qn_to_file_path.get(module_qn)):
            return None
        found = [
            qn
            for qn in self.simple_name_lookup.get(type_name, ())
            if self.function_registry.get(qn) in _JAVA_TYPE_NODE_TYPES
            and (owner := module_qn_for_entity(qn, self.module_qn_to_file_path))
            and qn == f"{owner}{cs.SEPARATOR_DOT}{type_name}"
            and self.module_qn_to_file_path[owner].parent == current_file.parent
        ]
        return found[0] if len(found) == 1 else None

    def _java_type_declaration(self, type_qn: str, owner: str) -> ASTNode | None:
        # A registered type's declaration, top-level or nested
        # (`module.Outer.Inner`), read from the tree of its file `owner`.
        if not (entry := self.ast_cache.load(self.module_qn_to_file_path[owner])):
            return None
        scope: ASTNode | None = entry[0]
        declaration: ASTNode | None = None
        for name in type_qn[len(owner) + 1 :].split(cs.SEPARATOR_DOT):
            if scope is None:
                return None
            declaration = next(
                (
                    child
                    for child in scope.children
                    if child.type in cs.JAVA_CLASS_NODE_TYPES
                    and safe_decode_text(child.child_by_field_name(cs.FIELD_NAME))
                    == name
                ),
                None,
            )
            if declaration is None:
                return None
            scope = declaration.child_by_field_name(cs.FIELD_BODY)
        return declaration

    def _search_method_in_alternate_modules(
        self,
        class_qn: str,
        method_name: str,
        current_module_qn: str | None,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
    ) -> tuple[str, str] | None:
        suffixes = class_qn.split(cs.SEPARATOR_DOT)
        lookup_keys = [
            cs.SEPARATOR_DOT.join(suffixes[i:]) for i in range(len(suffixes))
        ] or [class_qn]

        candidate_modules = self._collect_candidate_modules(lookup_keys)
        ranked_candidates = self._rank_module_candidates(
            candidate_modules, class_qn, current_module_qn
        )

        simple_class_name = class_qn.rsplit(cs.SEPARATOR_DOT, maxsplit=1)[-1]

        for module_qn in ranked_candidates:
            registry_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{simple_class_name}"
            if result := self._search_method_in_class(
                registry_class_qn, method_name, arg_count, arg_types
            ):
                return result

        return None

    def _collect_candidate_modules(self, lookup_keys: list[str]) -> list[str]:
        candidate_modules: list[str] = []
        seen_modules: set[str] = set()

        for key in lookup_keys:
            if key in self._fqn_to_module_qn:
                for module_candidate in self._fqn_to_module_qn[key]:
                    if module_candidate not in seen_modules:
                        candidate_modules.append(module_candidate)
                        seen_modules.add(module_candidate)

        return candidate_modules

    def _is_matching_method(self, member: str, method_name: str) -> bool:
        return (
            member == method_name
            or member.startswith(f"{method_name}{cs.CHAR_PAREN_OPEN}")
            or member == f"{method_name}{cs.EMPTY_PARENS}"
        )

    @recursion_guard(
        key_func=lambda self, class_qn, *_, **__: class_qn,
        guard_name=cs.GUARD_INHERITED_METHOD,
    )
    def _find_inherited_method(
        self,
        class_qn: str,
        method_name: str,
        module_qn: str,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
    ) -> tuple[str, str] | None:
        if not (superclass_qn := self._get_superclass_name(class_qn)):
            return None

        if method_result := self._find_method_with_any_signature(
            superclass_qn, method_name, module_qn, arg_count, arg_types
        ):
            return method_result

        return self._find_inherited_method(
            superclass_qn, method_name, module_qn, arg_count, arg_types
        )

    def _find_interface_method(
        self,
        class_qn: str,
        method_name: str,
        module_qn: str,
        arg_count: int | None = None,
        arg_types: tuple[str | None, ...] = (),
    ) -> tuple[str, str] | None:
        for interface_qn in self._get_implemented_interfaces(class_qn):
            if method_result := self._find_method_with_any_signature(
                interface_qn, method_name, module_qn, arg_count, arg_types
            ):
                return method_result

        return None

    def _resolve_java_method_return_type(
        self, method_call: str, module_qn: str
    ) -> str | None:
        if not method_call:
            return None

        parts = method_call.split(cs.SEPARATOR_DOT)
        if len(parts) < 2:
            method_name = method_call
            if (current_class_qn := self._get_current_class_name(module_qn)) and (
                result := self._find_method_return_type(current_class_qn, method_name)
            ):
                return result
        else:
            object_part = cs.SEPARATOR_DOT.join(parts[:-1])
            method_name = parts[-1]

            if object_part in self.function_registry:
                return self._find_method_return_type(object_part, method_name)

            if object_type := self._lookup_variable_type(object_part, module_qn):
                return self._find_method_return_type(object_type, method_name)

            potential_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{object_part}"
            if potential_class_qn in self.function_registry:
                return self._find_method_return_type(potential_class_qn, method_name)

        return self._heuristic_method_return_type(method_call)

    def _find_method_return_type(
        self,
        class_qn: str,
        method_name: str,
        param_types: tuple[str, ...] = (),
    ) -> str | None:
        if not class_qn or not method_name:
            return None

        ctx = get_class_context_from_qn(
            class_qn, self.module_qn_to_file_path, self.ast_cache
        )
        if not ctx:
            return None

        return self._find_method_return_type_in_ast(
            ctx.root_node,
            ctx.target_class_name,
            method_name,
            ctx.module_qn,
            param_types,
        )

    def _find_method_return_type_in_ast(
        self,
        node: ASTNode,
        class_name: str,
        method_name: str,
        module_qn: str,
        param_types: tuple[str, ...] = (),
    ) -> str | None:
        # Interfaces, enums and records declare methods too, and instance-method
        # lookup already binds through them, so a chain whose inner call is
        # declared on one must be typeable the same way.
        if node.type in cs.JAVA_CLASS_NODE_TYPES:
            if (
                name_node := node.child_by_field_name(cs.KEY_NAME)
            ) and safe_decode_text(name_node) == class_name:
                if body_node := node.child_by_field_name(cs.FIELD_BODY):
                    return self._search_methods_in_class_body(
                        body_node, method_name, module_qn, param_types
                    )

        for child in node.children:
            if result := self._find_method_return_type_in_ast(
                child, class_name, method_name, module_qn, param_types
            ):
                return result

        return None

    def _search_methods_in_class_body(
        self,
        body_node: ASTNode,
        method_name: str,
        module_qn: str,
        param_types: tuple[str, ...] = (),
    ) -> str | None:
        named = [
            child
            for child in body_node.children
            if child.type == cs.TS_METHOD_DECLARATION
            and (name_node := child.child_by_field_name(cs.KEY_NAME)) is not None
            and safe_decode_text(name_node) == method_name
        ]
        chosen = _pick_declared_overload(named, param_types)
        if chosen is None:
            return None
        if (type_node := chosen.child_by_field_name(cs.KEY_TYPE)) and (
            return_type := safe_decode_text(type_node)
        ):
            return self._resolve_java_type_name(return_type, module_qn)
        return None

    def _heuristic_method_return_type(self, method_call: str) -> str | None:
        method_lower = method_call.lower()
        if cs.JAVA_GETTER_PATTERN in method_lower and (
            getter_type := _java_getter_return_type(method_lower)
        ):
            return getter_type

        if (
            cs.JAVA_CREATE_PATTERN in method_lower
            or cs.JAVA_NEW_PATTERN in method_lower
        ) and (factory_type := _java_factory_return_type(method_call)):
            return factory_type

        if cs.JAVA_IS_PATTERN in method_lower or cs.JAVA_HAS_PATTERN in method_lower:
            return cs.JAVA_TYPE_BOOLEAN

        return None

    def _infer_arg_types(
        self, call_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> tuple[str | None, ...]:
        # Infer the simple type of each argument so same-arity overloads can be told
        # apart (isX(String) vs isX(Class)). An argument whose type cannot be
        # inferred is None (unknown), which _overload_rank treats as
        # a wildcard.
        args_node = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        if not args_node:
            return ()
        arg_types: list[str | None] = []
        for child in args_node.children:
            if child.type in cs.DELIMITER_TOKENS:
                continue
            if child.type != cs.TS_IDENTIFIER:
                # A literal carries its type in its node type, and a `new T()`
                # or a call carries it in the expression: the shared inference
                # types all of them (issue #1344).
                arg_types.append(
                    self._infer_java_type_from_expression(
                        child, module_qn, local_var_types
                    )
                )
                continue
            name = safe_decode_text(child)
            var_type = local_var_types.get(name) if name else None
            if not var_type and name:
                var_type = self._lookup_variable_type(name, module_qn)
            arg_types.append(var_type or None)
        return tuple(arg_types)

    @staticmethod
    def _has_call_receiver(call_node: ASTNode) -> bool:
        receiver = call_node.child_by_field_name(cs.TS_FIELD_OBJECT)
        return receiver is not None and receiver.type == cs.TS_METHOD_INVOCATION

    def _chained_receiver_type(
        self,
        call_node: ASTNode,
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> str | None:
        receiver = call_node.child_by_field_name(cs.TS_FIELD_OBJECT)
        if receiver is None:
            return None
        # Recursion walks the chain leftwards; the leftmost receiver is an
        # identifier or field access, which the existing paths already type.
        resolved = self._do_resolve_java_method_call(
            receiver, local_var_types, module_qn
        )
        if not resolved:
            return None
        return self._declared_return_type_of(resolved[1])

    def _declared_return_type_of(self, method_qn: str) -> str | None:
        open_idx = method_qn.find(cs.CHAR_PAREN_OPEN)
        unsignatured = method_qn[:open_idx] if open_idx >= 0 else method_qn
        class_qn, _, method_name = unsignatured.rpartition(cs.SEPARATOR_DOT)
        if not class_qn or not method_name:
            return None
        # The signature of the OVERLOAD the inner call resolved to: overloads
        # may declare different return types, and a name-only lookup would type
        # the chain from whichever one is declared first.
        return self._find_method_return_type(
            class_qn, method_name, tuple(_java_param_type_names(method_qn))
        )

    @depth_guard(
        max_depth=cs.JAVA_MAX_INFERENCE_DEPTH,
        guard_name=cs.GUARD_JAVA_INFERENCE_DEPTH,
    )
    def _do_resolve_java_method_call(
        self,
        call_node: ASTNode,
        local_var_types: dict[str, str],
        module_qn: str,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        if call_node.type != cs.TS_METHOD_INVOCATION:
            return None

        call_info = extract_method_call_info(call_node)
        if not call_info:
            return None

        method_name = call_info[cs.FIELD_NAME]
        object_ref = call_info[cs.FIELD_OBJECT]
        arg_count = call_info[cs.FIELD_ARGUMENTS]
        arg_types = self._infer_arg_types(call_node, local_var_types, module_qn)

        if not method_name:
            logger.debug(ls.JAVA_NO_METHOD_NAME)
            return None

        logger.debug(ls.JAVA_RESOLVING_CALL, method=method_name, object=object_ref)

        # A chained step (`from(..).where(..)`) has a method_invocation receiver,
        # which carries no name to look up. Typing it means resolving the inner
        # call first and reading its DECLARED return type -- the same thing the
        # compiler does, and what makes `return this;` builders resolve.
        if self._has_call_receiver(call_node):
            if chained_type := self._chained_receiver_type(
                call_node, local_var_types, module_qn
            ):
                logger.debug(ls.JAVA_OBJ_TYPE_RESOLVED, type=chained_type)
                return self._resolve_instance_method(
                    chained_type, str(method_name), module_qn, arg_count, arg_types
                )
            # An untypeable receiver (a call into a third-party type) leaves the
            # step unresolved. It must not reach the unqualified path below:
            # `expr().m()` is never `this.m()`, and that scan binds by name
            # alone, which is how the chain used to land on an unrelated class.
            logger.debug(ls.JAVA_OBJ_TYPE_UNKNOWN, object=method_name)
            return None

        if not object_ref:
            return self._resolve_unqualified_java_call(
                call_node, str(method_name), module_qn, arg_count, arg_types, caller_qn
            )

        logger.debug(ls.JAVA_RESOLVING_OBJ_TYPE, object=object_ref)
        if not (
            object_type := self._resolve_java_object_type(
                str(object_ref), local_var_types, module_qn, call_node
            )
        ):
            logger.debug(ls.JAVA_OBJ_TYPE_UNKNOWN, object=object_ref)
            return None

        logger.debug(ls.JAVA_OBJ_TYPE_RESOLVED, type=object_type)
        result = self._resolve_instance_method(
            object_type, str(method_name), module_qn, arg_count, arg_types
        )
        if result:
            logger.debug(ls.JAVA_FOUND_INSTANCE, result=result)
        else:
            logger.debug(
                ls.JAVA_INSTANCE_NOT_FOUND, type=object_type, method=method_name
            )
        return result
