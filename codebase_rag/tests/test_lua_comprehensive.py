from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import run_updater


def test_lua_pcall_patterns(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test pcall error handling patterns."""
    project = temp_repo / "lua_pcall"
    project.mkdir()

    (project / "error_handler.lua").write_text(
        encoding="utf-8",
        data="""
local ErrorHandler = {}

function ErrorHandler.safe_divide(a, b)
    if b == 0 then
        error("Division by zero")
    end
    return a / b
end

function ErrorHandler.try_divide(a, b)
    local success, result = pcall(ErrorHandler.safe_divide, a, b)
    if success then
        return result
    else
        return nil, result
    end
end

function ErrorHandler.with_retry(func, max_attempts)
    for attempt = 1, max_attempts do
        local success, result = pcall(func)
        if success then
            return result
        elseif attempt == max_attempts then
            error("Function failed after " .. max_attempts .. " attempts")
        end
    end
end

return ErrorHandler
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_xpcall_patterns(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test xpcall with custom error handlers."""
    project = temp_repo / "lua_xpcall"
    project.mkdir()

    (project / "error_handler.lua").write_text(
        encoding="utf-8",
        data="""
local ErrorHandler = {}

function ErrorHandler.error_handler(err)
    local trace = debug.traceback(err, 2)
    return {error = err, trace = trace, timestamp = os.time()}
end

function ErrorHandler.safe_call(func, ...)
    return xpcall(func, ErrorHandler.error_handler, ...)
end

function ErrorHandler.risky_function(x)
    if x < 0 then
        error("Negative input not allowed")
    end
    return math.sqrt(x)
end

return ErrorHandler
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_string_patterns(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua string pattern matching."""
    project = temp_repo / "lua_strings"
    project.mkdir()

    (project / "string_utils.lua").write_text(
        encoding="utf-8",
        data="""
local StringUtils = {}

function StringUtils.extract_emails(text)
    local emails = {}
    for email in string.gmatch(text, "[%w%.%-_]+@[%w%.%-_]+%.%w+") do
        table.insert(emails, email)
    end
    return emails
end

function StringUtils.validate_phone(phone)
    return string.match(phone, "^%d%d%d%-%d%d%d%-%d%d%d%d$") ~= nil
end

function StringUtils.capitalize_words(text)
    return string.gsub(text, "(%a)([%w_']*)", function(first, rest)
        return string.upper(first) .. string.lower(rest)
    end)
end

function StringUtils.strip_html(text)
    return string.gsub(text, "<[^>]*>", "")
end

function StringUtils.word_count(text)
    local count = 0
    for word in string.gmatch(text, "%S+") do
        count = count + 1
    end
    return count
end

return StringUtils
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_string_interpolation(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test string formatting and interpolation."""
    project = temp_repo / "lua_interpolation"
    project.mkdir()

    (project / "formatter.lua").write_text(
        encoding="utf-8",
        data="""
local Formatter = {}

function Formatter.template(template_str, values)
    return string.gsub(template_str, "{(%w+)}", values)
end

function Formatter.sprintf_like(format, ...)
    return string.format(format, ...)
end

function Formatter.currency(amount, currency)
    return string.format("%.2f %s", amount, currency or "USD")
end

function Formatter.pad_left(str, width, char)
    char = char or " "
    local padding = string.rep(char, width - #str)
    return padding .. str
end

function Formatter.pad_right(str, width, char)
    char = char or " "
    local padding = string.rep(char, width - #str)
    return str .. padding
end

return Formatter
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_table_operations(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test advanced table operations."""
    project = temp_repo / "lua_tables"
    project.mkdir()

    (project / "table_ops.lua").write_text(
        encoding="utf-8",
        data="""
local TableOps = {}

function TableOps.deep_copy(orig)
    local copy
    if type(orig) == 'table' then
        copy = {}
        for k, v in next, orig, nil do
            copy[TableOps.deep_copy(k)] = TableOps.deep_copy(v)
        end
        setmetatable(copy, TableOps.deep_copy(getmetatable(orig)))
    else
        copy = orig
    end
    return copy
end

function TableOps.merge(t1, t2)
    local result = TableOps.deep_copy(t1)
    for k, v in pairs(t2) do
        result[k] = v
    end
    return result
end

function TableOps.keys(t)
    local keys = {}
    for k in pairs(t) do
        table.insert(keys, k)
    end
    return keys
end

function TableOps.values(t)
    local values = {}
    for _, v in pairs(t) do
        table.insert(values, v)
    end
    return values
end

function TableOps.find(t, predicate)
    for k, v in pairs(t) do
        if predicate(v, k) then
            return v, k
        end
    end
    return nil
end

return TableOps
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_table_iteration(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test table iteration patterns."""
    project = temp_repo / "lua_iteration"
    project.mkdir()

    (project / "iterators.lua").write_text(
        encoding="utf-8",
        data="""
local Iterators = {}

function Iterators.ipairs_reverse(t)
    local i = #t + 1
    return function()
        i = i - 1
        if i > 0 then
            return i, t[i]
        end
    end
end

function Iterators.sorted_pairs(t, sort_func)
    local keys = {}
    for k in pairs(t) do
        table.insert(keys, k)
    end
    table.sort(keys, sort_func)

    local i = 0
    return function()
        i = i + 1
        local k = keys[i]
        if k then
            return k, t[k]
        end
    end
end

function Iterators.filtered_pairs(t, predicate)
    local next_key, next_value = next(t)
    return function()
        while next_key do
            local key, value = next_key, next_value
            next_key, next_value = next(t, key)
            if predicate(value, key) then
                return key, value
            end
        end
    end
end

function Iterators.enumerate(t)
    local index = 0
    local iter = pairs(t)
    return function()
        local k, v = iter()
        if k then
            index = index + 1
            return index, k, v
        end
    end
end

return Iterators
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_environment_management(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test environment and global variable management."""
    project = temp_repo / "lua_environment"
    project.mkdir()

    (project / "env_manager.lua").write_text(
        encoding="utf-8",
        data="""
local EnvManager = {}

function EnvManager.create_sandbox()
    local sandbox = {
        print = print,
        pairs = pairs,
        ipairs = ipairs,
        next = next,
        type = type,
        tostring = tostring,
        tonumber = tonumber,
        math = math,
        string = string,
        table = table,
    }
    return sandbox
end

function EnvManager.run_in_sandbox(code, sandbox)
    sandbox = sandbox or EnvManager.create_sandbox()
    local func, err = load(code, "sandbox", "t", sandbox)
    if func then
        return pcall(func)
    else
        return false, err
    end
end

function EnvManager.get_globals()
    local globals = {}
    for k, v in pairs(_G) do
        globals[k] = v
    end
    return globals
end

function EnvManager.backup_global(name)
    return _G[name]
end

function EnvManager.restore_global(name, value)
    _G[name] = value
end

return EnvManager
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_module_system(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test advanced module system patterns."""
    project = temp_repo / "lua_modules"
    project.mkdir()

    (project / "module_loader.lua").write_text(
        encoding="utf-8",
        data="""
local ModuleLoader = {}

function ModuleLoader.create_module(name, init_func)
    local module = {_NAME = name, _VERSION = "1.0"}
    if init_func then
        init_func(module)
    end
    return module
end

function ModuleLoader.lazy_require(module_name)
    local cached_module = nil
    return function()
        if not cached_module then
            cached_module = require(module_name)
        end
        return cached_module
    end
end

function ModuleLoader.conditional_require(module_name, condition)
    if condition then
        return require(module_name)
    else
        return nil
    end
end

function ModuleLoader.safe_require(module_name)
    local success, module = pcall(require, module_name)
    if success then
        return module
    else
        return nil, module
    end
end

return ModuleLoader
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_file_operations(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test file I/O operations."""
    project = temp_repo / "lua_file_io"
    project.mkdir()

    (project / "file_utils.lua").write_text(
        encoding="utf-8",
        data="""
local FileUtils = {}

function FileUtils.read_file(filename)
    local file, err = io.open(filename, "r")
    if not file then
        return nil, err
    end
    local content = file:read("*all")
    file:close()
    return content
end

function FileUtils.write_file(filename, content)
    local file, err = io.open(filename, "w")
    if not file then
        return false, err
    end
    file:write(content)
    file:close()
    return true
end

function FileUtils.append_file(filename, content)
    local file, err = io.open(filename, "a")
    if not file then
        return false, err
    end
    file:write(content)
    file:close()
    return true
end

function FileUtils.read_lines(filename)
    local lines = {}
    local file = io.open(filename, "r")
    if file then
        for line in file:lines() do
            table.insert(lines, line)
        end
        file:close()
    end
    return lines
end

function FileUtils.file_exists(filename)
    local file = io.open(filename, "r")
    if file then
        file:close()
        return true
    end
    return false
end

return FileUtils
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_json_serialization(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test JSON serialization patterns."""
    project = temp_repo / "lua_json"
    project.mkdir()

    (project / "json_utils.lua").write_text(
        encoding="utf-8",
        data="""
local JSONUtils = {}

function JSONUtils.encode_value(value)
    local value_type = type(value)

    if value_type == "nil" then
        return "null"
    elseif value_type == "boolean" then
        return tostring(value)
    elseif value_type == "number" then
        return tostring(value)
    elseif value_type == "string" then
        return '"' .. string.gsub(value, '"', '\\"') .. '"'
    elseif value_type == "table" then
        return JSONUtils.encode_table(value)
    else
        error("Unsupported type: " .. value_type)
    end
end

function JSONUtils.encode_table(t)
    local is_array = true
    local max_index = 0

    for k, v in pairs(t) do
        if type(k) ~= "number" or k <= 0 or k ~= math.floor(k) then
            is_array = false
            break
        end
        max_index = math.max(max_index, k)
    end

    if is_array then
        local parts = {}
        for i = 1, max_index do
            table.insert(parts, JSONUtils.encode_value(t[i]))
        end
        return "[" .. table.concat(parts, ",") .. "]"
    else
        local parts = {}
        for k, v in pairs(t) do
            local key = '"' .. tostring(k) .. '"'
            local value = JSONUtils.encode_value(v)
            table.insert(parts, key .. ":" .. value)
        end
        return "{" .. table.concat(parts, ",") .. "}"
    end
end

function JSONUtils.encode(value)
    return JSONUtils.encode_value(value)
end

function JSONUtils.decode(json_str)
    -- Simplified decoder for demo purposes
    local success, result = pcall(load("return " .. json_str))
    if success then
        return result
    else
        error("Invalid JSON")
    end
end

return JSONUtils
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_linked_list(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test linked list implementation."""
    project = temp_repo / "lua_linked_list"
    project.mkdir()

    (project / "linked_list.lua").write_text(
        encoding="utf-8",
        data="""
local LinkedList = {}
LinkedList.__index = LinkedList

function LinkedList:new()
    return setmetatable({head = nil, tail = nil, size = 0}, LinkedList)
end

function LinkedList:push_front(value)
    local node = {value = value, next = self.head, prev = nil}
    if self.head then
        self.head.prev = node
    else
        self.tail = node
    end
    self.head = node
    self.size = self.size + 1
end

function LinkedList:push_back(value)
    local node = {value = value, next = nil, prev = self.tail}
    if self.tail then
        self.tail.next = node
    else
        self.head = node
    end
    self.tail = node
    self.size = self.size + 1
end

function LinkedList:pop_front()
    if not self.head then return nil end
    local value = self.head.value
    self.head = self.head.next
    if self.head then
        self.head.prev = nil
    else
        self.tail = nil
    end
    self.size = self.size - 1
    return value
end

function LinkedList:pop_back()
    if not self.tail then return nil end
    local value = self.tail.value
    self.tail = self.tail.prev
    if self.tail then
        self.tail.next = nil
    else
        self.head = nil
    end
    self.size = self.size - 1
    return value
end

function LinkedList:to_array()
    local result = {}
    local current = self.head
    while current do
        table.insert(result, current.value)
        current = current.next
    end
    return result
end

return LinkedList
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_binary_tree(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test binary tree implementation."""
    project = temp_repo / "lua_binary_tree"
    project.mkdir()

    (project / "binary_tree.lua").write_text(
        encoding="utf-8",
        data="""
local BinaryTree = {}
BinaryTree.__index = BinaryTree

function BinaryTree:new()
    return setmetatable({root = nil}, BinaryTree)
end

function BinaryTree:insert(value)
    if not self.root then
        self.root = {value = value, left = nil, right = nil}
    else
        self:_insert_node(self.root, value)
    end
end

function BinaryTree:_insert_node(node, value)
    if value < node.value then
        if node.left then
            self:_insert_node(node.left, value)
        else
            node.left = {value = value, left = nil, right = nil}
        end
    else
        if node.right then
            self:_insert_node(node.right, value)
        else
            node.right = {value = value, left = nil, right = nil}
        end
    end
end

function BinaryTree:search(value)
    return self:_search_node(self.root, value)
end

function BinaryTree:_search_node(node, value)
    if not node then
        return false
    elseif node.value == value then
        return true
    elseif value < node.value then
        return self:_search_node(node.left, value)
    else
        return self:_search_node(node.right, value)
    end
end

function BinaryTree:in_order()
    local result = {}
    self:_in_order_traversal(self.root, result)
    return result
end

function BinaryTree:_in_order_traversal(node, result)
    if node then
        self:_in_order_traversal(node.left, result)
        table.insert(result, node.value)
        self:_in_order_traversal(node.right, result)
    end
end

return BinaryTree
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_hash_table(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test hash table implementation."""
    project = temp_repo / "lua_hash_table"
    project.mkdir()

    (project / "hash_table.lua").write_text(
        encoding="utf-8",
        data="""
local HashTable = {}
HashTable.__index = HashTable

function HashTable:new(capacity)
    local obj = setmetatable({
        capacity = capacity or 16,
        size = 0,
        buckets = {}
    }, HashTable)
    for i = 1, obj.capacity do
        obj.buckets[i] = {}
    end
    return obj
end

function HashTable:_hash(key)
    local hash = 0
    for i = 1, #key do
        hash = (hash * 31 + string.byte(key, i)) % self.capacity
    end
    return hash + 1  -- Lua arrays are 1-indexed
end

function HashTable:put(key, value)
    local bucket_index = self:_hash(tostring(key))
    local bucket = self.buckets[bucket_index]

    for i, pair in ipairs(bucket) do
        if pair.key == key then
            pair.value = value
            return
        end
    end

    table.insert(bucket, {key = key, value = value})
    self.size = self.size + 1
end

function HashTable:get(key)
    local bucket_index = self:_hash(tostring(key))
    local bucket = self.buckets[bucket_index]

    for _, pair in ipairs(bucket) do
        if pair.key == key then
            return pair.value
        end
    end

    return nil
end

function HashTable:remove(key)
    local bucket_index = self:_hash(tostring(key))
    local bucket = self.buckets[bucket_index]

    for i, pair in ipairs(bucket) do
        if pair.key == key then
            table.remove(bucket, i)
            self.size = self.size - 1
            return pair.value
        end
    end

    return nil
end

function HashTable:keys()
    local keys = {}
    for _, bucket in ipairs(self.buckets) do
        for _, pair in ipairs(bucket) do
            table.insert(keys, pair.key)
        end
    end
    return keys
end

return HashTable
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_sorting_algorithms(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test various sorting algorithms."""
    project = temp_repo / "lua_sorting"
    project.mkdir()

    (project / "sorting.lua").write_text(
        encoding="utf-8",
        data="""
local Sorting = {}

function Sorting.bubble_sort(arr)
    local n = #arr
    for i = 1, n do
        for j = 1, n - i do
            if arr[j] > arr[j + 1] then
                arr[j], arr[j + 1] = arr[j + 1], arr[j]
            end
        end
    end
    return arr
end

function Sorting.quick_sort(arr, low, high)
    low = low or 1
    high = high or #arr

    if low < high then
        local pi = Sorting._partition(arr, low, high)
        Sorting.quick_sort(arr, low, pi - 1)
        Sorting.quick_sort(arr, pi + 1, high)
    end
    return arr
end

function Sorting._partition(arr, low, high)
    local pivot = arr[high]
    local i = low - 1

    for j = low, high - 1 do
        if arr[j] < pivot then
            i = i + 1
            arr[i], arr[j] = arr[j], arr[i]
        end
    end

    arr[i + 1], arr[high] = arr[high], arr[i + 1]
    return i + 1
end

function Sorting.merge_sort(arr)
    if #arr <= 1 then
        return arr
    end

    local mid = math.floor(#arr / 2)
    local left = {}
    local right = {}

    for i = 1, mid do
        left[i] = arr[i]
    end
    for i = mid + 1, #arr do
        right[i - mid] = arr[i]
    end

    left = Sorting.merge_sort(left)
    right = Sorting.merge_sort(right)

    return Sorting._merge(left, right)
end

function Sorting._merge(left, right)
    local result = {}
    local i, j = 1, 1

    while i <= #left and j <= #right do
        if left[i] <= right[j] then
            table.insert(result, left[i])
            i = i + 1
        else
            table.insert(result, right[j])
            j = j + 1
        end
    end

    while i <= #left do
        table.insert(result, left[i])
        i = i + 1
    end

    while j <= #right do
        table.insert(result, right[j])
        j = j + 1
    end

    return result
end

return Sorting
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_search_algorithms(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test search algorithms."""
    project = temp_repo / "lua_searching"
    project.mkdir()

    (project / "searching.lua").write_text(
        encoding="utf-8",
        data="""
local Searching = {}

function Searching.linear_search(arr, target)
    for i, value in ipairs(arr) do
        if value == target then
            return i
        end
    end
    return -1
end

function Searching.binary_search(arr, target)
    local left, right = 1, #arr

    while left <= right do
        local mid = math.floor((left + right) / 2)
        if arr[mid] == target then
            return mid
        elseif arr[mid] < target then
            left = mid + 1
        else
            right = mid - 1
        end
    end

    return -1
end

function Searching.interpolation_search(arr, target)
    local low, high = 1, #arr

    while low <= high and target >= arr[low] and target <= arr[high] do
        if low == high then
            if arr[low] == target then
                return low
            end
            return -1
        end

        local pos = low + math.floor(((target - arr[low]) / (arr[high] - arr[low])) * (high - low))

        if arr[pos] == target then
            return pos
        elseif arr[pos] < target then
            low = pos + 1
        else
            high = pos - 1
        end
    end

    return -1
end

function Searching.find_all(arr, target)
    local indices = {}
    for i, value in ipairs(arr) do
        if value == target then
            table.insert(indices, i)
        end
    end
    return indices
end

function Searching.find_min_max(arr)
    if #arr == 0 then
        return nil, nil
    end

    local min, max = arr[1], arr[1]
    for i = 2, #arr do
        if arr[i] < min then
            min = arr[i]
        elseif arr[i] > max then
            max = arr[i]
        end
    end

    return min, max
end

return Searching
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_observer_pattern(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test observer design pattern."""
    project = temp_repo / "lua_observer"
    project.mkdir()

    (project / "observer.lua").write_text(
        encoding="utf-8",
        data="""
local Subject = {}
Subject.__index = Subject

function Subject:new()
    return setmetatable({observers = {}}, Subject)
end

function Subject:attach(observer)
    table.insert(self.observers, observer)
end

function Subject:detach(observer)
    for i, obs in ipairs(self.observers) do
        if obs == observer then
            table.remove(self.observers, i)
            break
        end
    end
end

function Subject:notify(...)
    for _, observer in ipairs(self.observers) do
        if observer.update then
            observer:update(...)
        end
    end
end

local Observer = {}
Observer.__index = Observer

function Observer:new(name, callback)
    return setmetatable({name = name, callback = callback}, Observer)
end

function Observer:update(...)
    if self.callback then
        self.callback(self, ...)
    end
end

return {Subject = Subject, Observer = Observer}
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_factory_pattern(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test factory design pattern."""
    project = temp_repo / "lua_factory"
    project.mkdir()

    (project / "factory.lua").write_text(
        encoding="utf-8",
        data="""
local ShapeFactory = {}

local Circle = {}
Circle.__index = Circle

function Circle:new(radius)
    return setmetatable({radius = radius, type = "circle"}, Circle)
end

function Circle:area()
    return math.pi * self.radius * self.radius
end

local Rectangle = {}
Rectangle.__index = Rectangle

function Rectangle:new(width, height)
    return setmetatable({width = width, height = height, type = "rectangle"}, Rectangle)
end

function Rectangle:area()
    return self.width * self.height
end

local Triangle = {}
Triangle.__index = Triangle

function Triangle:new(base, height)
    return setmetatable({base = base, height = height, type = "triangle"}, Triangle)
end

function Triangle:area()
    return 0.5 * self.base * self.height
end

function ShapeFactory.create_shape(shape_type, ...)
    if shape_type == "circle" then
        return Circle:new(...)
    elseif shape_type == "rectangle" then
        return Rectangle:new(...)
    elseif shape_type == "triangle" then
        return Triangle:new(...)
    else
        error("Unknown shape type: " .. tostring(shape_type))
    end
end

function ShapeFactory.get_available_types()
    return {"circle", "rectangle", "triangle"}
end

return {
    ShapeFactory = ShapeFactory,
    Circle = Circle,
    Rectangle = Rectangle,
    Triangle = Triangle
}
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_strategy_pattern(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test strategy design pattern."""
    project = temp_repo / "lua_strategy"
    project.mkdir()

    (project / "strategy.lua").write_text(
        encoding="utf-8",
        data="""
local SortContext = {}
SortContext.__index = SortContext

function SortContext:new(strategy)
    return setmetatable({strategy = strategy}, SortContext)
end

function SortContext:set_strategy(strategy)
    self.strategy = strategy
end

function SortContext:sort(data)
    if self.strategy and self.strategy.sort then
        return self.strategy:sort(data)
    else
        error("No sorting strategy set")
    end
end

local BubbleSortStrategy = {}
BubbleSortStrategy.__index = BubbleSortStrategy

function BubbleSortStrategy:new()
    return setmetatable({name = "Bubble Sort"}, BubbleSortStrategy)
end

function BubbleSortStrategy:sort(arr)
    local n = #arr
    local result = {}
    for i = 1, n do result[i] = arr[i] end

    for i = 1, n do
        for j = 1, n - i do
            if result[j] > result[j + 1] then
                result[j], result[j + 1] = result[j + 1], result[j]
            end
        end
    end
    return result
end

local QuickSortStrategy = {}
QuickSortStrategy.__index = QuickSortStrategy

function QuickSortStrategy:new()
    return setmetatable({name = "Quick Sort"}, QuickSortStrategy)
end

function QuickSortStrategy:sort(arr)
    local result = {}
    for i = 1, #arr do result[i] = arr[i] end
    return self:_quick_sort(result, 1, #result)
end

function QuickSortStrategy:_quick_sort(arr, low, high)
    if low < high then
        local pi = self:_partition(arr, low, high)
        self:_quick_sort(arr, low, pi - 1)
        self:_quick_sort(arr, pi + 1, high)
    end
    return arr
end

function QuickSortStrategy:_partition(arr, low, high)
    local pivot = arr[high]
    local i = low - 1

    for j = low, high - 1 do
        if arr[j] < pivot then
            i = i + 1
            arr[i], arr[j] = arr[j], arr[i]
        end
    end

    arr[i + 1], arr[high] = arr[high], arr[i + 1]
    return i + 1
end

return {
    SortContext = SortContext,
    BubbleSortStrategy = BubbleSortStrategy,
    QuickSortStrategy = QuickSortStrategy
}
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_memory_management(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test memory management patterns."""
    project = temp_repo / "lua_memory"
    project.mkdir()

    (project / "memory_manager.lua").write_text(
        encoding="utf-8",
        data="""
local MemoryManager = {}

function MemoryManager.get_memory_usage()
    return collectgarbage("count")
end

function MemoryManager.force_gc()
    collectgarbage("collect")
end

function MemoryManager.set_gc_params(pause, step_multiplier)
    collectgarbage("setpause", pause or 200)
    collectgarbage("setstepmul", step_multiplier or 200)
end

function MemoryManager.create_object_pool(create_func, reset_func)
    local pool = {objects = {}, create_func = create_func, reset_func = reset_func}

    function pool:acquire()
        local obj = table.remove(self.objects)
        if not obj then
            obj = self.create_func()
        end
        return obj
    end

    function pool:release(obj)
        if self.reset_func then
            self.reset_func(obj)
        end
        table.insert(self.objects, obj)
    end

    function pool:size()
        return #self.objects
    end

    return pool
end

function MemoryManager.profile_memory(func, ...)
    collectgarbage("collect")
    local before = collectgarbage("count")

    local results = {func(...)}

    collectgarbage("collect")
    local after = collectgarbage("count")

    return {
        results = results,
        memory_used = after - before,
        before_kb = before,
        after_kb = after
    }
end

return MemoryManager
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_performance_utils(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test performance measurement utilities."""
    project = temp_repo / "lua_performance"
    project.mkdir()

    (project / "performance.lua").write_text(
        encoding="utf-8",
        data="""
local Performance = {}

function Performance.benchmark(func, iterations, ...)
    iterations = iterations or 1000
    local args = {...}

    local start_time = os.clock()
    for i = 1, iterations do
        func(unpack(args))
    end
    local end_time = os.clock()

    return {
        total_time = end_time - start_time,
        average_time = (end_time - start_time) / iterations,
        iterations = iterations
    }
end

function Performance.compare_functions(functions, iterations, ...)
    local results = {}
    local args = {...}

    for name, func in pairs(functions) do
        results[name] = Performance.benchmark(func, iterations, unpack(args))
    end

    return results
end

function Performance.time_function(func, ...)
    local start_time = os.clock()
    local results = {func(...)}
    local end_time = os.clock()

    return {
        results = results,
        execution_time = end_time - start_time
    }
end

function Performance.profile_calls(func, ...)
    local call_count = 0
    local original_func = func

    local profiled_func = function(...)
        call_count = call_count + 1
        return original_func(...)
    end

    local result = profiled_func(...)

    return {
        result = result,
        call_count = call_count
    }
end

function Performance.memoize_with_stats(func)
    local cache = {}
    local stats = {hits = 0, misses = 0}

    local memoized = function(...)
        local key = table.concat({...}, ",")
        if cache[key] then
            stats.hits = stats.hits + 1
            return cache[key]
        else
            stats.misses = stats.misses + 1
            local result = func(...)
            cache[key] = result
            return result
        end
    end

    memoized.get_stats = function() return stats end
    memoized.clear_cache = function() cache = {}; stats = {hits = 0, misses = 0} end

    return memoized
end

return Performance
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_web_framework(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test web framework-like patterns."""
    project = temp_repo / "lua_web_framework"
    project.mkdir()

    (project / "web_framework.lua").write_text(
        encoding="utf-8",
        data="""
local WebFramework = {}

function WebFramework.create_app()
    local app = {
        routes = {},
        middleware = {},
        config = {}
    }

    function app:get(path, handler)
        self.routes["GET " .. path] = handler
    end

    function app:post(path, handler)
        self.routes["POST " .. path] = handler
    end

    function app:use(middleware)
        table.insert(self.middleware, middleware)
    end

    function app:handle_request(request)
        local context = {request = request, response = {}}

        -- Apply middleware
        for _, middleware in ipairs(self.middleware) do
            middleware(context)
        end

        -- Find and execute route handler
        local route_key = request.method .. " " .. request.path
        local handler = self.routes[route_key]

        if handler then
            return handler(context)
        else
            context.response.status = 404
            context.response.body = "Not Found"
            return context.response
        end
    end

    return app
end

function WebFramework.json_middleware()
    return function(context)
        context.json = function(data)
            context.response.headers = context.response.headers or {}
            context.response.headers["Content-Type"] = "application/json"
            context.response.body = WebFramework._encode_json(data)
        end
    end
end

function WebFramework.cors_middleware(options)
    options = options or {}
    return function(context)
        context.response.headers = context.response.headers or {}
        context.response.headers["Access-Control-Allow-Origin"] = options.origin or "*"
        context.response.headers["Access-Control-Allow-Methods"] = options.methods or "GET,POST,PUT,DELETE"
    end
end

function WebFramework._encode_json(data)
    -- Simplified JSON encoder
    if type(data) == "table" then
        local parts = {}
        for k, v in pairs(data) do
            table.insert(parts, '"' .. k .. '":' .. WebFramework._encode_json(v))
        end
        return "{" .. table.concat(parts, ",") .. "}"
    elseif type(data) == "string" then
        return '"' .. data .. '"'
    else
        return tostring(data)
    end
end

return WebFramework
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_database_orm(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test ORM-like database patterns."""
    project = temp_repo / "lua_orm"
    project.mkdir()

    (project / "orm.lua").write_text(
        encoding="utf-8",
        data="""
local ORM = {}

function ORM.create_model(table_name, schema)
    local model = {
        table_name = table_name,
        schema = schema,
        instances = {}
    }

    function model:new(data)
        local instance = {_model = model, _data = {}}

        -- Validate and set data
        for field, value in pairs(data or {}) do
            if schema[field] then
                instance._data[field] = value
            end
        end

        -- Add accessor methods
        for field, field_type in pairs(schema) do
            instance["get_" .. field] = function(self)
                return self._data[field]
            end

            instance["set_" .. field] = function(self, value)
                if field_type == "string" and type(value) ~= "string" then
                    error("Field " .. field .. " must be a string")
                elseif field_type == "number" and type(value) ~= "number" then
                    error("Field " .. field .. " must be a number")
                end
                self._data[field] = value
                return self
            end
        end

        function instance:save()
            -- Simulate database save
            table.insert(model.instances, self)
            return self
        end

        function instance:to_table()
            return self._data
        end

        return instance
    end

    function model:find_all()
        return self.instances
    end

    function model:find_by(field, value)
        local results = {}
        for _, instance in ipairs(self.instances) do
            if instance._data[field] == value then
                table.insert(results, instance)
            end
        end
        return results
    end

    function model:count()
        return #self.instances
    end

    return model
end

function ORM.create_query_builder(model)
    local query = {
        model = model,
        conditions = {},
        order_by = nil,
        limit_count = nil
    }

    function query:where(field, operator, value)
        table.insert(self.conditions, {field = field, operator = operator, value = value})
        return self
    end

    function query:order(field, direction)
        self.order_by = {field = field, direction = direction or "ASC"}
        return self
    end

    function query:limit(count)
        self.limit_count = count
        return self
    end

    function query:execute()
        local results = {}

        -- Apply conditions
        for _, instance in ipairs(self.model.instances) do
            local matches = true
            for _, condition in ipairs(self.conditions) do
                local field_value = instance._data[condition.field]
                if condition.operator == "=" and field_value ~= condition.value then
                    matches = false
                    break
                elseif condition.operator == ">" and field_value <= condition.value then
                    matches = false
                    break
                elseif condition.operator == "<" and field_value >= condition.value then
                    matches = false
                    break
                end
            end

            if matches then
                table.insert(results, instance)
            end
        end

        -- Apply ordering
        if self.order_by then
            table.sort(results, function(a, b)
                local a_val = a._data[self.order_by.field]
                local b_val = b._data[self.order_by.field]
                if self.order_by.direction == "DESC" then
                    return a_val > b_val
                else
                    return a_val < b_val
                end
            end)
        end

        -- Apply limit
        if self.limit_count and #results > self.limit_count then
            local limited = {}
            for i = 1, self.limit_count do
                limited[i] = results[i]
            end
            results = limited
        end

        return results
    end

    return query
end

return ORM
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_template_engine(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test template engine patterns."""
    project = temp_repo / "lua_template"
    project.mkdir()

    (project / "template_engine.lua").write_text(
        encoding="utf-8",
        data="""
local TemplateEngine = {}

function TemplateEngine.create_template(template_string)
    local template = {
        source = template_string,
        compiled = nil
    }

    function template:compile()
        local lua_code = "return function(data)\n"
        lua_code = lua_code .. "  local output = {}\n"
        lua_code = lua_code .. "  local function write(text) table.insert(output, tostring(text or '')) end\n"

        -- Simple template parsing
        local i = 1
        while i <= #self.source do
            local start_expr = string.find(self.source, "{{", i)
            if start_expr then
                -- Add literal text before expression
                if start_expr > i then
                    local literal = string.sub(self.source, i, start_expr - 1)
                    lua_code = lua_code .. "  write(" .. string.format("%q", literal) .. ")\n"
                end

                -- Find end of expression
                local end_expr = string.find(self.source, "}}", start_expr + 2)
                if end_expr then
                    local expr = string.sub(self.source, start_expr + 2, end_expr - 1)
                    expr = string.gsub(expr, "^%s*(.-)%s*$", "%1") -- trim

                    if string.sub(expr, 1, 1) == "=" then
                        -- Expression to output
                        lua_code = lua_code .. "  write(" .. string.sub(expr, 2) .. ")\n"
                    else
                        -- Code block
                        lua_code = lua_code .. "  " .. expr .. "\n"
                    end

                    i = end_expr + 2
                else
                    error("Unclosed template expression")
                end
            else
                -- Add remaining literal text
                local literal = string.sub(self.source, i)
                if #literal > 0 then
                    lua_code = lua_code .. "  write(" .. string.format("%q", literal) .. ")\n"
                end
                break
            end
        end

        lua_code = lua_code .. "  return table.concat(output)\n"
        lua_code = lua_code .. "end"

        local func, err = load(lua_code)
        if func then
            self.compiled = func()
        else
            error("Template compilation failed: " .. err)
        end

        return self
    end

    function template:render(data)
        if not self.compiled then
            self:compile()
        end

        -- Create template environment
        local env = {
            data = data or {},
            pairs = pairs,
            ipairs = ipairs,
            tostring = tostring,
            type = type,
            math = math,
            string = string,
            table = table
        }

        -- Make data fields directly accessible
        setmetatable(env, {__index = data or {}})

        -- Set environment for the compiled function
        setfenv(self.compiled, env)

        return self.compiled(data)
    end

    return template
end

function TemplateEngine.render_string(template_string, data)
    local template = TemplateEngine.create_template(template_string)
    return template:render(data)
end

function TemplateEngine.create_loader(template_dir)
    local loader = {template_dir = template_dir, cache = {}}

    function loader:load_template(name)
        if self.cache[name] then
            return self.cache[name]
        end

        -- In a real implementation, you'd read from the file system
        -- For testing, we'll simulate file loading
        local template_content = "Mock template content for " .. name
        local template = TemplateEngine.create_template(template_content)
        self.cache[name] = template

        return template
    end

    function loader:render_template(name, data)
        local template = self:load_template(name)
        return template:render(data)
    end

    function loader:clear_cache()
        self.cache = {}
    end

    return loader
end

return TemplateEngine
""",
    )

    run_updater(project, mock_ingestor)


def test_lua_final_comprehensive_check(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Final comprehensive test to ensure we've covered major Lua features."""
    project = temp_repo / "lua_final_check"
    project.mkdir()

    (project / "comprehensive.lua").write_text(
        encoding="utf-8",
        data="""
-- This file demonstrates comprehensive Lua feature coverage
local Comprehensive = {}

-- Basic function
function Comprehensive.basic_function()
    return "basic"
end

-- Function with varargs
function Comprehensive.vararg_function(...)
    local args = {...}
    return #args
end

-- Function returning multiple values
function Comprehensive.multiple_returns()
    return 1, 2, 3
end

-- Closure example
function Comprehensive.create_closure(x)
    return function(y)
        return x + y
    end
end

-- Table as object
Comprehensive.TableObject = {}
Comprehensive.TableObject.__index = Comprehensive.TableObject

function Comprehensive.TableObject:new(value)
    return setmetatable({value = value}, Comprehensive.TableObject)
end

function Comprehensive.TableObject:get_value()
    return self.value
end

-- Metatable example
function Comprehensive.create_metatable_example()
    local t = {}
    local mt = {
        __index = function(table, key)
            return "default"
        end
    }
    return setmetatable(t, mt)
end

-- Coroutine example
function Comprehensive.create_coroutine()
    return coroutine.create(function()
        for i = 1, 3 do
            coroutine.yield(i)
        end
    end)
end

-- Error handling
function Comprehensive.safe_operation(x)
    local success, result = pcall(function()
        if x < 0 then error("Negative value") end
        return x * 2
    end)
    return success, result
end

return Comprehensive
""",
    )

    run_updater(project, mock_ingestor)
