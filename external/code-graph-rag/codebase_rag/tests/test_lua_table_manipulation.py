from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater


def test_table_construction_and_access(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Test table construction and access patterns."""
    project = temp_repo / "lua_table_construction"
    project.mkdir()

    (project / "table_access.lua").write_text(
        encoding="utf-8",
        data="""
function create_tables()
    local array = {1, 2, 3, 4, 5}
    local hash = {name = "John", age = 30, city = "NYC"}
    local mixed = {10, 20, x = "value", y = "another"}

    local first = array[1]
    local name = hash["name"]
    local alt_name = hash.name

    array[6] = 6
    hash["country"] = "USA"
    hash.email = "john@example.com"

    return {
        array = array,
        hash = hash,
        mixed = mixed
    }
end

local nested = {
    level1 = {
        level2 = {
            value = "deep"
        }
    }
}

local deep_value = nested.level1.level2.value
nested.level1.level2.new_value = "added"
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    assert len(defines_rels) >= 1, (
        f"Expected at least 1 DEFINES relationship, got {len(defines_rels)}"
    )


def test_table_iteration_patterns(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test table iteration function calls."""
    project = temp_repo / "lua_table_iteration"
    project.mkdir()

    (project / "iteration.lua").write_text(
        encoding="utf-8",
        data="""
function iterate_tables(data)
    local results = {}

    for key, value in pairs(data) do
        results[key] = value * 2
    end

    local array_sum = 0
    for index, value in ipairs(data) do
        array_sum = array_sum + value
    end

    local manual_iter = {}
    local key = next(data, nil)
    while key do
        manual_iter[key] = data[key]
        key = next(data, key)
    end

    return {
        doubled = results,
        sum = array_sum,
        manual = manual_iter
    }
end

function count_elements(tbl)
    local count = 0
    for _ in pairs(tbl) do
        count = count + 1
    end
    return count
end

local numbers = {10, 20, 30}
for i = 1, #numbers do
    print(numbers[i])
end
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    assert len(defines_rels) >= 2, (
        f"Expected at least 2 DEFINES relationships, got {len(defines_rels)}"
    )


def test_table_modification_functions(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Test table modification function calls."""
    project = temp_repo / "lua_table_modification"
    project.mkdir()

    (project / "table_mod.lua").write_text(
        encoding="utf-8",
        data="""
function modify_arrays(arr)
    table.insert(arr, "new_item")
    table.insert(arr, 2, "inserted_at_2")

    local removed = table.remove(arr)
    local removed_at_index = table.remove(arr, 1)

    table.sort(arr)
    table.sort(arr, function(a, b) return a > b end)

    return {
        modified = arr,
        last_removed = removed,
        first_removed = removed_at_index
    }
end

function join_arrays(arrays)
    local result = {}
    for _, arr in ipairs(arrays) do
        for _, value in ipairs(arr) do
            table.insert(result, value)
        end
    end

    local joined_string = table.concat(result, ", ")
    local custom_join = table.concat(result, " | ", 2, 5)

    return {
        combined = result,
        string_form = joined_string,
        partial_join = custom_join
    }
end

local data = {3, 1, 4, 1, 5}
table.sort(data)
local text = table.concat(data, "-")
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    assert len(defines_rels) >= 2, (
        f"Expected at least 2 DEFINES relationships, got {len(defines_rels)}"
    )


def test_table_utility_functions(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test table utility function calls."""
    project = temp_repo / "lua_table_utilities"
    project.mkdir()

    (project / "table_utils.lua").write_text(
        encoding="utf-8",
        data="""
function table_utilities(tbl)
    local max_n = table.maxn(tbl)

    table.foreach(tbl, function(key, value)
        print(key, value)
    end)

    table.foreachi({1, 2, 3}, function(index, value)
        print(index, value)
    end)

    local copy = {}
    table.foreach(tbl, function(k, v)
        copy[k] = v
    end)

    return {
        max_index = max_n,
        copy = copy
    }
end

function deep_copy(original)
    local copy = {}
    for key, value in pairs(original) do
        if type(value) == "table" then
            copy[key] = deep_copy(value)
        else
            copy[key] = value
        end
    end
    return copy
end

local test_table = {a = 1, b = 2, [10] = "ten"}
local max_numeric = table.maxn(test_table)
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    assert len(defines_rels) >= 2, (
        f"Expected at least 2 DEFINES relationships, got {len(defines_rels)}"
    )


def test_table_metatable_operations(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test metatable operations on tables."""
    project = temp_repo / "lua_metatables"
    project.mkdir()

    (project / "metatables.lua").write_text(
        encoding="utf-8",
        data="""
function create_vector(x, y)
    local vector = {x = x or 0, y = y or 0}

    local mt = {
        __add = function(v1, v2)
            return create_vector(v1.x + v2.x, v1.y + v2.y)
        end,
        __tostring = function(v)
            return string.format("Vector(%d, %d)", v.x, v.y)
        end,
        __index = function(t, k)
            if k == "magnitude" then
                return math.sqrt(t.x^2 + t.y^2)
            end
        end
    }

    setmetatable(vector, mt)
    return vector
end

function table_with_proxy()
    local data = {}
    local proxy = {}

    local mt = {
        __index = data,
        __newindex = function(t, k, v)
            print("Setting", k, "to", v)
            rawset(data, k, v)
        end,
        __pairs = function(t)
            return pairs(data)
        end
    }

    setmetatable(proxy, mt)
    return proxy
end

local v1 = create_vector(3, 4)
local v2 = create_vector(1, 2)
local sum = v1 + v2
local magnitude = v1.magnitude

local proxy = table_with_proxy()
proxy.name = "test"
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    assert len(defines_rels) >= 2, (
        f"Expected at least 2 DEFINES relationships, got {len(defines_rels)}"
    )


def test_table_serialization(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test table serialization operations."""
    project = temp_repo / "lua_serialization"
    project.mkdir()

    (project / "serialization.lua").write_text(
        encoding="utf-8",
        data="""
function serialize_table(tbl, indent)
    indent = indent or 0
    local spaces = string.rep("  ", indent)
    local result = "{\n"

    for key, value in pairs(tbl) do
        local key_str = type(key) == "string" and key or tostring(key)

        if type(value) == "table" then
            result = result .. spaces .. "  " .. key_str .. " = " .. serialize_table(value, indent + 1) .. ",\n"
        elseif type(value) == "string" then
            result = result .. spaces .. "  " .. key_str .. " = \"" .. value .. "\",\n"
        else
            result = result .. spaces .. "  " .. key_str .. " = " .. tostring(value) .. ",\n"
        end
    end

    result = result .. spaces .. "}"
    return result
end

function deserialize_from_string(str)
    local chunk = loadstring("return " .. str)
    if chunk then
        return chunk()
    else
        return nil
    end
end

local data = {name = "John", age = 30, nested = {city = "NYC"}}
local serialized = serialize_table(data)
local deserialized = deserialize_from_string(serialized)
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    assert len(defines_rels) >= 2, (
        f"Expected at least 2 DEFINES relationships, got {len(defines_rels)}"
    )
