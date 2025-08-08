"""Tests for Lua metatables and metamethods."""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def test_lua_arithmetic_metamethods(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test arithmetic metamethods (__add, __sub, __mul, etc.)."""
    project = temp_repo / "lua_arithmetic_meta"
    project.mkdir()

    (project / "vector.lua").write_text("""
local Vector = {}
Vector.__index = Vector

function Vector:new(x, y)
    local obj = setmetatable({x = x or 0, y = y or 0}, Vector)
    return obj
end

function Vector.__add(a, b)
    return Vector:new(a.x + b.x, a.y + b.y)
end

function Vector.__sub(a, b)
    return Vector:new(a.x - b.x, a.y - b.y)
end

function Vector.__mul(a, b)
    if type(b) == "number" then
        return Vector:new(a.x * b, a.y * b)
    elseif type(a) == "number" then
        return Vector:new(a * b.x, a * b.y)
    else
        -- Dot product
        return a.x * b.x + a.y * b.y
    end
end

function Vector.__div(a, b)
    if type(b) == "number" then
        return Vector:new(a.x / b, a.y / b)
    else
        error("Cannot divide vector by vector")
    end
end

function Vector.__unm(a)
    return Vector:new(-a.x, -a.y)
end

function Vector.__len(a)
    return math.sqrt(a.x * a.x + a.y * a.y)
end

function Vector.__tostring(a)
    return string.format("Vector(%g, %g)", a.x, a.y)
end

function Vector:magnitude()
    return #self
end

function Vector:normalize()
    local mag = #self
    if mag > 0 then
        return self / mag
    end
    return Vector:new(0, 0)
end

return Vector
""")

    (project / "main.lua").write_text("""
local Vector = require('vector')

local v1 = Vector:new(3, 4)
local v2 = Vector:new(1, 2)

local sum = v1 + v2
local diff = v1 - v2
local scaled = v1 * 2
local divided = v1 / 2
local negated = -v1
local dot_product = v1 * v2
local magnitude = #v1

print(tostring(sum))
print(tostring(diff))
print(tostring(scaled))
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

    vector_qn = f"{project.name}.vector"

    assert f"{vector_qn}.Vector:new" in fn_qns or f"{vector_qn}.Vector.new" in fn_qns
    assert f"{vector_qn}.Vector.__add" in fn_qns
    assert f"{vector_qn}.Vector.__sub" in fn_qns
    assert f"{vector_qn}.Vector.__mul" in fn_qns
    assert f"{vector_qn}.Vector.__div" in fn_qns
    assert f"{vector_qn}.Vector.__unm" in fn_qns
    assert f"{vector_qn}.Vector.__len" in fn_qns
    assert f"{vector_qn}.Vector.__tostring" in fn_qns
    assert (
        f"{vector_qn}.Vector:magnitude" in fn_qns
        or f"{vector_qn}.Vector.magnitude" in fn_qns
    )
    assert (
        f"{vector_qn}.Vector:normalize" in fn_qns
        or f"{vector_qn}.Vector.normalize" in fn_qns
    )


def test_lua_comparison_metamethods(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test comparison metamethods (__eq, __lt, __le)."""
    project = temp_repo / "lua_comparison_meta"
    project.mkdir()

    (project / "comparable.lua").write_text("""
local Comparable = {}
Comparable.__index = Comparable

function Comparable:new(value, priority)
    local obj = setmetatable({
        value = value,
        priority = priority or 0
    }, Comparable)
    return obj
end

function Comparable.__eq(a, b)
    return a.value == b.value and a.priority == b.priority
end

function Comparable.__lt(a, b)
    if a.priority == b.priority then
        return a.value < b.value
    end
    return a.priority < b.priority
end

function Comparable.__le(a, b)
    return a < b or a == b
end

function Comparable:compare(other)
    if self == other then
        return 0
    elseif self < other then
        return -1
    else
        return 1
    end
end

-- Priority queue using comparison metamethods
local PriorityQueue = {}
PriorityQueue.__index = PriorityQueue

function PriorityQueue:new()
    local obj = setmetatable({items = {}}, PriorityQueue)
    return obj
end

function PriorityQueue:push(item)
    table.insert(self.items, item)
    table.sort(self.items)
end

function PriorityQueue:pop()
    return table.remove(self.items, 1)
end

function PriorityQueue:size()
    return #self.items
end

return {
    Comparable = Comparable,
    PriorityQueue = PriorityQueue
}
""")

    (project / "main.lua").write_text("""
local lib = require('comparable')
local Comparable = lib.Comparable
local PriorityQueue = lib.PriorityQueue

local queue = PriorityQueue:new()

-- Add items with different priorities
queue:push(Comparable:new("Low priority", 3))
queue:push(Comparable:new("High priority", 1))
queue:push(Comparable:new("Medium priority", 2))

-- Pop items (should come out in priority order)
while queue:size() > 0 do
    local item = queue:pop()
    print(item.value, item.priority)
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

    comp_qn = f"{project.name}.comparable"

    assert (
        f"{comp_qn}.Comparable:new" in fn_qns or f"{comp_qn}.Comparable.new" in fn_qns
    )
    assert f"{comp_qn}.Comparable.__eq" in fn_qns
    assert f"{comp_qn}.Comparable.__lt" in fn_qns
    assert f"{comp_qn}.Comparable.__le" in fn_qns
    assert (
        f"{comp_qn}.Comparable:compare" in fn_qns
        or f"{comp_qn}.Comparable.compare" in fn_qns
    )

    assert (
        f"{comp_qn}.PriorityQueue:new" in fn_qns
        or f"{comp_qn}.PriorityQueue.new" in fn_qns
    )
    assert (
        f"{comp_qn}.PriorityQueue:push" in fn_qns
        or f"{comp_qn}.PriorityQueue.push" in fn_qns
    )
    assert (
        f"{comp_qn}.PriorityQueue:pop" in fn_qns
        or f"{comp_qn}.PriorityQueue.pop" in fn_qns
    )
    assert (
        f"{comp_qn}.PriorityQueue:size" in fn_qns
        or f"{comp_qn}.PriorityQueue.size" in fn_qns
    )


def test_lua_index_metamethods(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test __index and __newindex metamethods."""
    project = temp_repo / "lua_index_meta"
    project.mkdir()

    (project / "proxy.lua").write_text("""
local Proxy = {}

function Proxy.create_readonly(target)
    local proxy = {}
    local mt = {
        __index = target,
        __newindex = function(t, k, v)
            error("Attempt to modify readonly table")
        end
    }
    return setmetatable(proxy, mt)
end

function Proxy.create_logged(target)
    local proxy = {}
    local mt = {
        __index = function(t, k)
            print("Reading:", k)
            return target[k]
        end,
        __newindex = function(t, k, v)
            print("Writing:", k, "=", v)
            target[k] = v
        end
    }
    return setmetatable(proxy, mt)
end

function Proxy.create_default(target, default_value)
    local proxy = {}
    local mt = {
        __index = function(t, k)
            local val = target[k]
            if val == nil then
                return default_value
            end
            return val
        end,
        __newindex = target
    }
    return setmetatable(proxy, mt)
end

-- Property system using metamethods
local Properties = {}
Properties.__index = Properties

function Properties:new()
    local obj = setmetatable({
        _values = {},
        _getters = {},
        _setters = {}
    }, Properties)
    return obj
end

function Properties:define_property(name, getter, setter)
    self._getters[name] = getter
    self._setters[name] = setter
end

function Properties.__index(obj, key)
    local getter = obj._getters[key]
    if getter then
        return getter(obj)
    end
    return obj._values[key]
end

function Properties.__newindex(obj, key, value)
    local setter = obj._setters[key]
    if setter then
        setter(obj, value)
    else
        obj._values[key] = value
    end
end

return {
    Proxy = Proxy,
    Properties = Properties
}
""")

    (project / "main.lua").write_text("""
local lib = require('proxy')
local Proxy = lib.Proxy
local Properties = lib.Properties

-- Test readonly proxy
local data = {a = 1, b = 2}
local readonly = Proxy.create_readonly(data)

print("Readonly a:", readonly.a)

-- Test logged proxy
local logged = Proxy.create_logged({})
logged.x = 10
local val = logged.x

-- Test default proxy
local with_defaults = Proxy.create_default({name = "test"}, "default")
print("Name:", with_defaults.name)
print("Missing:", with_defaults.missing)

-- Test properties
local props = Properties:new()
props:define_property("computed",
    function(self) return (self._values.x or 0) * 2 end,
    function(self, val) self._values.x = val / 2 end
)

props.computed = 20
print("Computed:", props.computed)
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

    proxy_qn = f"{project.name}.proxy"

    assert f"{proxy_qn}.Proxy.create_readonly" in fn_qns
    assert f"{proxy_qn}.Proxy.create_logged" in fn_qns
    assert f"{proxy_qn}.Proxy.create_default" in fn_qns
    assert (
        f"{proxy_qn}.Properties:new" in fn_qns or f"{proxy_qn}.Properties.new" in fn_qns
    )
    assert (
        f"{proxy_qn}.Properties:define_property" in fn_qns
        or f"{proxy_qn}.Properties.define_property" in fn_qns
    )
    assert f"{proxy_qn}.Properties.__index" in fn_qns
    assert f"{proxy_qn}.Properties.__newindex" in fn_qns


def test_lua_call_metamethod(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test __call metamethod."""
    project = temp_repo / "lua_call_meta"
    project.mkdir()

    (project / "callable.lua").write_text("""
-- Function factory using __call
local FunctionFactory = {}
FunctionFactory.__index = FunctionFactory

function FunctionFactory:new(base_func)
    local obj = setmetatable({
        base = base_func,
        call_count = 0
    }, FunctionFactory)
    return obj
end

function FunctionFactory.__call(obj, ...)
    obj.call_count = obj.call_count + 1
    return obj.base(...)
end

function FunctionFactory:get_call_count()
    return self.call_count
end

-- Memoization using __call
local Memoizer = {}
Memoizer.__index = Memoizer

function Memoizer:new(func)
    local obj = setmetatable({
        func = func,
        cache = {}
    }, Memoizer)
    return obj
end

function Memoizer.__call(obj, ...)
    local key = table.concat({...}, ",")
    if obj.cache[key] == nil then
        obj.cache[key] = obj.func(...)
    end
    return obj.cache[key]
end

-- Partial application using __call
local Partial = {}
Partial.__index = Partial

function Partial:new(func, ...)
    local obj = setmetatable({
        func = func,
        args = {...}
    }, Partial)
    return obj
end

function Partial.__call(obj, ...)
    local all_args = {}
    for _, v in ipairs(obj.args) do
        table.insert(all_args, v)
    end
    for _, v in ipairs({...}) do
        table.insert(all_args, v)
    end
    return obj.func(unpack(all_args))
end

-- Event emitter using __call
local EventEmitter = {}
EventEmitter.__index = EventEmitter

function EventEmitter:new()
    local obj = setmetatable({
        listeners = {}
    }, EventEmitter)
    return obj
end

function EventEmitter:on(event, callback)
    if not self.listeners[event] then
        self.listeners[event] = {}
    end
    table.insert(self.listeners[event], callback)
end

function EventEmitter.__call(obj, event, ...)
    local callbacks = obj.listeners[event]
    if callbacks then
        for _, callback in ipairs(callbacks) do
            callback(...)
        end
    end
end

return {
    FunctionFactory = FunctionFactory,
    Memoizer = Memoizer,
    Partial = Partial,
    EventEmitter = EventEmitter
}
""")

    (project / "main.lua").write_text("""
local lib = require('callable')
local FunctionFactory = lib.FunctionFactory
local Memoizer = lib.Memoizer
local Partial = lib.Partial
local EventEmitter = lib.EventEmitter

-- Test function factory
local counter = FunctionFactory:new(function(x) return x + 1 end)
print("Result:", counter(5))
print("Call count:", counter:get_call_count())

-- Test memoization
local fib = Memoizer:new(function(n)
    if n <= 1 then return n end
    return fib(n-1) + fib(n-2)
end)

print("Fib(10):", fib(10))

-- Test partial application
local add = function(a, b, c) return a + b + c end
local add5 = Partial:new(add, 5)
print("Partial result:", add5(3, 2))

-- Test event emitter
local emitter = EventEmitter:new()
emitter:on("test", function(msg) print("Received:", msg) end)
emitter("test", "Hello World")
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

    callable_qn = f"{project.name}.callable"

    assert (
        f"{callable_qn}.FunctionFactory:new" in fn_qns
        or f"{callable_qn}.FunctionFactory.new" in fn_qns
    )
    assert f"{callable_qn}.FunctionFactory.__call" in fn_qns
    assert (
        f"{callable_qn}.FunctionFactory:get_call_count" in fn_qns
        or f"{callable_qn}.FunctionFactory.get_call_count" in fn_qns
    )

    assert (
        f"{callable_qn}.Memoizer:new" in fn_qns
        or f"{callable_qn}.Memoizer.new" in fn_qns
    )
    assert f"{callable_qn}.Memoizer.__call" in fn_qns

    assert (
        f"{callable_qn}.Partial:new" in fn_qns or f"{callable_qn}.Partial.new" in fn_qns
    )
    assert f"{callable_qn}.Partial.__call" in fn_qns

    assert (
        f"{callable_qn}.EventEmitter:new" in fn_qns
        or f"{callable_qn}.EventEmitter.new" in fn_qns
    )
    assert (
        f"{callable_qn}.EventEmitter:on" in fn_qns
        or f"{callable_qn}.EventEmitter.on" in fn_qns
    )
    assert f"{callable_qn}.EventEmitter.__call" in fn_qns


def test_lua_weak_references(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test weak reference patterns with metatables."""
    project = temp_repo / "lua_weak_refs"
    project.mkdir()

    (project / "weak_refs.lua").write_text("""
local WeakCache = {}
WeakCache.__index = WeakCache

function WeakCache:new()
    local obj = setmetatable({
        cache = setmetatable({}, {__mode = "v"})  -- weak values
    }, WeakCache)
    return obj
end

function WeakCache:set(key, value)
    self.cache[key] = value
end

function WeakCache:get(key)
    return self.cache[key]
end

function WeakCache:size()
    local count = 0
    for _ in pairs(self.cache) do
        count = count + 1
    end
    return count
end

-- Observer pattern with weak references
local Observable = {}
Observable.__index = Observable

function Observable:new()
    local obj = setmetatable({
        observers = setmetatable({}, {__mode = "k"})  -- weak keys
    }, Observable)
    return obj
end

function Observable:add_observer(observer, callback)
    self.observers[observer] = callback
end

function Observable:remove_observer(observer)
    self.observers[observer] = nil
end

function Observable:notify(...)
    for observer, callback in pairs(self.observers) do
        if callback then
            callback(observer, ...)
        end
    end
end

function Observable:observer_count()
    local count = 0
    for _ in pairs(self.observers) do
        count = count + 1
    end
    return count
end

-- Object registry with weak references
local Registry = {}
Registry.__index = Registry

function Registry:new()
    local obj = setmetatable({
        objects = setmetatable({}, {__mode = "kv"}),  -- weak keys and values
        id_counter = 0
    }, Registry)
    return obj
end

function Registry:register(obj)
    self.id_counter = self.id_counter + 1
    local id = self.id_counter
    self.objects[obj] = id
    self.objects[id] = obj
    return id
end

function Registry:get_by_id(id)
    return self.objects[id]
end

function Registry:get_id(obj)
    return self.objects[obj]
end

function Registry:count()
    local count = 0
    for k, v in pairs(self.objects) do
        if type(k) ~= "number" then  -- Only count objects, not IDs
            count = count + 1
        end
    end
    return count
end

return {
    WeakCache = WeakCache,
    Observable = Observable,
    Registry = Registry
}
""")

    (project / "main.lua").write_text("""
local lib = require('weak_refs')
local WeakCache = lib.WeakCache
local Observable = lib.Observable
local Registry = lib.Registry

-- Test weak cache
local cache = WeakCache:new()
local temp_data = {value = "temporary"}

cache:set("temp", temp_data)
print("Cache size:", cache:size())

temp_data = nil
collectgarbage()  -- Force garbage collection

print("Cache size after GC:", cache:size())

-- Test observable pattern
local observable = Observable:new()
local observer1 = {name = "Observer1"}
local observer2 = {name = "Observer2"}

observable:add_observer(observer1, function(obs, msg)
    print(obs.name, "received:", msg)
end)

observable:add_observer(observer2, function(obs, msg)
    print(obs.name, "got:", msg)
end)

observable:notify("Hello observers!")
print("Observer count:", observable:observer_count())

-- Test registry
local registry = Registry:new()
local obj1 = {data = "object1"}
local obj2 = {data = "object2"}

local id1 = registry:register(obj1)
local id2 = registry:register(obj2)

print("Registry count:", registry:count())
print("Found by ID:", registry:get_by_id(id1).data)
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

    weak_qn = f"{project.name}.weak_refs"

    # WeakCache methods
    assert f"{weak_qn}.WeakCache:new" in fn_qns or f"{weak_qn}.WeakCache.new" in fn_qns
    assert f"{weak_qn}.WeakCache:set" in fn_qns or f"{weak_qn}.WeakCache.set" in fn_qns
    assert f"{weak_qn}.WeakCache:get" in fn_qns or f"{weak_qn}.WeakCache.get" in fn_qns
    assert (
        f"{weak_qn}.WeakCache:size" in fn_qns or f"{weak_qn}.WeakCache.size" in fn_qns
    )

    # Observable methods
    assert (
        f"{weak_qn}.Observable:new" in fn_qns or f"{weak_qn}.Observable.new" in fn_qns
    )
    assert (
        f"{weak_qn}.Observable:add_observer" in fn_qns
        or f"{weak_qn}.Observable.add_observer" in fn_qns
    )
    assert (
        f"{weak_qn}.Observable:remove_observer" in fn_qns
        or f"{weak_qn}.Observable.remove_observer" in fn_qns
    )
    assert (
        f"{weak_qn}.Observable:notify" in fn_qns
        or f"{weak_qn}.Observable.notify" in fn_qns
    )
    assert (
        f"{weak_qn}.Observable:observer_count" in fn_qns
        or f"{weak_qn}.Observable.observer_count" in fn_qns
    )

    # Registry methods
    assert f"{weak_qn}.Registry:new" in fn_qns or f"{weak_qn}.Registry.new" in fn_qns
    assert (
        f"{weak_qn}.Registry:register" in fn_qns
        or f"{weak_qn}.Registry.register" in fn_qns
    )
    assert (
        f"{weak_qn}.Registry:get_by_id" in fn_qns
        or f"{weak_qn}.Registry.get_by_id" in fn_qns
    )
    assert (
        f"{weak_qn}.Registry:get_id" in fn_qns or f"{weak_qn}.Registry.get_id" in fn_qns
    )
    assert (
        f"{weak_qn}.Registry:count" in fn_qns or f"{weak_qn}.Registry.count" in fn_qns
    )
