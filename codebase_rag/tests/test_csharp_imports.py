# C# Phase 1: using directives become IMPORTS.
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_imports"
    project.mkdir()
    return project


def test_using_directives_emit_imports(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "App.cs").write_text(
        """
using System;
using System.Collections.Generic;
using Json = System.Text.Json;
global using System.Linq;

namespace App;
public class Program { public void Main() {} }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    imports = get_relationships(mock_ingestor, "IMPORTS")
    # Every using directive above should produce an IMPORTS edge.
    assert len(imports) >= 4, f"expected >=4 IMPORTS, got {len(imports)}"


def test_internal_namespace_import_targets_referenced_modules(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # A `using` of a namespace defined INSIDE the repo must land on the
    # specific Module nodes whose types the file actually references, not on
    # a dead-end ExternalModule keyed by the namespace string (issue #1347).
    host = csharp_project / "HostApp"
    lib = csharp_project / "PlatformLib"
    host.mkdir()
    lib.mkdir()
    (host / "Installer.cs").write_text(
        """
using System;
using Acme.Core.PlatformLib;

namespace Acme.Core.HostApp;
public class Installer
{
    public void Wire()
    {
        RemoteStorageProvider.Ping();
        Console.WriteLine("wired");
    }
}
""",
        encoding="utf-8",
    )
    (lib / "RemoteStorageProvider.cs").write_text(
        """
namespace Acme.Core.PlatformLib;
public class RemoteStorageProvider
{
    public static void Ping() {}
}
""",
        encoding="utf-8",
    )
    (lib / "Unused.cs").write_text(
        """
namespace Acme.Core.PlatformLib;
public class UnusedThing
{
    public static void Noop() {}
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    imports = {
        (str(c.args[0][0]), str(c.args[0][2]), str(c.args[2][0]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    installer = next(
        (f for _fl, f, _tl, _t in imports if f.endswith(".HostApp.Installer")), None
    )
    assert installer is not None, imports

    provider_edges = [
        edge
        for edge in imports
        if edge[1] == installer
        and edge[3].endswith(".PlatformLib.RemoteStorageProvider")
    ]
    assert provider_edges, imports
    assert all(edge[2] == "Module" for edge in provider_edges), provider_edges

    assert not any(
        edge[1] == installer and edge[3] == "Acme.Core.PlatformLib" for edge in imports
    ), imports
    assert not any(
        edge[1] == installer and edge[3].endswith(".PlatformLib.Unused")
        for edge in imports
    ), imports
    # A genuinely external namespace keeps its ExternalModule edge.
    assert any(
        edge[1] == installer and edge[2] == "ExternalModule" and edge[3] == "System"
        for edge in imports
    ), imports
