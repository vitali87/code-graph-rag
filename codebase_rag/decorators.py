import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Concatenate

from loguru import logger

from . import logs as ls
from . import tool_errors as te
from .types_defs import (
    LoadableProtocol,
    PathResultFactory,
    PathValidatorProtocol,
)
from .utils.path_utils import resolve_in_root


def ensure_loaded[S: LoadableProtocol, **P, T](
    func: Callable[Concatenate[S, P], T],
) -> Callable[Concatenate[S, P], T]:
    @wraps(func)
    def wrapper(self: S, *args: P.args, **kwargs: P.kwargs) -> T:
        self._ensure_loaded()
        return func(self, *args, **kwargs)

    return wrapper


def async_timing_decorator[**P, T](
    func: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T]]:
    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        start = time.perf_counter()
        try:
            return await func(*args, **kwargs)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            # `wraps` copied the qualified name onto the wrapper; a bare Callable
            # is not known to carry one.
            logger.info(ls.FUNC_TIMING.format(func=wrapper.__qualname__, time=elapsed))

    return wrapper


def validate_project_path[S: PathValidatorProtocol, **P, T](
    result_factory: PathResultFactory[T],
) -> Callable[
    [Callable[Concatenate[S, Path, P], Awaitable[T]]],
    Callable[Concatenate[S, str, P], Awaitable[T]],
]:
    """The method called with its first argument, the path a caller gave,
    resolved inside the project root; a path outside it is answered with
    `result_factory` instead. The caller passes a string, the method receives
    the resolved Path, and both signatures stay visible to the checker."""

    def decorator(
        func: Callable[Concatenate[S, Path, P], Awaitable[T]],
    ) -> Callable[Concatenate[S, str, P], Awaitable[T]]:
        @wraps(func)
        async def wrapper(
            self: S, file_path: str, *args: P.args, **kwargs: P.kwargs
        ) -> T:
            # A tool call's arguments arrive as JSON, so a number can get here.
            full_path = (
                resolve_in_root(self.project_root, file_path)
                if isinstance(file_path, str)
                else None
            )
            if full_path is None:
                return result_factory(
                    file_path=str(file_path),
                    error_message=te.PATH_OUTSIDE_ROOT.format(
                        path=file_path, root=self.project_root
                    ),
                )
            return await func(self, full_path, *args, **kwargs)

        return wrapper

    return decorator


_GUARD_REGISTRY: dict[str, ContextVar[set[str] | None]] = {}


def recursion_guard[**P, T](
    key_func: Callable[..., str],
    guard_name: str | None = None,
) -> Callable[[Callable[P, T | None]], Callable[P, T | None]]:
    if guard_name:
        context_var = _GUARD_REGISTRY.get(guard_name)
        if context_var is None:
            new_var = ContextVar[set[str] | None](guard_name, default=None)
            context_var = _GUARD_REGISTRY.setdefault(guard_name, new_var)
    else:
        name = getattr(key_func, "__name__", "guard")
        context_var = ContextVar[set[str] | None](name, default=None)

    def decorator(func: Callable[P, T | None]) -> Callable[P, T | None]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
            guard_set = context_var.get()
            if guard_set is None:
                guard_set = set()
                context_var.set(guard_set)

            key = key_func(*args, **kwargs)
            if key in guard_set:
                return None
            guard_set.add(key)
            try:
                return func(*args, **kwargs)
            finally:
                guard_set.discard(key)

        return wrapper

    return decorator


_DEPTH_REGISTRY: dict[str, ContextVar[int]] = {}


def depth_guard[**P, T](
    max_depth: int, guard_name: str
) -> Callable[[Callable[P, T | None]], Callable[P, T | None]]:
    """Answer None once `max_depth` guarded calls are already on the stack.

    Every function decorated with the same `guard_name` shares one counter, so
    mutually recursive functions are bounded together. It stops input-driven
    recursion (a long call chain, deeply nested arguments) well before
    Python's own limit, where a RecursionError would abort the whole caller.
    """
    context_var = _DEPTH_REGISTRY.get(guard_name)
    if context_var is None:
        context_var = _DEPTH_REGISTRY.setdefault(
            guard_name, ContextVar[int](guard_name, default=0)
        )

    def decorator(func: Callable[P, T | None]) -> Callable[P, T | None]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
            depth = context_var.get()
            if depth >= max_depth:
                return None
            token = context_var.set(depth + 1)
            try:
                return func(*args, **kwargs)
            finally:
                context_var.reset(token)

        return wrapper

    return decorator


def depth_exhausted(guard_name: str, max_depth: int) -> bool:
    """Whether a `depth_guard` with this name would refuse its next call.

    For a caller holding a fallback: once the guarded call was refused for
    depth, falling back would run the same unbounded work the guard exists
    to stop.
    """
    context_var = _DEPTH_REGISTRY.get(guard_name)
    return context_var is not None and context_var.get() >= max_depth
