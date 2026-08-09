# Dead-code reachability engine. Roots (entry points, framework hooks,
# module-load callees, test code) expand over CALLS/REFERENCES edges;
# whatever is never reached is reported. Reachability runs client-side in
# Python: the per-root *BFS Cypher formulation is O(roots x graph) and hit
# memgraph's 600s timeout on big projects (django: 31k roots, 101k CALLS
# edges), whereas a multi-source walk over the fetched edges is linear and
# finishes in milliseconds.
import re
from collections import defaultdict
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


def _norm_decorator(decorator: str) -> str:
    # Drop '@' and any surrounding attribute brackets, take the text before
    # '(', then the last dotted segment, lowercased -> `@app.route(...)` and a
    # C# `[Route("x")]` both become `route`. Bracket-stripping keeps the
    # normalization robust to whatever a highlight query captures.
    cleaned = decorator.replace(cs.DECORATOR_AT, "").strip("[] ")
    head = cleaned.split(cs.CHAR_PAREN_OPEN)[0]
    return head.split(cs.SEPARATOR_DOT)[-1].strip("[]").lower()


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
        declared_here = props.get(cs.KEY_RUST_CFG_TEST_MODS)
        if isinstance(declared_here, list):
            declared.extend(str(target) for target in declared_here)
        ungated_here = props.get(cs.KEY_RUST_UNGATED_MODS)
        if isinstance(ungated_here, list):
            ungated.update(str(target) for target in ungated_here)
        path = str(props.get(cs.KEY_PATH, ""))
        if not (
            path.endswith(cs.EXT_RS) or path.startswith(cs.INLINE_MODULE_PATH_PREFIX)
        ):
            continue
        qn = str(uid)
        rust_modules.add(qn)
        if qn.rsplit(cs.SEPARATOR_DOT, 1)[
            -1
        ] in cs.RUST_TEST_MODULE_SEGMENTS or _has_rust_cfg_test_gate(props):
            modules.add(qn)
    # An ungated declaration from ANY target (src/main.rs compiling the
    # module for production) outweighs a gated sibling declaration.
    modules.update(
        target
        for target in declared
        if target in rust_modules and target not in ungated
    )
    return modules


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


_WELL_KNOWN_SYMBOL_KEY_RE = re.compile(r"\[Symbol\.(?P<name>[A-Za-z_$][\w$]*)\]$")


def is_well_known_symbol_member(name: str) -> bool:
    m = _WELL_KNOWN_SYMBOL_KEY_RE.search(name)
    return m is not None and m.group("name") in cs.JS_WELL_KNOWN_SYMBOLS


def _has_root_decorator(props: PropertyDict, root_decorators: frozenset[str]) -> bool:
    decorators = props.get(cs.KEY_DECORATORS)
    if not isinstance(decorators, list):
        return False
    return any(_norm_decorator(str(d)) in root_decorators for d in decorators)


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


def dead_code_from_graph(
    nodes: dict[_NodeId, PropertyDict],
    rels: list[_RelTuple],
    project_prefix: str,
    config: DeadCodeConfig,
) -> set[str]:
    labels = {_FUNCTION, _METHOD}
    traversal = {_CALLS, _REFERENCES}
    module_rels = {_CALLS, _REFERENCES}
    if config.include_classes:
        labels.add(_CLASS)
        traversal |= {_INSTANTIATES, _INHERITS}
        module_rels.add(_INSTANTIATES)

    candidates: set[str] = set()
    props_by_qn: dict[str, PropertyDict] = {}
    method_qns: set[str] = set()
    module_path: dict[str, str] = {}
    rust_test_modules = _rust_test_modules_from_nodes(nodes)
    # Both polarities need the spans: excluded test fns take their nested
    # symbols with them, and INCLUDED ones must root those same symbols (a
    # fn passed as a value, `filter(is_even)`, has no CALLS edge to revive
    # it).
    rust_test_spans = _rust_test_fn_spans(nodes)
    # Normalized decorators per CLASS qn (collected for every class, not just
    # class candidates), so a method root rule can consult its class's
    # @Injectable/@Controller/@Module marker (NestJS DI roots, issue #973).
    class_decorators_norm: dict[str, frozenset[str]] = {}
    for (label, uid), props in nodes.items():
        if label == _MODULE:
            module_path[str(uid)] = str(props.get(cs.KEY_PATH, ""))
        if label == _CLASS:
            decorators = props.get(cs.KEY_DECORATORS)
            if isinstance(decorators, list):
                class_decorators_norm[str(uid)] = frozenset(
                    _norm_decorator(str(d)) for d in decorators
                )
        if label in labels and str(uid).startswith(project_prefix):
            # With tests excluded, a test symbol's only callers are excluded
            # as roots, so reporting it is noise (test helpers and mocks are
            # infrastructure, not dead production code). Rust test code lives
            # INSIDE source files, so it is matched by attribute/module, not
            # path (issue #1008).
            if not config.include_tests and _is_test_symbol(
                props,
                str(uid),
                str(props.get(cs.KEY_PATH) or ""),
                config.test_patterns,
                rust_test_modules,
                rust_test_spans,
            ):
                continue
            candidates.add(str(uid))
            props_by_qn[str(uid)] = props
            if label == _METHOD:
                method_qns.add(str(uid))

    roots: set[str] = set()
    # A method of a typing.Protocol subclass is an interface stub whose callers
    # resolve to the implementations; DEFINES edges from functions/methods feed
    # the live-owner registration round below.
    defines_pairs: list[tuple[str, str]] = []
    protocol_classes: set[str] = set()
    class_methods: list[tuple[str, str]] = []
    nested_class_pairs: list[tuple[str, str]] = []
    # A class implementing an EXTERNAL NestJS `...OptionsFactory` interface (one
    # not defined in this project) has its factory method invoked by Nest, so its
    # methods are roots (issue #973). Restricted to that naming convention so an
    # unrelated third-party interface implementer is not force-rooted.
    nest_factory_classes: set[str] = set()
    # A class that `extends` a React component base (directly or through a
    # first-party intermediate base) is a class component whose lifecycle methods
    # React drives at runtime (issue #978). Seeds are direct extenders; the
    # transitive closure over INHERITS is computed after the scan.
    react_component_classes: set[str] = set()
    inherits_subclasses: dict[str, set[str]] = defaultdict(set)
    for from_label, from_val, rel_type, to_label, to_val in rels:
        if rel_type == _DEFINES and from_label in (_FUNCTION, _METHOD):
            defines_pairs.append((str(from_val), str(to_val)))
            if to_label == _CLASS:
                nested_class_pairs.append((str(from_val), str(to_val)))
        elif rel_type == _INHERITS:
            inherits_subclasses[str(to_val)].add(str(from_val))
            if str(to_val) in cs.PROTOCOL_BASE_QNS:
                protocol_classes.add(str(from_val))
            elif _is_react_base_qn(str(to_val)):
                react_component_classes.add(str(from_val))
        elif rel_type == _DEFINES_METHOD:
            class_methods.append((str(from_val), str(to_val)))
        elif (
            rel_type == _IMPLEMENTS
            and not str(to_val).startswith(project_prefix)
            and str(to_val)
            .rsplit(cs.SEPARATOR_DOT, 1)[-1]
            .endswith(cs.NEST_OPTIONS_FACTORY_SUFFIX)
        ):
            nest_factory_classes.add(str(from_val))
        if from_label != _MODULE or rel_type not in module_rels:
            continue
        target_qn = str(to_val)
        if target_qn not in candidates:
            continue
        path = module_path.get(str(from_val), "")
        is_test = matches_test_path(path, config.test_patterns)
        if config.include_tests or not is_test:
            roots.add(target_qn)
    protocol_stubs = {m for c, m in class_methods if c in protocol_classes}
    method_to_class = {m: c for c, m in class_methods}
    # Expand React components down the inheritance tree: a class extending a
    # first-party base that (transitively) extends react.Component is itself a
    # React component (a shared `BaseComponent extends React.Component` is common).
    _walk(set(react_component_classes), inherits_subclasses, react_component_classes)

    for qn in candidates:
        if qn in roots:
            continue
        props = props_by_qn[qn]
        # The duplicate-qn marker (`init@51`, a SECOND Go init() in one file)
        # is a registration artifact, never part of the written name; strip it
        # so every name-scoped root rule sees the real leaf (kubernetes
        # pkg.apis.abac register.init@51 reported dead).
        leaf = qn.rsplit(cs.SEPARATOR_DOT, 1)[-1].split(cs.DUP_QN_MARKER, 1)[0]
        path = str(props.get(cs.KEY_PATH, ""))
        if _has_root_decorator(props, config.root_decorators):
            roots.add(qn)
        elif props.get(cs.KEY_IS_EXPORTED) is True:
            roots.add(qn)
        # A method overriding an EXTERNAL stdlib base's method (click's
        # textwrap.TextWrapper subclass) is invoked by the base's machinery,
        # never by a first-party call, so it is a root.
        elif props.get(cs.KEY_OVERRIDES_EXTERNAL) is True:
            roots.add(qn)
        elif qn in protocol_stubs:
            roots.add(qn)
        elif qn in method_qns and _is_dunder(leaf) and path.endswith(cs.EXT_PY):
            roots.add(qn)
        # Python Enum protocol hooks (_generate_next_value_, _missing_) are
        # invoked by the enum machinery by NAME, like dunders: roots, not
        # dead code (django's TextChoices._generate_next_value_).
        elif (
            qn in method_qns
            and leaf in cs.PY_ENUM_HOOK_METHOD_NAMES
            and path.endswith(cs.EXT_PY)
        ):
            roots.add(qn)
        elif (
            qn not in method_qns
            and leaf in cs.GO_ROOT_FUNCTION_NAMES
            and path.endswith(cs.EXT_GO)
        ):
            roots.add(qn)
        elif _is_rust_runtime_root(leaf, qn in method_qns, path):
            roots.add(qn)
        # NOT leaf-based: the computed name contains a dot, so the qn's
        # last dotted segment is `toStringTag]`; match on the bracketed
        # member name as registered.
        elif _is_js_well_known_symbol_root(
            str(props.get(cs.KEY_NAME) or ""), qn in method_qns, path
        ):
            roots.add(qn)
        elif _is_cpp_operator_root(leaf, path) or _is_c_cpp_entry_root(
            leaf, qn in method_qns, path, qn, project_prefix
        ):
            roots.add(qn)
        elif _is_java_serialization_root(
            leaf.split(cs.CHAR_PAREN_OPEN, 1)[0], qn in method_qns, path
        ):
            roots.add(qn)
        elif _is_csharp_attribute_root(props, path):
            roots.add(qn)
        elif _is_csharp_dispose_root(
            leaf.split(cs.CHAR_PAREN_OPEN, 1)[0], qn in method_qns, path
        ):
            roots.add(qn)
        elif _is_csharp_operator_or_finalizer_root(leaf, path):
            roots.add(qn)
        elif _is_nest_root(
            qn,
            leaf.split(cs.CHAR_PAREN_OPEN, 1)[0],
            qn in method_qns,
            path,
            method_to_class,
            class_decorators_norm,
            nest_factory_classes,
        ):
            roots.add(qn)
        elif _is_react_root(
            qn,
            leaf.split(cs.CHAR_PAREN_OPEN, 1)[0],
            qn in method_qns,
            path,
            method_to_class,
            react_component_classes,
        ):
            roots.add(qn)
        elif is_well_known_symbol_member(qn) and str(
            props.get(cs.KEY_PATH, "")
        ).endswith(cs.JS_TS_ALL_EXTENSIONS):
            roots.add(qn)
        elif any(qn.endswith(entry) for entry in config.entry_points):
            roots.add(qn)
        elif config.include_tests and _is_test_symbol(
            props,
            qn,
            path,
            config.test_patterns,
            rust_test_modules,
            rust_test_spans,
        ):
            roots.add(qn)

    adjacency: dict[str, set[str]] = defaultdict(set)
    # OVERRIDES is recorded overrider -> overridden; keep the REVERSE mapping
    # (overridden -> overriders) to expand virtual-dispatch targets below.
    override_rev: dict[str, set[str]] = defaultdict(set)
    for from_label, from_val, rel_type, _to_label, to_val in rels:
        if rel_type in traversal:
            adjacency[str(from_val)].add(str(to_val))
        elif rel_type == _OVERRIDES:
            override_rev[str(to_val)].add(str(from_val))

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
        for o, c in defines_pairs
        if o in live
        and c not in live
        and c in props_by_qn
        and props_by_qn[c].get(cs.KEY_DECORATORS)
    }
    live |= closure_roots
    _walk(closure_roots, adjacency, live)

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
    for owner, cls in nested_class_pairs:
        classes_by_owner[owner].add(cls)
    methods_by_class: dict[str, set[str]] = defaultdict(set)
    for cls, m in class_methods:
        methods_by_class[cls].add(m)

    frontier = set(live)
    while frontier:
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

        override_roots: set[str] = set()
        stack = list(frontier | added)
        while stack:
            for overrider in override_rev.get(stack.pop(), ()):
                if overrider not in live and overrider not in override_roots:
                    override_roots.add(overrider)
                    stack.append(overrider)
        live |= override_roots
        added |= override_roots
        _walk(override_roots, adjacency, live, added=added)

        frontier = added

    dead = candidates - live
    # Suppress generated files (openapi-ts client/core, routeTree.gen.ts) from
    # the REPORT only, after reachability: they stay full participants as roots
    # and callers, so a real function invoked only from generated glue is not
    # newly flagged; excluding earlier would drop those live edges.
    if config.exclude_patterns:
        dead = {
            qn
            for qn in dead
            if not any(
                fnmatch(str(props_by_qn[qn].get(cs.KEY_PATH) or ""), pattern)
                for pattern in config.exclude_patterns
            )
        }
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
    }


def _as_line(value: object) -> int:
    return value if isinstance(value, int) else 0


def _row_qn(row: ResultRow) -> str:
    return str(row.get(cs.KEY_QUALIFIED_NAME) or "")


def collect_dead_code(
    ingestor: GraphQueryClient, project_name: str, config: DeadCodeConfig
) -> list[ResultRow]:
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
    ]

    dead = dead_code_from_graph(nodes, rels, prefix, config)
    rows = [row for row in node_rows if _row_qn(row) in dead]
    rows.sort(key=_row_qn)
    return rows
