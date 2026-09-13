from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as lg
from ...types_defs import (
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    SimpleNameLookup,
)
from ..import_processor import ImportProcessor
from .ast_analyzer import PythonAstAnalyzerMixin
from .expression_analyzer import PythonExpressionAnalyzerMixin
from .variable_analyzer import PythonVariableAnalyzerMixin

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ..factory import ASTCacheProtocol
    from ..js_ts import JsTypeInferenceEngine


class PythonTypeInferenceEngine(
    PythonExpressionAnalyzerMixin,
    PythonAstAnalyzerMixin,
    PythonVariableAnalyzerMixin,
):
    __slots__ = (
        "import_processor",
        "function_registry",
        "repo_path",
        "project_name",
        "ast_cache",
        "queries",
        "module_qn_to_file_path",
        "class_inheritance",
        "simple_name_lookup",
        "_js_type_inference_getter",
        "_method_return_type_cache",
        "_type_inference_in_progress",
        "_available_classes_cache",
        "_return_stmt_cache",
        "_self_assignment_cache",
        "_class_member_type_cache",
    )

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        repo_path: Path,
        project_name: str,
        ast_cache: ASTCacheProtocol,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        module_qn_to_file_path: dict[str, Path],
        class_inheritance: dict[str, list[str]],
        simple_name_lookup: SimpleNameLookup,
        js_type_inference_getter: Callable[[], JsTypeInferenceEngine],
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.repo_path = repo_path
        self.project_name = project_name
        self.ast_cache = ast_cache
        self.queries = queries
        self.module_qn_to_file_path = module_qn_to_file_path
        self.class_inheritance = class_inheritance
        self.simple_name_lookup = simple_name_lookup
        self._js_type_inference_getter = js_type_inference_getter

        self._method_return_type_cache: dict[str, str | None] = {}
        self._type_inference_in_progress: set[str] = set()
        self._available_classes_cache: dict[str, list[str]] = {}
        # Keyed by the Node itself, never id(node): Node hashes by its tree-sitter
        # identity, while a freed wrapper's id() is reused by unrelated nodes,
        # producing stale hits that vary with memory layout (nondeterministic graphs,
        # caught by the determinism test). Keys hold the node, so entries never collide.
        self._return_stmt_cache: dict[Node, list] = {}
        self._self_assignment_cache: dict[tuple[Node, str], dict[str, str] | None] = {}
        self._class_member_type_cache: dict[str, dict[str, str]] = {}

    @staticmethod
    def _first_parameter_name(def_node: Node) -> str | None:
        params = def_node.child_by_field_name(cs.FIELD_PARAMETERS)
        first = params.named_children[0] if params and params.named_children else None
        if first is not None and first.type != cs.TS_PY_IDENTIFIER:
            first = next(
                (c for c in first.named_children if c.type == cs.TS_PY_IDENTIFIER), None
            )
        if first is None or first.text is None:
            return None
        return first.text.decode(cs.ENCODING_UTF8)

    @staticmethod
    def _parameter_names(def_node: Node) -> set[str]:
        params = def_node.child_by_field_name(cs.FIELD_PARAMETERS)
        names: set[str] = set()
        for param in params.named_children if params else []:
            ident = (
                param
                if param.type == cs.TS_PY_IDENTIFIER
                else next(
                    (c for c in param.named_children if c.type == cs.TS_PY_IDENTIFIER),
                    None,
                )
            )
            if ident is not None and ident.text is not None:
                names.add(ident.text.decode(cs.ENCODING_UTF8))
        return names

    @staticmethod
    def _is_static_method(def_node: Node) -> bool:
        parent = def_node.parent
        if parent is None or parent.type != cs.TS_PY_DECORATED_DEFINITION:
            return False
        for child in parent.named_children:
            if child.type == cs.TS_PY_DECORATOR and child.text is not None:
                text = child.text.decode(cs.ENCODING_UTF8).lstrip("@").strip()
                if text.split("(", 1)[0].rsplit(".", 1)[-1] in cs.STATIC_DECORATORS:
                    return True
        return False

    @staticmethod
    def _binds_name(target: Node, name: str) -> bool:
        """Whether a binding target (an identifier, or a pattern holding one) binds `name`."""
        stack = [target]
        while stack:
            node = stack.pop()
            if (
                node.type == cs.TS_PY_IDENTIFIER
                and node.text is not None
                and node.text.decode(cs.ENCODING_UTF8) == name
            ):
                return True
            stack.extend(node.named_children)
        return False

    @classmethod
    def _rebinds(cls, def_node: Node, name: str) -> bool:
        """Whether the def's OWN body rebinds `name` in any binding form.

        Assignment and augmented assignment (`self = pick()`, `a, self = pair`),
        a `for self in ...` target, a `with ... as self` / `except ... as self`
        alias and a walrus `(self := pick())`. Nested defs, classes and lambdas
        are not descended into: a binding there belongs to that scope, not to
        this receiver.
        """
        stack = list(def_node.named_children)
        while stack:
            node = stack.pop()
            if node.type in (
                cs.TS_PY_FUNCTION_DEFINITION,
                cs.TS_PY_CLASS_DEFINITION,
                cs.TS_PY_LAMBDA,
            ):
                continue
            target: Node | None = None
            if node.type in (
                cs.TS_PY_ASSIGNMENT,
                cs.TS_PY_AUGMENTED_ASSIGNMENT,
                cs.TS_PY_FOR_STATEMENT,
            ):
                target = node.child_by_field_name(cs.FIELD_LEFT)
            elif node.type == cs.TS_PY_NAMED_EXPRESSION:
                target = node.child_by_field_name(cs.FIELD_NAME)
            elif node.type == cs.TS_PY_AS_PATTERN:
                target = node.child_by_field_name(cs.FIELD_ALIAS)
            if target is not None and cls._binds_name(target, name):
                return True
            stack.extend(node.named_children)
        return False

    @classmethod
    def _receiver_parameter_names(cls, caller_node: Node) -> list[str]:
        """The bound receiver (`self` or `cls`) visible to `caller_node`, or nothing.

        Only the method DIRECTLY in the class body has a bound receiver, and
        only when it is not a staticmethod: its first parameter, named `self`
        or `cls`. A staticmethod's `self` or a nested def's own `self`
        parameter is a caller-supplied value of unknown type. A closure inside
        a method sees the method's receiver unless one of the defs between
        them declares a parameter of that name (shadowing). A def that
        rebinds the receiver in its body (`self = pick()`) gets no seed: the
        alias pass yields to an existing entry, so the seed would have kept
        the class type over the factory's.
        """
        chain: list[Node] = []
        node: Node | None = caller_node
        while node is not None and node.type != cs.TS_PY_CLASS_DEFINITION:
            if node.type == cs.TS_PY_FUNCTION_DEFINITION:
                chain.append(node)
            node = node.parent
        if node is None or not chain:
            return []  # not inside a class at all
        method = chain[-1]
        if cls._is_static_method(method):
            return []
        receiver = cls._first_parameter_name(method)
        if receiver not in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS):
            return []
        for inner in chain[:-1]:
            if receiver in cls._parameter_names(inner):
                return []
        if any(cls._rebinds(d, receiver) for d in chain):
            return []
        return [receiver]

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str, class_context: str | None = None
    ) -> dict[str, str]:
        local_var_types: dict[str, str] = {}

        try:
            self._infer_parameter_types(caller_node, local_var_types, module_qn)
            # A method's `self` (a classmethod's `cls`) IS the enclosing
            # class. Seeded HERE, before the assignment walk, because that
            # walk types `w = self.parse()` through the receiver's entry:
            # without one the local stayed untyped and a later `w.render()`
            # fell to the bare-name fallback (issue #1901). Only a name that
            # is the FIRST PARAMETER of this def or of an enclosing def under
            # the class is seeded: a staticmethod has neither, and a body
            # binding `cls = pick()` in any def must keep the type the alias
            # pass gives it, which it could not with a seed already present
            # (that pass yields to an existing entry). The seed is an aid to
            # the walk only and is removed again below: `self.m()` CALLS keep
            # their own resolution (the concrete-sibling-over-abstract-stub
            # policy the resolver applies to self calls), which a typed `self`
            # in the returned map would override. An annotated parameter of
            # that name is kept as it is.
            seeded: list[str] = []
            if class_context:
                for name in self._receiver_parameter_names(caller_node):
                    if name not in local_var_types:
                        local_var_types[name] = class_context
                        seeded.append(name)
            # Single-pass traversal avoids O(5*N) traversals for type inference.
            comprehensions, for_statements = self._traverse_single_pass(
                caller_node, local_var_types, module_qn
            )
            self._infer_instance_attributes_from_init(
                caller_node, local_var_types, module_qn
            )
            self._infer_property_return_types(caller_node, local_var_types, module_qn)
            self._infer_class_annotation_types(caller_node, local_var_types, module_qn)
            # Attribute-backed iterables (`for w in self.widgets`) only type
            # after the attribute passes above populated `self.x`; re-running
            # the loop analyzers picks them up (they never downgrade a type).
            for comp in comprehensions:
                self._analyze_comprehension(comp, local_var_types, module_qn)
            for for_stmt in for_statements:
                self._analyze_for_loop(for_stmt, local_var_types, module_qn)
            aliases = self._collect_local_aliases(caller_node)
            self._expand_chained_attribute_types(local_var_types, module_qn, aliases)
            # The seed itself goes (a body binding that replaced it stays), and
            # so do the `cls.<field>` entries the chained-attribute pass
            # derives from a seeded `cls`, which no pass produces otherwise.
            # `self.<attr>` entries are KEPT: the instance-attribute passes
            # produce them without any seed and the property / chained-
            # attribute resolution reads them, so removing them broke it.
            cls_prefix = f"{cs.PY_KEYWORD_CLS}{cs.SEPARATOR_DOT}"
            for key in [
                k
                for k in local_var_types
                if (k in seeded and local_var_types[k] == class_context)
                or (cs.PY_KEYWORD_CLS in seeded and k.startswith(cls_prefix))
            ]:
                del local_var_types[key]

        except Exception as e:
            logger.debug(lg.PY_BUILD_VAR_MAP_FAILED, error=e)

        return local_var_types
