"""Work counted as the lines run inside chosen modules.

A bound on how a walk's work grows, read off a clock, fails on a busy or
traced runner without the walk doing any more work (#3798). The lines run
inside the modules that hold the walk are the same on every run: counted
with `sys.monitoring` on those modules' code objects only, so the rest of
the process runs unmonitored, and under a tool id of their own, so
coverage's own tracing is untouched.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Generator
from contextlib import contextmanager
from types import CodeType, FunctionType, ModuleType

import pytest

_MONITOR_NAME = "cgr-work-counter"


class LineCounter:
    def __init__(self) -> None:
        self.lines = 0

    def on_line(self, _code: CodeType, _line: int) -> None:
        self.lines += 1


def _module_code_objects(
    modules: tuple[ModuleType, ...], functions: frozenset[str] | None = None
) -> list[CodeType]:
    """Every code object defined in `modules`, nested and decorated ones too.

    Read off the modules' own namespaces (functions, and the methods of the
    classes they define) rather than `gc.get_objects()`, which would touch
    every live object, lazy library proxies included. With `functions`, only
    those named functions and the code nested in them.
    """
    files = {module.__file__ for module in modules}
    pending: list[CodeType] = []
    for module in modules:
        for value in vars(module).values():
            members = vars(value).values() if isinstance(value, type) else [value]
            for member in members:
                if isinstance(member, (staticmethod, classmethod)):
                    member = member.__func__
                if isinstance(member, property):
                    accessors = [member.fget, member.fset]
                else:
                    accessors = [inspect.unwrap(member)] if callable(member) else []
                pending.extend(
                    accessor.__code__
                    for accessor in accessors
                    if isinstance(accessor, FunctionType)
                )
    if functions is not None:
        pending = [code for code in pending if code.co_name in functions]
    seen: set[CodeType] = set()
    while pending:
        code = pending.pop()
        if code in seen or code.co_filename not in files:
            continue
        seen.add(code)
        pending.extend(c for c in code.co_consts if isinstance(c, CodeType))
    return list(seen)


def _free_tool_id() -> int:
    for tool in range(2, 6):
        if sys.monitoring.get_tool(tool) is None:
            return tool
    pytest.skip("no free sys.monitoring tool id")


@contextmanager
def lines_executed_in(
    *modules: ModuleType, functions: frozenset[str] | None = None
) -> Generator[LineCounter]:
    """Count the lines run inside `modules` while the block executes.

    Local events on those code objects only, so the rest of the process runs
    unmonitored; a dedicated tool id, so coverage's own tracing is untouched.
    """
    monitoring = sys.monitoring
    tool = _free_tool_id()
    codes = _module_code_objects(modules, functions)
    counter = LineCounter()
    monitoring.use_tool_id(tool, _MONITOR_NAME)
    monitoring.register_callback(tool, monitoring.events.LINE, counter.on_line)
    for code in codes:
        monitoring.set_local_events(tool, code, monitoring.events.LINE)
    try:
        yield counter
    finally:
        for code in codes:
            monitoring.set_local_events(tool, code, monitoring.events.NO_EVENTS)
        monitoring.register_callback(tool, monitoring.events.LINE, None)
        monitoring.free_tool_id(tool)
