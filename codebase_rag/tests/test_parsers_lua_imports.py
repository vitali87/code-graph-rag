"""`LuaImporter`: `require` calls, `pcall(require, ...)` and the standard
library's heads in the shared import map, a project file resolving to its
module qn and anything else staying the written path."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.lua.imports import LuaImporter, _lua_relative_module
from codebase_rag.tests.collaborator_support import edges, index
from evals.recorded_graph import RecordingIngestor

_FILES = {
    "util/strings.lua": "local M = {}\nreturn M\n",
    "lib/helpers.lua": "local H = {}\nreturn H\n",
    "lib/main.lua": (
        'local strings = require("util.strings")\n'
        'local slashed = require("util/strings")\n'
        'local ok, json = pcall(require, "json")\n'
        'local helpers = require("./helpers")\n'
        'require("socket.http")\n'
        'local s = string.format("%d", 1)\n'
    ),
}


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: RecordingIngestor) -> GraphUpdater:
    return index(temp_repo / "lp", mock_ingestor, _FILES)


def test_requires_bind_their_local_names(updater: GraphUpdater) -> None:
    mapping = updater.factory.import_processor.import_mapping["lp.lib.main"]
    assert mapping == {
        "strings": "lp.util.strings",
        "slashed": "lp.util.strings",
        # The second name `pcall` binds is the module.
        "json": "json",
        "helpers": "lp.lib.helpers",
        # An unbound require is named by its last segment.
        "http": "socket.http",
        "string": "string",
    }


def test_project_modules_and_external_ones_get_their_own_targets(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    imports = edges(mock_ingestor, cs.RelationshipType.IMPORTS)
    assert {
        ("lp.lib.main", "lp.util.strings"),
        ("lp.lib.main", "lp.lib.helpers"),
        ("lp.lib.main", "json"),
        ("lp.lib.main", "string"),
    } <= imports


def test_the_importer_is_the_processors_lua_strategy(updater: GraphUpdater) -> None:
    importer = updater.factory.import_processor.lua
    assert isinstance(importer, LuaImporter)
    assert importer._resolve_lua_module_path("util.strings", "lp.lib.main") == (
        "lp.util.strings"
    )
    assert importer._resolve_lua_module_path("missing.mod", "lp.lib.main") == (
        "missing.mod"
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("./helpers", "lp.lib.helpers"),
        ("../util/strings", "lp.util.strings"),
        ("./a/../b", "lp.lib.b"),
        ("..\\util\\x", "lp.util.x"),
        # Climbing past the top stops there.
        ("../../../top", "top"),
    ],
)
def test_relative_requires_resolve_against_the_importing_package(
    path: str, expected: str
) -> None:
    assert _lua_relative_module(path, "lp.lib.main") == expected
