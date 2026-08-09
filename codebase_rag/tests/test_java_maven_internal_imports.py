"""Intra-project imports under src/main/java must resolve to Module nodes,
not dead-end ExternalModule nodes (issue #1121)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships, run_updater

SERVICE = """
package com.example.myapp;

import com.example.myapp.util.MyHelper;

public class MyService {
    public int serve() {
        return MyHelper.help();
    }
}
"""

HELPER = """
package com.example.myapp.util;

public class MyHelper {
    public static int help() {
        return 42;
    }
}
"""


def _write_maven_project(temp_repo: Path) -> None:
    base = temp_repo / "src" / "main" / "java" / "com" / "example" / "myapp"
    (base / "util").mkdir(parents=True)
    (base / "MyService.java").write_text(SERVICE, encoding="utf-8")
    (base / "util" / "MyHelper.java").write_text(HELPER, encoding="utf-8")


def _skip_without_java() -> None:
    parsers, _ = load_parsers()
    if "java" not in parsers:
        pytest.skip("java parser not available")


def _import_edges(mock_ingestor: MagicMock, from_qn: str) -> list[tuple[str, str]]:
    return [
        (str(call.args[2][0]), call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.IMPORTS.value)
        if call.args[0][2] == from_qn
    ]


def test_maven_internal_import_targets_the_module_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _skip_without_java()
    _write_maven_project(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    service_qn = f"{project}.src.main.java.com.example.myapp.MyService"
    helper_qn = f"{project}.src.main.java.com.example.myapp.util.MyHelper"
    edges = _import_edges(mock_ingestor, service_qn)
    assert (str(cs.NodeLabel.MODULE), helper_qn) in edges, edges


def test_maven_internal_import_creates_no_external_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _skip_without_java()
    _write_maven_project(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    external_qns = {
        c.args[1].get("qualified_name")
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == str(cs.NodeLabel.EXTERNAL_MODULE)
    }
    internal = {qn for qn in external_qns if qn and "com.example.myapp" in qn}
    assert not internal, internal


def test_external_import_sharing_local_top_level_stays_external(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`src/main/java/com` existing must not make com.fasterxml.* local."""
    _skip_without_java()
    _write_maven_project(temp_repo)
    consumer = temp_repo / "src" / "main" / "java" / "com" / "example" / "myapp"
    (consumer / "JsonUser.java").write_text(
        """
package com.example.myapp;

import com.fasterxml.jackson.databind.ObjectMapper;

public class JsonUser {
    private ObjectMapper mapper = new ObjectMapper();
}
""",
        encoding="utf-8",
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    consumer_qn = f"{project}.src.main.java.com.example.myapp.JsonUser"
    edges = _import_edges(mock_ingestor, consumer_qn)
    targets = {qn for _, qn in edges}
    assert not any("fasterxml" in qn and qn.startswith(project) for qn in targets), (
        targets
    )
    assert (
        str(cs.NodeLabel.EXTERNAL_MODULE),
        "com.fasterxml.jackson.databind",
    ) in edges, edges


def test_static_member_import_targets_the_owning_class_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """import static ...MyHelper.help must land on the MyHelper Module."""
    _skip_without_java()
    _write_maven_project(temp_repo)
    consumer = temp_repo / "src" / "main" / "java" / "com" / "example" / "myapp"
    (consumer / "StaticUser.java").write_text(
        """
package com.example.myapp;

import static com.example.myapp.util.MyHelper.help;

public class StaticUser {
    public int use() {
        return help();
    }
}
""",
        encoding="utf-8",
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    user_qn = f"{project}.src.main.java.com.example.myapp.StaticUser"
    helper_qn = f"{project}.src.main.java.com.example.myapp.util.MyHelper"
    edges = _import_edges(mock_ingestor, user_qn)
    assert (str(cs.NodeLabel.MODULE), helper_qn) in edges, edges
    assert not [e for e in edges if e[0] == str(cs.NodeLabel.EXTERNAL_MODULE)], edges


def test_nested_class_import_targets_the_outer_class_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """import ...Outer.Inner must land on the Outer Module, not go external."""
    _skip_without_java()
    _write_maven_project(temp_repo)
    base = temp_repo / "src" / "main" / "java" / "com" / "example" / "myapp"
    (base / "Outer.java").write_text(
        """
package com.example.myapp;

public class Outer {
    public static class Inner {
        public static final int CONSTANT = 1;
    }
}
""",
        encoding="utf-8",
    )
    (base / "NestedUser.java").write_text(
        """
package com.example.myapp;

import com.example.myapp.Outer.Inner;

public class NestedUser {
    public int use() {
        return Inner.CONSTANT;
    }
}
""",
        encoding="utf-8",
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    user_qn = f"{project}.src.main.java.com.example.myapp.NestedUser"
    outer_qn = f"{project}.src.main.java.com.example.myapp.Outer"
    edges = _import_edges(mock_ingestor, user_qn)
    assert (str(cs.NodeLabel.MODULE), outer_qn) in edges, edges
    assert not [e for e in edges if e[0] == str(cs.NodeLabel.EXTERNAL_MODULE)], edges


def test_test_root_class_resolves_to_the_test_source_root(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """With com/ under both roots, a test-only class binds to src/test/java."""
    _skip_without_java()
    _write_maven_project(temp_repo)
    test_base = temp_repo / "src" / "test" / "java" / "com" / "example" / "myapp"
    (test_base / "support").mkdir(parents=True)
    (test_base / "support" / "Fixtures.java").write_text(
        """
package com.example.myapp.support;

public class Fixtures {
    public static int seed() {
        return 7;
    }
}
""",
        encoding="utf-8",
    )
    (test_base / "MyServiceTest.java").write_text(
        """
package com.example.myapp;

import com.example.myapp.support.Fixtures;

public class MyServiceTest {
    public int prepare() {
        return Fixtures.seed();
    }
}
""",
        encoding="utf-8",
    )
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    tester_qn = f"{project}.src.test.java.com.example.myapp.MyServiceTest"
    fixtures_qn = f"{project}.src.test.java.com.example.myapp.support.Fixtures"
    edges = _import_edges(mock_ingestor, tester_qn)
    assert (str(cs.NodeLabel.MODULE), fixtures_qn) in edges, edges
