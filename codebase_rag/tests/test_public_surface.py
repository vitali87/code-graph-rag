"""The checked-in public surface matches the code (issue #3629).

`docs/reference/public-surface.json` records what users and agents reach:
commands and options, exit codes, MCP tools and their input schemas,
settings, graph labels and relationships, the export's records, the protobuf
index and the Python SDK. A change to any of them must show up in a pull
request as a change to that file, so it is reviewed, and so the Rust rewrite
can be held to it. A removal or rename there breaks users: it goes through
the breaking-change process in CONTRIBUTING.md.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath, PureWindowsPath

from codebase_rag import constants as cs
from scripts.public_surface import (
    SNAPSHOT,
    _json_value,
    build_surface,
    mcp_tool_list,
    render,
)


def test_the_public_surface_snapshot_is_current() -> None:
    recorded = json.loads(SNAPSHOT.read_text(encoding=cs.ENCODING_UTF8))
    current = json.loads(render(build_surface()))
    stale = [
        section for section in current if recorded.get(section) != current[section]
    ]
    assert not stale, (
        f"The public surface changed in {stale}. If the change is intended, "
        f"run `{cs.SURFACE_REGENERATE}` and commit {cs.SURFACE_SNAPSHOT_PATH}."
    )


def test_a_path_default_reads_the_same_on_every_platform() -> None:
    assert _json_value(PureWindowsPath("a") / "b") == "a/b"
    assert _json_value(PurePosixPath("a") / "b") == "a/b"


def test_the_semantic_tool_is_recorded_whether_or_not_its_extra_is_installed() -> None:
    tools = {tool["name"]: tool for tool in mcp_tool_list()}

    assert (
        tools[cs.MCPToolName.SEMANTIC_SEARCH]["needs_extra"] == cs.SurfaceExtra.SEMANTIC
    )
    assert tools[cs.MCPToolName.LIST_PROJECTS]["needs_extra"] is None
