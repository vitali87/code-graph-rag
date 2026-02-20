import json
import time
from pathlib import Path
from typing import TypedDict

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..types_defs import FunctionRegistryTrieProtocol


class StdlibCacheStats(TypedDict):
    cache_entries: int
    cache_languages: list[str]
    total_cached_results: int
    external_tools_checked: dict[str, bool]


_STDLIB_CACHE: dict[str, dict[str, str]] = {}
_CACHE_TTL = cs.IMPORT_CACHE_TTL
_CACHE_TIMESTAMPS: dict[str, float] = {}

_EXTERNAL_TOOLS: dict[str, bool] = {}


def _is_tool_available(tool_name: str) -> bool:
    if tool_name in _EXTERNAL_TOOLS:
        return _EXTERNAL_TOOLS[tool_name]

    import subprocess

    try:
        subprocess.run(
            [tool_name, "--version"], check=False, capture_output=True, timeout=2
        )
        _EXTERNAL_TOOLS[tool_name] = True
        return True
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
    ):
        _EXTERNAL_TOOLS[tool_name] = False
        logger.debug(ls.IMP_TOOL_NOT_AVAILABLE.format(tool=tool_name))
        return False


def _get_cached_stdlib_result(language: str, full_qualified_name: str) -> str | None:
    cache_key = f"{language}:{full_qualified_name}"

    if cache_key not in _STDLIB_CACHE:
        return None

    if (
        cache_key in _CACHE_TIMESTAMPS
        and time.time() - _CACHE_TIMESTAMPS[cache_key] > _CACHE_TTL
    ):
        del _STDLIB_CACHE[cache_key]
        del _CACHE_TIMESTAMPS[cache_key]
        return None

    return _STDLIB_CACHE[cache_key].get(full_qualified_name)


def _cache_stdlib_result(language: str, full_qualified_name: str, result: str) -> None:
    cache_key = f"{language}:{full_qualified_name}"
    _STDLIB_CACHE.setdefault(cache_key, {})[full_qualified_name] = result
    _CACHE_TIMESTAMPS[cache_key] = time.time()


def load_persistent_cache() -> None:
    try:
        cache_file = Path.home() / cs.IMPORT_CACHE_DIR / cs.IMPORT_CACHE_FILE
        if cache_file.exists():
            with cache_file.open() as f:
                data = json.load(f)
                _STDLIB_CACHE.update(data.get(cs.IMPORT_CACHE_KEY, {}))
                _CACHE_TIMESTAMPS.update(data.get(cs.IMPORT_TIMESTAMPS_KEY, {}))
            logger.debug(ls.IMP_CACHE_LOADED.format(path=cache_file))
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(ls.IMP_CACHE_LOAD_ERROR.format(error=e))


def save_persistent_cache() -> None:
    try:
        cache_dir = Path.home() / cs.IMPORT_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / cs.IMPORT_CACHE_FILE

        with cache_file.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    cs.IMPORT_CACHE_KEY: _STDLIB_CACHE,
                    cs.IMPORT_TIMESTAMPS_KEY: _CACHE_TIMESTAMPS,
                },
                f,
                indent=2,
            )
        logger.debug(ls.IMP_CACHE_SAVED.format(path=cache_file))
    except OSError as e:
        logger.debug(ls.IMP_CACHE_SAVE_ERROR.format(error=e))


def flush_stdlib_cache() -> None:
    save_persistent_cache()


def clear_stdlib_cache() -> None:
    _STDLIB_CACHE.clear()
    _CACHE_TIMESTAMPS.clear()
    try:
        cache_file = Path.home() / cs.IMPORT_CACHE_DIR / cs.IMPORT_CACHE_FILE
        if cache_file.exists():
            cache_file.unlink()
            logger.debug(ls.IMP_CACHE_CLEARED)
    except OSError as e:
        logger.debug(ls.IMP_CACHE_CLEAR_ERROR.format(error=e))


def get_stdlib_cache_stats() -> StdlibCacheStats:
    return StdlibCacheStats(
        cache_entries=len(_STDLIB_CACHE),
        cache_languages=list(_STDLIB_CACHE.keys()),
        total_cached_results=sum(
            len(lang_cache) for lang_cache in _STDLIB_CACHE.values()
        ),
        external_tools_checked=_EXTERNAL_TOOLS.copy(),
    )


class StdlibExtractor:
    def __init__(
        self,
        function_registry: FunctionRegistryTrieProtocol | None = None,
        repo_path: Path | None = None,
        project_name: str | None = None,
    ) -> None:
        self.function_registry = function_registry
        self.repo_path = repo_path
        self.project_name = project_name

    def extract_module_path(
        self,
        full_qualified_name: str,
        language: cs.SupportedLanguage = cs.SupportedLanguage.PYTHON,
    ) -> str:
        if self.function_registry and full_qualified_name in self.function_registry:
            entity_type = self.function_registry[full_qualified_name]
            if entity_type in (cs.ENTITY_CLASS, cs.ENTITY_FUNCTION, cs.ENTITY_METHOD):
                parts = full_qualified_name.rsplit(cs.SEPARATOR_DOT, 1)
                if len(parts) == 2:
                    return parts[0]

        match language:
            case cs.SupportedLanguage.PYTHON:
                return self._extract_python_stdlib_path(full_qualified_name)
            case cs.SupportedLanguage.JS | cs.SupportedLanguage.TS:
                return self._extract_js_stdlib_path(full_qualified_name)
            case cs.SupportedLanguage.GO:
                return self._extract_go_stdlib_path(full_qualified_name)
            case cs.SupportedLanguage.RUST:
                return self._extract_rust_stdlib_path(full_qualified_name)
            case cs.SupportedLanguage.CPP:
                return self._extract_cpp_stdlib_path(full_qualified_name)
            case cs.SupportedLanguage.JAVA:
                return self._extract_java_stdlib_path(full_qualified_name)
            case cs.SupportedLanguage.LUA:
                return self._extract_lua_stdlib_path(full_qualified_name)
            case _:
                return self._extract_generic_stdlib_path(full_qualified_name)

    def _extract_python_stdlib_path(self, full_qualified_name: str) -> str:
        parts = full_qualified_name.split(cs.SEPARATOR_DOT)
        if len(parts) >= 3:
            return self._resolve_python_entity_module_path(parts, full_qualified_name)

        cached_result = _get_cached_stdlib_result(
            cs.SupportedLanguage.PYTHON, full_qualified_name
        )
        if cached_result is not None:
            return cached_result

        if len(parts) >= 2:
            return self._resolve_python_entity_module_path(parts, full_qualified_name)
        return full_qualified_name

    def _resolve_python_entity_module_path(
        self, parts: list[str], full_qualified_name: str
    ) -> str:
        module_name = parts[0]
        entity_name = parts[-1]

        if len(parts) >= 3 and self.repo_path and self.project_name:
            try:
                import importlib

                importlib.import_module(module_name)
            except ImportError:
                if (
                    self.function_registry
                    and full_qualified_name in self.function_registry
                ):
                    module_path = cs.SEPARATOR_DOT.join(parts[:-1])
                    _cache_stdlib_result(
                        cs.SupportedLanguage.PYTHON, full_qualified_name, module_path
                    )
                    return module_path

                if parts[0] == self.project_name:
                    relative_parts = parts[1:]
                    module_file = (
                        self.repo_path
                        / Path(*relative_parts[:-1])
                        / f"{relative_parts[-1]}.py"
                    )
                    module_init = self.repo_path / Path(*relative_parts) / "__init__.py"

                    if module_file.exists() or module_init.exists():
                        return full_qualified_name

                module_path = cs.SEPARATOR_DOT.join(parts[:-1])
                _cache_stdlib_result(
                    cs.SupportedLanguage.PYTHON, full_qualified_name, module_path
                )
                return module_path

        try:
            import importlib
            import inspect

            module = importlib.import_module(module_name)

            if hasattr(module, entity_name):
                obj = getattr(module, entity_name)
                if (
                    inspect.isclass(obj)
                    or inspect.isfunction(obj)
                    or not inspect.ismodule(obj)
                ):
                    module_path = cs.SEPARATOR_DOT.join(parts[:-1])
                    _cache_stdlib_result(
                        cs.SupportedLanguage.PYTHON, full_qualified_name, module_path
                    )
                    return module_path
        except (ImportError, AttributeError):
            pass

        result = (
            cs.SEPARATOR_DOT.join(parts[:-1])
            if entity_name[:1].isupper()
            else full_qualified_name
        )
        _cache_stdlib_result(cs.SupportedLanguage.PYTHON, full_qualified_name, result)
        return result

    def _extract_js_stdlib_path(self, full_qualified_name: str) -> str:
        cached_result = _get_cached_stdlib_result(
            cs.SupportedLanguage.JS, full_qualified_name
        )
        if cached_result is not None:
            return cached_result

        parts = full_qualified_name.split(cs.SEPARATOR_DOT)
        if len(parts) >= 2:
            return self._resolve_js_entity_module_path(parts, full_qualified_name)
        return full_qualified_name

    def _resolve_js_entity_module_path(
        self, parts: list[str], full_qualified_name: str
    ) -> str:
        module_name = parts[0]
        entity_name = parts[-1]

        if _is_tool_available("node"):
            try:
                import os
                import subprocess

                node_script = """
                    const moduleName = process.env.MODULE_NAME;
                    const entityName = process.env.ENTITY_NAME;

                    if (!moduleName || !entityName) {
                        console.log(JSON.stringify({hasEntity: false, entityType: null}));
                        process.exit(0);
                    }

                    try {
                        const module = require(moduleName);
                        const hasEntity = entityName in module;
                        const entityType = hasEntity ? typeof module[entityName] : null;
                        console.log(JSON.stringify({hasEntity, entityType}));
                    } catch (e) {
                        console.log(JSON.stringify({hasEntity: false, entityType: null}));
                    }
                    """

                env = os.environ.copy()
                env["MODULE_NAME"] = module_name
                env["ENTITY_NAME"] = entity_name

                subprocess_result = subprocess.run(
                    ["node", "-e", node_script],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=env,
                )

                if subprocess_result.returncode == 0:
                    data = json.loads(subprocess_result.stdout.strip())
                    if data[cs.JSON_KEY_HAS_ENTITY] and data[
                        cs.JSON_KEY_ENTITY_TYPE
                    ] in {
                        cs.TS_FIELD_FUNCTION,
                        cs.TS_FIELD_OBJECT,
                    }:
                        module_path = cs.SEPARATOR_DOT.join(parts[:-1])
                        _cache_stdlib_result(
                            cs.SupportedLanguage.JS, full_qualified_name, module_path
                        )
                        return module_path

            except (
                subprocess.TimeoutExpired,
                subprocess.CalledProcessError,
                json.JSONDecodeError,
            ):
                pass

        result = (
            cs.SEPARATOR_DOT.join(parts[:-1])
            if entity_name[:1].isupper()
            else full_qualified_name
        )
        _cache_stdlib_result(cs.SupportedLanguage.JS, full_qualified_name, result)
        return result

    def _extract_go_stdlib_path(self, full_qualified_name: str) -> str:
        parts = full_qualified_name.split(cs.SEPARATOR_SLASH)
        if len(parts) >= 2:
            try:
                import os
                import subprocess

                package_path = cs.SEPARATOR_SLASH.join(parts[:-1])
                entity_name = parts[-1]

                resolve_result = subprocess.run(
                    ["go", "list", "-f", "{{.Dir}}", package_path],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )

                if resolve_result.returncode != 0:
                    raise subprocess.CalledProcessError(
                        resolve_result.returncode, resolve_result.args
                    )

                package_dir = resolve_result.stdout.strip()
                if not package_dir:
                    raise subprocess.CalledProcessError(1, ["go", "list"])

                go_script = """
package main

import (
    "encoding/json"
    "fmt"
    "go/doc"
    "go/parser"
    "go/token"
    "os"
)

func main() {
    packagePath := os.Getenv("PACKAGE_PATH")
    entityName := os.Getenv("ENTITY_NAME")

    if packagePath == "" || entityName == "" {
        fmt.Print("{\"hasEntity\": false}")
        return
    }

    fset := token.NewFileSet()
    pkgs, err := parser.ParseDir(fset, packagePath, nil, parser.ParseComments)
    if err != nil {
        fmt.Print("{\"hasEntity\": false}")
        return
    }

    for _, pkg := range pkgs {
        d := doc.New(pkg, packagePath, doc.AllDecls)

        // Check functions
        for _, f := range d.Funcs {
            if f.Name == entityName {
                fmt.Print("{\"hasEntity\": true, \"entityType\": \"function\"}")
                return
            }
        }

        // Check types (structs, interfaces, etc.)
        for _, t := range d.Types {
            if t.Name == entityName {
                fmt.Print("{\"hasEntity\": true, \"entityType\": \"type\"}")
                return
            }
        }

        // Check constants and variables
        for _, v := range d.Vars {
            for _, name := range v.Names {
                if name == entityName {
                    fmt.Print("{\"hasEntity\": true, \"entityType\": \"variable\"}")
                    return
                }
            }
        }

        for _, c := range d.Consts {
            for _, name := range c.Names {
                if name == entityName {
                    fmt.Print("{\"hasEntity\": true, \"entityType\": \"constant\"}")
                    return
                }
            }
        }
    }

    fmt.Print("{\"hasEntity\": false}")
}
                """

                env = os.environ.copy()
                env["PACKAGE_PATH"] = package_dir
                env["ENTITY_NAME"] = entity_name

                with subprocess.Popen(
                    ["go", "run", "-"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                ) as proc:
                    stdout, _ = proc.communicate(go_script, timeout=10)

                    if proc.returncode == 0:
                        data = json.loads(stdout.strip())
                        if data[cs.JSON_KEY_HAS_ENTITY]:
                            return package_path

            except (
                subprocess.TimeoutExpired,
                subprocess.CalledProcessError,
                json.JSONDecodeError,
                FileNotFoundError,
            ):
                pass

            entity_name = parts[-1]
            if entity_name[:1].isupper():
                return cs.SEPARATOR_SLASH.join(parts[:-1])

        return full_qualified_name

    def _extract_rust_stdlib_path(self, full_qualified_name: str) -> str:
        parts = full_qualified_name.split(cs.SEPARATOR_DOUBLE_COLON)
        if len(parts) >= 2:
            entity_name = parts[-1]

            if (
                entity_name[:1].isupper()
                or entity_name.isupper()
                or (cs.CHAR_UNDERSCORE not in entity_name and entity_name.islower())
            ):
                return cs.SEPARATOR_DOUBLE_COLON.join(parts[:-1])

        return full_qualified_name

    def _extract_cpp_stdlib_path(self, full_qualified_name: str) -> str:
        parts = full_qualified_name.split(cs.SEPARATOR_DOUBLE_COLON)
        if len(parts) >= 2:
            namespace = parts[0]
            if namespace == cs.CPP_STD_NAMESPACE:
                entity_name = parts[-1]

                try:
                    import os
                    import subprocess
                    import tempfile

                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".txt", delete=False
                    ) as f:
                        f.write(entity_name)
                        entity_file = f.name

                    try:
                        cpp_template_program = f"""
#include <iostream>
#include <fstream>
#include <string>

int main() {{
    std::ifstream file("{entity_file}");
    std::string entity_name;
    std::getline(file, entity_name);
    file.close();

    // This is a compile-time check strategy - we can't dynamically construct templates
    // Fall back to heuristic approach for safety
    std::cout << "heuristic_check" << std::endl;
    return 0;
}}
                        """

                        subprocess.run(
                            ["g++", "-std=c++17", "-x", "c++", "-", "-o", "/dev/null"],
                            check=False,
                            input=cpp_template_program,
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )

                    finally:
                        os.unlink(entity_file)

                except (
                    subprocess.TimeoutExpired,
                    subprocess.CalledProcessError,
                    OSError,
                ):
                    pass

                entity_name = parts[-1]
                if (
                    entity_name[:1].isupper()
                    or entity_name.startswith(cs.CPP_PREFIX_IS)
                    or entity_name.startswith(cs.CPP_PREFIX_HAS)
                    or entity_name in cs.CPP_STDLIB_ENTITIES
                ):
                    return cs.SEPARATOR_DOUBLE_COLON.join(parts[:-1])

        return full_qualified_name

    def _extract_java_stdlib_path(self, full_qualified_name: str) -> str:
        parts = full_qualified_name.split(cs.SEPARATOR_DOT)
        if len(parts) >= 2:
            try:
                import os
                import subprocess
                import tempfile

                package_name = cs.SEPARATOR_DOT.join(parts[:-1])
                entity_name = parts[-1]

                java_program = """
import java.lang.reflect.*;

public class StdlibCheck {
    public static void main(String[] args) {
        if (args.length < 2) {
            System.out.println("{\\"hasEntity\\": false}");
            return;
        }

        String packageName = args[0];
        String entityName = args[1];

        try {
            Class<?> clazz = Class.forName(packageName + "." + entityName);
            System.out.println("{\\"hasEntity\\": true, \\"entityType\\": \\"class\\"}");
        } catch (ClassNotFoundException e) {
            // Try as method or field in parent package
            try {
                Class<?> packageClass = Class.forName(packageName);
                Method[] methods = packageClass.getMethods();
                Field[] fields = packageClass.getFields();

                boolean foundMethod = false;
                for (Method method : methods) {
                    if (method.getName().equals(entityName)) {
                        foundMethod = true;
                        break;
                    }
                }

                boolean foundField = false;
                for (Field field : fields) {
                    if (field.getName().equals(entityName)) {
                        foundField = true;
                        break;
                    }
                }

                if (foundMethod || foundField) {
                    System.out.println("{\\"hasEntity\\": true, \\"entityType\\": \\"member\\"}");
                } else {
                    System.out.println("{\\"hasEntity\\": false}");
                }
            } catch (Exception ex) {
                System.out.println("{\\"hasEntity\\": false}");
            }
        }
    }
}
                """

                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".java", delete=False
                ) as f:
                    f.write(java_program)
                    java_file = f.name

                try:
                    compile_result = subprocess.run(
                        ["javac", java_file],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )

                    if compile_result.returncode == 0:
                        class_name = os.path.splitext(os.path.basename(java_file))[0]
                        run_result = subprocess.run(
                            [
                                "java",
                                "-cp",
                                os.path.dirname(java_file),
                                class_name,
                                package_name,
                                entity_name,
                            ],
                            check=False,
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )

                        if run_result.returncode == 0:
                            data = json.loads(run_result.stdout.strip())
                            if data.get(cs.JSON_KEY_HAS_ENTITY):
                                return cs.SEPARATOR_DOT.join(parts[:-1])

                finally:
                    for ext in (cs.EXT_JAVA, cs.EXT_CLASS):
                        temp_file = os.path.splitext(java_file)[0] + ext
                        try:
                            os.unlink(temp_file)
                        except OSError:
                            pass

            except (
                subprocess.TimeoutExpired,
                subprocess.CalledProcessError,
                json.JSONDecodeError,
                OSError,
            ):
                pass

            entity_name = parts[-1]
            if (
                entity_name[:1].isupper()
                or entity_name.endswith(cs.JAVA_SUFFIX_EXCEPTION)
                or entity_name.endswith(cs.JAVA_SUFFIX_ERROR)
                or entity_name.endswith(cs.JAVA_SUFFIX_INTERFACE)
                or entity_name.endswith(cs.JAVA_SUFFIX_BUILDER)
                or entity_name in cs.JAVA_STDLIB_CLASSES
            ):
                return cs.SEPARATOR_DOT.join(parts[:-1])

        return full_qualified_name

    def _extract_lua_stdlib_path(self, full_qualified_name: str) -> str:
        parts = full_qualified_name.split(cs.SEPARATOR_DOT)
        if len(parts) >= 2:
            module_name = parts[0]
            entity_name = parts[-1]

            try:
                import os
                import subprocess

                lua_script = """
-- Get module and entity names from environment
local module_name = os.getenv("MODULE_NAME")
local entity_name = os.getenv("ENTITY_NAME")

if not module_name or not entity_name then
    print("hasEntity=false")
    return
end

-- Check built-in modules first (they're global tables in Lua)
local module_table = _G[module_name]
if module_table and type(module_table) == "table" then
    local hasEntity = module_table[entity_name] ~= nil
    if hasEntity then
        print("hasEntity=true")
    else
        print("hasEntity=false")
    end
else
    -- Try require for user modules
    local success, loaded_module = pcall(require, module_name)
    if success and type(loaded_module) == "table" then
        local hasEntity = loaded_module[entity_name] ~= nil
        if hasEntity then
            print("hasEntity=true")
        else
            print("hasEntity=false")
        end
    else
        print("hasEntity=false")
    end
end
                """

                env = os.environ.copy()
                env["MODULE_NAME"] = module_name
                env["ENTITY_NAME"] = entity_name

                result = subprocess.run(
                    ["lua", "-e", lua_script],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=env,
                )

                if result.returncode == 0:
                    output = result.stdout.strip()
                    if "hasEntity=true" in output:
                        return cs.SEPARATOR_DOT.join(parts[:-1])

            except (
                subprocess.TimeoutExpired,
                subprocess.CalledProcessError,
                FileNotFoundError,
            ):
                pass

            entity_name = parts[-1]
            if entity_name[:1].isupper() or entity_name in cs.LUA_STDLIB_MODULES:
                return cs.SEPARATOR_DOT.join(parts[:-1])

        return full_qualified_name

    def _extract_generic_stdlib_path(self, full_qualified_name: str) -> str:
        parts = full_qualified_name.split(cs.SEPARATOR_DOT)
        if len(parts) >= 2:
            entity_name = parts[-1]
            if entity_name[:1].isupper():
                return cs.SEPARATOR_DOT.join(parts[:-1])

        return full_qualified_name
