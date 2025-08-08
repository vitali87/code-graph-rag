"""Tests for Lua error handling, pcall, xpcall, and debugging patterns."""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def test_lua_pcall_xpcall_patterns(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test pcall and xpcall error handling patterns."""
    project = temp_repo / "lua_error_pcall"
    project.mkdir()

    (project / "safe_ops.lua").write_text("""
local SafeOps = {}

-- Safe division using pcall
function SafeOps.safe_divide(a, b)
    local function divide()
        if b == 0 then
            error("Division by zero")
        end
        return a / b
    end

    local ok, result = pcall(divide)
    if ok then
        return result, nil
    else
        return nil, result  -- result is error message
    end
end

-- Safe file operations using pcall
function SafeOps.safe_read_file(filename)
    local function read_file()
        local file = io.open(filename, "r")
        if not file then
            error("Could not open file: " .. filename)
        end
        local content = file:read("*all")
        file:close()
        return content
    end

    return pcall(read_file)
end

-- Safe JSON parsing simulation
function SafeOps.safe_json_decode(json_string)
    local function parse_json()
        -- Simulate JSON parsing that might fail
        if not json_string or json_string == "" then
            error("Empty JSON string")
        end
        if not json_string:match("^%s*{") then
            error("Invalid JSON format")
        end
        -- Simulate successful parsing
        return {data = "parsed"}
    end

    return pcall(parse_json)
end

-- Error handler for xpcall
function SafeOps.error_handler(err)
    local trace = debug.traceback()
    return {
        message = tostring(err),
        stack = trace,
        timestamp = os.time()
    }
end

-- Safe operation with custom error handler
function SafeOps.safe_operation_with_traceback(operation)
    return xpcall(operation, SafeOps.error_handler)
end

-- Multiple operation wrapper
function SafeOps.try_multiple(operations)
    local results = {}
    local errors = {}

    for i, op in ipairs(operations) do
        local ok, result = pcall(op)
        if ok then
            results[i] = result
        else
            errors[i] = result
        end
    end

    return results, errors
end

-- Retry pattern with error handling
function SafeOps.retry_with_backoff(operation, max_attempts, initial_delay)
    max_attempts = max_attempts or 3
    initial_delay = initial_delay or 1

    for attempt = 1, max_attempts do
        local ok, result = pcall(operation)
        if ok then
            return result, nil
        end

        if attempt < max_attempts then
            -- Exponential backoff simulation
            local delay = initial_delay * (2 ^ (attempt - 1))
            print(string.format("Attempt %d failed, retrying in %d seconds...", attempt, delay))
            os.execute("sleep " .. delay)  -- Simulate delay
        else
            return nil, result  -- Final failure
        end
    end
end

return SafeOps
""")

    (project / "error_types.lua").write_text("""
local ErrorTypes = {}

-- Custom error types
ErrorTypes.ValidationError = {}
ErrorTypes.ValidationError.__index = ErrorTypes.ValidationError

function ErrorTypes.ValidationError:new(message, field)
    local obj = setmetatable({
        message = message,
        field = field,
        type = "ValidationError"
    }, ErrorTypes.ValidationError)
    return obj
end

function ErrorTypes.ValidationError:__tostring()
    return string.format("ValidationError in field '%s': %s", self.field, self.message)
end

ErrorTypes.NetworkError = {}
ErrorTypes.NetworkError.__index = ErrorTypes.NetworkError

function ErrorTypes.NetworkError:new(message, status_code)
    local obj = setmetatable({
        message = message,
        status_code = status_code,
        type = "NetworkError"
    }, ErrorTypes.NetworkError)
    return obj
end

function ErrorTypes.NetworkError:__tostring()
    return string.format("NetworkError [%d]: %s", self.status_code, self.message)
end

-- Error checking functions
function ErrorTypes.validate_user(user)
    if not user then
        error(ErrorTypes.ValidationError:new("User cannot be nil", "user"))
    end
    if not user.name or user.name == "" then
        error(ErrorTypes.ValidationError:new("Name is required", "name"))
    end
    if not user.email or not user.email:match("@") then
        error(ErrorTypes.ValidationError:new("Valid email is required", "email"))
    end
    return true
end

function ErrorTypes.simulate_network_call(url)
    -- Simulate network failure
    if url:match("timeout") then
        error(ErrorTypes.NetworkError:new("Request timed out", 408))
    elseif url:match("notfound") then
        error(ErrorTypes.NetworkError:new("Resource not found", 404))
    elseif url:match("forbidden") then
        error(ErrorTypes.NetworkError:new("Access forbidden", 403))
    end
    return {status = "success", data = "response"}
end

-- Protected validation
function ErrorTypes.safe_validate_user(user)
    return pcall(ErrorTypes.validate_user, user)
end

-- Protected network call with error classification
function ErrorTypes.safe_network_call(url)
    local ok, result = pcall(ErrorTypes.simulate_network_call, url)
    if ok then
        return result, nil
    else
        -- Classify error type
        if type(result) == "table" and result.type then
            return nil, result
        else
            -- Unknown error, wrap it
            return nil, {type = "UnknownError", message = tostring(result)}
        end
    end
end

return ErrorTypes
""")

    (project / "main.lua").write_text("""
local SafeOps = require('safe_ops')
local ErrorTypes = require('error_types')

-- Test pcall patterns
print("=== Testing Safe Division ===")
local result, err = SafeOps.safe_divide(10, 2)
if result then
    print("Division result:", result)
else
    print("Division error:", err)
end

local result2, err2 = SafeOps.safe_divide(10, 0)
if result2 then
    print("Division result:", result2)
else
    print("Division error:", err2)
end

-- Test xpcall with traceback
print("=== Testing xpcall with traceback ===")
local ok, result = SafeOps.safe_operation_with_traceback(function()
    error("Something went wrong!")
end)
if not ok then
    print("Error details:", result.message)
    print("Stack trace available:", result.stack ~= nil)
end

-- Test multiple operations
print("=== Testing multiple operations ===")
local operations = {
    function() return "success1" end,
    function() error("failed operation") end,
    function() return "success2" end
}
local results, errors = SafeOps.try_multiple(operations)
print("Successful operations:", #results)
print("Failed operations:", #errors)

-- Test custom error types
print("=== Testing custom error types ===")
local user = {name = "", email = "invalid"}
local ok, err = ErrorTypes.safe_validate_user(user)
if not ok then
    print("Validation failed:", tostring(err))
end

-- Test network simulation
local response, net_err = ErrorTypes.safe_network_call("http://example.com/timeout")
if response then
    print("Network success:", response.status)
else
    print("Network error:", net_err.type, net_err.message)
end
""")

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    )
    updater.run()

    created_functions = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == "Function"
    ]
    fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

    safe_ops_qn = f"{project.name}.safe_ops"
    error_types_qn = f"{project.name}.error_types"

    # SafeOps functions
    assert f"{safe_ops_qn}.SafeOps.safe_divide" in fn_qns
    assert f"{safe_ops_qn}.SafeOps.safe_read_file" in fn_qns
    assert f"{safe_ops_qn}.SafeOps.safe_json_decode" in fn_qns
    assert f"{safe_ops_qn}.SafeOps.error_handler" in fn_qns
    assert f"{safe_ops_qn}.SafeOps.safe_operation_with_traceback" in fn_qns
    assert f"{safe_ops_qn}.SafeOps.try_multiple" in fn_qns
    assert f"{safe_ops_qn}.SafeOps.retry_with_backoff" in fn_qns

    # ErrorTypes functions
    assert (
        f"{error_types_qn}.ErrorTypes.ValidationError:new" in fn_qns
        or f"{error_types_qn}.ErrorTypes.ValidationError.new" in fn_qns
    )
    assert (
        f"{error_types_qn}.ErrorTypes.ValidationError:__tostring" in fn_qns
        or f"{error_types_qn}.ErrorTypes.ValidationError.__tostring" in fn_qns
    )
    assert (
        f"{error_types_qn}.ErrorTypes.NetworkError:new" in fn_qns
        or f"{error_types_qn}.ErrorTypes.NetworkError.new" in fn_qns
    )
    assert (
        f"{error_types_qn}.ErrorTypes.NetworkError:__tostring" in fn_qns
        or f"{error_types_qn}.ErrorTypes.NetworkError.__tostring" in fn_qns
    )
    assert f"{error_types_qn}.ErrorTypes.validate_user" in fn_qns
    assert f"{error_types_qn}.ErrorTypes.simulate_network_call" in fn_qns
    assert f"{error_types_qn}.ErrorTypes.safe_validate_user" in fn_qns
    assert f"{error_types_qn}.ErrorTypes.safe_network_call" in fn_qns


def test_lua_debug_library(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua debug library functions."""
    project = temp_repo / "lua_debug_lib"
    project.mkdir()

    (project / "debugger.lua").write_text("""
local Debugger = {}

-- Stack inspection
function Debugger.print_stack()
    local level = 1
    while true do
        local info = debug.getinfo(level, "nSl")
        if not info then break end

        print(string.format("Level %d: %s (%s:%d)",
            level, info.name or "?", info.short_src, info.currentline))
        level = level + 1
    end
end

-- Variable inspection
function Debugger.inspect_locals(level)
    level = level or 2  -- Skip this function
    local locals = {}
    local i = 1

    while true do
        local name, value = debug.getlocal(level, i)
        if not name then break end
        locals[name] = value
        i = i + 1
    end

    return locals
end

-- Upvalue inspection
function Debugger.inspect_upvalues(func)
    local upvalues = {}
    local i = 1

    while true do
        local name, value = debug.getupvalue(func, i)
        if not name then break end
        upvalues[name] = value
        i = i + 1
    end

    return upvalues
end

-- Function info
function Debugger.get_function_info(func)
    local info = debug.getinfo(func, "nSlu")
    return {
        name = info.name,
        source = info.source,
        short_src = info.short_src,
        linedefined = info.linedefined,
        lastlinedefined = info.lastlinedefined,
        what = info.what,
        nups = info.nups
    }
end

-- Profiling with debug hooks
function Debugger.create_profiler()
    local calls = {}
    local start_times = {}

    local function hook(event, line)
        local info = debug.getinfo(2, "nS")
        local func_name = info.name or "anonymous"

        if event == "call" then
            start_times[func_name] = os.clock()
            calls[func_name] = (calls[func_name] or 0) + 1
        elseif event == "return" and start_times[func_name] then
            local duration = os.clock() - start_times[func_name]
            print(string.format("%s took %.4f seconds", func_name, duration))
            start_times[func_name] = nil
        end
    end

    return {
        start = function() debug.sethook(hook, "cr") end,
        stop = function() debug.sethook() end,
        get_stats = function() return calls end
    }
end

-- Memory monitoring
function Debugger.memory_usage()
    collectgarbage("collect")  -- Force full garbage collection
    return {
        memory_kb = collectgarbage("count"),
        objects = collectgarbage("count") * 1024
    }
end

-- Safe module loading with detailed error reporting
function Debugger.safe_require(module_name)
    local function load_module()
        return require(module_name)
    end

    local ok, result = xpcall(load_module, function(err)
        return {
            error = tostring(err),
            stack = debug.traceback(),
            module = module_name,
            search_path = package.path
        }
    end)

    if ok then
        return result, nil
    else
        return nil, result
    end
end

-- Error boundary pattern
function Debugger.create_error_boundary(handler)
    return function(operation)
        local ok, result = xpcall(operation, handler or debug.traceback)
        if ok then
            return result
        else
            print("Error caught by boundary:", result)
            return nil
        end
    end
end

return Debugger
""")

    (project / "validators.lua").write_text("""
local Validators = {}

-- Type validation with detailed errors
function Validators.validate_type(value, expected_type, field_name)
    if type(value) ~= expected_type then
        error(string.format(
            "Type validation failed for field '%s': expected %s, got %s",
            field_name, expected_type, type(value)
        ))
    end
    return true
end

-- Range validation
function Validators.validate_range(value, min_val, max_val, field_name)
    if type(value) ~= "number" then
        error(string.format("Field '%s' must be a number", field_name))
    end
    if value < min_val or value > max_val then
        error(string.format(
            "Field '%s' must be between %d and %d, got %d",
            field_name, min_val, max_val, value
        ))
    end
    return true
end

-- String validation
function Validators.validate_string_length(value, min_len, max_len, field_name)
    if type(value) ~= "string" then
        error(string.format("Field '%s' must be a string", field_name))
    end
    local len = #value
    if len < min_len or len > max_len then
        error(string.format(
            "Field '%s' length must be between %d and %d, got %d",
            field_name, min_len, max_len, len
        ))
    end
    return true
end

-- Email validation
function Validators.validate_email(email, field_name)
    field_name = field_name or "email"
    if type(email) ~= "string" then
        error(string.format("Field '%s' must be a string", field_name))
    end
    if not email:match("^[%w._%+-]+@[%w.-]+%.%w+$") then
        error(string.format("Field '%s' must be a valid email address", field_name))
    end
    return true
end

-- Complex object validation
function Validators.validate_user_object(user)
    local errors = {}

    -- Validate presence
    if not user then
        table.insert(errors, "User object is required")
        return false, errors
    end

    -- Validate individual fields
    local validations = {
        function() Validators.validate_type(user.name, "string", "name") end,
        function() Validators.validate_string_length(user.name, 1, 100, "name") end,
        function() Validators.validate_email(user.email, "email") end,
        function() Validators.validate_type(user.age, "number", "age") end,
        function() Validators.validate_range(user.age, 0, 150, "age") end
    }

    for _, validation in ipairs(validations) do
        local ok, err = pcall(validation)
        if not ok then
            table.insert(errors, err)
        end
    end

    return #errors == 0, errors
end

-- Batch validation with error aggregation
function Validators.validate_batch(items, validator)
    local results = {}
    local all_errors = {}

    for i, item in ipairs(items) do
        local ok, errors = pcall(validator, item)
        if ok then
            results[i] = {valid = true}
        else
            results[i] = {valid = false, error = errors}
            table.insert(all_errors, {index = i, error = errors})
        end
    end

    return results, all_errors
end

-- Assertion helpers
function Validators.assert_not_nil(value, message)
    if value == nil then
        error(message or "Value cannot be nil")
    end
    return value
end

function Validators.assert_positive(value, message)
    if type(value) ~= "number" or value <= 0 then
        error(message or "Value must be a positive number")
    end
    return value
end

function Validators.assert_in_range(value, min_val, max_val, message)
    if type(value) ~= "number" then
        error("Value must be a number")
    end
    if value < min_val or value > max_val then
        error(message or string.format("Value must be between %d and %d", min_val, max_val))
    end
    return value
end

return Validators
""")

    (project / "main.lua").write_text("""
local Debugger = require('debugger')
local Validators = require('validators')

-- Test safe operations
print("=== Testing safe division ===")
local result, err = Debugger.safe_require('json')
if result then
    print("Successfully loaded json module")
else
    print("Failed to load json:", err.error)
end

-- Test validation
print("=== Testing validation ===")
local valid_user = {name = "John", email = "john@example.com", age = 30}
local invalid_user = {name = "", email = "invalid", age = -5}

local ok1, errs1 = Validators.validate_user_object(valid_user)
print("Valid user:", ok1)

local ok2, errs2 = Validators.validate_user_object(invalid_user)
print("Invalid user:", ok2)
if not ok2 then
    for _, err in ipairs(errs2) do
        print("  Error:", err)
    end
end

-- Test error boundary
print("=== Testing error boundary ===")
local boundary = Debugger.create_error_boundary()
local safe_result = boundary(function()
    error("Boundary test error")
end)
print("Boundary result:", safe_result)

-- Test profiler
print("=== Testing profiler ===")
local profiler = Debugger.create_profiler()
profiler.start()

local function expensive_operation()
    local sum = 0
    for i = 1, 1000 do
        sum = sum + i
    end
    return sum
end

expensive_operation()
profiler.stop()
local stats = profiler.get_stats()
for func, count in pairs(stats) do
    print(string.format("Function %s called %d times", func, count))
end
""")

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    )
    updater.run()

    created_functions = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == "Function"
    ]
    fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

    debugger_qn = f"{project.name}.debugger"
    validators_qn = f"{project.name}.validators"

    # Verify Debugger functions
    assert f"{debugger_qn}.Debugger.print_stack" in fn_qns
    assert f"{debugger_qn}.Debugger.inspect_locals" in fn_qns
    assert f"{debugger_qn}.Debugger.inspect_upvalues" in fn_qns
    assert f"{debugger_qn}.Debugger.get_function_info" in fn_qns
    assert f"{debugger_qn}.Debugger.create_profiler" in fn_qns
    assert f"{debugger_qn}.Debugger.memory_usage" in fn_qns
    assert f"{debugger_qn}.Debugger.safe_require" in fn_qns
    assert f"{debugger_qn}.Debugger.create_error_boundary" in fn_qns

    # Verify Validators functions
    assert f"{validators_qn}.Validators.validate_type" in fn_qns
    assert f"{validators_qn}.Validators.validate_range" in fn_qns
    assert f"{validators_qn}.Validators.validate_string_length" in fn_qns
    assert f"{validators_qn}.Validators.validate_email" in fn_qns
    assert f"{validators_qn}.Validators.validate_user_object" in fn_qns
    assert f"{validators_qn}.Validators.validate_batch" in fn_qns
    assert f"{validators_qn}.Validators.assert_not_nil" in fn_qns
    assert f"{validators_qn}.Validators.assert_positive" in fn_qns
    assert f"{validators_qn}.Validators.assert_in_range" in fn_qns


def test_lua_exception_patterns(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua exception-like patterns using coroutines and error objects."""
    project = temp_repo / "lua_exceptions"
    project.mkdir()

    (project / "exceptions.lua").write_text("""
local Exceptions = {}

-- Exception class
local Exception = {}
Exception.__index = Exception

function Exception:new(message, code)
    local obj = setmetatable({
        message = message or "Unknown error",
        code = code or "GENERIC_ERROR",
        timestamp = os.time(),
        stack = debug.traceback()
    }, Exception)
    return obj
end

function Exception:__tostring()
    return string.format("[%s] %s", self.code, self.message)
end

-- Specific exception types
local DatabaseException = setmetatable({}, {__index = Exception})
DatabaseException.__index = DatabaseException

function DatabaseException:new(message, query)
    local obj = Exception.new(self, message, "DB_ERROR")
    obj.query = query
    return obj
end

local NetworkException = setmetatable({}, {__index = Exception})
NetworkException.__index = NetworkException

function NetworkException:new(message, url, status_code)
    local obj = Exception.new(self, message, "NETWORK_ERROR")
    obj.url = url
    obj.status_code = status_code
    return obj
end

-- Try-catch simulation using coroutines
function Exceptions.try_catch(try_block, catch_block, finally_block)
    local co = coroutine.create(try_block)
    local ok, result = coroutine.resume(co)

    if ok and coroutine.status(co) == "dead" then
        -- Success case
        if finally_block then
            finally_block()
        end
        return result
    else
        -- Error case
        if catch_block then
            local handled = catch_block(result)
            if finally_block then
                finally_block()
            end
            return handled
        else
            if finally_block then
                finally_block()
            end
            error(result)  -- Re-throw if no catch block
        end
    end
end

-- Async error handling
function Exceptions.async_operation(operation, callback)
    local co = coroutine.create(function()
        local ok, result = pcall(operation)
        if ok then
            callback(nil, result)
        else
            callback(result, nil)
        end
    end)

    coroutine.resume(co)
    return co
end

-- Error recovery patterns
function Exceptions.with_fallback(operations)
    for i, operation in ipairs(operations) do
        local ok, result = pcall(operation)
        if ok then
            return result, nil
        elseif i == #operations then
            -- Last operation failed, return error
            return nil, result
        end
        -- Try next operation
    end
end

-- Circuit breaker pattern
function Exceptions.create_circuit_breaker(failure_threshold, timeout)
    failure_threshold = failure_threshold or 5
    timeout = timeout or 60

    local state = "closed"  -- closed, open, half-open
    local failure_count = 0
    local last_failure_time = 0

    return function(operation)
        local now = os.time()

        -- Check if we should move from open to half-open
        if state == "open" and (now - last_failure_time) > timeout then
            state = "half-open"
            failure_count = 0
        end

        -- Reject immediately if circuit is open
        if state == "open" then
            error("Circuit breaker is open")
        end

        -- Try the operation
        local ok, result = pcall(operation)

        if ok then
            -- Success - reset or close circuit
            if state == "half-open" then
                state = "closed"
            end
            failure_count = 0
            return result
        else
            -- Failure - increment count and potentially open circuit
            failure_count = failure_count + 1
            last_failure_time = now

            if failure_count >= failure_threshold then
                state = "open"
            end

            error(result)
        end
    end
end

return {
    Exception = Exception,
    DatabaseException = DatabaseException,
    NetworkException = NetworkException,
    try_catch = Exceptions.try_catch,
    async_operation = Exceptions.async_operation,
    with_fallback = Exceptions.with_fallback,
    create_circuit_breaker = Exceptions.create_circuit_breaker
}
""")

    (project / "main.lua").write_text("""
local lib = require('exceptions')
local Exception = lib.Exception
local DatabaseException = lib.DatabaseException

-- Test basic exceptions
print("=== Testing basic exceptions ===")
local db_err = DatabaseException:new("Connection failed", "SELECT * FROM users")
print("Database error:", tostring(db_err))
print("Query was:", db_err.query)

-- Test try-catch pattern
print("=== Testing try-catch ===")
local result = lib.try_catch(
    function()
        error("Something went wrong in try block")
    end,
    function(err)
        print("Caught error:", err)
        return "recovered"
    end,
    function()
        print("Finally block executed")
    end
)
print("Try-catch result:", result)

-- Test fallback pattern
print("=== Testing fallback pattern ===")
local fallback_result, fallback_err = lib.with_fallback({
    function() error("First operation failed") end,
    function() error("Second operation failed") end,
    function() return "Third operation succeeded" end
})

if fallback_result then
    print("Fallback succeeded:", fallback_result)
else
    print("All fallbacks failed:", fallback_err)
end

-- Test circuit breaker
print("=== Testing circuit breaker ===")
local breaker = lib.create_circuit_breaker(2, 5)  -- 2 failures, 5 second timeout

-- Simulate failing operations
for i = 1, 4 do
    local ok, result = pcall(breaker, function()
        if i <= 3 then
            error("Simulated failure " .. i)
        else
            return "Success!"
        end
    end)

    if ok then
        print("Operation", i, "succeeded:", result)
    else
        print("Operation", i, "failed:", result)
    end
end
""")

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    )
    updater.run()

    created_functions = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == "Function"
    ]
    fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

    exceptions_qn = f"{project.name}.exceptions"

    # Verify exception functions are detected
    assert (
        f"{exceptions_qn}.Exception:new" in fn_qns
        or f"{exceptions_qn}.Exception.new" in fn_qns
    )
    assert (
        f"{exceptions_qn}.Exception:__tostring" in fn_qns
        or f"{exceptions_qn}.Exception.__tostring" in fn_qns
    )
    assert (
        f"{exceptions_qn}.DatabaseException:new" in fn_qns
        or f"{exceptions_qn}.DatabaseException.new" in fn_qns
    )
    assert (
        f"{exceptions_qn}.NetworkException:new" in fn_qns
        or f"{exceptions_qn}.NetworkException.new" in fn_qns
    )
    assert f"{exceptions_qn}.Exceptions.try_catch" in fn_qns
    assert f"{exceptions_qn}.Exceptions.async_operation" in fn_qns
    assert f"{exceptions_qn}.Exceptions.with_fallback" in fn_qns
    assert f"{exceptions_qn}.Exceptions.create_circuit_breaker" in fn_qns


def test_lua_error_recovery(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test advanced error recovery and resilience patterns."""
    project = temp_repo / "lua_error_recovery"
    project.mkdir()

    (project / "resilience.lua").write_text("""
local Resilience = {}

-- Timeout wrapper
function Resilience.with_timeout(operation, timeout_seconds)
    local start_time = os.time()

    return function(...)
        local args = {...}
        local co = coroutine.create(function()
            return operation(unpack(args))
        end)

        while coroutine.status(co) ~= "dead" do
            if os.time() - start_time > timeout_seconds then
                error("Operation timed out after " .. timeout_seconds .. " seconds")
            end
            coroutine.resume(co)
        end

        local ok, result = coroutine.resume(co)
        if ok then
            return result
        else
            error(result)
        end
    end
end

-- Bulkhead pattern (resource isolation)
function Resilience.create_bulkhead(max_concurrent)
    local active_operations = 0

    return function(operation)
        if active_operations >= max_concurrent then
            error("Bulkhead limit exceeded: " .. max_concurrent)
        end

        active_operations = active_operations + 1

        local ok, result = pcall(operation)
        active_operations = active_operations - 1

        if ok then
            return result
        else
            error(result)
        end
    end
end

-- Health check pattern
function Resilience.create_health_checker(check_interval)
    local last_check = 0
    local is_healthy = true
    local health_checks = {}

    local function run_checks()
        local now = os.time()
        if now - last_check < check_interval then
            return is_healthy
        end

        last_check = now
        is_healthy = true

        for name, check in pairs(health_checks) do
            local ok, result = pcall(check)
            if not ok then
                print("Health check failed:", name, result)
                is_healthy = false
            end
        end

        return is_healthy
    end

    return {
        add_check = function(name, check_func)
            health_checks[name] = check_func
        end,
        is_healthy = run_checks,
        force_check = function()
            last_check = 0
            return run_checks()
        end
    }
end

-- Rate limiting with error handling
function Resilience.create_rate_limiter(max_requests, time_window)
    local requests = {}

    return function(operation)
        local now = os.time()

        -- Clean old requests
        local filtered_requests = {}
        for _, req_time in ipairs(requests) do
            if now - req_time < time_window then
                table.insert(filtered_requests, req_time)
            end
        end
        requests = filtered_requests

        -- Check rate limit
        if #requests >= max_requests then
            error(string.format(
                "Rate limit exceeded: %d requests in %d seconds",
                max_requests, time_window
            ))
        end

        -- Record this request
        table.insert(requests, now)

        -- Execute operation
        return operation()
    end
end

-- Graceful degradation
function Resilience.with_degradation(primary_operation, fallback_operation)
    return function(...)
        local ok, result = pcall(primary_operation, ...)
        if ok then
            return result, false  -- false indicates no degradation
        else
            print("Primary operation failed, using fallback:", result)
            local fallback_ok, fallback_result = pcall(fallback_operation, ...)
            if fallback_ok then
                return fallback_result, true  -- true indicates degradation
            else
                error("Both primary and fallback operations failed: " .. fallback_result)
            end
        end
    end
end

-- Safe iterator with error boundaries
function Resilience.safe_iterator(items, processor)
    local processed = {}
    local errors = {}

    for i, item in ipairs(items) do
        local ok, result = pcall(processor, item, i)
        if ok then
            processed[i] = result
        else
            errors[i] = {error = result, item = item}
            print(string.format("Error processing item %d: %s", i, result))
        end
    end

    return processed, errors
end

return Resilience
""")

    (project / "main.lua").write_text("""
local Resilience = require('resilience')

-- Test timeout
print("=== Testing timeout wrapper ===")
local timeout_op = Resilience.with_timeout(function()
    -- Simulate long operation
    for i = 1, 1000000 do
        math.sqrt(i)
    end
    return "completed"
end, 2)

local ok, result = pcall(timeout_op)
print("Timeout operation result:", ok, result)

-- Test bulkhead
print("=== Testing bulkhead ===")
local bulkhead = Resilience.create_bulkhead(2)

for i = 1, 3 do
    local ok, result = pcall(bulkhead, function()
        return "Operation " .. i
    end)
    print("Bulkhead operation", i, ":", ok, result)
end

-- Test health checker
print("=== Testing health checker ===")
local health = Resilience.create_health_checker(1)
health.add_check("database", function()
    -- Simulate health check
    return true
end)
health.add_check("api", function()
    error("API is down")
end)

print("System healthy:", health.is_healthy())

-- Test graceful degradation
print("=== Testing graceful degradation ===")
local degraded_op = Resilience.with_degradation(
    function() error("Primary service unavailable") end,
    function() return "Fallback response" end
)

local result, degraded = degraded_op()
print("Degraded operation result:", result, "degraded:", degraded)
""")

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    )
    updater.run()

    created_functions = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == "Function"
    ]
    fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

    resilience_qn = f"{project.name}.resilience"

    # Verify resilience functions
    assert f"{resilience_qn}.Resilience.with_timeout" in fn_qns
    assert f"{resilience_qn}.Resilience.create_bulkhead" in fn_qns
    assert f"{resilience_qn}.Resilience.create_health_checker" in fn_qns
    assert f"{resilience_qn}.Resilience.create_rate_limiter" in fn_qns
    assert f"{resilience_qn}.Resilience.with_degradation" in fn_qns
    assert f"{resilience_qn}.Resilience.safe_iterator" in fn_qns
