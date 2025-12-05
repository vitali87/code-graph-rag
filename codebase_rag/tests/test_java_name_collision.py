"""
Java name collision resolution testing.
Tests that when multiple classes have the same simple name in different packages,
the type inference engine uses import distance to select the most appropriate one.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def java_collision_project(temp_repo: Path) -> Path:
    """Create a Java project with name collisions."""
    project_path = temp_repo / "java_collision_test"
    project_path.mkdir()

    # Create package structure
    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example" / "utils").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example" / "other").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example" / "app").mkdir()

    return project_path


def test_name_collision_prefers_explicit_import(
    java_collision_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """
    Test that explicit imports are respected when multiple classes share a name.
    """
    # Create Helper class in utils package
    utils_helper = (
        java_collision_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "utils"
        / "Helper.java"
    )
    utils_helper.write_text(
        """
package com.example.utils;

public class Helper {
    public String utilsMethod() {
        return "From utils package";
    }
}
"""
    )

    # Create Helper class in other package (name collision!)
    other_helper = (
        java_collision_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "other"
        / "Helper.java"
    )
    other_helper.write_text(
        """
package com.example.other;

public class Helper {
    public String otherMethod() {
        return "From other package";
    }
}
"""
    )

    # Create Service class that explicitly imports utils.Helper
    service = (
        java_collision_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "app"
        / "Service.java"
    )
    service.write_text(
        """
package com.example.app;

import com.example.utils.Helper;

public class Service {
    private Helper helper;

    public void processData() {
        helper.utilsMethod();
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_collision_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Check that CALLS relationships were created
    call_relationships = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c.args) > 1 and c.args[1] == "CALLS"
    ]

    # Count calls from Service to utils.Helper (should be 1)
    utils_calls = 0
    # Count calls from Service to other.Helper (should be 0)
    other_calls = 0

    for call in call_relationships:
        if len(call.args) > 2:
            from_tuple = call.args[0]
            to_tuple = call.args[2]
            if isinstance(from_tuple, tuple) and len(from_tuple) >= 3:
                from_qn = from_tuple[2]
                if isinstance(to_tuple, tuple) and len(to_tuple) >= 3:
                    to_qn = to_tuple[2]
                    # Check if call is from Service
                    if "Service" in from_qn and "processData" in from_qn:
                        # Check which Helper it calls
                        if "utils.Helper" in to_qn and "utilsMethod" in to_qn:
                            utils_calls += 1
                        elif "other.Helper" in to_qn:
                            other_calls += 1

    assert utils_calls == 1, (
        f"Expected 1 call to utils.Helper.utilsMethod(), found {utils_calls}. "
        "The explicit import should resolve to utils.Helper."
    )

    assert other_calls == 0, (
        f"Expected 0 calls to other.Helper, found {other_calls}. "
        "Should NOT call other.Helper when utils.Helper is explicitly imported."
    )


def test_name_collision_prefers_same_package(
    java_collision_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """
    Test that when no explicit import exists, same-package classes are preferred.
    """
    # Create Helper in same package as Service
    app_helper = (
        java_collision_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "app"
        / "Helper.java"
    )
    app_helper.write_text(
        """
package com.example.app;

public class Helper {
    public String appMethod() {
        return "From app package";
    }
}
"""
    )

    # Create Helper in distant package
    other_helper = (
        java_collision_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "other"
        / "Helper.java"
    )
    other_helper.write_text(
        """
package com.example.other;

public class Helper {
    public String otherMethod() {
        return "From other package";
    }
}
"""
    )

    # Create Service without explicit import (relies on same package)
    service = (
        java_collision_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "app"
        / "Service.java"
    )
    service.write_text(
        """
package com.example.app;

public class Service {
    private Helper helper;

    public void processData() {
        helper.appMethod();
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_collision_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Check that CALLS relationships were created
    call_relationships = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c.args) > 1 and c.args[1] == "CALLS"
    ]

    # Count calls from Service to app.Helper (should be 1)
    app_calls = 0
    # Count calls from Service to other.Helper (should be 0)
    other_calls = 0

    for call in call_relationships:
        if len(call.args) > 2:
            from_tuple = call.args[0]
            to_tuple = call.args[2]
            if isinstance(from_tuple, tuple) and len(from_tuple) >= 3:
                from_qn = from_tuple[2]
                if isinstance(to_tuple, tuple) and len(to_tuple) >= 3:
                    to_qn = to_tuple[2]
                    # Check if call is from Service
                    if "Service" in from_qn and "processData" in from_qn:
                        # Check which Helper it calls
                        if "app.Helper" in to_qn and "appMethod" in to_qn:
                            app_calls += 1
                        elif "other.Helper" in to_qn:
                            other_calls += 1

    assert app_calls == 1, (
        f"Expected 1 call to app.Helper.appMethod(), found {app_calls}. "
        "Should prefer same-package Helper over distant one (import distance)."
    )

    assert other_calls == 0, (
        f"Expected 0 calls to other.Helper, found {other_calls}. "
        "Should NOT call other.Helper when app.Helper is in the same package."
    )
