from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import (
    get_node_names,
    get_nodes,
    get_relationships,
    run_updater,
)


@pytest.fixture
def c_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "c_test_project"
    project_path.mkdir()

    (project_path / "Makefile").write_text("all:\n\tgcc -o main main.c\n")

    (project_path / "main.c").write_text(
        '#include "utils.h"\n'
        "#include <stdio.h>\n"
        "\n"
        "void greet(void) {\n"
        '    printf("Hello\\n");\n'
        "}\n"
        "\n"
        "int add(int a, int b) {\n"
        "    return a + b;\n"
        "}\n"
        "\n"
        "int* get_ptr(void) {\n"
        "    static int x = 42;\n"
        "    return &x;\n"
        "}\n"
        "\n"
        "int main(void) {\n"
        "    greet();\n"
        "    int result = add(1, 2);\n"
        "    int* p = get_ptr();\n"
        "    return 0;\n"
        "}\n"
    )

    (project_path / "utils.h").write_text(
        "#ifndef UTILS_H\n"
        "#define UTILS_H\n"
        "\n"
        "int add(int a, int b);\n"
        "void greet(void);\n"
        "\n"
        "#endif\n"
    )

    (project_path / "types.c").write_text(
        "struct Point {\n"
        "    int x;\n"
        "    int y;\n"
        "};\n"
        "\n"
        "union Value {\n"
        "    int i;\n"
        "    float f;\n"
        "};\n"
        "\n"
        "enum Color {\n"
        "    RED,\n"
        "    GREEN,\n"
        "    BLUE\n"
        "};\n"
    )

    return project_path


@pytest.fixture
def c_subdir_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "c_subdir_project"
    project_path.mkdir()

    (project_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\nproject(myapp)\n"
    )

    src_dir = project_path / "src"
    src_dir.mkdir()
    (src_dir / "Makefile").write_text("all:\n\tgcc -o app app.c\n")

    (src_dir / "app.c").write_text(
        "void run(void) {}\n\nint main(void) {\n    run();\n    return 0;\n}\n"
    )

    return project_path


class TestCFunctionNodes:
    def test_simple_function_detected(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        func_names = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
        assert any("add" in name for name in func_names)

    def test_void_function_detected(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        func_names = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
        assert any("greet" in name for name in func_names)

    def test_pointer_return_function_detected(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        func_names = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
        assert any("get_ptr" in name for name in func_names)

    def test_main_function_detected(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        func_names = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
        assert any("main" in name for name in func_names)

    def test_function_with_parameters(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        func_nodes = get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
        add_nodes = [
            n for n in func_nodes if "add" in n[0][1].get(cs.KEY_QUALIFIED_NAME, "")
        ]
        assert len(add_nodes) > 0


class TestCStructNodes:
    def test_struct_detected(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        class_names = get_node_names(mock_ingestor, cs.NodeLabel.CLASS)
        assert any("Point" in name for name in class_names)

    def test_struct_has_qualified_name(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        class_nodes = get_nodes(mock_ingestor, cs.NodeLabel.CLASS)
        point_nodes = [
            n for n in class_nodes if "Point" in n[0][1].get(cs.KEY_QUALIFIED_NAME, "")
        ]
        assert len(point_nodes) > 0
        qn = point_nodes[0][0][1][cs.KEY_QUALIFIED_NAME]
        assert "." in qn


class TestCUnionNodes:
    def test_union_detected(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        union_names = get_node_names(mock_ingestor, cs.NodeLabel.UNION)
        class_names = get_node_names(mock_ingestor, cs.NodeLabel.CLASS)
        all_names = union_names | class_names
        assert any("Value" in name for name in all_names)


class TestCEnumNodes:
    def test_enum_detected(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        enum_names = get_node_names(mock_ingestor, cs.NodeLabel.ENUM)
        class_names = get_node_names(mock_ingestor, cs.NodeLabel.CLASS)
        all_names = enum_names | class_names
        assert any("Color" in name for name in all_names)


class TestCCallsRelationships:
    def test_function_call_detected(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        calls = get_relationships(mock_ingestor, str(cs.RelationshipType.CALLS))
        assert len(calls) > 0

    def test_main_calls_greet(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        calls = get_relationships(mock_ingestor, str(cs.RelationshipType.CALLS))
        call_pairs = []
        for c in calls:
            src = c.args[0] if c.args else c[0][0]
            tgt = c.args[2] if len(c.args) > 2 else c[0][2]
            if isinstance(src, tuple) and isinstance(tgt, tuple):
                call_pairs.append((src, tgt))
        found_greet = any(
            "main" in str(src) and "greet" in str(tgt) for src, tgt in call_pairs
        )
        assert found_greet

    def test_multiple_calls_from_main(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        calls = get_relationships(mock_ingestor, str(cs.RelationshipType.CALLS))
        main_calls = [
            c for c in calls if "main" in str(c.args[0] if c.args else c[0][0])
        ]
        assert len(main_calls) >= 2


class TestCDefinesRelationships:
    def test_module_defines_functions(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        defines = get_relationships(mock_ingestor, str(cs.RelationshipType.DEFINES))
        assert len(defines) > 0

    def test_main_module_defines_add(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        defines = get_relationships(mock_ingestor, str(cs.RelationshipType.DEFINES))
        found = any("add" in str(d) for d in defines)
        assert found


class TestCImportsRelationships:
    def test_include_creates_external_module(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        module_nodes = get_nodes(mock_ingestor, cs.NodeLabel.MODULE)
        external_modules = [n for n in module_nodes if n[0][1].get(cs.KEY_IS_EXTERNAL)]
        has_stdio = any("stdio" in str(n) for n in external_modules)
        has_utils = any(
            "utils" in n[0][1].get(cs.KEY_QUALIFIED_NAME, "") for n in module_nodes
        )
        assert has_stdio or has_utils

    def test_include_utils_h_module_exists(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        module_nodes = get_nodes(mock_ingestor, cs.NodeLabel.MODULE)
        module_qnames = {n[0][1].get(cs.KEY_QUALIFIED_NAME, "") for n in module_nodes}
        assert any("utils" in qn for qn in module_qnames)


class TestCFileAndModuleNodes:
    def test_c_file_nodes_created(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        file_nodes = get_nodes(mock_ingestor, cs.NodeLabel.FILE)
        file_paths = {n[0][1].get(cs.KEY_PATH, "") for n in file_nodes}
        assert any("main.c" in p for p in file_paths)
        assert any("types.c" in p for p in file_paths)

    def test_c_module_nodes_created(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        module_nodes = get_nodes(mock_ingestor, cs.NodeLabel.MODULE)
        module_names = {n[0][1].get(cs.KEY_QUALIFIED_NAME, "") for n in module_nodes}
        assert any("main" in name for name in module_names)

    def test_header_file_node_created(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        file_nodes = get_nodes(mock_ingestor, cs.NodeLabel.FILE)
        file_paths = {n[0][1].get(cs.KEY_PATH, "") for n in file_nodes}
        assert any("utils.h" in p for p in file_paths)


class TestCQualifiedNames:
    def test_function_qualified_name_has_project(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        func_names = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
        for name in func_names:
            assert "." in name, f"Qualified name should contain '.': {name}"

    def test_function_qualified_name_format(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        func_names = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
        add_names = [n for n in func_names if "add" in n]
        assert len(add_names) > 0
        parts = add_names[0].split(".")
        assert len(parts) >= 2


class TestCPackageDetection:
    def test_makefile_creates_package(
        self,
        c_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_project, mock_ingestor, skip_if_missing="c")
        package_nodes = get_nodes(mock_ingestor, cs.NodeLabel.PACKAGE)
        assert len(package_nodes) > 0

    def test_cmakelists_creates_package(
        self,
        c_subdir_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_subdir_project, mock_ingestor, skip_if_missing="c")
        package_nodes = get_nodes(mock_ingestor, cs.NodeLabel.PACKAGE)
        assert len(package_nodes) > 0

    def test_subdirectory_with_makefile_is_package(
        self,
        c_subdir_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        run_updater(c_subdir_project, mock_ingestor, skip_if_missing="c")
        package_nodes = get_nodes(mock_ingestor, cs.NodeLabel.PACKAGE)
        package_qnames = {n[0][1].get(cs.KEY_QUALIFIED_NAME, "") for n in package_nodes}
        assert any("src" in qn for qn in package_qnames)
