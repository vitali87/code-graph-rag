from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_nodes, run_updater

TS_CODE = (
    "interface Greeter {\n"
    "    greet(): string;\n"
    "}\n\n"
    "enum Direction {\n"
    "    Up = 'UP',\n"
    "    Down = 'DOWN',\n"
    "}\n\n"
    "class MyGreeter implements Greeter {\n"
    "    greet(): string { return 'hi'; }\n"
    "}\n"
)

CPP_MODULE_INTERFACE = "export module mymod;\nexport int add(int a, int b);\n"

CPP_MODULE_IMPL = "module mymod;\nint add(int a, int b) { return a + b; }\n"


@pytest.fixture(scope="module")
def parsers_and_queries() -> tuple:
    return load_parsers()


@pytest.fixture
def python_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "abs_path_test"
    project_path.mkdir()

    pkg_dir = project_path / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")

    (pkg_dir / "mymodule.py").write_text(
        "class MyClass:\n"
        "    def my_method(self):\n"
        "        pass\n"
        "\n"
        "def my_function():\n"
        "    pass\n"
    )

    misc_dir = project_path / "misc"
    misc_dir.mkdir()
    (misc_dir / "notes.txt").write_text("not a package")

    (project_path / "standalone.py").write_text("def standalone_func():\n    pass\n")

    return project_path


class TestAbsolutePathOnNodes:
    def test_file_nodes_have_absolute_path(
        self,
        python_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.PYTHON not in parsers_and_queries[0]:
            pytest.skip("Python parser not available")
        run_updater(python_project, mock_ingestor)
        file_nodes = get_nodes(mock_ingestor, cs.NodeLabel.FILE)
        assert len(file_nodes) > 0
        for node_call in file_nodes:
            props = node_call[0][1]
            assert cs.KEY_ABSOLUTE_PATH in props
            abs_path = props[cs.KEY_ABSOLUTE_PATH]
            assert Path(abs_path).is_absolute()
            assert abs_path == Path(abs_path).resolve().as_posix()

    def test_module_nodes_have_absolute_path(
        self,
        python_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.PYTHON not in parsers_and_queries[0]:
            pytest.skip("Python parser not available")
        run_updater(python_project, mock_ingestor)
        module_nodes = get_nodes(mock_ingestor, cs.NodeLabel.MODULE)
        internal_modules = [c for c in module_nodes if not c[0][1].get("is_external")]
        assert len(internal_modules) > 0
        for node_call in internal_modules:
            props = node_call[0][1]
            assert cs.KEY_ABSOLUTE_PATH in props
            abs_path = props[cs.KEY_ABSOLUTE_PATH]
            assert Path(abs_path).is_absolute()

    def test_package_nodes_have_absolute_path(
        self,
        python_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.PYTHON not in parsers_and_queries[0]:
            pytest.skip("Python parser not available")
        run_updater(python_project, mock_ingestor)
        package_nodes = get_nodes(mock_ingestor, cs.NodeLabel.PACKAGE)
        assert len(package_nodes) > 0
        for node_call in package_nodes:
            props = node_call[0][1]
            assert cs.KEY_ABSOLUTE_PATH in props
            abs_path = props[cs.KEY_ABSOLUTE_PATH]
            assert Path(abs_path).is_absolute()

    def test_function_nodes_have_absolute_path(
        self,
        python_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.PYTHON not in parsers_and_queries[0]:
            pytest.skip("Python parser not available")
        run_updater(python_project, mock_ingestor)
        func_nodes = get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
        assert len(func_nodes) > 0
        for node_call in func_nodes:
            props = node_call[0][1]
            assert cs.KEY_ABSOLUTE_PATH in props
            assert cs.KEY_PATH in props
            abs_path = props[cs.KEY_ABSOLUTE_PATH]
            assert Path(abs_path).is_absolute()

    def test_class_nodes_have_absolute_path(
        self,
        python_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.PYTHON not in parsers_and_queries[0]:
            pytest.skip("Python parser not available")
        run_updater(python_project, mock_ingestor)
        class_nodes = get_nodes(mock_ingestor, cs.NodeLabel.CLASS)
        assert len(class_nodes) > 0
        for node_call in class_nodes:
            props = node_call[0][1]
            assert cs.KEY_ABSOLUTE_PATH in props
            assert cs.KEY_PATH in props
            abs_path = props[cs.KEY_ABSOLUTE_PATH]
            assert Path(abs_path).is_absolute()

    def test_method_nodes_have_absolute_path(
        self,
        python_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.PYTHON not in parsers_and_queries[0]:
            pytest.skip("Python parser not available")
        run_updater(python_project, mock_ingestor)
        method_nodes = get_nodes(mock_ingestor, cs.NodeLabel.METHOD)
        assert len(method_nodes) > 0
        for node_call in method_nodes:
            props = node_call[0][1]
            assert cs.KEY_ABSOLUTE_PATH in props
            assert cs.KEY_PATH in props
            abs_path = props[cs.KEY_ABSOLUTE_PATH]
            assert Path(abs_path).is_absolute()

    def test_folder_nodes_have_absolute_path(
        self,
        python_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.PYTHON not in parsers_and_queries[0]:
            pytest.skip("Python parser not available")
        run_updater(python_project, mock_ingestor)
        folder_nodes = get_nodes(mock_ingestor, cs.NodeLabel.FOLDER)
        assert len(folder_nodes) > 0
        for node_call in folder_nodes:
            props = node_call[0][1]
            assert cs.KEY_ABSOLUTE_PATH in props
            abs_path = props[cs.KEY_ABSOLUTE_PATH]
            assert Path(abs_path).is_absolute()

    def test_absolute_path_matches_resolved_file(
        self,
        python_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.PYTHON not in parsers_and_queries[0]:
            pytest.skip("Python parser not available")
        run_updater(python_project, mock_ingestor)
        module_nodes = get_nodes(mock_ingestor, cs.NodeLabel.MODULE)
        mymodule_nodes = [
            c for c in module_nodes if c[0][1].get(cs.KEY_NAME) == "mymodule.py"
        ]
        assert len(mymodule_nodes) == 1
        props = mymodule_nodes[0][0][1]
        expected = (python_project / "mypkg" / "mymodule.py").resolve().as_posix()
        assert props[cs.KEY_ABSOLUTE_PATH] == expected

    def test_absolute_path_is_posix_format(
        self,
        python_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.PYTHON not in parsers_and_queries[0]:
            pytest.skip("Python parser not available")
        run_updater(python_project, mock_ingestor)
        file_nodes = get_nodes(mock_ingestor, cs.NodeLabel.FILE)
        for node_call in file_nodes:
            abs_path = node_call[0][1][cs.KEY_ABSOLUTE_PATH]
            assert "\\" not in abs_path

    def test_project_node_has_no_absolute_path(
        self,
        python_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.PYTHON not in parsers_and_queries[0]:
            pytest.skip("Python parser not available")
        run_updater(python_project, mock_ingestor)
        project_nodes = get_nodes(mock_ingestor, cs.NodeLabel.PROJECT)
        assert len(project_nodes) > 0
        for node_call in project_nodes:
            props = node_call[0][1]
            assert cs.KEY_ABSOLUTE_PATH not in props


@pytest.fixture
def ts_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "ts_abs_test"
    project_path.mkdir()
    (project_path / "types.ts").write_text(TS_CODE)
    return project_path


class TestTypeScriptAbsolutePath:
    def test_interface_nodes_have_absolute_path(
        self,
        ts_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.TS not in parsers_and_queries[0]:
            pytest.skip("TypeScript parser not available")
        run_updater(ts_project, mock_ingestor)
        interface_nodes = get_nodes(mock_ingestor, cs.NodeLabel.INTERFACE)
        assert len(interface_nodes) > 0
        for node_call in interface_nodes:
            props = node_call[0][1]
            assert cs.KEY_ABSOLUTE_PATH in props
            assert Path(props[cs.KEY_ABSOLUTE_PATH]).is_absolute()

    def test_enum_nodes_have_absolute_path(
        self,
        ts_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.TS not in parsers_and_queries[0]:
            pytest.skip("TypeScript parser not available")
        run_updater(ts_project, mock_ingestor)
        enum_nodes = get_nodes(mock_ingestor, cs.NodeLabel.ENUM)
        assert len(enum_nodes) > 0
        for node_call in enum_nodes:
            props = node_call[0][1]
            assert cs.KEY_ABSOLUTE_PATH in props
            assert Path(props[cs.KEY_ABSOLUTE_PATH]).is_absolute()


@pytest.fixture
def cpp_module_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "cpp_abs_test"
    project_path.mkdir()
    (project_path / "mymod.cppm").write_text(CPP_MODULE_INTERFACE)
    (project_path / "mymod_impl.cpp").write_text(CPP_MODULE_IMPL)
    return project_path


class TestCppModuleAbsolutePath:
    def test_module_interface_nodes_have_absolute_path(
        self,
        cpp_module_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.CPP not in parsers_and_queries[0]:
            pytest.skip("C++ parser not available")
        run_updater(cpp_module_project, mock_ingestor)
        mi_nodes = get_nodes(mock_ingestor, cs.NodeLabel.MODULE_INTERFACE)
        if len(mi_nodes) == 0:
            pytest.skip("No ModuleInterface nodes produced")
        for node_call in mi_nodes:
            props = node_call[0][1]
            assert cs.KEY_ABSOLUTE_PATH in props
            assert Path(props[cs.KEY_ABSOLUTE_PATH]).is_absolute()

    def test_module_implementation_nodes_have_absolute_path(
        self,
        cpp_module_project: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple,
    ) -> None:
        if cs.SupportedLanguage.CPP not in parsers_and_queries[0]:
            pytest.skip("C++ parser not available")
        run_updater(cpp_module_project, mock_ingestor)
        mi_nodes = get_nodes(mock_ingestor, cs.NodeLabel.MODULE_IMPLEMENTATION)
        if len(mi_nodes) == 0:
            pytest.skip("No ModuleImplementation nodes produced")
        for node_call in mi_nodes:
            props = node_call[0][1]
            assert cs.KEY_ABSOLUTE_PATH in props
            assert Path(props[cs.KEY_ABSOLUTE_PATH]).is_absolute()
