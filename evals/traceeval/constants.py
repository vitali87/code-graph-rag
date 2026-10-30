import os
from enum import StrEnum

from codebase_rag import constants as cs

# TraceEval (Li et al., "An Execution-Verified Multi-Language Benchmark for
# Code Semantic Reasoning", arXiv 2605.11006): 10,583 harnessed real-world
# programs whose call edges were each witnessed by a dynamic tracer. Pinned to
# one commit and one archive hash so a score names the exact ground truth it
# was measured against. Data is CC-BY 4.0 and is fetched, never committed.
CORPUS_NAME = "traceeval"
CORPUS_URL = "https://github.com/yikun-li/TraceEval"
CORPUS_COMMIT = "07fe7b845b267c49250bbb1ab933c18fbbade9a2"
CORPUS_ZIP = "data/traceeval.zip"
CORPUS_ZIP_SHA256 = "1a9deb3f8e9977996a2a1bddd4ff5705526e0fe596f63cdb211d98590d6b1727"
CORPUS_SPLIT_DIR = "data/traceeval_split"
SPLIT_IDS_FILENAME = "{split}_ids.json"
EXTRACT_DIRNAME = "{name}_extracted"
BENCHMARK_DIRNAME = "benchmark"
EXTRACT_DONE_MARKER = ".extracted"
# The archive was built on macOS: resource forks and Finder files ride along.
ZIP_SKIP_PREFIX = "__MACOSX/"
ZIP_SKIP_NAMES: frozenset[str] = frozenset({".DS_Store"})
HASH_CHUNK_BYTES = 1 << 20

GROUND_TRUTH_FILENAME = "callgraph.json"
# Files a program directory carries that are not its source: the ground
# truth itself, provenance, and a few stray model outputs the authors left in.
NON_SOURCE_FILENAMES: frozenset[str] = frozenset({GROUND_TRUTH_FILENAME, "README.md"})
NON_SOURCE_PREFIXES: tuple[str, ...] = ("result_", ".")

PROJECT_NAME = "traceeval_program"
# Shared with the indexing benchmark's pinned checkouts (gitignored).
CORPORA_DIRNAME = "corpora"
DEFAULT_WORKERS = max(1, (os.cpu_count() or 2) - 1)
WORKSPACE_DIRNAME = "traceeval_workspace"
CGR_HOME_DIRNAME = "cgr_home"
SCORES_FILENAME = "traceeval_scores.csv"
DIFF_FILENAME = "traceeval_diff.json"
TABLE_TITLE = "cgr CALLS vs TraceEval execution-verified edges ({split} split)"
CATEGORY = "traceeval"
MACRO_LABEL = "macro_avg"
PAPER_SUFFIX = ""
STATIC_SUFFIX = ":static"
EXACT_SUFFIX = ":exact"
FAILED_KEY = "failed:{language}"
FAILED_REPR = "{program}: {error}"
# Per-language diff samples: enough to read the dominant failure shapes
# without committing a multi-megabyte file.
DIFF_SAMPLE = 200
DIFF_EDGE_REPR = "{program}: {caller} -> {callee}"
WORKER_LOG_LEVEL = "ERROR"
MAIN_LOG_LEVEL = "INFO"
WORKER_NOT_STARTED = "init_worker must run before run_program"
DECODE_ERRORS = "replace"
POOL_CHUNKSIZE = 8


class Language(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    JAVA = "java"


class Split(StrEnum):
    TEST = "test"
    TRAIN = "train"


class NormMode(StrEnum):
    INIT = "init"
    NESTED_CLASS = "nested_class"
    STDLIB = "stdlib"


# The paper's compute_metrics.py always applies every mode; so does this eval,
# so a row here reads against Table 3 of the paper.
ALL_NORM_MODES: tuple[NormMode, ...] = (
    NormMode.INIT,
    NormMode.NESTED_CLASS,
    NormMode.STDLIB,
)

LANGUAGE_SUFFIXES: dict[Language, tuple[str, ...]] = {
    Language.PYTHON: (cs.EXT_PY,),
    Language.JAVASCRIPT: (".js",),
    Language.JAVA: (".java",),
}

CALLER_LABELS: frozenset[str] = frozenset(
    {
        cs.NodeLabel.MODULE.value,
        cs.NodeLabel.FUNCTION.value,
        cs.NodeLabel.METHOD.value,
    }
)
CALLEE_LABELS: frozenset[str] = frozenset(
    {cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value}
)

DOT = "."
PATH_SEP = "/"
JAVA_MEMBER_SEP = ":"
JAVA_PARAMS = "({params})"
JAVA_PARAM_JOIN = ","
JAVA_CTOR = "<init>"
JAVA_CLINIT = "<clinit>"
PY_CTOR = "__init__"
PY_NEW = "__new__"
JS_CTOR = "constructor"
PAREN_OPEN = "("
# Module-level JS code is credited to `main`: the tracer concatenates every
# file into one script whose call stack starts there.
JS_ROOT_CALLER = "main"
JAVA_MODULE_CALLER = "<module>:{path}"
DUPLICATE_SUFFIX_PATTERN = r"@\d+(?::\d+)?$"
# The JS tracer's first rewrite pass: an arrow bound by const/let/var is
# wrapped under an ordinal name, wherever it sits.
JS_ARROW_BINDING_PATTERN = r"(?:const|let|var)\s+{name}\s*=\s*(?:\([^)]*\)|\w+)\s*=>"
PY_LAMBDA_BINDING_PATTERN = r"\b{name}\s*=\s*lambda\b"
ARROW_MARKER = "<arrow:{name}>"
LAMBDA_MARKER = "<lambda:{name}>"
JS_PROPERTY_PATTERN = r"\b{name}\s*(?::|\()"
JS_OBJECT_BINDING_PATTERN = r"(?:const|let|var)\s+(\w+)\s*=$"
JS_NESTED_PROPERTY_PATTERN = r"(\w+)\s*:$"
ANON_OPEN = "<"
ANON_CLOSE = ">"
NAMED_ANGLE_SEGMENTS: frozenset[str] = frozenset({"<module>"})

# The Java tracer names a method from its source text (scripts/tracers/
# java_tracer.py, `add_simple_trace`): each parameter contributes the first
# whitespace token of its declaration, mapped through this table, else
# `<package>.<T>` when T is a class or interface declared in the same file,
# else the java.lang aliases below, else the token as written. Mirroring the
# rule is what lets a cgr method qn land on the tracer's name for it.
JAVA_TRACER_TYPE_MAP: dict[str, str] = {
    "String[]": "java.lang.String[]",
    "String": "java.lang.String",
    "Object": "java.lang.Object",
    "Throwable": "java.lang.Throwable",
}
JAVA_TRACER_LANG_ALIASES: dict[str, str] = {
    "Class": "java.lang.Class",
    "Thread": "java.lang.Thread",
    "Runnable": "java.lang.Runnable",
}
JAVA_PACKAGE_PATTERN = r"package\s+([\w.]+)\s*;"
JAVA_LOCAL_TYPE_PATTERN = r"(?:class|interface)\s+(\w+)"
JAVA_GENERIC_PATTERN = r"<[^<>]*>"
JAVA_SIGNATURE_PATTERN = r"^(?P<name>[^(]+)\((?P<params>.*)\)$"

# Edge normalization, ported from TraceEval's scripts/normalize_edges.py
# (Apache-2.0) so the scores here are computed exactly as the paper's are.
JAVA_STDLIB_PACKAGE_PREFIXES: tuple[str, ...] = (
    "java.",
    "javax.",
    "jdk.",
    "sun.",
    "com.sun.",
    "org.w3c.dom.",
    "org.xml.sax.",
    "org.ietf.jgss.",
    "org.omg.",
)
JAVA_STDLIB_HEAD_PATTERN = r"[:(]"
PY_BUILTINS_HEAD = "builtins"
JS_PROTOTYPE = "prototype"
JS_NODE_SCHEME = "node:"

JS_BUILTIN_GLOBALS: frozenset[str] = frozenset(
    {
        "Object",
        "Function",
        "Boolean",
        "Symbol",
        "Number",
        "BigInt",
        "Math",
        "Date",
        "String",
        "RegExp",
        "Error",
        "AggregateError",
        "EvalError",
        "RangeError",
        "ReferenceError",
        "SyntaxError",
        "TypeError",
        "URIError",
        "InternalError",
        "Array",
        "Map",
        "Set",
        "WeakMap",
        "WeakSet",
        "ArrayBuffer",
        "SharedArrayBuffer",
        "Atomics",
        "DataView",
        "Int8Array",
        "Uint8Array",
        "Uint8ClampedArray",
        "Int16Array",
        "Uint16Array",
        "Int32Array",
        "Uint32Array",
        "Float32Array",
        "Float64Array",
        "BigInt64Array",
        "BigUint64Array",
        "Reflect",
        "Proxy",
        "Promise",
        "Generator",
        "GeneratorFunction",
        "AsyncFunction",
        "AsyncGenerator",
        "AsyncGeneratorFunction",
        "JSON",
        "WeakRef",
        "FinalizationRegistry",
        "Iterator",
        "AsyncIterator",
        "Intl",
        "console",
        "URL",
        "URLSearchParams",
        "TextEncoder",
        "TextDecoder",
        "globalThis",
        "Buffer",
    }
)
JS_GLOBAL_FUNCTIONS: frozenset[str] = frozenset(
    {
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        "encodeURI",
        "decodeURI",
        "encodeURIComponent",
        "decodeURIComponent",
        "eval",
        "setTimeout",
        "setInterval",
        "clearTimeout",
        "clearInterval",
        "setImmediate",
        "clearImmediate",
        "queueMicrotask",
        "structuredClone",
        "atob",
        "btoa",
        "fetch",
        "require",
    }
)
NODE_BUILTIN_MODULES: frozenset[str] = frozenset(
    {
        "assert",
        "async_hooks",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "constants",
        "crypto",
        "dgram",
        "diagnostics_channel",
        "dns",
        "domain",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "inspector",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "sys",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
    }
)
JAVA_BARE_STDLIB_CLASSES: frozenset[str] = frozenset(
    {
        "String",
        "Object",
        "Integer",
        "Long",
        "Double",
        "Float",
        "Short",
        "Byte",
        "Boolean",
        "Character",
        "Number",
        "Math",
        "StrictMath",
        "System",
        "Thread",
        "ThreadGroup",
        "Runnable",
        "Throwable",
        "Exception",
        "RuntimeException",
        "IllegalArgumentException",
        "IllegalStateException",
        "NullPointerException",
        "IndexOutOfBoundsException",
        "ArrayIndexOutOfBoundsException",
        "ClassNotFoundException",
        "ClassCastException",
        "UnsupportedOperationException",
        "NumberFormatException",
        "Error",
        "AssertionError",
        "OutOfMemoryError",
        "StackOverflowError",
        "StringBuilder",
        "StringBuffer",
        "CharSequence",
        "Class",
        "ClassLoader",
        "Enum",
        "Iterable",
        "Comparable",
        "Cloneable",
        "AutoCloseable",
        "Process",
        "ProcessBuilder",
        "Package",
        "Void",
        "Record",
        "ArrayList",
        "LinkedList",
        "Vector",
        "Stack",
        "HashMap",
        "LinkedHashMap",
        "TreeMap",
        "Hashtable",
        "WeakHashMap",
        "IdentityHashMap",
        "ConcurrentHashMap",
        "ConcurrentSkipListMap",
        "HashSet",
        "LinkedHashSet",
        "TreeSet",
        "ConcurrentSkipListSet",
        "PriorityQueue",
        "ArrayDeque",
        "ArrayBlockingQueue",
        "LinkedBlockingQueue",
        "Deque",
        "Queue",
        "BlockingQueue",
        "List",
        "Set",
        "Map",
        "Collection",
        "SortedMap",
        "SortedSet",
        "NavigableMap",
        "NavigableSet",
        "Iterator",
        "ListIterator",
        "Spliterator",
        "Enumeration",
        "Comparator",
        "Optional",
        "OptionalInt",
        "OptionalLong",
        "OptionalDouble",
        "Random",
        "SecureRandom",
        "ThreadLocalRandom",
        "Scanner",
        "Date",
        "Calendar",
        "GregorianCalendar",
        "TimeZone",
        "SimpleTimeZone",
        "Locale",
        "Properties",
        "ResourceBundle",
        "Collections",
        "Arrays",
        "Objects",
        "EnumSet",
        "EnumMap",
        "Timer",
        "TimerTask",
        "UUID",
        "Base64",
        "BitSet",
        "Currency",
        "StringJoiner",
        "StringTokenizer",
        "Stream",
        "IntStream",
        "LongStream",
        "DoubleStream",
        "Collectors",
        "Collector",
        "CompletableFuture",
        "Future",
        "Executor",
        "Executors",
        "ExecutorService",
        "ScheduledExecutorService",
        "ThreadPoolExecutor",
        "ForkJoinPool",
        "AtomicInteger",
        "AtomicLong",
        "AtomicBoolean",
        "AtomicReference",
        "CountDownLatch",
        "CyclicBarrier",
        "Semaphore",
        "Phaser",
        "ReentrantLock",
        "ReentrantReadWriteLock",
        "Lock",
        "ReadWriteLock",
        "Condition",
        "Function",
        "BiFunction",
        "Consumer",
        "BiConsumer",
        "Supplier",
        "Predicate",
        "BiPredicate",
        "UnaryOperator",
        "BinaryOperator",
        "File",
        "FileReader",
        "FileWriter",
        "FileInputStream",
        "FileOutputStream",
        "BufferedReader",
        "BufferedWriter",
        "BufferedInputStream",
        "BufferedOutputStream",
        "PrintWriter",
        "PrintStream",
        "InputStream",
        "OutputStream",
        "Reader",
        "Writer",
        "InputStreamReader",
        "OutputStreamWriter",
        "DataInputStream",
        "DataOutputStream",
        "ObjectInputStream",
        "ObjectOutputStream",
        "Serializable",
        "Externalizable",
        "IOException",
        "FileNotFoundException",
        "EOFException",
        "ByteArrayInputStream",
        "ByteArrayOutputStream",
        "StringWriter",
        "StringReader",
        "PipedInputStream",
        "PipedOutputStream",
        "Path",
        "Paths",
        "Files",
        "FileSystems",
        "FileSystem",
        "ByteBuffer",
        "CharBuffer",
        "IntBuffer",
        "LongBuffer",
        "FloatBuffer",
        "DoubleBuffer",
        "Channel",
        "Channels",
        "FileChannel",
        "SocketChannel",
        "ServerSocketChannel",
        "Charset",
        "StandardCharsets",
        "CharsetEncoder",
        "CharsetDecoder",
        "StandardOpenOption",
        "OpenOption",
        "BigInteger",
        "BigDecimal",
        "MathContext",
        "RoundingMode",
        "MessageDigest",
        "MessageDigestSpi",
        "KeyPair",
        "KeyPairGenerator",
        "Signature",
        "Key",
        "PrivateKey",
        "PublicKey",
        "Provider",
        "Security",
        "SignatureException",
        "NoSuchAlgorithmException",
        "KeyStore",
        "Certificate",
        "SimpleDateFormat",
        "DateFormat",
        "DecimalFormat",
        "NumberFormat",
        "MessageFormat",
        "ChoiceFormat",
        "Format",
        "FieldPosition",
        "ParsePosition",
        "ParseException",
        "LocalDate",
        "LocalTime",
        "LocalDateTime",
        "ZonedDateTime",
        "OffsetDateTime",
        "OffsetTime",
        "Instant",
        "Duration",
        "Period",
        "Year",
        "YearMonth",
        "MonthDay",
        "DayOfWeek",
        "Month",
        "Clock",
        "ZoneId",
        "ZoneOffset",
        "DateTimeFormatter",
        "URL",
        "URI",
        "URLConnection",
        "HttpURLConnection",
        "Socket",
        "ServerSocket",
        "InetAddress",
        "Inet4Address",
        "Inet6Address",
        "InetSocketAddress",
        "MalformedURLException",
        "URISyntaxException",
        "UnknownHostException",
        "Connection",
        "Statement",
        "PreparedStatement",
        "CallableStatement",
        "ResultSet",
        "ResultSetMetaData",
        "DatabaseMetaData",
        "DriverManager",
        "SQLException",
        "Driver",
        "Time",
        "Timestamp",
        "Pattern",
        "Matcher",
        "MatchResult",
        "Logger",
        "Level",
        "Handler",
        "Formatter",
        "LogRecord",
        "Servlet",
        "ServletRequest",
        "ServletResponse",
        "HttpServlet",
        "HttpServletRequest",
        "HttpServletResponse",
    }
)
