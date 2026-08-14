# The Lua agent must observe table-dispatched calls (invisible to static
# analysis), see through C-function glue, aggregate exact counts, and write
# a valid interchange trace that the standard ingest pipeline resolves
# (issue #1254).

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.trace.records import read_trace_file

_AGENT_DIR = Path(__file__).resolve().parents[1] / "trace" / "lua_agent"

_SAMPLE = """
    local HANDLERS = {}

    local function greet()
        return "hi"
    end

    local function register(name, fn)
        HANDLERS[name] = fn
    end

    local function handle(name)
        -- Not a tail call: `return HANDLERS[name]()` would replace this
        -- frame entirely and attribute the callee to run_all.
        local result = HANDLERS[name]()
        return result
    end

    local function run_all()
        register("greet", greet)
        local out = ""
        for _ = 1, 3 do
            out = out .. handle("greet")
        end
        -- A C-function boundary between two project frames.
        local keys = { "b", "a" }
        table.sort(keys, function(x, y)
            return x < y
        end)
        return out
    end

    print(run_all())
    -- Write explicitly so the workload produces a trace under LuaJIT too,
    -- where the __gc-at-shutdown fallback does not run.
    require("cgr_trace").write()
"""

lua = shutil.which("lua")
pytestmark = pytest.mark.skipif(lua is None, reason="lua interpreter not available")


def _run_traced_lua(tmp_path: Path) -> tuple:
    script = tmp_path / "main.lua"
    script.write_text(textwrap.dedent(_SAMPLE))
    output = tmp_path / "cgr-trace.jsonl"
    env = dict(
        os.environ,
        CGR_TRACE_REPO=str(tmp_path),
        CGR_TRACE_OUTPUT=str(output),
        CGR_TRACE_WORKLOAD="lua-run",
        LUA_PATH=f"{_AGENT_DIR}/?.lua;;",
    )
    result = subprocess.run(
        [str(lua), "-l", "cgr_trace", str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert output.exists(), result.stderr
    header, records = read_trace_file(output)
    return header, list(records)


def test_agent_records_registry_dispatch_with_exact_counts(tmp_path):
    header, records = _run_traced_lua(tmp_path)

    assert header.language == "lua"
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    # A table-dispatched function has no runtime name; its definition site
    # is what span resolution needs.
    dispatch = edges.get(("handle", cs.TRACE_QUALNAME_ANONYMOUS))
    assert dispatch is not None, sorted(edges)
    assert dispatch.count == 3
    assert dispatch.callee.path.endswith("main.lua")
    assert dispatch.callee.line > 0
    assert edges[("run_all", "handle")].count == 3


def test_agent_sees_through_c_function_glue(tmp_path):
    _header, records = _run_traced_lua(tmp_path)

    # table.sort invokes the comparator; the edge must attribute to run_all.
    edges = {(r.caller.qualname, r.callee.qualname) for r in records}
    assert ("run_all", "<anonymous>") in edges


def test_agent_scopes_to_repo_and_labels_workloads(tmp_path):
    _header, records = _run_traced_lua(tmp_path)

    assert records
    root = str(tmp_path)
    for record in records:
        assert record.caller.path.startswith(root)
        assert record.callee.path.startswith(root)
        assert record.workloads == ("lua-run",)


def test_relative_script_invocations_stay_in_scope(tmp_path):
    # `lua -l cgr_trace main.lua` (no absolute path) surfaces sources as
    # `@main.lua`; the agent must anchor them to the working directory or
    # every frame silently falls out of scope.
    script = tmp_path / "main.lua"
    script.write_text(textwrap.dedent(_SAMPLE))
    output = tmp_path / "cgr-trace.jsonl"
    env = dict(
        os.environ,
        CGR_TRACE_REPO=str(tmp_path),
        CGR_TRACE_OUTPUT=str(output),
        PWD=str(tmp_path),
        LUA_PATH=f"{_AGENT_DIR}/?.lua;;",
    )
    result = subprocess.run(
        [str(lua), "-l", "cgr_trace", "main.lua"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr

    _header, records = read_trace_file(output)
    assert list(records)


def test_sibling_directories_sharing_the_root_prefix_stay_out_of_scope(tmp_path):
    # /work/repo must not admit /work/repo-private frames.
    repo = tmp_path / "repo"
    repo.mkdir()
    sibling = tmp_path / "repo-private"
    sibling.mkdir()
    (sibling / "helper.lua").write_text(
        "local function secret()\n    return 1\nend\nsecret()\nreturn true\n"
    )
    script = repo / "main.lua"
    script.write_text(
        'dofile("'
        + str(sibling / "helper.lua")
        + '")\nprint("ok")\nrequire("cgr_trace").write()\n'
    )
    output = repo / "cgr-trace.jsonl"
    env = dict(
        os.environ,
        CGR_TRACE_REPO=str(repo),
        CGR_TRACE_OUTPUT=str(output),
        LUA_PATH=f"{_AGENT_DIR}/?.lua;;",
    )
    result = subprocess.run(
        [str(lua), "-l", "cgr_trace", str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=repo,
    )
    assert result.returncode == 0, result.stderr

    _header, records = read_trace_file(output)
    for record in records:
        assert "repo-private" not in record.caller.path
        assert "repo-private" not in record.callee.path


def test_main_chunk_frames_use_module_marker(tmp_path):
    _header, records = _run_traced_lua(tmp_path)

    toplevel = [r for r in records if r.caller.qualname == cs.TRACE_QUALNAME_MODULE]
    assert toplevel
