# Dead-code reachability roots: entry points and framework hooks.

# A C++ operator overload / user-defined literal is defined with the reserved
# `operator` keyword heading the name (`operator==`, `operator[]`, `operator""_json`).
# It is invoked by operator/literal syntax, not a named call, so it is a dead-code
# reachability root; the keyword can only head such definitions, so this prefix on a
# C++ file uniquely identifies them (member or free function).
CPP_OPERATOR_PREFIX = "operator"

# C/C++ program entries the OS runtime invokes with no visible call site:
# hosted `main`/Windows `wmain`, GUI `WinMain`/`wWinMain`, and a DLL's
# `DllMain`. Free functions on C/C++ files only.
C_CPP_ENTRY_FUNCTION_NAMES: frozenset[str] = frozenset(
    {"main", "wmain", "WinMain", "wWinMain", "DllMain"}
)

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
        # Registration idioms whose handler the framework or the interpreter
        # calls with no call site in the code (issue #2736): Celery's
        # `@shared_task`, Flask's error handler and request hooks, FastAPI /
        # Starlette's exception handler and middleware, Django's signal
        # `@receiver`, SQLAlchemy's `@event.listens_for`, and `register`
        # (`@atexit.register`, `@admin.register`). A singledispatch
        # implementation's `@generic.register` is not a root: it lives with
        # its generic (SINGLEDISPATCH_DECORATORS).
        "shared_task",
        "errorhandler",
        "before_request",
        "after_request",
        "teardown_request",
        "before_app_request",
        "after_app_request",
        "teardown_app_request",
        "exception_handler",
        "middleware",
        "receiver",
        "listens_for",
        "register",
    }
)

# A function decorated with one of these dispatches to the implementations
# registered with `@<it>.register`, so each implementation is reachable exactly
# when the generic is (issue #2736).
SINGLEDISPATCH_DECORATORS: frozenset[str] = frozenset(
    {"singledispatch", "singledispatchmethod"}
)
DISPATCH_REGISTER_DECORATOR = "register"

# Go functions the runtime invokes with no explicit call site: `func init()`
# runs at package load (any number per package), `func main()` is the program
# entry. Both are reachability roots (like Python dunders), gated by the .go
# extension so same-named symbols in other languages are unaffected.
GO_ROOT_FUNCTION_NAMES: frozenset[str] = frozenset({"init", "main"})

# Rust `fn main()` is the binary entry point, invoked by the runtime with no
# call site -- a reachability root (gated by .rs).
RUST_ROOT_FUNCTION_NAMES: frozenset[str] = frozenset({"main"})

# Rust test attributes: the harness invokes these functions with no call site.
# Bare names match exactly; scoped runner variants (#[tokio::test],
# #[async_std::test]) match by the ::test suffix. Gated by .rs (issue #1008).
RUST_TEST_ATTRIBUTE_NAMES: frozenset[str] = frozenset({"test", "bench"})
RUST_TEST_ATTRIBUTE_SUFFIX = "::test"

# The `#[cfg(test)] mod tests` convention: a real MODULE node named
# `tests` or `test` in a .rs file marks inline test code (issue #1008).
# This is deliberately WIDER than the test-path rules (TEST_NAME_WORDS),
# which see files and directories, never an inline mod declared inside
# src/lib.rs: the name is a proxy for the `#[cfg(test)]` gate the
# graph does not yet record. Measured across a 972-crate corpus, 4155
# such modules are cfg-gated test code and 41 are ungated, of which two
# ship as production API (aws-lc-rs `pub mod test`, alacritty's terminal
# test helpers), both test-support by nature; that residual silencing is
# accepted until issue #1010 replaces the name proxy with cfg awareness.
RUST_TEST_MODULE_SEGMENTS: frozenset[str] = frozenset({"tests", "test"})

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

# The C# program entry point the runtime invokes: a static method with this
# name, whatever its accessibility (`dotnet new console` declares it without
# `public`), returning one of these types and taking no parameter or a single
# string array. Types are compared as _csharp_type_name spells them: without
# whitespace and with every namespace or `global::` qualifier dropped.
CSHARP_ENTRY_METHOD_NAME = "Main"
CSHARP_ENTRY_TASK_RETURN_TYPES: frozenset[str] = frozenset(
    {"Task", "Task<int>", "Task<Int32>"}
)
CSHARP_ENTRY_RETURN_TYPES: frozenset[str] = (
    frozenset({"void", "int", "Int32"}) | CSHARP_ENTRY_TASK_RETURN_TYPES
)
CSHARP_ENTRY_ARGS_TYPES: frozenset[str] = frozenset({"string[]", "String[]"})
CSHARP_TYPE_QUALIFIER_PATTERN = r"(?:\w+(?:\.|::))+"
# A nullable-reference annotation (`string[]?`, `string?[]`, `Task?`) leaves
# the CLR type unchanged, so it is ignored on the args parameter and on a
# Task return. On a value type it is not an annotation: `int?` is
# Nullable<int>, which is not an entry-point return.
CSHARP_NULLABLE_MARKER = "?"

# Base classes that mark a class as a structural interface: its method stubs
# are never call targets themselves (callers resolve to the implementations),
# so dead-code analysis roots every method the class defines.
# ponytail: direct bases only; transitive Protocol subclassing is not chased.
PROTOCOL_BASE_QNS: tuple[str, ...] = ("typing.Protocol", "typing_extensions.Protocol")

# What marks a repo-relative path as test code (path_filters.matches_test_path).
# Judged on whole words of path segments, never raw substrings: `test_` as a
# substring made shortest_paths/ and latest_prices.py tests (issue #2618).
#
# A snake/kebab word of any directory or file stem, case-insensitive: tests/,
# Tests/ (SwiftPM), __tests__/ (Jest), test_x.py, x_test.go, conftest.py,
# snappy_unittest.cc (googletest), e2e-tests/.
TEST_NAME_WORDS: frozenset[str] = frozenset(
    {"test", "tests", "unittest", "unittests", "conftest"}
)
# CamelCase words, case-sensitive so Latest and Contest stay production:
# FooTests.cs, AppTests/ (Xcode), androidTest/ and integrationTest/ (Gradle
# source sets), testFixtures/. A leading `Test` is not a marker: FluentValidation
# ships its TestHelper namespace to users as production API.
TEST_NAME_CAMEL_SUFFIXES: tuple[str, ...] = ("Test", "Tests")
TEST_NAME_CAMEL_PREFIX = "test"
# A dotted qualifier after the first name part: foo.test.ts, foo.spec.tsx,
# and .NET test projects (Foo.Tests/, Foo.Specs/). `_spec` and a CamelCase
# Spec stay production (TensorFlow's type_spec.py, PodSpec.java).
TEST_NAME_QUALIFIERS: frozenset[str] = frozenset({"test", "tests", "spec", "specs"})
# Whole directory names, underscores stripped: numpy/testing/, pandas/_testing/.
TEST_DIR_NAMES: frozenset[str] = frozenset({"testing"})
# Only at the repo root (RSpec, Jasmine): a nested spec/ is as often a domain
# package (the JDK's java/security/spec/, swagger-ui's plugins/spec/).
TEST_ROOT_DIR_NAMES: frozenset[str] = frozenset({"spec", "specs"})
# Distinct paths matches_test_path remembers; a large monorepo's file count.
TEST_PATH_CACHE_SIZE = 65536

# NestJS component decorators that mark a CLASS as instantiated and driven by
# the DI container / framework, never by a first-party `new` the graph can see:
# `@Injectable` (providers/services), `@Controller`, `@Module`, `@Catch`
# (exception filters), `@Resolver` (GraphQL), `@WebSocketGateway`. Such a class's
# constructor is invoked by the container and its framework-contract methods by
# Nest, so both are reachability roots (gated by a JS/TS extension). Names are the
# lowercased, argument-stripped form _norm_decorator produces.
NEST_ROOT_CLASS_DECORATORS: frozenset[str] = frozenset(
    {
        "injectable",
        "controller",
        "module",
        "catch",
        "resolver",
        "websocketgateway",
    }
)

# NestJS async-options factory interfaces follow the `XxxOptionsFactory` naming
# convention (`TypeOrmOptionsFactory`, `MongooseOptionsFactory`,
# `GqlOptionsFactory`, ...). A class that `implements` one has its factory method
# invoked by Nest, so its methods are roots. Matched on the interface leaf name
# so only these framework contracts root (an unrelated third-party interface
# does not), gated to an EXTERNAL interface (outside the project prefix).
NEST_OPTIONS_FACTORY_SUFFIX = "OptionsFactory"

# NestJS methods invoked by the framework through a lifecycle or interface
# contract, never by a named call the graph can see: lifecycle hooks
# (`onModuleInit`, `onApplicationBootstrap`, ...) and the single-method
# interface contracts (`NestMiddleware.use`, `NestInterceptor.intercept`,
# `NestModule.configure`, `CanActivate.canActivate`, `ExceptionFilter.catch`,
# `PipeTransform.transform`). Rooted only on a method of a NestJS component
# class (a NEST_ROOT_CLASS_DECORATORS decorator), so a same-named ordinary
# method (`use`, `transform`) on a plain class is NOT force-rooted.
NEST_FRAMEWORK_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "onModuleInit",
        "onModuleDestroy",
        "onApplicationBootstrap",
        "onApplicationShutdown",
        "beforeApplicationShutdown",
        "configure",
        "use",
        "intercept",
        "canActivate",
        "catch",
        "transform",
    }
)

# React component base classes: a class that `extends` one of these is a class
# component whose lifecycle methods React drives at runtime. Matched on the base
# interface/class leaf name (`React.Component`, `PureComponent`, and the rarely
# spelled-out `React.PureComponent`), so the INHERITS target's last segment
# identifies it regardless of the import alias.
REACT_COMPONENT_BASE_NAMES: frozenset[str] = frozenset({"Component", "PureComponent"})

# The module namespace React's `Component`/`PureComponent` base lives in
# (`react.Component`, `React.Component`). The base's namespace must equal this
# EXACTLY (lowercased), so a look-alike (`preact.Component`, `notreact.Component`,
# Ember/Glimmer's `@glimmer/component.Component`) is not mistaken for React.
REACT_NAMESPACE_TOKEN = "react"

# React class-component lifecycle methods the runtime invokes (mount/update/
# unmount/render/error), plus the constructor React calls when it instantiates
# the component. Never called by a first-party call the graph can see, so they
# are reachability roots on a React component class (gated by INHERITS to a
# REACT_COMPONENT_BASE_NAMES base and a JS/TS extension); the methods and
# callbacks they reach via `this.` then expand from them.
REACT_LIFECYCLE_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "render",
        "constructor",
        "componentDidMount",
        "componentDidUpdate",
        "componentWillUnmount",
        "shouldComponentUpdate",
        "getSnapshotBeforeUpdate",
        "componentDidCatch",
        "getDerivedStateFromProps",
        "getDerivedStateFromError",
        "componentWillMount",
        "componentWillReceiveProps",
        "componentWillUpdate",
        "UNSAFE_componentWillMount",
        "UNSAFE_componentWillReceiveProps",
        "UNSAFE_componentWillUpdate",
    }
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

# (H) ES well-known symbols (incl. Explicit Resource Management: dispose/asyncDispose)
# (H) These are invoked implicitly by the runtime (e.g. [Symbol.iterator] by for..of)
# (H) and never called by name, so they are dead-code liveness roots.
JS_WELL_KNOWN_SYMBOLS: frozenset[str] = frozenset(
    {
        "asyncIterator",
        "hasInstance",
        "isConcatSpreadable",
        "iterator",
        "match",
        "matchAll",
        "replace",
        "search",
        "species",
        "split",
        "toPrimitive",
        "toStringTag",
        "unscopables",
        "dispose",
        "asyncDispose",
    }
)

# PHP test code that lives beside the production code (issue #2472):
# PHPUnit's default test-file suffix, the PSR-4 `Tests/` directory that
# Symfony-style components keep their tests in, and the name suffix of the
# base every PHPUnit test class extends (PHPUnit's `TestCase`, Symfony's
# `KernelTestCase`/`WebTestCase`, a project's abstract `*TestCase`). All three
# apply to .php files only, so the capitalised directory and the suffixes keep
# their meaning in other languages.
PHP_TEST_FILE_SUFFIX = "Test.php"
PHP_TEST_DIR_SEGMENT = "/Tests/"
PHP_TEST_CASE_SUFFIX = "TestCase"
# The external bases that make a PHP class a test, as the graph holds them:
# by class name alone (spelled fully qualified or never imported), or
# namespace-qualified through a `use` import. A name ending in
# PHP_TEST_CASE_SUFFIX counts only from one of these test-framework
# namespaces, so a vendor's `Acme\Qa\ScenarioTestCase` is not a test base.
PHP_TEST_BASE_NAMES: frozenset[str] = frozenset(
    {"TestCase", "KernelTestCase", "WebTestCase", "ApiTestCase", "PantherTestCase"}
)
PHP_TEST_FRAMEWORK_NAMESPACES: tuple[str, ...] = (
    "PHPUnit.",
    "Symfony.Bundle.FrameworkBundle.Test.",
    "Symfony.Component.Panther.",
    "Illuminate.Foundation.Testing.",
    "Orchestra.Testbench.",
    "Mockery.Adapter.Phpunit.",
)
