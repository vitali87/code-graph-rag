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
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.cli_help import CLICommandName
from codebase_rag.config import AppConfig
from codebase_rag.mcp import tools as mcp_tools
from codec import schema_pb2
from scripts.public_surface import (
    SNAPSHOT,
    _json_value,
    build_surface,
    cli_commands,
    mcp_tool_list,
    protobuf_enums,
    python_sdk,
    render,
    setting_list,
)

ROOT = cs.SURFACE_ROOT_COMMAND


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


@pytest.mark.parametrize("installed", [True, False])
def test_the_tool_list_is_the_same_whichever_extras_are_installed(
    installed: bool,
) -> None:
    # Bot review on PR #3636: the structural tools are served only with the
    # `ast-grep` extra, so a machine without it wrote a different list.
    with (
        patch.object(mcp_tools, "has_ast_grep", lambda: installed),
        patch.object(mcp_tools, "has_semantic_dependencies", lambda: installed),
    ):
        tools = {tool["name"]: tool for tool in mcp_tool_list()}

    for name in (cs.MCPToolName.STRUCTURAL_SEARCH, cs.MCPToolName.STRUCTURAL_REPLACE):
        assert tools[name]["needs_extra"] == cs.SurfaceExtra.AST_GREP
    assert (
        tools[cs.MCPToolName.SEMANTIC_SEARCH]["needs_extra"] == cs.SurfaceExtra.SEMANTIC
    )


def test_the_global_options_are_recorded_on_the_root_command() -> None:
    commands = {command["command"]: command for command in cli_commands()}

    flags = {flag for param in commands[ROOT]["params"] for flag in param["flags"]}

    assert {"--version", "-v", "--quiet", "-q"} <= flags


def test_an_option_records_the_values_it_accepts() -> None:
    commands = {command["command"]: command for command in cli_commands()}
    serve = commands[f"{ROOT} {CLICommandName.MCP_SERVER}"]["params"]
    callers = commands[f"{ROOT} {CLICommandName.GRAPH} callers"]["params"]

    transports = next(param["choices"] for param in serve if param["choices"])
    depth = next(param["bounds"] for param in callers if param["bounds"])

    assert set(transports) == {str(item) for item in cs.MCPTransport}
    assert depth is not None
    assert depth["min"] == 1


def test_a_setting_is_recorded_under_the_environment_variable_it_is_read_from() -> None:
    settings = {setting["name"]: setting for setting in setting_list()}

    for name, field in AppConfig.model_fields.items():
        if isinstance(field.validation_alias, str):
            assert settings[name]["env"] == [field.validation_alias]
        elif field.validation_alias is None:
            assert settings[name]["env"] == [name]


def test_every_relationship_type_number_is_recorded() -> None:
    enums = {enum["name"]: enum["values"] for enum in protobuf_enums()}
    stored = schema_pb2.Relationship.RelationshipType

    assert enums["Relationship.RelationshipType"] == {
        name: stored.Value(name) for name in stored.keys()
    }


def test_an_sdk_class_records_its_public_methods() -> None:
    sdk = {entry["name"]: entry for entry in python_sdk()}

    loader = {member["name"] for member in sdk["GraphLoader"]["members"]}
    generator = {member["name"] for member in sdk["CypherGenerator"]["members"]}
    every = {member["name"] for entry in sdk.values() for member in entry["members"]}

    assert "find_nodes_by_label" in loader
    assert "generate" in generator
    assert not any(
        name.startswith((cs.SURFACE_PRIVATE_PREFIX, cs.SURFACE_PYDANTIC_PREFIX))
        for name in every
    )
