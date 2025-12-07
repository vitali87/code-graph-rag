from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def test_lua_class_pattern_basic(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test basic Lua class pattern with constructor and methods."""
    project = temp_repo / "lua_oop_test"
    project.mkdir()

    (project / "person.lua").write_text("""
-- Define a Person "class"
local Person = {}
Person.__index = Person

function Person:new(name, age)
    local obj = setmetatable({}, Person)
    obj.name = name
    obj.age = age
    return obj
end

function Person:greet()
    print("Hello, my name is " .. self.name)
end

function Person:getAge()
    return self.age
end

return Person
""")

    (project / "main.lua").write_text("""
local Person = require('person')

local alice = Person:new("Alice", 30)
alice:greet()
local age = alice:getAge()

local bob = Person:new("Bob", 25)
bob:greet()
""")

    updater = create_and_run_updater(project, mock_ingestor)

    person_qn = f"{project.name}.person"
    main_qn = f"{project.name}.main"

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    imports_rels = get_relationships(mock_ingestor, "IMPORTS")

    assert len(defines_rels) >= 3, (
        f"Expected at least 3 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 2, (
        f"Expected at least 2 CALLS relationships, got {len(calls_rels)}"
    )

    assert len(imports_rels) >= 1, (
        f"Expected at least 1 IMPORTS relationship, got {len(imports_rels)}"
    )

    import_map = updater.factory.import_processor.import_mapping.get(main_qn, {})
    assert "Person" in import_map
    assert import_map["Person"] == person_qn


def test_lua_inheritance_pattern(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua inheritance pattern using metatables."""
    project = temp_repo / "lua_inheritance_test"
    project.mkdir()

    (project / "animal.lua").write_text("""
local Animal = {}
Animal.__index = Animal

function Animal:new(name)
    local obj = setmetatable({}, Animal)
    obj.name = name
    return obj
end

function Animal:speak()
    print(self.name .. " makes a sound")
end

function Animal:getName()
    return self.name
end

return Animal
""")

    (project / "dog.lua").write_text("""
local Animal = require('animal')

local Dog = {}
Dog.__index = Dog
setmetatable(Dog, {__index = Animal})  -- Inheritance

function Dog:new(name, breed)
    local obj = Animal.new(self, name)  -- Call parent constructor
    setmetatable(obj, Dog)
    obj.breed = breed
    return obj
end

function Dog:speak()
    print(self.name .. " barks!")
end

function Dog:getBreed()
    return self.breed
end

return Dog
""")

    (project / "main.lua").write_text("""
local Dog = require('dog')

local myDog = Dog:new("Buddy", "Golden Retriever")
myDog:speak()  -- Should call Dog's speak
local name = myDog:getName()  -- Should call inherited Animal's getName
local breed = myDog:getBreed()  -- Should call Dog's getBreed
""")

    updater = create_and_run_updater(project, mock_ingestor)

    animal_qn = f"{project.name}.animal"
    dog_qn = f"{project.name}.dog"
    main_qn = f"{project.name}.main"

    created_functions = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == "Function"
    ]
    fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

    assert f"{animal_qn}.Animal:new" in fn_qns or f"{animal_qn}.Animal.new" in fn_qns
    assert (
        f"{animal_qn}.Animal:speak" in fn_qns or f"{animal_qn}.Animal.speak" in fn_qns
    )
    assert (
        f"{animal_qn}.Animal:getName" in fn_qns
        or f"{animal_qn}.Animal.getName" in fn_qns
    )

    assert f"{dog_qn}.Dog:new" in fn_qns or f"{dog_qn}.Dog.new" in fn_qns
    assert f"{dog_qn}.Dog:speak" in fn_qns or f"{dog_qn}.Dog.speak" in fn_qns
    assert f"{dog_qn}.Dog:getBreed" in fn_qns or f"{dog_qn}.Dog.getBreed" in fn_qns

    dog_imports = updater.factory.import_processor.import_mapping.get(dog_qn, {})
    assert "Animal" in dog_imports

    main_imports = updater.factory.import_processor.import_mapping.get(main_qn, {})
    assert "Dog" in main_imports


def test_lua_module_pattern(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua module pattern with encapsulation."""
    project = temp_repo / "lua_module_test"
    project.mkdir()

    (project / "calculator.lua").write_text("""
local Calculator = {}

-- Private function (local)
local function validate(x, y)
    if type(x) ~= "number" or type(y) ~= "number" then
        error("Arguments must be numbers")
    end
end

-- Public functions
function Calculator.add(x, y)
    validate(x, y)
    return x + y
end

function Calculator.subtract(x, y)
    validate(x, y)
    return x - y
end

function Calculator.multiply(x, y)
    validate(x, y)
    return x * y
end

-- Another way to define methods
Calculator.divide = function(x, y)
    validate(x, y)
    if y == 0 then
        error("Division by zero")
    end
    return x / y
end

return Calculator
""")

    (project / "main.lua").write_text("""
local calc = require('calculator')

local sum = calc.add(5, 3)
local diff = calc.subtract(10, 4)
local product = calc.multiply(6, 7)
local quotient = calc.divide(20, 5)
""")

    updater = create_and_run_updater(project, mock_ingestor)

    calc_qn = f"{project.name}.calculator"
    main_qn = f"{project.name}.main"

    created_functions = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == "Function"
    ]
    fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

    assert f"{calc_qn}.Calculator.add" in fn_qns
    assert f"{calc_qn}.Calculator.subtract" in fn_qns
    assert f"{calc_qn}.Calculator.multiply" in fn_qns
    assert (
        f"{calc_qn}.Calculator.divide" in fn_qns
    )  # Assignment pattern should now be detected

    assert f"{calc_qn}.validate" in fn_qns

    import_map = updater.factory.import_processor.import_mapping.get(main_qn, {})
    assert "calc" in import_map


def test_lua_prototype_pattern(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua prototype-based OOP pattern."""
    project = temp_repo / "lua_prototype_test"
    project.mkdir()

    (project / "shape.lua").write_text("""
-- Prototype object
local ShapePrototype = {
    x = 0,
    y = 0
}

function ShapePrototype:clone()
    local obj = {}
    for k, v in pairs(self) do
        obj[k] = v
    end
    setmetatable(obj, {__index = self})
    return obj
end

function ShapePrototype:moveTo(newX, newY)
    self.x = newX
    self.y = newY
end

function ShapePrototype:getPosition()
    return self.x, self.y
end

-- Factory function
local function createCircle(x, y, radius)
    local circle = ShapePrototype:clone()
    circle.x = x
    circle.y = y
    circle.radius = radius

    function circle:getArea()
        return math.pi * self.radius * self.radius
    end

    return circle
end

-- Export
return {
    ShapePrototype = ShapePrototype,
    createCircle = createCircle
}
""")

    (project / "main.lua").write_text("""
local shapes = require('shape')

local circle1 = shapes.createCircle(10, 20, 5)
circle1:moveTo(30, 40)
local area = circle1:getArea()
local x, y = circle1:getPosition()

local circle2 = shapes.createCircle(0, 0, 10)
""")

    updater = create_and_run_updater(project, mock_ingestor)

    shape_qn = f"{project.name}.shape"
    main_qn = f"{project.name}.main"

    created_functions = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == "Function"
    ]
    fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

    assert (
        f"{shape_qn}.ShapePrototype:clone" in fn_qns
        or f"{shape_qn}.ShapePrototype.clone" in fn_qns
    )
    assert (
        f"{shape_qn}.ShapePrototype:moveTo" in fn_qns
        or f"{shape_qn}.ShapePrototype.moveTo" in fn_qns
    )
    assert (
        f"{shape_qn}.ShapePrototype:getPosition" in fn_qns
        or f"{shape_qn}.ShapePrototype.getPosition" in fn_qns
    )

    assert f"{shape_qn}.createCircle" in fn_qns

    import_map = updater.factory.import_processor.import_mapping.get(main_qn, {})
    assert "shapes" in import_map


def test_lua_mixin_pattern(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua mixin pattern for multiple inheritance."""
    project = temp_repo / "lua_mixin_test"
    project.mkdir()

    (project / "printable.lua").write_text("""
local Printable = {}

function Printable:toString()
    local str = "{"
    for k, v in pairs(self) do
        if type(v) ~= "function" then
            str = str .. k .. "=" .. tostring(v) .. ", "
        end
    end
    return str .. "}"
end

function Printable:print()
    print(self:toString())
end

return Printable
""")

    (project / "serializable.lua").write_text("""
local Serializable = {}

function Serializable:serialize()
    -- Simple JSON-like serialization
    local result = {}
    for k, v in pairs(self) do
        if type(v) ~= "function" then
            table.insert(result, '"' .. k .. '":"' .. tostring(v) .. '"')
        end
    end
    return "{" .. table.concat(result, ",") .. "}"
end

function Serializable:deserialize(data)
    -- Simplified deserialization
    -- In real code, you'd parse the JSON properly
    return data
end

return Serializable
""")

    (project / "user.lua").write_text("""
local Printable = require('printable')
local Serializable = require('serializable')

local User = {}
User.__index = User

-- Apply mixins
for k, v in pairs(Printable) do
    User[k] = v
end
for k, v in pairs(Serializable) do
    User[k] = v
end

function User:new(name, email)
    local obj = setmetatable({}, User)
    obj.name = name
    obj.email = email
    return obj
end

function User:getName()
    return self.name
end

function User:getEmail()
    return self.email
end

return User
""")

    (project / "main.lua").write_text("""
local User = require('user')

local user = User:new("John Doe", "john@example.com")
user:print()  -- From Printable mixin
local json = user:serialize()  -- From Serializable mixin
local name = user:getName()  -- From User itself
""")

    updater = create_and_run_updater(project, mock_ingestor)

    printable_qn = f"{project.name}.printable"
    serializable_qn = f"{project.name}.serializable"
    user_qn = f"{project.name}.user"
    main_qn = f"{project.name}.main"

    created_functions = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == "Function"
    ]
    fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

    assert (
        f"{printable_qn}.Printable:toString" in fn_qns
        or f"{printable_qn}.Printable.toString" in fn_qns
    )
    assert (
        f"{printable_qn}.Printable:print" in fn_qns
        or f"{printable_qn}.Printable.print" in fn_qns
    )

    assert (
        f"{serializable_qn}.Serializable:serialize" in fn_qns
        or f"{serializable_qn}.Serializable.serialize" in fn_qns
    )
    assert (
        f"{serializable_qn}.Serializable:deserialize" in fn_qns
        or f"{serializable_qn}.Serializable.deserialize" in fn_qns
    )

    assert f"{user_qn}.User:new" in fn_qns or f"{user_qn}.User.new" in fn_qns
    assert f"{user_qn}.User:getName" in fn_qns or f"{user_qn}.User.getName" in fn_qns
    assert f"{user_qn}.User:getEmail" in fn_qns or f"{user_qn}.User.getEmail" in fn_qns

    user_imports = updater.factory.import_processor.import_mapping.get(user_qn, {})
    assert "Printable" in user_imports
    assert "Serializable" in user_imports

    main_imports = updater.factory.import_processor.import_mapping.get(main_qn, {})
    assert "User" in main_imports


def test_lua_singleton_pattern(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua singleton pattern."""
    project = temp_repo / "lua_singleton_test"
    project.mkdir()

    (project / "config.lua").write_text("""
-- Singleton pattern
local Config = {}
local instance = nil

function Config:getInstance()
    if not instance then
        instance = {}
        setmetatable(instance, {__index = Config})

        -- Initialize with default values
        instance.settings = {
            debug = false,
            logLevel = "info",
            maxConnections = 100
        }
    end
    return instance
end

function Config:get(key)
    return self.settings[key]
end

function Config:set(key, value)
    self.settings[key] = value
end

function Config:reset()
    self.settings = {
        debug = false,
        logLevel = "info",
        maxConnections = 100
    }
end

return Config
""")

    (project / "main.lua").write_text("""
local Config = require('config')

local config1 = Config:getInstance()
config1:set("debug", true)

local config2 = Config:getInstance()
local debug = config2:get("debug")  -- Should be true (same instance)

config2:reset()
""")

    updater = create_and_run_updater(project, mock_ingestor)

    config_qn = f"{project.name}.config"
    main_qn = f"{project.name}.main"

    created_functions = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == "Function"
    ]
    fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

    assert (
        f"{config_qn}.Config:getInstance" in fn_qns
        or f"{config_qn}.Config.getInstance" in fn_qns
    )
    assert f"{config_qn}.Config:get" in fn_qns or f"{config_qn}.Config.get" in fn_qns
    assert f"{config_qn}.Config:set" in fn_qns or f"{config_qn}.Config.set" in fn_qns
    assert (
        f"{config_qn}.Config:reset" in fn_qns or f"{config_qn}.Config.reset" in fn_qns
    )

    import_map = updater.factory.import_processor.import_mapping.get(main_qn, {})
    assert "Config" in import_map
