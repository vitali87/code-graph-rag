import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from functools import wraps

from loguru import logger

from . import exceptions as ex
from . import logs as ls
from .types_defs import (
    LoadableProtocol,
    PathResultFactory,
    PathValidatorProtocol,
)


def ensure_loaded[T](func: Callable[..., T]) -> Callable[..., T]:
    @wraps(func)
    def wrapper(self: LoadableProtocol, *args, **kwargs) -> T:
        self._ensure_loaded()
        return func(self, *args, **kwargs)

    return wrapper


def timing_decorator[**P, T](func: Callable[P, T]) -> Callable[P, T]:
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            # `wraps` copied the qualified name onto the wrapper; a bare Callable
            # is not known to carry one.
            logger.info(ls.FUNC_TIMING.format(func=wrapper.__qualname__, time=elapsed))

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


def validate_project_path[T](
    result_factory: PathResultFactory[T],
    path_arg_name: str,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        sig = inspect.signature(func)

        @wraps(func)
        async def wrapper(self: PathValidatorProtocol, *args, **kwargs) -> T:
            bound = sig.bind(self, *args, **kwargs)
            bound.apply_defaults()
            file_path_str = bound.arguments.get(path_arg_name)

            if not isinstance(file_path_str, str):
                return result_factory(
                    file_path=str(file_path_str), error_message=ex.ACCESS_DENIED
                )
            try:
                full_path = (self.project_root / file_path_str).resolve()
                project_root = self.project_root.resolve()
                full_path.relative_to(project_root)
            except (ValueError, RuntimeError):
                return result_factory(
                    file_path=file_path_str,
                    error_message=ls.FILE_OUTSIDE_ROOT.format(action="access"),
                )

            bound.arguments[path_arg_name] = full_path
            return await func(*bound.args, **bound.kwargs)

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


def log_operation[T](
    start_msg: str,
    end_msg: str,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            logger.info(start_msg)
            result = func(*args, **kwargs)
            logger.info(end_msg)
            return result

        return wrapper

    return decorator


def mcp_try_except[T](
    error_factory: Callable[[str], T],
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            try:
                return await func(*args, **kwargs)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                return error_factory(str(e))

        return wrapper

    return decorator
