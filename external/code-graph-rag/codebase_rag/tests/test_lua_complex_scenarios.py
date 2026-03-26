from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater


def test_web_framework_scenario(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua web framework scenario."""
    project = temp_repo / "lua_web_framework"
    project.mkdir()

    (project / "web_framework.lua").write_text(
        encoding="utf-8",
        data="""
local http = require("socket.http")
local ltn12 = require("ltn12")
local json = require("cjson")
local template = require("resty.template")

local WebServer = {}
WebServer.__index = WebServer

function WebServer:new(port)
    local instance = {
        port = port or 8080,
        routes = {},
        middleware = {}
    }
    setmetatable(instance, self)
    return instance
end

function WebServer:use(middleware_func)
    table.insert(self.middleware, middleware_func)
end

function WebServer:get(path, handler)
    self.routes[path] = {method = "GET", handler = handler}
end

function WebServer:post(path, handler)
    self.routes[path] = {method = "POST", handler = handler}
end

function WebServer:handle_request(request)
    local route = self.routes[request.path]
    if not route then
        return {status = 404, body = "Not Found"}
    end

    local context = {
        request = request,
        response = {},
        render = function(template_name, data)
            return template.render(template_name, data)
        end,
        json = function(data)
            return json.encode(data)
        end
    }

    for _, middleware in ipairs(self.middleware) do
        middleware(context)
    end

    return route.handler(context)
end

function WebServer:start()
    print(string.format("Server starting on port %d", self.port))

    while true do
        local client = socket.accept(self.socket)
        if client then
            local request_data = client:receive("*all")
            local response = self:handle_request(parse_request(request_data))

            client:send(string.format("HTTP/1.1 %d OK\r\n\r\n%s",
                                    response.status or 200,
                                    response.body or ""))
            client:close()
        end
    end
end

local app = WebServer:new(3000)
app:use(function(ctx) ctx.start_time = os.time() end)
app:get("/", function(ctx) return ctx:json({message = "Hello World"}) end)
app:post("/data", function(ctx) return {status = 201, body = "Created"} end)
app:start()
""",
    )

    (project / "main.lua").write_text(
        encoding="utf-8",
        data="""
local WebServer = require('web_framework')

local app = WebServer:new(3000)
app:use(function(ctx) ctx.start_time = os.time() end)
app:get("/", function(ctx) return ctx:json({message = "Hello World"}) end)
app:start()
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    imports_rels = get_relationships(mock_ingestor, "IMPORTS")

    assert len(defines_rels) >= 5, (
        f"Expected at least 5 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 1, (
        f"Expected at least 1 CALLS relationship, got {len(calls_rels)}"
    )

    assert len(imports_rels) >= 1, (
        f"Expected at least 1 IMPORTS relationship, got {len(imports_rels)}"
    )


def test_database_orm_scenario(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua database ORM scenario."""
    project = temp_repo / "lua_database_orm"
    project.mkdir()

    (project / "database_orm.lua").write_text(
        encoding="utf-8",
        data="""
local mysql = require("luasql.mysql")
local inspect = require("inspect")

local Database = {}
Database.__index = Database

function Database:new(config)
    local env = mysql.mysql()
    local conn = env:connect(config.database, config.user, config.password, config.host, config.port)

    local instance = {
        env = env,
        connection = conn,
        queries = {}
    }
    setmetatable(instance, self)
    return instance
end

function Database:query(sql, params)
    local prepared_sql = sql
    if params then
        for key, value in pairs(params) do
            local placeholder = ":" .. key
            local safe_value = self:escape_value(value)
            prepared_sql = string.gsub(prepared_sql, placeholder, safe_value)
        end
    end

    table.insert(self.queries, prepared_sql)
    local cursor = self.connection:execute(prepared_sql)

    local results = {}
    if cursor then
        local row = cursor:fetch({}, "a")
        while row do
            table.insert(results, row)
            row = cursor:fetch({}, "a")
        end
        cursor:close()
    end

    return results
end

function Database:escape_value(value)
    if type(value) == "string" then
        return "'" .. string.gsub(value, "'", "''") .. "'"
    elseif type(value) == "number" then
        return tostring(value)
    elseif value == nil then
        return "NULL"
    else
        return "'" .. tostring(value) .. "'"
    end
end

function Database:close()
    if self.connection then
        self.connection:close()
    end
    if self.env then
        self.env:close()
    end
end

local db = Database:new({
    host = "localhost",
    database = "myapp",
    user = "root",
    password = "secret"
})

local users = db:query("SELECT * FROM users WHERE age > :min_age", {min_age = 18})
print(inspect(users))

db:close()
""",
    )

    (project / "main.lua").write_text(
        encoding="utf-8",
        data="""
local Database = require('database_orm')
local inspect = require('inspect')

local db = Database:new({
    host = "localhost",
    database = "myapp",
    user = "root",
    password = "secret"
})

local users = db:query("SELECT * FROM users WHERE age > :min_age", {min_age = 18})
print(inspect(users))
db:close()
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    imports_rels = get_relationships(mock_ingestor, "IMPORTS")

    assert len(defines_rels) >= 4, (
        f"Expected at least 4 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 1, (
        f"Expected at least 1 CALLS relationship, got {len(calls_rels)}"
    )

    assert len(imports_rels) >= 1, (
        f"Expected at least 1 IMPORTS relationship, got {len(imports_rels)}"
    )


def test_game_engine_scenario(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua game engine scenario."""
    project = temp_repo / "lua_game_engine"
    project.mkdir()

    (project / "game_engine.lua").write_text(
        encoding="utf-8",
        data="""
local love = require("love")
local vector = require("vector")
local physics = require("physics")

local Game = {
    entities = {},
    systems = {},
    delta_time = 0
}

function Game:init()
    love.graphics.setBackgroundColor(0.1, 0.1, 0.1)
    love.window.setTitle("Lua Game Engine")

    self.world = physics.newWorld(0, 9.81 * 64, true)
    self.world:setCallbacks(self.beginContact, self.endContact)

    self:load_assets()
    self:setup_entities()
end

function Game:load_assets()
    self.sprites = {
        player = love.graphics.newImage("assets/player.png"),
        enemy = love.graphics.newImage("assets/enemy.png"),
        background = love.graphics.newImage("assets/bg.png")
    }

    self.sounds = {
        jump = love.audio.newSource("assets/jump.wav", "static"),
        music = love.audio.newSource("assets/music.ogg", "stream")
    }

    love.audio.play(self.sounds.music)
end

function Game:setup_entities()
    local player = {
        position = vector.new(100, 100),
        velocity = vector.new(0, 0),
        sprite = self.sprites.player,
        body = physics.newBody(self.world, 100, 100, "dynamic")
    }

    player.body:setFixedRotation(true)
    local shape = physics.newRectangleShape(32, 64)
    local fixture = physics.newFixture(player.body, shape, 1)

    table.insert(self.entities, player)
end

function Game:update(dt)
    self.delta_time = dt
    self.world:update(dt)

    for _, entity in ipairs(self.entities) do
        if entity.update then
            entity:update(dt)
        end

        if entity.body then
            local x, y = entity.body:getPosition()
            entity.position = vector.new(x, y)
        end
    end

    self:handle_input()
    self:check_collisions()
end

function Game:handle_input()
    if love.keyboard.isDown("space") then
        love.audio.play(self.sounds.jump)
        self.entities[1].body:applyLinearImpulse(0, -500)
    end

    if love.keyboard.isDown("left") then
        self.entities[1].body:setX(self.entities[1].body:getX() - 200 * self.delta_time)
    end

    if love.keyboard.isDown("right") then
        self.entities[1].body:setX(self.entities[1].body:getX() + 200 * self.delta_time)
    end
end

function Game:draw()
    love.graphics.draw(self.sprites.background, 0, 0)

    for _, entity in ipairs(self.entities) do
        if entity.sprite then
            love.graphics.draw(entity.sprite, entity.position.x, entity.position.y)
        end
    end

    love.graphics.print(string.format("FPS: %d", love.timer.getFPS()), 10, 10)
end

Game:init()
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


def test_configuration_management_scenario(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Test complex configuration management scenario."""
    project = temp_repo / "lua_config_management"
    project.mkdir()

    (project / "config_mgmt.lua").write_text(
        encoding="utf-8",
        data="""
local yaml = require("lyaml")
local lfs = require("lfs")
local socket = require("socket")

local ConfigManager = {}
ConfigManager.__index = ConfigManager

function ConfigManager:new(config_dir)
    local instance = {
        config_dir = config_dir or "./config",
        configs = {},
        watchers = {},
        cache = {}
    }
    setmetatable(instance, self)
    return instance
end

function ConfigManager:load_all_configs()
    for file in lfs.dir(self.config_dir) do
        if string.match(file, "%.yaml$") or string.match(file, "%.yml$") then
            local full_path = self.config_dir .. "/" .. file
            local success, config = self:load_config_file(full_path)

            if success then
                local name = string.gsub(file, "%.ya?ml$", "")
                self.configs[name] = config
                self.cache[name] = {
                    data = config,
                    loaded_at = os.time(),
                    file_path = full_path
                }
            else
                print(string.format("Failed to load config: %s", file))
            end
        end
    end
end

function ConfigManager:load_config_file(file_path)
    local file = io.open(file_path, "r")
    if not file then
        return false, "File not found"
    end

    local content = file:read("*all")
    file:close()

    local success, config = pcall(yaml.load, content)
    if success then
        config = self:process_config(config)
        return true, config
    else
        return false, config
    end
end

function ConfigManager:process_config(config)
    local processed = {}

    for key, value in pairs(config) do
        if type(value) == "string" then
            local env_var = string.match(value, "^%$([%w_]+)$")
            if env_var then
                processed[key] = os.getenv(env_var) or value
            else
                local interpolated = string.gsub(value, "%$([%w_]+)", function(var)
                    return os.getenv(var) or ("$" .. var)
                end)
                processed[key] = interpolated
            end
        elseif type(value) == "table" then
            processed[key] = self:process_config(value)
        else
            processed[key] = value
        end
    end

    return processed
end

function ConfigManager:watch_configs()
    for name, cache_entry in pairs(self.cache) do
        local attr = lfs.attributes(cache_entry.file_path)
        if attr and attr.modification > cache_entry.loaded_at then
            print(string.format("Reloading config: %s", name))
            local success, new_config = self:load_config_file(cache_entry.file_path)

            if success then
                self.configs[name] = new_config
                self.cache[name].data = new_config
                self.cache[name].loaded_at = os.time()
            end
        end
    end
end

function ConfigManager:get(config_name, key_path)
    local config = self.configs[config_name]
    if not config then return nil end

    local keys = {}
    for key in string.gmatch(key_path, "[^%.]+") do
        table.insert(keys, key)
    end

    local current = config
    for _, key in ipairs(keys) do
        if type(current) == "table" and current[key] then
            current = current[key]
        else
            return nil
        end
    end

    return current
end

local config_mgr = ConfigManager:new("/etc/myapp")
config_mgr:load_all_configs()

local db_host = config_mgr:get("database", "host")
local db_port = config_mgr:get("database", "port")
local api_key = config_mgr:get("api", "key")

setmetatable({}, {
    __index = function(t, k)
        return config_mgr:get("default", k)
    end
})
""",
    )

    (project / "main.lua").write_text(
        encoding="utf-8",
        data="""
local ConfigManager = require('config_mgmt')

local config_mgr = ConfigManager:new("/etc/myapp")
config_mgr:load_all_configs()

local db_host = config_mgr:get("database", "host")
local db_port = config_mgr:get("database", "port")
local api_key = config_mgr:get("api", "key")
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    imports_rels = get_relationships(mock_ingestor, "IMPORTS")

    assert len(defines_rels) >= 6, (
        f"Expected at least 6 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 1, (
        f"Expected at least 1 CALLS relationship, got {len(calls_rels)}"
    )

    assert len(imports_rels) >= 1, (
        f"Expected at least 1 IMPORTS relationship, got {len(imports_rels)}"
    )


def test_data_processing_pipeline(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test complex data processing pipeline."""
    project = temp_repo / "lua_data_pipeline"
    project.mkdir()

    (project / "data_pipeline.lua").write_text(
        encoding="utf-8",
        data="""
local csv = require("csv")
local json = require("cjson")
local http = require("socket.http")
local ltn12 = require("ltn12")

local DataPipeline = {}
DataPipeline.__index = DataPipeline

function DataPipeline:new()
    local instance = {
        transformers = {},
        validators = {},
        outputs = {},
        stats = {processed = 0, errors = 0}
    }
    setmetatable(instance, self)
    return instance
end

function DataPipeline:add_transformer(name, func)
    self.transformers[name] = func
    return self
end

function DataPipeline:add_validator(name, func)
    self.validators[name] = func
    return self
end

function DataPipeline:add_output(name, func)
    self.outputs[name] = func
    return self
end

function DataPipeline:process_csv_file(filename)
    local file = io.open(filename, "r")
    if not file then
        error("Cannot open file: " .. filename)
    end

    local content = file:read("*all")
    file:close()

    local rows = csv.parse(content)
    local results = {}

    for i, row in ipairs(rows) do
        if i > 1 then
            local processed_row = self:transform_row(row)
            local is_valid = self:validate_row(processed_row)

            if is_valid then
                table.insert(results, processed_row)
                self.stats.processed = self.stats.processed + 1
            else
                print(string.format("Invalid row %d: %s", i, table.concat(row, ",")))
                self.stats.errors = self.stats.errors + 1
            end
        end
    end

    return results
end

function DataPipeline:transform_row(row)
    local transformed = {}

    for name, transformer in pairs(self.transformers) do
        transformed = transformer(transformed, row)
    end

    return transformed
end

function DataPipeline:validate_row(row)
    for name, validator in pairs(self.validators) do
        if not validator(row) then
            return false
        end
    end
    return true
end

function DataPipeline:export_results(results)
    for name, output_func in pairs(self.outputs) do
        local success, err = pcall(output_func, results)
        if not success then
            print(string.format("Export failed for %s: %s", name, err))
        end
    end
end

function DataPipeline:fetch_remote_data(url)
    local result = {}
    local response, status = http.request{
        url = url,
        sink = ltn12.sink.table(result)
    }

    if status == 200 then
        local data = table.concat(result)
        return json.decode(data)
    else
        error(string.format("HTTP request failed: %d", status))
    end
end

local pipeline = DataPipeline:new()

pipeline:add_transformer("normalize_names", function(row, original)
    row.name = string.lower(string.gsub(original[1] or "", "%s+", "_"))
    return row
end)

pipeline:add_validator("check_email", function(row)
    return row.email and string.match(row.email, "[%w%.%-_]+@[%w%.%-]+%.%w+")
end)

pipeline:add_output("json_file", function(data)
    local file = io.open("output.json", "w")
    file:write(json.encode(data))
    file:close()
end)

local csv_data = pipeline:process_csv_file("input.csv")
local api_data = pipeline:fetch_remote_data("https://api.example.com/users")

pipeline:export_results(csv_data)
""",
    )

    (project / "main.lua").write_text(
        encoding="utf-8",
        data="""
local DataPipeline = require('data_pipeline')

local pipeline = DataPipeline:new()

pipeline:add_transformer("normalize_names", function(row, original)
    row.name = string.lower(string.gsub(original[1] or "", "%s+", "_"))
    return row
end)

local csv_data = pipeline:process_csv_file("input.csv")
pipeline:export_results(csv_data)
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

    assert len(imports_rels) >= 1, (
        f"Expected at least 1 IMPORTS relationship, got {len(imports_rels)}"
    )


def test_microservice_architecture(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test microservice architecture scenario."""
    project = temp_repo / "lua_microservices"
    project.mkdir()

    (project / "microservices.lua").write_text(
        encoding="utf-8",
        data="""
local resty_http = require("resty.http")
local redis = require("resty.redis")
local jwt = require("resty.jwt")
local cjson = require("cjson")

local ServiceMesh = {}
ServiceMesh.__index = ServiceMesh

function ServiceMesh:new(config)
    local instance = {
        services = {},
        circuit_breakers = {},
        redis_client = redis:new(),
        http_client = resty_http:new(),
        config = config
    }
    setmetatable(instance, self)
    return instance
end

function ServiceMesh:register_service(name, endpoint)
    self.services[name] = {
        endpoint = endpoint,
        health_check = endpoint .. "/health",
        last_check = 0,
        status = "unknown",
        failures = 0
    }

    self.circuit_breakers[name] = {
        state = "closed",
        failure_count = 0,
        last_failure = 0,
        timeout = 30
    }
end

function ServiceMesh:check_service_health(service_name)
    local service = self.services[service_name]
    if not service then return false end

    local httpc = resty_http:new()
    local res, err = httpc:request_uri(service.health_check, {
        method = "GET",
        timeout = 5000
    })

    if res and res.status == 200 then
        service.status = "healthy"
        service.failures = 0
        self.circuit_breakers[service_name].failure_count = 0
        return true
    else
        service.status = "unhealthy"
        service.failures = service.failures + 1
        service.last_check = os.time()

        local cb = self.circuit_breakers[service_name]
        cb.failure_count = cb.failure_count + 1
        cb.last_failure = os.time()

        if cb.failure_count >= 5 then
            cb.state = "open"
            print(string.format("Circuit breaker opened for service: %s", service_name))
        end

        return false
    end
end

function ServiceMesh:call_service(service_name, path, method, data)
    local cb = self.circuit_breakers[service_name]

    if cb.state == "open" then
        if os.time() - cb.last_failure > cb.timeout then
            cb.state = "half-open"
        else
            error("Circuit breaker is open for service: " .. service_name)
        end
    end

    local service = self.services[service_name]
    local url = service.endpoint .. path

    local httpc = resty_http:new()
    local request_data = {
        method = method or "GET",
        timeout = 10000,
        headers = {
            ["Content-Type"] = "application/json",
            ["Authorization"] = "Bearer " .. self:get_service_token(service_name)
        }
    }

    if data then
        request_data.body = cjson.encode(data)
    end

    local res, err = httpc:request_uri(url, request_data)

    if res and res.status >= 200 and res.status < 300 then
        if cb.state == "half-open" then
            cb.state = "closed"
            cb.failure_count = 0
        end

        local response_data = cjson.decode(res.body)
        return response_data
    else
        self:handle_service_failure(service_name, err)
        error(string.format("Service call failed: %s", err or "Unknown error"))
    end
end

function ServiceMesh:get_service_token(service_name)
    local redis_key = "token:" .. service_name
    local token = self.redis_client:get(redis_key)

    if not token then
        token = self:generate_service_token(service_name)
        self.redis_client:setex(redis_key, 3600, token)
    end

    return token
end

function ServiceMesh:generate_service_token(service_name)
    local payload = {
        service = service_name,
        issued_at = os.time(),
        expires_at = os.time() + 3600
    }

    local token = jwt:sign(self.config.jwt_secret, payload)
    return token
end

local mesh = ServiceMesh:new({jwt_secret = "secret123"})
mesh:register_service("user-service", "http://users:8080")
mesh:register_service("order-service", "http://orders:8080")

local user_data = mesh:call_service("user-service", "/users/123", "GET")
local order_result = mesh:call_service("order-service", "/orders", "POST", {
    user_id = 123,
    items = {"item1", "item2"}
})
""",
    )

    (project / "main.lua").write_text(
        encoding="utf-8",
        data="""
local ServiceMesh = require('microservices')

local mesh = ServiceMesh:new({jwt_secret = "secret123"})
mesh:register_service("user-service", "http://users:8080")
mesh:register_service("order-service", "http://orders:8080")

local user_data = mesh:call_service("user-service", "/users/123", "GET")
""",
    )

    run_updater(project, mock_ingestor)

    defines_rels = get_relationships(mock_ingestor, "DEFINES")

    calls_rels = get_relationships(mock_ingestor, "CALLS")

    imports_rels = get_relationships(mock_ingestor, "IMPORTS")

    assert len(defines_rels) >= 6, (
        f"Expected at least 6 DEFINES relationships, got {len(defines_rels)}"
    )

    assert len(calls_rels) >= 1, (
        f"Expected at least 1 CALLS relationship, got {len(calls_rels)}"
    )

    assert len(imports_rels) >= 1, (
        f"Expected at least 1 IMPORTS relationship, got {len(imports_rels)}"
    )
