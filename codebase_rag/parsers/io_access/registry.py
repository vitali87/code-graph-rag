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
)

IO_SINKS: dict[cs.SupportedLanguage, tuple[IOSink, ...]] = {
    cs.SupportedLanguage.PYTHON: _PYTHON_SINKS,
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
        "write": IODirection.WRITE,
        "writelines": IODirection.WRITE,
        "write_text": IODirection.WRITE,
        "write_bytes": IODirection.WRITE,
    },
    ResourceKind.NETWORK: {
        "get": IODirection.READ,
        "head": IODirection.READ,
        "options": IODirection.READ,
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
