from __future__ import annotations

from ... import constants as cs
from .constants import IODirection, ResourceKind
from .models import HandleConstructor, IOSink

# (H) Direct I/O sink calls, keyed by normalised (import-expanded) callee name.
_PYTHON_SINKS: tuple[IOSink, ...] = (
    IOSink("open", ResourceKind.FILE, IODirection.READ, target_arg=0, mode_arg=1),
    IOSink("os.getenv", ResourceKind.ENV, IODirection.READ, target_arg=0),
    IOSink("os.environ.get", ResourceKind.ENV, IODirection.READ, target_arg=0),
    IOSink("print", ResourceKind.STDOUT, IODirection.WRITE),
    IOSink("json.load", ResourceKind.FILE, IODirection.READ),
    IOSink("json.dump", ResourceKind.FILE, IODirection.WRITE),
    IOSink("requests.get", ResourceKind.NETWORK, IODirection.READ, target_arg=0),
    IOSink("requests.head", ResourceKind.NETWORK, IODirection.READ, target_arg=0),
    IOSink("requests.post", ResourceKind.NETWORK, IODirection.WRITE, target_arg=0),
    IOSink("requests.put", ResourceKind.NETWORK, IODirection.WRITE, target_arg=0),
    IOSink("requests.patch", ResourceKind.NETWORK, IODirection.WRITE, target_arg=0),
    IOSink("requests.delete", ResourceKind.NETWORK, IODirection.WRITE, target_arg=0),
    IOSink(
        "urllib.request.urlopen",
        ResourceKind.NETWORK,
        IODirection.READ,
        target_arg=0,
    ),
)

IO_SINKS: dict[cs.SupportedLanguage, tuple[IOSink, ...]] = {
    cs.SupportedLanguage.PYTHON: _PYTHON_SINKS,
}

# (H) Calls whose result is a resource handle; later method calls on the bound
# (H) variable are attributed to the same resource.
_PYTHON_HANDLE_CONSTRUCTORS: tuple[HandleConstructor, ...] = (
    HandleConstructor("open", ResourceKind.FILE, target_arg=0),
    HandleConstructor("sqlite3.connect", ResourceKind.DATABASE, target_arg=0),
    HandleConstructor("socket.socket", ResourceKind.SOCKET),
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
        "write": IODirection.WRITE,
        "writelines": IODirection.WRITE,
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
