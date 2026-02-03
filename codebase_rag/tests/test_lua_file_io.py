from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater


def test_file_operations(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test file I/O operations."""
    project = temp_repo / "lua_file_operations"
    project.mkdir()

    (project / "file_ops.lua").write_text(
        encoding="utf-8",
        data="""
function read_file(filename)
    local file = io.open(filename, "r")
    if not file then
        return nil, "Could not open file"
    end

    local content = file:read("*all")
    file:close()

    return content
end

function write_file(filename, data)
    local file = io.open(filename, "w")
    if not file then
        return false, "Could not create file"
    end

    file:write(data)
    file:flush()
    file:close()

    return true
end

function append_to_file(filename, data)
    local file = io.open(filename, "a")
    if file then
        file:write(data)
        file:close()
        return true
    end
    return false
end

local input_file = io.input("data.txt")
local output_file = io.output("result.txt")
local line = io.read("*line")
io.write("Processing complete\n")
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    assert len(defines_rels) >= 3, (
        f"Expected at least 3 DEFINES relationships, got {len(defines_rels)}"
    )


def test_file_reading_modes(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test different file reading modes."""
    project = temp_repo / "lua_file_reading"
    project.mkdir()

    (project / "read_modes.lua").write_text(
        encoding="utf-8",
        data="""
function read_file_modes(filename)
    local file = io.open(filename, "r")
    if not file then return nil end

    local all_content = file:read("*all")
    file:seek("set", 0)

    local one_line = file:read("*line")
    local number_val = file:read("*number")
    local ten_chars = file:read(10)

    local remaining_lines = {}
    for line in file:lines() do
        table.insert(remaining_lines, line)
    end

    file:close()

    return {
        all = all_content,
        first_line = one_line,
        number = number_val,
        chars = ten_chars,
        lines = remaining_lines
    }
end

function process_large_file(filename)
    local file = io.open(filename, "r")
    local line_count = 0

    while true do
        local line = file:read("*line")
        if not line then break end
        line_count = line_count + 1
    end

    file:close()
    return line_count
end

for line in io.lines("config.txt") do
    print("Config:", line)
end
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    assert len(defines_rels) >= 2, (
        f"Expected at least 2 DEFINES relationships, got {len(defines_rels)}"
    )


def test_file_positioning_and_info(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test file positioning and information calls."""
    project = temp_repo / "lua_file_positioning"
    project.mkdir()

    (project / "file_positioning.lua").write_text(
        encoding="utf-8",
        data="""
function file_manipulation(filename)
    local file = io.open(filename, "r+")
    if not file then return nil end

    local current_pos = file:seek()
    file:seek("set", 0)
    local start_pos = file:seek()

    file:seek("end", 0)
    local file_size = file:seek()

    file:seek("set", 10)
    local content = file:read(20)

    file:seek("cur", -5)
    local back_content = file:read(5)

    file:close()

    return {
        initial_pos = current_pos,
        start = start_pos,
        size = file_size,
        content = content,
        back_content = back_content
    }
end

function get_file_info(filename)
    local file = io.open(filename, "r")
    if not file then
        return nil
    end

    local pos = file:seek("cur")
    local size = file:seek("end")
    file:seek("set", pos)

    file:close()

    return {
        size = size,
        exists = true
    }
end

local temp = io.tmpfile()
temp:write("temporary data")
temp:seek("set", 0)
local temp_content = temp:read("*all")
temp:close()
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    assert len(defines_rels) >= 2, (
        f"Expected at least 2 DEFINES relationships, got {len(defines_rels)}"
    )


def test_serialization_patterns(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test data serialization patterns."""
    project = temp_repo / "lua_data_serialization"
    project.mkdir()

    (project / "serialization.lua").write_text(
        encoding="utf-8",
        data="""
function serialize_to_file(data, filename)
    local file = io.open(filename, "w")
    if not file then return false end

    local function serialize_value(value, indent)
        indent = indent or 0
        local spaces = string.rep("  ", indent)

        if type(value) == "table" then
            file:write("{\n")
            for k, v in pairs(value) do
                file:write(spaces .. "  ")
                if type(k) == "string" then
                    file:write(k .. " = ")
                else
                    file:write("[" .. tostring(k) .. "] = ")
                end
                serialize_value(v, indent + 1)
                file:write(",\n")
            end
            file:write(spaces .. "}")
        elseif type(value) == "string" then
            file:write(string.format("%q", value))
        else
            file:write(tostring(value))
        end
    end

    serialize_value(data)
    file:close()
    return true
end

function deserialize_from_file(filename)
    local file = io.open(filename, "r")
    if not file then return nil end

    local content = file:read("*all")
    file:close()

    local chunk = loadstring("return " .. content)
    if chunk then
        local success, data = pcall(chunk)
        return success and data or nil
    end

    return nil
end

function save_json_like(data, filename)
    local file = io.open(filename, "w")

    local function escape_string(str)
        local escaped = string.gsub(str, '["\\]', "\\%1")
        return '"' .. escaped .. '"'
    end

    local function to_json(value)
        if type(value) == "table" then
            local parts = {}
            for k, v in pairs(value) do
                local key_str = type(k) == "string" and escape_string(k) or tostring(k)
                table.insert(parts, key_str .. ":" .. to_json(v))
            end
            return "{" .. table.concat(parts, ",") .. "}"
        elseif type(value) == "string" then
            return escape_string(value)
        else
            return tostring(value)
        end
    end

    file:write(to_json(data))
    file:close()
end

serialize_to_file({name = "John", age = 30}, "person.lua")
local person = deserialize_from_file("person.lua")
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 3, (
        f"Expected at least 3 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 1, (
        f"Expected at least 1 CALLS relationship, got {len(calls_rels)}"
    )


def test_binary_file_operations(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test binary file operations."""
    project = temp_repo / "lua_binary_io"
    project.mkdir()

    (project / "binary_io.lua").write_text(
        encoding="utf-8",
        data="""
function read_binary_file(filename)
    local file = io.open(filename, "rb")
    if not file then return nil end

    local header = file:read(4)
    local size = file:read("*number") or 0
    local data = file:read(size)

    file:close()

    return {
        header = header,
        size = size,
        data = data
    }
end

function write_binary_file(filename, data)
    local file = io.open(filename, "wb")
    if not file then return false end

    file:write("BIN\001")
    file:write(string.char(#data))
    file:write(data)

    file:close()
    return true
end

function copy_binary_file(source, dest)
    local src = io.open(source, "rb")
    local dst = io.open(dest, "wb")

    if not src or not dst then
        if src then src:close() end
        if dst then dst:close() end
        return false
    end

    while true do
        local chunk = src:read(1024)
        if not chunk then break end
        dst:write(chunk)
    end

    src:close()
    dst:close()
    return true
end

local binary_data = read_binary_file("image.png")
write_binary_file("copy.png", binary_data.data)
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    assert len(defines_rels) >= 3, (
        f"Expected at least 3 DEFINES relationships, got {len(defines_rels)}"
    )
