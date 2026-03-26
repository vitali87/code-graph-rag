import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers import stdlib_extractor as se
from codebase_rag.parsers.stdlib_extractor import StdlibExtractor


@pytest.fixture(autouse=True)
def reset_caches() -> None:
    se._STDLIB_CACHE.clear()
    se._CACHE_TIMESTAMPS.clear()
    se._EXTERNAL_TOOLS.clear()


class TestCacheHelpers:
    def test_cache_stdlib_result_creates_entry(self) -> None:
        se._cache_stdlib_result("python", "collections.Counter", "collections")

        assert "python:collections.Counter" in se._STDLIB_CACHE
        assert (
            se._STDLIB_CACHE["python:collections.Counter"]["collections.Counter"]
            == "collections"
        )
        assert "python:collections.Counter" in se._CACHE_TIMESTAMPS

    def test_get_cached_stdlib_result_returns_cached_value(self) -> None:
        se._cache_stdlib_result("python", "json.loads", "json")

        result = se._get_cached_stdlib_result("python", "json.loads")

        assert result == "json"

    def test_get_cached_stdlib_result_returns_none_for_missing(self) -> None:
        result = se._get_cached_stdlib_result("python", "nonexistent.module")

        assert result is None

    def test_cache_ttl_expiration(self) -> None:
        se._cache_stdlib_result("python", "os.path", "os")
        cache_key = "python:os.path"
        se._CACHE_TIMESTAMPS[cache_key] = time.time() - (cs.IMPORT_CACHE_TTL + 100)

        result = se._get_cached_stdlib_result("python", "os.path")

        assert result is None
        assert cache_key not in se._STDLIB_CACHE
        assert cache_key not in se._CACHE_TIMESTAMPS


class TestToolAvailability:
    def test_is_tool_available_caches_result(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result1 = se._is_tool_available("python")
            result2 = se._is_tool_available("python")

            assert result1 is True
            assert result2 is True
            assert mock_run.call_count == 1

    def test_is_tool_available_returns_false_on_file_not_found(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = se._is_tool_available("nonexistent_tool")

            assert result is False
            assert se._EXTERNAL_TOOLS["nonexistent_tool"] is False

    def test_is_tool_available_returns_false_on_timeout(self) -> None:
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 2)):
            result = se._is_tool_available("slow_tool")

            assert result is False


class TestCachePersistence:
    def test_save_and_load_persistent_cache(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / ".cache" / "codebase_rag"

        with patch.object(Path, "home", return_value=tmp_path):
            se._cache_stdlib_result("python", "pathlib.Path", "pathlib")
            se._cache_stdlib_result("javascript", "fs.readFile", "fs")

            se.save_persistent_cache()

            assert (cache_dir / "stdlib_cache.json").exists()

            se._STDLIB_CACHE.clear()
            se._CACHE_TIMESTAMPS.clear()

            se.load_persistent_cache()

            assert "python:pathlib.Path" in se._STDLIB_CACHE
            assert "javascript:fs.readFile" in se._STDLIB_CACHE

    def test_clear_stdlib_cache(self, tmp_path: Path) -> None:
        with patch.object(Path, "home", return_value=tmp_path):
            cache_dir = tmp_path / ".cache" / "codebase_rag"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = cache_dir / "stdlib_cache.json"
            cache_file.write_text(encoding="utf-8", data="{}")

            se._cache_stdlib_result("python", "test.module", "test")

            se.clear_stdlib_cache()

            assert len(se._STDLIB_CACHE) == 0
            assert len(se._CACHE_TIMESTAMPS) == 0
            assert not cache_file.exists()

    def test_flush_stdlib_cache_calls_save(self, tmp_path: Path) -> None:
        with patch.object(Path, "home", return_value=tmp_path):
            se._cache_stdlib_result("python", "test.func", "test")

            se.flush_stdlib_cache()

            cache_file = tmp_path / ".cache" / "codebase_rag" / "stdlib_cache.json"
            assert cache_file.exists()


class TestGetStdlibCacheStats:
    def test_returns_correct_stats(self) -> None:
        se._cache_stdlib_result("python", "json.loads", "json")
        se._cache_stdlib_result("python", "os.path", "os")
        se._cache_stdlib_result("javascript", "fs.readFile", "fs")
        se._EXTERNAL_TOOLS["node"] = True
        se._EXTERNAL_TOOLS["go"] = False

        stats = se.get_stdlib_cache_stats()

        assert stats["cache_entries"] == 3
        assert "python:json.loads" in stats["cache_languages"]
        assert stats["total_cached_results"] == 3
        assert stats["external_tools_checked"]["node"] is True
        assert stats["external_tools_checked"]["go"] is False


class TestStdlibExtractorExtractModulePath:
    @pytest.fixture
    def extractor(self) -> StdlibExtractor:
        return StdlibExtractor(function_registry=None)

    @pytest.fixture
    def extractor_with_registry(self) -> StdlibExtractor:
        registry = {
            "myproject.models.User": "Class",
            "myproject.utils.helper": "Function",
            "myproject.services.api.get": "Method",
        }
        return StdlibExtractor(function_registry=registry)

    def test_returns_module_for_registered_class(
        self, extractor_with_registry: StdlibExtractor
    ) -> None:
        result = extractor_with_registry.extract_module_path(
            "myproject.models.User", cs.SupportedLanguage.PYTHON
        )

        assert result == "myproject.models"

    def test_returns_module_for_registered_function(
        self, extractor_with_registry: StdlibExtractor
    ) -> None:
        result = extractor_with_registry.extract_module_path(
            "myproject.utils.helper", cs.SupportedLanguage.PYTHON
        )

        assert result == "myproject.utils"

    def test_python_stdlib_uppercase_entity(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path(
            "collections.Counter", cs.SupportedLanguage.PYTHON
        )

        assert result == "collections"

    def test_python_stdlib_lowercase_entity(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path(
            "json.loads", cs.SupportedLanguage.PYTHON
        )

        assert result == "json"

    def test_python_single_part_returns_unchanged(
        self, extractor: StdlibExtractor
    ) -> None:
        result = extractor.extract_module_path("os", cs.SupportedLanguage.PYTHON)

        assert result == "os"

    def test_rust_stdlib_uppercase_entity(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path(
            "std::collections::HashMap", cs.SupportedLanguage.RUST
        )

        assert result == "std::collections"

    def test_rust_stdlib_all_uppercase(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path(
            "std::sync::ONCE_INIT", cs.SupportedLanguage.RUST
        )

        assert result == "std::sync"

    def test_rust_single_part_returns_unchanged(
        self, extractor: StdlibExtractor
    ) -> None:
        result = extractor.extract_module_path("std", cs.SupportedLanguage.RUST)

        assert result == "std"

    def test_go_uppercase_entity(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path("fmt/Println", cs.SupportedLanguage.GO)

        assert result == "fmt"

    def test_go_single_part_returns_unchanged(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path("fmt", cs.SupportedLanguage.GO)

        assert result == "fmt"

    def test_cpp_std_namespace_entity(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path("std::vector", cs.SupportedLanguage.CPP)

        assert result == "std"

    def test_cpp_non_std_returns_unchanged(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path(
            "mylib::MyClass", cs.SupportedLanguage.CPP
        )

        assert result == "mylib::MyClass"

    def test_java_uppercase_class(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path(
            "java.util.ArrayList", cs.SupportedLanguage.JAVA
        )

        assert result == "java.util"

    def test_java_exception_suffix(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path(
            "java.io.IOException", cs.SupportedLanguage.JAVA
        )

        assert result == "java.io"

    def test_lua_uppercase_entity(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path("string.Upper", cs.SupportedLanguage.LUA)

        assert result == "string"

    def test_lua_stdlib_module_uppercase(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path("math.PI", cs.SupportedLanguage.LUA)

        assert result == "math"

    def test_lua_entity_in_stdlib_set(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path("foo.string", cs.SupportedLanguage.LUA)

        assert result == "foo"

    def test_scala_uppercase_entity(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path(
            "module.ClassName", cs.SupportedLanguage.SCALA
        )

        assert result == "module"

    def test_scala_lowercase_returns_unchanged(
        self, extractor: StdlibExtractor
    ) -> None:
        result = extractor.extract_module_path(
            "module.function", cs.SupportedLanguage.SCALA
        )

        assert result == "module.function"


class TestStdlibExtractorWithMockedSubprocesses:
    @pytest.fixture
    def extractor(self) -> StdlibExtractor:
        return StdlibExtractor(function_registry=None)

    def test_js_stdlib_uppercase_entity_without_node(
        self, extractor: StdlibExtractor
    ) -> None:
        with patch.object(se, "_is_tool_available", return_value=False):
            result = extractor.extract_module_path(
                "fs.ReadStream", cs.SupportedLanguage.JS
            )

            assert result == "fs"

    def test_js_stdlib_lowercase_entity_without_node(
        self, extractor: StdlibExtractor
    ) -> None:
        with patch.object(se, "_is_tool_available", return_value=False):
            result = extractor.extract_module_path(
                "fs.readFile", cs.SupportedLanguage.JS
            )

            assert result == "fs"

    def test_ts_uses_js_extraction_uppercase(self, extractor: StdlibExtractor) -> None:
        with patch.object(se, "_is_tool_available", return_value=False):
            result = extractor.extract_module_path("path.Path", cs.SupportedLanguage.TS)

            assert result == "path"

    def test_ts_lowercase_strips_entity(self, extractor: StdlibExtractor) -> None:
        with patch.object(se, "_is_tool_available", return_value=False):
            result = extractor.extract_module_path("path.join", cs.SupportedLanguage.TS)

            assert result == "path"


class TestEdgeCases:
    @pytest.fixture
    def extractor(self) -> StdlibExtractor:
        return StdlibExtractor(function_registry=None)

    def test_empty_string(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path("", cs.SupportedLanguage.PYTHON)

        assert result == ""

    def test_single_part_path(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path("os", cs.SupportedLanguage.PYTHON)

        assert result == "os"

    def test_deeply_nested_path(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path(
            "a.b.c.d.e.ClassName", cs.SupportedLanguage.PYTHON
        )

        assert result == "a.b.c.d.e"

    def test_rust_deeply_nested(self, extractor: StdlibExtractor) -> None:
        result = extractor.extract_module_path(
            "std::collections::hash_map::HashMap", cs.SupportedLanguage.RUST
        )

        assert result == "std::collections::hash_map"

    def test_function_registry_none_handling(self) -> None:
        extractor = StdlibExtractor(function_registry=None)

        result = extractor.extract_module_path(
            "module.Class", cs.SupportedLanguage.PYTHON
        )

        assert result == "module"

    def test_function_registry_entity_not_found(self) -> None:
        registry = {"other.Module": "Class"}
        extractor = StdlibExtractor(function_registry=registry)

        result = extractor.extract_module_path(
            "module.Class", cs.SupportedLanguage.PYTHON
        )

        assert result == "module"


class TestCachePersistenceErrorHandling:
    def test_load_persistent_cache_handles_json_decode_error(
        self, tmp_path: Path
    ) -> None:
        with patch.object(Path, "home", return_value=tmp_path):
            cache_dir = tmp_path / ".cache" / "codebase_rag"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = cache_dir / "stdlib_cache.json"
            cache_file.write_text(encoding="utf-8", data="invalid json {{{")

            se._cache_stdlib_result("python", "existing.module", "existing")

            se.load_persistent_cache()

            assert "python:existing.module" in se._STDLIB_CACHE

    def test_load_persistent_cache_handles_missing_file(self, tmp_path: Path) -> None:
        with patch.object(Path, "home", return_value=tmp_path):
            se.load_persistent_cache()

            assert len(se._STDLIB_CACHE) == 0

    def test_save_persistent_cache_handles_os_error(self, tmp_path: Path) -> None:
        with patch.object(Path, "home", return_value=tmp_path):
            with patch.object(Path, "mkdir", side_effect=OSError("Permission denied")):
                se._cache_stdlib_result("python", "test.func", "test")

                se.save_persistent_cache()

    def test_clear_stdlib_cache_handles_unlink_error(self, tmp_path: Path) -> None:
        with patch.object(Path, "home", return_value=tmp_path):
            cache_dir = tmp_path / ".cache" / "codebase_rag"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = cache_dir / "stdlib_cache.json"
            cache_file.write_text(encoding="utf-8", data="{}")

            se._cache_stdlib_result("python", "test.module", "test")

            with patch.object(Path, "unlink", side_effect=OSError("Permission denied")):
                se.clear_stdlib_cache()

            assert len(se._STDLIB_CACHE) == 0


class TestGoExtractorWithMockedSubprocess:
    @pytest.fixture
    def extractor(self) -> StdlibExtractor:
        return StdlibExtractor(function_registry=None)

    def test_go_extractor_returns_package_on_successful_introspection(
        self, extractor: StdlibExtractor
    ) -> None:
        mock_list_result = MagicMock()
        mock_list_result.returncode = 0
        mock_list_result.stdout = "/usr/local/go/src/fmt"

        mock_popen = MagicMock()
        mock_popen.__enter__ = MagicMock(return_value=mock_popen)
        mock_popen.__exit__ = MagicMock(return_value=False)
        mock_popen.returncode = 0
        mock_popen.communicate.return_value = (
            '{"hasEntity": true, "entityType": "function"}',
            "",
        )

        with (
            patch("subprocess.run", return_value=mock_list_result),
            patch("subprocess.Popen", return_value=mock_popen),
        ):
            result = extractor.extract_module_path(
                "fmt/Println", cs.SupportedLanguage.GO
            )

            assert result == "fmt"

    def test_go_extractor_fallback_on_go_list_failure(
        self, extractor: StdlibExtractor
    ) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            result = extractor.extract_module_path(
                "fmt/Println", cs.SupportedLanguage.GO
            )

            assert result == "fmt"

    def test_go_extractor_fallback_on_timeout(self, extractor: StdlibExtractor) -> None:
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("go", 5)):
            result = extractor.extract_module_path(
                "fmt/Println", cs.SupportedLanguage.GO
            )

            assert result == "fmt"

    def test_go_extractor_lowercase_entity_returns_unchanged(
        self, extractor: StdlibExtractor
    ) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("subprocess.run", return_value=mock_result):
            result = extractor.extract_module_path(
                "fmt/lowercase", cs.SupportedLanguage.GO
            )

            assert result == "fmt/lowercase"


class TestJavaExtractorWithMockedSubprocess:
    @pytest.fixture
    def extractor(self) -> StdlibExtractor:
        return StdlibExtractor(function_registry=None)

    def test_java_extractor_fallback_on_compile_failure(
        self, extractor: StdlibExtractor
    ) -> None:
        mock_compile = MagicMock()
        mock_compile.returncode = 1

        with patch("subprocess.run", return_value=mock_compile):
            result = extractor.extract_module_path(
                "java.util.ArrayList", cs.SupportedLanguage.JAVA
            )

            assert result == "java.util"

    def test_java_extractor_fallback_on_timeout(
        self, extractor: StdlibExtractor
    ) -> None:
        import subprocess

        with patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("javac", 10)
        ):
            result = extractor.extract_module_path(
                "java.util.HashMap", cs.SupportedLanguage.JAVA
            )

            assert result == "java.util"

    def test_java_extractor_fallback_on_file_not_found(
        self, extractor: StdlibExtractor
    ) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = extractor.extract_module_path(
                "java.lang.String", cs.SupportedLanguage.JAVA
            )

            assert result == "java.lang"

    def test_java_extractor_builder_suffix(self, extractor: StdlibExtractor) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = extractor.extract_module_path(
                "java.lang.StringBuilder", cs.SupportedLanguage.JAVA
            )

            assert result == "java.lang"

    def test_java_extractor_error_suffix(self, extractor: StdlibExtractor) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = extractor.extract_module_path(
                "java.lang.OutOfMemoryError", cs.SupportedLanguage.JAVA
            )

            assert result == "java.lang"


class TestLuaExtractorWithMockedSubprocess:
    @pytest.fixture
    def extractor(self) -> StdlibExtractor:
        return StdlibExtractor(function_registry=None)

    def test_lua_extractor_returns_module_on_successful_introspection(
        self, extractor: StdlibExtractor
    ) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "hasEntity=true"

        with patch("subprocess.run", return_value=mock_result):
            result = extractor.extract_module_path(
                "string.upper", cs.SupportedLanguage.LUA
            )

            assert result == "string"

    def test_lua_extractor_fallback_on_entity_not_found(
        self, extractor: StdlibExtractor
    ) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "hasEntity=false"

        with patch("subprocess.run", return_value=mock_result):
            result = extractor.extract_module_path(
                "string.nonexistent", cs.SupportedLanguage.LUA
            )

            assert result == "string.nonexistent"

    def test_lua_extractor_fallback_on_timeout(
        self, extractor: StdlibExtractor
    ) -> None:
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("lua", 5)):
            result = extractor.extract_module_path(
                "math.floor", cs.SupportedLanguage.LUA
            )

            assert result == "math.floor"

    def test_lua_extractor_fallback_on_lua_not_found(
        self, extractor: StdlibExtractor
    ) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = extractor.extract_module_path(
                "table.insert", cs.SupportedLanguage.LUA
            )

            assert result == "table.insert"

    def test_lua_extractor_stdlib_module_in_set(
        self, extractor: StdlibExtractor
    ) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = extractor.extract_module_path(
                "custom.string", cs.SupportedLanguage.LUA
            )

            assert result == "custom"


class TestPythonExtractorEdgeCases:
    @pytest.fixture
    def extractor(self) -> StdlibExtractor:
        return StdlibExtractor(function_registry=None)

    def test_python_lowercase_entity_with_import_failure(
        self, extractor: StdlibExtractor
    ) -> None:
        with patch("importlib.import_module", side_effect=ImportError):
            result = extractor.extract_module_path(
                "nonexistent_module.lowercase_func", cs.SupportedLanguage.PYTHON
            )

            assert result == "nonexistent_module.lowercase_func"

    def test_python_uppercase_entity_with_import_failure(
        self, extractor: StdlibExtractor
    ) -> None:
        with patch("importlib.import_module", side_effect=ImportError):
            result = extractor.extract_module_path(
                "nonexistent_module.UppercaseClass", cs.SupportedLanguage.PYTHON
            )

            assert result == "nonexistent_module"

    def test_python_entity_not_found_in_module(
        self, extractor: StdlibExtractor
    ) -> None:
        mock_module = MagicMock(spec=[])

        with patch("importlib.import_module", return_value=mock_module):
            result = extractor.extract_module_path(
                "real_module.NonexistentClass", cs.SupportedLanguage.PYTHON
            )

            assert result == "real_module"

    def test_python_entity_is_module_not_class(
        self, extractor: StdlibExtractor
    ) -> None:
        import types

        mock_module = MagicMock()
        mock_submodule = types.ModuleType("submodule")
        mock_module.submodule = mock_submodule

        with (
            patch("importlib.import_module", return_value=mock_module),
            patch("inspect.ismodule", return_value=True),
            patch("inspect.isclass", return_value=False),
            patch("inspect.isfunction", return_value=False),
        ):
            result = extractor.extract_module_path(
                "parent.submodule", cs.SupportedLanguage.PYTHON
            )

            assert result == "parent.submodule"


class TestJsExtractorWithMockedNode:
    @pytest.fixture
    def extractor(self) -> StdlibExtractor:
        return StdlibExtractor(function_registry=None)

    def test_js_extractor_returns_module_on_successful_introspection(
        self, extractor: StdlibExtractor
    ) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"hasEntity": true, "entityType": "function"}'

        with (
            patch.object(se, "_is_tool_available", return_value=True),
            patch("subprocess.run", return_value=mock_result),
        ):
            result = extractor.extract_module_path(
                "fs.readFile", cs.SupportedLanguage.JS
            )

            assert result == "fs"

    def test_js_extractor_fallback_on_entity_not_found(
        self, extractor: StdlibExtractor
    ) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"hasEntity": false, "entityType": null}'

        with (
            patch.object(se, "_is_tool_available", return_value=True),
            patch("subprocess.run", return_value=mock_result),
        ):
            result = extractor.extract_module_path(
                "fs.nonexistent", cs.SupportedLanguage.JS
            )

            assert result == "fs"

    def test_js_extractor_fallback_on_json_decode_error(
        self, extractor: StdlibExtractor
    ) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "invalid json"

        with (
            patch.object(se, "_is_tool_available", return_value=True),
            patch("subprocess.run", return_value=mock_result),
        ):
            result = extractor.extract_module_path("path.join", cs.SupportedLanguage.JS)

            assert result == "path"

    def test_js_extractor_fallback_on_timeout(self, extractor: StdlibExtractor) -> None:
        import subprocess

        with (
            patch.object(se, "_is_tool_available", return_value=True),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("node", 5)),
        ):
            result = extractor.extract_module_path(
                "http.createServer", cs.SupportedLanguage.JS
            )

            assert result == "http"
