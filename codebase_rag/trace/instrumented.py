"""Convert C/C++ instrumented address traces to the interchange format.

The ``cgr_trace_shim.c`` agent (compiled into the target with
``-finstrument-functions``) records exact (caller, callee) function-address
pairs and writes them with the executable path and ASLR slide. This
converter symbolises those addresses into function names and source
positions — ``atos`` on macOS, ``addr2line`` elsewhere — and emits
interchange records. Counts are true invocation counts.

C++ names demangle to ``Dog::sound()``; identity for resolution is the
symbolised source position (span-first, like every native tracer here), so
names normalise to their bare member form. Frames that symbolise outside
the repository (libc, the C++ runtime) are glue: the shim already recorded
direct caller/callee pairs, so out-of-repo endpoints simply drop the edge.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from loguru import logger

from .. import constants as cs
from .records import (
    CallRecord,
    FramePoint,
    TraceFormatError,
    TraceHeader,
    write_trace_file,
)

if TYPE_CHECKING:
    from pathlib import Path

Symbolizer = Callable[[str, int, Sequence[int]], dict[int, tuple[str, str, int]]]


def _bare_name(symbol: str) -> str:
    """``Dog::sound(int)`` as the member name ``sound``."""
    head = symbol.split("(", 1)[0].strip()
    return head.rsplit("::", 1)[-1] or cs.TRACE_QUALNAME_ANONYMOUS


def _atos_symbolizer(
    exe: str, slide: int, addresses: Sequence[int]
) -> dict[int, tuple[str, str, int]]:
    """macOS symbolisation; atos output: ``name (in x) (file.c:12)``."""
    command = [
        "atos",
        "-fullPath",
        "-o",
        exe,
        "-s",
        hex(slide),
        *[hex(address) for address in addresses],
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    symbols: dict[int, tuple[str, str, int]] = {}
    lines = result.stdout.splitlines()
    for address, line in zip(addresses, lines, strict=False):
        name = line.split(" (in ", 1)[0].strip()
        path = ""
        line_no = 0
        if line.endswith(")") and "(" in line:
            position = line.rsplit("(", 1)[1].rstrip(")")
            file_part, separator, line_part = position.rpartition(":")
            if separator and line_part.isdigit():
                path = file_part
                line_no = int(line_part)
        symbols[address] = (name, path, line_no)
    return symbols


def _addr2line_symbolizer(
    exe: str, slide: int, addresses: Sequence[int]
) -> dict[int, tuple[str, str, int]]:
    """ELF symbolisation; addr2line -f -C prints name then file:line."""
    command = [
        "addr2line",
        "-e",
        exe,
        "-f",
        "-C",
        *[hex(address - slide) for address in addresses],
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines()
    symbols: dict[int, tuple[str, str, int]] = {}
    for index, address in enumerate(addresses):
        name = lines[index * 2] if index * 2 < len(lines) else ""
        position = lines[index * 2 + 1] if index * 2 + 1 < len(lines) else ""
        file_part, separator, line_part = position.rpartition(":")
        line_no = int(line_part) if separator and line_part.isdigit() else 0
        path = "" if file_part in ("", "??") else file_part
        symbols[address] = (name, path, line_no)
    return symbols


def _default_symbolizer() -> Symbolizer:
    if shutil.which("atos"):
        return _atos_symbolizer
    if shutil.which("addr2line"):
        return _addr2line_symbolizer
    raise TraceFormatError(cs.TRACE_ERR_NO_SYMBOLIZER)


def _parse_addrs(addrs_path: Path) -> tuple[str, int, list[tuple[int, int, int]]]:
    """The executable path, load slide, and (caller, callee, count) edges.

    Raises ``TraceFormatError`` for an unparseable trace or one the shim
    marked ``dropped`` (its edge table overflowed, so counts are incomplete).
    """
    exe = ""
    slide = 0
    dropped = False
    pairs: list[tuple[int, int, int]] = []
    for line in addrs_path.read_text(encoding="utf-8").splitlines():
        # Header keys are single tokens; the value is the rest of the line so
        # an executable path may contain spaces. Edge rows are three tokens.
        key, _sep, rest = line.partition(" ")
        if key == "exe":
            exe = rest
        elif key == "slide" and rest.lstrip("-").isdigit():
            slide = int(rest)
        elif key == "dropped":
            dropped = True
        else:
            parts = line.split()
            if len(parts) == 3:
                pairs.append((int(parts[0], 16), int(parts[1], 16), int(parts[2])))
    if not exe or not pairs:
        raise TraceFormatError(cs.TRACE_ERR_BAD_ADDRS.format(path=addrs_path))
    if dropped:
        # The shim's fixed table overflowed and lost caller/callee pairs, so
        # the exact invocation-count contract can no longer hold. Refuse the
        # trace rather than pass off an incomplete call graph as exact.
        raise TraceFormatError(cs.TRACE_ERR_ADDRS_DROPPED.format(path=addrs_path))
    return exe, slide, pairs


def convert_instrumented(
    addrs_path: Path,
    repo_root: Path,
    output: Path,
    workload: str | None = None,
    symbolizer: Symbolizer | None = None,
) -> int:
    """Write ``addrs_path``'s symbolised call edges to ``output``."""
    exe, slide, pairs = _parse_addrs(addrs_path)

    addresses = sorted({a for pair in pairs for a in pair[:2]})
    resolve = symbolizer or _default_symbolizer()
    symbols = resolve(exe, slide, addresses)

    root_prefix = repo_root.resolve().as_posix() + "/"
    # An address that symbolised to no usable source position (addr2line/atos
    # returned an empty/``??`` name, or a name but no file/line because debug
    # info was stripped) is genuinely unresolved, distinct from an address that
    # resolved to a real position in glue outside the repository; report the
    # former so a symbolisation gap is visible rather than silently dropped.
    unresolved: set[int] = set()

    def _frame(address: int) -> FramePoint | None:
        name, path, line = symbols.get(address, ("", "", 0))
        if not name or name == "??" or not path or path == "??" or line <= 0:
            unresolved.add(address)
            return None
        if not path.startswith(root_prefix):
            return None
        return FramePoint(path=path, qualname=_bare_name(name), line=line)

    edges: dict[tuple[FramePoint, FramePoint], int] = {}
    for caller_address, callee_address, count in pairs:
        caller = _frame(caller_address)
        callee = _frame(callee_address)
        if caller is None or callee is None:
            continue
        key = (caller, callee)
        edges[key] = edges.get(key, 0) + count

    if unresolved:
        logger.warning(
            cs.TRACE_MSG_ADDRS_UNRESOLVED.format(
                count=len(unresolved), total=len(addresses)
            )
        )

    workloads = (workload,) if workload else ()
    records = [
        CallRecord(
            caller=caller,
            callee=callee,
            count=count,
            workloads=workloads,
            receiver_types=(),
        )
        for (caller, callee), count in edges.items()
    ]
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=cs.TRACE_LANGUAGE_CPP,
        repo_root=str(repo_root),
        tracer=cs.TRACE_TOOL_NAME_INSTRUMENTED,
        sampled=False,
    )
    write_trace_file(output, header, records)
    return len(records)
