"""Go interfaces in the graph (issue #2483).

An interface's method specs become Method nodes it defines, so a call
through the interface has a node to bind to. After every type is
registered, each interface's embedded interfaces become INHERITS edges, and
each named type whose methods cover an interface's method set IMPLEMENTS it.
Each method of an implementer then OVERRIDES the interface method it
satisfies, for every interface it satisfies, which is what lets a rename of
one implementation find the interface method and the other implementations.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...types_defs import (
    FunctionLocation,
    FunctionLocations,
    FunctionRegistryTrieProtocol,
    NodeType,
    PendingTypeFact,
    PropertyDict,
    SimpleNameLookup,
)
from ...utils import qn_markers
from ..class_ingest.relationships import (
    create_implements_relationship,
    create_inheritance_relationship,
)
from ..parameter_nodes import PendingParameterType
from ..utils import ingest_method
from . import interfaces as go_ifaces
from . import utils as go_utils

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ..import_processor import ImportProcessor

# The labels a Go type that can carry methods is registered under: a struct
# (Class) or any other defined type (Type, as `type HandlerFunc func()`).
_GO_IMPLEMENTER_TYPES = frozenset({NodeType.CLASS, NodeType.TYPE})
_GO_METHOD_OWNER_TYPES = _GO_IMPLEMENTER_TYPES | {NodeType.INTERFACE}


class _GoMethodOwners(NamedTuple):
    """Each method owner's method set, the package declaring it (a method is
    declared in its receiver type's package), and every declaration of each
    of its methods: one per build that declares it again (`T.M` in
    `t_windows.go`, `T.M@5` in `t_linux.go`)."""

    method_sets: dict[str, go_ifaces.GoMethodSet]
    packages: dict[str, str]
    declarations: dict[str, dict[go_ifaces.GoMethodKey, list[go_ifaces.GoMethodSpec]]]


def _satisfying_files(
    declarations: list[go_ifaces.GoMethodSpec],
    wanted: tuple[go_ifaces.GoSignature, ...],
) -> set[str]:
    return {
        spec.file
        for spec in declarations
        if go_ifaces.satisfies((spec.signature,), wanted)
    }


@dataclass
class _GoCompilerView:
    """What the go/types runs read: the module roots they analysed and the
    files of them they type-checked. A run reads only the files its build
    selects (its GOOS, its tags, no cgo) in packages that type-check, so its
    pairs answer for a type and an interface only where it read the
    interface and every declaration that may satisfy a method the interface
    asks for. A method each build declares again was read in one build
    only, and another build's may satisfy the interface where it did not."""

    repo_path: Path
    analysed_roots: frozenset[str]
    checked_files: frozenset[str]
    # The types and interfaces it compared: a checked file can declare one it
    # skips (a generic type or interface), and no pair speaks for that one.
    compared: frozenset[str] = frozenset()
    roots: dict[str, str | None] = field(default_factory=dict)

    def owns(
        self,
        pair: tuple[str, str],
        type_package: str | None,
        interface: go_ifaces.GoInterfaceDecl,
        method_files: list[set[str]],
    ) -> bool:
        if not self.compared.issuperset(pair) or (
            interface.file not in self.checked_files
            or not all(files and files <= self.checked_files for files in method_files)
        ):
            return False
        type_root = self._root(type_package)
        return (
            type_root is not None
            and type_root == self._root(interface.package)
            and type_root in self.analysed_roots
        )

    def _root(self, package: str | None) -> str | None:
        if package is None:
            return None
        return go_utils.module_root(
            self.repo_path, go_utils.package_key_dir(package), self.roots
        )


class GoInterfaceMixin:
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: FunctionRegistryTrieProtocol
    simple_name_lookup: SimpleNameLookup
    import_processor: ImportProcessor
    module_qn_to_file_path: dict[str, Path]
    class_inheritance: dict[str, list[str]]
    interface_implementers: dict[str, set[str]]
    function_locations: FunctionLocations
    pending_type_facts: list[PendingTypeFact]
    pending_parameter_types: list[PendingParameterType]
    go_package_names: dict[str, str]
    # Go method qn (receiver methods and interface method specs) -> its
    # parameter count and package, from the parse or read back.
    go_methods: dict[str, go_ifaces.GoMethodSpec]
    # Go interface qn -> the types it embeds, as written, and its package.
    go_interfaces: dict[str, go_ifaces.GoInterfaceDecl]
    # The interfaces this run parsed -> their module: only those resolve
    # their embedded names and write the INHERITS edges, since an unchanged
    # interface's edges are still in the graph and were read back.
    go_interfaces_parsed: dict[str, str]
    # The (interface, implementer) pairs in `interface_implementers` that
    # go/types proved, and those matched here by name and arity only.
    go_proven_pairs: set[tuple[str, str]]
    go_guessed_pairs: set[tuple[str, str]]
    go_builtin_shadows: dict[str, frozenset[str]]
    rehydrated_definition_paths: dict[str, str]

    @abstractmethod
    def _get_docstring(
        self, node: Node, language: cs.SupportedLanguage
    ) -> str | None: ...

    @abstractmethod
    def _resolve_go_container_qn(self, module_qn: str, receiver_type: str) -> str: ...

    def _ingest_go_interface_methods(
        self,
        type_spec: Node,
        interface_qn: str,
        module_qn: str,
        file_path: Path | None,
    ) -> None:
        interface = go_ifaces.interface_type(type_spec)
        if interface is None:
            return
        rel_file = go_utils.rel_file(file_path, self.repo_path)
        package = go_utils.package_key(rel_file, self.go_package_names.get(module_qn))
        self.go_interfaces[interface_qn] = go_ifaces.GoInterfaceDecl(
            tuple(go_ifaces.embedded_types(interface)), package, rel_file
        )
        self.go_interfaces_parsed[interface_qn] = module_qn
        for method in go_ifaces.interface_methods(interface):
            method_qn = ingest_method(
                method,
                interface_qn,
                cs.NodeLabel.INTERFACE,
                self.ingestor,
                self.function_registry,
                self.simple_name_lookup,
                self._get_docstring,
                cs.SupportedLanguage.GO,
                file_path=file_path,
                repo_path=self.repo_path,
                module_qn=module_qn,
                type_fact_sink=self.pending_type_facts,
                parameter_type_sink=self.pending_parameter_types,
            )
            if method_qn is not None:
                self.go_methods[method_qn] = go_ifaces.GoMethodSpec(
                    go_ifaces.signature(method), package, rel_file
                )
                self._register_interface_method_location(
                    module_qn, method, method_qn, interface_qn
                )

    def _register_interface_method_location(
        self, module_qn: str, method: Node, method_qn: str, interface_qn: str
    ) -> None:
        # The go/types frontend names a call through an interface by the
        # interface method's NAME token; keyed there, its fact joins this
        # node instead of falling back to a by-name guess.
        name = method.child_by_field_name(cs.FIELD_NAME)
        if name is None:
            return
        key = (module_qn, name.start_point[0] + 1, name.start_point[1])
        self.function_locations[key] = FunctionLocation(
            label=cs.NodeLabel.METHOD.value,
            qualified_name=method_qn,
            container_qn=interface_qn,
        )

    def link_go_interfaces(
        self,
        analysed_roots: frozenset[str],
        checked_files: frozenset[str],
        compared: frozenset[str],
    ) -> int:
        """Embedding, implicit satisfaction and the overrides it implies.

        `analysed_roots` are the module roots (`.` for the repository's own)
        the go/types frontend analysed, `checked_files` the files of them it
        type-checked, and `compared` the types and interfaces it compared. A
        type and an interface of one such module that it compared, whose
        declarations it read, are paired by the compiler alone: a pair it did
        not prove does not hold, whatever the names say. Every other pair, in
        a module it never ran on or whose run failed, resting on a file its
        build left out, or on a type or interface it skips (a generic one),
        is matched by name. Returns the number of IMPLEMENTS edges written
        here.
        """
        self._record_go_builtin_shadows()
        embeds = self._resolve_go_embeds()
        owners = self._go_method_sets()
        compiler = _GoCompilerView(
            self.repo_path, analysed_roots, checked_files, compared
        )
        linked = self._link_implicit_implementations(embeds, owners, compiler)
        self._link_go_overrides(owners.method_sets, checked_files)
        if linked:
            logger.info(ls.GO_IMPLICIT_IMPLEMENTS_LINKED.format(count=linked))
        return linked

    def _link_implicit_implementations(
        self,
        embeds: dict[str, tuple[str, ...] | None],
        owners: _GoMethodOwners,
        compiler: _GoCompilerView,
    ) -> int:
        interfaces = go_ifaces.full_method_sets(owners.method_sets, embeds)
        types = {
            qn: methods
            for qn, methods in owners.method_sets.items()
            if self.function_registry.get(qn) in _GO_IMPLEMENTER_TYPES
        }

        def compiler_owned(type_qn: str, interface_qn: str) -> bool:
            declared = owners.declarations.get(type_qn, {})
            required = interfaces[interface_qn] or {}
            return compiler.owns(
                (type_qn, interface_qn),
                owners.packages.get(type_qn),
                self.go_interfaces[interface_qn],
                [
                    _satisfying_files(declared.get(key, []), wanted)
                    for key, wanted in required.items()
                ],
            )

        # The pairs go/types proved (joined before this) are not guessed
        # again, whatever other build declares a method again.
        pairs = [
            (type_qn, interface_qn)
            for type_qn, interface_qn in go_ifaces.implicit_implementations(
                interfaces, types
            )
            if (interface_qn, type_qn) not in self.go_proven_pairs
            and not compiler_owned(type_qn, interface_qn)
        ]
        for type_qn, interface_qn in pairs:
            create_implements_relationship(
                str(self.function_registry[type_qn]),
                type_qn,
                interface_qn,
                self.ingestor,
                resolution=cs.EdgeResolution.HEURISTIC,
            )
        self.replace_go_implementers(
            self.go_guessed_pairs,
            [(interface_qn, type_qn) for type_qn, interface_qn in pairs],
            keep=self.go_proven_pairs,
        )
        return len(pairs)

    def replace_go_implementers(
        self,
        previous: set[tuple[str, str]],
        current: list[tuple[str, str]],
        keep: set[tuple[str, str]],
    ) -> None:
        """Swap one source's Go (interface, implementer) pairs in
        `interface_implementers` for this pass's. The map only ever gained
        entries, so an updater reused across an edit (the watcher, a second
        sync) kept a type the edit had taken out of an interface, with its
        OVERRIDES and dispatch edges, where a clean index has none. A pair
        the other source holds (`keep`) stays: one guessed while go/types
        failed and proven once it ran is still an implementer."""
        for interface_qn, implementer_qn in previous - keep:
            self.interface_implementers.get(interface_qn, set()).discard(implementer_qn)
        previous.clear()
        for interface_qn, implementer_qn in current:
            self.interface_implementers.setdefault(interface_qn, set()).add(
                implementer_qn
            )
            previous.add((interface_qn, implementer_qn))

    def _record_go_builtin_shadows(self) -> None:
        # Go lets a package declare a predeclared name for itself (`type int
        # = string`, a defined `type uint8 int`), and its files then mean the
        # package's type by it. Any package-level declaration of the name
        # counts, and a package is its directory, so the name is unknown in
        # every file there.
        shadows: dict[str, set[str]] = {}
        for name in (*cs.GO_PREDECLARED_TYPES, *cs.GO_PREDECLARED_ALIASES):
            for qn in self.function_registry.find_ending_with(name):
                if (path := self._go_declaring_file(qn)) is not None:
                    directory = PurePosixPath(path).parent.as_posix()
                    shadows.setdefault(directory, set()).add(name)
        # In place: the call resolver holds the map.
        self.go_builtin_shadows.clear()
        self.go_builtin_shadows.update(
            {directory: frozenset(names) for directory, names in shadows.items()}
        )

    def _go_declaring_file(self, qn: str) -> str | None:
        # The Go file declaring package-level `qn`, read back or parsed.
        path = self.rehydrated_definition_paths.get(qn)
        if path is None:
            module_path = self.module_qn_to_file_path.get(
                qn.rpartition(cs.SEPARATOR_DOT)[0]
            )
            path = go_utils.rel_file(module_path, self.repo_path) or None
        return path if path is not None and path.endswith(cs.EXT_GO) else None

    def _go_method_sets(self) -> _GoMethodOwners:
        owners = _GoMethodOwners({}, {}, {})
        registry = self.function_registry
        for method_qn, declared in self.go_methods.items():
            if registry.get(method_qn) != NodeType.METHOD:
                continue
            owner, _, name = method_qn.rpartition(cs.SEPARATOR_DOT)
            if registry.get(owner) not in _GO_METHOD_OWNER_TYPES:
                continue
            spec = declared._replace(
                signature=go_ifaces.in_package(declared, self.go_builtin_shadows)
            )
            key = go_ifaces.method_key(qn_markers.strip_dup_marker(name), spec.package)
            method_set = owners.method_sets.setdefault(owner, {})
            method_set[key] = (*method_set.get(key, ()), spec.signature)
            owners.packages[owner] = spec.package
            owners.declarations.setdefault(owner, {}).setdefault(key, []).append(spec)
        return owners

    def _link_go_overrides(
        self,
        method_sets: dict[str, go_ifaces.GoMethodSet],
        checked_files: frozenset[str],
    ) -> None:
        # The generic override walk links a method to the FIRST interface
        # declaring its name, as Java resolves it. A Go method satisfies every
        # interface its type satisfies, so each one gets its own edge.
        for interface_qn, implementers in sorted(self.interface_implementers.items()):
            declared = method_sets.get(interface_qn)
            if not declared or interface_qn not in self.go_interfaces:
                continue
            for type_qn in sorted(implementers):
                guessed = (interface_qn, type_qn) in self.go_guessed_pairs
                for key in sorted(declared.keys() & method_sets.get(type_qn, {})):
                    self._override_declarations(
                        f"{type_qn}{cs.SEPARATOR_DOT}{key[1]}",
                        f"{interface_qn}{cs.SEPARATOR_DOT}{key[1]}",
                        declared[key],
                        None if guessed else checked_files,
                    )

    def _override_declarations(
        self,
        method_qn: str,
        interface_method_qn: str,
        wanted: tuple[go_ifaces.GoSignature, ...],
        proven_in: frozenset[str] | None,
    ) -> None:
        # Each build that declares the method again holds a declaration of
        # its own (`T.M`, `T.M@5`), and renaming the interface method must
        # rewrite every one, or the build that holds a missed one no longer
        # compiles. `proven_in` holds the files of the build go/types proved
        # the pair for (None for a guessed pair): a declaration there is the
        # compiler's word, whatever the names say. Any other is matched by
        # name, and one whose signature differs for certain implements the
        # interface in no build.
        for variant in self.function_registry.variants(method_qn):
            spec = self.go_methods.get(variant)
            proven = proven_in is not None and (spec is None or spec.file in proven_in)
            if (
                proven
                or spec is None
                or go_ifaces.satisfies(
                    (go_ifaces.in_package(spec, self.go_builtin_shadows),), wanted
                )
            ):
                self._emit_go_override(variant, interface_method_qn, not proven)

    def _emit_go_override(
        self, method_qn: str, interface_method_qn: str, guessed: bool
    ) -> None:
        registry = self.function_registry
        if (
            registry.get(method_qn) != NodeType.METHOD
            or registry.get(interface_method_qn) != NodeType.METHOD
        ):
            return
        # A pair matched by name and arity alone carries that on the edge,
        # as its IMPLEMENTS does: rename follows OVERRIDES, and must not
        # rewrite an interface method a guess tied to the target unasked.
        properties: PropertyDict = (
            {cs.KEY_RESOLUTION: cs.EdgeResolution.HEURISTIC} if guessed else {}
        )
        self.ingestor.ensure_relationship_batch(
            (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
            cs.RelationshipType.OVERRIDES,
            (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, interface_method_qn),
            properties,
        )

    def _resolve_go_embeds(self) -> dict[str, tuple[str, ...] | None]:
        """Each Go interface's embedded interfaces as qns (or a predeclared
        name), None when one does not resolve to an interface."""
        resolved: dict[str, tuple[str, ...] | None] = {}
        for interface_qn, decl in self.go_interfaces.items():
            if self.function_registry.get(interface_qn) != NodeType.INTERFACE:
                continue
            module_qn = self.go_interfaces_parsed.get(interface_qn)
            resolved[interface_qn] = (
                self._stored_embeds(interface_qn, decl.embeds)
                if module_qn is None
                else self._parsed_embeds(interface_qn, module_qn, decl.embeds)
            )
        self.go_interfaces_parsed.clear()
        return resolved

    def _parsed_embeds(
        self, interface_qn: str, module_qn: str, written: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        resolve = self._parsed_embed_resolver(
            module_qn, self._go_shadowed(interface_qn)
        )
        bases = [base for text in written if (base := resolve(text)) is not None]
        self._record_go_embedding(interface_qn, bases)
        return tuple(bases) if len(bases) == len(written) else None

    def _stored_embeds(
        self, interface_qn: str, written: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        # An unchanged interface's embeds were resolved when it was parsed and
        # are its INHERITS edges now, read back into class_inheritance in
        # `base_index` order, the order they were written in. Matching them
        # by position keeps `a.Contract` and `b.Contract` two bases; by name
        # alone both would find the first. A count or name that disagrees
        # means an embed did not resolve then, so the method set is unknown.
        stored = iter(self.class_inheritance.get(interface_qn, []))
        shadowed = self._go_shadowed(interface_qn)
        bases: list[str] = []
        for text in written:
            if text in cs.GO_BUILTIN_INTERFACE_METHODS and text not in shadowed:
                bases.append(text)
                continue
            base = next(stored, None)
            name = text.rpartition(cs.SEPARATOR_DOT)[2]
            if base is None or base.rpartition(cs.SEPARATOR_DOT)[2] != name:
                return None
            bases.append(base)
        return None if next(stored, None) is not None else tuple(bases)

    def _go_shadowed(self, interface_qn: str) -> frozenset[str]:
        # The predeclared names the interface's package declares itself.
        directory = PurePosixPath(self.go_interfaces[interface_qn].file).parent
        return self.go_builtin_shadows.get(directory.as_posix(), frozenset())

    def _parsed_embed_resolver(
        self, module_qn: str, shadowed: frozenset[str]
    ) -> Callable[[str], str | None]:
        # A package that declares `error` or `any` itself means its own by
        # the name, an interface of its methods or no interface at all, and
        # never the predeclared one (issue #2483).
        def resolve(text: str) -> str | None:
            if text in cs.GO_BUILTIN_INTERFACE_METHODS and text not in shadowed:
                return text
            qualifier, dot, name = text.partition(cs.SEPARATOR_DOT)
            if not dot:
                candidate = self._resolve_go_container_qn(module_qn, text)
                return self._interface_or_none(candidate)
            return self._imported_interface(module_qn, qualifier, name)

        return resolve

    def _imported_interface(
        self, module_qn: str, qualifier: str, name: str
    ) -> str | None:
        imports = self.import_processor.import_mapping.get(module_qn) or {}
        package_qn = imports.get(qualifier)
        if package_qn is None:
            return None
        candidates = go_utils.package_level_definitions(
            self.function_registry,
            name,
            {NodeType.INTERFACE},
            lambda qn: (
                self._go_module_package(qn.rpartition(cs.SEPARATOR_DOT)[0])
                == package_qn
            ),
        )
        return candidates[0] if len(candidates) == 1 else None

    def _go_module_package(self, module_qn: str) -> str:
        return go_utils.file_package_qn(
            module_qn,
            self.module_qn_to_file_path.get(module_qn),
            self.repo_path,
            self.project_name,
        )

    def _interface_or_none(self, qn: str) -> str | None:
        return qn if self.function_registry.get(qn) == NodeType.INTERFACE else None

    def _record_go_embedding(self, interface_qn: str, bases: list[str]) -> None:
        # An embedded interface is to Go what an extended one is to Java, so
        # it is recorded the same way: an INHERITS edge, which method lookup
        # through the interface and the override walk both follow.
        first_party = [
            base for base in bases if base not in cs.GO_BUILTIN_INTERFACE_METHODS
        ]
        self.class_inheritance[interface_qn] = first_party
        for index, base in enumerate(first_party):
            create_inheritance_relationship(
                cs.NodeLabel.INTERFACE,
                interface_qn,
                base,
                self.function_registry,
                self.ingestor,
                index,
            )
