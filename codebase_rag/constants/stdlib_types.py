# Language stdlib entity tables for external-call classification.

# JavaScript built-in types
JS_BUILTIN_TYPES: frozenset[str] = frozenset(
    {
        "Array",
        "Object",
        "String",
        "Number",
        "Date",
        "RegExp",
        "Function",
        "Map",
        "Set",
        "Promise",
        "Error",
        "Boolean",
    }
)

# JavaScript built-in function patterns
# JS/TS runtime global classes usable as `extends` bases with no import.
# A base matching one that resolves to no first-party class is positively
# external (builtin.<Name>), not an unresolvable guess.
JS_GLOBAL_CLASS_NAMES: frozenset[str] = frozenset(
    {
        "Error",
        "TypeError",
        "RangeError",
        "SyntaxError",
        "ReferenceError",
        "EvalError",
        "URIError",
        "AggregateError",
        "Object",
        "Array",
        "Function",
        "Promise",
        "Map",
        "Set",
        "WeakMap",
        "WeakSet",
        "Date",
        "RegExp",
        "ArrayBuffer",
        "SharedArrayBuffer",
        "DataView",
        "EventTarget",
        "Event",
        "HTMLElement",
    }
)

JS_BUILTIN_PATTERNS: frozenset[str] = frozenset(
    {
        "Object.create",
        "Object.keys",
        "Object.values",
        "Object.entries",
        "Object.assign",
        "Object.freeze",
        "Object.seal",
        "Object.defineProperty",
        "Object.getPrototypeOf",
        "Object.setPrototypeOf",
        "Array.from",
        "Array.of",
        "Array.isArray",
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        "encodeURIComponent",
        "decodeURIComponent",
        "setTimeout",
        "clearTimeout",
        "setInterval",
        "clearInterval",
        "console.log",
        "console.error",
        "console.warn",
        "console.info",
        "console.debug",
        "JSON.parse",
        "JSON.stringify",
        "Math.random",
        "Math.floor",
        "Math.ceil",
        "Math.round",
        "Math.abs",
        "Math.max",
        "Math.min",
        "Date.now",
        "Date.parse",
    }
)

JS_METHOD_BIND = "bind"
JS_METHOD_CALL = "call"
JS_METHOD_APPLY = "apply"
JS_SUFFIX_BIND = ".bind"
JS_SUFFIX_CALL = ".call"
JS_SUFFIX_APPLY = ".apply"
JS_FUNCTION_PROTOTYPE_SUFFIXES: dict[str, str] = {
    JS_SUFFIX_BIND: JS_METHOD_BIND,
    JS_SUFFIX_CALL: JS_METHOD_CALL,
    JS_SUFFIX_APPLY: JS_METHOD_APPLY,
}
# `fn.bind(ctx)` / `fn.call(...)` / `fn.apply(...)` all use `fn`; in a value
# position (`onError: handleError.bind(toast)`) the `.bind` resolves to the
# Function.prototype builtin, so `fn` must be referenced separately or it
# reports as dead.
JS_FUNCTION_PROTOTYPE_METHODS = frozenset(
    {JS_METHOD_BIND, JS_METHOD_CALL, JS_METHOD_APPLY}
)

# Lua stdlib module names
LUA_STDLIB_MODULES = frozenset(
    {
        "string",
        "math",
        "table",
        "os",
        "io",
        "debug",
        "package",
        "coroutine",
        "utf8",
        "bit32",
    }
)

# C++ stdlib namespace and type inference prefixes
CPP_STD_NAMESPACE = "std"
CPP_PREFIX_IS = "is_"
CPP_PREFIX_HAS = "has_"

# C++ stdlib entity names for heuristic detection
CPP_STDLIB_ENTITIES = frozenset(
    {
        "vector",
        "string",
        "map",
        "set",
        "list",
        "deque",
        "unique_ptr",
        "shared_ptr",
        "weak_ptr",
        "thread",
        "mutex",
        "condition_variable",
        "future",
        "promise",
        "sort",
        "find",
        "copy",
        "transform",
        "accumulate",
    }
)

# Java stdlib package prefixes for static stdlib detection
JAVA_STDLIB_PREFIXES = (
    "java.",
    "javax.",
    "jdk.",
    "com.sun.",
    "sun.",
    "org.w3c.",
    "org.xml.",
    "org.ietf.",
    "org.omg.",
    "netscape.",
)

# Java common class names for heuristic detection
JAVA_STDLIB_CLASSES = frozenset(
    {
        "String",
        "Object",
        "Integer",
        "Double",
        "Boolean",
        "ArrayList",
        "HashMap",
        "HashSet",
        "LinkedList",
        "File",
        "URL",
        "Pattern",
        "LocalDateTime",
        "BigDecimal",
    }
)

# Java type inference constants
JAVA_LANG_PREFIX = "java.lang."
JAVA_ARRAY_SUFFIX = "[]"
JAVA_SUFFIX_EXCEPTION = "Exception"
JAVA_SUFFIX_ERROR = "Error"
JAVA_SUFFIX_INTERFACE = "Interface"
JAVA_SUFFIX_BUILDER = "Builder"
JAVA_PRIMITIVE_TYPES = frozenset(
    {
        "int",
        "long",
        "double",
        "float",
        "boolean",
        "char",
        "byte",
        "short",
    }
)
JAVA_WRAPPER_TYPES = frozenset(
    {
        "String",
        "Object",
        "Integer",
        "Long",
        "Double",
        "Boolean",
    }
)

# java.lang types Java code names WITHOUT an import (the implicit java.lang
# import). A bare `extends`/`implements` base matching one that resolves to
# no first-party type is positively external (java.lang.<Name>); mirrors the
# JS global class rule (JS_GLOBAL_CLASS_NAMES -> builtin.<Name>).
JAVA_LANG_CLASS_NAMES = JAVA_WRAPPER_TYPES | frozenset(
    {
        "Byte",
        "Short",
        "Float",
        "Character",
        "Number",
        "Void",
        "Enum",
        "Record",
        "Thread",
        "ThreadLocal",
        "ClassLoader",
        "SecurityManager",
        "StringBuilder",
        "StringBuffer",
        "Throwable",
        "Exception",
        "RuntimeException",
        "Error",
        "IllegalArgumentException",
        "IllegalStateException",
        "UnsupportedOperationException",
        "NullPointerException",
        "IndexOutOfBoundsException",
        "ArrayIndexOutOfBoundsException",
        "StringIndexOutOfBoundsException",
        "ClassCastException",
        "ArithmeticException",
        "NumberFormatException",
        "InterruptedException",
        "CloneNotSupportedException",
        "ReflectiveOperationException",
        "ClassNotFoundException",
        "NoSuchMethodException",
        "NoSuchFieldException",
        "InstantiationException",
        "IllegalAccessException",
        "SecurityException",
        "AssertionError",
        "StackOverflowError",
        "OutOfMemoryError",
        "LinkageError",
        "NoClassDefFoundError",
        "Runnable",
        "Comparable",
        "Iterable",
        "Cloneable",
        "AutoCloseable",
        "CharSequence",
        "Appendable",
        "Readable",
    }
)

# C# base class library / framework roots. A qualified name under one of
# these namespaces (`System.Collections.Generic.List`) is external stdlib,
# not first-party code, so stdlib extraction folds the trailing PascalCase
# type into its namespace path.
CSHARP_STDLIB_PREFIXES = (
    "System.",
    "Microsoft.",
    "Windows.",
    "Mono.",
)

# Recognized BCL types. ONLY a name in this set folds into its namespace
# (`System.Collections.Generic.List` -> `System.Collections.Generic`); every
# other PascalCase leaf is kept whole as a namespace, because C# namespaces
# are PascalCase too and a case heuristic would misfold them
# (`Microsoft.Extensions.Logging`).
CSHARP_STDLIB_CLASSES = frozenset(
    {
        # System primitives / core types
        "Object",
        "String",
        "Int32",
        "Int64",
        "Boolean",
        "Double",
        "Decimal",
        "Single",
        "Byte",
        "Char",
        "Guid",
        "DateTime",
        "DateTimeOffset",
        "TimeSpan",
        "Uri",
        "Exception",
        "Nullable",
        "Type",
        "Action",
        "Func",
        "Console",
        # System.Threading.Tasks
        "Task",
        "ValueTask",
        "CancellationToken",
        # System.Collections.Generic
        "List",
        "Dictionary",
        "HashSet",
        "Queue",
        "Stack",
        "SortedList",
        "SortedDictionary",
        "LinkedList",
        "IEnumerable",
        "ICollection",
        "IList",
        "IDictionary",
        "IReadOnlyList",
        "IReadOnlyDictionary",
        "KeyValuePair",
        # System.Linq
        "Enumerable",
        "IQueryable",
        # System interfaces
        "IDisposable",
        "IAsyncDisposable",
        "IComparable",
        "IEquatable",
        # Other ubiquitous BCL types (curated common set; a complete list
        # is unbounded, so the tail stays as full type paths rather than risk
        # a case heuristic that would misfold PascalCase namespaces).
        "Math",
        "MathF",
        "Random",
        "Convert",
        "Environment",
        "Array",
        "Span",
        "Memory",
        "Tuple",
        "Lazy",
        "GC",
        "StringBuilder",
        "StringComparer",
        "Regex",
        "Match",
        "Encoding",
        "File",
        "Directory",
        "Path",
        "Stream",
        "MemoryStream",
        "FileStream",
        "StreamReader",
        "StreamWriter",
        "TextReader",
        "TextWriter",
        "HttpClient",
        "HttpResponseMessage",
        "HttpRequestMessage",
        "JsonSerializer",
        "Thread",
        "Mutex",
        "SemaphoreSlim",
        "Stopwatch",
        "Timer",
        "CultureInfo",
        "IServiceProvider",
        "IServiceCollection",
        "ILogger",
    }
)
