"""pymgclient when it is installed, a stand-in that explains when it is not.

The Memgraph driver is a C extension, and where no wheel fits the platform
it builds from source with cmake: the step first-time users stumbled on
(issue #386). The embedded graph, the default backend, needs none of it, so
the driver moved to the `memgraph` extra. Modules that talk to Memgraph
import `mgclient` from here and keep naming its exception types: without
the driver those are placeholders, and `connect` raises one of them with a
message saying what to install, which each caller already reports as a
failed connection.
"""

from __future__ import annotations

from types import ModuleType

from . import exceptions as ex

_MGCLIENT_MODULE = "mgclient"


class _AbsentError(Exception):
    pass


class _AbsentDatabaseError(_AbsentError):
    pass


class _AbsentOperationalError(_AbsentDatabaseError):
    pass


class _AbsentTransientError(_AbsentDatabaseError):
    pass


def _absent_connect(*args: str | int | None, **kwargs: str | int | None) -> None:
    del args, kwargs
    raise _AbsentOperationalError(ex.MGCLIENT_MISSING)


def _absent_driver() -> ModuleType:
    """A module with the parts of `mgclient` this project names, and no driver."""
    module = ModuleType(_MGCLIENT_MODULE)
    vars(module).update(
        Error=_AbsentError,
        DatabaseError=_AbsentDatabaseError,
        OperationalError=_AbsentOperationalError,
        TransientError=_AbsentTransientError,
        connect=_absent_connect,
    )
    return module


def _load() -> tuple[ModuleType, bool]:
    # A plain import rather than importlib, so PyInstaller still sees the
    # driver and bundles it into the binary when it is installed.
    try:
        import mgclient as driver
    except ImportError:
        return _absent_driver(), False
    return driver, True


mgclient, MGCLIENT_AVAILABLE = _load()
