from __future__ import annotations

from ... import constants as cs
from .constants import IODirection, ResourceKind
from .models import HandleConstructor, IOSink

# (H) Direct I/O sink calls, keyed by normalised (import-expanded) callee name.
_PYTHON_SINKS: tuple[IOSink, ...] = (
    IOSink(
        "open",
        ResourceKind.FILE,
        IODirection.READ,
        target_arg=0,
        mode_arg=1,
        target_kw="file",
        mode_kw="mode",
    ),
    IOSink(
        "os.getenv", ResourceKind.ENV, IODirection.READ, target_arg=0, target_kw="key"
    ),
    IOSink(
        "os.environ.get",
        ResourceKind.ENV,
        IODirection.READ,
        target_arg=0,
        target_kw="key",
    ),
    IOSink("print", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("json.load", ResourceKind.FILE, IODirection.READ),
    IOSink("json.dump", ResourceKind.FILE, IODirection.WRITE),
    IOSink(
        "requests.get",
        ResourceKind.NETWORK,
        IODirection.READ,
        target_arg=0,
        target_kw="url",
    ),
    IOSink(
        "requests.head",
        ResourceKind.NETWORK,
        IODirection.READ,
        target_arg=0,
        target_kw="url",
    ),
    IOSink(
        "requests.post",
        ResourceKind.NETWORK,
        IODirection.WRITE,
        target_arg=0,
        target_kw="url",
    ),
    IOSink(
        "requests.put",
        ResourceKind.NETWORK,
        IODirection.WRITE,
        target_arg=0,
        target_kw="url",
    ),
    IOSink(
        "requests.patch",
        ResourceKind.NETWORK,
        IODirection.WRITE,
        target_arg=0,
        target_kw="url",
    ),
    IOSink(
        "requests.delete",
        ResourceKind.NETWORK,
        IODirection.WRITE,
        target_arg=0,
        target_kw="url",
    ),
    IOSink(
        "urllib.request.urlopen",
        ResourceKind.NETWORK,
        IODirection.READ,
        target_arg=0,
        target_kw="url",
    ),
    # (H) httpx module-level calls mirror requests.*: GET/HEAD read, the rest write.
    IOSink(
        "httpx.get",
        ResourceKind.NETWORK,
        IODirection.READ,
        target_arg=0,
        target_kw="url",
    ),
    IOSink(
        "httpx.head",
        ResourceKind.NETWORK,
        IODirection.READ,
        target_arg=0,
        target_kw="url",
    ),
    IOSink(
        "httpx.post",
        ResourceKind.NETWORK,
        IODirection.WRITE,
        target_arg=0,
        target_kw="url",
    ),
    IOSink(
        "httpx.put",
        ResourceKind.NETWORK,
        IODirection.WRITE,
        target_arg=0,
        target_kw="url",
    ),
    IOSink(
        "httpx.patch",
        ResourceKind.NETWORK,
        IODirection.WRITE,
        target_arg=0,
        target_kw="url",
    ),
    IOSink(
        "httpx.delete",
        ResourceKind.NETWORK,
        IODirection.WRITE,
        target_arg=0,
        target_kw="url",
    ),
    # (H) aiohttp.request(method, url): the HTTP verb is arg 0 and the url arg 1, so
    # (H) direction is unknown without reading the verb -- READ_WRITE is the honest
    # (H) "either" label (same stance as DB execute()).
    IOSink(
        "aiohttp.request",
        ResourceKind.NETWORK,
        IODirection.READ_WRITE,
        target_arg=1,
        target_kw="url",
    ),
)

# (H) JS/TS direct-call I/O sinks (issue #714, first increment). Keyed by the
# (H) dotted callee text (`console.log`, `fs.writeFileSync`, `axios.get`); a bare
# (H) global like `fetch` matches when it is not shadowed by an import. Node has
# (H) no keyword args, so the URL/path is always positional arg 0. Member-access
# (H) reads (`process.env.X`) and stream handles are a follow-up.
_JS_TS_SINKS: tuple[IOSink, ...] = (
    IOSink("console.log", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("console.info", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("console.warn", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("console.error", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("fetch", ResourceKind.NETWORK, IODirection.READ, target_arg=0),
    IOSink("axios.get", ResourceKind.NETWORK, IODirection.READ, target_arg=0),
    IOSink("axios.head", ResourceKind.NETWORK, IODirection.READ, target_arg=0),
    IOSink("axios.post", ResourceKind.NETWORK, IODirection.WRITE, target_arg=0),
    IOSink("axios.put", ResourceKind.NETWORK, IODirection.WRITE, target_arg=0),
    IOSink("axios.patch", ResourceKind.NETWORK, IODirection.WRITE, target_arg=0),
    IOSink("axios.delete", ResourceKind.NETWORK, IODirection.WRITE, target_arg=0),
    IOSink("http.get", ResourceKind.NETWORK, IODirection.READ, target_arg=0),
    IOSink("https.get", ResourceKind.NETWORK, IODirection.READ, target_arg=0),
    IOSink("fs.readFile", ResourceKind.FILE, IODirection.READ, target_arg=0),
    IOSink("fs.readFileSync", ResourceKind.FILE, IODirection.READ, target_arg=0),
    IOSink("fs.writeFile", ResourceKind.FILE, IODirection.WRITE, target_arg=0),
    IOSink("fs.writeFileSync", ResourceKind.FILE, IODirection.WRITE, target_arg=0),
    IOSink("fs.appendFile", ResourceKind.FILE, IODirection.WRITE, target_arg=0),
    IOSink("fs.appendFileSync", ResourceKind.FILE, IODirection.WRITE, target_arg=0),
)

# (H) Go direct-call I/O sinks (issue #714). Keyed by the FULL import path plus the
# (H) function (`os.Getenv`, `net/http.Get`, `io/ioutil.ReadFile`), because a Go call
# (H) `http.Get` resolves through import_map to its package path (`http -> net/http`)
# (H) and match_normalised keys on that. Using the full path (not the bare package
# (H) name) is what distinguishes the stdlib `net/http` from a third-party package
# (H) that also happens to be named `http`, and it handles import aliases for free
# (H) (`import h "net/http"` -> h resolves to net/http). Go has no keyword args, so the
# (H) path/url is positional arg 0. Handle-returning calls (`os.Open`) are treated as
# (H) a direct read/write of the path (stream handles are a follow-up); log.* and
# (H) fmt.Fprint* (writer-targeted) are follow-ups.
_GO_SINKS: tuple[IOSink, ...] = (
    IOSink("os.Getenv", ResourceKind.ENV, IODirection.READ, target_arg=0),
    IOSink("os.LookupEnv", ResourceKind.ENV, IODirection.READ, target_arg=0),
    IOSink("os.ReadFile", ResourceKind.FILE, IODirection.READ, target_arg=0),
    IOSink("os.Open", ResourceKind.FILE, IODirection.READ, target_arg=0),
    IOSink("io/ioutil.ReadFile", ResourceKind.FILE, IODirection.READ, target_arg=0),
    IOSink("os.WriteFile", ResourceKind.FILE, IODirection.WRITE, target_arg=0),
    IOSink("os.Create", ResourceKind.FILE, IODirection.WRITE, target_arg=0),
    IOSink("os.Remove", ResourceKind.FILE, IODirection.WRITE, target_arg=0),
    IOSink("io/ioutil.WriteFile", ResourceKind.FILE, IODirection.WRITE, target_arg=0),
    IOSink("fmt.Print", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("fmt.Println", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("fmt.Printf", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("net/http.Get", ResourceKind.NETWORK, IODirection.READ, target_arg=0),
    IOSink("net/http.Head", ResourceKind.NETWORK, IODirection.READ, target_arg=0),
    IOSink("net/http.Post", ResourceKind.NETWORK, IODirection.WRITE, target_arg=0),
    IOSink("net/http.PostForm", ResourceKind.NETWORK, IODirection.WRITE, target_arg=0),
)

# (H) Java direct-call I/O sinks (issue #714). Keyed by the dotted callee text
# (H) reconstructed from `method_invocation` (`System.getenv`, `System.out.println`):
# (H) `System` (java.lang) and `Files` (java.nio.file) are effective globals, so the
# (H) sink table is not import-gated (sinks_require_import=False); a local named
# (H) `System`/`Files` still shadows it. Java has no keyword args, so the env key /
# (H) path is positional arg 0. Handle-based I/O (java.io/java.nio streams, java.sql
# (H) Statement.execute*, HttpClient) and static-imported bare calls are a follow-up.
_JAVA_SYSTEM_SINKS: tuple[IOSink, ...] = (
    IOSink("System.getenv", ResourceKind.ENV, IODirection.READ, target_arg=0),
    IOSink("System.out.println", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("System.out.print", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("System.out.printf", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("System.out.format", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("System.out.write", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("System.err.println", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("System.err.print", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("System.err.printf", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("System.err.format", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("System.err.write", ResourceKind.STDOUT, IODirection.WRITE),
)

# (H) java.nio.file.Files static sinks. Registered under BOTH the simple `Files.X`
# (H) (bare call, Files not in import_map) and the fully-qualified
# (H) `java.nio.file.Files.X` key: with `import java.nio.file.Files` the call
# (H) normalises to the FQN, and a fully-qualified call carries the FQN in its text --
# (H) so both the imported and the qualified-call cases resolve.
_JAVA_FILES_METHODS: tuple[tuple[str, IODirection], ...] = (
    ("readString", IODirection.READ),
    ("readAllLines", IODirection.READ),
    ("readAllBytes", IODirection.READ),
    ("lines", IODirection.READ),
    ("writeString", IODirection.WRITE),
    ("write", IODirection.WRITE),
)

_JAVA_SINKS: tuple[IOSink, ...] = (
    *_JAVA_SYSTEM_SINKS,
    *(
        IOSink(f"{prefix}.{method}", ResourceKind.FILE, direction, target_arg=0)
        for method, direction in _JAVA_FILES_METHODS
        for prefix in ("Files", "java.nio.file.Files")
    ),
)

# (H) Rust direct-call I/O sinks (issue #714). call_name yields the callee's path text
# (H) with `::` separators (`std::env::var`, `std::fs::write`); Rust code reaches these
# (H) either fully-qualified or via `use std::fs;` + `fs::write(..)`, so each sink is
# (H) keyed under BOTH the full `std::MODULE::fn` and the short `MODULE::fn` form (the
# (H) `::` path is not shadowable by a local, so no import-gating needed). Rust has no
# (H) keyword args -> path/key is positional arg 0. File handles (`File::open`,
# (H) `BufReader`) and the print macros (handled separately) are follow-ups here.
_RUST_CALL_METHODS: tuple[
    tuple[str, str, ResourceKind, IODirection, int | None], ...
] = (
    ("env", "var", ResourceKind.ENV, IODirection.READ, 0),
    ("env", "vars", ResourceKind.ENV, IODirection.READ, None),
    ("fs", "read_to_string", ResourceKind.FILE, IODirection.READ, 0),
    ("fs", "read", ResourceKind.FILE, IODirection.READ, 0),
    ("fs", "write", ResourceKind.FILE, IODirection.WRITE, 0),
    ("fs", "remove_file", ResourceKind.FILE, IODirection.WRITE, 0),
    ("fs", "create_dir", ResourceKind.FILE, IODirection.WRITE, 0),
    ("fs", "create_dir_all", ResourceKind.FILE, IODirection.WRITE, 0),
    ("fs", "remove_dir", ResourceKind.FILE, IODirection.WRITE, 0),
    ("fs", "remove_dir_all", ResourceKind.FILE, IODirection.WRITE, 0),
)

# (H) Keyed only under the fully `std::`-qualified form. A bare short path
# (H) (`use std::fs; fs::write`) resolves by expanding its head segment through the
# (H) import map (fs -> std::fs); keying the bare `fs::write` too would overmatch a
# (H) local `mod fs { fn write() }` that never touches std.
_RUST_SINKS: tuple[IOSink, ...] = tuple(
    IOSink(f"std::{module}::{fn}", kind, direction, target_arg=arg)
    for module, fn, kind, direction, arg in _RUST_CALL_METHODS
)

# (H) C++ direct-call I/O sinks (issue #714). getenv reads ENV; printf/puts write
# (H) STDOUT unconditionally. Deliberately EXCLUDED (ambiguous by name alone, deferred
# (H) to the stream-handle follow-up): fprintf/fputs/fwrite (first arg is a FILE* that
# (H) may be stdout/stderr OR a file); remove/rename (std::remove/rename overload the
# (H) STL erase-remove algorithm on iterators, so keying them would false-match).
_CPP_CALL_METHODS: tuple[tuple[str, ResourceKind, IODirection, int | None], ...] = (
    ("getenv", ResourceKind.ENV, IODirection.READ, 0),
    ("secure_getenv", ResourceKind.ENV, IODirection.READ, 0),
    ("printf", ResourceKind.STDOUT, IODirection.WRITE, None),
    ("puts", ResourceKind.STDOUT, IODirection.WRITE, None),
    ("putchar", ResourceKind.STDOUT, IODirection.WRITE, None),
)

# (H) Keyed under both the bare (C linkage / `using namespace std`) and `std::`-
# (H) qualified forms; C++ has no use-alias import map to expand a short path.
_CPP_SINKS: tuple[IOSink, ...] = tuple(
    IOSink(f"{prefix}{fn}", kind, direction, target_arg=arg)
    for fn, kind, direction, arg in _CPP_CALL_METHODS
    for prefix in ("", "std::")
)

IO_SINKS: dict[cs.SupportedLanguage, tuple[IOSink, ...]] = {
    cs.SupportedLanguage.PYTHON: _PYTHON_SINKS,
    cs.SupportedLanguage.JS: _JS_TS_SINKS,
    cs.SupportedLanguage.TS: _JS_TS_SINKS,
    cs.SupportedLanguage.TSX: _JS_TS_SINKS,
    cs.SupportedLanguage.GO: _GO_SINKS,
    cs.SupportedLanguage.JAVA: _JAVA_SINKS,
    cs.SupportedLanguage.RUST: _RUST_SINKS,
    cs.SupportedLanguage.CPP: _CPP_SINKS,
}

# (H) Macro-call I/O sinks keyed by macro name (issue #714). Rust's stdout/stderr write
# (H) path is the `println!`/`print!`/`eprintln!`/`eprint!` macros, not calls; the target
# (H) is a format template, so the resource identity is always <dynamic> (STDOUT).
_RUST_MACRO_SINKS: dict[str, IOSink] = {
    name: IOSink(name, ResourceKind.STDOUT, IODirection.WRITE)
    for name in ("println", "print", "eprintln", "eprint")
}

IO_MACRO_SINKS: dict[cs.SupportedLanguage, dict[str, IOSink]] = {
    cs.SupportedLanguage.RUST: _RUST_MACRO_SINKS,
}

# (H) Stream-insertion I/O sinks keyed by the left-spine base of a `<<` chain
# (H) (`std::cout << x` writes STDOUT). Both the bare and std:: forms are keyed.
_CPP_STREAM_SINKS: dict[str, IOSink] = {
    f"{prefix}{name}": IOSink(name, ResourceKind.STDOUT, IODirection.WRITE)
    for name in ("cout", "cerr", "clog", "wcout", "wcerr", "wclog")
    for prefix in ("", "std::")
}

IO_STREAM_SINKS: dict[cs.SupportedLanguage, dict[str, IOSink]] = {
    cs.SupportedLanguage.CPP: _CPP_STREAM_SINKS,
}

# (H) Member/subscript accesses that are I/O reads, keyed by the object prefix:
# (H) `process.env.X` / `process.env['X']` reads env var X (issue #714). The head
# (H) token (`process`) is shadow-checked like a sink, so a local `process` is
# (H) not the Node global.
_JS_TS_MEMBER_READS: tuple[tuple[str, ResourceKind], ...] = (
    ("process.env", ResourceKind.ENV),
)

IO_MEMBER_READS: dict[cs.SupportedLanguage, tuple[tuple[str, ResourceKind], ...]] = {
    cs.SupportedLanguage.JS: _JS_TS_MEMBER_READS,
    cs.SupportedLanguage.TS: _JS_TS_MEMBER_READS,
    cs.SupportedLanguage.TSX: _JS_TS_MEMBER_READS,
}

# (H) Calls whose result is a resource handle; later method calls on the bound
# (H) variable are attributed to the same resource.
_PYTHON_HANDLE_CONSTRUCTORS: tuple[HandleConstructor, ...] = (
    HandleConstructor("open", ResourceKind.FILE, target_arg=0, target_kw="file"),
    HandleConstructor(
        "sqlite3.connect", ResourceKind.DATABASE, target_arg=0, target_kw="database"
    ),
    HandleConstructor("socket.socket", ResourceKind.SOCKET),
    # (H) A pathlib.Path is a FILE handle: read_text/write_text on the bound var
    # (H) attribute to the path literal passed to Path(...). Non-I/O methods
    # (H) (.exists, .parent) simply aren't in IO_HANDLE_METHODS, so no false edge.
    HandleConstructor("pathlib.Path", ResourceKind.FILE, target_arg=0),
    # (H) httpx/aiohttp client objects are NETWORK handles; later client.get/post
    # (H) resolve through cross-scope handle resolution (the URL is on the method
    # (H) call, not the constructor, so the resource identity is <dynamic>).
    HandleConstructor("httpx.Client", ResourceKind.NETWORK),
    HandleConstructor("httpx.AsyncClient", ResourceKind.NETWORK),
    HandleConstructor("aiohttp.ClientSession", ResourceKind.NETWORK),
)

IO_HANDLE_CONSTRUCTORS: dict[cs.SupportedLanguage, tuple[HandleConstructor, ...]] = {
    cs.SupportedLanguage.PYTHON: _PYTHON_HANDLE_CONSTRUCTORS,
}

# (H) Per-kind handle methods and the direction each implies.
IO_HANDLE_METHODS: dict[ResourceKind, dict[str, IODirection]] = {
    ResourceKind.FILE: {
        "read": IODirection.READ,
        "readline": IODirection.READ,
        "readlines": IODirection.READ,
        "read_text": IODirection.READ,
        "read_bytes": IODirection.READ,
        "iterdir": IODirection.READ,
        "glob": IODirection.READ,
        "rglob": IODirection.READ,
        # (H) Path.open() returns a nested file handle we don't chain; its direction
        # (H) depends on the mode arg the handle-method path can't inspect, so default
        # (H) to READ like the top-level open() sink does with no mode (a false WRITE
        # (H) on the common read case would be worse than a coarse READ).
        "open": IODirection.READ,
        "write": IODirection.WRITE,
        "writelines": IODirection.WRITE,
        "write_text": IODirection.WRITE,
        "write_bytes": IODirection.WRITE,
        "touch": IODirection.WRITE,
        "unlink": IODirection.WRITE,
    },
    ResourceKind.NETWORK: {
        "get": IODirection.READ,
        "head": IODirection.READ,
        "options": IODirection.READ,
        # (H) client.request(method, url): verb-dependent direction, READ_WRITE = either.
        "request": IODirection.READ_WRITE,
        "post": IODirection.WRITE,
        "put": IODirection.WRITE,
        "patch": IODirection.WRITE,
        "delete": IODirection.WRITE,
    },
    ResourceKind.DATABASE: {
        "execute": IODirection.READ_WRITE,
        "executemany": IODirection.WRITE,
        "executescript": IODirection.WRITE,
        "fetchone": IODirection.READ,
        "fetchall": IODirection.READ,
        "fetchmany": IODirection.READ,
        "commit": IODirection.WRITE,
    },
    ResourceKind.SOCKET: {
        "recv": IODirection.READ,
        "recvfrom": IODirection.READ,
        "send": IODirection.WRITE,
        "sendall": IODirection.WRITE,
        "sendto": IODirection.WRITE,
    },
}

# (H) Methods that DERIVE a same-resource sub-handle from a handle, keyed by the
# (H) parent's kind: `cur = conn.cursor()` (Python sqlite3/DB-API) and
# (H) `Statement st = conn.createStatement()` (java.sql) both yield a handle whose
# (H) I/O touches the connection's database (issue #714). Method names are
# (H) distinctive enough to share one kind-keyed table across languages.
IO_HANDLE_DERIVES: dict[ResourceKind, frozenset[str]] = {
    ResourceKind.DATABASE: frozenset({"cursor", "createStatement", "prepareStatement"}),
}

# (H) Lean-walk handle constructors (issue #714): call-shaped calls whose return
# (H) value is a resource handle, keyed exactly like each language's IO_SINKS
# (H) (Go: full import path; Java: effective-global head, both simple and FQN;
# (H) Rust: full std:: path, short paths expand through the import map on `::`).
_JS_TS_LEAN_HANDLE_CONSTRUCTORS: tuple[HandleConstructor, ...] = (
    HandleConstructor("fs.createReadStream", ResourceKind.FILE, target_arg=0),
    HandleConstructor("fs.createWriteStream", ResourceKind.FILE, target_arg=0),
)

# (H) os.Open / os.Create are ALSO direct sinks (the construction touches the
# (H) path); os.OpenFile is a handle only, because its direction depends on flags.
_GO_LEAN_HANDLE_CONSTRUCTORS: tuple[HandleConstructor, ...] = (
    HandleConstructor("os.Open", ResourceKind.FILE, target_arg=0),
    HandleConstructor("os.Create", ResourceKind.FILE, target_arg=0),
    HandleConstructor("os.OpenFile", ResourceKind.FILE, target_arg=0),
    HandleConstructor("database/sql.Open", ResourceKind.DATABASE, target_arg=1),
    HandleConstructor("net.Dial", ResourceKind.SOCKET, target_arg=1),
)

_JAVA_LEAN_HANDLE_CONSTRUCTORS: tuple[HandleConstructor, ...] = tuple(
    HandleConstructor(f"{prefix}.{method}", kind, target_arg=arg)
    for method, kind, arg, prefixes in (
        ("newBufferedReader", ResourceKind.FILE, 0, ("Files", "java.nio.file.Files")),
        ("newBufferedWriter", ResourceKind.FILE, 0, ("Files", "java.nio.file.Files")),
        ("newInputStream", ResourceKind.FILE, 0, ("Files", "java.nio.file.Files")),
        ("newOutputStream", ResourceKind.FILE, 0, ("Files", "java.nio.file.Files")),
        (
            "getConnection",
            ResourceKind.DATABASE,
            0,
            ("DriverManager", "java.sql.DriverManager"),
        ),
    )
    for prefix in prefixes
)

_RUST_LEAN_HANDLE_CONSTRUCTORS: tuple[HandleConstructor, ...] = (
    HandleConstructor("std::fs::File::open", ResourceKind.FILE, target_arg=0),
    HandleConstructor("std::fs::File::create", ResourceKind.FILE, target_arg=0),
    HandleConstructor(
        "std::net::TcpStream::connect", ResourceKind.SOCKET, target_arg=0
    ),
)

IO_LEAN_HANDLE_CONSTRUCTORS: dict[
    cs.SupportedLanguage, tuple[HandleConstructor, ...]
] = {
    cs.SupportedLanguage.JS: _JS_TS_LEAN_HANDLE_CONSTRUCTORS,
    cs.SupportedLanguage.TS: _JS_TS_LEAN_HANDLE_CONSTRUCTORS,
    cs.SupportedLanguage.TSX: _JS_TS_LEAN_HANDLE_CONSTRUCTORS,
    cs.SupportedLanguage.GO: _GO_LEAN_HANDLE_CONSTRUCTORS,
    cs.SupportedLanguage.JAVA: _JAVA_LEAN_HANDLE_CONSTRUCTORS,
    cs.SupportedLanguage.RUST: _RUST_LEAN_HANDLE_CONSTRUCTORS,
}

# (H) `new`-shaped handle constructors keyed by the written type name (Java
# (H) `new FileWriter("x")`). PrintWriter appears here for its filename overload;
# (H) its writer-wrapping overload resolves via IO_NEW_HANDLE_WRAPPERS first.
IO_NEW_HANDLE_CONSTRUCTORS: dict[cs.SupportedLanguage, dict[str, HandleConstructor]] = {
    cs.SupportedLanguage.JAVA: {
        name: HandleConstructor(name, kind, target_arg=0)
        for name, kind in (
            ("FileReader", ResourceKind.FILE),
            ("FileInputStream", ResourceKind.FILE),
            ("FileWriter", ResourceKind.FILE),
            ("FileOutputStream", ResourceKind.FILE),
            ("PrintWriter", ResourceKind.FILE),
            ("RandomAccessFile", ResourceKind.FILE),
            ("Socket", ResourceKind.SOCKET),
        )
    },
}

# (H) `new`-shaped WRAPPER types: the resource identity comes from arg0, which is
# (H) either a nested handle constructor (`new BufferedReader(new FileReader(p))`)
# (H) or an already-bound handle variable.
IO_NEW_HANDLE_WRAPPERS: dict[cs.SupportedLanguage, frozenset[str]] = {
    cs.SupportedLanguage.JAVA: frozenset(
        {
            "BufferedReader",
            "BufferedWriter",
            "BufferedInputStream",
            "BufferedOutputStream",
            "InputStreamReader",
            "OutputStreamWriter",
            "PrintWriter",
            "Scanner",
        }
    ),
}

# (H) Call-shaped wrapper constructors (`BufReader::new(f)`, `bufio.NewReader(f)`),
# (H) keyed like sinks; the value is unused (membership only, dict for the shared
# (H) shadow-aware resolution).
IO_CALL_HANDLE_WRAPPERS: dict[cs.SupportedLanguage, dict[str, str]] = {
    cs.SupportedLanguage.RUST: {
        name: name for name in ("std::io::BufReader::new", "std::io::BufWriter::new")
    },
    cs.SupportedLanguage.GO: {
        name: name
        for name in ("bufio.NewReader", "bufio.NewWriter", "bufio.NewScanner")
    },
}

# (H) Type-declaration constructors (C++ `std::ifstream in("x")`): a declaration
# (H) whose written type is one of these binds a FILE handle on its declarator.
IO_TYPE_HANDLE_CONSTRUCTORS: dict[cs.SupportedLanguage, dict[str, ResourceKind]] = {
    cs.SupportedLanguage.CPP: {
        f"{prefix}{name}": ResourceKind.FILE
        for name in ("ifstream", "ofstream", "fstream")
        for prefix in ("", "std::")
    },
}

# (H) Identity unwrapping for constructor targets: `Path.of("cfg.txt")` /
# (H) `new File("x")` in a constructor's target position carry the literal.
IO_IDENTITY_UNWRAP_CALLS: dict[cs.SupportedLanguage, frozenset[str]] = {
    cs.SupportedLanguage.JAVA: frozenset(
        {
            "Path.of",
            "Paths.get",
            "java.nio.file.Path.of",
            "java.nio.file.Paths.get",
        }
    ),
}

# (H) `new File("x")` is not a handle itself, but it designates the resource: in
# (H) a constructor's target position it carries the identity literal, and under
# (H) a wrapper (`new Scanner(new File("x"))`) it resolves to a handle of the
# (H) mapped kind.
IO_IDENTITY_UNWRAP_NEW_TYPES: dict[cs.SupportedLanguage, dict[str, ResourceKind]] = {
    cs.SupportedLanguage.JAVA: {"File": ResourceKind.FILE},
}

# (H) JS/TS share one method table across the three dialects.
_JS_TS_LEAN_HANDLE_METHODS: dict[ResourceKind, dict[str, IODirection]] = {
    ResourceKind.FILE: {
        "read": IODirection.READ,
        "write": IODirection.WRITE,
        "end": IODirection.WRITE,
    },
}

# (H) Per-language, per-kind handle methods for the lean walk and the direction
# (H) each implies (Python keeps IO_HANDLE_METHODS above). READ_WRITE entries
# (H) (java.sql execute) are refined by the SQL first-keyword heuristic.
IO_LEAN_HANDLE_METHODS: dict[
    cs.SupportedLanguage, dict[ResourceKind, dict[str, IODirection]]
] = {
    cs.SupportedLanguage.JS: _JS_TS_LEAN_HANDLE_METHODS,
    cs.SupportedLanguage.TS: _JS_TS_LEAN_HANDLE_METHODS,
    cs.SupportedLanguage.TSX: _JS_TS_LEAN_HANDLE_METHODS,
    cs.SupportedLanguage.GO: {
        ResourceKind.FILE: {
            "Read": IODirection.READ,
            "ReadAt": IODirection.READ,
            "ReadString": IODirection.READ,
            "ReadLine": IODirection.READ,
            "ReadByte": IODirection.READ,
            "ReadRune": IODirection.READ,
            "Scan": IODirection.READ,
            "Write": IODirection.WRITE,
            "WriteAt": IODirection.WRITE,
            "WriteString": IODirection.WRITE,
            "Flush": IODirection.WRITE,
        },
        ResourceKind.DATABASE: {
            "Query": IODirection.READ,
            "QueryRow": IODirection.READ,
            "QueryContext": IODirection.READ,
            "QueryRowContext": IODirection.READ,
            "Exec": IODirection.WRITE,
            "ExecContext": IODirection.WRITE,
        },
        ResourceKind.SOCKET: {
            "Read": IODirection.READ,
            "Write": IODirection.WRITE,
        },
    },
    cs.SupportedLanguage.JAVA: {
        ResourceKind.FILE: {
            "read": IODirection.READ,
            "readLine": IODirection.READ,
            "readAllBytes": IODirection.READ,
            "readNBytes": IODirection.READ,
            "lines": IODirection.READ,
            "next": IODirection.READ,
            "nextLine": IODirection.READ,
            "nextInt": IODirection.READ,
            "write": IODirection.WRITE,
            "append": IODirection.WRITE,
            "newLine": IODirection.WRITE,
            "print": IODirection.WRITE,
            "println": IODirection.WRITE,
            "printf": IODirection.WRITE,
            "format": IODirection.WRITE,
        },
        ResourceKind.DATABASE: {
            "executeQuery": IODirection.READ,
            "executeUpdate": IODirection.WRITE,
            "executeBatch": IODirection.WRITE,
            "execute": IODirection.READ_WRITE,
        },
    },
    cs.SupportedLanguage.RUST: {
        ResourceKind.FILE: {
            "read_to_string": IODirection.READ,
            "read": IODirection.READ,
            "read_exact": IODirection.READ,
            "read_line": IODirection.READ,
            "lines": IODirection.READ,
            "write_all": IODirection.WRITE,
            "write": IODirection.WRITE,
            "write_fmt": IODirection.WRITE,
            "flush": IODirection.WRITE,
        },
        ResourceKind.SOCKET: {
            "read_to_string": IODirection.READ,
            "read": IODirection.READ,
            "read_exact": IODirection.READ,
            "write_all": IODirection.WRITE,
            "write": IODirection.WRITE,
        },
    },
    cs.SupportedLanguage.CPP: {
        ResourceKind.FILE: {
            "read": IODirection.READ,
            "get": IODirection.READ,
            "getline": IODirection.READ,
            "write": IODirection.WRITE,
            "put": IODirection.WRITE,
        },
    },
}
