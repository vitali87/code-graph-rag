from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater


def test_lua_basic_coroutines(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test basic coroutine creation and yielding."""
    project = temp_repo / "lua_coroutines_basic"
    project.mkdir()

    (project / "coroutines.lua").write_text(
        encoding="utf-8",
        data="""
local co = {}

function co.producer(n)
    return coroutine.create(function()
        for i = 1, n do
            print("Producing:", i)
            coroutine.yield(i)
        end
    end)
end

function co.consumer(producer_co)
    while coroutine.status(producer_co) ~= "dead" do
        local success, value = coroutine.resume(producer_co)
        if success and value then
            print("Consumed:", value)
            return value * 2
        end
    end
end

function co.fibonacci()
    local function fib()
        local a, b = 0, 1
        while true do
            coroutine.yield(a)
            a, b = b, a + b
        end
    end
    return coroutine.create(fib)
end

return co
""",
    )

    (project / "main.lua").write_text(
        encoding="utf-8",
        data="""
local co = require('coroutines')

-- Test producer-consumer pattern
local prod = co.producer(5)
local result = co.consumer(prod)

-- Test fibonacci coroutine
local fib = co.fibonacci()
for i = 1, 10 do
    local _, val = coroutine.resume(fib)
    print("Fib:", val)
end
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 3, (
        f"Expected at least 3 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 3, (
        f"Expected at least 3 CALLS relationships, got {len(calls_rels)}"
    )


def test_lua_coroutine_scheduler(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test coroutine-based cooperative scheduler."""
    project = temp_repo / "lua_scheduler"
    project.mkdir()

    (project / "scheduler.lua").write_text(
        encoding="utf-8",
        data="""
local Scheduler = {}
Scheduler.__index = Scheduler

function Scheduler:new()
    local obj = setmetatable({}, Scheduler)
    obj.tasks = {}
    obj.current_id = 0
    return obj
end

function Scheduler:spawn(func, ...)
    self.current_id = self.current_id + 1
    local task = {
        id = self.current_id,
        co = coroutine.create(func),
        args = {...}
    }
    table.insert(self.tasks, task)
    return self.current_id
end

function Scheduler:yield(...)
    return coroutine.yield(...)
end

function Scheduler:run()
    while #self.tasks > 0 do
        for i = #self.tasks, 1, -1 do
            local task = self.tasks[i]
            local success, result = coroutine.resume(task.co, unpack(task.args))

            if not success then
                print("Task", task.id, "failed:", result)
                table.remove(self.tasks, i)
            elseif coroutine.status(task.co) == "dead" then
                print("Task", task.id, "completed:", result)
                table.remove(self.tasks, i)
            end

            task.args = {}
        end
    end
end

function Scheduler:kill(task_id)
    for i, task in ipairs(self.tasks) do
        if task.id == task_id then
            table.remove(self.tasks, i)
            return true
        end
    end
    return false
end

return Scheduler
""",
    )

    (project / "tasks.lua").write_text(
        encoding="utf-8",
        data="""
local Tasks = {}

function Tasks.worker1(scheduler, n)
    for i = 1, n do
        print("Worker1 step", i)
        scheduler:yield()
    end
    return "Worker1 done"
end

function Tasks.worker2(scheduler, name)
    for i = 1, 3 do
        print(name, "iteration", i)
        scheduler:yield()
    end
    return name .. " finished"
end

function Tasks.long_task(scheduler, iterations)
    local sum = 0
    for i = 1, iterations do
        sum = sum + i
        if i % 1000 == 0 then
            scheduler:yield()
        end
    end
    return sum
end

return Tasks
""",
    )

    (project / "main.lua").write_text(
        encoding="utf-8",
        data="""
local Scheduler = require('scheduler')
local Tasks = require('tasks')

local sched = Scheduler:new()

-- Spawn multiple tasks
local id1 = sched:spawn(Tasks.worker1, sched, 5)
local id2 = sched:spawn(Tasks.worker2, sched, "TaskA")
local id3 = sched:spawn(Tasks.worker2, sched, "TaskB")
local id4 = sched:spawn(Tasks.long_task, sched, 10000)

-- Run scheduler
sched:run()
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    imports_rels = get_relationships(mock_ingestor, "IMPORTS")

    assert len(defines_rels) >= 8, (
        f"Expected at least 8 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 1, (
        f"Expected at least 1 CALLS relationship, got {len(calls_rels)}"
    )

    assert len(imports_rels) >= 2, (
        f"Expected at least 2 IMPORTS relationships, got {len(imports_rels)}"
    )


def test_lua_async_patterns(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test asynchronous programming patterns with coroutines."""
    project = temp_repo / "lua_async"
    project.mkdir()

    (project / "async.lua").write_text(
        encoding="utf-8",
        data="""
local Async = {}

-- Simulated async operations
function Async.setTimeout(callback, delay)
    return coroutine.create(function()
        -- Simulate delay
        for i = 1, delay do
            coroutine.yield()
        end
        if callback then
            callback()
        end
    end)
end

function Async.fetch(url)
    return coroutine.create(function()
        -- Simulate network request
        for i = 1, 5 do
            coroutine.yield()
        end
        return "Data from " .. url
    end)
end

function Async.parallel(tasks)
    return coroutine.create(function()
        local results = {}
        local completed = 0

        while completed < #tasks do
            for i, task in ipairs(tasks) do
                if coroutine.status(task) ~= "dead" then
                    local success, result = coroutine.resume(task)
                    if success and coroutine.status(task) == "dead" then
                        results[i] = result
                        completed = completed + 1
                    end
                end
            end
            if completed < #tasks then
                coroutine.yield()
            end
        end

        return results
    end)
end

function Async.sequence(tasks)
    return coroutine.create(function()
        local results = {}

        for i, task in ipairs(tasks) do
            while coroutine.status(task) ~= "dead" do
                local success, result = coroutine.resume(task)
                if not success then
                    error("Task failed: " .. tostring(result))
                end
                if coroutine.status(task) ~= "dead" then
                    coroutine.yield()
                else
                    results[i] = result
                end
            end
        end

        return results
    end)
end

return Async
""",
    )

    (project / "main.lua").write_text(
        encoding="utf-8",
        data="""
local Async = require('async')

-- Test parallel execution
local tasks = {
    Async.fetch("http://api1.com"),
    Async.fetch("http://api2.com"),
    Async.fetch("http://api3.com")
}

local parallel_task = Async.parallel(tasks)

-- Run until completion
while coroutine.status(parallel_task) ~= "dead" do
    coroutine.resume(parallel_task)
end

-- Test sequential execution
local seq_tasks = {
    Async.fetch("http://step1.com"),
    Async.fetch("http://step2.com")
}

local sequence_task = Async.sequence(seq_tasks)
while coroutine.status(sequence_task) ~= "dead" do
    coroutine.resume(sequence_task)
end
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 4, (
        f"Expected at least 4 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 1, (
        f"Expected at least 1 CALLS relationship, got {len(calls_rels)}"
    )


def test_lua_generator_patterns(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test generator patterns using coroutines."""
    project = temp_repo / "lua_generators"
    project.mkdir()

    (project / "generators.lua").write_text(
        encoding="utf-8",
        data="""
local Gen = {}

function Gen.range(start, stop, step)
    step = step or 1
    return coroutine.create(function()
        local current = start
        while current <= stop do
            coroutine.yield(current)
            current = current + step
        end
    end)
end

function Gen.filter(gen, predicate)
    return coroutine.create(function()
        while coroutine.status(gen) ~= "dead" do
            local success, value = coroutine.resume(gen)
            if success and value and predicate(value) then
                coroutine.yield(value)
            elseif not success then
                break
            end
        end
    end)
end

function Gen.map(gen, transform)
    return coroutine.create(function()
        while coroutine.status(gen) ~= "dead" do
            local success, value = coroutine.resume(gen)
            if success and value then
                coroutine.yield(transform(value))
            elseif not success then
                break
            end
        end
    end)
end

function Gen.take(gen, n)
    return coroutine.create(function()
        local count = 0
        while count < n and coroutine.status(gen) ~= "dead" do
            local success, value = coroutine.resume(gen)
            if success and value then
                coroutine.yield(value)
                count = count + 1
            elseif not success then
                break
            end
        end
    end)
end

function Gen.collect(gen)
    local results = {}
    while coroutine.status(gen) ~= "dead" do
        local success, value = coroutine.resume(gen)
        if success and value then
            table.insert(results, value)
        elseif not success then
            break
        end
    end
    return results
end

return Gen
""",
    )

    (project / "main.lua").write_text(
        encoding="utf-8",
        data="""
local Gen = require('generators')

-- Create a range generator
local numbers = Gen.range(1, 100)

-- Filter even numbers
local evens = Gen.filter(numbers, function(x) return x % 2 == 0 end)

-- Square the numbers
local squares = Gen.map(evens, function(x) return x * x end)

-- Take first 10
local limited = Gen.take(squares, 10)

-- Collect results
local results = Gen.collect(limited)

print("First 10 squares of even numbers:", table.concat(results, ", "))
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    assert len(defines_rels) >= 5, (
        f"Expected at least 5 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 1, (
        f"Expected at least 1 CALLS relationship, got {len(calls_rels)}"
    )


def test_lua_state_machines(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test state machine implementation with coroutines."""
    project = temp_repo / "lua_state_machine"
    project.mkdir()

    (project / "state_machine.lua").write_text(
        encoding="utf-8",
        data="""
local StateMachine = {}
StateMachine.__index = StateMachine

function StateMachine:new(initial_state)
    local obj = setmetatable({}, StateMachine)
    obj.current_state = initial_state
    obj.states = {}
    obj.co = nil
    return obj
end

function StateMachine:add_state(name, handler)
    self.states[name] = handler
end

function StateMachine:transition(new_state, ...)
    if self.states[new_state] then
        self.current_state = new_state
        self.co = coroutine.create(self.states[new_state])
        return coroutine.resume(self.co, self, ...)
    else
        error("Unknown state: " .. tostring(new_state))
    end
end

function StateMachine:step(...)
    if self.co and coroutine.status(self.co) ~= "dead" then
        return coroutine.resume(self.co, ...)
    end
    return false, "No active coroutine"
end

function StateMachine:get_state()
    return self.current_state
end

return StateMachine
""",
    )

    (project / "game_ai.lua").write_text(
        encoding="utf-8",
        data="""
local StateMachine = require('state_machine')

local GameAI = {}

function GameAI.create_enemy()
    local enemy = StateMachine:new("idle")

    enemy:add_state("idle", function(sm, dt)
        print("Enemy idling...")
        for i = 1, 3 do
            coroutine.yield("waiting")
        end
        sm:transition("patrol")
    end)

    enemy:add_state("patrol", function(sm, dt)
        print("Enemy patrolling...")
        for i = 1, 5 do
            coroutine.yield("moving")
        end
        sm:transition("chase")
    end)

    enemy:add_state("chase", function(sm, dt)
        print("Enemy chasing player!")
        for i = 1, 10 do
            coroutine.yield("chasing")
        end
        sm:transition("idle")
    end)

    enemy:add_state("attack", function(sm, dt)
        print("Enemy attacking!")
        coroutine.yield("attacking")
        sm:transition("idle")
    end)

    return enemy
end

function GameAI.update_enemy(enemy, dt)
    local success, result = enemy:step(dt)
    return success and result
end

return GameAI
""",
    )

    (project / "main.lua").write_text(
        encoding="utf-8",
        data="""
local GameAI = require('game_ai')

local enemy = GameAI.create_enemy()

-- Simulate game loop
for frame = 1, 20 do
    print("Frame", frame, "- State:", enemy:get_state())
    GameAI.update_enemy(enemy, 0.016)
end
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    imports_rels = get_relationships(mock_ingestor, "IMPORTS")

    assert len(defines_rels) >= 7, (
        f"Expected at least 7 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 1, (
        f"Expected at least 1 CALLS relationship, got {len(calls_rels)}"
    )

    assert len(imports_rels) >= 1, (
        f"Expected at least 1 IMPORTS relationship, got {len(imports_rels)}"
    )
