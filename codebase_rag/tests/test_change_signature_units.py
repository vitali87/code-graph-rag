from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing import ParamSpec, SignatureRefused, change_signature
from codebase_rag.editing.signature import parse_param_spec
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.change_signature_helpers import (
    PROJECT,
    _index,
    _qn,
    _write,
    repo_fixture,
)
from evals.cgr_graph import _StatefulIngestor


@pytest.fixture
def repo(temp_repo: Path):
    return repo_fixture(temp_repo)


# --- units -------------------------------------------------------------------------


def test_parse_param_spec_forms() -> None:
    assert parse_param_spec("a@0") == ParamSpec("a", from_index=0)
    assert parse_param_spec("a@old") == ParamSpec("a", from_name="old")
    assert parse_param_spec("n:int=1") == ParamSpec(
        "n", literal="1", annotation="int", default="1"
    )
    assert parse_param_spec("b:str='x'@1") == ParamSpec(
        "b", from_index=1, annotation="str", default="'x'"
    )
    assert parse_param_spec("extra").unmapped
    with pytest.raises(SignatureRefused):
        parse_param_spec("1bad")


def test_unknown_source_and_duplicate_names_are_refused(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    with pytest.raises(SignatureRefused, match="No old parameter"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.util.helper"),
            ["a@zz"],
            dry_run=True,
        )
    with pytest.raises(SignatureRefused, match="listed twice"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.util.helper"),
            ["a@0", "a@1"],
            dry_run=True,
        )


async def test_mcp_change_signature_tool_reports_sites_and_unmapped(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    root, store, updater = repo
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    registry._live_updater = updater
    schema = next(
        s
        for s in registry.get_tool_schemas()
        if s.name == cs.MCPToolName.CHANGE_SIGNATURE
    )
    assert set(schema.inputSchema["required"]) == {
        cs.MCPParamName.QUALIFIED_NAME,
        cs.MCPParamName.NEW_PARAMS,
    }
    payload = await registry.change_signature(
        qualified_name=_qn("pkg.util.helper"),
        new_params=["a@0", "n:int=1", "b@1"],
        project=PROJECT,
    )
    assert isinstance(payload, dict)
    assert payload["applied"] is True
    assert len(payload[cs.KEY_SITES]) == 3 and payload[cs.KEY_UNMAPPED] == []
    assert payload[cs.KEY_VERDICT]["ok"] is True
    refused = await registry.change_signature(
        qualified_name=_qn("pkg.util.helper"), new_params=["a@zz"], project=PROJECT
    )
    assert isinstance(refused, dict) and cs.DICT_KEY_ERROR in refused


def test_a_chained_call_rewrites_its_own_arguments(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/util.py", "def helper(a):\n    return str(a)\n")
    _write(
        root,
        "pkg/app.py",
        "from pkg.util import helper\n\n\ndef run():\n    return helper(2).upper()\n",
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        ["a@0", "n:int=1"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    # `helper(2)`'s arguments, not the outer `.upper()` call's.
    assert "return helper(2, 1).upper()" in (root / "pkg/app.py").read_text()


def test_a_variadic_definition_is_refused(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/util.py", "def helper(a, *args):\n    return a\n")
    _write(
        root,
        "pkg/app.py",
        "from pkg.util import helper\n\n\ndef run():\n    return helper(1, 2, 3)\n",
    )
    store, _updater = _index(root)
    with pytest.raises(SignatureRefused):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.pkg.util.helper",
            ["a@0", "args@1"],
        )
    assert "helper(1, 2, 3)" in (root / "pkg/app.py").read_text()


def test_keyword_only_separator_is_not_a_parameter(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/util.py", "def helper(a, *, b='x'):\n    return a + b\n")
    _write(
        root,
        "pkg/app.py",
        "from pkg.util import helper\n\n\ndef run():\n    return helper(1, b='y')\n",
    )
    store, updater = _index(root)
    # A keyword-only parameter has no positional index to map from.
    with pytest.raises(SignatureRefused):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.pkg.util.helper",
            ["a@0", "b@b"],
            dry_run=True,
        )
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        ["a@0", "n:int=1"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.old_params == ("a",)
    # The keyword-only section follows the new positionals behind its own
    # `*`, exactly as written; the site keeps its keyword value.
    assert "def helper(a, n: int = 1, *, b='x'):" in (root / "pkg/util.py").read_text()
    assert "helper(1, 1, b='y')" in (root / "pkg/app.py").read_text()


def test_allow_heuristic_applies_through_the_contract(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/util.py", "def helper(a):\n    return a\n")
    # Not imported: bound by name only, a heuristic site.
    _write(root, "pkg/app.py", "def run():\n    return helper(2)\n")
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        ["a@0", "n:int=1"],
        allow_heuristic=True,
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert "helper(2, 1)" in (root / "pkg/app.py").read_text()


def test_an_override_with_fewer_parameters_is_refused(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(
        root,
        "pkg/shapes.py",
        "class Base:\n    def area(self, scale, unit):\n        return scale\n\n\n"
        "class Square(Base):\n    def area(self, scale):\n        return scale * 4\n",
    )
    store, _updater = _index(root)
    with pytest.raises(SignatureRefused):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.pkg.shapes.Base.area",
            ["scale@0", "unit@1", "extra:int=0"],
            dry_run=True,
        )


def test_a_partial_respec_keeps_the_old_default(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(
        root,
        "pkg/util.py",
        "def helper(a: int, b='x') -> str:\n    return str(a) + b\n",
    )
    _write(
        root,
        "pkg/app.py",
        "from pkg.util import helper\n\n\ndef run():\n    return helper(2)\n",
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        ["a@0", "b:str@1"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    # The annotation is added; the old default survives, so `helper(2)` still runs.
    assert (
        "def helper(a: int, b: str = 'x') -> str:" in (root / "pkg/util.py").read_text()
    )
