from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...types_defs import (
    ASTNode,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    LanguageQueries,
    SimpleNameLookup,
)
from ..frontends.protocol import CallSiteKey, ResolvedCallSite
from ..import_processor import ImportProcessor
from ..semantic_call_join import call_site_key, resolve_call_site
from ..utils import safe_decode_text
from .method_resolver import JavaMethodResolverMixin
from .type_resolver import JavaClassHeaders, JavaTypeResolverMixin
from .utils import find_package_start_index
from .variable_analyzer import JavaVariableAnalyzerMixin

if TYPE_CHECKING:
    from ..factory import ASTCacheProtocol


# The sentinel a proven-external call resolves to: not a target, an instruction
# to emit no first-party edge at all.
JAVA_EXTERNAL_TARGET: tuple[str, str] = ("", "")


def _capture_site(scope_node: ASTNode) -> tuple[ASTNode, ASTNode, ASTNode] | None:
    """For a method of an anonymous or local class: the method whose locals
    it captures, the node that creates or declares the class (where the
    captures are read), and the class body. None for a method of a member or
    top-level class."""
    body = scope_node.parent
    if body is None or body.type != cs.TS_CLASS_BODY or body.parent is None:
        return None
    # The class body belongs to `new T() { ... }` or to a class declaration;
    # above either, a method comes before any named type only when the class
    # is declared inside that method.
    site = body.parent
    current = site.parent
    while current is not None:
        if current.type in cs.JAVA_METHOD_NODE_TYPES:
            return current, site, body
        if current.type in cs.JAVA_CLASS_NODE_TYPES:
            return None
        current = current.parent
    return None


def _declared_field_names(class_body: ASTNode) -> set[str]:
    return {
        name
        for field in class_body.children
        if field.type == cs.TS_FIELD_DECLARATION
        for declarator in field.children_by_field_name(cs.FIELD_DECLARATOR)
        if (name_node := declarator.child_by_field_name(cs.FIELD_NAME))
        and (name := safe_decode_text(name_node))
    }


def _capture_base_name(site: ASTNode) -> str | None:
    """The superclass the class created or declared at `site` names: `Base`
    in `new Base() { ... }` or `class Local extends Base { ... }`."""
    if site.type == cs.TS_OBJECT_CREATION_EXPRESSION:
        type_node = site.child_by_field_name(cs.FIELD_TYPE)
    elif (clause := site.child_by_field_name(cs.FIELD_SUPERCLASS)) is not None:
        type_node = clause.named_child(0)
    else:
        return None
    text = safe_decode_text(type_node) if type_node is not None else None
    return text.split(cs.CHAR_ANGLE_OPEN, 1)[0] if text else None


def _encloses(container: ASTNode, node: ASTNode) -> bool:
    return (
        container.start_byte <= node.start_byte and node.end_byte <= container.end_byte
    )


def _nodes_of_type(root: ASTNode, node_type: str) -> list[ASTNode]:
    found: list[ASTNode] = []
    pending = [root]
    while pending:
        current = pending.pop()
        if current.type == node_type:
            found.append(current)
        pending.extend(reversed(current.children))
    return found


class JavaTypeInferenceEngine(
    JavaTypeResolverMixin,
    JavaVariableAnalyzerMixin,
    JavaMethodResolverMixin,
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
        "_lookup_cache",
        "_lookup_in_progress",
        "_fqn_to_module_qn",
        "class_owner_module",
        "rehydrated_definition_paths",
        "java_call_sites",
        "java_external_sites",
        "function_locations",
        "_rel_to_module",
        "_family_picks",
        "_class_headers",
        "_supertype_walks",
    )

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        repo_path: Path,
        project_name: str,
        ast_cache: "ASTCacheProtocol",
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        module_qn_to_file_path: dict[str, Path],
        class_inheritance: dict[str, list[str]],
        simple_name_lookup: SimpleNameLookup,
        class_owner_module: dict[str, str] | None = None,
        rehydrated_definition_paths: dict[str, str] | None = None,
        java_call_sites: dict[CallSiteKey, ResolvedCallSite] | None = None,
        java_external_sites: set[CallSiteKey] | None = None,
        function_locations: dict[FunctionSpanKey, FunctionLocation] | None = None,
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
        # Shared reference (as with class_inheritance): {class qn: module
        # qn of the file that declared it}, written at class ingest
        # (issue #1772). The variable analyzer reads it to tell the file
        # that declares a nested class from a same-named file in a
        # subpackage (Greptile P1 on #3525).
        self.class_owner_module = (
            class_owner_module if class_owner_module is not None else {}
        )
        # Shared reference (as with class_owner_module): {class qn: the
        # repo-relative path its definition was rehydrated from}, written
        # when a row is read back from the graph. A rehydrated class has
        # no `class_owner_module` entry, so the variable analyzer reads
        # this record to tell the file that declares a nested class from
        # a same-named file in a subpackage on an incremental run
        # (Greptile P1 on #3525).
        self.rehydrated_definition_paths = (
            rehydrated_definition_paths
            if rehydrated_definition_paths is not None
            else {}
        )

        self.java_call_sites = java_call_sites if java_call_sites is not None else {}
        self.java_external_sites = (
            java_external_sites if java_external_sites is not None else set()
        )
        self.function_locations = (
            function_locations if function_locations is not None else {}
        )
        self._rel_to_module: dict[str, str] = {}
        self._family_picks: dict[str, list[tuple[str, str]]] = {}

        self._lookup_cache: dict[str, str | None] = {}
        self._lookup_in_progress: set[str] = set()
        self._class_headers: dict[Path, JavaClassHeaders] = {}
        self._supertype_walks: dict[str, tuple[str, ...]] = {}

        self._fqn_to_module_qn: dict[str, list[str]] = self._build_fqn_lookup_map()

    def reset_resolution_memos(self) -> None:
        # A file re-parsed since the last call pass has a new tree to read
        # its class headers and implemented interfaces from.
        self._class_headers.clear()
        self._supertype_walks.clear()

    def resolve_java_call_site(
        self, call_node: ASTNode, module_qn: str
    ) -> tuple[str, str] | None:
        # The javac path: a HIT is the declaration the compiler bound this call
        # to (the overload argument types select, which name-and-arity matching
        # cannot), and the external sentinel is its proof that the call leaves
        # the repo. Any miss returns None so the caller falls back to the
        # tree-sitter heuristics.
        return resolve_call_site(
            call_node,
            module_qn,
            self._java_call_site_key,
            self.java_call_sites,
            self.java_external_sites,
            JAVA_EXTERNAL_TARGET,
            self.function_locations,
            self.module_qn_to_file_path,
            self.repo_path,
            self._rel_to_module,
        )

    def _java_call_site_key(
        self, call_node: ASTNode, module_qn: str
    ) -> CallSiteKey | None:
        # The callee NAME token, matching the tool's own choice: the `name`
        # field of method_invocation carries it for both `m()` and `x.m()`.
        name_node = call_node.child_by_field_name(cs.TS_FIELD_NAME)
        if name_node is None:
            return None
        name = safe_decode_text(name_node)
        if not name:
            return None
        return call_site_key(
            name_node, name, module_qn, self.module_qn_to_file_path, self.repo_path
        )

    def _build_fqn_lookup_map(self) -> dict[str, list[str]]:
        fqn_map: dict[str, list[str]] = {}

        def _add_mapping(key: str, value: str) -> None:
            modules = fqn_map.setdefault(key, [])
            if value not in modules:
                modules.append(value)

        for module_qn in self.module_qn_to_file_path.keys():
            parts = module_qn.split(cs.SEPARATOR_DOT)
            # Without a recognised src/main/java layout find_package_start_index
            # returns None, leaving the whole map empty so cross-file Java resolution
            # (static calls, instance dispatch in sibling files) silently fails. Fall
            # back to the segment after the project root (index 1) so flat /
            # non-standard layouts still register their simple class names.
            # find_package_start_index never returns 0.
            package_start_idx = find_package_start_index(parts) or 1
            if simple_class_name := cs.SEPARATOR_DOT.join(parts[package_start_idx:]):
                _add_mapping(simple_class_name, module_qn)

                class_parts = simple_class_name.split(cs.SEPARATOR_DOT)
                for j in range(1, len(class_parts)):
                    suffix = cs.SEPARATOR_DOT.join(class_parts[j:])
                    _add_mapping(suffix, module_qn)

        return fqn_map

    def build_variable_type_map(
        self, scope_node: ASTNode, module_qn: str
    ) -> dict[str, str]:
        local_var_types: dict[str, str] = {}

        try:
            # A method of an anonymous or local class sees the parameters and
            # locals of the method that declares the class (effectively final
            # captures, so their declared types hold): `codec.encode(in)` in a
            # `new Handler() { ... }` bound nothing where the same call from a
            # lambda bound exactly (issue #2937). They are seeded first, so the
            # method's own locals infer through them (`var alias = codec;`) and
            # its own names shadow them (bot review on PR #2991).
            self._seed_captures(scope_node, local_var_types, module_qn)
            self._collect_all_variable_types(scope_node, local_var_types, module_qn)
            logger.debug(ls.JAVA_VAR_TYPE_MAP_BUILT, count=len(local_var_types))

        except Exception as e:
            logger.error(ls.JAVA_VAR_TYPE_MAP_FAILED, error=e)

        return local_var_types

    def _seed_captures(
        self, scope_node: ASTNode, types: dict[str, str], module_qn: str
    ) -> None:
        if (capture := _capture_site(scope_node)) is None:
            return
        enclosing, site, body = capture
        # A field the class declares hides a captured local of the same name
        # (JLS 6.4.1), so `codec` in its methods is the field, not the
        # capture (bot review on PR #2991). The class's field pass types it
        # where it can; a capture left in place would bind the wrong class.
        shadowed = _declared_field_names(body)
        base = (
            self._java_type_ref(base_name, module_qn)
            if (base_name := _capture_base_name(site))
            else None
        )
        for name, type_name in self._captures_at(enclosing, site, module_qn).items():
            if name in shadowed:
                continue
            # A field inherited from the superclass hides it too, and is
            # what the name reads (bot review on PR #2991).
            inherited = (
                self._lookup_java_field_type(base, name, module_qn) if base else None
            )
            types[name] = inherited or type_name

    def _captures_at(
        self, enclosing: ASTNode, site: ASTNode, module_qn: str
    ) -> dict[str, str]:
        """The names of `enclosing` in scope at `site`, with their types.

        Only declarations visible there: the method's parameters, the fields
        it sees, and a local declared before `site` in a block (or a for
        loop) that encloses it. A same-named local of a sibling block is
        another variable (bot review on PR #2991). An enclosing method that
        is itself in an anonymous class sees its own captures the same way.
        """
        types: dict[str, str] = {}
        self._seed_captures(enclosing, types, module_qn)
        self._analyze_java_parameters(enclosing, types, module_qn)
        for decl in _nodes_of_type(enclosing, cs.TS_LOCAL_VARIABLE_DECLARATION):
            if (
                decl.parent is not None
                and decl.start_byte < site.start_byte
                and _encloses(decl.parent, site)
            ):
                self._process_java_variable_declaration(decl, types, module_qn)
        self._analyze_java_class_fields(enclosing, types, module_qn)
        for loop in _nodes_of_type(enclosing, cs.TS_ENHANCED_FOR_STATEMENT):
            if _encloses(loop, site):
                self._process_enhanced_for_statement(loop, types, module_qn)
        # A catch parameter and a try-with-resources variable are in scope in
        # their clause and statement, nearer than the fields (bot review on
        # PR #2991).
        for param in _nodes_of_type(enclosing, cs.TS_CATCH_FORMAL_PARAMETER):
            if param.parent is not None and _encloses(param.parent, site):
                self._process_catch_formal_parameter(param, types, module_qn)
        for resource in _nodes_of_type(enclosing, cs.TS_JAVA_RESOURCE):
            statement = resource.parent.parent if resource.parent else None
            if (
                statement is not None
                and resource.end_byte <= site.start_byte
                and _encloses(statement, site)
            ):
                self._process_java_resource(resource, types, module_qn)
        # A lambda around `site` binds its parameters nearer than the fields
        # and the method's own names, outermost first so the innermost wins
        # (bot review on PR #2991).
        for lam in _nodes_of_type(enclosing, cs.TS_JAVA_LAMBDA_EXPRESSION):
            if _encloses(lam, site):
                self._bind_lambda_parameters(lam, types, module_qn)
        return types

    def _bind_lambda_parameters(
        self, lam: ASTNode, types: dict[str, str], module_qn: str
    ) -> None:
        params = lam.child_by_field_name(cs.FIELD_PARAMETERS)
        if params is None:
            return
        # `x -> ...` and `(x, y) -> ...` leave the type to inference, which
        # this pass cannot do: the name is unknown rather than a field's.
        untyped = (
            [params]
            if params.type == cs.TS_IDENTIFIER
            else [
                child
                for child in params.named_children
                if child.type == cs.TS_IDENTIFIER
            ]
        )
        for name_node in untyped:
            if name := safe_decode_text(name_node):
                types.pop(name, None)
        self._analyze_java_parameters(lam, types, module_qn)

    def resolve_java_method_call(
        self,
        call_node: ASTNode,
        local_var_types: dict[str, str] | None,
        module_qn: str,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        # A pick recorded for an earlier call must not lend this one its
        # overload family.
        self._family_picks.clear()
        return self._do_resolve_java_method_call(
            call_node, local_var_types or {}, module_qn, caller_qn
        )

    def _find_containing_java_class(self, node: ASTNode) -> ASTNode | None:
        current = node.parent
        while current:
            match current.type:
                case (
                    cs.TS_CLASS_DECLARATION
                    | cs.TS_INTERFACE_DECLARATION
                    | cs.TS_ENUM_DECLARATION
                    | cs.TS_RECORD_DECLARATION
                ):
                    return current
            current = current.parent
        return None
