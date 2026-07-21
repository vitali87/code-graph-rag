# Dead-code reachability roots: entry points and framework hooks.

# A C++ operator overload / user-defined literal is defined with the reserved
# `operator` keyword heading the name (`operator==`, `operator[]`, `operator""_json`).
# It is invoked by operator/literal syntax, not a named call, so it is a dead-code
# reachability root; the keyword can only head such definitions, so this prefix on a
# C++ file uniquely identifies them (member or free function).
CPP_OPERATOR_PREFIX = "operator"

# Decorators whose presence marks a function/method as an implicit entry point
# (web routes, task/flow handlers, fixtures, CLI commands, event listeners, and
# Pydantic validators/serializers the framework invokes by registration).
DEFAULT_ROOT_DECORATORS: frozenset[str] = frozenset(
    {
        "route",
        "get",
        "post",
        "callback",
        "put",
        "delete",
        "patch",
        "websocket",
        "task",
        "flow",
        "fixture",
        "command",
        "cli",
        "app",
        "on_event",
        "listener",
        "validator",
        "field_validator",
        "model_validator",
        "root_validator",
        "field_serializer",
        "model_serializer",
        "computed_field",
        "abstractmethod",
        # Property-family accessors are invoked by ATTRIBUTE syntax -- a bare
        # read/write like `obj._output_field_or_none` produces no call node,
        # so no CALLS edge can ever land on them (django WhereNode.
        # _output_field_or_none, Expression._constructor_signature). The same
        # invisible-invocation argument as dunders: roots, not dead code.
        "property",
        "cached_property",
        "classproperty",
        "hybrid_property",
        "setter",
        "deleter",
    }
)

# Go functions the runtime invokes with no explicit call site: `func init()`
# runs at package load (any number per package), `func main()` is the program
# entry. Both are reachability roots (like Python dunders), gated by the .go
# extension so same-named symbols in other languages are unaffected.
GO_ROOT_FUNCTION_NAMES: frozenset[str] = frozenset({"init", "main"})

# Rust `fn main()` is the binary entry point, invoked by the runtime with no
# call site -- a reachability root (gated by .rs).
RUST_ROOT_FUNCTION_NAMES: frozenset[str] = frozenset({"main"})

# Rust trait-impl methods the language/std dispatches implicitly (Display::fmt
# via format!, PartialEq::eq via ==, Iterator::next via for, operator traits,
# Drop::drop, serde, ...), never through an explicit call the graph can see.
# Rooting them by name (gated by .rs) mirrors the Python-dunder exemption: these
# names are conventionally reserved for trait impls, so a same-named user method
# that is genuinely dead is under-reported rather than mis-reported.
RUST_TRAIT_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "fmt",
        "eq",
        "ne",
        "cmp",
        "partial_cmp",
        "hash",
        "next",
        "next_back",
        "into_iter",
        "size_hint",
        "drop",
        "clone",
        "clone_from",
        "default",
        "from",
        "from_str",
        "try_from",
        "into",
        "try_into",
        "deref",
        "deref_mut",
        "as_ref",
        "as_mut",
        "borrow",
        "borrow_mut",
        "poll",
        "serialize",
        "deserialize",
        "source",
        "add",
        "add_assign",
        "sub",
        "sub_assign",
        "mul",
        "mul_assign",
        "div",
        "div_assign",
        "rem",
        "rem_assign",
        "neg",
        "not",
        "bitand",
        "bitand_assign",
        "bitor",
        "bitor_assign",
        "bitxor",
        "bitxor_assign",
        "shl",
        "shl_assign",
        "shr",
        "shr_assign",
        "index",
        "index_mut",
    }
)

# Java serialization hooks the java.io runtime invokes reflectively during
# (de)serialization -- never through a call the graph can see -- so they are
# reachability roots (like Python dunders / Rust trait methods), gated by the .java
# extension. These names are reserved by the Serializable contract, so rooting them
# by name under-reports a same-named genuinely-dead method rather than mis-reporting.
JAVA_SERIALIZATION_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "readObject",
        "writeObject",
        "readObjectNoData",
        "readResolve",
        "writeReplace",
    }
)

# C# attributes that mark a method as invoked by a framework/runtime rather
# than a first-party call the graph can see -- so an attributed method is a
# reachability root, gated by the .cs extension. Test runners invoke [Fact]/
# [Theory]/[Test]/... ; ASP.NET routes to [HttpGet]/[Route]/... ; the
# serialization runtime invokes [OnDeserialized]/... callbacks reflectively.
# Names are the lowercased, argument-stripped form _norm_decorator produces.
CSHARP_ROOT_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "fact",
        "theory",
        "test",
        "testmethod",
        "testcase",
        "setup",
        "teardown",
        "onetimesetup",
        "onetimeteardown",
        "globalsetup",
        "apicontroller",
        "route",
        "httpget",
        "httppost",
        "httpput",
        "httpdelete",
        "httppatch",
        "httphead",
        "httpoptions",
        "ondeserialized",
        "ondeserializing",
        "onserialized",
        "onserializing",
    }
)

# IDisposable methods the runtime invokes at the close of a `using` block (or
# `await using`), never through a named call -- reachability roots, gated by
# the .cs extension and method-ness (name-scoped, like the Java hooks above).
CSHARP_DISPOSE_METHOD_NAMES: frozenset[str] = frozenset({"Dispose", "DisposeAsync"})

# Base classes that mark a class as a structural interface: its method stubs
# are never call targets themselves (callers resolve to the implementations),
# so dead-code analysis roots every method the class defines.
# ponytail: direct bases only; transitive Protocol subclassing is not chased.
PROTOCOL_BASE_QNS: tuple[str, ...] = ("typing.Protocol", "typing_extensions.Protocol")

# Substrings in a node's file path that mark it as test code. Covers Python
# (test_, _test, conftest, /tests/), the JS/TS filename convention
# (foo.test.ts, foo.spec.tsx), the Jest __tests__/ directory, and the
# Node.js/mocha singular /test/ dir (express: 34 of 49 dead-code reports
# were test/ helpers). Matching is segment-anchored via the leading-slash
# normalization, so contest/ and latest/ do not match. Singular /spec/
# stays excluded: it collides with product code (a domain "spec" module),
# which would misclassify live code as test.
TEST_PATH_PATTERNS: tuple[str, ...] = (
    "test_",
    "_test",
    "conftest",
    "/tests/",
    "/test/",
    ".test.",
    ".spec.",
    "__tests__",
)

# Python Enum protocol hooks: the enum machinery invokes these sunder
# METHODS by NAME (_generate_next_value_ on auto(), _missing_ on a failed
# value lookup), never through a call the graph can see -- runtime roots
# exactly like dunders. A closed set: arbitrary sunder names are not part
# of the protocol, and _ignore_/_order_ are class ATTRIBUTES consumed at
# class creation, not methods, so they are deliberately absent.
PY_ENUM_HOOK_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "_generate_next_value_",
        "_missing_",
    }
)
