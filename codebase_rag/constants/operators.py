# (H) A C++ operator overload / user-defined literal is defined with the reserved
# (H) `operator` keyword heading the name (`operator==`, `operator[]`, `operator""_json`).
# (H) It is invoked by operator/literal syntax, not a named call, so it is a dead-code
# (H) reachability root; the keyword can only head such definitions, so this prefix on a
# (H) C++ file uniquely identifies them (member or free function).
CPP_OPERATOR_PREFIX = "operator"
# (H) JS/TS import specifier schemes that name genuinely external code (node
# (H) builtins, package registries, URLs). A specifier with any OTHER scheme
# (H) (`ext:` deno-runtime aliases, bundler virtual modules) points at first-party
# (H) code under a non-file-path name, so its unresolved calls defer to the trie.
JS_EXTERNAL_IMPORT_SCHEMES: frozenset[str] = frozenset(
    {"node", "npm", "jsr", "bun", "http", "https", "data", "file", "blob"}
)
TSCONFIG_FILENAMES: tuple[str, ...] = (
    "tsconfig.json",
    "tsconfig.base.json",
    "jsconfig.json",
)
# (H) When searching subdirectories for tsconfig files (monorepo `frontend/`,
# (H) `packages/*`), skip dependency/build/VCS trees: their tsconfigs carry
# (H) unrelated aliases and there can be thousands of them under node_modules.
TS_ALIAS_SKIP_DIRS: frozenset[str] = frozenset(
    {"node_modules", "dist", "build", "out", ".git"}
)
JS_INDEX_STEM = "index"
TS_COMPILER_OPTIONS_KEY = "compilerOptions"
TS_PATHS_KEY = "paths"
TS_BASE_URL_KEY = "baseUrl"
PATH_RELATIVE_PREFIX = "./"
PATH_PARENT_PREFIX = "../"
CPP_IMPORT_PARTITION_PREFIX = "import :"
CPP_PARTITION_PREFIX = "partition_"


# (H) Decorators whose presence marks a function/method as an implicit entry point
# (H) (web routes, task/flow handlers, fixtures, CLI commands, event listeners, and
# (H) Pydantic validators/serializers the framework invokes by registration).
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
    }
)

# (H) Go functions the runtime invokes with no explicit call site: `func init()`
# (H) runs at package load (any number per package), `func main()` is the program
# (H) entry. Both are reachability roots (like Python dunders), gated by the .go
# (H) extension so same-named symbols in other languages are unaffected.
GO_ROOT_FUNCTION_NAMES: frozenset[str] = frozenset({"init", "main"})

# (H) Rust `fn main()` is the binary entry point, invoked by the runtime with no
# (H) call site -- a reachability root (gated by .rs).
RUST_ROOT_FUNCTION_NAMES: frozenset[str] = frozenset({"main"})

# (H) Rust trait-impl methods the language/std dispatches implicitly (Display::fmt
# (H) via format!, PartialEq::eq via ==, Iterator::next via for, operator traits,
# (H) Drop::drop, serde, ...), never through an explicit call the graph can see.
# (H) Rooting them by name (gated by .rs) mirrors the Python-dunder exemption: these
# (H) names are conventionally reserved for trait impls, so a same-named user method
# (H) that is genuinely dead is under-reported rather than mis-reported.
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

# (H) Base classes that mark a class as a structural interface: its method stubs
# (H) are never call targets themselves (callers resolve to the implementations),
# (H) so dead-code analysis roots every method the class defines.
# (H) ponytail: direct bases only; transitive Protocol subclassing is not chased.
PROTOCOL_BASE_QNS: tuple[str, ...] = ("typing.Protocol", "typing_extensions.Protocol")

# (H) Substrings in a node's file path that mark it as test code. Covers Python
# (H) (test_, _test, conftest, /tests/), the JS/TS filename convention
# (H) (foo.test.ts, foo.spec.tsx), and the Jest __tests__/ directory so those
# (H) files are not reported as dead. Singular /test/ and /spec/ directories are
# (H) intentionally excluded: they collide with product code (a domain "spec"
# (H) module), which would misclassify live code as test.
# (H) Substrings in a node's file path that mark it as test code. Covers Python
# (H) (test_, _test, conftest, /tests/), the JS/TS filename convention
# (H) (foo.test.ts, foo.spec.tsx), the Jest __tests__/ directory, and the
# (H) Node.js/mocha singular /test/ dir (express: 34 of 49 dead-code reports
# (H) were test/ helpers). Matching is segment-anchored via the leading-slash
# (H) normalization, so contest/ and latest/ do not match. Singular /spec/
# (H) stays excluded: it collides with product code (a domain "spec" module),
# (H) which would misclassify live code as test.
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
