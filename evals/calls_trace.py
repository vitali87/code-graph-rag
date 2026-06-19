import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from . import constants as ec

_SYNTHETIC_QUALNAME_MARKERS = (
    "<module>",
    "<lambda>",
    "<listcomp>",
    "<dictcomp>",
    "<setcomp>",
    "<genexpr>",
)
_LOCALS_SEGMENT = ".<locals>"


def _frame_qn(frame: FrameType, target: Path, project_name: str) -> str | None:
    code = frame.f_code
    try:
        file = Path(code.co_filename).resolve()
    except (OSError, ValueError):
        return None
    try:
        rel = file.relative_to(target)
    except ValueError:
        return None
    if not file.name.endswith(ec.PY_SUFFIX):
        return None

    qualname = code.co_qualname
    if any(marker in qualname for marker in _SYNTHETIC_QUALNAME_MARKERS):
        return None
    qualname = qualname.replace(_LOCALS_SEGMENT, "")

    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == ec.INIT_STEM:
        parts = parts[:-1]
    module_dotted = ec.SEP.join([project_name, *parts])
    return ec.SEP.join([module_dotted, qualname])


def trace_calls(
    workload: Callable[[], None], target: Path, project_name: str
) -> set[tuple[str, str]]:
    target = target.resolve()
    edges: set[tuple[str, str]] = set()

    def tracer(frame: FrameType, event: str, arg: object) -> None:
        if event != ec.TRACE_CALL_EVENT:
            return None
        caller = frame.f_back
        if caller is None:
            return None
        callee_qn = _frame_qn(frame, target, project_name)
        if callee_qn is None:
            return None
        caller_qn = _frame_qn(caller, target, project_name)
        if caller_qn is None or caller_qn == callee_qn:
            return None
        edges.add((caller_qn, callee_qn))
        return None

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        workload()
    finally:
        sys.settrace(previous)
    return edges
