"""`GoResolver`: the package block a bare Go name binds through across the
package's files, which files of a directory are another package, the build
variants that declare one name again, and the type a bare type name names."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.parsers.call_site import CallSite
from codebase_rag.parsers.go.resolution import GoResolver
from codebase_rag.tests.collaborator_support import edges, index
from evals.recorded_graph import RecordingIngestor

_FILES = {
    "svc/a.go": (
        "package svc\n\ntype Mux struct{}\n\n"
        "func run() string { return helper() }\n"
        "func usePlat() int { return plat() }\n"
    ),
    "svc/b.go": 'package svc\n\nfunc helper() string { return "" }\n',
    "svc/v_linux.go": "//go:build linux\n\npackage svc\n\nfunc plat() int { return 1 }\n",
    "svc/v_windows.go": (
        "//go:build windows\n\npackage svc\n\nfunc plat() int { return 2 }\n"
    ),
    # Same directory, other packages: an external test package and a
    # generator under its own clause.
    "svc/x_test.go": 'package svc_test\n\nfunc helper() string { return "t" }\n',
    "svc/gen.go": "//go:build ignore\n\npackage main\n\nfunc plat() int { return 3 }\n",
    # Another package's same names.
    "other/o.go": 'package other\n\ntype Mux struct{}\n\nfunc helper() string { return "o" }\n',
}


@pytest.fixture
def resolver(
    temp_repo: Path, mock_ingestor: RecordingIngestor, monkeypatch: pytest.MonkeyPatch
) -> GoResolver:
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    updater = index(temp_repo / "gp", mock_ingestor, _FILES)
    go = updater.factory.call_processor._resolver.go
    assert isinstance(go, GoResolver)
    return go


def _site(call_name: str, module_qn: str = "gp.svc.a") -> CallSite:
    return CallSite(
        call_name, module_qn, None, None, None, cs.SupportedLanguage.GO, None, False
    )


def test_a_bare_call_binds_the_same_packages_function_in_another_file(
    resolver: GoResolver, mock_ingestor: RecordingIngestor
) -> None:
    assert resolver.resolve_go_same_package(_site("helper"), None) == (
        True,
        (cs.NodeLabel.FUNCTION, "gp.svc.b.helper"),
    )
    calls = edges(mock_ingestor, cs.RelationshipType.CALLS)
    assert ("gp.svc.a.run", "gp.svc.b.helper") in calls
    assert ("gp.svc.a.run", "gp.other.o.helper") not in calls
    assert ("gp.svc.a.run", "gp.svc.x_test.helper") not in calls


def test_a_qualified_or_unknown_name_is_left_to_the_other_stages(
    resolver: GoResolver,
) -> None:
    assert resolver.resolve_go_same_package(_site("other.helper"), None) == (
        False,
        None,
    )
    assert resolver.resolve_go_same_package(_site("missing"), None) == (False, None)


def test_a_build_variants_siblings_are_the_other_builds_copies_only(
    resolver: GoResolver, mock_ingestor: RecordingIngestor
) -> None:
    assert resolver.go_package_sibling_targets("gp.svc.v_linux.plat") == {
        (cs.NodeLabel.FUNCTION, "gp.svc.v_windows.plat")
    }
    calls = edges(mock_ingestor, cs.RelationshipType.CALLS)
    assert {target for source, target in calls if source == "gp.svc.a.usePlat"} == {
        "gp.svc.v_linux.plat",
        "gp.svc.v_windows.plat",
    }


def test_a_test_file_is_visible_to_no_production_caller(resolver: GoResolver) -> None:
    assert not resolver.go_declaration_is_visible("gp.svc.x_test.helper", "gp.svc.a")
    assert resolver.go_declaration_is_visible("gp.svc.b.helper", "gp.svc.a")
    # Another package of the same directory is not in the caller's block.
    assert not resolver.go_declaration_is_visible("gp.svc.gen.plat", "gp.svc.a")
    # A lookup through an import reaches another package, clause and all.
    assert resolver.go_declaration_is_visible("gp.other.o.helper", "gp.svc.a")


def test_a_bare_type_name_names_the_callers_packages_type(
    resolver: GoResolver,
) -> None:
    assert resolver.go_package_type("Mux", "gp.svc.b") == "gp.svc.a.Mux"
    assert resolver.go_package_type("Mux", "gp.other.o") == "gp.other.o.Mux"
    assert resolver.go_package_type("other.Mux", "gp.svc.b") is None
