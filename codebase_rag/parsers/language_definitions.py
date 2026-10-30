"""The hook points the definition pass reaches each language's own behaviour
through. `ClassIngest`, `FunctionIngest` and `DefinitionProcessor` each build
one collaborator per language from that language's package
(`parsers/<language>/class_ingest.py`, `function_ingest.py`,
`file_ingest.py`), a subclass of the base here that overrides what its
language does differently, and look a definition's or a file's language up
to call it. A language with no collaborator, and every hook its collaborator
leaves alone, takes the neutral answer given here: the shared rule, or
nothing."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from tree_sitter import Node

from .. import constants as cs
from .positional_params import declared_positional_params

if TYPE_CHECKING:
    from ..language_spec import LanguageSpec
    from ..types_defs import (
        DeferredInherit,
        FunctionLocation,
        FunctionSpanKey,
        LanguageQueries,
        PropertyDict,
    )
    from .function_identity import FunctionResolution


class LanguageClassIngest:
    """What `ClassIngest` asks the collaborator of a class's language: whether
    the language takes a class node itself, where the node starts and what it
    records beside the shared node, members and relationships, how its
    methods register, and how a held-back base resolves once every class is
    registered."""

    __slots__ = ()

    # Whether the language keeps types and values in separate declaration
    # spaces (TypeScript), so a definition registers under its kind and one
    # from the other space keeps the natural name (`register_unique_qn`).
    separate_declaration_spaces: ClassVar[bool] = False
    # Whether a method's span keeps the location an earlier pass recorded
    # rather than taking this pass's.
    keeps_claimed_method_spans: ClassVar[bool] = False
    # Whether the override walk joins the implementer pairs read back from
    # the graph for a file of this language.
    joins_read_back_implementers: ClassVar[bool] = True

    def extra_class_nodes(self, root_node: Node) -> list[Node]:
        # Class nodes the file's class captures miss.
        return []

    def ingest_inline_modules(
        self, module_nodes: list[Node], module_qn: str, lang_config: LanguageSpec
    ) -> None:
        return None

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
        # True when the language took the node itself (or holds it back), so
        # the shared pass writes no class node for it.
        return False

    def local_type_qn(self, class_node: Node, module_qn: str) -> str | None:
        # The qn of a type written in a function body, which that function
        # owns; None for a type the shared identity names.
        return None

    def class_start_point(self, class_node: Node) -> tuple[int, int]:
        return class_node.start_point[0] + 1, class_node.start_point[1]

    def record_type_props(
        self,
        class_node: Node,
        class_qn: str,
        class_name: str,
        label: cs.NodeLabel,
        class_props: PropertyDict,
    ) -> None:
        return None

    def non_method_members(self, class_node: Node) -> list[str]:
        # The names a class body binds to a value that is no method.
        return []

    def record_type_location(
        self,
        class_qn: str,
        label: cs.NodeLabel,
        file_path: Path,
        class_start_line: int,
        class_start_col: int,
    ) -> dict[str, str] | None:
        # Records where the type is declared for a frontend's facts to join
        # back through, and returns the base kinds that frontend gives it.
        return None

    def is_enum(self, class_qn: str) -> bool:
        # An enum the language declares as a class, which only its resolved
        # bases identify.
        return False

    def record_heritage(self, member_node: Node, class_qn: str, module_qn: str) -> None:
        return None

    def record_members(
        self,
        class_node: Node,
        member_node: Node,
        class_qn: str,
        module_qn: str,
        label: cs.NodeLabel,
        modifiers: list[str],
        file_path: Path | None,
    ) -> None:
        # What a type's body declares beside its methods (field types,
        # member specs), once the type's relationships are written.
        return None

    def member_aliases(self, body_node: Node) -> Mapping[int, tuple[str, ...]]:
        # Method node id -> the other member names the class body binds it to.
        return {}

    def external_override_names(self, class_qn: str) -> frozenset[str]:
        # The method names an external base of the class defines.
        return frozenset()

    def repeats_member_signature(
        self, method_node: Node, seen_signatures: set[str]
    ) -> bool:
        # True for a member whose signature an earlier one in the same body
        # declared; `seen_signatures` is that body's own record.
        return False

    def method_qualified_name(self, method_node: Node, class_qn: str) -> str | None:
        # A qn the language spells itself (a parameter signature), else None
        # for the natural one.
        return None

    def record_method_return_type(self, method_node: Node, class_qn: str) -> None:
        # Recorded under the method's declared name whether or not it
        # registered.
        return None

    def record_method(
        self,
        class_node: Node,
        class_qn: str,
        module_qn: str | None,
        method_node: Node,
        ingested_qn: str,
    ) -> None:
        return None

    def register_name_alias(
        self, module_qn: str, method_node: Node, location: FunctionLocation
    ) -> None:
        # A second location a frontend keys the method at.
        return None

    def record_resolved_base(self, entry: DeferredInherit, parent_qn: str) -> None:
        # A held-back base resolved to the first-party `parent_qn`.
        return None

    def scoped_base(self, entry: DeferredInherit) -> str | None:
        # The base the language's own scoping binds, ahead of the name search.
        return None

    def base_by_arity(
        self, entry: DeferredInherit, resolved: tuple[str, bool] | None
    ) -> tuple[str, bool] | None:
        return resolved

    def arity_sibling(self, entry: DeferredInherit) -> str | None:
        # A different type of the child's name its base names, when parse
        # time resolved that base to the child itself.
        return None

    def imported_base(self, entry: DeferredInherit) -> str | None:
        # The base the file's imports bind, ahead of the registry check.
        return None

    def reexport_target(self, entry: DeferredInherit) -> str | None:
        # The declaration a base names through a re-export.
        return None

    def namespace_member(self, raw_name: str, entry: DeferredInherit) -> str | None:
        # The type a qualified base names as a member of an imported module.
        return None

    def namespaced_class(self, class_name: str) -> str | None:
        # The class a namespace-qualified base names in its declared form.
        return None

    def externalized_base(self, raw_name: str) -> str | None:
        # The canonical external qn of a base no indexed class answers, else
        # None for the written name.
        return None

    def overrides_linked_apart(self) -> Collection[str]:
        # The types whose overrides another pass links, which neither
        # override walk visits.
        return ()


class LanguageFunctionIngest:
    """What `FunctionIngest` asks the collaborator of a function's language:
    whether the language takes a captured function itself, how it names one,
    whether its registration waits, what it records beside the shared node,
    and where its DEFINES edge comes from."""

    __slots__ = ()

    # Whether the language keeps types and values in separate declaration
    # spaces (TypeScript), so a function registers under its kind.
    separate_declaration_spaces: ClassVar[bool] = False
    # Whether the language names a definition the generic naming (a name
    # field, a binding name, an anonymous position) misses, through
    # `resolve_function`.
    resolves_own_identity: ClassVar[bool] = False
    # Whether a nested function's parent guess carries the parent node's span
    # for the deferred link to swap in the identity recorded there.
    pins_parent_span: ClassVar[bool] = False
    # Node types the parent walk climbs through without treating them as a
    # boundary or a parent: wrappers the language spec counts as module or
    # function nodes elsewhere but that only bind the nested function here.
    transparent_parent_types: ClassVar[frozenset[str]] = frozenset()

    def skips_function(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
        file_path: Path | None,
    ) -> bool:
        # True when the language took the node itself (or holds it back), so
        # the shared pass registers no function for it.
        return False

    def resolve_function(
        self, func_node: Node, module_qn: str
    ) -> FunctionResolution | None:
        # Asked only where `resolves_own_identity` is set.
        return None

    def nameless_function_name(self, func_node: Node) -> tuple[str | None, str | None]:
        # (name, display name) a function with no name of its own takes.
        return None, None

    def qualified_name(
        self, func_node: Node, module_qn: str, func_name: str
    ) -> str | None:
        # A qn the language derives itself, else None for the nested one.
        return None

    def defers_registration(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
    ) -> bool:
        # True when the registration waits for the file's flush.
        return False

    def positional_params(
        self, func_node: Node, language: cs.SupportedLanguage
    ) -> list[str] | None:
        # Every parameter with the optionality the signature declares (issue
        # #2517), or None where the language declares none.
        return declared_positional_params(func_node, language)

    def is_program_main(self, func_node: Node) -> bool:
        return False

    def mark_name_scope(
        self, func_node: Node, qualified_name: str, func_props: PropertyDict
    ) -> None:
        # Marks what the function's name is visible to, beyond its module.
        return None

    def register_name_alias(
        self, module_qn: str, decl_node: Node, location: FunctionLocation
    ) -> None:
        # A second location a frontend keys the function at.
        return None

    def record_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        lang_config: LanguageSpec,
        location: FunctionLocation,
    ) -> None:
        # What the language records once the function and its location are.
        return None

    def defers_containment(
        self,
        func_node: Node,
        child_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
    ) -> bool:
        # True when the DEFINES edge waits for the parent's final qn.
        return False

    def extra_relationships(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        parent_label: str,
        parent_qn: str,
    ) -> None:
        # Edges the language writes beside the function's DEFINES.
        return None

    def record_return_type(self, func_node: Node, qualified_name: str) -> None:
        return None

    def is_body_local(self, func_node: Node) -> bool:
        # A free function local to a body the shared walk reads as a type's,
        # so no method of it.
        return False

    def enclosing_parent(
        self, current: Node, func_qn: str, module_qn: str
    ) -> tuple[str, str, FunctionSpanKey | None] | None:
        # The parent a function nested in `current` binds to, when the
        # identity re-derived from `current` would miss it.
        return None

    def inline_module_parent(
        self, func_node: Node, func_qn: str
    ) -> tuple[str, str, FunctionSpanKey | None] | None:
        # The module inside the file that contains a function no enclosing
        # function does.
        return None

    def anonymous_class_name(self, class_node: Node) -> str | None:
        # The path segment of a class with no name of its own.
        return None

    def next_path_ancestor(self, node: Node) -> Node | None:
        # Where the nested-qn walk resumes above a node that gave it a part.
        return node.parent


class LanguageFileIngest:
    """What `DefinitionProcessor` asks the collaborator of a file's language
    around the shared parse, Module node and passes: how the file is read
    and its parse settled, what its Module node records, what runs before
    and after its imports, the order its function and class passes run in,
    and what runs once they have."""

    __slots__ = ()

    # Whether the file's class pass runs before its function pass, with no
    # inline-module pass ahead of either.
    types_before_functions: ClassVar[bool] = False

    def read_source(self, file_path: Path, source_bytes: bytes | None) -> bytes | None:
        # The bytes the file is parsed from; None reads the file at parse.
        return source_bytes

    def settle_parse(
        self,
        file_path: Path,
        relative_path: str,
        root_node: Node,
        source_bytes: bytes | None,
    ) -> None:
        return None

    def record_module_facts(self, module_qn: str, root_node: Node) -> None:
        return None

    def record_module_props(self, root_node: Node, module_props: PropertyDict) -> None:
        return None

    def reserve_names(
        self,
        combined_captures: dict[str, list] | None,
        module_qn: str,
        lang_queries: LanguageQueries,
        file_path: Path,
    ) -> None:
        # Names a later pass's definitions are owed, reserved before the
        # function pass registers any.
        return None

    def ingest_after_imports(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        file_path: Path,
    ) -> None:
        return None

    def folded_overloads(
        self,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list] | None,
    ) -> frozenset[int] | None:
        # The start bytes of the overload declarations that fold into their
        # implementation, which the function and method passes skip; None
        # where the language folds none.
        return None

    def finalise(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        # What runs once the file's function and class passes have.
        return None
