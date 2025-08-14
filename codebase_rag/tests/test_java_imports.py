"""
Java import parsing and resolution testing.
Tests various Java import patterns including static imports, wildcard imports,
and package declarations.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def java_imports_project(temp_repo: Path) -> Path:
    """Create a Java project with complex import patterns."""
    project_path = temp_repo / "java_imports_test"
    project_path.mkdir()

    # Create package structure
    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example" / "utils").mkdir()

    return project_path


def test_basic_java_imports(
    java_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic Java import parsing."""
    test_file = (
        java_imports_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "BasicImports.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.List;
import java.util.ArrayList;
import java.util.Map;
import java.util.HashMap;
import java.io.IOException;
import java.io.FileNotFoundException;

public class BasicImports {
    private List<String> names;
    private Map<String, Integer> scores;

    public BasicImports() {
        this.names = new ArrayList<>();
        this.scores = new HashMap<>();
    }

    public void readFile() throws IOException, FileNotFoundException {
        // Method using imported exceptions
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_imports_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Check import mapping was created
    project_name = java_imports_project.name
    module_qn = f"{project_name}.src.main.java.com.example.BasicImports"

    assert module_qn in updater.factory.import_processor.import_mapping, (
        f"No import mapping for {module_qn}"
    )

    imports = updater.factory.import_processor.import_mapping[module_qn]

    # Check that basic imports were parsed
    expected_imports = {
        "List": "java.util.List",
        "ArrayList": "java.util.ArrayList",
        "Map": "java.util.Map",
        "HashMap": "java.util.HashMap",
        "IOException": "java.io.IOException",
        "FileNotFoundException": "java.io.FileNotFoundException",
    }

    for name, path in expected_imports.items():
        assert name in imports, f"Missing import: {name}"
        assert imports[name] == path, (
            f"Wrong path for {name}: expected {path}, got {imports[name]}"
        )


def test_static_imports(
    java_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java static import parsing."""
    test_file = (
        java_imports_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "StaticImports.java"
    )
    test_file.write_text(
        """
package com.example;

import static java.lang.Math.PI;
import static java.lang.Math.sqrt;
import static java.lang.Math.pow;
import static java.lang.System.out;
import static java.util.Collections.sort;
import static java.util.Arrays.asList;

public class StaticImports {

    public void calculateCircleArea(double radius) {
        double area = PI * pow(radius, 2);
        out.println("Area: " + area);
    }

    public void demonstrateStaticMethods() {
        double distance = sqrt(pow(3, 2) + pow(4, 2));
        out.println("Distance: " + distance);

        java.util.List<String> names = asList("Alice", "Bob", "Charlie");
        sort(names);
        out.println("Sorted: " + names);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_imports_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Check static import mapping
    project_name = java_imports_project.name
    module_qn = f"{project_name}.src.main.java.com.example.StaticImports"

    assert module_qn in updater.factory.import_processor.import_mapping, (
        f"No import mapping for {module_qn}"
    )

    imports = updater.factory.import_processor.import_mapping[module_qn]

    # Check that static imports were parsed
    expected_static_imports = {
        "PI": "java.lang.Math.PI",
        "sqrt": "java.lang.Math.sqrt",
        "pow": "java.lang.Math.pow",
        "out": "java.lang.System.out",
        "sort": "java.util.Collections.sort",
        "asList": "java.util.Arrays.asList",
    }

    for name, path in expected_static_imports.items():
        assert name in imports, f"Missing static import: {name}"
        assert imports[name] == path, (
            f"Wrong path for static import {name}: expected {path}, got {imports[name]}"
        )


def test_wildcard_imports(
    java_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java wildcard import parsing."""
    test_file = (
        java_imports_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "WildcardImports.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.io.*;
import javax.swing.*;
import static java.lang.Math.*;
import static java.util.Collections.*;

public class WildcardImports {

    public void useCollections() {
        List<String> list = new ArrayList<>();
        Set<Integer> set = new HashSet<>();
        Map<String, Object> map = new HashMap<>();

        // Using static wildcard imports
        double result = sqrt(abs(-25));
        sort(list);
        reverse(list);
    }

    public void useIO() throws IOException {
        FileReader reader = new FileReader("test.txt");
        BufferedReader buffered = new BufferedReader(reader);
        FileWriter writer = new FileWriter("output.txt");
    }

    public void useSwing() {
        JFrame frame = new JFrame("Test");
        JButton button = new JButton("Click");
        JLabel label = new JLabel("Hello");
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_imports_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Check wildcard import mapping
    project_name = java_imports_project.name
    module_qn = f"{project_name}.src.main.java.com.example.WildcardImports"

    assert module_qn in updater.factory.import_processor.import_mapping, (
        f"No import mapping for {module_qn}"
    )

    imports = updater.factory.import_processor.import_mapping[module_qn]

    # Check that wildcard imports were parsed
    expected_wildcard_imports = {
        "*java.util": "java.util",
        "*java.io": "java.io",
        "*javax.swing": "javax.swing",
        "*java.lang.Math": "java.lang.Math",
        "*java.util.Collections": "java.util.Collections",
    }

    for name, path in expected_wildcard_imports.items():
        assert name in imports, f"Missing wildcard import: {name}"
        assert imports[name] == path, (
            f"Wrong path for wildcard import {name}: expected {path}, got {imports[name]}"
        )


def test_package_local_imports(
    java_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test imports from the same package and local packages."""

    # Create utility class in same package
    util_file = (
        java_imports_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "StringUtils.java"
    )
    util_file.write_text(
        """
package com.example;

public class StringUtils {
    public static String capitalize(String str) {
        return str.substring(0, 1).toUpperCase() + str.substring(1);
    }

    public static boolean isEmpty(String str) {
        return str == null || str.trim().isEmpty();
    }
}
"""
    )

    # Create utility class in sub-package
    sub_util_file = (
        java_imports_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "utils"
        / "MathUtils.java"
    )
    sub_util_file.write_text(
        """
package com.example.utils;

public class MathUtils {
    public static int add(int a, int b) {
        return a + b;
    }

    public static double average(double... numbers) {
        if (numbers.length == 0) return 0;
        double sum = 0;
        for (double num : numbers) {
            sum += num;
        }
        return sum / numbers.length;
    }
}
"""
    )

    # Main test file using local imports
    test_file = (
        java_imports_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "LocalImports.java"
    )
    test_file.write_text(
        """
package com.example;

import com.example.utils.MathUtils;

public class LocalImports {

    public void useLocalClasses() {
        // StringUtils is in same package, no import needed
        String name = StringUtils.capitalize("hello");
        boolean empty = StringUtils.isEmpty(name);

        // MathUtils requires explicit import
        int sum = MathUtils.add(5, 3);
        double avg = MathUtils.average(1.5, 2.5, 3.5);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_imports_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Check import mapping for local imports
    project_name = java_imports_project.name
    module_qn = f"{project_name}.src.main.java.com.example.LocalImports"

    assert module_qn in updater.factory.import_processor.import_mapping, (
        f"No import mapping for {module_qn}"
    )

    imports = updater.factory.import_processor.import_mapping[module_qn]

    # Check that local package import was parsed
    expected_imports = {
        "MathUtils": "com.example.utils.MathUtils",
    }

    for name, path in expected_imports.items():
        assert name in imports, f"Missing local import: {name}"
        assert imports[name] == path, (
            f"Wrong path for local import {name}: expected {path}, got {imports[name]}"
        )


def test_qualified_names_without_imports(
    java_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test usage of fully qualified class names without imports."""
    test_file = (
        java_imports_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "QualifiedNames.java"
    )
    test_file.write_text(
        """
package com.example;

public class QualifiedNames {

    public void useQualifiedNames() {
        // Using fully qualified names without imports
        java.util.List<String> list = new java.util.ArrayList<>();
        java.util.Map<String, Integer> map = new java.util.HashMap<>();

        java.io.File file = new java.io.File("test.txt");
        java.nio.file.Path path = java.nio.file.Paths.get("test.txt");

        javax.swing.JFrame frame = new javax.swing.JFrame("Test");

        // Using primitive wrapper classes
        java.lang.Integer num = java.lang.Integer.valueOf(42);
        java.lang.String str = java.lang.String.valueOf(num);
    }

    public java.util.Date getCurrentDate() {
        return new java.util.Date();
    }

    public void handleException() {
        try {
            throw new java.lang.RuntimeException("Test");
        } catch (java.lang.RuntimeException e) {
            java.lang.System.out.println(e.getMessage());
        }
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_imports_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # For this test, we mainly verify that parsing doesn't fail
    # and that the class and methods are detected even without imports
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    project_name = java_imports_project.name
    expected_class = (
        f"{project_name}.src.main.java.com.example.QualifiedNames.QualifiedNames"
    )
    assert expected_class in created_classes, (
        f"QualifiedNames class not found in: {created_classes}"
    )
