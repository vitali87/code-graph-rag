"""C/C++'s share of the class pass: the forward declarations it holds until
every definition is registered, the template and elaborated-type captures
that register nothing, the types a function body declares, the C++20 module
units, the INHERITS edges it resolves once every class is known, and the
field and method return types a C++ class records.

`ClassIngest` owns what every language shares (the scan of a file's class
captures, a class's node, members and relationships) and hands C/C++'s part
to the `CppClassIngest` it builds."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import DeferredCppInherit, FunctionSpanKey
from ..class_ingest import cpp_modules
from ..class_ingest import identity as id_
from ..class_ingest import relationships as rel
from ..utils import function_span_key, module_qn_for_entity, written_simple_name
from . import local_types as cpp_local_types
from . import utils as cpp_utils
from .type_inference import CppTypeInferenceEngine

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...services import IngestorProtocol
    from ...types_defs import FunctionRegistryTrieProtocol, LanguageQueries
    from ..definition_facts import DefinitionFacts
    from ..import_processor import ImportProcessor
    from .function_ingest import CppFunctionIngest


class CppClassHost(Protocol):
    """What C/C++ class ingestion calls on the class pass that builds it: the
    class-node pass a held-back forward declaration goes through once no
    definition has claimed its name."""

    def _process_class_node(
        self,
        class_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
        lang_config: LanguageSpec,
        file_path: Path | None,
        sorted_func_nodes: list[Node] | None = None,
        func_node_starts: list[int] | None = None,
        allow_defer: bool = True,
    ) -> None: ...


class _DeferredForwardDecl(NamedTuple):
    # A C/C++ forward declaration held back until every file's real definitions
    # are registered, so we can tell an only-forward-declared type (keep it) from
    # one that also has a bodied definition elsewhere (drop the phantom).
    class_node: Node
    class_name: str
    # The namespace-qualified name (module-file prefix stripped, so `A::Foo` is
    # `A.Foo` regardless of which header declares it). Comparing on this, not the
    # bare simple name, keeps a forward-declared `B::Foo` when only `A::Foo` is
    # defined, while still matching a cross-file forward/definition of one type.
    ns_qn: str
    module_qn: str
    language: cs.SupportedLanguage
    lang_queries: LanguageQueries
    lang_config: LanguageSpec
    file_path: Path | None
    sorted_func_nodes: list[Node] | None
    func_node_starts: list[int] | None


def _is_template_inner_specifier(
    class_node: Node, language: cs.SupportedLanguage
) -> bool:
    return (
        language == cs.SupportedLanguage.CPP
        and class_node.type in cs.CPP_TYPE_SPECIFIER_NODE_TYPES
        and class_node.parent is not None
        and class_node.parent.type == cs.CppNodeType.TEMPLATE_DECLARATION
    )


def _is_elaborated_type_use(class_node: Node, language: cs.SupportedLanguage) -> bool:
    # `struct Table *metatable;`, `f(struct stat *st)`, `sizeof(struct node)`:
    # a bodyless tag written as the type of something only names a type
    # declared elsewhere. Indexing it minted a Class nested under whatever
    # held the use (`Udata.Table`, `node.node`, libc's `stat`), and that
    # phantom could even win the field's OF_TYPE edge (issue #2615). A forward
    # declaration has no declarator and stays on the deferred path, as does a
    # bodyless typedef: `typedef struct lua_State lua_State;` is C's
    # opaque-handle idiom and may be the only declaration the type has.
    if (
        language not in cs.C_FAMILY_LANGUAGES
        or class_node.type not in cs.C_ELABORATED_TYPE_NODE_TYPES
        or class_node.child_by_field_name(cs.FIELD_BODY) is not None
        or (parent := class_node.parent) is None
        or parent.type == cs.CppNodeType.TYPE_DEFINITION
        or (written := parent.child_by_field_name(cs.FIELD_TYPE)) is None
        or written.id != class_node.id
    ):
        return False
    return (
        parent.type not in cs.C_FORWARD_DECLARING_NODE_TYPES
        or parent.child_by_field_name(cs.FIELD_DECLARATOR) is not None
    )


def type_spec(class_node: Node) -> Node | None:
    if class_node.type != cs.CppNodeType.TEMPLATE_DECLARATION:
        return class_node
    return next(
        (
            child
            for child in class_node.children
            if child.type in cs.CPP_TYPE_SPECIFIER_NODE_TYPES
        ),
        None,
    )


class CppClassIngest:
    """Decides the C/C++ class captures that register nothing or wait, and
    writes what C/C++ types record beside the shared class pass.

    Built and owned by `ClassIngest`. The registries, the module paths, the
    definition facts and the module-interface sets are the pass's own, held
    by reference; the class lookup is the C/C++ function pass's."""

    __slots__ = (
        "_host",
        "_function_ingest",
        "ingestor",
        "repo_path",
        "project_name",
        "function_registry",
        "module_qn_to_file_path",
        "import_processor",
        "facts",
        "module_interfaces",
        "interfaces_parsed_this_run",
        "deferred_inherits",
        "_deferred_forward_decls",
        "_deferred_module_impls",
    )

    def __init__(
        self,
        host: CppClassHost,
        function_ingest: CppFunctionIngest,
        *,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: FunctionRegistryTrieProtocol,
        module_qn_to_file_path: dict[str, Path],
        import_processor: ImportProcessor,
        facts: DefinitionFacts,
        module_interfaces: set[str],
        interfaces_parsed_this_run: set[str],
    ) -> None:
        self._host = host
        # An INHERITS base resolves through the lookup out-of-class methods
        # use.
        self._function_ingest = function_ingest
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.function_registry = function_registry
        self.module_qn_to_file_path = module_qn_to_file_path
        self.import_processor = import_processor
        self.facts = facts
        self.module_interfaces = module_interfaces
        self.interfaces_parsed_this_run = interfaces_parsed_this_run
        # The INHERITS edges a class's relationships hold back, read by the
        # class pass each time it writes them.
        self.deferred_inherits: list[DeferredCppInherit] = []
        self._deferred_forward_decls: list[_DeferredForwardDecl] = []
        # C++20 implementation units whose IMPLEMENTS edge waits for its
        # interface to be known.
        self._deferred_module_impls: list[tuple[str, str]] = []

    def skips_class(
        self,
        class_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
        lang_config: LanguageSpec,
        file_path: Path | None,
        sorted_func_nodes: list[Node] | None,
        func_node_starts: list[int] | None,
        allow_defer: bool,
    ) -> bool:
        # A C/C++ forward declaration (`class Widget;`, or `template <T> class
        # Widget;`) is a bodyless type specifier. Registering it collides with the
        # real definition's qn (suffixing it `@line`) and fragments one class into
        # several same-named nodes, which makes member-call resolution pick among
        # duplicates nondeterministically. But a type ONLY ever forward-declared
        # (an opaque handle, or a metaprogramming primary defined solely via
        # specializations) has no other node, so it must be kept. We cannot tell
        # which until every file's definitions are in, so defer the forward
        # declaration and decide in resolve_deferred_forward_declarations.
        # The class query captures a templated class twice: the inner
        # class_specifier AND its template_declaration wrapper. The wrapper is the
        # canonical node (it carries the template params and always registers with
        # its natural qn); the inner specifier is redundant. Drop the inner one
        # outright (for bodied definitions too, not just bodyless forward decls)
        # so the class registers exactly once. Registering both suffixes the second
        # `@line`, splitting members (which attach to the bodied specifier) away
        # from the natural qn that callers reference, orphaning the whole class.
        if _is_template_inner_specifier(class_node, language):
            return True
        if _is_elaborated_type_use(class_node, language):
            return True

        spec = type_spec(class_node)
        if (
            spec is not None
            and spec.type in cs.CPP_TYPE_SPECIFIER_NODE_TYPES
            and spec.child_by_field_name(cs.FIELD_BODY) is None
            and allow_defer
        ):
            self._defer_forward_declaration(
                class_node,
                module_qn,
                language,
                lang_queries,
                lang_config,
                file_path,
                sorted_func_nodes,
                func_node_starts,
            )
            return True
        return False

    def _defer_forward_declaration(
        self,
        class_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
        lang_config: LanguageSpec,
        file_path: Path | None,
        sorted_func_nodes: list[Node] | None,
        func_node_starts: list[int] | None,
    ) -> None:
        deferred_identity = id_.resolve_class_identity(
            class_node, module_qn, language, lang_config, file_path
        )
        if deferred_identity:
            self._deferred_forward_decls.append(
                _DeferredForwardDecl(
                    class_node,
                    deferred_identity[1],
                    self._namespace_qn(deferred_identity[0], module_qn),
                    module_qn,
                    language,
                    lang_queries,
                    lang_config,
                    file_path,
                    sorted_func_nodes,
                    func_node_starts,
                )
            )

    def resolve_deferred_forward_declarations(self) -> int:
        # Run after every file's definitions are registered. A deferred forward
        # declaration whose class name already produced a real node is a phantom
        # (the bodied definition exists) -> drop it. Otherwise it is the only
        # representation of the type -> register it now. Deterministic: the
        # deferred list is in file (sorted) order, and the first surviving forward
        # declaration of an only-declared type claims the name for the rest.
        deferred = self._deferred_forward_decls
        if not deferred:
            return 0
        self._deferred_forward_decls = []
        registered = 0
        for entry in deferred:
            # Drop the forward declaration only when a real definition of the SAME
            # namespace-qualified type exists (not merely the same simple name in
            # another namespace). Otherwise it is the type's only node -> keep it.
            if self._namespace_qn_has_definition(entry.ns_qn):
                continue
            self._host._process_class_node(
                entry.class_node,
                entry.module_qn,
                entry.language,
                entry.lang_queries,
                entry.lang_config,
                entry.file_path,
                sorted_func_nodes=entry.sorted_func_nodes,
                func_node_starts=entry.func_node_starts,
                allow_defer=False,
            )
            registered += 1
        return registered

    def _namespace_qn(self, class_qn: str, module_qn: str) -> str:
        # Strip the module-file prefix so two nodes for the same C++ type in
        # different headers share one key (`leveldb.db.x.h.leveldb.VersionSet` and
        # `...y.h.leveldb.VersionSet` both -> `leveldb.VersionSet`), while types in
        # different namespaces stay distinct.
        prefix = f"{module_qn}{cs.SEPARATOR_DOT}"
        return class_qn[len(prefix) :] if class_qn.startswith(prefix) else class_qn

    def _namespace_qn_has_definition(self, ns_qn: str) -> bool:
        # A real definition of this namespace-qualified type is registered iff some
        # class qn ends with it (`....leveldb.VersionSet`). find_ending_with is
        # indexed by simple name, and because it is queried AFTER the registry is
        # rehydrated from the graph, it also covers definitions in files an
        # incremental run did not re-parse (issue: a forward decl must still drop
        # when its definition lives in an unchanged file).
        simple = ns_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        suffix = f"{cs.SEPARATOR_DOT}{ns_qn}"
        return any(
            qn.endswith(suffix)
            for qn in self.function_registry.find_ending_with(simple)
        )

    def ingest_module_declarations(
        self,
        root_node: Node,
        module_qn: str,
        file_path: Path,
    ) -> None:
        before = set(self.module_interfaces)
        cpp_modules.ingest_cpp_module_declarations(
            root_node,
            module_qn,
            file_path,
            self.repo_path,
            self.project_name,
            self.ingestor,
            self.module_interfaces,
            self._deferred_module_impls,
        )
        # Remember what this parse added, so rehydration can rebuild the
        # graph-derived entries without dropping an interface whose node
        # write has not been flushed yet.
        self.interfaces_parsed_this_run |= self.module_interfaces - before

    def resolve_deferred_module_impls(self) -> int:
        """Emit ModuleImplementation IMPLEMENTS edges for interfaces that exist.

        An implementation unit (`module X;`) whose interface (`export module
        X;`) lives in no parsed file has nothing real to point at; emitting
        the edge anyway would mint a phantom the database drops.
        """
        deferred = self._deferred_module_impls
        if not deferred:
            return 0
        self._deferred_module_impls = []
        emitted = 0
        for impl_qn, interface_qn in deferred:
            if interface_qn not in self.module_interfaces:
                continue
            self.ingestor.ensure_relationship_batch(
                (cs.NodeLabel.MODULE_IMPLEMENTATION, cs.KEY_QUALIFIED_NAME, impl_qn),
                cs.RelationshipType.IMPLEMENTS,
                (cs.NodeLabel.MODULE_INTERFACE, cs.KEY_QUALIFIED_NAME, interface_qn),
            )
            emitted += 1
        return emitted

    def resolve_deferred_inherits(self) -> int:
        """Emit C++ INHERITS edges now that every class is registered.

        A base written in another header resolves scope-first across all
        parsed files (the same lookup out-of-class methods use); a base that
        resolves nowhere emits no edge, because the module-anchored guess is a
        phantom endpoint the database silently drops anyway. Resolved qns
        replace the guesses in class_inheritance in place so Pass-3 method
        resolution and override detection walk the real hierarchy.
        """
        deferred = self.deferred_inherits
        if not deferred:
            return 0
        self.deferred_inherits = []
        emitted = 0
        for entry in deferred:
            parent_qn = self._resolve_base_qn(entry)
            if parent_qn is None:
                module_qn = module_qn_for_entity(
                    entry.child_qn, self.module_qn_to_file_path
                )
                if module_qn is not None:
                    self.import_processor.note_unresolved(
                        module_qn, written_simple_name(entry.base_name)
                    )
                continue
            bases = self.facts.class_inheritance.get(entry.child_qn)
            if bases is not None and entry.base_index < len(bases):
                bases[entry.base_index] = parent_qn
            rel.create_inheritance_relationship(
                entry.child_label,
                entry.child_qn,
                parent_qn,
                self.function_registry,
                self.ingestor,
                entry.base_index,
            )
            emitted += 1
        return emitted

    def _resolve_base_qn(self, entry: DeferredCppInherit) -> str | None:
        normalized = entry.base_name.replace(
            cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT
        )
        # Scope-first (see CppFunctionIngest.resolve_deferred_methods): the
        # enclosing namespaces distinguish same-leaf classes. The child itself
        # is excluded so `class Type : public other::Type` never self-inherits.
        candidates = [normalized]
        if entry.namespace_path:
            candidates.insert(
                0, f"{entry.namespace_path}{cs.SEPARATOR_DOT}{normalized}"
            )
        for candidate in candidates:
            parent_qn, resolved = self._function_ingest.resolve_class_qn(
                candidate, "", exclude_qn=entry.child_qn
            )
            if resolved:
                return parent_qn
        # The guess strips a qualified base (`other::Type`) to its leaf, so
        # it can collide with the CHILD's own qn when the base is
        # unresolvable; a self-INHERITS is never real.
        if (
            entry.guess_qn != entry.child_qn
            and self.function_registry.get(entry.guess_qn) is not None
        ):
            return entry.guess_qn
        return None

    def local_type_qn(self, class_node: Node, module_qn: str) -> str | None:
        # A type written in a function body is that function's, not the
        # module's: two functions' local `Checker`s must not collide, and an
        # unnamed functor needs a name for its operator() to hang off
        # (issue #2555).
        return cpp_local_types.local_type_qn(
            class_node, module_qn, self.facts.function_locations
        )

    @staticmethod
    def local_type_identity(class_node: Node, local_qn: str) -> tuple[str, str, bool]:
        return (
            local_qn,
            local_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1],
            cpp_utils.is_exported(class_node),
        )

    def local_type_parent(
        self, class_node: Node, local_qn: str, module_qn: str
    ) -> tuple[str, str, FunctionSpanKey | None]:
        # A local type is defined by the callable it is written in, or by the
        # local type it is nested in. The callable's span pins the parent
        # node for the deferred link: an out-of-line method registers only
        # once its class resolves after every file, and an overload's local
        # type must bind to that overload, not a same-named twin.
        owner_qn = local_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        parent = cpp_local_types.local_type_parent(class_node)
        if parent is None or parent.type in cs.CPP_COMPOUND_TYPES:
            return cs.NodeLabel.CLASS, owner_qn, None
        span = function_span_key(module_qn, parent)
        recorded = self.facts.function_locations.get(span)
        label = recorded.label if recorded is not None else cs.NodeLabel.METHOD
        return label, owner_qn, span

    def record_field_types(self, member_node: Node, class_qn: str) -> None:
        # Record this class's member-field types now (from the class body,
        # usually a header) so out-of-line method bodies in other files can
        # resolve `field_.method()` via the field's type at call resolution.
        if field_types := CppTypeInferenceEngine().build_field_type_map(member_node):
            self.facts.class_field_types[class_qn] = field_types

    def record_method_return_type(self, method_node: Node, class_qn: str) -> None:
        # Record a C++ method's return type so a chained call off a
        # static factory method (`parser(...).parse(...)`, nlohmann's
        # basic_json) can type the receiver and resolve the next hop.
        method_name = cpp_utils.extract_function_name(method_node)
        if method_name and (
            return_type := cpp_utils.extract_return_type_name(method_node)
        ):
            self.facts.method_return_types[
                f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
            ] = return_type
