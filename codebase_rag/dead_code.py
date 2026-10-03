# Dead-code reachability engine. Roots (entry points, framework hooks,
# module-load callees, test code) expand over CALLS/REFERENCES edges;
# whatever is never reached is reported. Reachability runs client-side in
# Python: the per-root *BFS Cypher formulation is O(roots x graph) and hit
# memgraph's 600s timeout on big projects (django: 31k roots, 101k CALLS
# edges), whereas a multi-source walk over the fetched edges is linear and
# finishes in milliseconds.
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch

from . import constants as cs
from . import cypher_queries as cq
from .path_filters import matches_test_path
from .types_defs import (
    DeadCodeConfig,
    GraphQueryClient,
    PropertyDict,
    PropertyValue,
    ResultRow,
    ResultValue,
)
from .utils import qn_markers

_MODULE = cs.NodeLabel.MODULE.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value
_CLASS = cs.NodeLabel.CLASS.value
_CALLS = cs.RelationshipType.CALLS.value
_REFERENCES = cs.RelationshipType.REFERENCES.value
_INSTANTIATES = cs.RelationshipType.INSTANTIATES.value
_INHERITS = cs.RelationshipType.INHERITS.value
_DEFINES = cs.RelationshipType.DEFINES.value
_DEFINES_METHOD = cs.RelationshipType.DEFINES_METHOD.value
_OVERRIDES = cs.RelationshipType.OVERRIDES.value
_IMPLEMENTS = cs.RelationshipType.IMPLEMENTS.value
_JS_TS_EXTS = cs.TS_EXTENSIONS + cs.TSX_EXTENSIONS + cs.JS_EXTENSIONS
_NodeId = tuple[str, PropertyValue]
_RelTuple = tuple[str, PropertyValue, str, str, PropertyValue]


# The relationships that carry a resolution label; structural ones (INHERITS,
# IMPLEMENTS, OVERRIDES, DEFINES, DEFINES_METHOD) never do and must survive any
# confidence floor, or the walk loses the paths that keep overrides, protocol
# stubs and nested registered definitions alive (issue #1526).
_RESOLUTION_LABELLED_RELS = frozenset(
    {
        cs.RelationshipType.CALLS.value,
        cs.RelationshipType.REFERENCES.value,
        cs.RelationshipType.INSTANTIATES.value,
    }
)


def _passes_floor(row: ResultRow, minimum: str | None) -> bool:
    if str(row.get(cs.KEY_REL_TYPE) or "") not in _RESOLUTION_LABELLED_RELS:
        return True
    return resolution_at_least(row.get(cs.KEY_RESOLUTION), minimum)


def resolution_at_least(resolution: ResultValue | None, minimum: str | None) -> bool:
    """Whether an edge's confidence meets `--min-resolution` (issue #1526).

    An edge without the label predates the label (or was emitted by a pass
    that binds exactly) and ranks as exact; an unknown label never passes.
    """
    if minimum is None:
        return True
    floor = cs.RESOLUTION_RANK.get(minimum, 0)
    label = str(resolution) if isinstance(resolution, str) else cs.EdgeResolution.EXACT
    return cs.RESOLUTION_RANK.get(label, 0) >= floor


def default_dead_code_config(
    include_tests: bool,
    include_classes: bool,
    exclude_patterns: tuple[str, ...] = (),
) -> DeadCodeConfig:
    return DeadCodeConfig(
        include_tests=include_tests,
        include_classes=include_classes,
        root_decorators=frozenset(d.lower() for d in cs.DEFAULT_ROOT_DECORATORS),
        entry_points=(),
        test_patterns=tuple(cs.TEST_PATH_PATTERNS),
        exclude_patterns=exclude_patterns,
    )


def normalize_decorator_root(decorator: str) -> str:
    """A decorator's dotted head, lowercased: `@app.route(...)` becomes
    `app.route`. Drops '@', any surrounding attribute brackets and the
    arguments, so a C# `[Route("x")]` becomes `route`. The same reading
    applies to a stored decorator and to a user's `--decorator-root`, which
    may be written `@registry.register` or `registry.register()`."""
    cleaned = decorator.replace(cs.DECORATOR_AT, "").strip("[] ")
    return cleaned.split(cs.CHAR_PAREN_OPEN)[0].strip("[] ").lower()


def _norm_decorator(decorator: str) -> str:
    # The last dotted segment of the head: `@app.route(...)` -> `route`, the
    # form the built-in roots are written in.
    return normalize_decorator_root(decorator).split(cs.SEPARATOR_DOT)[-1]


def _is_root_decorator(decorator: str, root_decorators: frozenset[str]) -> bool:
    """A bare root names the decorator's last segment, so `register` matches
    any `@x.register`. A dotted root names the head's trailing whole
    segments: `registry.register` matches `@registry.register(...)` and
    `@app.registry.register`, not `@other.register` or
    `@myregistry.register`. Comparing only the last segment made a dotted
    root, the documented form, match nothing at all (issue #2640)."""
    if _norm_decorator(decorator) in root_decorators:
        return True
    head = normalize_decorator_root(decorator)
    return any(
        cs.SEPARATOR_DOT in root
        and (head == root or head.endswith(cs.SEPARATOR_DOT + root))
        for root in root_decorators
    )


def _is_dunder(name: str) -> bool:
    # A __dunder__ method is invoked by the Python runtime (async with,
    # iteration, operators), never by an explicit call the graph can see, so it
    # is a reachability root, not dead code.
    return (
        len(name) > len(cs.PY_NAME_DUNDER) * 2
        and name.startswith(cs.PY_NAME_DUNDER)
        and name.endswith(cs.PY_NAME_DUNDER)
    )


def _is_rust_runtime_root(name: str, is_method: bool, path: str) -> bool:
    # A Rust `.rs` symbol the language/runtime invokes with no call site: `fn
    # main()` (entry) or a trait-impl method (Display::fmt, Iterator::next).
    # Name-scoped like Python dunders; trait methods must be methods.
    if not path.endswith(cs.EXT_RS):
        return False
    # `main` is only the entry point as a receiverless `fn main()`; a method
    # named main is not, so gate it to non-methods. Trait methods are the reverse.
    if name in cs.RUST_ROOT_FUNCTION_NAMES:
        return not is_method
    return is_method and name in cs.RUST_TRAIT_METHOD_NAMES


def _has_rust_test_attribute(props: PropertyDict) -> bool:
    decorators = props.get(cs.KEY_DECORATORS)
    if not isinstance(decorators, list):
        return False
    for decorator in decorators:
        head = str(decorator).strip("#[] ").split(cs.CHAR_PAREN_OPEN)[0]
        # Attribute paths are token streams: `#[tokio :: test]` names the
        # same attribute as `#[tokio::test]`, so drop internal whitespace
        # before matching.
        name = "".join(head.split())
        if name in cs.RUST_TEST_ATTRIBUTE_NAMES or name.endswith(
            cs.RUST_TEST_ATTRIBUTE_SUFFIX
        ):
            return True
    return False


def _is_rust_test_symbol(
    props: PropertyDict, qn: str, path: str, rust_test_modules: set[str]
) -> bool:
    # Rust unit tests live INSIDE source files (`#[cfg(test)] mod tests`), so
    # path-based test detection never sees them (issue #1008). Test code is a
    # function carrying a `#[test]` family attribute (#[test], #[tokio::test],
    # #[bench]) or any symbol inside a `tests`/`test` MODULE (the #[cfg(test)]
    # convention), including the plain helpers such modules define. The
    # module check walks the qn's PREFIXES against real Module qns: a type
    # or method named `tests`, or a dotted project name, shares the string
    # shape but has no Module node and must stay reportable.
    if not path.endswith(cs.EXT_RS):
        return False
    if rust_test_modules:
        prefix = ""
        for segment in qn.split(cs.SEPARATOR_DOT)[:-1]:
            prefix = f"{prefix}{cs.SEPARATOR_DOT}{segment}" if prefix else segment
            if prefix in rust_test_modules:
                return True
    return _has_rust_test_attribute(props)


def _rust_test_modules_from_nodes(
    nodes: dict[_NodeId, PropertyDict],
) -> set[str]:
    # Test modules by three signals, all gated to Rust files (inline `mod
    # tests` blocks carry synthesised inline paths): a test-module NAME
    # spelling, an OWN `#[cfg(test)]` decorator (bodied inline mods), or a
    # DECLARATION-recorded gate (`#[cfg(test)] mod testutil;` stores its
    # target-qn candidates on the declaring module, issue #1010). Declared
    # candidates count only when they name a real Rust module here: a
    # spelling the qn scheme does not produce (a #[path] override) must
    # stay inert instead of mismarking whatever shares the string.
    modules: set[str] = set()
    rust_modules: set[str] = set()
    declared: list[str] = []
    ungated: set[str] = set()
    for (label, uid), props in nodes.items():
        if label != _MODULE:
            continue
        declared.extend(_str_items(props.get(cs.KEY_RUST_CFG_TEST_MODS)))
        ungated.update(_str_items(props.get(cs.KEY_RUST_UNGATED_MODS)))
        if not _is_rust_module_path(str(props.get(cs.KEY_PATH, ""))):
            continue
        qn = str(uid)
        rust_modules.add(qn)
        if _is_rust_test_module(qn, props):
            modules.add(qn)
    # An ungated declaration from ANY target (src/main.rs compiling the
    # module for production) outweighs a gated sibling declaration.
    modules.update(
        target
        for target in declared
        if target in rust_modules and target not in ungated
    )
    return modules


def _str_items(value: PropertyValue | None) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _is_rust_module_path(path: str) -> bool:
    return path.endswith(cs.EXT_RS) or path.startswith(cs.INLINE_MODULE_PATH_PREFIX)


def _is_rust_test_module(qn: str, props: PropertyDict) -> bool:
    return qn.rsplit(cs.SEPARATOR_DOT, 1)[
        -1
    ] in cs.RUST_TEST_MODULE_SEGMENTS or _has_rust_cfg_test_gate(props)


def _has_rust_cfg_test_gate(props: PropertyDict) -> bool:
    decorators = props.get(cs.KEY_DECORATORS)
    if not isinstance(decorators, list):
        return False
    return any(
        "".join(str(decorator).split()) == cs.RS_CFG_TEST_ATTRIBUTE
        for decorator in decorators
    )


def _rust_test_fn_spans(
    nodes: dict[_NodeId, PropertyDict],
) -> dict[str, list[tuple[int, int]]]:
    # Spans of `#[test]` family functions per Rust file: a nested fn
    # registers FLAT under the module qn and a closure as
    # anonymous_<line>_<col>, so with tests excluded, symbols lexically
    # inside a suppressed test fn are recognised by span containment.
    spans: dict[str, list[tuple[int, int]]] = {}
    for (label, uid), props in nodes.items():
        if label not in (_FUNCTION, _METHOD):
            continue
        path = str(props.get(cs.KEY_PATH, ""))
        if not path.endswith(cs.EXT_RS) or not _has_rust_test_attribute(props):
            continue
        start = props.get(cs.KEY_START_LINE)
        end = props.get(cs.KEY_END_LINE)
        if isinstance(start, int) and isinstance(end, int) and start > 0:
            spans.setdefault(path, []).append((start, end))
    return spans


def _is_test_symbol(
    props: PropertyDict,
    qn: str,
    path: str,
    test_patterns: tuple[str, ...],
    rust_test_modules: set[str],
    rust_test_spans: dict[str, list[tuple[int, int]]],
) -> bool:
    # One definition for BOTH polarities: a symbol excluded as test code
    # when tests are off must be the same symbol rooted when they are on,
    # or the two modes silently diverge.
    return (
        matches_test_path(path, test_patterns)
        or _is_rust_test_symbol(props, qn, path, rust_test_modules)
        or _within_rust_test_span(props, rust_test_spans)
    )


def _within_rust_test_span(
    props: PropertyDict, spans: dict[str, list[tuple[int, int]]]
) -> bool:
    path_spans = spans.get(str(props.get(cs.KEY_PATH, "")))
    if not path_spans:
        return False
    start = props.get(cs.KEY_START_LINE)
    end = props.get(cs.KEY_END_LINE)
    if not isinstance(start, int) or not isinstance(end, int) or start <= 0:
        return False
    # Strict on both ends: spans carry no columns, so a production fn
    # packed onto the test fn's first or last physical line must not be
    # swallowed; every rustfmt-shaped nested symbol sits strictly inside.
    return any(s < start and end < e for s, e in path_spans)


def _is_c_cpp_entry_root(
    name: str, is_method: bool, path: str, qn: str, project_prefix: str
) -> bool:
    # A C/C++ program entry (`main`, Windows' `wWinMain`/`WinMain`/`wmain`, a
    # DLL's `DllMain`) is invoked by the OS runtime, never by a call the graph
    # sees, so it roots its whole call tree (an unrooted wWinMain reported all
    # 34 windows/runner symbols of a Flutter desktop shim dead). Only a free
    # function at FILE scope in a translation-unit source counts: a method, a
    # namespace-scoped `main`, or a header-defined `WinMain` is ordinary code
    # the OS cannot invoke. (Linkage is not captured in the graph, so a
    # file-scope `static DllMain` in a source file still roots.)
    if is_method or name not in cs.C_CPP_ENTRY_FUNCTION_NAMES:
        return False
    if not path.endswith(cs.C_CPP_SOURCE_EXTENSIONS):
        return False
    # File scope, exactly: a file-scope definition's qn is the project prefix
    # plus the path's dotted form (extension dropped) plus the name
    # (`proj.runner.main.wWinMain` for runner/main.cpp). A namespace inserts
    # its own segment, even one named like the file stem (`namespace main`
    # in main.cpp), so nothing short of the exact qn earns the root.
    dotted_module = path.rsplit(cs.SEPARATOR_DOT, 1)[0].replace(
        cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT
    )
    return qn == f"{project_prefix}{dotted_module}{cs.SEPARATOR_DOT}{name}"


def _is_cpp_operator_root(name: str, path: str) -> bool:
    # A C++ operator overload / user-defined literal (`operator==`, `operator[]`,
    # `operator""_json`) is invoked by operator/literal SYNTAX, not a named call
    # the graph sees, so it is a reachability root (like Python dunders / Rust
    # trait methods). `operator` heads every such definition (member or free),
    # so the name prefix on a C++ file identifies them.
    return name.startswith(cs.CPP_OPERATOR_PREFIX) and path.endswith(cs.CPP_EXTENSIONS)


def _is_js_well_known_symbol_root(name: str, is_method: bool, path: str) -> bool:
    # A JS/TS class member keyed by a well-known symbol (`[Symbol.iterator]`,
    # `get [Symbol.toStringTag]`) is invoked implicitly by the language
    # runtime (iteration protocol, Object.prototype.toString, using/dispose),
    # never by a name the graph can see, so it is a reachability root (like
    # Python dunders / Rust trait methods). The registered leaf keeps the
    # computed-name brackets, so the `[Symbol.` prefix on a JS/TS file
    # identifies exactly these members; a user symbol key registers without
    # the `Symbol.` path and stays ordinary code.
    if not is_method or not path.endswith(_JS_TS_EXTS):
        return False
    # Formatting must not decide (`[ Symbol.iterator ]`, `[\tSymbol.iterator\t]`,
    # `[Symbol["iterator"]]` spell the same protocol member): compare free of
    # ALL whitespace, accepting the dotted and the bracket-notation access off
    # the Symbol global. The member itself must sit on an allowlist: the
    # well-known set, plus the registry keys runtimes invoke themselves
    # (Node calls `Symbol.for('nodejs.util.inspect.custom')`). An
    # application-defined `Symbol.for('app.tag')` member is reached only by
    # code the graph can see, so rooting it would hide dead protocol members.
    # A string key (`['Symbol.fake']`) or user symbol variable (`[mySym]`)
    # keeps its own spelling and never matches.
    member = _js_symbol_member(name)
    if member is None:
        return False
    if member in cs.JS_WELL_KNOWN_SYMBOLS:
        return True
    return _js_registry_symbol_key(member) in cs.JS_RUNTIME_REGISTRY_SYMBOL_KEYS


def _js_symbol_member(name: str) -> str | None:
    compact = "".join(name.split())
    if not compact.endswith(cs.JS_COMPUTED_NAME_SUFFIX):
        return None
    if compact.startswith(cs.JS_WELL_KNOWN_SYMBOL_NAME_PREFIX):
        return compact[len(cs.JS_WELL_KNOWN_SYMBOL_NAME_PREFIX) : -1]
    if compact.startswith(cs.JS_WELL_KNOWN_SYMBOL_BRACKET_PREFIX):
        return _js_unquote(compact[len(cs.JS_WELL_KNOWN_SYMBOL_BRACKET_PREFIX) : -2])
    return None


def _js_registry_symbol_key(member: str | None) -> str | None:
    if (
        member is None
        or not member.startswith(cs.JS_SYMBOL_FOR_PREFIX)
        or not member.endswith(cs.CHAR_PAREN_CLOSE)
    ):
        return None
    return _js_unquote(member[len(cs.JS_SYMBOL_FOR_PREFIX) : -1])


def _js_unquote(text: str) -> str | None:
    if len(text) > 1 and text[0] == text[-1] and text[0] in cs.JS_STRING_QUOTES:
        return text[1:-1]
    return None


def _is_java_serialization_root(name: str, is_method: bool, path: str) -> bool:
    # A Java serialization hook (`readObject`/`writeObject`/`writeReplace`/
    # `readResolve`/`readObjectNoData`) is invoked reflectively by the java.io
    # runtime, never by a named call the graph sees, so it is a reachability root
    # (like Python dunders / Rust trait methods). Gated to methods on a .java
    # file; `name` is the bare method name (signature stripped by caller).
    return (
        is_method
        and path.endswith(cs.EXT_JAVA)
        and name in cs.JAVA_SERIALIZATION_METHOD_NAMES
    )


def _is_csharp_attribute_root(props: PropertyDict, path: str) -> bool:
    # A C# method carrying a framework/runtime attribute ([Fact], [HttpGet],
    # [OnDeserialized]) is invoked reflectively, never by a call the graph sees,
    # so it is a reachability root. Gated to .cs; the decorator set matches via
    # the normalized (lowercased, arg-stripped) form.
    return path.endswith(cs.EXT_CS) and _has_root_decorator(
        props, cs.CSHARP_ROOT_ATTRIBUTES
    )


def _is_csharp_dispose_root(name: str, is_method: bool, path: str) -> bool:
    # `Dispose`/`DisposeAsync` are invoked by a `using` block's teardown, not
    # a named call; a reachability root on a .cs method (like the Java hooks).
    return (
        is_method
        and path.endswith(cs.EXT_CS)
        and name in cs.CSHARP_DISPOSE_METHOD_NAMES
    )


def _is_csharp_operator_or_finalizer_root(name: str, path: str) -> bool:
    # An operator overload is invoked by operator SYNTAX (`a + b`) and a
    # finalizer (`~Foo`) by the GC, never a named call the graph sees, so both
    # are reachability roots on a .cs file (cf. the C++ operator root). The
    # synthesized leaf carries the `operator_`/`~` prefix.
    return path.endswith(cs.EXT_CS) and (
        name.startswith(cs.TS_CSHARP_OPERATOR_NAME_PREFIX)
        or name.startswith(cs.TS_CSHARP_DESTRUCTOR_NAME_PREFIX)
    )


_CSHARP_TYPE_QUALIFIER_RE = re.compile(cs.CSHARP_TYPE_QUALIFIER_PATTERN)


def _is_csharp_entry_point_root(
    name: str, is_method: bool, path: str, props: PropertyDict
) -> bool:
    # The runtime invokes `static Main` with no call site the graph sees, and
    # the compiler accepts it whatever its accessibility. Most programs
    # declare it without `public`, so the exported rule missed it and Main
    # was reported dead with its whole call tree (issue #2471). The
    # compiler's own signature test decides, so an instance `Main`, a
    # `ValueTask Main` or a `Main(string)` stays ordinary code.
    if not (
        is_method and name == cs.CSHARP_ENTRY_METHOD_NAME and path.endswith(cs.EXT_CS)
    ):
        return False
    params = _str_items(props.get(cs.KEY_PARAM_TYPES))
    return (
        cs.TS_CSHARP_MODIFIER_STATIC in _str_items(props.get(cs.KEY_MODIFIERS))
        and _csharp_type_name(str(props.get(cs.KEY_RETURN_TYPE) or ""))
        in cs.CSHARP_ENTRY_RETURN_TYPES
        and (
            not params
            or (
                len(params) == 1
                and _csharp_args_type(params[0]) in cs.CSHARP_ENTRY_ARGS_TYPES
            )
        )
    )


def _csharp_type_name(type_text: str) -> str:
    # Spelling must not decide: `Task < int >` and
    # `global::System.Threading.Tasks.Task<System.Int32>` are one type.
    return _CSHARP_TYPE_QUALIFIER_RE.sub("", "".join(type_text.split()))


def _csharp_args_type(param_type: str) -> str:
    return _csharp_type_name(param_type.removeprefix(cs.CSHARP_PARAMS_PREFIX)).replace(
        cs.CSHARP_NULLABLE_MARKER, ""
    )


_WELL_KNOWN_SYMBOL_KEY_RE = re.compile(r"\[Symbol\.(?P<name>[A-Za-z_$][\w$]*)\]$")


def is_well_known_symbol_member(name: str) -> bool:
    m = _WELL_KNOWN_SYMBOL_KEY_RE.search(name)
    return m is not None and m.group("name") in cs.JS_WELL_KNOWN_SYMBOLS


def _has_root_decorator(props: PropertyDict, root_decorators: frozenset[str]) -> bool:
    decorators = props.get(cs.KEY_DECORATORS)
    if not isinstance(decorators, list):
        return False
    return any(_is_root_decorator(str(d), root_decorators) for d in decorators)


def _has_non_route_root_decorator(
    props: PropertyDict, root_decorators: frozenset[str]
) -> bool:
    """A root decorator that is not the route itself: with endpoint roots
    off the route stops rooting its handler, a fixture or CLI command on the
    same definition does not (bot review on PR #1975). A dispatch registrar
    (`@task`, `@flow`) is the route of a dispatch endpoint, so it is not one
    either (local review)."""
    from .parsers.endpoints import parse_route_decorator
    from .parsers.io_access.constants import DISPATCH_REGISTRARS

    decorators = props.get(cs.KEY_DECORATORS)
    if not isinstance(decorators, list):
        return False
    return any(
        _is_root_decorator(str(d), root_decorators)
        and _norm_decorator(str(d)) not in DISPATCH_REGISTRARS
        and not parse_route_decorator(str(d))
        for d in decorators
    )


def _is_nest_component_class(
    class_qn: str,
    class_decorators_norm: dict[str, frozenset[str]],
    nest_factory_classes: set[str],
) -> bool:
    if class_qn in nest_factory_classes:
        return True
    decorators = class_decorators_norm.get(class_qn)
    return bool(decorators and decorators & cs.NEST_ROOT_CLASS_DECORATORS)


def _is_nest_root(
    qn: str,
    member: str,
    is_method: bool,
    path: str,
    method_to_class: dict[str, str],
    class_decorators_norm: dict[str, frozenset[str]],
    nest_factory_classes: set[str],
) -> bool:
    # NestJS runs code the static graph sees no call to. A class decorated
    # @Injectable/@Controller/@Module/... is instantiated by the DI container
    # (root its constructor) and driven by the framework through lifecycle and
    # single-method interface contracts (root those methods by name). A class
    # implementing an EXTERNAL `...OptionsFactory` interface has its factory
    # method invoked by Nest, so root all its methods (mirrors overrides_external).
    # Gated to JS/TS; a same-named ordinary method on a plain class is untouched.
    if not path.endswith(_JS_TS_EXTS):
        return False
    if not is_method:
        # A CLASS-node candidate (only present with --classes): the component
        # class is instantiated by the container, so it roots itself -- a rooted
        # constructor cannot revive it because DEFINES_METHOD is not a
        # reachability edge. (A non-class, non-method candidate -- a bare
        # function -- is not in either map, so this returns False.)
        return _is_nest_component_class(qn, class_decorators_norm, nest_factory_classes)
    cls = method_to_class.get(qn)
    if cls is None:
        return False
    if cls in nest_factory_classes:
        return True
    decorators = class_decorators_norm.get(cls)
    if decorators and decorators & cs.NEST_ROOT_CLASS_DECORATORS:
        return (
            member == cs.KEYWORD_CONSTRUCTOR or member in cs.NEST_FRAMEWORK_METHOD_NAMES
        )
    return False


def _is_react_base_qn(base_qn: str) -> bool:
    # A React component base: the simple name is `Component`/`PureComponent` AND
    # it lives in a react-namespaced module (`react.Component`, `React.Component`,
    # `react.PureComponent`). The namespace check keeps an unrelated base that
    # merely SHARES the `Component` simple name (Ember/Glimmer's
    # `@glimmer/component.Component`, a bespoke `ui.Component`) from being taken
    # for React.
    namespace, sep, leaf = base_qn.rpartition(cs.SEPARATOR_DOT)
    return (
        bool(sep)
        and leaf in cs.REACT_COMPONENT_BASE_NAMES
        and namespace.lower() == cs.REACT_NAMESPACE_TOKEN
    )


def _is_react_root(
    qn: str,
    member: str,
    is_method: bool,
    path: str,
    method_to_class: dict[str, str],
    react_component_classes: set[str],
) -> bool:
    # A React class-component lifecycle method (render/componentDidMount/... and
    # the constructor React calls on instantiation) is invoked by React, never by
    # a first-party call the graph sees, so it is a reachability root on a class
    # that INHERITS a React component base. The methods/callbacks it reaches via
    # `this.` then expand from it. Gated to JS/TS methods; a same-named method on
    # a plain (non-React) class is untouched.
    if not is_method or not path.endswith(_JS_TS_EXTS):
        return False
    if member not in cs.REACT_LIFECYCLE_METHOD_NAMES:
        return False
    cls = method_to_class.get(qn)
    return cls is not None and cls in react_component_classes


def _walk(
    frontier: set[str],
    adjacency: dict[str, set[str]],
    live: set[str],
    added: set[str] | None = None,
) -> None:
    stack = list(frontier)
    while stack:
        current = stack.pop()
        for nxt in adjacency.get(current, ()):
            if nxt not in live:
                live.add(nxt)
                if added is not None:
                    added.add(nxt)
                stack.append(nxt)


def _is_root(
    qn: str,
    props: PropertyDict,
    config: DeadCodeConfig,
    method_qns: set[str],
    protocol_stubs: set[str],
    method_to_class: dict[str, str],
    class_decorators_norm: dict[str, frozenset[str]],
    nest_factory_classes: set[str],
    react_component_classes: set[str],
    rust_test_modules: set[str],
    rust_test_spans: dict[str, list[tuple[int, int]]],
    project_prefix: str,
    endpoint_links: dict[str, int] | None = None,
) -> bool:
    """Whether any name-, path- or decorator-scoped rule makes `qn` a root.

    The rules are a tuple of thunks evaluated lazily in order by `any`: the
    same first-match semantics as the `elif` chain this replaced, without
    the branch per rule that put the chain over Sonar's complexity limit.
    """
    # The duplicate-qn marker (`init@51`, a SECOND Go init() in one file)
    # is a registration artifact, never part of the written name; strip it
    # so every name-scoped root rule sees the real leaf (kubernetes
    # pkg.apis.abac register.init@51 reported dead).
    leaf = qn_markers.strip_dup_marker(qn.rsplit(cs.SEPARATOR_DOT, 1)[-1])
    path = str(props.get(cs.KEY_PATH, ""))
    is_method = qn in method_qns
    bare_leaf = leaf.split(cs.CHAR_PAREN_OPEN, 1)[0]
    # With endpoint roots off, a handler that EXPOSES an endpoint is live only
    # if an indexed call site reaches the endpoint (issue #1603). That verdict
    # must come before the rules below: every framework registers a handler
    # by exporting it, so "exported symbols are roots" kept every public
    # handler alive and only `_private` ones were ever reported (issue
    # #2664). A second root decorator (a fixture, a CLI command), a named
    # entry point and test code still root it, on the same definition too.
    if (
        not config.endpoint_roots
        and endpoint_links is not None
        and qn in endpoint_links
    ):
        return (
            endpoint_links[qn] > 0
            or _has_non_route_root_decorator(props, config.root_decorators)
            or _is_named_entry_point(qn, config)
            or _is_rooted_test_symbol(
                props, qn, path, config, rust_test_modules, rust_test_spans
            )
        )
    rules: tuple[Callable[[], bool], ...] = (
        lambda: _has_root_decorator(props, config.root_decorators),
        lambda: props.get(cs.KEY_IS_EXPORTED) is True,
        # A method overriding an EXTERNAL stdlib base's method (click's
        # textwrap.TextWrapper subclass) is invoked by the base's machinery,
        # never by a first-party call, so it is a root.
        lambda: props.get(cs.KEY_OVERRIDES_EXTERNAL) is True,
        lambda: qn in protocol_stubs,
        lambda: is_method and _is_dunder(leaf) and path.endswith(cs.EXT_PY),
        # Python Enum protocol hooks (_generate_next_value_, _missing_) are
        # invoked by the enum machinery by NAME, like dunders: roots, not
        # dead code (django's TextChoices._generate_next_value_).
        lambda: (
            is_method
            and leaf in cs.PY_ENUM_HOOK_METHOD_NAMES
            and path.endswith(cs.EXT_PY)
        ),
        lambda: (
            not is_method
            and leaf in cs.GO_ROOT_FUNCTION_NAMES
            and path.endswith(cs.EXT_GO)
        ),
        lambda: _is_rust_runtime_root(leaf, is_method, path),
        # NOT leaf-based: the computed name contains a dot, so the qn's
        # last dotted segment is `toStringTag]`; match on the bracketed
        # member name as registered.
        lambda: _is_js_well_known_symbol_root(
            str(props.get(cs.KEY_NAME) or ""), is_method, path
        ),
        lambda: (
            _is_cpp_operator_root(leaf, path)
            or _is_c_cpp_entry_root(leaf, is_method, path, qn, project_prefix)
        ),
        lambda: _is_java_serialization_root(bare_leaf, is_method, path),
        lambda: _is_csharp_attribute_root(props, path),
        lambda: _is_csharp_dispose_root(bare_leaf, is_method, path),
        lambda: _is_csharp_operator_or_finalizer_root(leaf, path),
        lambda: _is_csharp_entry_point_root(bare_leaf, is_method, path, props),
        lambda: _is_nest_root(
            qn,
            bare_leaf,
            is_method,
            path,
            method_to_class,
            class_decorators_norm,
            nest_factory_classes,
        ),
        lambda: _is_react_root(
            qn, bare_leaf, is_method, path, method_to_class, react_component_classes
        ),
        lambda: (
            is_well_known_symbol_member(qn)
            and str(props.get(cs.KEY_PATH, "")).endswith(cs.JS_TS_ALL_EXTENSIONS)
        ),
        lambda: _is_named_entry_point(qn, config),
        lambda: _is_rooted_test_symbol(
            props, qn, path, config, rust_test_modules, rust_test_spans
        ),
    )
    return any(rule() for rule in rules)


def _is_named_entry_point(qn: str, config: DeadCodeConfig) -> bool:
    return any(qn.endswith(entry) for entry in config.entry_points)


def _is_rooted_test_symbol(
    props: PropertyDict,
    qn: str,
    path: str,
    config: DeadCodeConfig,
    rust_test_modules: set[str],
    rust_test_spans: dict[str, list[tuple[int, int]]],
) -> bool:
    return config.include_tests and _is_test_symbol(
        props, qn, path, config.test_patterns, rust_test_modules, rust_test_spans
    )


@dataclass
class _CandidateScan:
    candidates: set[str] = field(default_factory=set)
    props_by_qn: dict[str, PropertyDict] = field(default_factory=dict)
    method_qns: set[str] = field(default_factory=set)
    module_path: dict[str, str] = field(default_factory=dict)
    # Normalized decorators per CLASS qn (collected for every class, not just
    # class candidates), so a method root rule can consult its class's
    # @Injectable/@Controller/@Module marker (NestJS DI roots, issue #973).
    class_decorators_norm: dict[str, frozenset[str]] = field(default_factory=dict)


@dataclass
class _StructuralRels:
    # A method of a typing.Protocol subclass is an interface stub whose callers
    # resolve to the implementations; DEFINES edges from functions/methods feed
    # the live-owner registration round.
    defines_pairs: list[tuple[str, str]] = field(default_factory=list)
    protocol_classes: set[str] = field(default_factory=set)
    class_methods: list[tuple[str, str]] = field(default_factory=list)
    nested_class_pairs: list[tuple[str, str]] = field(default_factory=list)
    # A class implementing an EXTERNAL NestJS `...OptionsFactory` interface (one
    # not defined in this project) has its factory method invoked by Nest, so its
    # methods are roots (issue #973). Restricted to that naming convention so an
    # unrelated third-party interface implementer is not force-rooted.
    nest_factory_classes: set[str] = field(default_factory=set)
    # A class that `extends` a React component base (directly or through a
    # first-party intermediate base) is a class component whose lifecycle methods
    # React drives at runtime (issue #978). Seeds are direct extenders; the
    # transitive closure over INHERITS is computed after the scan.
    react_component_classes: set[str] = field(default_factory=set)
    inherits_subclasses: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )


def _record_module_or_class(
    scan: _CandidateScan, label: str, qn: str, props: PropertyDict
) -> None:
    if label == _MODULE:
        scan.module_path[qn] = str(props.get(cs.KEY_PATH, ""))
    elif label == _CLASS and isinstance(
        decorators := props.get(cs.KEY_DECORATORS), list
    ):
        scan.class_decorators_norm[qn] = frozenset(
            _norm_decorator(str(d)) for d in decorators
        )


def _scan_candidates(
    nodes: dict[_NodeId, PropertyDict],
    labels: set[str],
    project_prefix: str,
    config: DeadCodeConfig,
    rust_test_modules: set[str],
    rust_test_spans: dict[str, list[tuple[int, int]]],
) -> _CandidateScan:
    scan = _CandidateScan()
    for (label, uid), props in nodes.items():
        qn = str(uid)
        _record_module_or_class(scan, label, qn, props)
        if label not in labels or not qn.startswith(project_prefix):
            continue
        # With tests excluded, a test symbol's only callers are excluded as
        # roots, so reporting it is noise (test helpers and mocks are
        # infrastructure, not dead production code). Rust test code lives
        # INSIDE source files, so it is matched by attribute/module, not path
        # (issue #1008).
        if not config.include_tests and _is_test_symbol(
            props,
            qn,
            str(props.get(cs.KEY_PATH) or ""),
            config.test_patterns,
            rust_test_modules,
            rust_test_spans,
        ):
            continue
        scan.candidates.add(qn)
        scan.props_by_qn[qn] = props
        if label == _METHOD:
            scan.method_qns.add(qn)
    return scan


def _is_external_nest_options_factory(to_qn: str, project_prefix: str) -> bool:
    return not to_qn.startswith(project_prefix) and to_qn.rsplit(cs.SEPARATOR_DOT, 1)[
        -1
    ].endswith(cs.NEST_OPTIONS_FACTORY_SUFFIX)


def _scan_structural_rels(
    rels: list[_RelTuple], project_prefix: str
) -> _StructuralRels:
    found = _StructuralRels()
    for from_label, from_val, rel_type, to_label, to_val in rels:
        from_qn, to_qn = str(from_val), str(to_val)
        if rel_type == _DEFINES and from_label in (_FUNCTION, _METHOD):
            found.defines_pairs.append((from_qn, to_qn))
            if to_label == _CLASS:
                found.nested_class_pairs.append((from_qn, to_qn))
        elif rel_type == _INHERITS:
            found.inherits_subclasses[to_qn].add(from_qn)
            if to_qn in cs.PROTOCOL_BASE_QNS:
                found.protocol_classes.add(from_qn)
            elif _is_react_base_qn(to_qn):
                found.react_component_classes.add(from_qn)
        elif rel_type == _DEFINES_METHOD:
            found.class_methods.append((from_qn, to_qn))
        elif rel_type == _IMPLEMENTS and _is_external_nest_options_factory(
            to_qn, project_prefix
        ):
            found.nest_factory_classes.add(from_qn)
    return found


def _module_roots(
    rels: list[_RelTuple],
    module_rels: set[str],
    scan: _CandidateScan,
    config: DeadCodeConfig,
) -> set[str]:
    roots: set[str] = set()
    for from_label, from_val, rel_type, _to_label, to_val in rels:
        if from_label != _MODULE or rel_type not in module_rels:
            continue
        target_qn = str(to_val)
        if target_qn not in scan.candidates:
            continue
        path = scan.module_path.get(str(from_val), "")
        if config.include_tests or not matches_test_path(path, config.test_patterns):
            roots.add(target_qn)
    return roots


def _traversal_maps(
    rels: list[_RelTuple], traversal: set[str]
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    # OVERRIDES is recorded overrider -> overridden; keep the REVERSE mapping
    # (overridden -> overriders) to expand virtual-dispatch targets.
    override_rev: dict[str, set[str]] = defaultdict(set)
    for _from_label, from_val, rel_type, _to_label, to_val in rels:
        if rel_type in traversal:
            adjacency[str(from_val)].add(str(to_val))
        elif rel_type == _OVERRIDES:
            override_rev[str(to_val)].add(str(from_val))
    return adjacency, override_rev


def _revive_factory_classes(
    frontier: set[str],
    classes_by_owner: dict[str, set[str]],
    methods_by_class: dict[str, set[str]],
    live: set[str],
    adjacency: dict[str, set[str]],
) -> set[str]:
    added: set[str] = set()
    factory_method_roots: set[str] = set()
    for owner in frontier:
        for cls in classes_by_owner.get(owner, ()):
            if cls not in live:
                live.add(cls)
                added.add(cls)
            factory_method_roots |= methods_by_class[cls] - live
    live |= factory_method_roots
    added |= factory_method_roots
    _walk(factory_method_roots, adjacency, live, added=added)
    return added


def _revive_overriders(
    seeds: set[str],
    override_rev: dict[str, set[str]],
    live: set[str],
    adjacency: dict[str, set[str]],
) -> set[str]:
    override_roots: set[str] = set()
    stack = list(seeds)
    while stack:
        for overrider in override_rev.get(stack.pop(), ()):
            if overrider not in live and overrider not in override_roots:
                override_roots.add(overrider)
                stack.append(overrider)
    live |= override_roots
    added = set(override_roots)
    _walk(override_roots, adjacency, live, added=added)
    return added


def _expand_factories_and_overrides(
    live: set[str],
    adjacency: dict[str, set[str]],
    override_rev: dict[str, set[str]],
    structural: _StructuralRels,
) -> None:
    # Factory-class and override expansions, iterated together to a fixed
    # point because they feed each other (a factory revived only via an
    # override's callee still needs its class rooted, and vice versa).
    #
    # Factory-class rule: a class defined inside a LIVE function
    # (django's create_reverse_many_to_one_manager) escapes via its return
    # value or arguments, so no call edge lands on its methods. Treat them as
    # dispatch surface and revive their callee closure; a DEAD factory's
    # class stays dead.
    #
    # Override rule: a call to a base or interface method dispatches at
    # runtime to any override, so every transitive override of a LIVE method
    # is a reachable dispatch target, as is its callee closure. `override_rev`
    # walks all multi-level overriders (Base<-Sub<-SubSub); an override of a
    # DEAD base stays dead.
    #
    # Each round scans only nodes revived since the last (the pair maps and
    # override_rev are static, so a rescanned node yields nothing new),
    # keeping the loop O(live) total; a round that adds nothing ends it.
    classes_by_owner: dict[str, set[str]] = defaultdict(set)
    for owner, cls in structural.nested_class_pairs:
        classes_by_owner[owner].add(cls)
    methods_by_class: dict[str, set[str]] = defaultdict(set)
    for cls, m in structural.class_methods:
        methods_by_class[cls].add(m)

    frontier = set(live)
    while frontier:
        added = _revive_factory_classes(
            frontier, classes_by_owner, methods_by_class, live, adjacency
        )
        added |= _revive_overriders(frontier | added, override_rev, live, adjacency)
        frontier = added


def _drop_excluded_paths(
    dead: set[str], props_by_qn: dict[str, PropertyDict], patterns: tuple[str, ...]
) -> set[str]:
    return {
        qn
        for qn in dead
        if not any(
            fnmatch(str(props_by_qn[qn].get(cs.KEY_PATH) or ""), pattern)
            for pattern in patterns
        )
    }


def dead_code_from_graph(
    nodes: dict[_NodeId, PropertyDict],
    rels: list[_RelTuple],
    project_prefix: str,
    config: DeadCodeConfig,
    endpoint_links: dict[str, int] | None = None,
) -> set[str]:
    """`endpoint_links` maps each exposing handler to the number of call
    sites reaching its endpoint; consulted only with `endpoint_roots` off."""
    labels = {_FUNCTION, _METHOD}
    traversal = {_CALLS, _REFERENCES}
    module_rels = {_CALLS, _REFERENCES}
    if config.include_classes:
        labels.add(_CLASS)
        traversal |= {_INSTANTIATES, _INHERITS}
        module_rels.add(_INSTANTIATES)

    rust_test_modules = _rust_test_modules_from_nodes(nodes)
    # Both polarities need the spans: excluded test fns take their nested
    # symbols with them, and INCLUDED ones must root those same symbols (a
    # fn passed as a value, `filter(is_even)`, has no CALLS edge to revive
    # it).
    rust_test_spans = _rust_test_fn_spans(nodes)
    scan = _scan_candidates(
        nodes, labels, project_prefix, config, rust_test_modules, rust_test_spans
    )
    structural = _scan_structural_rels(rels, project_prefix)
    roots = _module_roots(rels, module_rels, scan, config)

    protocol_stubs = {
        m for c, m in structural.class_methods if c in structural.protocol_classes
    }
    method_to_class = {m: c for c, m in structural.class_methods}
    # Expand React components down the inheritance tree: a class extending a
    # first-party base that (transitively) extends react.Component is itself a
    # React component (a shared `BaseComponent extends React.Component` is common).
    _walk(
        set(structural.react_component_classes),
        structural.inherits_subclasses,
        structural.react_component_classes,
    )

    # Every rule in _is_root makes `qn` a root; they are alternatives, not a
    # priority order, so one membership test replaces a chain of identical
    # branches (Sonar S1871, #1669).
    roots |= {
        qn
        for qn in scan.candidates - roots
        if _is_root(
            qn,
            scan.props_by_qn[qn],
            config,
            scan.method_qns,
            protocol_stubs,
            method_to_class,
            scan.class_decorators_norm,
            structural.nest_factory_classes,
            structural.react_component_classes,
            rust_test_modules,
            rust_test_spans,
            project_prefix,
            endpoint_links,
        )
    }

    adjacency, override_rev = _traversal_maps(rels, traversal)
    live = set(roots)
    _walk(roots, adjacency, live)

    # Second expansion: a decorated function DEFINED by a LIVE owner is
    # framework-registered when the owner runs, so it and its callees are
    # live; the closure of a DEAD owner never registers and stays in the
    # reported cluster. ponytail: one round, so a registration chain nested
    # two closures deep is missed; iterate to fixed point if real code ever
    # registers closures from inside registered closures.
    closure_roots = {
        c
        for o, c in structural.defines_pairs
        if o in live
        and c not in live
        and c in scan.props_by_qn
        and scan.props_by_qn[c].get(cs.KEY_DECORATORS)
    }
    live |= closure_roots
    _walk(closure_roots, adjacency, live)

    _expand_factories_and_overrides(live, adjacency, override_rev, structural)

    dead = scan.candidates - live
    # Suppress generated files (openapi-ts client/core, routeTree.gen.ts) from
    # the REPORT only, after reachability: they stay full participants as roots
    # and callers, so a real function invoked only from generated glue is not
    # newly flagged; excluding earlier would drop those live edges.
    if config.exclude_patterns:
        dead = _drop_excluded_paths(dead, scan.props_by_qn, config.exclude_patterns)
    return dead


def _as_str_list(value: ResultValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _node_props(row: ResultRow) -> PropertyDict:
    # Coalesce NULL column values at the fetch boundary so the engine never
    # sees None where a str/list/bool is expected. Only properties the engine
    # reads are kept; the report is built from the raw rows.
    return {
        cs.KEY_PATH: str(row.get(cs.KEY_PATH) or ""),
        # The registered member NAME survives here because a computed
        # well-known-symbol name (`[Symbol.toStringTag]`) contains a dot and
        # cannot be recovered from the qn's last dotted segment.
        cs.KEY_NAME: str(row.get(cs.KEY_NAME) or ""),
        cs.KEY_DECORATORS: _as_str_list(row.get(cs.KEY_DECORATORS)),
        cs.KEY_IS_EXPORTED: row.get(cs.KEY_IS_EXPORTED) is True,
        cs.KEY_OVERRIDES_EXTERNAL: row.get(cs.KEY_OVERRIDES_EXTERNAL) is True,
        # Spans feed the Rust nested-test-symbol exclusion (issue #1008);
        # non-int values coalesce to 0 so the containment checks skip them.
        cs.KEY_START_LINE: _as_line(row.get(cs.KEY_START_LINE)),
        cs.KEY_END_LINE: _as_line(row.get(cs.KEY_END_LINE)),
        cs.KEY_RUST_CFG_TEST_MODS: _as_str_list(row.get(cs.KEY_RUST_CFG_TEST_MODS)),
        cs.KEY_RUST_UNGATED_MODS: _as_str_list(row.get(cs.KEY_RUST_UNGATED_MODS)),
        # The signature the C# entry-point rule checks (issue #2471).
        cs.KEY_MODIFIERS: _as_str_list(row.get(cs.KEY_MODIFIERS)),
        cs.KEY_RETURN_TYPE: str(row.get(cs.KEY_RETURN_TYPE) or ""),
        cs.KEY_PARAM_TYPES: _as_str_list(row.get(cs.KEY_PARAM_TYPES)),
    }


def _as_line(value: object) -> int:
    return value if isinstance(value, int) else 0


def _row_qn(row: ResultRow) -> str:
    return str(row.get(cs.KEY_QUALIFIED_NAME) or "")


def count_structural_tier_symbols(node_rows: list[ResultRow]) -> int:
    """Count symbols the reachability walk could never have reported.

    The ast-grep tier emits no CALLS edges and marks its symbols exported, so
    they are unconditional roots. Reporting this count keeps a "no dead code"
    result honest about the languages that were never analyzed.
    """
    from .parsers.ast_grep_tier import structural_tier_extensions

    extensions = structural_tier_extensions()
    if not extensions:
        return 0
    symbol_labels = {_FUNCTION, _METHOD, _CLASS}
    return sum(
        1
        for row in node_rows
        if str(row.get(cs.KEY_LABEL) or "") in symbol_labels
        and str(row.get(cs.KEY_PATH) or "").endswith(tuple(extensions))
    )


def _endpoint_links(
    ingestor: GraphQueryClient, params: dict[str, PropertyValue]
) -> dict[str, int]:
    """Indexed call sites per handler exposing an endpoint, read only when
    the endpoint-roots switch is off (issue #1603)."""
    links: dict[str, int] = {}
    for row in ingestor.fetch_all(cq.CYPHER_DEAD_CODE_ENDPOINT_LINKS, params):
        handler = row.get(cs.KEY_HANDLER)
        callers = row.get(cs.KEY_CALLERS)
        if isinstance(handler, str) and handler:
            links[handler] = links.get(handler, 0) + (
                callers if isinstance(callers, int) else 0
            )
    return links


def collect_dead_code(
    ingestor: GraphQueryClient, project_name: str, config: DeadCodeConfig
) -> list[ResultRow]:
    return collect_dead_code_with_coverage(ingestor, project_name, config)[0]


def collect_dead_code_with_coverage(
    ingestor: GraphQueryClient, project_name: str, config: DeadCodeConfig
) -> tuple[list[ResultRow], int]:
    """Dead-code rows plus the count of symbols no analysis could cover.

    The count rides along here because it needs the unfiltered node rows, which
    only this fetch has; recomputing it in the CLI would mean a second query.
    """
    prefix = project_name + cs.SEPARATOR_DOT
    params: dict[str, PropertyValue] = {cs.KEY_PROJECT_PREFIX: prefix}

    node_rows = ingestor.fetch_all(cq.CYPHER_DEAD_CODE_NODES, params)
    nodes: dict[_NodeId, PropertyDict] = {
        (str(row.get(cs.KEY_LABEL) or ""), _row_qn(row)): _node_props(row)
        for row in node_rows
    }

    rels: list[_RelTuple] = [
        (
            str(row.get(cs.KEY_FROM_LABEL) or ""),
            str(row.get(cs.KEY_FROM_QN) or ""),
            str(row.get(cs.KEY_REL_TYPE) or ""),
            str(row.get(cs.KEY_TO_LABEL) or ""),
            str(row.get(cs.KEY_TO_QN) or ""),
        )
        for row in ingestor.fetch_all(cq.CYPHER_DEAD_CODE_RELS, params)
        if _passes_floor(row, config.min_resolution)
    ]

    endpoint_links = (
        None if config.endpoint_roots else _endpoint_links(ingestor, params)
    )
    dead = dead_code_from_graph(nodes, rels, prefix, config, endpoint_links)
    rows = [row for row in node_rows if _row_qn(row) in dead]
    rows.sort(key=_row_qn)
    return rows, count_structural_tier_symbols(node_rows)
