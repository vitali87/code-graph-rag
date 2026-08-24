# C# Phase 1: using directives become IMPORTS.
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.constants import SupportedLanguage
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


def test_method_group_only_usage_still_pins_the_import(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # The importing file never INVOKES anything from the namespace; it only
    # passes a method group as a delegate. The reference resolution is the
    # sole evidence, and it must still pin the IMPORTS edge (issue #1347).
    host = csharp_project / "HostApp"
    lib = csharp_project / "PlatformLib"
    host.mkdir()
    lib.mkdir()
    (host / "Wiring.cs").write_text(
        """
using System;
using Acme.Core.PlatformLib;

namespace Acme.Core.HostApp;
public class Wiring
{
    public void Hook()
    {
        Register(Provider.Handle);
    }

    private void Register(Action target)
    {
        target();
    }
}
""",
        encoding="utf-8",
    )
    (lib / "Provider.cs").write_text(
        """
namespace Acme.Core.PlatformLib;
public class Provider
{
    public static void Handle() {}
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    imports = {
        (str(c.args[0][2]), str(c.args[2][0]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    wiring = next((f for f, _tl, _t in imports if f.endswith(".HostApp.Wiring")), None)
    assert wiring is not None, imports
    assert any(
        f == wiring and t.endswith(".PlatformLib.Provider") and tl == "Module"
        for f, tl, t in imports
    ), imports
    assert not any(
        f == wiring and t == "Acme.Core.PlatformLib" for f, _tl, t in imports
    ), imports


def test_unparsed_module_namespaces_recover_at_flush(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # An incremental run re-parses only CHANGED files: the provider module is
    # known (rehydrated) but its parse_imports never ran this run, so its
    # declared namespace must be recovered from source at flush time or the
    # internal detection silently fails (issue #1347).
    from codebase_rag.parsers.import_processor import ImportProcessor

    lib = csharp_project / "PlatformLib"
    lib.mkdir()
    provider_path = lib / "RemoteStorageProvider.cs"
    provider_path.write_text(
        """
namespace Acme.Core.PlatformLib;
public class RemoteStorageProvider
{
    public static void Ping() {}
}
""",
        encoding="utf-8",
    )
    processor = ImportProcessor(
        repo_path=csharp_project, project_name="proj", ingestor=mock_ingestor
    )
    installer_qn = "proj.HostApp.Installer"
    provider_qn = "proj.PlatformLib.RemoteStorageProvider"
    processor.import_mapping[installer_qn] = {"PlatformLib": "Acme.Core.PlatformLib"}
    processor.defer_import_edge(
        installer_qn, "Acme.Core.PlatformLib", SupportedLanguage.CSHARP
    )
    processor.record_resolved_cross_module_use(installer_qn, provider_qn)
    emitted = processor.flush_deferred_import_edges(
        {
            installer_qn: str(csharp_project / "HostApp" / "Installer.cs"),
            provider_qn: str(provider_path),
        }
    )
    assert emitted == 1
    edges = {
        (str(c.args[0][2]), str(c.args[2][0]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    assert (installer_qn, "Module", provider_qn) in edges, edges


def test_type_only_namespace_use_still_pins_the_import(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # The importing file uses the namespace ONLY in type positions (a field
    # declaration); no call, inherit, or delegate resolution ever fires, so
    # the declared-type-name evidence must carry the edge (issue #1347).
    host = csharp_project / "HostApp"
    lib = csharp_project / "PlatformLib"
    host.mkdir()
    lib.mkdir()
    (host / "Holder.cs").write_text(
        """
using Acme.Core.PlatformLib;

namespace Acme.Core.HostApp;
public class Holder
{
    private RemoteStorageProvider _provider;

    public Holder(RemoteStorageProvider provider)
    {
        _provider = provider;
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
        (str(c.args[0][2]), str(c.args[2][0]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    holder = next((f for f, _tl, _t in imports if f.endswith(".HostApp.Holder")), None)
    assert holder is not None, imports
    assert any(
        f == holder
        and tl == "Module"
        and t.endswith(".PlatformLib.RemoteStorageProvider")
        for f, tl, t in imports
    ), imports
    assert not any(
        f == holder and t.endswith(".PlatformLib.Unused") for f, _tl, t in imports
    ), imports


def test_watched_modification_reemits_internal_import_edges(
    csharp_project: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A watched edit runs the realtime path, not run(): the deferred import
    # flush must happen there too, or the live graph loses the module-level
    # import edges until the next full index (issue #1347).
    from typing import Protocol, runtime_checkable

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    from watchdog.events import FileModifiedEvent

    import realtime_updater
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()
    if "c_sharp" not in parsers:
        pytest.skip("c_sharp parser not available")

    host = csharp_project / "HostApp"
    lib = csharp_project / "PlatformLib"
    host.mkdir()
    lib.mkdir()
    installer = host / "Installer.cs"
    installer.write_text(
        """
using Acme.Core.PlatformLib;

namespace Acme.Core.HostApp;
public class Installer
{
    public void Wire()
    {
        RemoteStorageProvider.Ping();
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
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=csharp_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    installer.write_text(
        installer.read_text(encoding="utf-8") + "\n// touched\n", encoding="utf-8"
    )
    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(installer)))

    imports = {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    assert any(
        f.endswith(".HostApp.Installer")
        and t.endswith(".PlatformLib.RemoteStorageProvider")
        for f, t in imports
    ), imports


def test_a_locally_declared_name_is_not_evidence_for_an_import(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # The file declares its OWN RemoteStorageProvider and never uses the
    # imported namespace: an unqualified mention binds to the local type, so
    # the name must not count as evidence for the namespace's module.
    host = csharp_project / "HostApp"
    lib = csharp_project / "PlatformLib"
    host.mkdir()
    lib.mkdir()
    (host / "Shadow.cs").write_text(
        """
using Acme.Core.PlatformLib;

namespace Acme.Core.HostApp;
public class RemoteStorageProvider
{
    public static void Ping() {}
}

public class Shadow
{
    public void Wire()
    {
        RemoteStorageProvider.Ping();
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
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    imports = {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    assert not any(
        f.endswith(".HostApp.Shadow")
        and t.endswith(".PlatformLib.RemoteStorageProvider")
        for f, t in imports
    ), imports


def test_delegate_only_namespace_use_still_pins_the_import(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    host = csharp_project / "HostApp"
    lib = csharp_project / "PlatformLib"
    host.mkdir()
    lib.mkdir()
    (host / "Consumer.cs").write_text(
        """
using Acme.Core.PlatformLib;

namespace Acme.Core.HostApp;
public class Consumer
{
    private StorageChanged _handler;
}
""",
        encoding="utf-8",
    )
    (lib / "Events.cs").write_text(
        """
namespace Acme.Core.PlatformLib;
public delegate void StorageChanged(string path);
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    imports = {
        (str(c.args[0][2]), str(c.args[2][0]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    assert any(
        f.endswith(".HostApp.Consumer")
        and tl == "Module"
        and t.endswith(".PlatformLib.Events")
        for f, tl, t in imports
    ), imports


def test_watched_provider_edit_keeps_the_importers_edge(
    csharp_project: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Editing the PROVIDER deletes and recreates its Module node, severing the
    # unchanged importer's edge; the importer never re-parses, so its using
    # entries must be requeued from the persistent import mapping or the edge
    # stays lost until a full re-index (issue #1347).
    from typing import Protocol, runtime_checkable

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    from watchdog.events import FileModifiedEvent

    import realtime_updater
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()
    if "c_sharp" not in parsers:
        pytest.skip("c_sharp parser not available")

    host = csharp_project / "HostApp"
    lib = csharp_project / "PlatformLib"
    host.mkdir()
    lib.mkdir()
    (host / "Installer.cs").write_text(
        """
using Acme.Core.PlatformLib;

namespace Acme.Core.HostApp;
public class Installer
{
    public void Wire()
    {
        RemoteStorageProvider.Ping();
    }
}
""",
        encoding="utf-8",
    )
    provider = lib / "RemoteStorageProvider.cs"
    provider.write_text(
        """
namespace Acme.Core.PlatformLib;
public class RemoteStorageProvider
{
    public static void Ping() {}
}
""",
        encoding="utf-8",
    )
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=csharp_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    provider.write_text(
        provider.read_text(encoding="utf-8") + "\n// touched\n", encoding="utf-8"
    )
    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(provider)))

    imports = {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    assert any(
        f.endswith(".HostApp.Installer")
        and t.endswith(".PlatformLib.RemoteStorageProvider")
        for f, t in imports
    ), imports


def test_a_name_position_identifier_is_not_import_evidence(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # The file's only textual match with the namespace's declared types is a
    # PARAMETER NAME; a name position declares, it does not reference, so it
    # must not pin the import.
    host = csharp_project / "HostApp"
    lib = csharp_project / "PlatformLib"
    host.mkdir()
    lib.mkdir()
    (host / "Naming.cs").write_text(
        """
using Acme.Core.PlatformLib;

namespace Acme.Core.HostApp;
public class Naming
{
    public void Configure(string RemoteStorageProvider)
    {
        System.Console.WriteLine(RemoteStorageProvider);
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
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    imports = {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    assert not any(
        f.endswith(".HostApp.Naming")
        and t.endswith(".PlatformLib.RemoteStorageProvider")
        for f, t in imports
    ), imports


def test_watched_provider_deletion_stops_targeting_its_module(
    csharp_project: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deleting the provider must also drop its in-memory C# import state, or
    # the requeue rebuilds an IMPORTS edge to a Module that no longer exists,
    # diverging from what a clean rebuild of the same tree would produce.
    from typing import Protocol, runtime_checkable

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    from watchdog.events import FileDeletedEvent

    import realtime_updater
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()
    if "c_sharp" not in parsers:
        pytest.skip("c_sharp parser not available")

    host = csharp_project / "HostApp"
    lib = csharp_project / "PlatformLib"
    host.mkdir()
    lib.mkdir()
    (host / "Installer.cs").write_text(
        """
using Acme.Core.PlatformLib;

namespace Acme.Core.HostApp;
public class Installer
{
    public void Wire()
    {
        RemoteStorageProvider.Ping();
    }
}
""",
        encoding="utf-8",
    )
    provider = lib / "RemoteStorageProvider.cs"
    provider.write_text(
        """
namespace Acme.Core.PlatformLib;
public class RemoteStorageProvider
{
    public static void Ping() {}
}
""",
        encoding="utf-8",
    )
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=csharp_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    provider.unlink()
    mock_ingestor.reset_mock()
    handler.dispatch(FileDeletedEvent(str(provider)))

    imports = {
        (str(c.args[0][2]), str(c.args[2][0]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    assert not any(
        tl == "Module" and t.endswith(".PlatformLib.RemoteStorageProvider")
        for _f, tl, t in imports
    ), imports


def test_a_namesake_property_keeps_the_type_evidence(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # `RemoteStorageProvider RemoteStorageProvider { get; set; }` is idiomatic
    # C#: the property NAME matches the TYPE. The type-position use is real
    # evidence and must survive the name-position subtraction.
    host = csharp_project / "HostApp"
    lib = csharp_project / "PlatformLib"
    host.mkdir()
    lib.mkdir()
    (host / "Options.cs").write_text(
        """
using Acme.Core.PlatformLib;

namespace Acme.Core.HostApp;
public class Options
{
    public RemoteStorageProvider RemoteStorageProvider { get; set; }
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
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    imports = {
        (str(c.args[0][2]), str(c.args[2][0]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "IMPORTS")
    }
    assert any(
        f.endswith(".HostApp.Options")
        and tl == "Module"
        and t.endswith(".PlatformLib.RemoteStorageProvider")
        for f, tl, t in imports
    ), imports
