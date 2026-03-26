from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater


def test_math_module_functions(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test user-defined math wrapper functions."""
    project = temp_repo / "lua_math"
    project.mkdir()

    (project / "math_ops.lua").write_text(
        encoding="utf-8",
        data="""
local math_utils = {}

function math_utils.circle_area(radius)
    -- Wrapper for math operations
    return math.pi * math.pow(radius, 2)
end

function math_utils.get_random_in_range(min, max)
    -- Initialize random seed and return random number
    math.randomseed(os.time())
    return math.random(min, max)
end

function math_utils.get_max_value(numbers)
    -- Find maximum from a table of numbers
    local max_val = numbers[1]
    for i = 2, #numbers do
        if numbers[i] > max_val then
            max_val = numbers[i]
        end
    end
    return max_val
end

function calculate_area(radius)
    return math_utils.circle_area(radius)
end

function get_random_value()
    return math_utils.get_random_in_range(1, 100)
end

-- Use our wrapper functions
local area = calculate_area(5)
local random = get_random_value()
local max_value = math_utils.get_max_value({10, 20, 30})

return math_utils
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


def test_string_module_functions(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test user-defined string processing functions."""
    project = temp_repo / "lua_string"
    project.mkdir()

    (project / "string_ops.lua").write_text(
        encoding="utf-8",
        data="""
local string_utils = {}

function string_utils.get_text_info(text)
    -- Get comprehensive text information
    return {
        length = string.len(text),
        upper = string.upper(text),
        lower = string.lower(text),
        first_five = string.sub(text, 1, 5)
    }
end

function string_utils.find_word_position(text, word)
    -- Find position of a word in text
    return string.find(text, word)
end

function string_utils.format_text_info(text, length)
    -- Format text information
    return string.format("Text: %s (length: %d)", text, length)
end

function string_utils.extract_numbers(text)
    -- Extract numbers from text
    return string.match(text, "%d+")
end

function string_utils.replace_word(text, old_word, new_word)
    -- Replace word in text
    return string.gsub(text, old_word, new_word)
end

function process_string(text)
    local info = string_utils.get_text_info(text)
    local find_pos = string_utils.find_word_position(text, "World")
    return string_utils.format_text_info(text, info.length)
end

-- Use our string utility functions
local str = "Hello, World!"
local processed = process_string(str)
local pattern_match = string_utils.extract_numbers("abc123def")
local gsub_result = string_utils.replace_word("hello world", "world", "universe")

return string_utils
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 6, (
        f"Expected at least 6 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 4, (
        f"Expected at least 4 CALLS relationships, got {len(calls_rels)}"
    )


def test_table_module_functions(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test user-defined table manipulation functions."""
    project = temp_repo / "lua_table"
    project.mkdir()

    (project / "table_ops.lua").write_text(
        encoding="utf-8",
        data="""
local table_utils = {}

function table_utils.add_element(tbl, element)
    -- Add element to table
    table.insert(tbl, element)
    return tbl
end

function table_utils.remove_first(tbl)
    -- Remove first element from table
    return table.remove(tbl, 1)
end

function table_utils.sort_table(tbl)
    -- Sort table in place
    table.sort(tbl)
    return tbl
end

function table_utils.join_elements(tbl, separator)
    -- Join table elements with separator
    return table.concat(tbl, separator)
end

function table_utils.get_max_numeric_key(tbl)
    -- Get maximum numeric key
    return table.maxn(tbl)
end

function table_utils.iterate_table(tbl, func)
    -- Apply function to each table element
    table.foreach(tbl, func)
end

function manage_table(tbl)
    table_utils.add_element(tbl, 6)
    table_utils.remove_first(tbl)
    table_utils.sort_table(tbl)
    return table_utils.join_elements(tbl, ", ")
end

-- Use our table utilities
local data = {1, 2, 3, 4, 5}
local result = manage_table(data)
local max_index = table_utils.get_max_numeric_key({a = 1, b = 2, [10] = 3})
table_utils.iterate_table({1, 2, 3}, print)

return table_utils
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 7, (
        f"Expected at least 7 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 4, (
        f"Expected at least 4 CALLS relationships, got {len(calls_rels)}"
    )


def test_os_module_functions(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test user-defined OS operation wrapper functions."""
    project = temp_repo / "lua_os"
    project.mkdir()

    (project / "os_ops.lua").write_text(
        encoding="utf-8",
        data="""
local os_utils = {}

function os_utils.get_current_time()
    -- Get current timestamp
    return os.time()
end

function os_utils.format_date(timestamp, format)
    -- Format timestamp as date string
    return os.date(format or "%Y-%m-%d", timestamp)
end

function os_utils.get_environment_var(var_name)
    -- Get environment variable
    return os.getenv(var_name)
end

function os_utils.execute_command(command)
    -- Execute system command
    return os.execute(command)
end

function os_utils.create_temp_filename()
    -- Create temporary filename
    return os.tmpname()
end

function os_utils.file_operations(old_path, new_path)
    -- Perform file operations
    local temp_file = os_utils.create_temp_filename()
    os.remove(temp_file)  -- Clean up temp file
    return os.rename(old_path, new_path)
end

function os_utils.get_process_time()
    -- Get CPU time used by process
    return os.clock()
end

function system_operations()
    local current_time = os_utils.get_current_time()
    local formatted_date = os_utils.format_date(current_time, "%Y-%m-%d")
    local env_var = os_utils.get_environment_var("PATH")

    os_utils.execute_command("ls -la")
    os_utils.file_operations("old.txt", "new.txt")

    return {
        time = current_time,
        date = formatted_date,
        env = env_var
    }
end

-- Use our OS utilities
local result = system_operations()
local clock_time = os_utils.get_process_time()

return os_utils
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 8, (
        f"Expected at least 8 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 4, (
        f"Expected at least 4 CALLS relationships, got {len(calls_rels)}"
    )


def test_io_module_functions(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test user-defined IO operation wrapper functions."""
    project = temp_repo / "lua_io"
    project.mkdir()

    (project / "io_ops.lua").write_text(
        encoding="utf-8",
        data="""
local io_utils = {}

function io_utils.open_file(filename, mode)
    -- Open file with error handling
    return io.open(filename, mode or "r")
end

function io_utils.read_entire_file(file_handle)
    -- Read entire file content
    return file_handle:read("*all")
end

function io_utils.close_file(file_handle)
    -- Close file handle
    return file_handle:close()
end

function io_utils.write_message(message)
    -- Write message to output
    io.write(message)
    io.flush()
end

function io_utils.write_error(error_message)
    -- Write error message to stderr
    io.stderr:write(error_message)
end

function io_utils.read_line()
    -- Read line from input
    return io.read("*line")
end

function io_utils.set_output_file(filename)
    -- Set output file
    return io.output(filename)
end

function io_utils.set_input_file(filename)
    -- Set input file
    return io.input(filename)
end

function io_utils.create_temp_file()
    -- Create temporary file
    return io.tmpfile()
end

function file_operations(filename)
    local file = io_utils.open_file(filename, "r")
    if file then
        local content = io_utils.read_entire_file(file)
        io_utils.close_file(file)

        io_utils.write_message("Processing file: " .. filename .. "\n")

        return content
    else
        io_utils.write_error("Error: Could not open file\n")
        return nil
    end
end

-- Use our IO utilities
local input = io_utils.read_line()
io_utils.set_output_file("output.txt")
io_utils.set_input_file("input.txt")
local temp_file = io_utils.create_temp_file()

return io_utils
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 10, (
        f"Expected at least 10 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 4, (
        f"Expected at least 4 CALLS relationships, got {len(calls_rels)}"
    )


def test_debug_module_functions(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test user-defined debug utility wrapper functions."""
    project = temp_repo / "lua_debug"
    project.mkdir()

    (project / "debug_ops.lua").write_text(
        encoding="utf-8",
        data="""
local debug_utils = {}

function debug_utils.get_function_info(level, what)
    -- Get function information
    return debug.getinfo(level or 1, what or "nSl")
end

function debug_utils.get_local_variable(level, index)
    -- Get local variable
    return debug.getlocal(level or 1, index or 1)
end

function debug_utils.set_hook(hook_func, mask)
    -- Set debug hook
    debug.sethook(hook_func, mask or "call")
end

function debug_utils.get_traceback()
    -- Get stack traceback
    return debug.traceback()
end

function debug_utils.get_registry()
    -- Get debug registry
    return debug.getregistry()
end

function debug_utils.get_metatable(obj)
    -- Get object metatable
    return debug.getmetatable(obj)
end

function debug_utils.set_metatable(obj, mt)
    -- Set object metatable
    return debug.setmetatable(obj, mt)
end

function debug_utils.get_upvalue(func, index)
    -- Get function upvalue
    return debug.getupvalue(func, index or 1)
end

function debug_operations()
    local info = debug_utils.get_function_info(1, "nSl")
    local local_vars = debug_utils.get_local_variable(1, 1)

    debug_utils.set_hook(function() end, "call")
    local traceback = debug_utils.get_traceback()

    local registry = debug_utils.get_registry()
    local metatable = debug_utils.get_metatable({})

    return {
        info = info,
        vars = local_vars,
        trace = traceback
    }
end

-- Use our debug utilities
local result = debug_operations()
debug_utils.set_metatable({}, {})
local upvalue = debug_utils.get_upvalue(print, 1)

return debug_utils
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 9, (
        f"Expected at least 9 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 6, (
        f"Expected at least 6 CALLS relationships, got {len(calls_rels)}"
    )


def test_package_module_functions(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test user-defined package management wrapper functions."""
    project = temp_repo / "lua_package"
    project.mkdir()

    (project / "package_ops.lua").write_text(
        encoding="utf-8",
        data="""
local package_utils = {}

function package_utils.unload_module(module_name)
    -- Unload module from package.loaded
    package.loaded[module_name] = nil
    return true
end

function package_utils.backup_path()
    -- Backup current package.path
    return package.path
end

function package_utils.add_to_path(additional_path)
    -- Add path to package.path
    package.path = package.path .. ";" .. additional_path
    return package.path
end

function package_utils.backup_cpath()
    -- Backup current package.cpath
    return package.cpath
end

function package_utils.add_to_cpath(additional_cpath)
    -- Add path to package.cpath
    package.cpath = package.cpath .. ";" .. additional_cpath
    return package.cpath
end

function package_utils.get_loader(index)
    -- Get specific loader function
    return package.loaders[index]
end

function package_utils.get_preloaded_module(module_name)
    -- Get preloaded module
    return package.preload[module_name]
end

function package_utils.make_global(env)
    -- Apply package.seeall to environment
    return package.seeall(env)
end

function package_utils.search_module_path(module_name, path)
    -- Search for module in path
    return package.searchpath(module_name, path or package.path)
end

function module_operations()
    package_utils.unload_module("mymodule")

    local path_backup = package_utils.backup_path()
    package_utils.add_to_path("./modules/?.lua")

    local cpath_backup = package_utils.backup_cpath()
    package_utils.add_to_cpath("./lib/?.so")

    local loader = package_utils.get_loader(2)
    local preload = package_utils.get_preloaded_module("custom_module")

    return {
        path = package.path,
        cpath = package.cpath,
        loaded = package.loaded
    }
end

-- Use our package utilities
local result = module_operations()
package_utils.make_global({})
local searchpath = package_utils.search_module_path("module", package.path)

return package_utils
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 10, (
        f"Expected at least 10 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 8, (
        f"Expected at least 8 CALLS relationships, got {len(calls_rels)}"
    )


def test_builtin_functions(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test user-defined wrapper functions for built-in operations."""
    project = temp_repo / "lua_builtins"
    project.mkdir()

    (project / "builtins.lua").write_text(
        encoding="utf-8",
        data="""
local builtin_utils = {}

function builtin_utils.to_string_safe(value)
    -- Safe conversion to string
    return tostring(value)
end

function builtin_utils.to_number_safe(value)
    -- Safe conversion to number
    return tonumber(value)
end

function builtin_utils.get_type_info(obj)
    -- Get type information
    return {
        type = type(obj),
        length = obj and #obj or 0
    }
end

function builtin_utils.iterate_pairs(tbl)
    -- Create pairs iterator
    return pairs(tbl)
end

function builtin_utils.iterate_ipairs(tbl)
    -- Create ipairs iterator
    return ipairs(tbl)
end

function builtin_utils.get_next_value(tbl, key)
    -- Get next value in table
    return next(tbl, key)
end

function builtin_utils.select_from(index, ...)
    -- Select values from arguments
    return select(index, ...)
end

function builtin_utils.unpack_table(tbl)
    -- Unpack table to values
    return unpack(tbl)
end

function builtin_utils.table_access(tbl, key, value)
    -- Safe table access operations
    local old_value = rawget(tbl, key)
    if value ~= nil then
        rawset(tbl, key, value)
    end
    return {
        old_value = old_value,
        equal_check = rawequal(tbl, tbl),
        raw_length = rawlen(tbl)
    }
end

function builtin_utils.manage_metatable(obj, mt)
    -- Manage object metatable
    local old_mt = getmetatable(obj)
    if mt then
        setmetatable(obj, mt)
    end
    return old_mt
end

function builtin_utils.safe_assert(condition, message)
    -- Safe assertion
    return assert(condition, message)
end

function builtin_utils.safe_call(func, ...)
    -- Protected call
    return pcall(func, ...)
end

function builtin_utils.safe_call_with_handler(func, error_handler, ...)
    -- Extended protected call
    return xpcall(func, error_handler, ...)
end

function builtin_utils.manage_memory(action)
    -- Memory management operations
    if action == "collect" then
        collectgarbage("collect")
    elseif action == "count" then
        return collectgarbage("count")
    end
end

function builtin_operations()
    local num_str = builtin_utils.to_string_safe(42)
    local str_num = builtin_utils.to_number_safe("42.5")

    local tbl = {1, 2, 3}
    local type_info = builtin_utils.get_type_info(tbl)

    local pairs_iter = builtin_utils.iterate_pairs(tbl)
    local ipairs_iter = builtin_utils.iterate_ipairs(tbl)
    local next_val = builtin_utils.get_next_value(tbl, nil)

    local selected = builtin_utils.select_from(2, "a", "b", "c")
    local unpacked = builtin_utils.unpack_table({1, 2, 3})

    local access_result = builtin_utils.table_access(tbl, 4, "four")

    local old_mt = builtin_utils.manage_metatable(tbl, {__index = function() return "default" end})

    return {
        string_val = num_str,
        number_val = str_num,
        type_info = type_info
    }
end

-- Use our builtin utilities
local result = builtin_operations()
local assert_result = builtin_utils.safe_assert(true, "Should be true")
local error_result = builtin_utils.safe_call(function() error("Test error") end)
local xpcall_result = builtin_utils.safe_call_with_handler(
    function() return 1/0 end,
    function(err) return err end
)

builtin_utils.manage_memory("collect")
local memory = builtin_utils.manage_memory("count")

return builtin_utils
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 14, (
        f"Expected at least 14 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 10, (
        f"Expected at least 10 CALLS relationships, got {len(calls_rels)}"
    )
