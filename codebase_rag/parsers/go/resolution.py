"""Go's share of the name-based callee resolution: the package block a bare
name binds through across the package's files, the build variants that
declare one name again, and what a call through an interface or a variant
reaches in the builds that compile the caller.

`CallResolver` owns what every language shares (the order the stages run in,
the resolution cache and the verdict each answer carries, the simple-name
fallback) and hands Go's part to the `GoResolver` it builds, which also
bridges the call pass to the go/types answer the Go engine
(`GoTypeInferenceEngine`) holds for a call site."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...types_defs import FunctionRegistryTrieProtocol
from ..definition_facts import DefinitionFacts
from ..resolver_host import ResolverHost
from . import interfaces as go_ifaces
from . import utils as go_utils
from .builds import ALWAYS, Expr, all_of, holds_wherever, possible
from .interfaces import GoMethodSpec
from .link_state import GoLinkState

if TYPE_CHECKING:
    from ..call_site import CallSite
    from ..import_processor import ImportProcessor
    from ..type_inference import TypeInferenceEngine


# An unqualified Go call names a package-level function: a method needs its
# receiver.
_GO_PACKAGE_FUNCTION_TYPES = frozenset({cs.NodeLabel.FUNCTION})


class GoResolverHost(ResolverHost, Protocol):
    """What Go resolution reads and calls on the `CallResolver` that builds it:
    the resolution every language shares, which a Go stage hands a narrowed
    question to or falls back on, the simple-name fallback, and the
    breadth-first method lookup along a type's embedded bases."""

    def try_resolve_via_trie(
        self,
        call_name: str,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
        call_point: int | None = None,
        constructing: bool = False,
    ) -> tuple[str, str] | None: ...

    def resolve_inherited_breadth_first(
        self, class_qn: str, method_name: str
    ) -> tuple[str, str] | None: ...


class GoResolver:
    """Binds Go calls by the package block and the builds that compile them.

    Built and owned by `CallResolver`. The registry, the import processor,
    the type inference engine, the definition facts, the Go interface link's
    state and the call pass's local scopes are the resolver's own, each held
    by reference and read as it stands when a call resolves."""

    __slots__ = (
        "_host",
        "function_registry",
        "import_processor",
        "type_inference",
        "facts",
        "go_local_scopes",
        "guessed_implementations",
        "go_methods",
        "go_builtin_shadows",
        "go_variant_methods",
        "go_pair_builds",
        "go_builds",
    )

    def __init__(
        self,
        host: GoResolverHost,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        facts: DefinitionFacts,
        links: GoLinkState,
        go_local_scopes: dict[str, go_utils.GoLocalScopes],
    ) -> None:
        self._host = host
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        self.facts = facts
        # The call pass fills these per caller before the caller's calls
        # resolve (`CallResolver.go_local_scopes`).
        self.go_local_scopes = go_local_scopes
        # What the Go interface link shares, each container by reference
        # (#2483): the pairs above it matched by name, so what dispatch
        # through them reaches is a guess; each method's signature; the
        # files go/types type-checked; the predeclared names each package
        # declares; the declarations a type's build variants hold for it; each
        # interface's build variants; the builds holding each pair; and each
        # file's build constraint.
        self.guessed_implementations = links.guessed_implementations
        self.go_methods: Mapping[str, GoMethodSpec] = links.methods
        self.go_builtin_shadows: Mapping[str, frozenset[str]] = links.builtin_shadows
        self.go_variant_methods: Mapping[str, Mapping[str, list[str]]] = (
            links.variant_methods
        )
        self.go_pair_builds: Mapping[tuple[str, str], Expr] = links.pair_builds
        self.go_builds = links.builds

    def go_package_sibling_targets(self, callee_qn: str) -> set[tuple[str, str]]:
        # Go package-level functions are package-scoped, but cgr keys each file as
        # its own module (`pkgdir.file.name`). Two same-package functions with the
        # same name can ONLY be mutually-exclusive build-tag variants (gin's
        # `validate` under `//go:build !nomsgpack` vs `nomsgpack`); the compiler
        # rejects duplicate top-level identifiers in a package otherwise. A bare call
        # resolves to just one file's copy, orphaning the other build's copy; return
        # every same-package same-name package-level sibling so no build variant is
        # reported dead. Revive-only and precise: a same-name function in a DIFFERENT
        # package (different directory) is a distinct function, never a variant, so
        # the package-dir equality guard excludes it.
        file_module_qn, sep, name = callee_qn.rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return set()
        pkg_dir, dsep, _file = file_module_qn.rpartition(cs.SEPARATOR_DOT)
        if not dsep:
            return set()
        clause = self.facts.go_package_names.get(file_module_qn)
        targets: set[tuple[str, str]] = set()
        for qn in self.function_registry.find_ending_with(name):
            label = self.function_registry.get(qn)
            if qn == callee_qn or label != cs.NodeLabel.FUNCTION:
                continue
            other_module, d, other_name = qn.rpartition(cs.SEPARATOR_DOT)
            if not d or other_name != name or other_module == file_module_qn:
                continue
            other_pkg, d2, other_file = other_module.rpartition(cs.SEPARATOR_DOT)
            # Same directory is necessary but not sufficient for same-package: Go
            # permits an external test package (`package p_test`) in a `_test.go` file
            # sharing the directory. Production code can never call a function defined
            # in a `_test.go` file, so exclude such siblings; else a genuinely
            # test-only dead function would be masked as live. A file under
            # another `package` clause (a `//go:build ignore` generator's
            # `package main`) is another package too, never a build variant,
            # and its edge would carry the callee's `exact` label (#2571).
            other_clause = self.facts.go_package_names.get(other_module)
            if (
                d2
                and other_pkg == pkg_dir
                and not other_file.endswith(cs.GO_TEST_FILE_SUFFIX)
                and (clause is None or other_clause in (None, clause))
            ):
                targets.add((label, qn))
        return targets

    def _go_declaring_path(self, declaration_qn: str) -> PurePath | None:
        # The file declaring Go `declaration_qn`, which cgr files directly
        # under that file's module (`pkg.file.Name`). An incremental run
        # parses changed files only, so an unchanged file is known by the
        # repo-relative path its definition was rehydrated with.
        modules = self.type_inference.module_qn_to_file_path
        declaring_qn = declaration_qn.rpartition(cs.SEPARATOR_DOT)[0]
        if (path := modules.get(declaring_qn)) is not None:
            return path
        if rehydrated := self.facts.rehydrated_definition_paths.get(declaration_qn):
            return self.type_inference.repo_path / rehydrated
        return None

    def _go_declaring_dir(self, declaration_qn: str) -> PurePath | None:
        path = self._go_declaring_path(declaration_qn)
        return path.parent if path is not None else None

    def go_declaration_is_visible(self, declaration_qn: str, module_qn: str) -> bool:
        """Whether Go `declaration_qn` is in scope for `module_qn`.

        A directory is not a package: `package m_test` files sit beside
        `package m` files and are a DIFFERENT package, and any `_test.go` is
        compiled only under `go test`. Without this filter a production
        `Error{}` in a third file of the package saw both `types.Error` and a
        same-named `Error` from `m_test.go`, and the ambiguity rule emitted
        nothing (#1747). A test file is visible only to a test
        requester of the same package, and within the requester's own
        directory the `package` clauses must agree. A lookup through an
        import is into ANOTHER package, whose clause the requester does not
        share, so only the test rule applies there.

        An incremental run parses changed files only, so an unchanged
        declaring file is known by the path its definition was rehydrated
        with; its clause is unknown then and does not exclude it.
        """
        declaring_path = self._go_declaring_path(declaration_qn)
        if declaring_path is None:
            return True
        declaring_qn = declaration_qn.rpartition(cs.SEPARATOR_DOT)[0]
        requester = self.type_inference.module_qn_to_file_path.get(module_qn)
        requester_is_test = requester is not None and requester.stem.endswith(
            cs.GO_TEST_FILE_SUFFIX
        )
        # The package is the directory, whatever dots the file stems hold
        # (#2616); path-less registrations (mock harnesses) group by qn.
        own_package = (
            declaring_path.parent == requester.parent
            if requester is not None
            else declaring_qn.rpartition(cs.SEPARATOR_DOT)[0]
            == module_qn.rpartition(cs.SEPARATOR_DOT)[0]
        )
        if declaring_path.stem.endswith(cs.GO_TEST_FILE_SUFFIX) and not (
            requester_is_test and own_package
        ):
            return False
        if not own_package:
            return True
        requester_package = self.facts.go_package_names.get(module_qn)
        declaring_package = self.facts.go_package_names.get(declaring_qn)
        return (
            requester_package is None
            or declaring_package is None
            or declaring_package == requester_package
        )

    def _go_package_scope(
        self, name: str, module_qn: str, labels: frozenset[cs.NodeLabel]
    ) -> list[str]:
        # The `labels` definitions an unqualified `name` in Go file `module_qn`
        # can bind through its package block: declared in any file of the same
        # directory and `package` clause, `_test.go` files for test callers
        # only. Every parsed Go file has its clause recorded, so a caller
        # without one is another language and gets no answer here.
        if module_qn not in self.facts.go_package_names:
            return []
        requester = self.type_inference.module_qn_to_file_path.get(module_qn)
        if requester is None:
            return []
        package_dir = requester.parent
        prefix = self._go_package_prefix(module_qn, package_dir)
        return [
            qn
            for qn in go_utils.package_level_definitions(
                self.function_registry,
                name,
                labels,
                lambda qn: (
                    qn.startswith(prefix) and self._go_declaring_dir(qn) == package_dir
                ),
            )
            # The directory alone would let `pkg/helper.py`'s `helper` pass
            # for a Go function of `pkg`.
            if self._host.module_language(qn) == cs.SupportedLanguage.GO
            and self.go_declaration_is_visible(qn, module_qn)
        ]

    def _go_package_prefix(self, module_qn: str, package_dir: PurePath) -> str:
        # `project.dir.`, the qn prefix `base_module_qn` gives every file of
        # `package_dir`: a string test that drops the other packages' same
        # names before any path lookup. Empty, dropping nothing, when the
        # caller's own qn does not carry it.
        try:
            parts = package_dir.relative_to(self.type_inference.repo_path).parts
        except ValueError:
            return ""
        project = self.import_processor.project_name
        prefix = cs.SEPARATOR_DOT.join((project, *parts, ""))
        return prefix if module_qn.startswith(prefix) else ""

    def resolve_go_same_package(
        self, call: CallSite, cache_key: tuple[str, str, bool] | None
    ) -> tuple[bool, tuple[str, str] | None]:
        if (
            call.language == cs.SupportedLanguage.GO
            and cs.SEPARATOR_DOT not in call.call_name
            and (
                result := self._try_resolve_go_same_package(
                    call.call_name, call.module_qn
                )
            )
        ):
            self._host.remember_cacheable(cache_key, result)
            return True, result
        return False, None

    def _try_resolve_go_same_package(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        # `helper()` in `a.go` naming `func helper()` in `b.go` of the same
        # package is as certain as a same-file call: Go shares one namespace
        # across the package's files and rejects a duplicate (#2571). Two
        # candidates are build variants (`h_linux.go`, `h_windows.go`) only
        # when no build compiles them together: the caller then binds the
        # first its builds may hold, labelled by the builds it reaches
        # (`GoCallResolution.build_targets`), and its sibling fan-out reaches
        # the rest.
        # The name-only fallback took another package's same-named function
        # there, `p_test`'s even, and no variant (#3360).
        candidates = self._go_build_variant_candidates(
            self._go_package_scope(call_name, module_qn, _GO_PACKAGE_FUNCTION_TYPES),
            module_qn,
        )
        if not candidates:
            return None
        qn = candidates[0]
        logger.debug(ls.CALL_GO_SAME_PACKAGE, call_name=call_name, qn=qn)
        return self.function_registry[qn], qn

    def _go_build_variant_candidates(
        self, candidates: list[str], module_qn: str
    ) -> list[str]:
        # The one candidate, or, of several, those a build compiling the
        # caller may hold when no build compiles two of them together; []
        # for any other several.
        if len(candidates) <= 1 or self.go_builds is None:
            return candidates
        caller = self.go_caller_rel(module_qn)
        files = {qn: self.go_declaring_rel(qn) for qn in candidates}
        reachable = sorted(
            qn for qn in candidates if not self.go_builds.apart((caller, files[qn]))
        )
        apart = all(
            self.go_builds.apart((files[first], files[second]))
            for index, first in enumerate(reachable)
            for second in reachable[index + 1 :]
        )
        return reachable if apart else []

    def go_package_type(self, name: str, module_qn: str) -> str | None:
        # A bare Go type name (`var m Mux`, `q *Mux`, the `*Mux` a same-package
        # `NewRouter()` returns) is the caller's package's type. The repo-wide
        # search by simple name behind this took whichever same-named type
        # sorted first, often another package's, and labelled the method call
        # on it exact (#2571).
        if cs.SEPARATOR_DOT in name:
            return None
        candidates = self._go_package_scope(
            name, module_qn, go_utils.TYPE_DECLARATION_TYPES
        )
        if len(candidates) > 1:
            # A function-local type is filed directly under its module too,
            # and shadows the package-level one in its own file. Directly:
            # a sibling's stem can extend this file's (`helper.gen.go` beside
            # `helper.go`), so a qn prefix is not enough.
            own = [
                qn
                for qn in candidates
                if qn.rpartition(cs.SEPARATOR_DOT)[0] == module_qn
            ]
            candidates = own if own else candidates
        # An interface each build declares again is the first of its
        # variants; a call through it binds per build at emission.
        found = go_utils.one_declaration(candidates, self.facts.go_declaration_variants)
        # The Go type walker reduces a declared `*aaa.Mux` to `Mux` as well,
        # so a name that a package this file imports also declares may be
        # that one; it keeps the lookup it had before.
        if found is None or self._go_imports_declare_type(name, module_qn):
            return None
        return found

    def _go_imports_declare_type(self, name: str, module_qn: str) -> bool:
        imported = set(
            (self.import_processor.import_mapping.get(module_qn) or {}).values()
        )
        return any(
            self.function_registry.get(qn) in go_utils.TYPE_DECLARATION_TYPES
            and qn.rpartition(cs.SEPARATOR_DOT)[0].rpartition(cs.SEPARATOR_DOT)[0]
            in imported
            for qn in self.function_registry.find_ending_with(name)
        )

    def interface_dispatch_targets(
        self, callee_qn: str
    ) -> dict[tuple[str, str], dict[str, bool]]:
        # A Go call through an interface runs whichever implementation the
        # value holds, and the types implement it implicitly, so a sole
        # implementer is no likelier than one of many: every implementer's
        # method is a target (issue #2483). Each maps to the implementers it
        # runs for, each with whether only a guess ties it to the interface
        # (a pair matched by name and arity), which its edge then says rather
        # than borrowing the call's own label.
        class_qn, sep, method_name = callee_qn.rpartition(cs.SEPARATOR_DOT)
        if not sep or self.function_registry.get(class_qn) != cs.NodeLabel.INTERFACE:
            return {}
        targets: dict[tuple[str, str], dict[str, bool]] = {}
        for implementer in self.facts.interface_implementers.get(class_qn, ()):
            guessed = (class_qn, implementer) in self.guessed_implementations
            found = [
                (cs.NodeLabel.METHOD.value, qn)
                for qn in self.go_variant_methods.get(implementer, {}).get(
                    method_name, ()
                )
            ]
            if result := self._host.try_resolve_method(implementer, method_name):
                found.append(result)
            for target in found:
                targets.setdefault(target, {})[implementer] = guessed
        return targets

    def go_declaration_satisfies(
        self, method_qn: str, interface_method_qn: str
    ) -> bool:
        # A build that declares the method again declares its own copy
        # (`T.M@5`), and one whose signature cannot be the interface
        # method's never runs for a call through the interface: in that
        # build the type is no implementer.
        have = self.go_methods.get(method_qn)
        want = self.go_methods.get(interface_method_qn)
        return (
            have is None
            or want is None
            or go_ifaces.compatible(
                go_ifaces.in_package(have, self.go_builtin_shadows),
                go_ifaces.in_package(want, self.go_builtin_shadows),
            )
        )

    def go_declaration_read(self, method_qn: str) -> bool:
        spec = self.go_methods.get(method_qn)
        checked = self.type_inference.facts.frontend_facts[cs.SupportedLanguage.GO]
        return spec is None or spec.file in checked.checked_files

    def with_go_variants(self, bases: Iterable[str]) -> list[str]:
        # A Go interface embedding a name each build declares again embeds
        # the one its build holds, so a method only one build's declares is
        # still reached through it (#3360).
        found: list[str] = []
        for base in bases:
            found.append(base)
            found.extend(
                variant
                for variant in self.facts.go_declaration_variants.get(base, ())
                if variant != base
            )
        return found

    def go_variant_callees(
        self, callee_qn: str, module_qn: str
    ) -> list[tuple[str, bool]] | None:
        """For a Go call bound to `callee_qn`, a method of an interface each
        build declares again, the method of every declaration a build
        compiling the caller's file may hold, each with whether every such
        build holds it. None when the interface has no build variants.

        `J` embedding `p.I` reached `i_linux.go`'s `M` for a caller every
        build compiles: an exact edge true on Linux alone, and Windows'
        `M` never reached (#3360)."""
        owner, dot, name = callee_qn.rpartition(cs.SEPARATOR_DOT)
        group = self.facts.go_declaration_variants.get(owner)
        if not dot or not group or self.go_builds is None:
            return None
        caller = self.go_caller_rel(module_qn)
        reached = [
            (method_qn, file)
            for variant in group
            if (method_qn := f"{variant}{cs.SEPARATOR_DOT}{name}")
            in self.function_registry
            and self.go_builds.together(
                (caller, file := self.go_declaring_rel(variant))
            )
        ]
        exact = len(reached) == 1 and self.go_builds.covered(caller, [reached[0][1]])
        return [(method_qn, exact) for method_qn, _ in reached]

    def go_reaches_everywhere(
        self, module_qn: str, declarations: Iterable[str]
    ) -> bool:
        """Whether every build compiling Go file `module_qn` compiles every
        one of `declarations`: an edge to one only some of them hold is no
        exact edge."""
        if self.go_builds is None:
            return True
        return self.go_builds.everywhere(
            self.go_caller_rel(module_qn),
            [self.go_declaring_rel(qn) for qn in declarations],
        )

    def go_dispatch_builds(
        self,
        module_qn: str,
        interface_qn: str,
        implementer: str,
        declaration: str,
    ) -> tuple[bool, bool]:
        """Whether `declaration` runs for `implementer` through a call through
        `interface_qn` from Go file `module_qn` in some build compiling the
        caller, and whether in every one."""
        if self.go_builds is None:
            return True, True
        caller = self.go_builds.constraint(self.go_caller_rel(module_qn))
        runs = all_of(
            (
                self.go_pair_builds.get((interface_qn, implementer), ALWAYS),
                self.go_builds.constraint(self.go_declaring_rel(declaration)),
            )
        )
        return possible((caller, runs)), holds_wherever(caller, runs)

    def go_apart_from_caller(self, module_qn: str, declaration: str) -> bool:
        """Whether no build compiles Go file `module_qn` with `declaration`:
        its call never reaches that one."""
        return self.go_builds is not None and self.go_builds.apart(
            (self.go_caller_rel(module_qn), self.go_declaring_rel(declaration))
        )

    def go_caller_rel(self, module_qn: str) -> str:
        return go_utils.rel_file(
            self.type_inference.module_qn_to_file_path.get(module_qn),
            self.type_inference.repo_path,
        )

    def go_declaring_rel(self, qn: str) -> str:
        # The repo-relative file declaring Go method or package-level `qn`.
        if (spec := self.go_methods.get(qn)) is not None:
            return spec.file
        path = self._go_declaring_path(qn)
        return (
            go_utils.rel_file(Path(path), self.type_inference.repo_path) if path else ""
        )

    def resolve_go_local_value(
        self, call: CallSite
    ) -> tuple[bool, tuple[str, str] | None]:
        # `helper := func() {...}; helper()`, or a `helper func()` parameter,
        # calls the local value, so the package-scope probes must not certify
        # a same-named package function as the target (#2571). Caller-specific,
        # so it answers before the module-keyed cache; the name trie keeps
        # the edge it gave before, labelled for what it is.
        if call.language != cs.SupportedLanguage.GO or not self._go_local_in_scope(
            call
        ):
            return False, None
        return True, self._host.try_resolve_via_trie(
            call.call_name, call.module_qn, call.language, call.call_point
        )

    def _go_local_in_scope(self, call: CallSite) -> bool:
        # Only where the local is in scope: a `helper()` before an inner
        # block's `helper := ...`, or after that block closes, is the
        # package's function (#2616). A call with no position falls
        # back to the whole function.
        if not call.caller_qn or cs.SEPARATOR_DOT in call.call_name:
            return False
        spans = self.go_local_scopes.get(call.caller_qn, {}).get(call.call_name)
        if not spans:
            return False
        point = call.call_point
        return point is None or any(start <= point < end for start, end in spans)

    def go_variant_method(
        self, class_qn: str, method_name: str
    ) -> tuple[str, str] | None:
        # A Go interface each build declares again has the methods of every
        # build's declaration, its own and embedded; the call binds to the
        # caller's builds at emission (`go_variant_callees`).
        for variant in self.facts.go_declaration_variants.get(class_qn, ()):
            if variant == class_qn:
                continue
            variant_qn = f"{variant}{cs.SEPARATOR_DOT}{method_name}"
            if variant_qn in self.function_registry:
                return self.function_registry[variant_qn], variant_qn
            if found := self._host.resolve_inherited_breadth_first(
                variant, method_name
            ):
                return found
        return None

    def resolve_go_call_site(
        self,
        call_node: Node,
        module_qn: str,
    ) -> tuple[str, str] | None:
        return self.type_inference.go_type_inference.resolve_go_call_site(
            call_node, module_qn
        )
