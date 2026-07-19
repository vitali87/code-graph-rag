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


@dataclass(frozen=True)
class ArgHandleSink:
    """A call-shaped handle sink: the resource handle arrives as an ARGUMENT
    (libc's `fprintf(f, ...)` / `fgets(buf, n, f)`), not as a receiver."""

    callee: str
    handle_arg: int
    direction: IODirection


@dataclass(frozen=True)
class HandleBinding:
    """A local variable bound to a resource handle within one function body."""

    kind: ResourceKind
    identity: str
