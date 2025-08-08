"""Tests for Lua 5.4 edge cases and advanced syntax features."""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def test_lua_54_goto_labels(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua 5.2+ goto statements and labels parsing."""
    project = temp_repo / "lua_54_goto"
    project.mkdir()

    (project / "goto_labels.lua").write_text(
        """
local GotoLabels = {}

-- Test basic goto and labels
function GotoLabels.test_basic_goto()
    local result = {}
    local i = 1

    ::loop_start::
    if i <= 5 then
        table.insert(result, i)
        i = i + 1
        goto loop_start
    end

    return result
end

-- Test goto for error handling
function GotoLabels.test_goto_error_handling()
    local function risky_operation(value)
        if value < 0 then
            goto error_handler
        end

        if value == 0 then
            goto zero_handler
        end

        -- Normal case
        return value * 2

        ::error_handler::
        error("Negative value not allowed")

        ::zero_handler::
        return 0
    end

    local results = {}
    for _, val in ipairs({-1, 0, 5, 10}) do
        local ok, result = pcall(risky_operation, val)
        table.insert(results, {val, ok, result})
    end

    return results
end

-- Test goto for cleanup patterns
function GotoLabels.test_goto_cleanup()
    local function process_with_cleanup(data)
        local file = nil
        local temp_table = {}

        -- Try to open file
        file = io.tmpfile()
        if not file then
            goto cleanup
        end

        -- Process data
        for i, item in ipairs(data) do
            if type(item) ~= "string" then
                goto cleanup
            end
            file:write(item .. "\n")
            table.insert(temp_table, string.upper(item))
        end

        -- Success path
        file:seek("set", 0)
        local content = file:read("*all")
        file:close()
        return content, temp_table

        ::cleanup::
        if file then
            file:close()
        end
        return nil, "Error during processing"
    end

    return process_with_cleanup({"hello", "world", "test"})
end

-- Test nested scopes with goto
function GotoLabels.test_nested_goto()
    local function complex_control_flow(n)
        local result = 0

        for i = 1, n do
            if i % 2 == 0 then
                goto even_handler
            else
                goto odd_handler
            end

            ::even_handler::
            result = result + i * 2
            goto continue

            ::odd_handler::
            result = result + i
            goto continue

            ::continue::
            -- Continue loop iteration
        end

        return result
    end

    return complex_control_flow(10)
end

-- Test goto with closures
function GotoLabels.test_goto_with_closures()
    local function create_state_machine()
        local state = "start"
        local transitions = 0

        return function(input)
            transitions = transitions + 1

            if state == "start" then
                goto start_state
            elseif state == "middle" then
                goto middle_state
            elseif state == "end" then
                goto end_state
            end

            ::start_state::
            if input == "begin" then
                state = "middle"
                return "started"
            else
                goto invalid_input
            end

            ::middle_state::
            if input == "process" then
                return "processing"
            elseif input == "finish" then
                state = "end"
                return "finishing"
            else
                goto invalid_input
            end

            ::end_state::
            if input == "reset" then
                state = "start"
                return "reset"
            else
                return "completed"
            end

            ::invalid_input::
            return "invalid input in state: " .. state
        end
    end

    local machine = create_state_machine()
    local results = {}

    table.insert(results, machine("begin"))
    table.insert(results, machine("process"))
    table.insert(results, machine("finish"))
    table.insert(results, machine("reset"))

    return results
end

return GotoLabels
"""
    )

    (project / "main.lua").write_text(
        """
local GotoLabels = require('goto_labels')

-- Test all goto label patterns
local basic_result = GotoLabels.test_basic_goto()
print("Basic goto:", table.concat(basic_result, ", "))

local error_results = GotoLabels.test_goto_error_handling()
for _, result in ipairs(error_results) do
    print("Error handling:", result[1], result[2], result[3])
end

local cleanup_content, cleanup_result = GotoLabels.test_goto_cleanup()
print("Cleanup test:", cleanup_content and "success" or cleanup_result)

local nested_result = GotoLabels.test_nested_goto()
print("Nested goto result:", nested_result)

local machine_results = GotoLabels.test_goto_with_closures()
print("State machine:", table.concat(machine_results, " -> "))
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

        goto_qn = f"{project.name}.goto_labels"

        # Verify goto-related functions were extracted
        expected_functions = [
            f"{goto_qn}.GotoLabels.test_basic_goto",
            f"{goto_qn}.GotoLabels.test_goto_error_handling",
            f"{goto_qn}.GotoLabels.test_goto_cleanup",
            f"{goto_qn}.GotoLabels.test_nested_goto",
            f"{goto_qn}.GotoLabels.test_goto_with_closures",
        ]

        for expected_fn in expected_functions:
            assert expected_fn in fn_qns, f"Missing function: {expected_fn}"

        print("✅ Lua 5.4 goto/labels test PASSED")

    except Exception as e:
        print(f"❌ Lua 5.4 goto/labels test FAILED: {e}")
        raise


def test_lua_54_utf8_library(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua 5.3+ UTF-8 library function calls."""
    project = temp_repo / "lua_54_utf8"
    project.mkdir()

    (project / "utf8_lib.lua").write_text(
        """
local UTF8Lib = {}

-- Test UTF-8 library functions
function UTF8Lib.test_utf8_functions()
    local test_string = "Hello, 世界! 🌍"
    local results = {}

    -- Test utf8.len
    results.length = utf8.len(test_string)

    -- Test utf8.char
    results.char_test = utf8.char(72, 101, 108, 108, 111)  -- "Hello"

    -- Test utf8.codes iterator
    local codes = {}
    for pos, code in utf8.codes(test_string) do
        table.insert(codes, {pos, code})
    end
    results.codes = codes

    -- Test utf8.codepoint
    local first_codepoint = utf8.codepoint(test_string, 1)
    local last_codepoint = utf8.codepoint(test_string, -1)
    results.codepoints = {first_codepoint, last_codepoint}

    -- Test utf8.offset
    local second_char_offset = utf8.offset(test_string, 2)
    local last_char_offset = utf8.offset(test_string, -1)
    results.offsets = {second_char_offset, last_char_offset}

    return results
end

-- Test UTF-8 validation and manipulation
function UTF8Lib.test_utf8_validation()
    local valid_utf8 = "Valid UTF-8: 你好"
    local invalid_utf8 = "Invalid: \xff\xfe"

    local results = {}

    -- Test valid string
    local valid_len = utf8.len(valid_utf8)
    results.valid = {string = valid_utf8, length = valid_len}

    -- Test invalid string (utf8.len returns nil for invalid)
    local invalid_len = utf8.len(invalid_utf8)
    results.invalid = {string = invalid_utf8, length = invalid_len}

    return results
end

-- Test UTF-8 string processing
function UTF8Lib.test_utf8_processing()
    local mixed_string = "ABC 🌟 xyz ñ ü"
    local processed = {}

    -- Extract each character
    local chars = {}
    for pos, code in utf8.codes(mixed_string) do
        local char = utf8.char(code)
        table.insert(chars, char)
    end
    processed.characters = chars

    -- Count different types of characters
    local ascii_count = 0
    local unicode_count = 0

    for _, char in ipairs(chars) do
        local code = utf8.codepoint(char)
        if code < 128 then
            ascii_count = ascii_count + 1
        else
            unicode_count = unicode_count + 1
        end
    end

    processed.counts = {ascii = ascii_count, unicode = unicode_count}

    return processed
end

-- Test UTF-8 with pattern matching
function UTF8Lib.test_utf8_patterns()
    local text = "Email: user@example.com, 电话: +1-555-0123"
    local results = {}

    -- Extract ASCII parts
    local ascii_parts = {}
    for match in string.gmatch(text, "[%w%p%s]+") do
        if utf8.len(match) == string.len(match) then  -- ASCII only
            table.insert(ascii_parts, match)
        end
    end
    results.ascii_parts = ascii_parts

    -- Find UTF-8 characters
    local utf8_chars = {}
    for pos, code in utf8.codes(text) do
        if code > 127 then
            table.insert(utf8_chars, utf8.char(code))
        end
    end
    results.utf8_chars = utf8_chars

    return results
end

-- Test UTF-8 normalization and comparison
function UTF8Lib.test_utf8_normalization()
    -- Different representations of the same character
    local composed = "é"     -- Single character
    local decomposed = "e\\u{0301}"  -- e + combining acute accent

    local results = {}

    -- Check lengths
    results.composed_len = utf8.len(composed)
    results.decomposed_len = utf8.len(decomposed)

    -- Check byte lengths
    results.composed_bytes = string.len(composed)
    results.decomposed_bytes = string.len(decomposed)

    -- Extract codepoints
    local composed_codes = {}
    for pos, code in utf8.codes(composed) do
        table.insert(composed_codes, code)
    end

    local decomposed_codes = {}
    for pos, code in utf8.codes(decomposed) do
        table.insert(decomposed_codes, code)
    end

    results.composed_codes = composed_codes
    results.decomposed_codes = decomposed_codes

    return results
end

return UTF8Lib
"""
    )

    (project / "main.lua").write_text(
        """
local UTF8Lib = require('utf8_lib')

-- Test UTF-8 library functions
local utf8_results = UTF8Lib.test_utf8_functions()
print("UTF-8 length:", utf8_results.length)
print("UTF-8 char test:", utf8_results.char_test)

local validation_results = UTF8Lib.test_utf8_validation()
print("Valid UTF-8 length:", validation_results.valid.length)
print("Invalid UTF-8 length:", validation_results.invalid.length)

local processing_results = UTF8Lib.test_utf8_processing()
print("Character count:", #processing_results.characters)
print("ASCII count:", processing_results.counts.ascii)
print("Unicode count:", processing_results.counts.unicode)

local pattern_results = UTF8Lib.test_utf8_patterns()
print("ASCII parts found:", #pattern_results.ascii_parts)
print("UTF-8 chars found:", #pattern_results.utf8_chars)

local norm_results = UTF8Lib.test_utf8_normalization()
print("Composed length:", norm_results.composed_len)
print("Decomposed length:", norm_results.decomposed_len)
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

        utf8_qn = f"{project.name}.utf8_lib"

        # Verify UTF-8 library functions were extracted
        expected_functions = [
            f"{utf8_qn}.UTF8Lib.test_utf8_functions",
            f"{utf8_qn}.UTF8Lib.test_utf8_validation",
            f"{utf8_qn}.UTF8Lib.test_utf8_processing",
            f"{utf8_qn}.UTF8Lib.test_utf8_patterns",
            f"{utf8_qn}.UTF8Lib.test_utf8_normalization",
        ]

        for expected_fn in expected_functions:
            assert expected_fn in fn_qns, f"Missing function: {expected_fn}"

        # Check UTF-8 library calls were extracted
        calls_rels = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == "CALLS"
        ]

        # Should have UTF-8 library function calls
        assert len(calls_rels) >= 5, f"Expected at least 5 CALLS, got {len(calls_rels)}"

        print("✅ Lua 5.4 UTF-8 library test PASSED")

    except Exception as e:
        print(f"❌ Lua 5.4 UTF-8 library test FAILED: {e}")
        raise


def test_lua_54_bitwise_operators(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua 5.3+ bitwise operators parsing."""
    project = temp_repo / "lua_54_bitwise"
    project.mkdir()

    (project / "bitwise_ops.lua").write_text(
        """
local BitwiseOps = {}

-- Test basic bitwise operations
function BitwiseOps.test_basic_bitwise()
    local a = 0xFF  -- 255
    local b = 0x0F  -- 15

    local results = {}

    -- Bitwise AND
    results.and_result = a & b

    -- Bitwise OR
    results.or_result = a | b

    -- Bitwise XOR
    results.xor_result = a ~ b

    -- Bitwise NOT
    results.not_a = ~a
    results.not_b = ~b

    -- Left shift
    results.left_shift = b << 2

    -- Right shift
    results.right_shift = a >> 2

    return results
end

-- Test bitwise operations with variables
function BitwiseOps.test_variable_bitwise()
    local function bit_operations(x, y)
        return {
            and_op = x & y,
            or_op = x | y,
            xor_op = x ~ y,
            not_x = ~x,
            shift_left = x << 1,
            shift_right = x >> 1
        }
    end

    local test_cases = {
        {10, 3},
        {255, 128},
        {0x1234, 0x5678}
    }

    local results = {}
    for i, case in ipairs(test_cases) do
        results[i] = bit_operations(case[1], case[2])
    end

    return results
end

-- Test bitwise operations in expressions
function BitwiseOps.test_complex_bitwise()
    local function rgb_to_int(r, g, b)
        return (r << 16) | (g << 8) | b
    end

    local function int_to_rgb(color)
        local r = (color >> 16) & 0xFF
        local g = (color >> 8) & 0xFF
        local b = color & 0xFF
        return r, g, b
    end

    -- Test color conversion
    local red, green, blue = 255, 128, 64
    local color_int = rgb_to_int(red, green, blue)
    local r2, g2, b2 = int_to_rgb(color_int)

    return {
        original = {red, green, blue},
        color_int = color_int,
        converted = {r2, g2, b2},
        match = (red == r2 and green == g2 and blue == b2)
    }
end

-- Test bitwise operations with metamethods
function BitwiseOps.test_bitwise_metamethods()
    local BitVector = {}
    BitVector.__index = BitVector

    function BitVector:new(value)
        return setmetatable({value = value or 0}, self)
    end

    -- Bitwise metamethods
    BitVector.__band = function(a, b)
        return BitVector:new(a.value & b.value)
    end

    BitVector.__bor = function(a, b)
        return BitVector:new(a.value | b.value)
    end

    BitVector.__bxor = function(a, b)
        return BitVector:new(a.value ~ b.value)
    end

    BitVector.__bnot = function(a)
        return BitVector:new(~a.value)
    end

    BitVector.__shl = function(a, n)
        return BitVector:new(a.value << n)
    end

    BitVector.__shr = function(a, n)
        return BitVector:new(a.value >> n)
    end

    -- Test the metamethods
    local vec1 = BitVector:new(0xF0)
    local vec2 = BitVector:new(0x0F)

    local and_result = vec1 & vec2
    local or_result = vec1 | vec2
    local xor_result = vec1 ~ vec2
    local not_result = ~vec1
    local shift_left = vec1 << 2
    local shift_right = vec1 >> 2

    return {
        and_val = and_result.value,
        or_val = or_result.value,
        xor_val = xor_result.value,
        not_val = not_result.value,
        shl_val = shift_left.value,
        shr_val = shift_right.value
    }
end

-- Test bitwise operations with bit manipulation algorithms
function BitwiseOps.test_bit_algorithms()
    local function count_set_bits(n)
        local count = 0
        while n > 0 do
            count = count + 1
            n = n & (n - 1)  -- Clear the lowest set bit
        end
        return count
    end

    local function is_power_of_two(n)
        return n > 0 and (n & (n - 1)) == 0
    end

    local function reverse_bits(n, bits)
        bits = bits or 8
        local result = 0
        for i = 0, bits - 1 do
            if (n & (1 << i)) ~= 0 then
                result = result | (1 << (bits - 1 - i))
            end
        end
        return result
    end

    local test_numbers = {15, 16, 255, 1024, 0x1234}
    local results = {}

    for _, num in ipairs(test_numbers) do
        results[num] = {
            set_bits = count_set_bits(num),
            is_power_of_2 = is_power_of_two(num),
            reversed = reverse_bits(num, 16)
        }
    end

    return results
end

return BitwiseOps
"""
    )

    (project / "main.lua").write_text(
        """
local BitwiseOps = require('bitwise_ops')

-- Test all bitwise operations
local basic_results = BitwiseOps.test_basic_bitwise()
print("Basic AND result:", basic_results.and_result)
print("Basic OR result:", basic_results.or_result)

local variable_results = BitwiseOps.test_variable_bitwise()
print("Variable tests count:", #variable_results)

local complex_results = BitwiseOps.test_complex_bitwise()
print("Color conversion match:", complex_results.match)

local metamethod_results = BitwiseOps.test_bitwise_metamethods()
print("Metamethod AND:", metamethod_results.and_val)
print("Metamethod OR:", metamethod_results.or_val)

local algorithm_results = BitwiseOps.test_bit_algorithms()
for num, result in pairs(algorithm_results) do
    print("Number", num, "has", result.set_bits, "set bits")
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

        bitwise_qn = f"{project.name}.bitwise_ops"

        # Verify bitwise operation functions were extracted
        expected_functions = [
            f"{bitwise_qn}.BitwiseOps.test_basic_bitwise",
            f"{bitwise_qn}.BitwiseOps.test_variable_bitwise",
            f"{bitwise_qn}.BitwiseOps.test_complex_bitwise",
            f"{bitwise_qn}.BitwiseOps.test_bitwise_metamethods",
            f"{bitwise_qn}.BitwiseOps.test_bit_algorithms",
        ]

        for expected_fn in expected_functions:
            assert expected_fn in fn_qns, f"Missing function: {expected_fn}"

        print("✅ Lua 5.4 bitwise operators test PASSED")

    except Exception as e:
        print(f"❌ Lua 5.4 bitwise operators test FAILED: {e}")
        raise
