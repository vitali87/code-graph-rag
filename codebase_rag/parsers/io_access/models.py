from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    MODE_READ_CHAR,
    MODE_UPDATE_CHAR,
    MODE_WRITE_CHARS,
    IODirection,
    ResourceKind,
)


@dataclass(frozen=True)
class IOSink:
    """A call whose invocation reads from or writes to an I/O resource."""

    callee: str
    kind: ResourceKind
    direction: IODirection
    target_arg: int | None = None
    mode_arg: int | None = None
    target_kw: str | None = None
    mode_kw: str | None = None
    # fetch-style calls carry the HTTP verb in an options object at this
    # positional index ({method: 'POST'}); the verb overrides `direction`.
    method_options_arg: int | None = None

    def effective_direction(self, mode_literal: str | None) -> IODirection:
        if self.mode_arg is None or mode_literal is None:
            return self.direction
        if MODE_UPDATE_CHAR in mode_literal:
            return IODirection.READ_WRITE
        if any(c in mode_literal for c in MODE_WRITE_CHARS):
            return IODirection.WRITE
        if MODE_READ_CHAR in mode_literal:
            return IODirection.READ
        return self.direction


@dataclass(frozen=True)
class HandleConstructor:
    """A call whose return value is a resource handle (file, connection, ...)."""

    callee: str
    kind: ResourceKind
    target_arg: int | None = None
    target_kw: str | None = None
    # Positional arg index of an ALREADY-BOUND handle that supplies this handle's
    # identity (C# `new SqlCommand(sql, conn)` inherits conn's DB identity from
    # arg1). None where the identity is a literal target_arg.
    handle_arg: int | None = None
    # Access capability of the constructed handle. The lean write-flow walk
    # (issue #1204) suppresses write emission for a READ-only handle: `os.Open` is
    # read-only, so `f.Write` on it is not a real sink. Defaults to READ_WRITE so
    # io_access -- which never inspects this field -- is unchanged, and a
    # flag-dependent ctor (`os.OpenFile`) stays a sound may-write.
    direction: IODirection = IODirection.READ_WRITE


@dataclass(frozen=True)
class ArgHandleSink:
    """A call-shaped handle sink: the resource handle arrives as an ARGUMENT
    (libc's `fprintf(f, ...)` / `fgets(buf, n, f)`), not as a receiver."""

    callee: str
    handle_arg: int
    direction: IODirection
    # Argument indices that carry the DATA payload written to / read from the
    # handle. None means "every non-handle argument" (fprintf's format + varargs are
    # all payload). An explicit tuple pins the payload position so control metadata is
    # not mistaken for data: `fwrite(buffer, size, count, stream)` writes only arg 0,
    # so a tainted `size`/`count` is not a leak (issue #1204, flow walk only; io_access
    # ignores this field). Used by the taint-tracking FLOWS_TO walk.
    data_args: tuple[int, ...] | None = None


@dataclass(frozen=True)
class HandleBinding:
    """A local variable bound to a resource handle within one function body."""

    kind: ResourceKind
    identity: str
