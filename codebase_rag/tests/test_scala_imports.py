# Scala import parsing (issues #105, #1186 stage 1).
#
# Scala fell through to _parse_generic_imports, which produced ZERO import
# mappings for every Scala form -- measured on a file with a plain import, a
# selector group and a rename. #1186 lists these explicitly under stage 1:
# "import a.b.c, wildcard _/*, selector groups and renames
# (import a.{B => C}), Scala 3 given imports".
#
# The assertions are on the RESOLVED PATH each local name maps to, not on the
# count. A parser that records `Try -> Try` rather than `Try -> scala.util.Try`
# produces the right number of mappings and resolves nothing.
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import create_and_run_updater

SKIP = "scala"


@pytest.fixture
def scala_project(temp_repo: Path) -> Path:
    project = temp_repo / "scala_imports"
    project.mkdir()
    return project


def _import_mapping(project: Path, mock_ingestor: MagicMock) -> dict:
    updater = create_and_run_updater(project, mock_ingestor, SKIP)
    mapping = updater.factory.import_processor.import_mapping
    for module_qn, entries in mapping.items():
        if module_qn.endswith("a"):
            return dict(entries)
    return {}


def test_a_plain_import_maps_the_simple_name_to_its_full_path(
    scala_project: Path, mock_ingestor: MagicMock
) -> None:
    """`import scala.util.Try` makes `Try` resolvable to its full path.

    The mapped VALUE is what matters: recording `Try -> Try` would look like a
    parsed import and resolve nothing downstream.
    """
    (scala_project / "a.scala").write_text(
        """
package com.example
import scala.util.Try

object Solo { def run(): Int = 1 }
"""
    )

    mapping = _import_mapping(scala_project, mock_ingestor)

    assert mapping.get("Try") == "scala.util.Try", mapping


def test_a_selector_group_maps_every_member(
    scala_project: Path, mock_ingestor: MagicMock
) -> None:
    """`import a.b.{C, D}` maps BOTH members, not just the first.

    A selector group is one import_declaration holding several names, so a
    parser that takes the first child silently drops the rest -- the same
    shape as reading only the first base of an `extends ... with ...` clause.
    """
    (scala_project / "a.scala").write_text(
        """
package com.example
import scala.collection.mutable.{Queue, Stack}

object Solo { def run(): Int = 1 }
"""
    )

    mapping = _import_mapping(scala_project, mock_ingestor)

    assert mapping.get("Queue") == "scala.collection.mutable.Queue", mapping
    assert mapping.get("Stack") == "scala.collection.mutable.Stack", mapping


def test_a_rename_binds_the_local_alias_not_the_original(
    scala_project: Path, mock_ingestor: MagicMock
) -> None:
    """`import a.b.{Map => MMap}` binds `MMap`, and `Map` must NOT be bound.

    Asserting the absence as well as the presence: a parser that records both
    names satisfies a presence-only check while making the original resolvable
    under a name the file never introduced.
    """
    (scala_project / "a.scala").write_text(
        """
package com.example
import scala.collection.mutable.{Map => MMap}

object Solo { def run(): Int = 1 }
"""
    )

    mapping = _import_mapping(scala_project, mock_ingestor)

    assert mapping.get("MMap") == "scala.collection.mutable.Map", mapping
    assert "Map" not in mapping, mapping


def test_a_wildcard_import_is_recorded(
    scala_project: Path, mock_ingestor: MagicMock
) -> None:
    """`import java.util._` records the package as a wildcard.

    Scala 2 spells it `_` and Scala 3 spells it `*`; both mean the same thing,
    and dropping them loses every name the package brings into scope.
    """
    (scala_project / "a.scala").write_text(
        """
package com.example
import java.util._

object Solo { def run(): Int = 1 }
"""
    )

    mapping = _import_mapping(scala_project, mock_ingestor)

    assert any(
        key.startswith("*") and value == "java.util" for key, value in mapping.items()
    ), mapping
