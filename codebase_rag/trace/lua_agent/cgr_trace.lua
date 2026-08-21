-- cgr runtime call tracer for Lua (issue #1254).
--
-- Pure Lua, no dependencies: `debug.sethook(..., "c")` fires on every call,
-- and `debug.getinfo` supplies the callee's definition site plus the
-- caller's current line. Calls are exact (not sampled) and aggregate in
-- memory; the trace is written in the cgr interchange format (JSONL,
-- format version 1) when the state is garbage-collected at VM shutdown or
-- when `write()` is called explicitly (required under LuaJIT, whose plain
-- tables have no __gc).
--
-- Usage:
--   CGR_TRACE_REPO=/abs/repo lua -l cgr_trace script.lua
-- Environment: CGR_TRACE_REPO (required scope), CGR_TRACE_OUTPUT
-- (default cgr-trace.jsonl), CGR_TRACE_WORKLOAD (optional label).
--
-- C functions and out-of-repo frames are glue: edges see through them to
-- the nearest in-repo caller, so dispatch through pcall, table.sort
-- comparators, and C-driven callbacks attributes to the code that
-- scheduled it. Lua dispatch is dynamic by construction (tables of
-- functions, metatables, __index chains), which is exactly what the static
-- tier cannot see.

local M = {}

local root = os.getenv("CGR_TRACE_REPO") or ""
-- Strip every trailing slash (`/repo`, `/repo/`, `/repo//` all normalise to
-- the same root) so the boundary check below stays exact.
root = (root:gsub("/+$", ""))
local output = os.getenv("CGR_TRACE_OUTPUT") or "cgr-trace.jsonl"
local workload = os.getenv("CGR_TRACE_WORKLOAD")

local edges = {}
local written = false

local function frame_of(info, line)
  if not info or info.what == "C" then
    return nil
  end
  local src = info.source
  if type(src) ~= "string" or src:sub(1, 1) ~= "@" then
    return nil
  end
  local path = src:sub(2)
  -- Scripts invoked by relative path surface relative sources; anchor them
  -- to the shell's working directory (standard Lua has no getcwd).
  if path:sub(1, 1) ~= "/" then
    local cwd = os.getenv("PWD")
    if cwd and cwd ~= "" then
      path = cwd .. "/" .. path
    else
      path = root .. "/" .. path
    end
  end
  -- Boundary-aware: root /work/repo must not admit the sibling
  -- /work/repo-private, only the root itself and paths under it.
  if root == "" or (path ~= root and path:sub(1, #root + 1) ~= root .. "/") then
    return nil
  end
  local name
  if info.what == "main" then
    name = "<module>"
  else
    name = info.name
    -- Table-dispatched and vararg-invoked functions surface as nil or "?";
    -- their definition site is what resolution needs.
    if name == nil or name == "" or name == "?" then
      name = "<anonymous>"
    end
  end
  if line == nil or line < 0 then
    line = 0
  end
  return { path = path, name = name, line = line }
end

local function on_call()
  local callee_info = debug.getinfo(2, "Sn")
  local callee = frame_of(callee_info, callee_info and callee_info.linedefined)
  if not callee then
    return
  end
  local depth = 3
  while true do
    local caller_info = debug.getinfo(depth, "Snl")
    if not caller_info then
      return
    end
    local caller = frame_of(caller_info, caller_info.currentline)
    if caller then
      local key = table.concat({
        caller.path, caller.name, tostring(caller.line),
        callee.path, callee.name, tostring(callee.line),
      }, "\1")
      local edge = edges[key]
      if edge then
        edge.count = edge.count + 1
      else
        edges[key] = { caller = caller, callee = callee, count = 1 }
      end
      return
    end
    depth = depth + 1
  end
end

local function json_string(value)
  local escaped = value:gsub('[%c"\\]', function(char)
    if char == '"' then
      return '\\"'
    elseif char == "\\" then
      return "\\\\"
    elseif char == "\n" then
      return "\\n"
    elseif char == "\r" then
      return "\\r"
    elseif char == "\t" then
      return "\\t"
    end
    return string.format("\\u%04x", string.byte(char))
  end)
  return '"' .. escaped .. '"'
end

local function json_frame(frame)
  return string.format(
    '{"path":%s,"qualname":%s,"line":%d}',
    json_string(frame.path), json_string(frame.name), frame.line
  )
end

function M.write()
  if written then
    return 0
  end
  written = true
  debug.sethook()
  local fh, err = io.open(output, "w")
  if not fh then
    io.stderr:write("cgr-trace-lua: cannot write trace: " .. tostring(err) .. "\n")
    return 0
  end
  local workloads = "[]"
  if workload and workload ~= "" then
    workloads = "[" .. json_string(workload) .. "]"
  end
  fh:write(string.format(
    '{"kind":"header","version":1,"language":"lua","repo_root":%s,"tracer":"cgr-trace-lua"}\n',
    json_string(root)
  ))
  local count = 0
  for _, edge in pairs(edges) do
    fh:write(string.format(
      '{"kind":"call","caller":%s,"callee":%s,"count":%d,"workloads":%s,"receiver_types":[]}\n',
      json_frame(edge.caller), json_frame(edge.callee), edge.count, workloads
    ))
    count = count + 1
  end
  fh:close()
  return count
end

if root == "" then
  io.stderr:write(
    "cgr-trace-lua: CGR_TRACE_REPO is not set; tracing disabled\n"
  )
else
  debug.sethook(on_call, "c")
  -- Lua 5.2+ runs __gc on tables at VM close; LuaJIT (5.1) does not, so
  -- embedders there must call require("cgr_trace").write() themselves.
  M.sentinel = setmetatable({}, { __gc = function() M.write() end })
end

return M
