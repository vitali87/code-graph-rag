"""A function's identity: the qualified name, name and export the function
pass registers it under.

`FunctionIngest` resolves every function it writes through the
`FunctionNaming` it owns. A language with a qualified-name spec in
`LANGUAGE_FQN_SPECS` is named from the scopes around the function; any other
is named by its collaborator when that resolves its own identity, or else by
the walk up the function's ancestors, which spells each enclosing function
and class and gives an anonymous function a name built from where it sits.
It calls back through `FunctionNamingHost` for the collaborator of a
function's language. What reads nothing of the pass, a function's own name
and the anonymous names, is module functions here."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LANGUAGE_FQN_SPECS, LanguageSpec
from ..node_text import safe_decode_text
from ..utils.fqn_resolver import scoped_name_parts
from . import export_detection
from .function_identity import FunctionResolution
from .js_ts import utils as js_ts_utils
from .language_definitions import LanguageFunctionIngest
from .utils import warn_if_name_truncated


def extract_function_name(func_node: Node) -> str | None:
    name_node = func_node.child_by_field_name(cs.FIELD_NAME)
    if name_node and name_node.text:
        return safe_decode_text(name_node)

    # An anonymous arrow / function expression bound to a name (a
    # `const f = () => ...` declarator OR a `helper = () => ...` class field)
    # takes that binding name, in lock-step with the call pass's
    # binding-name climb (they must agree or the caller qn is a phantom and
    # the arrow's body callbacks report dead). One shared helper keeps both
    # passes' naming identical.
    return js_ts_utils.arrow_binding_name(func_node)


def generate_anonymous_function_name(func_node: Node) -> str:
    parent = func_node.parent
    if parent and parent.type == cs.TS_PARENTHESIZED_EXPRESSION:
        grandparent = parent.parent
        if (
            grandparent
            and grandparent.type == cs.TS_CALL_EXPRESSION
            and grandparent.child_by_field_name(cs.FIELD_FUNCTION) == parent
        ):
            func_type = (
                cs.PREFIX_ARROW
                if func_node.type == cs.TS_ARROW_FUNCTION
                else cs.PREFIX_FUNC
            )
            return f"{cs.PREFIX_IIFE}{func_type}_{func_node.start_point[0]}_{func_node.start_point[1]}"

    if (
        parent
        and parent.type == cs.TS_CALL_EXPRESSION
        and parent.child_by_field_name(cs.FIELD_FUNCTION) == func_node
    ):
        return f"{cs.PREFIX_IIFE_DIRECT}{func_node.start_point[0]}_{func_node.start_point[1]}"

    return f"{cs.PREFIX_ANONYMOUS}{func_node.start_point[0]}_{func_node.start_point[1]}"


def extract_node_name(node: Node) -> str | None:
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    if name_node and name_node.text is not None:
        return safe_decode_text(name_node)
    return None


def format_nested_qn(module_qn: str, path_parts: list[str], func_name: str) -> str:
    if path_parts:
        return f"{module_qn}.{cs.SEPARATOR_DOT.join(path_parts)}.{func_name}"
    return f"{module_qn}.{func_name}"


class FunctionNamingHost(Protocol):
    """What the naming of a function calls on the function pass that owns
    it: the function collaborator of a language."""

    def _for(
        self, language: cs.SupportedLanguage | str | None
    ) -> LanguageFunctionIngest: ...


class FunctionNaming:
    """Resolves the identity a function registers under.

    Built and owned by `FunctionIngest`."""

    __slots__ = ("_host",)

    def __init__(self, host: FunctionNamingHost) -> None:
        self._host = host

    def resolve_identity(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        file_path: Path | None,
    ) -> FunctionResolution | None:
        resolution = self._try_unified_fqn_resolution(
            func_node, module_qn, language, file_path
        )
        if resolution:
            return resolution

        return self._fallback_function_resolution(
            func_node, module_qn, language, lang_config
        )

    def _try_unified_fqn_resolution(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        file_path: Path | None,
    ) -> FunctionResolution | None:
        fqn_config = LANGUAGE_FQN_SPECS.get(language)
        if not fqn_config or not file_path:
            return None

        func_name = fqn_config.get_name(func_node)
        if not func_name:
            return None
        warn_if_name_truncated(func_node, func_name, file_path)

        parts = [
            *scoped_name_parts(func_node.parent, fqn_config, module_qn, file_path),
            func_name,
        ]

        # Prefix with the module's resolved (collision-disambiguated) qn rather
        # than recomputing from the path, so same-stem cross-language siblings stay
        # distinct.
        func_qn = module_qn + cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(parts)
        # A plain dotted name (Lua `M.foo`) keeps only its final segment, as
        # the qn rsplit always did. A computed bracket name is different: its
        # dot belongs to the name itself, and re-splitting registered an
        # object-literal `[Symbol.iterator]` member as `iterator]`
        # (issue #998).
        simple_name = (
            func_name
            if func_name.startswith(cs.JS_COMPUTED_NAME_PREFIX)
            and func_name.endswith(cs.JS_COMPUTED_NAME_SUFFIX)
            else func_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        )

        is_exported = export_detection.is_exported(func_node, simple_name, language)
        return FunctionResolution(func_qn, simple_name, is_exported)

    def _fallback_function_resolution(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> FunctionResolution | None:
        if (language_ingest := self._host._for(language)).resolves_own_identity:
            return language_ingest.resolve_function(func_node, module_qn)
        return self._resolve_generic_function(
            func_node, module_qn, language, lang_config
        )

    def _resolve_generic_function(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> FunctionResolution:
        func_name = extract_function_name(func_node)
        display_name: str | None = None
        if not func_name:
            func_name, display_name = self._host._for(language).nameless_function_name(
                func_node
            )

        is_anonymous = not func_name
        if not func_name:
            func_name = generate_anonymous_function_name(func_node)

        func_qn = self._build_function_qn(
            func_node, module_qn, func_name, language, lang_config
        )
        is_exported = export_detection.is_exported(func_node, func_name, language)
        return FunctionResolution(
            func_qn, display_name or func_name, is_exported, is_anonymous
        )

    def _build_function_qn(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> str:
        if (
            qualified_name := self._host._for(language).qualified_name(
                func_node, module_qn, func_name
            )
        ) is not None:
            return qualified_name

        nested_qn = self.build_nested_qualified_name(
            func_node, module_qn, func_name, lang_config
        )
        return nested_qn or f"{module_qn}.{func_name}"

    def build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
        skip_classes: bool = False,
    ) -> str | None:
        current = func_node.parent
        if not isinstance(current, Node):
            logger.warning(
                ls.CALL_UNEXPECTED_PARENT.format(
                    node=func_node, parent_type=type(current)
                )
            )
            return None

        path_parts = self.collect_ancestor_path_parts(
            func_node, current, lang_config, skip_classes
        )
        if path_parts is None:
            return None

        return format_nested_qn(module_qn, path_parts, func_name)

    def collect_ancestor_path_parts(
        self,
        func_node: Node,
        current: Node | None,
        lang_config: LanguageSpec,
        skip_classes: bool,
    ) -> list[str] | None:
        path_parts: list[str] = []
        language_ingest = self._host._for(lang_config.language)

        while current and current.type not in lang_config.module_node_types:
            result = self._process_ancestor_for_path(
                func_node, current, lang_config, skip_classes
            )
            if result is False:
                return None
            if result is not None:
                path_parts.append(result)
            # A part can already spell the scopes above it (a PHP anonymous
            # class's spells the callable it is written in), so the walk
            # resumes where its language says.
            current = (
                language_ingest.next_path_ancestor(current)
                if result is not None
                else current.parent
            )

        path_parts.reverse()
        return path_parts

    def _process_ancestor_for_path(
        self,
        func_node: Node,
        current: Node,
        lang_config: LanguageSpec,
        skip_classes: bool,
    ) -> str | None | Literal[False]:
        if current.type in lang_config.function_node_types:
            return self._get_name_from_function_ancestor(current)

        if current.type in lang_config.class_node_types:
            return self._handle_class_ancestor(
                func_node, current, skip_classes, lang_config
            )

        if current.type == cs.TS_METHOD_DEFINITION:
            return extract_node_name(current)

        return None

    @staticmethod
    def _get_name_from_function_ancestor(node: Node) -> str | None:
        if name := extract_node_name(node):
            return name
        return extract_function_name(node)

    def _handle_class_ancestor(
        self,
        func_node: Node,
        class_node: Node,
        skip_classes: bool,
        lang_config: LanguageSpec,
    ) -> str | None | Literal[False]:
        if skip_classes:
            return None
        # A function that is a DIRECT class member is a method, handled by the
        # class-ingest path: return False so the whole qn collapses to the flat
        # module.name form that path expects. But a function NESTED inside a method
        # body (a Promise executor `new Promise(cb)`, a defineProperty getter, any
        # closure) must keep the full class.method.<name> path so the call pass,
        # which always builds that path, references the same node; otherwise the
        # closure is orphaned and reports as dead.
        if self._is_nested_within_class_member(func_node, class_node, lang_config):
            if name := extract_node_name(class_node):
                return name
            # A class with no `name` field (a PHP anonymous class, a JS class
            # expression) is named the way its language names it.
            return self._host._for(lang_config.language).anonymous_class_name(
                class_node
            )
        return False

    @staticmethod
    def _is_nested_within_class_member(
        func_node: Node, class_node: Node, lang_config: LanguageSpec
    ) -> bool:
        # True when a function/method boundary sits between func_node and its
        # enclosing class, i.e. func_node lives inside a member's body rather than
        # being the member itself. A direct method has no such intervening boundary
        # (its parent chain reaches the class body directly).
        current = func_node.parent
        while current is not None and current != class_node:
            if (
                current.type == cs.TS_METHOD_DEFINITION
                or current.type in lang_config.function_node_types
            ):
                return True
            current = current.parent
        return False
