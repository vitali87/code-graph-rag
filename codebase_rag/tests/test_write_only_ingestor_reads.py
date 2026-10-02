"""Graph reads against an ingestor with nothing to read.

The protobuf exporter only writes. The helpers that read the graph during an
update treat that as a failed read and fall back to their safe answer, the
same answer a read that raised gets.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.types_defs import (
    PropertyDict,
    PropertyParams,
    PropertyValue,
    ResultRow,
)


class _WriteOnlyIngestor:
    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        pass

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        pass

    def flush_all(self) -> None:
        pass


class _ReadableIngestor(_WriteOnlyIngestor):
    def __init__(self, rows: list[ResultRow]) -> None:
        self.rows = rows

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        return self.rows

    def execute_write(self, query: str, params: PropertyParams | None = None) -> None:
        pass


def _updater(ingestor: _WriteOnlyIngestor, tmp_path: Path) -> GraphUpdater:
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=tmp_path,
        parsers={},
        queries={},
        project_name="proj",
    )


def test_registered_projects_fall_back_to_this_project(tmp_path: Path) -> None:
    updater = _updater(_WriteOnlyIngestor(), tmp_path)
    assert updater._registered_project_names() == ["proj"]
    assert updater._registry_unread


def test_known_names_mark_every_file_fully_known(tmp_path: Path) -> None:
    updater = _updater(_WriteOnlyIngestor(), tmp_path)
    assert updater._known_simple_names_by_path(["m.py"]) == {"m.py": {"*"}}


def test_file_ownership_is_never_confirmed(tmp_path: Path) -> None:
    # Negative: no read means no evidence of sole ownership, so the legacy
    # sweep must never be allowed to delete the key.
    updater = _updater(_WriteOnlyIngestor(), tmp_path)
    key = str(tmp_path / "m.py")
    assert not updater._file_keys_owned_only_by_this_project([key], str(tmp_path))


def test_a_readable_graph_is_still_read(tmp_path: Path) -> None:
    updater = _updater(_ReadableIngestor([{"name": "proj.sub"}]), tmp_path)
    assert updater._registered_project_names() == ["proj.sub", "proj"]
    assert not updater._registry_unread
