from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater


def test_global_environment_access(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test global environment access patterns with user-defined functions."""
    project = temp_repo / "lua_global_env"
    project.mkdir()

    (project / "globals.lua").write_text(
        encoding="utf-8",
        data="""
local env_manager = {}

function env_manager.backup_global(name)
    -- Simulate backing up a global value
    local backup = {}
    backup[name] = _G[name]
    return backup
end

function env_manager.restore_global(backup, name)
    -- Simulate restoring a global value
    if backup[name] ~= nil then
        _G[name] = backup[name]
    else
        _G[name] = nil
    end
    return true
end

function manage_globals()
    -- Call our user-defined functions to manage globals
    local old_print_backup = env_manager.backup_global("print")

    _G.print = function(...)
        -- Custom print implementation (stdlib call not tracked)
        old_print_backup.print("LOG:", ...)
    end

    _G.custom_global = "Hello from global"
    local global_value = _G.custom_global

    _G["dynamic_key"] = "Dynamic value"
    local dynamic_value = _G["dynamic_key"]

    -- Use our restore function
    env_manager.restore_global(old_print_backup, "print")

    return {
        global_val = global_value,
        dynamic_val = dynamic_value
    }
end

function list_globals()
    local globals = {}
    for key, value in pairs(_G) do
        if type(value) ~= "function" then
            globals[key] = tostring(value)
        end
    end
    return globals
end

-- Create user-defined functions
_G.shared_data = {counter = 0}
_G.increment = function()
    _G.shared_data.counter = _G.shared_data.counter + 1
end

return env_manager
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 4, (
        f"Expected at least 4 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 2, (
        f"Expected at least 2 CALLS relationships, got {len(calls_rels)}"
    )


def test_environment_manipulation(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test environment manipulation with user-defined helper functions."""
    project = temp_repo / "lua_env_manipulation"
    project.mkdir()

    (project / "env_manip.lua").write_text(
        encoding="utf-8",
        data="""
local env_utils = {}

function env_utils.create_safe_env()
    -- Create a safe environment table
    return {
        print = print,
        tostring = tostring,
        tonumber = tonumber,
        type = type,
        pairs = pairs,
        ipairs = ipairs,
        math = math,
        string = string,
        table = table
    }
end

function env_utils.execute_with_env(code, env)
    -- Execute code in a given environment
    local chunk = loadstring(code)
    if chunk then
        setfenv(chunk, env)
        local result = chunk()
        return result
    end
    return nil
end

function create_sandbox()
    local sandbox_env = env_utils.create_safe_env()

    local safe_code = [[
        local x = 10
        local y = 20
        return x + y
    ]]

    return env_utils.execute_with_env(safe_code, sandbox_env)
end

function get_current_env()
    local env = getfenv(1)
    local env_type = type(env)

    local func_env = getfenv(print)

    return {
        current = env,
        env_type = env_type,
        print_env = func_env
    }
end

function modify_function_env(func)
    local new_env = env_utils.create_safe_env()

    -- Override print in the new environment
    new_env.print = function(...)
        print("MODIFIED:", ...)
    end

    setfenv(func, new_env)
    return func
end

-- Use our helper functions
local chunk = loadstring("return 42")
local safe_env = env_utils.create_safe_env()
setfenv(chunk, safe_env)
local result = chunk()

return env_utils
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 5, (
        f"Expected at least 5 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 2, (
        f"Expected at least 2 CALLS relationships, got {len(calls_rels)}"
    )


def test_module_environment_patterns(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test module environment patterns with user-defined functions."""
    project = temp_repo / "lua_module_env"
    project.mkdir()

    (project / "module_env.lua").write_text(
        encoding="utf-8",
        data="""
local M = {}
local env_helpers = {}

function env_helpers.setup_metatable(env, default_value_generator)
    setmetatable(env, {
        __index = function(t, k)
            return default_value_generator(k)
        end
    })
    return env
end

function env_helpers.create_logging_metatable()
    return {
        __index = _G,
        __newindex = function(t, k, v)
            print("Setting isolated:", k, v)
            rawset(t, k, v)
        end
    }
end

function M.setup_module_env()
    local env = getfenv(1)
    env.MODULE_NAME = "MyModule"
    env.VERSION = "1.0.0"

    local generator = function(k)
        return "DEFAULT_" .. tostring(k)
    end

    return env_helpers.setup_metatable(env, generator)
end

function M.create_isolated_env()
    local isolated = {}
    local logging_mt = env_helpers.create_logging_metatable()
    setmetatable(isolated, logging_mt)
    return isolated
end

function M.load_in_environment(code, env)
    local chunk = loadstring(code)
    if chunk then
        setfenv(chunk, env or getfenv(1))
        return pcall(chunk)
    end
    return false, "Failed to load code"
end

-- Test our helper functions
local test_env = M.setup_module_env()
local isolated = M.create_isolated_env()

setfenv(1, {
    print = print,
    require = require,
    M = M,
    env_helpers = env_helpers
})

return M
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 5, (
        f"Expected at least 5 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 2, (
        f"Expected at least 2 CALLS relationships, got {len(calls_rels)}"
    )


def test_dynamic_code_execution(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test dynamic code execution patterns with user-defined helpers."""
    project = temp_repo / "lua_dynamic_exec"
    project.mkdir()

    (project / "dynamic_exec.lua").write_text(
        encoding="utf-8",
        data="""
local code_executor = {}

function code_executor.create_safe_env()
    return {
        math = math,
        string = string,
        table = table,
        print = print,
        tostring = tostring,
        tonumber = tonumber,
        type = type
    }
end

function code_executor.substitute_template(template, params)
    return string.gsub(template, "%$(%w+)", params)
end

function code_executor.execute_in_env(code, env)
    local chunk = loadstring(code)
    if chunk then
        setfenv(chunk, env)
        local success, result = pcall(chunk)
        if success then
            return result
        else
            error("Execution failed: " .. result)
        end
    else
        error("Compilation failed")
    end
end

function execute_dynamic_code(code_str, params)
    local template = code_executor.substitute_template(code_str, params)
    local safe_env = code_executor.create_safe_env()
    return code_executor.execute_in_env(template, safe_env)
end

function load_config_file(filename)
    local file = io.open(filename, "r")
    if file then
        local content = file:read("*all")
        file:close()

        local config_chunk = loadstring("return " .. content)
        if config_chunk then
            local config_env = code_executor.create_safe_env()
            -- Add specific os functions
            config_env.os = {time = os.time, date = os.date}

            return code_executor.execute_in_env("return " .. content, config_env)
        end
    end
    return {}
end

-- Test our functions
local code = "return math.pi * 2"
local safe_env = code_executor.create_safe_env()
local result = code_executor.execute_in_env(code, safe_env)

return code_executor
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 5, (
        f"Expected at least 5 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 3, (
        f"Expected at least 3 CALLS relationships, got {len(calls_rels)}"
    )


def test_global_variable_management(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test global variable management patterns with user-defined functions."""
    project = temp_repo / "lua_global_mgmt"
    project.mkdir()

    (project / "global_mgmt.lua").write_text(
        encoding="utf-8",
        data="""
local global_manager = {}
local globals_backup = {}

function global_manager.backup_value(name)
    globals_backup[name] = _G[name]
    return globals_backup[name]
end

function global_manager.restore_value(name)
    if globals_backup[name] ~= nil then
        _G[name] = globals_backup[name]
    else
        _G[name] = nil
    end
    return _G[name]
end

function global_manager.find_matching_keys(pattern)
    local matches = {}
    for key, _ in pairs(_G) do
        if type(key) == "string" and string.match(key, pattern) then
            table.insert(matches, key)
        end
    end
    return matches
end

function save_globals(names)
    for _, name in ipairs(names) do
        global_manager.backup_value(name)
    end
end

function restore_globals(names)
    for _, name in ipairs(names) do
        global_manager.restore_value(name)
    end
end

function clear_globals(pattern)
    local to_remove = global_manager.find_matching_keys(pattern)

    for _, key in ipairs(to_remove) do
        _G[key] = nil
    end

    return #to_remove
end

function set_global_readonly(name, value)
    _G[name] = value

    local mt = getmetatable(_G) or {}
    local protected = mt.__protected or {}
    protected[name] = true
    mt.__protected = protected

    mt.__newindex = function(t, k, v)
        if protected[k] then
            error("Attempt to modify read-only global: " .. k)
        else
            rawset(t, k, v)
        end
    end

    setmetatable(_G, mt)
end

-- Use our functions
save_globals({"print", "require"})
_G.test_global = "test_value"
restore_globals({"print", "require"})

return global_manager
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 7, (
        f"Expected at least 7 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 3, (
        f"Expected at least 3 CALLS relationships, got {len(calls_rels)}"
    )
