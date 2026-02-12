from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag.decorators import (
    async_timing_decorator,
    ensure_loaded,
    log_operation,
    mcp_try_except,
    recursion_guard,
    timing_decorator,
    validate_project_path,
)


class TestEnsureLoaded:
    def test_calls_ensure_loaded_before_method(self) -> None:
        call_order: list[str] = []

        class MockLoader:
            def _ensure_loaded(self) -> None:
                call_order.append("ensure_loaded")

            @ensure_loaded
            def get_data(self) -> str:
                call_order.append("get_data")
                return "data"

        loader = MockLoader()
        result = loader.get_data()

        assert result == "data"
        assert call_order == ["ensure_loaded", "get_data"]

    def test_works_with_property(self) -> None:
        class MockLoader:
            def __init__(self) -> None:
                self._loaded = False
                self._value = "test"

            def _ensure_loaded(self) -> None:
                self._loaded = True

            @property
            @ensure_loaded
            def value(self) -> str:
                return self._value

        loader = MockLoader()
        assert not loader._loaded
        result = loader.value
        assert result == "test"
        assert loader._loaded

    def test_preserves_function_metadata(self) -> None:
        class MockLoader:
            def _ensure_loaded(self) -> None:
                pass

            @ensure_loaded
            def my_method(self) -> None:
                pass

        assert MockLoader.my_method.__name__ == "my_method"


class TestTimingDecorator:
    def test_returns_correct_result(self) -> None:
        @timing_decorator
        def add(a: int, b: int) -> int:
            return a + b

        result = add(2, 3)
        assert result == 5

    def test_logs_timing_info(self) -> None:
        with patch("codebase_rag.decorators.logger") as mock_logger:

            @timing_decorator
            def fast_func() -> str:
                return "done"

            result = fast_func()

            assert result == "done"
            mock_logger.info.assert_called_once()
            log_message = mock_logger.info.call_args[0][0]
            assert "fast_func" in log_message
            assert "ms" in log_message

    def test_handles_exceptions(self) -> None:
        with patch("codebase_rag.decorators.logger"):

            @timing_decorator
            def failing_func() -> None:
                raise ValueError("test error")

            with pytest.raises(ValueError, match="test error"):
                failing_func()

    def test_preserves_function_metadata(self) -> None:
        @timing_decorator
        def named_function() -> None:
            pass

        assert named_function.__name__ == "named_function"


class TestAsyncTimingDecorator:
    def test_returns_correct_result(self) -> None:
        @async_timing_decorator
        async def async_add(a: int, b: int) -> int:
            return a + b

        result = asyncio.run(async_add(2, 3))
        assert result == 5

    def test_logs_timing_info(self) -> None:
        with patch("codebase_rag.decorators.logger") as mock_logger:

            @async_timing_decorator
            async def async_func() -> str:
                return "done"

            result = asyncio.run(async_func())

            assert result == "done"
            mock_logger.info.assert_called_once()
            log_message = mock_logger.info.call_args[0][0]
            assert "async_func" in log_message

    def test_handles_exceptions(self) -> None:
        with patch("codebase_rag.decorators.logger"):

            @async_timing_decorator
            async def async_failing() -> None:
                raise ValueError("async error")

            with pytest.raises(ValueError, match="async error"):
                asyncio.run(async_failing())

    def test_preserves_function_metadata(self) -> None:
        @async_timing_decorator
        async def named_async_function() -> None:
            pass

        assert named_async_function.__name__ == "named_async_function"


class TestValidateProjectPath:
    def test_allows_valid_path_within_project(self) -> None:
        class ResultType:
            def __init__(
                self, file_path: str, error_message: str | None = None
            ) -> None:
                self.file_path = file_path
                self.error_message = error_message

        class MockService:
            project_root = Path("/project")
            allowed_roots: frozenset[Path] | None = None

            @validate_project_path(ResultType, "file_path")
            async def read(self, file_path: Path) -> ResultType:
                return ResultType(file_path=str(file_path))

        service = MockService()
        with patch.object(Path, "resolve", return_value=Path("/project/src/file.py")):
            result = asyncio.run(service.read(file_path="src/file.py"))

        assert result.error_message is None

    def test_rejects_path_outside_project(self) -> None:
        class ResultType:
            def __init__(
                self, file_path: str, error_message: str | None = None
            ) -> None:
                self.file_path = file_path
                self.error_message = error_message

        class MockService:
            project_root = Path("/project")
            allowed_roots: frozenset[Path] | None = None

            @validate_project_path(ResultType, "file_path")
            async def read(self, file_path: Path) -> ResultType:
                return ResultType(file_path=str(file_path))

        service = MockService()
        result = asyncio.run(service.read(file_path="../etc/passwd"))

        assert result.error_message is not None
        assert "outside" in result.error_message.lower()

    def test_rejects_non_string_path(self) -> None:
        class ResultType:
            def __init__(
                self, file_path: str, error_message: str | None = None
            ) -> None:
                self.file_path = file_path
                self.error_message = error_message

        class MockService:
            project_root = Path("/project")
            allowed_roots: frozenset[Path] | None = None

            @validate_project_path(ResultType, "file_path")
            async def read(self, file_path: Path) -> ResultType:
                return ResultType(file_path=str(file_path))

        service = MockService()
        result = asyncio.run(service.read(file_path=123))  # type: ignore[arg-type]

        assert result.error_message is not None

    def test_handles_path_not_first_positional_arg(self) -> None:
        class ResultType:
            def __init__(
                self, file_path: str, error_message: str | None = None
            ) -> None:
                self.file_path = file_path
                self.error_message = error_message

        class MockService:
            project_root = Path("/project")
            allowed_roots: frozenset[Path] | None = None

            @validate_project_path(ResultType, "file_path")
            async def save(self, content: str, file_path: Path) -> ResultType:
                return ResultType(file_path=str(file_path))

        service = MockService()
        with patch.object(Path, "resolve", return_value=Path("/project/test.txt")):
            result = asyncio.run(service.save("my content", "test.txt"))

        assert result.error_message is None
        assert "test.txt" in result.file_path


class TestRecursionGuard:
    def test_prevents_recursive_calls(self) -> None:
        call_count = 0

        class Analyzer:
            @recursion_guard(key_func=lambda self, key: key)
            def analyze(self, key: str) -> str | None:
                nonlocal call_count
                call_count += 1
                if call_count < 5:
                    return self.analyze(key)
                return "done"

        analyzer = Analyzer()
        result = analyzer.analyze("test_key")

        assert result is None
        assert call_count == 1

    def test_allows_different_keys(self) -> None:
        calls: list[str] = []

        class Analyzer:
            @recursion_guard(key_func=lambda self, key: key)
            def analyze(self, key: str) -> str | None:
                calls.append(key)
                return f"result_{key}"

        analyzer = Analyzer()
        r1 = analyzer.analyze("key1")
        r2 = analyzer.analyze("key2")

        assert r1 == "result_key1"
        assert r2 == "result_key2"
        assert calls == ["key1", "key2"]

    def test_clears_guard_after_completion(self) -> None:
        call_count = 0

        class Analyzer:
            @recursion_guard(key_func=lambda self, key: key)
            def analyze(self, key: str) -> str | None:
                nonlocal call_count
                call_count += 1
                return "done"

        analyzer = Analyzer()
        analyzer.analyze("key1")
        analyzer.analyze("key1")

        assert call_count == 2

    def test_clears_guard_on_exception(self) -> None:
        call_count = 0

        class Analyzer:
            @recursion_guard(key_func=lambda self, key: key)
            def analyze(self, key: str) -> str | None:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise ValueError("error")
                return "done"

        analyzer = Analyzer()
        with pytest.raises(ValueError):
            analyzer.analyze("key1")

        result = analyzer.analyze("key1")
        assert result == "done"
        assert call_count == 2

    def test_shared_guard_name(self) -> None:
        calls: list[str] = []

        class Analyzer:
            @recursion_guard(key_func=lambda self, key: key, guard_name="shared")
            def method_a(self, key: str) -> str | None:
                calls.append(f"a:{key}")
                return self.method_b(key)

            @recursion_guard(key_func=lambda self, key: key, guard_name="shared")
            def method_b(self, key: str) -> str | None:
                calls.append(f"b:{key}")
                return self.method_a(key)

        analyzer = Analyzer()
        result = analyzer.method_a("test")

        assert result is None
        assert calls == ["a:test"]

    def test_separate_guard_names(self) -> None:
        calls: list[str] = []

        class Analyzer:
            @recursion_guard(key_func=lambda self, key: key, guard_name="guard_a")
            def method_a(self, key: str) -> str | None:
                calls.append(f"a:{key}")
                return self.method_b(key)

            @recursion_guard(key_func=lambda self, key: key, guard_name="guard_b")
            def method_b(self, key: str) -> str | None:
                calls.append(f"b:{key}")
                return self.method_a(key)

        analyzer = Analyzer()
        result = analyzer.method_a("test")

        assert result is None
        assert calls == ["a:test", "b:test"]

    def test_handles_keyword_arguments_in_guarded_function(self) -> None:
        calls: list[tuple[str, str | None]] = []

        class Analyzer:
            @recursion_guard(
                key_func=lambda self,
                method_call,
                module_qn,
                *_,
                **__: f"{module_qn}:{method_call}"
            )
            def infer_type(
                self,
                method_call: str,
                module_qn: str,
                local_var_types: dict[str, str] | None = None,
            ) -> str | None:
                calls.append((method_call, module_qn))
                return f"type_{method_call}"

        analyzer = Analyzer()

        result1 = analyzer.infer_type("method1", "module1", None)
        result2 = analyzer.infer_type("method2", "module2", local_var_types=None)
        result3 = analyzer.infer_type(
            "method3", "module3", local_var_types={"x": "int"}
        )

        assert result1 == "type_method1"
        assert result2 == "type_method2"
        assert result3 == "type_method3"
        assert len(calls) == 3

    def test_key_func_receives_kwargs_correctly(self) -> None:
        received_kwargs: list[dict[str, str | None]] = []

        def key_func(self, a: str, b: str, **kwargs) -> str:
            received_kwargs.append(dict(kwargs))
            return f"{a}:{b}"

        class Analyzer:
            @recursion_guard(key_func=key_func)
            def process(
                self, a: str, b: str, optional: str | None = None
            ) -> str | None:
                return "done"

        analyzer = Analyzer()
        analyzer.process("x", "y", optional="z")

        assert len(received_kwargs) == 1
        assert received_kwargs[0] == {"optional": "z"}

    def test_recursion_guard_with_mixed_positional_and_keyword_args(self) -> None:
        call_count = 0

        class TypeInference:
            @recursion_guard(key_func=lambda self, call, mod, *_, **__: f"{mod}:{call}")
            def infer(
                self,
                method_call: str,
                module: str,
                vars: dict[str, str] | None = None,
            ) -> str | None:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return self.infer(method_call, module, vars=vars)
                return "resolved"

        inf = TypeInference()
        result = inf.infer("foo.bar", "mymod", vars={"x": "int"})

        assert result is None
        assert call_count == 1


class TestLogOperation:
    def test_logs_start_and_end_messages(self) -> None:
        with patch("codebase_rag.decorators.logger") as mock_logger:

            @log_operation("Starting...", "Finished!")
            def operation() -> str:
                return "result"

            result = operation()

            assert result == "result"
            assert mock_logger.info.call_count == 2
            calls = [call[0][0] for call in mock_logger.info.call_args_list]
            assert calls == ["Starting...", "Finished!"]

    def test_logs_end_even_on_success(self) -> None:
        with patch("codebase_rag.decorators.logger") as mock_logger:

            @log_operation("Start", "End")
            def success_op() -> int:
                return 42

            result = success_op()

            assert result == 42
            assert mock_logger.info.call_count == 2

    def test_preserves_function_metadata(self) -> None:
        @log_operation("Start", "End")
        def named_op() -> None:
            pass

        assert named_op.__name__ == "named_op"


class TestMcpTryExcept:
    def test_returns_result_on_success(self) -> None:
        @mcp_try_except(lambda e: f"error: {e}")
        async def successful_handler() -> str:
            return "success"

        result = asyncio.run(successful_handler())
        assert result == "success"

    def test_returns_error_on_exception(self) -> None:
        @mcp_try_except(lambda e: f"error: {e}")
        async def failing_handler() -> str:
            raise ValueError("something went wrong")

        result = asyncio.run(failing_handler())
        assert result == "error: something went wrong"

    def test_works_with_dict_error_factory(self) -> None:
        def error_factory(msg: str) -> dict[str, str]:
            return {"error": msg, "status": "failed"}

        @mcp_try_except(error_factory)
        async def handler() -> dict[str, str]:
            raise RuntimeError("db error")

        result = asyncio.run(handler())
        assert result == {"error": "db error", "status": "failed"}

    def test_preserves_function_metadata(self) -> None:
        @mcp_try_except(lambda e: e)
        async def named_handler() -> str:
            return "ok"

        assert named_handler.__name__ == "named_handler"

    def test_passes_arguments_correctly(self) -> None:
        @mcp_try_except(lambda e: f"error: {e}")
        async def handler_with_args(a: int, b: str) -> str:
            return f"{a}-{b}"

        result = asyncio.run(handler_with_args(1, "test"))
        assert result == "1-test"

    def test_reraises_keyboard_interrupt(self) -> None:
        @mcp_try_except(lambda e: f"error: {e}")
        async def handler_with_interrupt() -> str:
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            asyncio.run(handler_with_interrupt())

    def test_reraises_system_exit(self) -> None:
        @mcp_try_except(lambda e: f"error: {e}")
        async def handler_with_exit() -> str:
            raise SystemExit(1)

        with pytest.raises(SystemExit):
            asyncio.run(handler_with_exit())

    def test_reraises_cancelled_error(self) -> None:
        @mcp_try_except(lambda e: f"error: {e}")
        async def handler_with_cancel() -> str:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(handler_with_cancel())
