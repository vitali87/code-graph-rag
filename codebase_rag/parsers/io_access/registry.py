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
)

_RUST_SINKS: tuple[IOSink, ...] = tuple(
    IOSink(f"{prefix}{module}::{fn}", kind, direction, target_arg=arg)
    for module, fn, kind, direction, arg in _RUST_CALL_METHODS
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
