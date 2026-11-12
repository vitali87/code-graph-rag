"""Load the system MSVC runtime before a stray copy does.

Windows only, and a no-op everywhere else.

Endpoint-security agents ship their own copy of the Visual C++ runtime and put
their install directory on PATH. McAfee Endpoint Security's Threat Prevention
component is the one seen in the field: it carries an old ``MSVCP140.dll``
under ``Program Files\\McAfee\\Endpoint Security\\Threat Prevention``.

Windows resolves a DLL by base name, so whichever copy is loaded first wins for
the whole process. When a package that cgr imports before the embedder (in
practice ``pydantic_ai``, which the CLI loads while building its tool registry)
pulls in a C extension, that older runtime is the one the process gets. PyTorch
is built against a newer one: ``c10.dll`` then fails its initialisation
routine, and every later ``import torch`` in the process fails the same way:

    OSError: [WinError 1114] 动态链接库(DLL)初始化例程失败。
    Error loading "...\\torch\\lib\\c10.dll" or one of its dependencies.

The symptom is misleading -- the same ``import torch`` works from a fresh
interpreter, and only breaks inside ``cgr start`` -- so it is worth pinning
down here rather than debugging it twice.

Loading the System32 copy up front costs a few milliseconds and makes the
process bind the runtime PyTorch expects, no matter what else is imported
first.
"""

from __future__ import annotations

import sys

_preloaded = False

# c10.dll links against these; the first two are the ones endpoint agents
# shadow. vcruntime140_1.dll is the exception-handling half of the same
# runtime and is loaded by the interpreter already, so it is harmless to pin.
_RUNTIME_DLLS = ("msvcp140.dll", "vcruntime140_1.dll")


def preload_msvc_runtime() -> None:
    """Bind the System32 MSVC runtime, if it is not already the bound one."""
    global _preloaded
    if _preloaded or sys.platform != "win32":
        return
    _preloaded = True

    import ctypes
    import os

    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    for name in _RUNTIME_DLLS:
        try:
            ctypes.WinDLL(os.path.join(system32, name))
        except OSError:
            # A stripped-down Windows image may not carry the redistributable;
            # nothing to pin then, and torch will report what it is missing.
            pass
