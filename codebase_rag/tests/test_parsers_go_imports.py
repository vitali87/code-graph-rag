"""`GoImporter`: a Go file's import specs in the shared import map, under
their alias, their last path segment, or a dot key for a dot import, with a
path under a go.mod `module` directive rewritten to its package's qn."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.go.imports import GoImporter
from codebase_rag.tests.collaborator_support import edges, index
from evals.recorded_graph import RecordingIngestor

_FILES = {
    "go.mod": "module example.com/app\n\ngo 1.21\n",
    "util/util.go": "package util\n\nfunc Upper(s string) string { return s }\n",
    "main.go": (
        "package main\n\n"
        "import (\n"
        '\tu "example.com/app/util"\n'
        '\t"example.com/app"\n'
        '\t"fmt"\n'
        '\t. "strings"\n'
        '\t"github.com/acme/lib/v2"\n'
        ")\n\n"
        'import "os"\n\n'
        "func main() { fmt.Println(u.Upper(ToUpper(os.Args[0]))) }\n"
    ),
}


@pytest.fixture
def updater(
    temp_repo: Path, mock_ingestor: RecordingIngestor, monkeypatch: pytest.MonkeyPatch
) -> GraphUpdater:
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    return index(temp_repo / "gp", mock_ingestor, _FILES)


def test_each_spec_binds_its_name_in_the_files_import_map(
    updater: GraphUpdater,
) -> None:
    assert isinstance(updater.factory.import_processor.go, GoImporter)
    assert updater.factory.import_processor.import_mapping["gp.main"] == {
        # An alias names the package; a module path is the project's qn.
        "u": "gp.util",
        # The module root itself is the project.
        "app": "gp",
        "fmt": "fmt",
        # A dot import keeps its package under a key no identifier can be.
        ".strings": "strings",
        # Outside every go.mod: the written path, under its last segment.
        "v2": "github.com/acme/lib/v2",
        "os": "os",
    }


def test_an_import_of_the_projects_own_package_calls_into_it(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    calls = edges(mock_ingestor, cs.RelationshipType.CALLS)
    assert ("gp.main.main", "gp.util.util.Upper") in calls
