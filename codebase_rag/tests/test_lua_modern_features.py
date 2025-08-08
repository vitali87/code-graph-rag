"""Tests for modern Lua 5.4+ features and syntax parsing."""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def test_lua_54_attributes_syntax(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua 5.4 attributes: <close> and <const> variable declarations."""
    project = temp_repo / "lua_54_attributes"
    project.mkdir()

    (project / "attributes.lua").write_text(
        """
local Attributes = {}

-- Test <const> attribute
local PI <const> = 3.14159
local VERSION <const> = "1.0.0"

-- Test <close> attribute with file handling
function Attributes.safe_file_read(filename)
    local file <close> = io.open(filename, "r")
    if not file then
        error("Cannot open file: " .. filename)
    end

    local content = file:read("*all")
    return content
    -- file automatically closed due to <close> attribute
end

-- Test <close> with custom cleanup
function Attributes.with_cleanup()
    local cleanup_called = false

    local resource <close> = setmetatable({
        data = "some resource"
    }, {
        __close = function(self, err)
            cleanup_called = true
            print("Cleaning up resource:", self.data)
            if err then
                print("Error during cleanup:", err)
            end
        end
    })

    -- Use the resource
    print("Using resource:", resource.data)

    -- Test error case
    if math.random() > 0.5 then
        error("Random error for testing cleanup")
    end

    return "Success"
end

-- Test nested scopes with attributes
function Attributes.nested_attributes()
    local outer_const <const> = "outer"

    do
        local inner_const <const> = "inner"
        local temp_file <close> = io.tmpfile()

        if temp_file then
            temp_file:write("Hello from nested scope")
            temp_file:seek("set", 0)
            local content = temp_file:read("*all")
            print("Nested content:", content)
        end

        -- inner_const and temp_file go out of scope here
    end

    return outer_const
end

-- Test attributes in function parameters (if supported)
function Attributes.process_data(data)
    local result <const> = {}
    local processor <close> = setmetatable({
        processed_count = 0
    }, {
        __close = function(self)
            print("Processed", self.processed_count, "items")
        end
    })

    for i, item in ipairs(data) do
        -- Can't reassign result due to <const>
        result[i] = string.upper(tostring(item))
        processor.processed_count = processor.processed_count + 1
    end

    return result
end

-- Test attributes with coroutines
function Attributes.coroutine_with_attributes()
    return coroutine.create(function()
        local state <const> = "coroutine_state"
        local logger <close> = setmetatable({
            logs = {}
        }, {
            __close = function(self)
                print("Coroutine logs:", table.concat(self.logs, ", "))
            end
        })

        for i = 1, 3 do
            table.insert(logger.logs, "step_" .. i)
            coroutine.yield(state .. "_" .. i)
        end

        return "coroutine_complete"
    end)
end

-- Test attributes in complex assignments
function Attributes.complex_assignments()
    local a <const>, b <const> = 1, 2
    local x <close>, y <close> =
        setmetatable({name = "x"}, {__close = function(s) print("Closing", s.name) end}),
        setmetatable({name = "y"}, {__close = function(s) print("Closing", s.name) end})

    return a + b
end

return Attributes
"""
    )

    (project / "main.lua").write_text(
        """
local Attributes = require('attributes')

-- Test constant usage
print("PI value:", PI)  -- Should be accessible from attributes module

-- Test file operations with <close>
local content = Attributes.safe_file_read("test.txt")

-- Test cleanup functionality
local success, result = pcall(Attributes.with_cleanup)
print("Cleanup test result:", success, result)

-- Test nested scopes
local nested_result = Attributes.nested_attributes()
print("Nested result:", nested_result)

-- Test data processing
local processed = Attributes.process_data({"hello", "world", 123})
print("Processed data:", table.concat(processed, ", "))

-- Test coroutine with attributes
local co = Attributes.coroutine_with_attributes()
repeat
    local ok, value = coroutine.resume(co)
    if ok and value then
        print("Coroutine yielded:", value)
    end
until coroutine.status(co) == "dead"

-- Test complex assignments
local sum = Attributes.complex_assignments()
print("Sum:", sum)
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    )

    try:
        updater.run()

        # Check if functions were properly defined
        created_functions = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Function"
        ]
        fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

        attributes_qn = f"{project.name}.attributes"

        # Verify function definitions were extracted despite <const>/<close> syntax
        assert f"{attributes_qn}.Attributes.safe_file_read" in fn_qns
        assert f"{attributes_qn}.Attributes.with_cleanup" in fn_qns
        assert f"{attributes_qn}.Attributes.nested_attributes" in fn_qns
        assert f"{attributes_qn}.Attributes.process_data" in fn_qns
        assert f"{attributes_qn}.Attributes.coroutine_with_attributes" in fn_qns
        assert f"{attributes_qn}.Attributes.complex_assignments" in fn_qns

        # Check if calls were properly extracted
        calls_rels = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == "CALLS"
        ]

        # Should have extracted calls like io.open, setmetatable, etc.
        assert len(calls_rels) >= 5, f"Expected at least 5 CALLS, got {len(calls_rels)}"

        print("✅ Lua 5.4 attributes syntax test PASSED")

    except Exception as e:
        print(f"❌ Lua 5.4 attributes syntax test FAILED: {e}")
        # Re-raise for pytest to catch
        raise


def test_lua_54_enhanced_metamethods(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua 5.4 enhanced metamethod behavior changes."""
    project = temp_repo / "lua_54_metamethods"
    project.mkdir()

    (project / "metamethods.lua").write_text(
        """
local MetaMethods = {}

-- Test new metamethod behavior in Lua 5.4
local Comparable = {}
Comparable.__index = Comparable

function Comparable:new(value)
    return setmetatable({value = value}, self)
end

-- In Lua 5.4, __lt no longer automatically provides __le
Comparable.__lt = function(a, b)
    return a.value < b.value
end

-- Must explicitly define __le in Lua 5.4
Comparable.__le = function(a, b)
    return a.value <= b.value
end

-- Test __eq metamethod
Comparable.__eq = function(a, b)
    return a.value == b.value
end

-- Test all comparison operators
function MetaMethods.test_comparisons()
    local a = Comparable:new(5)
    local b = Comparable:new(10)
    local c = Comparable:new(5)

    -- These should all work correctly in Lua 5.4
    local lt_result = a < b   -- Uses __lt
    local le_result = a <= b  -- Uses __le (not derived from __lt in 5.4)
    local eq_result = a == c  -- Uses __eq
    local gt_result = b > a   -- Uses __lt with swapped arguments
    local ge_result = b >= a  -- Uses __le with swapped arguments

    return {
        lt = lt_result,
        le = le_result,
        eq = eq_result,
        gt = gt_result,
        ge = ge_result
    }
end

-- Test __close metamethod (new in Lua 5.4)
function MetaMethods.test_close_metamethod()
    local CloseableResource = {}
    CloseableResource.__index = CloseableResource

    function CloseableResource:new(name)
        return setmetatable({
            name = name,
            is_open = true
        }, self)
    end

    -- __close metamethod for automatic cleanup
    CloseableResource.__close = function(self, err)
        if self.is_open then
            print("Closing resource:", self.name)
            if err then
                print("Error occurred:", err)
            end
            self.is_open = false
        end
    end

    -- Use with <close> attribute
    local resource <close> = CloseableResource:new("test_resource")
    resource.data = "some important data"

    return resource.data
end

-- Test enhanced __call metamethod patterns
function MetaMethods.test_call_metamethod()
    local Callable = {}
    Callable.__index = Callable

    function Callable:new(func)
        return setmetatable({
            func = func,
            call_count = 0
        }, self)
    end

    Callable.__call = function(self, ...)
        self.call_count = self.call_count + 1
        print("Call #" .. self.call_count)
        return self.func(...)
    end

    -- Create callable object
    local multiplier = Callable:new(function(x, y)
        return x * y
    end)

    -- Call it like a function
    local result1 = multiplier(3, 4)
    local result2 = multiplier(5, 6)

    return {result1, result2, multiplier.call_count}
end

-- Test __index metamethod with enhanced behavior
function MetaMethods.test_index_metamethod()
    local DynamicTable = {}
    DynamicTable.__index = function(table, key)
        if type(key) == "string" and string.match(key, "^get_") then
            local property = string.sub(key, 5)  -- Remove "get_" prefix
            return function(self)
                return rawget(self, property) or ("default_" .. property)
            end
        elseif type(key) == "number" then
            return rawget(table, tostring(key)) or 0
        else
            return rawget(table, key)
        end
    end

    local obj = setmetatable({
        name = "test_object",
        value = 42
    }, DynamicTable)

    -- Test dynamic method generation
    local name = obj:get_name()      -- Should return "test_object"
    local missing = obj:get_missing() -- Should return "default_missing"
    local numeric = obj[1]           -- Should return 0 (default)

    return {name, missing, numeric}
end

return MetaMethods
"""
    )

    (project / "main.lua").write_text(
        """
local MetaMethods = require('metamethods')

-- Test comparison operators
local comparison_results = MetaMethods.test_comparisons()
for op, result in pairs(comparison_results) do
    print(op .. ":", result)
end

-- Test close metamethod
local close_result = MetaMethods.test_close_metamethod()
print("Close result:", close_result)

-- Test call metamethod
local call_results = MetaMethods.test_call_metamethod()
print("Call results:", table.concat(call_results, ", "))

-- Test index metamethod
local index_results = MetaMethods.test_index_metamethod()
print("Index results:", table.concat(index_results, ", "))
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    )

    try:
        updater.run()

        # Check function definitions
        created_functions = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Function"
        ]
        fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

        metamethods_qn = f"{project.name}.metamethods"

        # Verify metamethod-related functions were extracted
        assert f"{metamethods_qn}.MetaMethods.test_comparisons" in fn_qns
        assert f"{metamethods_qn}.MetaMethods.test_close_metamethod" in fn_qns
        assert f"{metamethods_qn}.MetaMethods.test_call_metamethod" in fn_qns
        assert f"{metamethods_qn}.MetaMethods.test_index_metamethod" in fn_qns

        # Check calls were extracted
        calls_rels = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == "CALLS"
        ]

        assert len(calls_rels) >= 3, f"Expected at least 3 CALLS, got {len(calls_rels)}"

        print("✅ Lua 5.4 enhanced metamethods test PASSED")

    except Exception as e:
        print(f"❌ Lua 5.4 enhanced metamethods test FAILED: {e}")
        raise


def test_lua_54_enhanced_stdlib(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua 5.4 enhanced standard library functions."""
    project = temp_repo / "lua_54_stdlib"
    project.mkdir()

    (project / "stdlib.lua").write_text(
        """
local StdLib = {}

-- Test enhanced math.random in Lua 5.4
function StdLib.test_enhanced_math()
    -- Lua 5.4 has improved random number generation
    math.randomseed(os.time())

    local results = {}
    for i = 1, 10 do
        results[i] = math.random()
    end

    -- Test other enhanced math functions
    local huge_test = math.huge
    local min_int = math.mininteger
    local max_int = math.maxinteger

    return {
        random_values = results,
        huge = huge_test,
        min_int = min_int,
        max_int = max_int
    }
end

-- Test enhanced string functions in Lua 5.4
function StdLib.test_enhanced_string()
    local test_string = "Hello, 世界! 🌍"

    -- Test string.pack and string.unpack (enhanced in 5.4)
    local packed = string.pack("i4i4", 123, 456)
    local a, b = string.unpack("i4i4", packed)

    -- Test string handling with unicode
    local byte_values = {string.byte(test_string, 1, -1)}
    local char_string = string.char(72, 101, 108, 108, 111)  -- "Hello"

    -- Test pattern matching enhancements
    local pattern_results = {}
    for word in string.gmatch(test_string, "%w+") do
        table.insert(pattern_results, word)
    end

    return {
        packed_unpacked = {a, b},
        bytes = byte_values,
        char_result = char_string,
        pattern_matches = pattern_results
    }
end

-- Test enhanced table functions
function StdLib.test_enhanced_table()
    local test_table = {10, 20, 30, 40, 50}

    -- Test table.move (available since 5.3, enhanced in 5.4)
    local target = {}
    table.move(test_table, 2, 4, 1, target)

    -- Test table operations
    table.insert(test_table, 3, 25)  -- Insert at position 3
    local removed = table.remove(test_table, 2)  -- Remove from position 2

    -- Test table.sort with custom comparator
    local sort_test = {5, 2, 8, 1, 9}
    table.sort(sort_test, function(a, b) return a > b end)  -- Descending

    return {
        moved = target,
        modified = test_table,
        removed = removed,
        sorted = sort_test
    }
end

-- Test enhanced coroutine functions
function StdLib.test_enhanced_coroutines()
    local function producer()
        for i = 1, 5 do
            coroutine.yield("item_" .. i)
        end
        return "producer_done"
    end

    local co = coroutine.create(producer)
    local results = {}

    repeat
        local status = coroutine.status(co)
        if status == "suspended" then
            local ok, value = coroutine.resume(co)
            if ok then
                table.insert(results, value)
            else
                table.insert(results, "error: " .. value)
                break
            end
        end
    until coroutine.status(co) == "dead"

    return results
end

-- Test enhanced io functions
function StdLib.test_enhanced_io()
    -- Test io.tmpfile (enhanced error handling in 5.4)
    local tmpfile = io.tmpfile()
    if tmpfile then
        tmpfile:write("Temporary data for testing")
        tmpfile:seek("set", 0)
        local content = tmpfile:read("*all")
        tmpfile:close()
        return content
    else
        return "tmpfile creation failed"
    end
end

-- Test enhanced os functions
function StdLib.test_enhanced_os()
    local results = {}

    -- Test time functions
    results.time = os.time()
    results.clock = os.clock()
    results.date = os.date("%Y-%m-%d %H:%M:%S")

    -- Test difftime
    local start_time = os.time()
    -- Simulate some work
    for i = 1, 1000 do
        math.sqrt(i)
    end
    local end_time = os.time()
    results.diff = os.difftime(end_time, start_time)

    return results
end

-- Comprehensive test function
function StdLib.run_all_tests()
    return {
        math_tests = StdLib.test_enhanced_math(),
        string_tests = StdLib.test_enhanced_string(),
        table_tests = StdLib.test_enhanced_table(),
        coroutine_tests = StdLib.test_enhanced_coroutines(),
        io_tests = StdLib.test_enhanced_io(),
        os_tests = StdLib.test_enhanced_os()
    }
end

return StdLib
"""
    )

    (project / "main.lua").write_text(
        """
local StdLib = require('stdlib')

-- Run all enhanced standard library tests
local all_results = StdLib.run_all_tests()

-- Print results
for category, results in pairs(all_results) do
    print("=== " .. category .. " ===")
    if type(results) == "table" then
        for key, value in pairs(results) do
            print(key .. ":", tostring(value))
        end
    else
        print(tostring(results))
    end
    print()
end
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    )

    try:
        updater.run()

        # Check function definitions
        created_functions = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Function"
        ]
        fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

        stdlib_qn = f"{project.name}.stdlib"

        # Verify enhanced stdlib functions were extracted
        expected_functions = [
            f"{stdlib_qn}.StdLib.test_enhanced_math",
            f"{stdlib_qn}.StdLib.test_enhanced_string",
            f"{stdlib_qn}.StdLib.test_enhanced_table",
            f"{stdlib_qn}.StdLib.test_enhanced_coroutines",
            f"{stdlib_qn}.StdLib.test_enhanced_io",
            f"{stdlib_qn}.StdLib.test_enhanced_os",
            f"{stdlib_qn}.StdLib.run_all_tests",
        ]

        for expected_fn in expected_functions:
            assert expected_fn in fn_qns, f"Missing function: {expected_fn}"

        # Check standard library calls were extracted
        calls_rels = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == "CALLS"
        ]

        # Should have many stdlib function calls
        assert len(calls_rels) >= 10, (
            f"Expected at least 10 CALLS, got {len(calls_rels)}"
        )

        print("✅ Lua 5.4 enhanced standard library test PASSED")

    except Exception as e:
        print(f"❌ Lua 5.4 enhanced standard library test FAILED: {e}")
        raise


def test_lua_54_numerical_for_loops(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua 5.4 numerical for loop semantic changes."""
    project = temp_repo / "lua_54_for_loops"
    project.mkdir()

    (project / "for_loops.lua").write_text(
        """
local ForLoops = {}

-- Test Lua 5.4 numerical for loop changes
function ForLoops.test_numerical_for()
    local results = {}

    -- Basic for loop
    for i = 1, 10 do
        table.insert(results, i)
    end

    -- For loop with step
    local step_results = {}
    for i = 1, 10, 2 do
        table.insert(step_results, i)
    end

    -- Negative step
    local negative_results = {}
    for i = 10, 1, -1 do
        table.insert(negative_results, i)
    end

    return {
        basic = results,
        step = step_results,
        negative = negative_results
    }
end

-- Test edge cases with numerical for loops
function ForLoops.test_for_edge_cases()
    local edge_results = {}

    -- Large numbers (Lua 5.4 changed overflow behavior)
    for i = math.maxinteger - 2, math.maxinteger do
        table.insert(edge_results, i)
    end

    -- Floating point for loops
    local float_results = {}
    for i = 0.1, 1.0, 0.1 do
        table.insert(float_results, math.floor(i * 10) / 10)  -- Round for consistency
    end

    return {
        large_numbers = edge_results,
        floating_point = float_results
    }
end

-- Test for loops with function calls
function ForLoops.test_for_with_calls()
    local call_results = {}

    local function get_start()
        return 1
    end

    local function get_end()
        return 5
    end

    local function get_step()
        return 1
    end

    -- For loop with function call bounds
    for i = get_start(), get_end(), get_step() do
        table.insert(call_results, i * 2)
    end

    return call_results
end

-- Test nested for loops
function ForLoops.test_nested_for()
    local matrix = {}

    for i = 1, 3 do
        matrix[i] = {}
        for j = 1, 3 do
            matrix[i][j] = i * j
        end
    end

    return matrix
end

-- Test for loops with break and continue patterns
function ForLoops.test_for_control_flow()
    local break_results = {}
    local continue_results = {}

    -- Test break
    for i = 1, 10 do
        if i > 5 then
            break
        end
        table.insert(break_results, i)
    end

    -- Test continue pattern (using goto in Lua 5.2+)
    for i = 1, 10 do
        if i % 2 == 0 then
            goto continue
        end
        table.insert(continue_results, i)
        ::continue::
    end

    return {
        break_test = break_results,
        continue_test = continue_results
    }
end

return ForLoops
"""
    )

    (project / "main.lua").write_text(
        """
local ForLoops = require('for_loops')

-- Test all for loop variations
local numerical_results = ForLoops.test_numerical_for()
local edge_results = ForLoops.test_for_edge_cases()
local call_results = ForLoops.test_for_with_calls()
local nested_results = ForLoops.test_nested_for()
local control_results = ForLoops.test_for_control_flow()

-- Print results
print("Numerical for loops:", #numerical_results.basic, "items")
print("Edge cases:", #edge_results.large_numbers, "large numbers")
print("Function calls:", #call_results, "results")
print("Nested loops: 3x3 matrix created")
print("Control flow: break =", #control_results.break_test, "continue =", #control_results.continue_test)
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    )

    try:
        updater.run()

        # Check function definitions
        created_functions = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Function"
        ]
        fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

        for_loops_qn = f"{project.name}.for_loops"

        # Verify for loop test functions were extracted
        expected_functions = [
            f"{for_loops_qn}.ForLoops.test_numerical_for",
            f"{for_loops_qn}.ForLoops.test_for_edge_cases",
            f"{for_loops_qn}.ForLoops.test_for_with_calls",
            f"{for_loops_qn}.ForLoops.test_nested_for",
            f"{for_loops_qn}.ForLoops.test_for_control_flow",
        ]

        for expected_fn in expected_functions:
            assert expected_fn in fn_qns, f"Missing function: {expected_fn}"

        print("✅ Lua 5.4 numerical for loops test PASSED")

    except Exception as e:
        print(f"❌ Lua 5.4 numerical for loops test FAILED: {e}")
        raise
