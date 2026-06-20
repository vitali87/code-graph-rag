import inspect
import sys
from collections.abc import Callable
from pathlib import Path
from types import CodeType, FrameType

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

# (H) functools.wraps decorator wrappers: the inner function is named "wrapper" and
# (H) closes over the wrapped callable under one of these free-variable names. cgr
# (H) resolves a call to a decorated function as a call to the function itself (it sees
# (H) through the decorator), so the trace must attribute the generic wrapper frame to
# (H) the function it wraps; otherwise calls would be credited to the recycled wrapper
# (H) node. See evals/README.md ("Decorator-wrapper normalization").
_WRAPPER_CODE_NAME = "wrapper"
_WRAPPED_FREE_VARS = ("func", "fn", "wrapped", "method", "f")


def _code_qn(code: CodeType, target: Path, project_name: str) -> str | None:
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


def _wrapped_code(frame: FrameType) -> CodeType | None:
    # (H) Recover the wrapped function's code from a @wraps wrapper frame via its
    # (H) closed-over callable, following any __wrapped__ chain to the real function.
    code = frame.f_code
    if code.co_name != _WRAPPER_CODE_NAME:
        return None
    for name in _WRAPPED_FREE_VARS:
        if name not in code.co_freevars:
            continue
        candidate = frame.f_locals.get(name)
        if not callable(candidate):
            continue
        unwrapped = inspect.unwrap(candidate)
        wrapped_code = getattr(unwrapped, "__code__", None) or getattr(
            getattr(unwrapped, "__func__", None), "__code__", None
        )
        if isinstance(wrapped_code, CodeType):
            return wrapped_code
    return None


def _frame_qn(frame: FrameType, target: Path, project_name: str) -> str | None:
    if (wrapped := _wrapped_code(frame)) is not None and (
        qn := _code_qn(wrapped, target, project_name)
    ) is not None:
        return qn
    return _code_qn(frame.f_code, target, project_name)


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
