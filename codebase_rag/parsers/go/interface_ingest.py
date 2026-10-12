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
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from pathlib import Path
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
from .builds import (
    ALWAYS,
    NEVER,
    UNKNOWN,
    Expr,
    GoBuilds,
    all_of,
    any_of,
    decide,
    holds_wherever,
    impossible,
    negate,
    possible,
)

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
    # The method qn of each declaration, in `declarations`' order.
    method_qns: dict[str, dict[go_ifaces.GoMethodKey, list[str]]]


def _add_declaration(
    owners: _GoMethodOwners,
    owner: str,
    key: go_ifaces.GoMethodKey,
    spec: go_ifaces.GoMethodSpec,
    method_qn: str,
) -> None:
    method_set = owners.method_sets.setdefault(owner, {})
    method_set[key] = (*method_set.get(key, ()), spec.signature)
    owners.packages[owner] = spec.package
    owners.declarations.setdefault(owner, {}).setdefault(key, []).append(spec)
    owners.method_qns.setdefault(owner, {}).setdefault(key, []).append(method_qn)


def _first_party(alternatives: go_ifaces.GoEmbedAlternatives | None) -> str | None:
    # The interface an embed's INHERITS edge names: the first of the
    # declarations it may be, one edge per embedded name as written.
    if alternatives is None:
        return None
    return next(
        (
            base
            for base, _ in alternatives
            if base not in cs.GO_BUILTIN_INTERFACE_METHODS
        ),
        None,
    )


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


@dataclass
class _GoPairBuilds:
    """The builds in which a type implements an interface, as a constraint
    on GOOS, GOARCH and tags: those holding the type, one of the interface's
    method sets, and for each of its methods a declaration of the type that
    may be it. A pair that holds in no build is no pair, and one go/types
    proved holds only where every build holding both ends holds it."""

    builds: GoBuilds
    owners: _GoMethodOwners
    interfaces: dict[str, tuple[go_ifaces.GoBuildSet, ...] | None]
    declaring_file: Callable[[str], str]
    # The files go/types type-checked: in a pair it proved, a declaration
    # there is the compiler's in the build it read, whatever the names say.
    trusted: frozenset[str]
    # The interfaces whose method set varies between builds past what the
    # names tell apart: a pair go/types proved (`proven_pairs`, as
    # (interface, type)) holds in the build it read, and nothing is known of
    # any other.
    varying: frozenset[str] = frozenset()
    proven_pairs: Collection[tuple[str, str]] = frozenset()
    memo: dict[tuple[str, str, bool], Expr] = field(default_factory=dict)
    trusted_builds: Expr = ALWAYS

    def __post_init__(self) -> None:
        # The build go/types read compiles every file it checked.
        self.trusted_builds = all_of(
            self.builds.constraint(path) for path in sorted(self.trusted)
        )

    def of_type(self, type_qn: str) -> Expr:
        return self.builds.constraint(self.declaring_file(type_qn))

    def within(
        self, type_qn: str, found: go_ifaces.GoBuildSet, proven: bool = False
    ) -> Expr:
        declared = self.owners.declarations.get(type_qn, {})
        return all_of(
            (
                found.builds,
                self.of_type(type_qn),
                *(
                    any_of(
                        self._declared(spec, wanted, proven)
                        for spec in declared.get(key, [])
                    )
                    for key, wanted in found.methods.items()
                ),
            )
        )

    def _declared(
        self,
        spec: go_ifaces.GoMethodSpec,
        wanted: tuple[go_ifaces.GoSignature, ...],
        proven: bool,
    ) -> Expr:
        # The builds in which `spec` may be the method `wanted` asks for.
        builds = self.builds.constraint(spec.file)
        if go_ifaces.satisfies((spec.signature,), wanted):
            return builds
        if proven and spec.file in self.trusted:
            return all_of((builds, self.trusted_builds))
        return NEVER

    def in_some_build(self, type_qn: str, found: go_ifaces.GoBuildSet) -> bool:
        return possible((self.within(type_qn, found),))

    def undecided(self, type_qn: str, found: go_ifaces.GoBuildSet) -> bool:
        """Whether cgr cannot tell if a build holds the type with `found`, or
        with one of the type's declarations of a method `found` asks for: a
        constraint it cannot read, or more tags than it enumerates. Such a
        pair, or such a declaration, is never linked, so a rename cannot see
        it."""
        within = self.within(type_qn, found)
        if decide((within,)) is None:
            return True
        declared = self.owners.declarations.get(type_qn, {})
        return any(
            decide((self.builds.constraint(spec.file), within)) is None
            for key in found.methods
            for spec in declared.get(key, [])
        )

    def holding(self, type_qn: str, interface_qn: str, proven: bool = False) -> Expr:
        if interface_qn in self.varying:
            if (interface_qn, type_qn) in self.proven_pairs:
                return self.trusted_builds
            return UNKNOWN
        key = (type_qn, interface_qn, proven)
        if key not in self.memo:
            self.memo[key] = any_of(
                self.within(type_qn, found, proven)
                for found in self.interfaces.get(interface_qn) or ()
                if found.methods
            )
        return self.memo[key]

    def interface_builds(self, interface_qn: str) -> Expr:
        # The builds compiling the interface: one of its method sets holds.
        if interface_qn in self.varying:
            return UNKNOWN
        return any_of(found.builds for found in self.interfaces.get(interface_qn) or ())

    def partial(self, type_qn: str, interface_qn: str) -> bool:
        """Whether a pair go/types proved holds in some build holding both
        ends but not in another: the type's, and one compiling the
        interface. A pair the names cannot see at all (go/types reads
        methods an embedded struct promotes) is the compiler's alone."""
        holding = self.holding(type_qn, interface_qn, proven=True)
        scope = all_of((self.of_type(type_qn), self.interface_builds(interface_qn)))
        return not impossible((scope, holding)) and not holds_wherever(scope, holding)


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
    # Go type -> method name -> the declarations a variant of the type that
    # another build declares (`t_linux.go`'s `T` beside `t_windows.go`'s)
    # holds for it in a file both builds compile, refilled by each link
    # (shared with the call resolver).
    go_variant_methods: dict[str, dict[str, list[str]]]
    # Go interface -> every declaration of its name that another build
    # makes (itself included), refilled by each link (shared with the call
    # resolver, which binds a call through it per the caller's builds).
    go_declaration_variants: dict[str, tuple[str, ...]]
    # (Go interface, implementer) -> the builds in which the pair holds,
    # refilled by each link (shared with the call resolver, which reaches an
    # implementation only from a caller one of those builds compiles).
    go_pair_builds: dict[tuple[str, str], Expr]
    go_builds: GoBuilds
    rehydrated_definition_paths: dict[str, str]

    @abstractmethod
    def _get_docstring(
        self, node: Node, language: cs.SupportedLanguage
    ) -> str | None: ...

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
        self._share_go_variant_methods(owners)
        self._record_go_interface_variants()
        files = {
            qn: self.go_builds.constraint(decl.file)
            for qn, decl in self.go_interfaces.items()
        }
        full = go_ifaces.full_method_sets(owners.method_sets, embeds, files)
        pair_builds = _GoPairBuilds(
            self.go_builds,
            owners,
            full.sets,
            self._go_declaring_rel,
            checked_files,
            full.varying,
            self.go_proven_pairs,
        )
        compiler = _GoCompilerView(
            self.repo_path, analysed_roots, checked_files, compared
        )
        types = self._go_implementer_types(owners)
        linked = self._link_implicit_implementations(pair_builds, compiler, types)
        self._link_go_overrides(owners.method_sets, checked_files, pair_builds)
        self._link_go_embedded_methods(embeds, owners, files)
        self._record_go_pair_builds(pair_builds)
        self._record_go_implementers_unknown(full, pair_builds, types)
        if linked:
            logger.info(ls.GO_IMPLICIT_IMPLEMENTS_LINKED.format(count=linked))
        return linked

    def _go_implementer_types(
        self, owners: _GoMethodOwners
    ) -> dict[str, go_ifaces.GoMethodSet]:
        return {
            qn: methods
            for qn, methods in owners.method_sets.items()
            if self.function_registry.get(qn) in _GO_IMPLEMENTER_TYPES
        }

    def _record_go_implementers_unknown(
        self,
        full: go_ifaces.GoFullMethodSets,
        pair_builds: _GoPairBuilds,
        types: dict[str, go_ifaces.GoMethodSet],
    ) -> None:
        """Mark each Go interface whose implementers cgr cannot tell, and
        why, on its node (#3360): one of unknown method set, or one some type
        may implement where cgr cannot tell whether a build holds the pair.
        Nothing links to it by name, so a rename refuses a method of its
        name rather than leave an implementation behind. Every interface is
        written, so a sync clears a mark that no longer holds."""
        unknown = {
            interface_qn: (
                cs.GoImplementersUnknown.BUILDS
                if interface_qn in full.varying
                else cs.GoImplementersUnknown.EMBEDS
            )
            for interface_qn, sets in full.sets.items()
            if sets is None
        }
        for type_qn, interface_qn, found in go_ifaces.covering(full.sets, types):
            if interface_qn not in unknown and pair_builds.undecided(type_qn, found):
                unknown[interface_qn] = cs.GoImplementersUnknown.CONSTRAINT
        for interface_qn in sorted(full.sets):
            self.ingestor.ensure_node_batch(
                cs.NodeLabel.INTERFACE,
                {
                    cs.KEY_QUALIFIED_NAME: interface_qn,
                    cs.KEY_IMPLEMENTERS_UNKNOWN: str(unknown.get(interface_qn, "")),
                },
            )

    def _link_implicit_implementations(
        self,
        pair_builds: _GoPairBuilds,
        compiler: _GoCompilerView,
        types: dict[str, go_ifaces.GoMethodSet],
    ) -> int:
        owners, interfaces = pair_builds.owners, pair_builds.interfaces
        # An interface whose method set differs between builds (it embeds
        # `p.I`, which `i_linux.go` and `i_windows.go` declare apart): the
        # one build go/types read speaks for no other, so its silence rules
        # nothing out, and a pair it proved holds in that build alone.
        per_build = pair_builds.varying | {
            qn for qn, builds in interfaces.items() if builds and len(builds) > 1
        }

        def compiler_owned(type_qn: str, interface_qn: str) -> bool:
            if interface_qn in per_build:
                return False
            declared = owners.declarations.get(type_qn, {})
            builds = interfaces[interface_qn] or ()
            methods = builds[0].methods if builds else {}
            return compiler.owns(
                (type_qn, interface_qn),
                owners.packages.get(type_qn),
                self.go_interfaces[interface_qn],
                [
                    _satisfying_files(declared.get(key, []), wanted)
                    for key, wanted in methods.items()
                ],
            )

        # The pairs go/types proved (joined before this) are not guessed
        # again, whatever other build declares a method again, unless the
        # interface is another in another build or the pair holds in some
        # builds holding both ends and not in others.
        pairs = [
            (type_qn, interface_qn)
            for type_qn, interface_qn in go_ifaces.implicit_implementations(
                interfaces, types, pair_builds.in_some_build
            )
            if (interface_qn, type_qn) not in self.go_proven_pairs
            and not compiler_owned(type_qn, interface_qn)
        ] + sorted(
            (type_qn, interface_qn)
            for interface_qn, type_qn in self.go_proven_pairs
            if interface_qn in per_build or pair_builds.partial(type_qn, interface_qn)
        )
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
        # counts, and a method of that name declares none. A package is its
        # directory and `package` clause: `p_test`, the external test package
        # `go test` builds beside `p`, declaring `error` leaves p's `error`
        # the predeclared one (#3360 review). What p's own `_test.go` files
        # declare is p's too, but `go build` never compiles it, so it is
        # recorded for those files alone (`go_utils.declaration_scope`).
        shadows: dict[str, set[str]] = {}
        for name in (*cs.GO_PREDECLARED_TYPES, *cs.GO_PREDECLARED_ALIASES):
            for qn in self.function_registry.find_ending_with(name):
                if (scope := self._go_declaration_scope(qn)) is not None:
                    shadows.setdefault(scope, set()).add(name)
        # In place: the call resolver holds the map.
        self.go_builtin_shadows.clear()
        self.go_builtin_shadows.update(
            {scope: frozenset(names) for scope, names in shadows.items()}
        )

    def _go_declaration_scope(self, qn: str) -> str | None:
        # The files that see Go declaration `qn`, read back or parsed, when it
        # is package-level: its file module is the qn above it.
        if self.function_registry.get(qn) == NodeType.METHOD:
            return None
        path = self._go_declaring_rel(qn)
        if not path.endswith(cs.EXT_GO):
            return None
        package = go_utils.package_key(
            path, self.go_package_names.get(qn.rpartition(cs.SEPARATOR_DOT)[0])
        )
        return go_utils.declaration_scope(path, package)

    def _go_declaring_rel(self, qn: str) -> str:
        # The repo-relative file declaring package-level `qn`, read back or
        # parsed ("" when unknown).
        return self.rehydrated_definition_paths.get(qn) or go_utils.rel_file(
            self.module_qn_to_file_path.get(qn.rpartition(cs.SEPARATOR_DOT)[0]),
            self.repo_path,
        )

    def _go_method_sets(self) -> _GoMethodOwners:
        owners = _GoMethodOwners({}, {}, {}, {})
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
            _add_declaration(owners, owner, key, spec, method_qn)
        return owners

    def _share_go_variant_methods(self, owners: _GoMethodOwners) -> None:
        # A type each build declares again (`type T struct{}` in `t_linux.go`
        # and in `t_windows.go`) is one type to the methods a file both
        # builds compile declares (`func (T) M()` in `t.go`), but cgr files
        # each method under one of them. The other has it too: in its method
        # set, and as a declaration its overrides and dispatch reach.
        shared: dict[str, dict[str, list[str]]] = {}
        for owner, declarations in self._own_go_declarations(owners).items():
            for variant in self._go_type_variants(owner):
                variant_file = self._go_declaring_rel(variant)
                for key, method_qn, spec in declarations:
                    if self.go_builds.together((spec.file, variant_file)):
                        _add_declaration(owners, variant, key, spec, method_qn)
                        shared.setdefault(variant, {}).setdefault(key[1], []).append(
                            method_qn
                        )
        # In place: the call resolver holds the map.
        self.go_variant_methods.clear()
        self.go_variant_methods.update(shared)

    def _own_go_declarations(
        self, owners: _GoMethodOwners
    ) -> dict[str, list[tuple[go_ifaces.GoMethodKey, str, go_ifaces.GoMethodSpec]]]:
        # Each type's own (key, method qn, declaration), before any is shared.
        return {
            owner: [
                (key, method_qn, spec)
                for key, declared in owners.declarations[owner].items()
                for method_qn, spec in zip(
                    owners.method_qns[owner][key], declared, strict=True
                )
            ]
            for owner in sorted(owners.declarations)
            if self.function_registry.get(owner) in _GO_IMPLEMENTER_TYPES
        }

    def _record_go_interface_variants(self) -> None:
        groups: dict[str, tuple[str, ...]] = {}
        for interface_qn in self.go_interfaces:
            if self.function_registry.get(interface_qn) != NodeType.INTERFACE:
                continue
            if variants := self._go_type_variants(
                interface_qn, frozenset({NodeType.INTERFACE})
            ):
                groups[interface_qn] = tuple(sorted((interface_qn, *variants)))
        # In place: the call resolver holds the map.
        self.go_declaration_variants.clear()
        self.go_declaration_variants.update(groups)

    def _go_type_variants(
        self, type_qn: str, labels: frozenset[NodeType] = _GO_IMPLEMENTER_TYPES
    ) -> list[str]:
        # The other declarations of `type_qn`'s name in its package that no
        # build compiles with it.
        scope = self._go_declaration_scope(type_qn)
        own_file = self._go_declaring_rel(type_qn)
        name = type_qn.rpartition(cs.SEPARATOR_DOT)[2]
        return [
            qn
            for qn in sorted(self.function_registry.find_ending_with(name))
            if qn != type_qn
            and qn.rpartition(cs.SEPARATOR_DOT)[2] == name
            and self.function_registry.get(qn) in labels
            and self._go_declaration_scope(qn) == scope
            and self.go_builds.apart((own_file, self._go_declaring_rel(qn)))
        ]

    def _link_go_overrides(
        self,
        method_sets: dict[str, go_ifaces.GoMethodSet],
        checked_files: frozenset[str],
        pair_builds: _GoPairBuilds,
    ) -> None:
        # The generic override walk links a method to the FIRST interface
        # declaring its name, as Java resolves it. A Go method satisfies every
        # interface its type satisfies, so each one gets its own edge.
        for interface_qn, implementers in sorted(self.interface_implementers.items()):
            declared = method_sets.get(interface_qn)
            if not declared or interface_qn not in self.go_interfaces:
                continue
            for type_qn in sorted(implementers):
                self._override_pair(
                    type_qn,
                    interface_qn,
                    {
                        key: declared[key]
                        for key in declared.keys() & method_sets.get(type_qn, {})
                    },
                    checked_files,
                    pair_builds,
                )

    def _override_pair(
        self,
        type_qn: str,
        interface_qn: str,
        shared: go_ifaces.GoMethodSet,
        checked_files: frozenset[str],
        pair_builds: _GoPairBuilds,
    ) -> None:
        guessed = (interface_qn, type_qn) in self.go_guessed_pairs
        # A declaration overrides only in a build where its type implements
        # the interface: `T.M` in `t_windows.go` overrides nothing a
        # Linux-only pair asks for. A proven pair's declaration is the
        # compiler's only where every build compiling it and the interface
        # holds the pair: `func (T) M()` in a file every build compiles is
        # Windows' `T`'s there, not `t_linux.go`'s.
        holding = pair_builds.holding(type_qn, interface_qn, not guessed)
        if not guessed and impossible((holding,)):
            holding = ALWAYS
        claim = all_of((pair_builds.of_type(type_qn), holding))
        scope = pair_builds.interface_builds(interface_qn)
        for key in sorted(shared):
            method_qn = f"{type_qn}{cs.SEPARATOR_DOT}{key[1]}"
            found = [
                variant
                for variant in (
                    *self.function_registry.variants(method_qn),
                    *self.go_variant_methods.get(type_qn, {}).get(key[1], []),
                )
                if possible((self._go_method_builds(variant), holding))
            ]
            exact = [
                variant
                for variant in found
                if not guessed
                and holds_wherever(
                    all_of((self._go_method_builds(variant), scope)), claim
                )
            ]
            target = f"{interface_qn}{cs.SEPARATOR_DOT}{key[1]}"
            self._override_declarations(exact, target, shared[key], checked_files)
            self._override_declarations(
                [variant for variant in found if variant not in exact],
                target,
                shared[key],
                None,
            )

    def _link_go_embedded_methods(
        self,
        embeds: dict[str, tuple[go_ifaces.GoEmbedAlternatives, ...] | None],
        owners: _GoMethodOwners,
        files: dict[str, Expr],
    ) -> None:
        # An interface method and the declarations of it its embeds hold:
        # what the generic override walks linked before they left Go's
        # interfaces to this pass, now down every build's declaration of an
        # embed and between two embeds that both declare it, in the builds
        # holding both. The INHERITS bases come first: they hold an
        # interface's resolved embeds even where another did not resolve.
        registry = self.function_registry
        bases: dict[str, list[str]] = {}
        for interface_qn in self.go_interfaces:
            if registry.get(interface_qn) != NodeType.INTERFACE:
                continue
            found = dict.fromkeys(self.class_inheritance.get(interface_qn, ()))
            for alternatives in embeds.get(interface_qn) or ():
                found.update(dict.fromkeys(base for base, _ in alternatives))
            bases[interface_qn] = [
                base for base in found if base not in cs.GO_BUILTIN_INTERFACE_METHODS
            ]
        own = {
            interface_qn: {
                key: qns[0]
                for key, qns in owners.method_qns.get(interface_qn, {}).items()
            }
            for interface_qn in bases
        }
        for method_qn, other in go_ifaces.embedded_method_links(own, bases, files):
            self._emit_go_override(method_qn, other, guessed=False)

    def _record_go_pair_builds(self, pair_builds: _GoPairBuilds) -> None:
        found: dict[tuple[str, str], Expr] = {}
        for interface_qn, implementers in self.interface_implementers.items():
            if interface_qn not in self.go_interfaces:
                continue
            for type_qn in implementers:
                guessed = (interface_qn, type_qn) not in self.go_proven_pairs
                holding = all_of(
                    (
                        pair_builds.of_type(type_qn),
                        pair_builds.holding(type_qn, interface_qn, not guessed),
                    )
                )
                # A pair go/types proved that the names cannot see holds
                # wherever both ends do.
                if not guessed and impossible((holding,)):
                    holding = pair_builds.of_type(type_qn)
                found[(interface_qn, type_qn)] = holding
        # In place: the call resolver holds the map.
        self.go_pair_builds.clear()
        self.go_pair_builds.update(found)

    def _go_method_builds(self, method_qn: str) -> Expr:
        spec = self.go_methods.get(method_qn)
        return ALWAYS if spec is None else self.go_builds.constraint(spec.file)

    def _override_declarations(
        self,
        declarations: list[str],
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
        for variant in declarations:
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

    def _resolve_go_embeds(
        self,
    ) -> dict[str, tuple[go_ifaces.GoEmbedAlternatives, ...] | None]:
        """Each Go interface's embedded names, each as the interfaces (or the
        predeclared name) it is in the builds that compile the interface,
        None when one does not resolve to an interface."""
        resolved: dict[str, tuple[go_ifaces.GoEmbedAlternatives, ...] | None] = {}
        for interface_qn, decl in self.go_interfaces.items():
            if self.function_registry.get(interface_qn) != NodeType.INTERFACE:
                continue
            module_qn = self.go_interfaces_parsed.get(interface_qn)
            resolved[interface_qn] = (
                self._stored_embeds(interface_qn, decl)
                if module_qn is None
                else self._parsed_embeds(interface_qn, module_qn, decl)
            )
        self.go_interfaces_parsed.clear()
        return resolved

    def _parsed_embeds(
        self, interface_qn: str, module_qn: str, decl: go_ifaces.GoInterfaceDecl
    ) -> tuple[go_ifaces.GoEmbedAlternatives, ...] | None:
        imports = self.import_processor.import_mapping.get(module_qn) or {}
        resolved: list[go_ifaces.GoEmbedAlternatives | None] = []
        for text in decl.embeds:
            qualifier, dot, name = text.partition(cs.SEPARATOR_DOT)
            if not dot:
                resolved.append(self._embed_alternatives(decl, text, None))
            elif (package_qn := imports.get(qualifier)) is not None:
                resolved.append(self._embed_alternatives(decl, name, package_qn))
            else:
                resolved.append(None)
        self._record_go_embedding(
            interface_qn,
            [base for found in resolved if (base := _first_party(found)) is not None],
        )
        return None if None in resolved else tuple(filter(None, resolved))

    def _stored_embeds(
        self, interface_qn: str, decl: go_ifaces.GoInterfaceDecl
    ) -> tuple[go_ifaces.GoEmbedAlternatives, ...] | None:
        # An unchanged interface's embeds were resolved when it was parsed and
        # are its INHERITS edges now, read back into class_inheritance in
        # `base_index` order, the order they were written in. Matching them
        # by position keeps `a.Contract` and `b.Contract` two bases; by name
        # alone both would find the first. A qualified name is the stored
        # base's package, its unqualified one the interface's own, read as
        # the parse read them. A count or name that disagrees means an embed
        # did not resolve then, so the method set is unknown.
        stored = iter(self.class_inheritance.get(interface_qn, []))
        resolved: list[go_ifaces.GoEmbedAlternatives] = []
        for text in decl.embeds:
            found = self._stored_embed(decl, text, stored)
            if found is None:
                return None
            resolved.append(found)
        return None if next(stored, None) is not None else tuple(resolved)

    def _stored_embed(
        self, decl: go_ifaces.GoInterfaceDecl, text: str, stored: Iterator[str]
    ) -> go_ifaces.GoEmbedAlternatives | None:
        _, dot, name = text.partition(cs.SEPARATOR_DOT)
        if not dot:
            return self._stored_local_embed(decl, text, stored)
        base = next(stored, None)
        if base is None or base.rpartition(cs.SEPARATOR_DOT)[2] != name:
            return None
        package_qn = self._go_module_package(base.rpartition(cs.SEPARATOR_DOT)[0])
        return self._embed_alternatives(decl, name, package_qn)

    def _stored_local_embed(
        self, decl: go_ifaces.GoInterfaceDecl, name: str, stored: Iterator[str]
    ) -> go_ifaces.GoEmbedAlternatives | None:
        # The parse wrote an edge for this name only where it found one of
        # the package's own interfaces by it.
        found = self._embed_alternatives(decl, name, None)
        if found is not None and _first_party(found) is not None:
            base = next(stored, None)
            if base is None or base.rpartition(cs.SEPARATOR_DOT)[2] != name:
                return None
        return found

    def _embed_alternatives(
        self, decl: go_ifaces.GoInterfaceDecl, name: str, package_qn: str | None
    ) -> go_ifaces.GoEmbedAlternatives | None:
        """What embedded `name` is in the builds that compile `decl`'s file:
        its own package's (`package_qn` None) or an imported package's
        declarations of it, one per build that declares it again
        (`i_linux.go`, `i_windows.go`). None when it names no interface, or
        two declarations one build compiles together. A package that
        declares `error` or `any` itself means its own by the name in the
        builds that hold that declaration, an interface of its methods or
        no interface at all (issue #2483), and the predeclared one in any
        other."""
        builtin = package_qn is None and name in cs.GO_BUILTIN_INTERFACE_METHODS
        meanings = (
            [(name, ALWAYS)]
            if builtin and name not in self._go_shadowed(decl)
            else self._declared_meanings(decl, name, package_qn, builtin)
        )
        return tuple(meanings) if meanings else None

    def _declared_meanings(
        self,
        decl: go_ifaces.GoInterfaceDecl,
        name: str,
        package_qn: str | None,
        builtin: bool,
    ) -> list[tuple[str, Expr]] | None:
        candidates = (
            self._go_package_declarations(decl, name)
            if package_qn is None
            else go_utils.package_level_definitions(
                self.function_registry,
                name,
                {NodeType.INTERFACE},
                lambda qn: self._go_imported_file(
                    qn.rpartition(cs.SEPARATOR_DOT)[0], package_qn
                ),
            )
        )
        # One declaration is the name's, as it always was; only a name
        # declared again has its files' builds read.
        found = (
            tuple(candidates)
            if len(candidates) == 1 and not builtin
            else self.go_builds.variants(
                ((qn, self._go_declaring_rel(qn)) for qn in candidates), decl.file
            )
        )
        if found is None or any(
            self.function_registry.get(qn) != NodeType.INTERFACE for qn in found
        ):
            return None
        meanings = [
            (qn, self.go_builds.constraint(self._go_declaring_rel(qn))) for qn in found
        ]
        if builtin:
            # The predeclared name in every build that holds none of them.
            meanings.append((name, negate(any_of(builds for _, builds in meanings))))
        return meanings

    def _go_package_declarations(
        self, decl: go_ifaces.GoInterfaceDecl, name: str
    ) -> list[str]:
        # The types named `name` that `decl`'s package declares and its file
        # sees: a `_test.go` file's only from its package's test files.
        seen_from = {decl.package, go_utils.declaration_scope(decl.file, decl.package)}
        return [
            qn
            for qn in sorted(self.function_registry.find_ending_with(name))
            if qn.rpartition(cs.SEPARATOR_DOT)[2] == name
            and self.function_registry.get(qn) in go_utils.TYPE_DECLARATION_TYPES
            and self._go_declaration_scope(qn) in seen_from
        ]

    def _go_shadowed(self, decl: go_ifaces.GoInterfaceDecl) -> frozenset[str]:
        # The predeclared names the interface's package declares itself.
        return go_ifaces.shadowed_in(decl.file, decl.package, self.go_builtin_shadows)

    def _go_imported_file(self, module_qn: str, package_qn: str) -> bool:
        # Whether Go file `module_qn` is a file of the package an import
        # binding `package_qn` names: in its directory, and not in `p_test`,
        # the external test package beside it that nothing imports. Its
        # `Reader` is no candidate for `p.Reader` (#3360 review). A module
        # with no recorded file (an unchanged one) is judged by its stem.
        path = self.module_qn_to_file_path.get(module_qn)
        stem = path.stem if path else module_qn.rpartition(cs.SEPARATOR_DOT)[2]
        return self._go_module_package(module_qn) == package_qn and not (
            go_utils.is_external_test(
                self.go_package_names.get(module_qn),
                stem.endswith(cs.GO_TEST_FILE_SUFFIX),
            )
        )

    def _go_module_package(self, module_qn: str) -> str:
        return go_utils.file_package_qn(
            module_qn,
            self.module_qn_to_file_path.get(module_qn),
            self.repo_path,
            self.project_name,
        )

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
