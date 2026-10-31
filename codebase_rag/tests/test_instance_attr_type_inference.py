# L3 finding from the evals/ harness: a method calls self.attr.method(), but the
# type of self.attr is only knowable from the __init__ assignment. cgr scanned
# only the calling method for self-assignments, so an ambiguous bare name
# resolved to the wrong global. Instance attributes assigned in __init__ must
# be visible to every method of the class.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

MODULE_SRC = """def run() -> str:
    return "global"


def status() -> str:
    return "globalprop"


class Helper:
    def run(self) -> str:
        return "real"

    @property
    def status(self) -> str:
        return "ok"


class App:
    def __init__(self) -> None:
        self.helper = Helper()

    def go(self) -> str:
        return self.helper.run()

    def check(self) -> str:
        return self.helper.status
"""


class _Capture:
    def __init__(self) -> None:
        self.rels: list[tuple[PropertyValue, str, PropertyValue, PropertyDict]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append((from_spec[2], str(rel_type), to_spec[2], properties or {}))

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _calls(tmp_path: Path) -> set[tuple[PropertyValue, PropertyValue]]:
    (tmp_path / "m.py").write_text(MODULE_SRC)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return {
        (frm, to) for (frm, rel, to, _) in cap.rels if rel == cs.RelationshipType.CALLS
    }


class TestInstanceAttrTypeInference:
    def test_method_call_resolves_via_init_attribute_type(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.App.go", "proj.m.Helper.run") in calls, calls

    def test_ambiguous_method_does_not_resolve_to_module_function(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.App.go", "proj.m.run") not in calls, calls

    def test_property_access_resolves_via_init_attribute_type(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.App.check", "proj.m.Helper.status") in calls, calls

    def test_property_access_not_resolved_to_module_function(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.App.check", "proj.m.status") not in calls, calls


@pytest.mark.parametrize("declaration", ["Store | None = None", "Store"])
@pytest.mark.parametrize("reset", [False, True])
def test_annotated_instance_calls_are_exact(
    tmp_path: Path, declaration: str, reset: bool
) -> None:
    (tmp_path / "models.py").write_text(
        "class Store:\n    def get(self): pass\nclass Cache:\n    def get(self): pass\n"
    )
    source = (
        "from models import Store, Cache\n"
        "class App:\n"
        f"    def __init__(self):\n        self._s: {declaration}\n"
        "    def read(self):\n        return self._s.get()\n"
    )
    if reset:
        source += "    def reset(self):\n        self._s = Store()\n"
    source += (
        "class Other:\n"
        "    def __init__(self):\n        self._s: Cache = None\n"
        "    def read(self):\n        return self._s.get()\n"
    )
    (tmp_path / "app.py").write_text(source)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    calls = {
        (frm, to, props.get("resolution"))
        for frm, rel, to, props in cap.rels
        if rel == cs.RelationshipType.CALLS
        and frm in {"proj.app.App.read", "proj.app.Other.read"}
    }
    assert calls == {
        ("proj.app.App.read", "proj.models.Store.get", "exact"),
        ("proj.app.Other.read", "proj.models.Cache.get", "exact"),
    }


@pytest.mark.parametrize(
    ("imports", "annotation"),
    [
        ("import models", "models.Store"),
        ("import models as m", "m.Store"),
        ("import models", "Optional[models.Store]"),
        ("import models", '"models.Store"'),
    ],
)
@pytest.mark.parametrize("initializer", ["", " = None", " = models_ctor()"])
def test_module_qualified_annotation_calls_are_exact(
    tmp_path: Path, imports: str, annotation: str, initializer: str
) -> None:
    (tmp_path / "models.py").write_text(
        "class Store:\n    def get(self): pass\nclass Cache:\n    def get(self): pass\n"
    )
    module_alias = imports.rpartition(" ")[2]
    initializer = initializer.replace("models_ctor", f"{module_alias}.Store")
    (tmp_path / "app.py").write_text(
        f"{imports}\nfrom typing import Optional\n"
        "class App:\n"
        f"    def __init__(self):\n        self._s: {annotation}{initializer}\n"
        "    def read(self):\n        return self._s.get()\n"
    )
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    calls = {
        (frm, to, props.get("resolution"))
        for frm, rel, to, props in cap.rels
        if rel == cs.RelationshipType.CALLS and frm == "proj.app.App.read"
    }
    assert calls == {("proj.app.App.read", "proj.models.Store.get", "exact")}
