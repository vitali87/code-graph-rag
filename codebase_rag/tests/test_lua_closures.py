"""Tests for Lua closures and upvalues."""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def test_lua_basic_closures(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test basic closure creation and upvalue access."""
    project = temp_repo / "lua_basic_closures"
    project.mkdir()

    (project / "closures.lua").write_text("""
local ClosureFactory = {}

function ClosureFactory.create_counter(initial)
    local count = initial or 0

    return function()
        count = count + 1
        return count
    end
end

function ClosureFactory.create_accumulator(initial)
    local total = initial or 0

    return {
        add = function(value)
            total = total + value
            return total
        end,

        subtract = function(value)
            total = total - value
            return total
        end,

        get = function()
            return total
        end,

        reset = function()
            total = initial or 0
            return total
        end
    }
end

function ClosureFactory.create_multiplier(factor)
    return function(x)
        return x * factor
    end
end

function ClosureFactory.create_adder(addend)
    return function(x)
        return x + addend
    end
end

-- Closure composition
function ClosureFactory.compose(f, g)
    return function(x)
        return f(g(x))
    end
end

-- Partial application using closures
function ClosureFactory.partial(func, ...)
    local args = {...}
    return function(...)
        local all_args = {}
        for _, v in ipairs(args) do
            table.insert(all_args, v)
        end
        for _, v in ipairs({...}) do
            table.insert(all_args, v)
        end
        return func(unpack(all_args))
    end
end

return ClosureFactory
""")

    (project / "main.lua").write_text("""
local ClosureFactory = require('closures')

-- Test counter
local counter = ClosureFactory.create_counter(10)
print("Count 1:", counter())
print("Count 2:", counter())

-- Test accumulator
local acc = ClosureFactory.create_accumulator(0)
print("Add 5:", acc.add(5))
print("Add 3:", acc.add(3))
print("Subtract 2:", acc.subtract(2))
print("Current:", acc.get())
acc.reset()
print("After reset:", acc.get())

-- Test function composition
local double = ClosureFactory.create_multiplier(2)
local add_one = ClosureFactory.create_adder(1)
local double_then_add_one = ClosureFactory.compose(add_one, double)

print("Compose result:", double_then_add_one(5))  -- (5 * 2) + 1 = 11

-- Test partial application
local add = function(a, b, c) return a + b + c end
local add_5_10 = ClosureFactory.partial(add, 5, 10)
print("Partial result:", add_5_10(3))  -- 5 + 10 + 3 = 18
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

    closures_qn = f"{project.name}.closures"

    assert f"{closures_qn}.ClosureFactory.create_counter" in fn_qns
    assert f"{closures_qn}.ClosureFactory.create_accumulator" in fn_qns
    assert f"{closures_qn}.ClosureFactory.create_multiplier" in fn_qns
    assert f"{closures_qn}.ClosureFactory.create_adder" in fn_qns
    assert f"{closures_qn}.ClosureFactory.compose" in fn_qns
    assert f"{closures_qn}.ClosureFactory.partial" in fn_qns


def test_lua_advanced_closures(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test advanced closure patterns like decorators and middleware."""
    project = temp_repo / "lua_advanced_closures"
    project.mkdir()

    (project / "decorators.lua").write_text("""
local Decorators = {}

-- Timing decorator
function Decorators.timed(func)
    return function(...)
        local start_time = os.clock()
        local results = {func(...)}
        local end_time = os.clock()
        print(string.format("Function took %.4f seconds", end_time - start_time))
        return unpack(results)
    end
end

-- Memoization decorator
function Decorators.memoized(func)
    local cache = {}
    return function(...)
        local key = table.concat({...}, ",")
        if cache[key] == nil then
            cache[key] = {func(...)}
        end
        return unpack(cache[key])
    end
end

-- Retry decorator
function Decorators.retry(func, max_attempts)
    max_attempts = max_attempts or 3
    return function(...)
        local attempts = 0
        while attempts < max_attempts do
            attempts = attempts + 1
            local success, result = pcall(func, ...)
            if success then
                return result
            elseif attempts >= max_attempts then
                error("Function failed after " .. max_attempts .. " attempts")
            else
                print("Attempt " .. attempts .. " failed, retrying...")
            end
        end
    end
end

-- Rate limiting decorator
function Decorators.rate_limited(func, calls_per_second)
    local last_calls = {}
    return function(...)
        local now = os.time()

        -- Clean old calls
        local new_calls = {}
        for _, call_time in ipairs(last_calls) do
            if now - call_time < 1 then  -- Within last second
                table.insert(new_calls, call_time)
            end
        end
        last_calls = new_calls

        if #last_calls >= calls_per_second then
            error("Rate limit exceeded")
        end

        table.insert(last_calls, now)
        return func(...)
    end
end

-- Logging decorator
function Decorators.logged(func, logger)
    logger = logger or print
    return function(...)
        local args = {...}
        logger("Calling function with args:", table.concat(args, ", "))
        local results = {func(...)}
        logger("Function returned:", table.concat(results, ", "))
        return unpack(results)
    end
end

return Decorators
""")

    (project / "middleware.lua").write_text("""
local Middleware = {}

-- Middleware chain
function Middleware.create_chain()
    local chain = {middlewares = {}}

    function chain:use(middleware)
        table.insert(self.middlewares, middleware)
        return self
    end

    function chain:execute(context, final_handler)
        local index = 1

        local function next()
            local middleware = self.middlewares[index]
            index = index + 1

            if middleware then
                return middleware(context, next)
            else
                return final_handler(context)
            end
        end

        return next()
    end

    return chain
end

-- Common middleware functions
function Middleware.logger()
    return function(context, next)
        print("Processing:", context.path or "unknown")
        local result = next()
        print("Completed:", context.path or "unknown")
        return result
    end
end

function Middleware.authenticator(required_role)
    return function(context, next)
        if not context.user or context.user.role ~= required_role then
            error("Authentication failed")
        end
        return next()
    end
end

function Middleware.validator(schema)
    return function(context, next)
        for field, validator in pairs(schema) do
            if not validator(context[field]) then
                error("Validation failed for field: " .. field)
            end
        end
        return next()
    end
end

function Middleware.cache_middleware()
    local cache = {}
    return function(context, next)
        local key = context.cache_key
        if key and cache[key] then
            print("Cache hit for:", key)
            return cache[key]
        end

        local result = next()
        if key then
            cache[key] = result
        end
        return result
    end
end

return Middleware
""")

    (project / "main.lua").write_text("""
local Decorators = require('decorators')
local Middleware = require('middleware')

-- Test decorators
local function slow_function(n)
    local sum = 0
    for i = 1, n do
        sum = sum + i
    end
    return sum
end

local timed_func = Decorators.timed(slow_function)
local memoized_func = Decorators.memoized(slow_function)

print("First call (timed):")
print("Result:", timed_func(1000))

print("Second call (memoized):")
print("Result:", memoized_func(1000))

-- Test middleware
local chain = Middleware.create_chain()
    :use(Middleware.logger())
    :use(Middleware.authenticator("admin"))
    :use(Middleware.cache_middleware())

local result = chain:execute(
    {
        path = "/api/users",
        user = {role = "admin"},
        cache_key = "users_list"
    },
    function(context)
        return "User data for " .. context.path
    end
)

print("Middleware result:", result)
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

    decorators_qn = f"{project.name}.decorators"
    middleware_qn = f"{project.name}.middleware"

    # Decorator functions
    assert f"{decorators_qn}.Decorators.timed" in fn_qns
    assert f"{decorators_qn}.Decorators.memoized" in fn_qns
    assert f"{decorators_qn}.Decorators.retry" in fn_qns
    assert f"{decorators_qn}.Decorators.rate_limited" in fn_qns
    assert f"{decorators_qn}.Decorators.logged" in fn_qns

    # Middleware functions
    assert f"{middleware_qn}.Middleware.create_chain" in fn_qns
    assert f"{middleware_qn}.Middleware.logger" in fn_qns
    assert f"{middleware_qn}.Middleware.authenticator" in fn_qns
    assert f"{middleware_qn}.Middleware.validator" in fn_qns
    assert f"{middleware_qn}.Middleware.cache_middleware" in fn_qns


def test_lua_event_system_closures(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test event system using closures for callbacks."""
    project = temp_repo / "lua_event_closures"
    project.mkdir()

    (project / "events.lua").write_text("""
local EventSystem = {}
EventSystem.__index = EventSystem

function EventSystem:new()
    local obj = setmetatable({
        listeners = {},
        once_listeners = {},
        middleware = {}
    }, EventSystem)
    return obj
end

function EventSystem:on(event, callback)
    if not self.listeners[event] then
        self.listeners[event] = {}
    end
    table.insert(self.listeners[event], callback)

    -- Return unsubscribe function
    return function()
        local callbacks = self.listeners[event]
        if callbacks then
            for i, cb in ipairs(callbacks) do
                if cb == callback then
                    table.remove(callbacks, i)
                    break
                end
            end
        end
    end
end

function EventSystem:once(event, callback)
    if not self.once_listeners[event] then
        self.once_listeners[event] = {}
    end
    table.insert(self.once_listeners[event], callback)
end

function EventSystem:off(event, callback)
    local callbacks = self.listeners[event]
    if callbacks then
        for i, cb in ipairs(callbacks) do
            if cb == callback then
                table.remove(callbacks, i)
                break
            end
        end
    end
end

function EventSystem:emit(event, ...)
    local args = {...}

    -- Apply middleware
    local context = {event = event, args = args, cancelled = false}
    for _, middleware in ipairs(self.middleware) do
        middleware(context)
        if context.cancelled then
            return false
        end
    end

    -- Regular listeners
    local callbacks = self.listeners[event]
    if callbacks then
        for _, callback in ipairs(callbacks) do
            callback(unpack(context.args))
        end
    end

    -- Once listeners
    local once_callbacks = self.once_listeners[event]
    if once_callbacks then
        for _, callback in ipairs(once_callbacks) do
            callback(unpack(context.args))
        end
        self.once_listeners[event] = {}  -- Clear once listeners
    end

    return true
end

function EventSystem:use(middleware)
    table.insert(self.middleware, middleware)
end

-- Event filters using closures
function EventSystem.create_filter(predicate)
    return function(context)
        if not predicate(context.event, unpack(context.args)) then
            context.cancelled = true
        end
    end
end

-- Event transformers using closures
function EventSystem.create_transformer(transform_func)
    return function(context)
        context.args = {transform_func(unpack(context.args))}
    end
end

-- Debounce utility using closures
function EventSystem.debounce(func, delay)
    local timer = nil
    return function(...)
        local args = {...}
        if timer then
            -- Cancel previous timer (simulated)
            timer = nil
        end
        timer = true
        -- In real implementation, you'd use a proper timer
        -- For testing, we'll just call immediately
        func(unpack(args))
    end
end

-- Throttle utility using closures
function EventSystem.throttle(func, interval)
    local last_call = 0
    return function(...)
        local now = os.time()
        if now - last_call >= interval then
            last_call = now
            return func(...)
        end
    end
end

return EventSystem
""")

    (project / "main.lua").write_text("""
local EventSystem = require('events')

local events = EventSystem:new()

-- Add middleware
events:use(EventSystem.create_filter(function(event, ...)
    return event ~= "blocked_event"
end))

events:use(EventSystem.create_transformer(function(data)
    return "Transformed: " .. tostring(data)
end))

-- Add listeners
local unsubscribe = events:on("test", function(data)
    print("Regular listener:", data)
end)

events:once("test", function(data)
    print("Once listener:", data)
end)

-- Test throttled function
local throttled_handler = EventSystem.throttle(function(msg)
    print("Throttled:", msg)
end, 1)

events:on("throttled", throttled_handler)

-- Test debounced function
local debounced_handler = EventSystem.debounce(function(msg)
    print("Debounced:", msg)
end, 0.1)

events:on("debounced", debounced_handler)

-- Emit events
print("=== Emitting test event ===")
events:emit("test", "Hello World")

print("=== Emitting test event again (once listener should not fire) ===")
events:emit("test", "Hello Again")

print("=== Emitting blocked event ===")
events:emit("blocked_event", "Should be blocked")

print("=== Emitting throttled events ===")
events:emit("throttled", "Message 1")
events:emit("throttled", "Message 2")  -- Should be throttled

-- Unsubscribe
unsubscribe()
print("=== After unsubscribe ===")
events:emit("test", "Should not show regular listener")
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

    events_qn = f"{project.name}.events"

    assert (
        f"{events_qn}.EventSystem:new" in fn_qns
        or f"{events_qn}.EventSystem.new" in fn_qns
    )
    assert (
        f"{events_qn}.EventSystem:on" in fn_qns
        or f"{events_qn}.EventSystem.on" in fn_qns
    )
    assert (
        f"{events_qn}.EventSystem:once" in fn_qns
        or f"{events_qn}.EventSystem.once" in fn_qns
    )
    assert (
        f"{events_qn}.EventSystem:off" in fn_qns
        or f"{events_qn}.EventSystem.off" in fn_qns
    )
    assert (
        f"{events_qn}.EventSystem:emit" in fn_qns
        or f"{events_qn}.EventSystem.emit" in fn_qns
    )
    assert (
        f"{events_qn}.EventSystem:use" in fn_qns
        or f"{events_qn}.EventSystem.use" in fn_qns
    )
    assert f"{events_qn}.EventSystem.create_filter" in fn_qns
    assert f"{events_qn}.EventSystem.create_transformer" in fn_qns
    assert f"{events_qn}.EventSystem.debounce" in fn_qns
    assert f"{events_qn}.EventSystem.throttle" in fn_qns


def test_lua_functional_programming(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test functional programming patterns with closures."""
    project = temp_repo / "lua_functional"
    project.mkdir()

    (project / "functional.lua").write_text("""
local Functional = {}

-- Higher-order functions
function Functional.map(list, func)
    local result = {}
    for i, v in ipairs(list) do
        result[i] = func(v)
    end
    return result
end

function Functional.filter(list, predicate)
    local result = {}
    for _, v in ipairs(list) do
        if predicate(v) then
            table.insert(result, v)
        end
    end
    return result
end

function Functional.reduce(list, func, initial)
    local accumulator = initial
    for _, v in ipairs(list) do
        accumulator = func(accumulator, v)
    end
    return accumulator
end

function Functional.fold_right(list, func, initial)
    local accumulator = initial
    for i = #list, 1, -1 do
        accumulator = func(list[i], accumulator)
    end
    return accumulator
end

-- Currying functions
function Functional.curry2(func)
    return function(a)
        return function(b)
            return func(a, b)
        end
    end
end

function Functional.curry3(func)
    return function(a)
        return function(b)
            return function(c)
                return func(a, b, c)
            end
        end
    end
end

-- Function composition
function Functional.compose(...)
    local functions = {...}
    return function(x)
        local result = x
        for i = #functions, 1, -1 do
            result = functions[i](result)
        end
        return result
    end
end

function Functional.pipe(...)
    local functions = {...}
    return function(x)
        local result = x
        for _, func in ipairs(functions) do
            result = func(result)
        end
        return result
    end
end

-- Lazy evaluation
function Functional.lazy(func)
    local cached = false
    local value = nil

    return function()
        if not cached then
            value = func()
            cached = true
        end
        return value
    end
end

-- Maybe monad pattern
function Functional.maybe(value)
    local maybe = {}

    function maybe:map(func)
        if value == nil then
            return Functional.maybe(nil)
        else
            return Functional.maybe(func(value))
        end
    end

    function maybe:flat_map(func)
        if value == nil then
            return Functional.maybe(nil)
        else
            return func(value)
        end
    end

    function maybe:get_or_else(default)
        if value == nil then
            return default
        else
            return value
        end
    end

    function maybe:is_some()
        return value ~= nil
    end

    function maybe:is_none()
        return value == nil
    end

    return maybe
end

-- Predicate combinators
function Functional.all(...)
    local predicates = {...}
    return function(value)
        for _, predicate in ipairs(predicates) do
            if not predicate(value) then
                return false
            end
        end
        return true
    end
end

function Functional.any(...)
    local predicates = {...}
    return function(value)
        for _, predicate in ipairs(predicates) do
            if predicate(value) then
                return true
            end
        end
        return false
    end
end

function Functional.not_pred(predicate)
    return function(value)
        return not predicate(value)
    end
end

return Functional
""")

    (project / "main.lua").write_text("""
local Functional = require('functional')

-- Test map, filter, reduce
local numbers = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}

local doubled = Functional.map(numbers, function(x) return x * 2 end)
print("Doubled:", table.concat(doubled, ", "))

local evens = Functional.filter(numbers, function(x) return x % 2 == 0 end)
print("Evens:", table.concat(evens, ", "))

local sum = Functional.reduce(numbers, function(acc, x) return acc + x end, 0)
print("Sum:", sum)

-- Test currying
local add = function(a, b) return a + b end
local curried_add = Functional.curry2(add)
local add_5 = curried_add(5)
print("Curried add:", add_5(3))

-- Test composition
local add_1 = function(x) return x + 1 end
local multiply_2 = function(x) return x * 2 end
local composed = Functional.compose(multiply_2, add_1)
print("Composed (add 1, then multiply 2):", composed(5))

-- Test pipe
local piped = Functional.pipe(add_1, multiply_2)
print("Piped (add 1, then multiply 2):", piped(5))

-- Test maybe monad
local safe_divide = function(a, b)
    if b == 0 then
        return Functional.maybe(nil)
    else
        return Functional.maybe(a / b)
    end
end

local result1 = safe_divide(10, 2):map(function(x) return x * 2 end)
print("Safe divide result:", result1:get_or_else("undefined"))

local result2 = safe_divide(10, 0):map(function(x) return x * 2 end)
print("Safe divide by zero:", result2:get_or_else("undefined"))

-- Test predicates
local is_positive = function(x) return x > 0 end
local is_even = function(x) return x % 2 == 0 end
local is_positive_and_even = Functional.all(is_positive, is_even)

print("Is 4 positive and even?", is_positive_and_even(4))
print("Is -2 positive and even?", is_positive_and_even(-2))
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

    functional_qn = f"{project.name}.functional"

    # Higher-order functions
    assert f"{functional_qn}.Functional.map" in fn_qns
    assert f"{functional_qn}.Functional.filter" in fn_qns
    assert f"{functional_qn}.Functional.reduce" in fn_qns
    assert f"{functional_qn}.Functional.fold_right" in fn_qns

    # Currying
    assert f"{functional_qn}.Functional.curry2" in fn_qns
    assert f"{functional_qn}.Functional.curry3" in fn_qns

    # Composition
    assert f"{functional_qn}.Functional.compose" in fn_qns
    assert f"{functional_qn}.Functional.pipe" in fn_qns

    # Lazy evaluation
    assert f"{functional_qn}.Functional.lazy" in fn_qns

    # Maybe monad
    assert f"{functional_qn}.Functional.maybe" in fn_qns

    # Predicates
    assert f"{functional_qn}.Functional.all" in fn_qns
    assert f"{functional_qn}.Functional.any" in fn_qns
    assert f"{functional_qn}.Functional.not_pred" in fn_qns
