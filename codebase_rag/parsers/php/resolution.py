"""PHP's share of the name-based callee resolution: the module a `use
function` import's namespace is declared in, the class a qualified class
path names, and the method a PHP class runs for a name, all compared the way
PHP compares names, ASCII case-insensitively.

`CallResolver` owns what every language shares (the order the stages run in,
the resolution cache and the verdict each answer carries, the direct-import
probe these gates sit in) and hands PHP's part to these functions and the
`PhpResolver` it builds."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, NamedTuple, Protocol

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol
from ..definition_facts import DefinitionFacts
from ..resolver_host import ResolverHost
from . import class_names as php_class_names
from .class_names import ClassPath as PhpClassPath
from .class_names import ascii_fold as _php_fold

if TYPE_CHECKING:
    from ..import_processor import ImportProcessor
    from ..type_inference import TypeInferenceEngine
    from .imports import PhpImporter


class _PhpCasedIndex(NamedTuple):
    """PHP definitions declared with capitals, by ASCII-folded name."""

    # (owner qn, folded method name) -> method qn
    methods: dict[tuple[str, str], str]
    # folded class name -> class qns
    classes: dict[str, list[str]]


def php_target_for_namespace_import(
    php: PhpImporter,
    function_registry: FunctionRegistryTrieProtocol,
    imported_path: str,
    kind: cs.NodeLabel | None = None,
) -> str | None:
    """Map a PHP `use function A\\B\\c` target onto the qn that registers it.

    CGR qualifies PHP by file path, so the import target `App.Text.format`
    never equals the registered `project.text.format`. Previously that miss
    dropped through to the simple-name trie, which binds to whichever
    same-named function it reaches first -- so `use function App\\Text\\format`
    could resolve to `App\\Money\\format` (issue #1185). A wrong edge, not a
    missing one.

    Splits the target into the namespace it names and the symbol within it,
    then finds the module that DECLARED that namespace and looks the symbol
    up there. PSR-4 maps a namespace prefix to a directory root, so the two
    cannot be derived from one another -- the declaration is the only link.

    Returns None rather than guessing whenever the answer is not unique:
    no module declares the namespace, or several do (PHP permits one
    namespace across many files, and two files may both define `format`).
    The trie fallback then applies exactly as before, so this can only
    replace an arbitrary binding with a determined one, never introduce a
    new arbitrary one.

    Matching is ASCII CASE-INSENSITIVE, because namespaces and function
    names are in PHP: `use function app\\text\\FORMAT` binds to
    `App\\Text\\format`, verified by executing it under PHP 8.5. Comparing
    with exact casing sent such imports to the trie, which is the same
    wrong-edge path this function exists to avoid.

    ASCII-only rather than `str.casefold()`: PHP folds A-Z only, so
    folding non-ASCII identifiers would match names the language treats as
    distinct -- trading a missed binding for a wrong one. The registered
    qualified name is returned unchanged; folding is for COMPARISON, never
    for the value handed back, which must stay the graph's real key.
    """
    # A fully-qualified import (`use function \App\Text\format`) is
    # idiomatic PHP and runs identically to the unqualified spelling,
    # verified under PHP 8.5. The leading backslash becomes a leading
    # separator when the path is dotted, which would never match a
    # declared `App.Text`. Stripping it is a spelling normalisation, not a
    # semantic change -- in PHP a `use` path is ALWAYS resolved from the
    # global namespace whether or not the backslash is written.
    imported_path = imported_path.lstrip(cs.SEPARATOR_DOT)
    namespace, separator, symbol = imported_path.rpartition(cs.SEPARATOR_DOT)
    if not separator or not namespace or not symbol:
        return None
    wanted = _php_fold(namespace)
    namespaces = php.php_module_namespaces
    matches = [
        (module_qn, f"{module_qn}{cs.SEPARATOR_DOT}{symbol}")
        for module_qn, declared in namespaces.items()
        if _php_fold(declared) == wanted
    ]
    found: list[str] = []
    for module_qn, exact_qn in matches:
        # `kind` keeps PHP's separate symbol tables apart: a class and a
        # function may share a name in one namespace (issue #2466).
        if exact_qn in function_registry and (
            kind is None or function_registry[exact_qn] == kind
        ):
            found.append(exact_qn)
            continue
        # The symbol's casing may differ from the registration too, so a
        # direct lookup can miss where PHP would bind. `find_with_prefix`
        # is the registry's own scoped query -- iterating the whole
        # registry is not part of the trie protocol at all, and would be
        # a full scan per import even where it happened to work.
        #
        # NO trailing separator. The trie splits the prefix on dots and
        # walks a node per part, so `"proj.text."` yields a final EMPTY
        # part that is never a key and the lookup always returns []. An
        # earlier version passed the dot and the whole case-insensitive
        # fallback was silently dead (#1484). The
        # boundary is re-established below by requiring the remainder to
        # start with the separator.
        prefix = module_qn
        wanted_symbol = _php_fold(symbol)
        found.extend(
            qn
            for qn, qn_kind in function_registry.find_with_prefix(prefix)
            if (kind is None or qn_kind == kind)
            # `find_with_prefix` walks whole dot-separated parts, so the
            # remainder always starts with the separator for a real
            # descendant. Requiring it re-establishes the boundary the
            # trailing dot used to provide, and rejects a sibling module
            # whose name merely starts with this one (`proj.textutil`).
            and (remainder := qn[len(prefix) :]).startswith(cs.SEPARATOR_DOT)
            # Direct children only: a nested `module.Class.method` is not
            # the free function this import names.
            and cs.SEPARATOR_DOT not in remainder[1:]
            and _php_fold(remainder[1:]) == wanted_symbol
        )
    if len(found) != 1:
        return None
    return found[0]


def is_php_function_import(
    php: PhpImporter, call_name: str, module_qn: str | None
) -> bool:
    """Whether `call_name` arrived via `use function`, not a class `use`.

    Returns False when `module_qn` is unknown rather than defaulting to
    True: an unidentified module cannot demonstrate the import was a
    function binding, and the fallback's whole purpose is to avoid
    asserting a target it cannot justify. The trie fallback still applies,
    so this can only decline to add an edge, never remove a correct one.
    """
    if module_qn is None:
        return False
    imported = php.php_function_imports.get(module_qn, frozenset())
    if call_name in imported:
        return True
    # PHP function names are case-insensitive at the CALL SITE too, so
    # `use function App\Text\format` followed by `FORMAT()` is valid and
    # runs. Verified by executing it under PHP 8.5. An exact-case lookup
    # here recognised the import but not the call, so the resolver
    # returned None and the call fell to the trie -- the same wrong-edge
    # path, reached through the alias rather than the target.
    folded = _php_fold(call_name)
    return any(_php_fold(name) == folded for name in imported)


def php_import_key(call_name: str, import_map: dict[str, str]) -> str | None:
    """The import-map key matching `call_name` under PHP case folding.

    Returns None unless EXACTLY ONE key folds to the same name. Two keys
    differing only in case cannot both be the intended target, and PHP
    would reject the duplicate `use` at compile time anyway -- so
    declining is correct rather than cautious, and the trie fallback still
    applies.
    """
    folded = _php_fold(call_name)
    matches = [key for key in import_map if _php_fold(key) == folded]
    return matches[0] if len(matches) == 1 else None


class PhpResolverHost(ResolverHost, Protocol):
    """What PHP resolution reads and calls on the `CallResolver` that builds it.

    `verdict` is how surely the class path being resolved binds: a global
    path reaching another file's class by name alone marks it a guess. The
    rest is resolution every language shares, a qn's parent, and the
    simple-name fallback's ranking."""

    def parent_qn(self, qualified_name: str) -> str: ...

    def best_trie_candidate(
        self, possible_matches: list[str], module_qn: str
    ) -> str: ...


class PhpResolver:
    """Binds PHP class paths and the methods PHP runs on them.

    Built and owned by `CallResolver`. The registry, the import processor
    (whose `PhpImporter` records each module's declared namespace), the type
    inference engine and the definition facts are the resolver's own, held
    by reference and read as they stand when a call resolves. The declared
    namespace and capitalised-name memos are this resolver's: the watch pass
    clears them with the resolver's other caches."""

    __slots__ = (
        "_host",
        "function_registry",
        "import_processor",
        "type_inference",
        "facts",
        "_php_class_namespaces",
        "_php_cased_index",
    )

    def __init__(
        self,
        host: PhpResolverHost,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        facts: DefinitionFacts,
    ) -> None:
        self._host = host
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        self.facts = facts
        # {PHP module qn: {class qn: folded namespace it is declared in}},
        # read off each file's declarations on first use (issue #2466).
        self._php_class_namespaces: dict[str, dict[str, PhpClassPath]] = {}
        self._php_cased_index: _PhpCasedIndex | None = None

    def reset_resolution_memos(self) -> None:
        # A re-parsed PHP file may have moved a class between namespaces.
        self._php_class_namespaces.clear()
        self._php_cased_index = None

    def php_class_path(
        self, path: PhpClassPath, module_qn: str
    ) -> tuple[str, str] | None:
        """The class a fully qualified PHP class path names (issue #2466).

        A class is a candidate only where PHP declares it in the namespace the
        path spells. A file with one statement-form namespace records it per
        module, which also matches a class written in another case; a class
        in a braced block is registered under that block (`mod.Vendor.Box`)
        and the file records nothing, so its declaration says where it lives.
        A namespaced path binds exactly or not at all. A global path binds
        the caller's own class exactly and another file's by name only.
        """
        *namespace, symbol = path
        if namespace and (
            class_qn := php_target_for_namespace_import(
                self.import_processor.php,
                self.function_registry,
                cs.SEPARATOR_DOT.join(path),
                kind=cs.NodeLabel.CLASS,
            )
        ):
            return self.function_registry[class_qn], class_qn
        wanted = tuple(_php_fold(part) for part in namespace)
        candidates = [
            qn
            for qn in self._php_class_candidates(symbol)
            if self._php_declared_namespace(qn) == wanted
        ]
        if namespace:
            # Several files declaring one namespaced class is a conditional
            # declaration this site cannot choose between.
            if len(candidates) != 1:
                return None
            return self.function_registry[candidates[0]], candidates[0]
        if own := next(
            (qn for qn in candidates if self._host.parent_qn(qn) == module_qn), None
        ):
            return self.function_registry[own], own
        if not candidates:
            return None
        best = self._host.best_trie_candidate(candidates, module_qn)
        self._host.verdict = cs.EdgeResolution.HEURISTIC
        return self.function_registry[best], best

    def _php_class_candidates(self, symbol: str) -> list[str]:
        # Every class whose name PHP would match to `symbol`, which compares
        # class names ASCII case-insensitively: as written, declared in lower
        # case, or declared with capitals (`new box()` builds `Box`). Each is
        # named as declared, which is what the edge must target.
        folded = _php_fold(symbol)
        found = dict.fromkeys(
            [
                *self.function_registry.find_ending_with(symbol),
                *self.function_registry.find_ending_with(folded),
                *self._php_cased_definitions().classes.get(folded, ()),
            ]
        )
        return [qn for qn in found if self.function_registry[qn] == cs.NodeLabel.CLASS]

    def _php_declared_namespace(self, class_qn: str) -> PhpClassPath | None:
        # The ASCII-folded namespace PHP declares `class_qn` in; None when its
        # file is not PHP or its declaration cannot be read this run, which
        # never passes for the global namespace.
        if self._host.module_language(class_qn) != cs.SupportedLanguage.PHP:
            return None
        module_qn = self._php_module_of(class_qn)
        if module_qn is None:
            return None
        declared = self._php_class_namespaces.get(module_qn)
        if declared is None:
            root = self._host.cached_module_root(module_qn)
            declared = (
                {
                    qn: tuple(_php_fold(part) for part in namespace)
                    for qn, namespace in php_class_names.declared_class_namespaces(
                        root, module_qn
                    ).items()
                }
                if root is not None
                else {}
            )
            self._php_class_namespaces[module_qn] = declared
        return declared.get(class_qn)

    def _php_module_of(self, qualified_name: str) -> str | None:
        modules = self.type_inference.module_qn_to_file_path
        probe = qualified_name
        while cs.SEPARATOR_DOT in probe:
            probe = self._host.parent_qn(probe)
            if probe in modules:
                return probe
        return None

    def php_constructor(self, class_qn: str) -> tuple[str, str] | None:
        """The `__construct` a `new` of `class_qn` runs: the class's own, else
        the nearest one it inherits, which PHP runs in its place."""
        return self.php_method(class_qn, cs.PHP_METHOD_CONSTRUCT)

    def php_method(self, class_qn: str, method_name: str) -> tuple[str, str] | None:
        # PHP method names are ASCII case-insensitive, so a child's
        # `__CONSTRUCT` overrides its parent's `__construct`: each class is
        # searched in any casing before its ancestors are, and the edge names
        # the method as declared.
        wanted = _php_fold(method_name)
        queue = deque([class_qn])
        seen = {class_qn}
        while queue:
            owner = self._host.follow_reexports(queue.popleft())
            if (
                method_qn := self._php_declared_method(owner, method_name, wanted)
            ) is not None:
                return self.function_registry[method_qn], method_qn
            for parent in self.facts.class_inheritance.get(owner, []):
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)
        return None

    def _php_declared_method(
        self, owner_qn: str, method_name: str, folded: str
    ) -> str | None:
        for spelling in (method_name, folded):
            method_qn = f"{owner_qn}{cs.SEPARATOR_DOT}{spelling}"
            if self.function_registry.get(method_qn) == cs.NodeLabel.METHOD:
                return method_qn
        return self._php_cased_definitions().methods.get((owner_qn, folded))

    def _php_cased_definitions(self) -> _PhpCasedIndex:
        # PHP classes and methods declared with an upper-case letter, by
        # folded name. A lookup by the written or the folded spelling misses
        # them (`box` for `Box`, `__construct` for `__CONSTRUCT`), and the
        # registry's prefix walk cannot list a method like `__CONSTRUCT`,
        # since the trie skips every `__`-prefixed key as one of its markers.
        if self._php_cased_index is None:
            index = _PhpCasedIndex({}, {})
            for qn, kind in self.function_registry.items():
                if kind not in (cs.NodeLabel.CLASS, cs.NodeLabel.METHOD):
                    continue
                owner_qn, _, leaf = qn.rpartition(cs.SEPARATOR_DOT)
                if (folded := _php_fold(leaf)) == leaf or (
                    self._host.module_language(owner_qn) != cs.SupportedLanguage.PHP
                ):
                    continue
                if kind == cs.NodeLabel.METHOD:
                    index.methods.setdefault((owner_qn, folded), qn)
                else:
                    index.classes.setdefault(folded, []).append(qn)
            self._php_cased_index = index
        return self._php_cased_index
