"""The callers a file's call pass walks, and the calls each one owns.

Every call node is attributed to the function, method or module whose body
holds it. The `CallerScan` the call pass builds walks a file's function
captures and the methods of each class body it can name, gives each caller
the qualified name and label the definition pass registered for it (its
recorded location first, then the way that pass builds one), slices its
share of the file's calls (`call_ownership`), and hands it to the pass's
caller pass with its class context. It calls back through `CallerHost` for
the caller pass, the recorded locations and a language's call strategy."""

from collections.abc import Mapping
from typing import NamedTuple, Protocol

from loguru import logger
from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LanguageSpec
from ..node_text import sorted_captures
from ..types_defs import LanguageQueries
from .call_host import CallStrategyHost, RecordedCallerHost
from .call_ownership import (
    RegisteredNames,
    body_function_nodes,
    calls_in_node,
    calls_owned_by,
    top_level_calls,
)
from .class_ingest.identity import build_nested_qualified_name_for_class
from .language_calls import LanguageCalls
from .utils import enclosing_class_node, get_function_captures


class FileCalls(NamedTuple):
    """A file's call nodes as its callers share them out: the combined
    captures it was scanned with (None: each caller queries its own), the
    call nodes in start order with their start offsets and the function
    nodes' (None where no function or class capture gives the slices a
    boundary), and the call names already read."""

    captures: dict[str, list[Node]] | None = None
    call_nodes: list[Node] | None = None
    call_starts: list[int] | None = None
    func_node_starts: list[int] | None = None
    call_name_cache: dict[int, str | None] | None = None

    @property
    def func_nodes(self) -> list[Node] | None:
        """The file's function captures in start order, where it has them."""
        if self.captures is None:
            return None
        return self.captures.get(cs.CAPTURE_FUNCTION)


# Scanned with nothing shared: each caller queries its own calls.
NO_FILE_CALLS = FileCalls()


def _class_qn_for_calls(
    class_node: Node,
    module_qn: str,
    class_name: str,
    calls: LanguageCalls,
    lang_config: LanguageSpec,
) -> str:
    # A C++ class inside a namespace, or a NESTED class (Outer.Inner), is bound
    # by the definition pass through its enclosing scope (qn `module.ns.Class`
    # / `module.Outer.Inner`); the bare `module.class_name` join drops those
    # ancestors, dangling every inline method's CALLS source off a phantom
    # node. Use the SAME builders the definition pass uses so the qns agree.
    return (
        calls.class_qn(class_node, module_qn, class_name)
        or build_nested_qualified_name_for_class(
            class_node, module_qn, class_name, lang_config
        )
        or f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
    )


class CallerHost(CallStrategyHost, RecordedCallerHost, Protocol):
    """What the caller scan calls on the call pass that builds it: the caller
    pass a function, method or module's calls go through, where the
    definition pass recorded a function node, the record of a caller whose
    pass failed, and a language's call strategy."""

    def _record_caller_failure(
        self, message: str, caller: str, error: Exception
    ) -> None: ...

    def _ingest_function_calls(
        self,
        caller_node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None = None,
        call_nodes: list[Node] | None = None,
        call_name_cache: dict[int, str | None] | None = None,
    ) -> None: ...


class CallerScan:
    """Names a file's callers and hands each its share of the file's calls.

    Built and owned by `CallProcessor`."""

    __slots__ = ("_host", "_julia")

    def __init__(self, host: CallerHost, julia: RegisteredNames) -> None:
        self._host = host
        # Julia's registered names decide which of its bodiless definitions
        # own their calls.
        self._julia = julia

    def ingest_function_callers(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        file_calls: FileCalls = NO_FILE_CALLS,
    ) -> None:
        """Run the caller pass of every function the class pass leaves out."""
        if combined_captures := file_calls.captures:
            lang_config = queries[language][cs.QUERY_CONFIG]
            func_nodes = combined_captures.get(cs.CAPTURE_FUNCTION, [])
            has_classes = bool(combined_captures.get(cs.CAPTURE_CLASS))
        else:
            result = get_function_captures(root_node, language, queries)
            if not result:
                return
            lang_config, captures = result
            func_nodes = captures.get(cs.CAPTURE_FUNCTION, [])
            has_classes = bool(captures.get(cs.CAPTURE_CLASS))
        # Rust only: calls inside a NAMED nested `fn` (which gets its own caller
        # node) must be owned by that nested fn, not double-counted onto the
        # enclosing free function. Anonymous closures (not attributable) stay
        # excluded so their calls still bubble up. Other languages keep the flat
        # calls_in_node behavior their flow-tracing relies on.
        owned_func_nodes = self._attributable_func_nodes(
            func_nodes, language, module_qn
        )
        language_calls = self._host.calls_for(language)
        for func_node in func_nodes:
            # Anything scoped inside a class body -- a method, and a function
            # nested in a method -- is walked by ingest_class_body_callers
            # with the class context set. Walking it here too emitted every
            # call edge twice, and once the class pass could type `self`
            # the second copy was a false bare-name edge (issue #1903).
            # Skip only what that pass will actually walk: it drops a class
            # it cannot name (a JS mixin's `(Base) => class extends Base`),
            # and those nested functions would otherwise lose every edge.
            if has_classes and self._class_pass_owns(func_node, lang_config, language):
                continue

            # The caller name a function node's body calls are attributed
            # under; None means the node gets no caller pass and is skipped.
            func_name = language_calls.caller_name(func_node, module_qn)
            if func_name is None:
                continue
            try:
                self._ingest_func_node_calls(
                    func_node,
                    func_name,
                    module_qn,
                    language,
                    queries,
                    lang_config,
                    file_calls,
                    owned_func_nodes,
                )
            except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
                # One pathological function costs its own edges, not every
                # other function's in the file (issue #3173).
                loc = self._host.recorded_caller(func_node, module_qn)
                caller = (
                    loc.qualified_name
                    if loc is not None
                    else f"{module_qn}{cs.SEPARATOR_DOT}{func_name}"
                )
                self._host._record_caller_failure(ls.CALLER_CALLS_FAILED, caller, e)

    def _ingest_func_node_calls(
        self,
        func_node: Node,
        func_name: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        lang_config: LanguageSpec,
        file_calls: FileCalls,
        owned_func_nodes: list[Node],
    ) -> None:
        host = self._host
        # The definition pass records where every function/method node
        # landed; reuse that qn/label instead of re-deriving them from
        # the AST -- the walks diverge on preprocessor-distorted C++
        # class bodies, TS declaration merging, and duplicate-suffixed
        # qns, and every divergence is a phantom caller (issue #652).
        if loc := host.recorded_caller(func_node, module_qn):
            filtered = self._filtered_calls_for(
                func_node, language, file_calls, owned_func_nodes
            )
            host._ingest_function_calls(
                func_node,
                loc.qualified_name,
                loc.label,
                module_qn,
                language,
                queries,
                loc.container_qn,
                call_nodes=filtered,
                call_name_cache=file_calls.call_name_cache,
            )
            return
        # A method written outside its type's body (a C++ out-of-line
        # definition, a Go receiver method) is bound by the definition pass
        # to that type's node. Attribute its body's calls to that method
        # node, not a phantom module-rooted qn, so the CALLS edges join to a
        # real node.
        language_calls = host.calls_for(language)
        if bound := language_calls.bound_method_caller(func_node, func_name, module_qn):
            caller_qn, container_qn = bound
            filtered = self._filtered_calls_for(
                func_node, language, file_calls, owned_func_nodes
            )
            host._ingest_function_calls(
                func_node,
                caller_qn,
                cs.NodeLabel.METHOD,
                module_qn,
                language,
                queries,
                container_qn,
                call_nodes=filtered,
                call_name_cache=file_calls.call_name_cache,
            )
            return
        # The qn the definition pass builds, through the language's own
        # builder where it has one, so the qns agree.
        func_qn = language_calls.function_qn(
            func_node, module_qn, func_name
        ) or self.build_nested_qualified_name(
            func_node, module_qn, func_name, lang_config
        )
        if func_qn:
            filtered = self._filtered_calls_for(
                func_node, language, file_calls, owned_func_nodes
            )
            host._ingest_function_calls(
                func_node,
                func_qn,
                cs.NodeLabel.FUNCTION,
                module_qn,
                language,
                queries,
                call_nodes=filtered,
                call_name_cache=file_calls.call_name_cache,
            )

    def _filtered_calls_for(
        self,
        func_node: Node,
        language: cs.SupportedLanguage,
        file_calls: FileCalls,
        owned_func_nodes: list[Node],
    ) -> list[Node] | None:
        # The caller's own calls, sliced from the module-wide call list. A Rust
        # named nested fn owns its body's calls, so its slice must exclude them
        # rather than take the whole byte range; every other language filters by
        # the plain byte span.
        all_call_nodes, call_starts = file_calls.call_nodes, file_calls.call_starts
        if all_call_nodes is None or call_starts is None:
            return None
        language_calls = self._host.calls_for(language)
        if language_calls.owns_nested_calls:
            return calls_owned_by(
                func_node, owned_func_nodes, all_call_nodes, call_starts
            )
        return language_calls.caller_calls(
            func_node, calls_in_node(all_call_nodes, call_starts, func_node)
        )

    def ingest_class_callers(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        file_calls: FileCalls = NO_FILE_CALLS,
    ) -> None:
        """Run the caller pass of every function in the bodies of the
        classes the language can name."""
        if file_calls.captures is not None:
            class_nodes = file_calls.captures.get(cs.CAPTURE_CLASS, [])
        else:
            query = queries[language][cs.QUERY_CLASSES]
            if not query:
                return
            cursor = QueryCursor(query)
            captures = sorted_captures(cursor, root_node)
            class_nodes = captures.get(cs.CAPTURE_CLASS, [])

        language_calls = self._host.calls_for(language)
        for class_node in class_nodes:
            class_name = language_calls.class_name(class_node)
            if not class_name:
                continue
            class_qn = _class_qn_for_calls(
                class_node,
                module_qn,
                class_name,
                language_calls,
                queries[language][cs.QUERY_CONFIG],
            )
            if body_node := class_node.child_by_field_name(cs.FIELD_BODY):
                self.ingest_class_body_callers(
                    body_node, class_qn, module_qn, language, queries, file_calls
                )

    def ingest_class_body_callers(
        self,
        body_node: Node,
        class_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        file_calls: FileCalls = NO_FILE_CALLS,
    ) -> None:
        """Run the caller pass of every function a class body holds, with the
        class as its context."""
        method_nodes = body_function_nodes(
            body_node,
            queries[language][cs.QUERY_FUNCTIONS],
            file_calls.func_nodes,
            file_calls.func_node_starts,
        )
        if method_nodes is None:
            return
        host = self._host
        lang_config = queries[language][cs.QUERY_CONFIG]
        # Only functions that get their own caller node exclude their calls from
        # the enclosing scope; anonymous arrows (skipped below) must not, so
        # their calls bubble up instead of dropping.
        owned_func_nodes = self._attributable_func_nodes(
            method_nodes, language, module_qn
        )
        all_call_nodes, call_starts = file_calls.call_nodes, file_calls.call_starts
        for method_node in method_nodes:
            # The body byte-range slice also captures functions of a NESTED
            # class (Outer body contains Inner.run); those belong to the
            # nested class and are processed when it is iterated, so skip any
            # whose nearest enclosing class is not this one (else run also
            # emits as the phantom Outer.run).
            if not self._method_in_class_body(method_node, body_node, lang_config):
                continue
            method_name = host.calls_for(language).method_name(method_node, module_qn)
            if not method_name:
                continue
            # method_nodes includes functions nested inside methods. Build the
            # qn through the enclosing-function chain (Class.method.nested, not
            # the method-dropping Class.nested) and label a nested function
            # FUNCTION, so the CALLS edge joins the real node.
            class_context = class_qn
            if loc := host.recorded_caller(method_node, module_qn):
                # Reuse the definition pass's recorded qn/label; the class
                # walk's structural derivation diverges from it on
                # preprocessor-distorted C++ class bodies and on TS
                # declaration merging (issue #652).
                caller_qn, caller_label = loc.qualified_name, loc.label
                class_context = loc.container_qn or class_qn
            else:
                caller_qn, caller_label = self.class_member_qn_and_label(
                    method_node, class_qn, method_name, lang_config, language
                )
            filtered = (
                calls_owned_by(
                    method_node, owned_func_nodes, all_call_nodes, call_starts
                )
                if all_call_nodes is not None and call_starts is not None
                else None
            )
            try:
                host._ingest_function_calls(
                    method_node,
                    caller_qn,
                    caller_label,
                    module_qn,
                    language,
                    queries,
                    class_context,
                    call_nodes=filtered,
                    call_name_cache=file_calls.call_name_cache,
                )
            except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
                # Contained to this method, as for a free function (#3173).
                host._record_caller_failure(ls.CALLER_CALLS_FAILED, caller_qn, e)

    def class_member_qn_and_label(
        self,
        func_node: Node,
        class_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
        language: str,
    ) -> tuple[str, str]:
        """A class-body function's qn and label, built through the chain of
        enclosing functions up to the class."""
        # A direct method is Class.method (METHOD); a function nested in a
        # method is Class.method.nested (FUNCTION).
        path_parts: list[str] = []
        current = func_node.parent
        while current and current.type not in lang_config.class_node_types:
            if (
                current.type in lang_config.function_node_types
                and (name_node := current.child_by_field_name(cs.FIELD_NAME))
                and name_node.text is not None
            ):
                path_parts.append(name_node.text.decode(cs.ENCODING_UTF8))
            current = current.parent
        path_parts.reverse()
        if path_parts:
            joined = cs.SEPARATOR_DOT.join([*path_parts, func_name])
            return f"{class_qn}{cs.SEPARATOR_DOT}{joined}", cs.NodeLabel.FUNCTION
        member = self._host.calls_for(language).member_name(func_node, func_name)
        return f"{class_qn}{cs.SEPARATOR_DOT}{member}", cs.NodeLabel.METHOD

    @staticmethod
    def _method_in_class_body(
        method_node: Node, class_body: Node, lang_config: LanguageSpec
    ) -> bool:
        # True when method_node's nearest enclosing class is the one whose
        # body is class_body: walk up, and the first class ancestor's body
        # must be this body (compared by byte span). A method with no enclosing
        # class (out-of-class C++ definition captured elsewhere) returns True
        # so existing handling is unaffected.
        current = method_node.parent
        while current is not None:
            if current.type in lang_config.class_node_types:
                body = current.child_by_field_name(cs.FIELD_BODY)
                return body is not None and (
                    body.start_byte == class_body.start_byte
                    and body.end_byte == class_body.end_byte
                )
            current = current.parent
        return True

    def module_level_calls(
        self,
        all_call_nodes: list[Node],
        call_starts: list[int] | None,
        sorted_func_nodes: list[Node] | None,
        language: cs.SupportedLanguage,
        module_qn: str,
    ) -> list[Node]:
        """The calls the module owns, outside every function body."""
        if sorted_func_nodes and call_starts is not None:
            # Where an anonymous function gets no caller pass (JS/TS),
            # exclude only calls owned by ATTRIBUTABLE functions, so a call
            # nested purely in anonymous scopes (`create((set) => ({ inc: ()
            # => set((state) => ...) }))`, zustand's store shape) is
            # processed at module scope instead of by nobody, and a call
            # inside a NAMED function is still excluded because that
            # function's flat filter owns it.
            exclusion_nodes = (
                self._attributable_func_nodes(sorted_func_nodes, language, module_qn)
                if self._host.calls_for(language).anonymous_calls_reach_module
                else sorted_func_nodes
            )
            return top_level_calls(
                all_call_nodes, call_starts, exclusion_nodes, module_qn, self._julia
            )
        return all_call_nodes

    def build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        """A function's qn through its enclosing functions, as the definition
        pass builds it; None inside a class, whose pass names it."""
        path_parts: list[str] = []
        current = func_node.parent

        if not isinstance(current, Node):
            logger.warning(
                ls.CALL_UNEXPECTED_PARENT, node=func_node, parent_type=type(current)
            )
            return None

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                part = self._ancestor_function_name(current, lang_config)
                if part is not None:
                    path_parts.append(part)
            elif current.type in lang_config.class_node_types:
                return None

            current = current.parent

        path_parts.reverse()
        if path_parts:
            return f"{module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(path_parts)}{cs.SEPARATOR_DOT}{func_name}"
        return f"{module_qn}{cs.SEPARATOR_DOT}{func_name}"

    def _ancestor_function_name(
        self, function_node: Node, lang_config: LanguageSpec
    ) -> str | None:
        name_node = function_node.child_by_field_name(cs.FIELD_NAME)
        if name_node is not None:
            if name_node.text is None:
                return None
            return name_node.text.decode(cs.ENCODING_UTF8)
        return self._host.calls_for(lang_config.language).scope_name(function_node)

    def _attributable_func_nodes(
        self, func_nodes: list[Node], language: cs.SupportedLanguage, module_qn: str
    ) -> list[Node]:
        # The func nodes that will get their own caller node: named functions
        # plus arrows/function-expressions bound to a name. An anonymous arrow
        # passed directly as an argument (`hooks.tap(name, (x) => {...})`) has
        # neither, so the call loop skips it. Its calls must therefore NOT be
        # excluded from the enclosing named scope by calls_owned_by; otherwise
        # they attribute to nothing and drop (webpack is saturated with this
        # callback pattern). Returning only attributable nodes as the exclusion
        # set lets an anonymous arrow's calls bubble up to the nearest named
        # function/method, matching where the oracle attributes them.
        return self._host.calls_for(language).attributable(func_nodes, module_qn)

    def _class_pass_owns(
        self, func_node: Node, lang_config: LanguageSpec, language: cs.SupportedLanguage
    ) -> bool:
        # Mirrors ingest_class_callers: a class with a body that it can
        # name gets a class pass; everything inside it is that pass's.
        class_node = enclosing_class_node(func_node, lang_config)
        if class_node is None:
            return False
        if class_node.child_by_field_name(cs.FIELD_BODY) is None:
            return False
        return bool(self._host.calls_for(language).class_name(class_node))
