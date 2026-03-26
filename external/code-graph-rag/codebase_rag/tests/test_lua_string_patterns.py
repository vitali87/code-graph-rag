from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater


def test_string_pattern_matching(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test user-defined string pattern processing functions."""
    project = temp_repo / "lua_patterns"
    project.mkdir()

    (project / "patterns.lua").write_text(
        encoding="utf-8",
        data="""
local pattern_utils = {}

function pattern_utils.match_pattern(text, pattern)
    -- Match pattern in text
    return string.match(text, pattern)
end

function pattern_utils.match_all_pattern(text, pattern)
    -- Match all occurrences of pattern
    local matches = {}
    for match in string.gmatch(text, pattern) do
        table.insert(matches, match)
    end
    return matches
end

function pattern_utils.find_pattern_position(text, pattern, start_pos)
    -- Find pattern position in text
    return string.find(text, pattern, start_pos or 1)
end

function pattern_utils.replace_pattern(text, pattern, replacement)
    -- Replace pattern in text
    return string.gsub(text, pattern, replacement)
end

function pattern_utils.convert_to_number(text)
    -- Convert text to number
    return tonumber(text)
end

function pattern_utils.add_to_table(table, item)
    -- Add item to table
    table.insert(table, item)
    return table
end

function validate_email(email)
    local pattern = "[%w%.%-_]+@[%w%.%-]+%.%w+"
    local match = pattern_utils.match_pattern(email, pattern)
    return match ~= nil
end

function extract_numbers(text)
    local number_strings = pattern_utils.match_all_pattern(text, "%d+")
    local numbers = {}
    for _, num_str in ipairs(number_strings) do
        local num = pattern_utils.convert_to_number(num_str)
        pattern_utils.add_to_table(numbers, num)
    end
    return numbers
end

function find_word_positions(text, word)
    local positions = {}
    local start = 1
    while true do
        local pos = pattern_utils.find_pattern_position(text, word, start)
        if not pos then break end
        pattern_utils.add_to_table(positions, pos)
        start = pos + 1
    end
    return positions
end

-- Use our pattern utilities
local phone = "123-456-7890"
local area_code = pattern_utils.match_pattern(phone, "(%d%d%d)")
local formatted = pattern_utils.replace_pattern(phone, "%-", ".")

-- Test our functions
local email_valid = validate_email("test@example.com")
local numbers = extract_numbers("I have 10 cats and 5 dogs")
local positions = find_word_positions("hello world hello", "hello")

return pattern_utils
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


def test_string_manipulation_functions(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Test user-defined string manipulation wrapper functions."""
    project = temp_repo / "lua_string_manip"
    project.mkdir()

    (project / "string_manip.lua").write_text(
        encoding="utf-8",
        data="""
local string_manip = {}

function string_manip.trim_whitespace(input)
    -- Trim leading and trailing whitespace
    return string.match(input, "^%s*(.-)%s*$")
end

function string_manip.reverse_string(input)
    -- Reverse string
    return string.reverse(input)
end

function string_manip.repeat_string(str, count)
    -- Repeat string multiple times
    return string.rep(str, count)
end

function string_manip.get_string_bytes(input, start_pos, end_pos)
    -- Get byte values of string characters
    return {string.byte(input, start_pos or 1, end_pos or -1)}
end

function string_manip.create_string_from_bytes(...)
    -- Create string from byte values
    return string.char(...)
end

function string_manip.get_string_length(input)
    -- Get string length
    return string.len(input)
end

function string_manip.get_substring(input, start_pos, end_pos)
    -- Get substring
    return string.sub(input, start_pos, end_pos)
end

function string_manip.replace_with_pattern(text, pattern, replacement)
    -- Replace pattern in text
    return string.gsub(text, pattern, replacement)
end

function string_manip.to_uppercase(text)
    -- Convert to uppercase
    return string.upper(text)
end

function string_manip.to_lowercase(text)
    -- Convert to lowercase
    return string.lower(text)
end

function string_manip.format_string(format, ...)
    -- Format string with values
    return string.format(format, ...)
end

function text_processing(input)
    local trimmed = string_manip.trim_whitespace(input)
    local reversed = string_manip.reverse_string(input)
    local repeated = string_manip.repeat_string("*", 10)

    local bytes = string_manip.get_string_bytes(input, 1, -1)
    local chars = string_manip.create_string_from_bytes(65, 66, 67)

    local length = string_manip.get_string_length(input)
    local substring = string_manip.get_substring(input, 2, -2)

    return {
        trimmed = trimmed,
        reversed = reversed,
        repeated = repeated,
        bytes = bytes,
        chars = chars,
        length = length,
        substring = substring
    }
end

function replace_patterns(text)
    local no_digits = string_manip.replace_with_pattern(text, "%d", "X")
    local no_spaces = string_manip.replace_with_pattern(text, "%s+", "_")

    local capitalized = string_manip.replace_with_pattern(text, "(%w)(%w*)", function(first, rest)
        return string_manip.to_uppercase(first) .. string_manip.to_lowercase(rest)
    end)
    return capitalized
end

-- Use our string manipulation functions
local test_input = "  hello world  "
local processed = text_processing(test_input)
local replaced = replace_patterns("hello 123 world")
local formatted_text = string_manip.format_string("Value: %d, Percent: %.2f%%", 42, 87.5)

return string_manip
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 13, (
        f"Expected at least 13 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 10, (
        f"Expected at least 10 CALLS relationships, got {len(calls_rels)}"
    )


def test_complex_pattern_operations(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test user-defined complex pattern processing functions."""
    project = temp_repo / "lua_complex_patterns"
    project.mkdir()

    (project / "complex_patterns.lua").write_text(
        encoding="utf-8",
        data="""
local complex_patterns = {}

function complex_patterns.extract_with_pattern(text, pattern)
    -- Extract text using pattern
    return string.match(text, pattern)
end

function complex_patterns.extract_all_with_pattern(text, pattern)
    -- Extract all matches with pattern
    local matches = {}
    for match in string.gmatch(text, pattern) do
        table.insert(matches, match)
    end
    return matches
end

function complex_patterns.escape_special_chars(input, char_pattern, replacement)
    -- Escape special characters in input
    return string.gsub(input, char_pattern, replacement)
end

function complex_patterns.add_to_collection(collection, item)
    -- Add item to collection
    table.insert(collection, item)
    return collection
end

function complex_patterns.trim_field(field)
    -- Trim whitespace from field
    return string.match(field, "^%s*(.-)%s*$")
end

function parse_log_entry(log_line)
    local timestamp_pattern = "(%d%d%d%d%-%d%d%-%d%d %d%d:%d%d:%d%d)"
    local level_pattern = "%[(%w+)%]"
    local message_pattern = ":%s*(.+)$"

    local timestamp = complex_patterns.extract_with_pattern(log_line, timestamp_pattern)
    local level = complex_patterns.extract_with_pattern(log_line, level_pattern)
    local message = complex_patterns.extract_with_pattern(log_line, message_pattern)

    return {
        timestamp = timestamp,
        level = level,
        message = message
    }
end

function extract_urls(text)
    local url_pattern = "https?://[%w%.%-_/%%?=&]+"
    return complex_patterns.extract_all_with_pattern(text, url_pattern)
end

function sanitize_input(input)
    local escaped = complex_patterns.escape_special_chars(
        input,
        "([%^%$%(%)%%%.%[%]%*%+%-%?])",
        "%%%1"
    )
    local safe = complex_patterns.escape_special_chars(escaped, "%c", "")
    return safe
end

function parse_csv_line(csv_line)
    local fields = complex_patterns.extract_all_with_pattern(csv_line, "([^,]+)")
    local trimmed_fields = {}

    for _, field in ipairs(fields) do
        local trimmed = complex_patterns.trim_field(field)
        complex_patterns.add_to_collection(trimmed_fields, trimmed)
    end

    return trimmed_fields
end

-- Use our complex pattern functions
local log_data = parse_log_entry("2023-01-01 10:00:00 [INFO]: Application started")
local urls = extract_urls("Visit https://example.com and http://test.org")
local safe_input = sanitize_input("user[input]")
local csv_fields = parse_csv_line("name, age, city")

return complex_patterns
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


def test_unicode_and_encoding(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test user-defined Unicode processing wrapper functions."""
    project = temp_repo / "lua_unicode"
    project.mkdir()

    (project / "unicode.lua").write_text(
        encoding="utf-8",
        data="""
local unicode_utils = {}
local utf8 = require("utf8")

function unicode_utils.get_utf8_length(text)
    -- Get UTF-8 character count
    return utf8.len(text)
end

function unicode_utils.get_codepoint_at(text, position)
    -- Get codepoint at position
    return utf8.codepoint(text, position)
end

function unicode_utils.create_char_from_codepoint(codepoint)
    -- Create character from codepoint
    return utf8.char(codepoint)
end

function unicode_utils.iterate_codepoints(text, processor_func)
    -- Iterate over codepoints in text
    for pos, code in utf8.codes(text) do
        processor_func(pos, code)
    end
end

function unicode_utils.get_byte_offset(text, char_position)
    -- Get byte offset for character position
    return utf8.offset(text, char_position)
end

function unicode_utils.format_codepoint(pos, code)
    -- Format codepoint for display
    return string.format("Position %d: U+%04X", pos, code)
end

function unicode_utils.print_codepoint_info(pos, code)
    -- Print codepoint information
    local formatted = unicode_utils.format_codepoint(pos, code)
    print(formatted)
end

function handle_unicode(text)
    local char_count = unicode_utils.get_utf8_length(text)
    local codepoint = unicode_utils.get_codepoint_at(text, 1)
    local char = unicode_utils.create_char_from_codepoint(0x1F4A9)

    unicode_utils.iterate_codepoints(text, unicode_utils.print_codepoint_info)

    local offset = unicode_utils.get_byte_offset(text, 5)

    return {
        length = char_count,
        first_code = codepoint,
        emoji = char,
        fifth_offset = offset
    }
end

-- Use our unicode utilities
local result = handle_unicode("Hello 🌍")
local valid_utf8 = unicode_utils.get_utf8_length("Hello 🌍")

return unicode_utils
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    imports_rels = get_relationships(mock_ingestor, "IMPORTS")

    assert len(defines_rels) >= 7, (
        f"Expected at least 7 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 5, (
        f"Expected at least 5 CALLS relationships, got {len(calls_rels)}"
    )

    assert len(imports_rels) >= 1, (
        f"Expected at least 1 IMPORTS relationship, got {len(imports_rels)}"
    )
