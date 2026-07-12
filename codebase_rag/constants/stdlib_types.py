# (H) Language stdlib entity tables for external-call classification.

# (H) JavaScript built-in types
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

# (H) JavaScript built-in function patterns
# (H) JS/TS runtime global classes usable as `extends` bases with no import.
# (H) A base matching one of these that resolves to no first-party class is
# (H) positively external (builtin.<Name>), not an unresolvable guess.
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
# (H) `fn.bind(ctx)` / `fn.call(...)` / `fn.apply(...)` all use `fn`; when such a
# (H) call sits in a value position (`onError: handleError.bind(toast)`) the `.bind`
# (H) resolves to the Function.prototype builtin, so `fn` itself must be referenced
# (H) separately or it reports as dead.
JS_FUNCTION_PROTOTYPE_METHODS = frozenset(
    {JS_METHOD_BIND, JS_METHOD_CALL, JS_METHOD_APPLY}
)

# (H) Lua stdlib module names
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

# (H) C++ stdlib namespace and type inference prefixes
CPP_STD_NAMESPACE = "std"
CPP_PREFIX_IS = "is_"
CPP_PREFIX_HAS = "has_"

# (H) C++ stdlib entity names for heuristic detection
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

# (H) Java stdlib package prefixes for static stdlib detection
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

# (H) Java common class names for heuristic detection
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

# (H) Java type inference constants
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

# (H) java.lang types Java code names WITHOUT an import (the implicit java.lang
# (H) import). A bare `extends`/`implements` base matching one of these that
# (H) resolves to no first-party type is positively external
# (H) (java.lang.<Name>), not an unresolvable guess; mirrors the JS global
# (H) class rule (JS_GLOBAL_CLASS_NAMES -> builtin.<Name>).
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

# (H) C# base class library / framework roots. A qualified name under one of
# (H) these namespaces (`System.Collections.Generic.List`, `System.Linq.Enumerable`)
# (H) is external stdlib, not first-party code, so stdlib extraction folds the
# (H) trailing PascalCase type into its namespace path.
CSHARP_STDLIB_PREFIXES = (
    "System.",
    "Microsoft.",
    "Windows.",
    "Mono.",
)

# (H) Common BCL namespaces. C# namespaces are PascalCase like types, so the
# (H) case heuristic cannot tell a bare namespace reference (`using
# (H) System.Text.Json;`) from a type; a name that IS one of these is kept whole
# (H) instead of being folded into its parent as if its leaf were a type.
CSHARP_STDLIB_NAMESPACES = frozenset(
    {
        "System",
        "System.Collections",
        "System.Collections.Generic",
        "System.Collections.Concurrent",
        "System.Collections.ObjectModel",
        "System.Linq",
        "System.Text",
        "System.Text.Json",
        "System.Text.RegularExpressions",
        "System.Threading",
        "System.Threading.Tasks",
        "System.IO",
        "System.Net",
        "System.Net.Http",
        "System.Reflection",
        "System.Runtime",
        "System.Runtime.Serialization",
        "System.Globalization",
        "System.Diagnostics",
        "System.ComponentModel",
    }
)

# (H) Bare System namespace root (`System` itself), plus the ubiquitous top-level
# (H) primitives/aliases that appear without the `System.` prefix.
CSHARP_STDLIB_CLASSES = frozenset(
    {
        "Object",
        "String",
        "Int32",
        "Int64",
        "Boolean",
        "Double",
        "Decimal",
        "Guid",
        "DateTime",
        "TimeSpan",
        "Exception",
        "Task",
        "Nullable",
        "IDisposable",
        "IEnumerable",
        "IList",
        "IDictionary",
    }
)
