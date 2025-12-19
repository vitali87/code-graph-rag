from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.types_defs import NodeType


class TestWildcardImportResolution:
    """Test wildcard import resolution for all supported languages."""

    @pytest.fixture
    def mock_updater(self) -> GraphUpdater:
        """Create a GraphUpdater instance with mock dependencies for testing."""
        mock_ingestor = MagicMock()
        updater = GraphUpdater(
            ingestor=mock_ingestor, repo_path=Path("/fake/repo"), parsers={}, queries={}
        )
        return updater

    def test_java_wildcard_import_resolution(self, mock_updater: GraphUpdater) -> None:
        """Test that Java wildcard imports (import java.util.*;) work correctly."""
        module_qn = "com.example.service"

        mock_updater.factory.import_processor.import_mapping[module_qn] = {}
        mock_updater.factory.import_processor.import_mapping[module_qn][
            "*java.util"
        ] = "java.util"

        mock_updater.function_registry["java.util.List"] = NodeType.CLASS
        mock_updater.function_registry["java.util.ArrayList"] = NodeType.CLASS
        mock_updater.function_registry["java.util.HashMap"] = NodeType.CLASS

        result = mock_updater.factory.call_processor._resolve_function_call(
            "List", module_qn
        )
        assert result is not None
        func_type, resolved_qn = result
        assert resolved_qn == "java.util.List"
        assert func_type == NodeType.CLASS

        result = mock_updater.factory.call_processor._resolve_function_call(
            "ArrayList", module_qn
        )
        assert result is not None
        assert result[1] == "java.util.ArrayList"

        result = mock_updater.factory.call_processor._resolve_function_call(
            "NonExistentClass", module_qn
        )
        assert result is None

    def test_rust_wildcard_import_resolution(self, mock_updater: GraphUpdater) -> None:
        """Test that Rust wildcard imports (use std::collections::*;) work correctly."""
        module_qn = "my_project::service"

        mock_updater.factory.import_processor.import_mapping[module_qn] = {}
        mock_updater.factory.import_processor.import_mapping[module_qn][
            "*std::collections"
        ] = "std::collections"

        mock_updater.function_registry["std::collections::HashMap"] = NodeType.FUNCTION
        mock_updater.function_registry["std::collections::BTreeMap"] = NodeType.FUNCTION
        mock_updater.function_registry["std::collections::VecDeque"] = NodeType.FUNCTION

        result = mock_updater.factory.call_processor._resolve_function_call(
            "HashMap", module_qn
        )
        assert result is not None
        func_type, resolved_qn = result
        assert resolved_qn == "std::collections::HashMap"
        assert func_type == NodeType.FUNCTION

        result = mock_updater.factory.call_processor._resolve_function_call(
            "BTreeMap", module_qn
        )
        assert result is not None
        assert result[1] == "std::collections::BTreeMap"

    def test_javascript_namespace_import_resolution(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that JavaScript namespace imports (import * as utils from './utils') work correctly."""
        module_qn = "src.service"

        mock_updater.factory.import_processor.import_mapping[module_qn] = {}
        mock_updater.factory.import_processor.import_mapping[module_qn]["utils"] = (
            "src.utils"
        )

        mock_updater.function_registry["src.utils"] = NodeType.MODULE

        result = mock_updater.factory.call_processor._resolve_function_call(
            "utils", module_qn
        )
        assert result is not None
        func_type, resolved_qn = result
        assert resolved_qn == "src.utils"

    def test_python_wildcard_import_resolution(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test Python wildcard imports (from module import *) when properly stored."""
        module_qn = "myproject.service"

        mock_updater.factory.import_processor.import_mapping[module_qn] = {}
        mock_updater.factory.import_processor.import_mapping[module_qn][
            "*myproject.utils"
        ] = "myproject.utils"

        mock_updater.function_registry["myproject.utils.helper_function"] = (
            NodeType.FUNCTION
        )
        mock_updater.function_registry["myproject.utils.UtilityClass"] = NodeType.CLASS

        result = mock_updater.factory.call_processor._resolve_function_call(
            "helper_function", module_qn
        )
        assert result is not None
        func_type, resolved_qn = result
        assert resolved_qn == "myproject.utils.helper_function"

        result = mock_updater.factory.call_processor._resolve_function_call(
            "UtilityClass", module_qn
        )
        assert result is not None
        assert result[1] == "myproject.utils.UtilityClass"

    def test_cpp_using_namespace_resolution(self, mock_updater: GraphUpdater) -> None:
        """Test C++ using namespace directives when properly stored."""
        module_qn = "myproject.service"

        mock_updater.factory.import_processor.import_mapping[module_qn] = {}
        mock_updater.factory.import_processor.import_mapping[module_qn]["*std"] = "std"
        mock_updater.factory.import_processor.import_mapping[module_qn][
            "*boost::algorithm"
        ] = "boost::algorithm"

        mock_updater.function_registry["std::vector"] = NodeType.CLASS
        mock_updater.function_registry["std::string"] = NodeType.CLASS
        mock_updater.function_registry["boost::algorithm::trim"] = NodeType.FUNCTION

        result = mock_updater.factory.call_processor._resolve_function_call(
            "vector", module_qn
        )
        assert result is not None
        func_type, resolved_qn = result
        assert resolved_qn == "std::vector"

        result = mock_updater.factory.call_processor._resolve_function_call(
            "string", module_qn
        )
        assert result is not None
        assert result[1] == "std::string"

        result = mock_updater.factory.call_processor._resolve_function_call(
            "trim", module_qn
        )
        assert result is not None
        assert result[1] == "boost::algorithm::trim"

    def test_scala_wildcard_import_resolution(self, mock_updater: GraphUpdater) -> None:
        """Test Scala wildcard imports (import scala.collection._) when properly stored."""
        module_qn = "com.example.service"

        mock_updater.factory.import_processor.import_mapping[module_qn] = {}
        mock_updater.factory.import_processor.import_mapping[module_qn][
            "*scala.collection"
        ] = "scala.collection"
        mock_updater.factory.import_processor.import_mapping[module_qn][
            "*scala.util"
        ] = "scala.util"

        mock_updater.function_registry["scala.collection.List"] = NodeType.CLASS
        mock_updater.function_registry["scala.collection.Map"] = NodeType.CLASS
        mock_updater.function_registry["scala.util.Try"] = NodeType.CLASS

        result = mock_updater.factory.call_processor._resolve_function_call(
            "List", module_qn
        )
        assert result is not None
        func_type, resolved_qn = result
        assert resolved_qn == "scala.collection.List"

        result = mock_updater.factory.call_processor._resolve_function_call(
            "Map", module_qn
        )
        assert result is not None
        assert result[1] == "scala.collection.Map"

        result = mock_updater.factory.call_processor._resolve_function_call(
            "Try", module_qn
        )
        assert result is not None
        assert result[1] == "scala.util.Try"

    def test_go_no_wildcard_imports(self, mock_updater: GraphUpdater) -> None:
        """Test that Go doesn't have wildcard imports (imports all public symbols by default)."""
        module_qn = "myproject/service"

        mock_updater.factory.import_processor.import_mapping[module_qn] = {}
        mock_updater.factory.import_processor.import_mapping[module_qn]["fmt"] = "fmt"
        mock_updater.factory.import_processor.import_mapping[module_qn]["strings"] = (
            "strings"
        )

        mock_updater.function_registry["fmt"] = NodeType.PACKAGE
        mock_updater.function_registry["strings"] = NodeType.PACKAGE

        result = mock_updater.factory.call_processor._resolve_function_call(
            "fmt", module_qn
        )
        assert result is not None
        func_type, resolved_qn = result
        assert resolved_qn == "fmt"

    def test_exact_import_priority_over_wildcard(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that exact imports take priority over wildcard imports."""
        module_qn = "com.example.service"

        mock_updater.factory.import_processor.import_mapping[module_qn] = {}
        mock_updater.factory.import_processor.import_mapping[module_qn]["List"] = (
            "my.custom.List"
        )
        mock_updater.factory.import_processor.import_mapping[module_qn][
            "*java.util"
        ] = "java.util"

        mock_updater.function_registry["my.custom.List"] = NodeType.CLASS
        mock_updater.function_registry["java.util.List"] = NodeType.CLASS

        result = mock_updater.factory.call_processor._resolve_function_call(
            "List", module_qn
        )
        assert result is not None
        func_type, resolved_qn = result
        assert resolved_qn == "my.custom.List"

    def test_multiple_wildcard_imports(self, mock_updater: GraphUpdater) -> None:
        """Test handling multiple wildcard imports in the same module."""
        module_qn = "com.example.service"

        mock_updater.factory.import_processor.import_mapping[module_qn] = {}
        mock_updater.factory.import_processor.import_mapping[module_qn][
            "*java.util"
        ] = "java.util"
        mock_updater.factory.import_processor.import_mapping[module_qn]["*java.io"] = (
            "java.io"
        )
        mock_updater.factory.import_processor.import_mapping[module_qn][
            "*std::collections"
        ] = "std::collections"

        mock_updater.function_registry["java.util.List"] = NodeType.CLASS
        mock_updater.function_registry["java.io.File"] = NodeType.CLASS
        mock_updater.function_registry["std::collections::HashMap"] = NodeType.FUNCTION

        result = mock_updater.factory.call_processor._resolve_function_call(
            "List", module_qn
        )
        assert result is not None
        assert result[1] == "java.util.List"

        result = mock_updater.factory.call_processor._resolve_function_call(
            "File", module_qn
        )
        assert result is not None
        assert result[1] == "java.io.File"

        result = mock_updater.factory.call_processor._resolve_function_call(
            "HashMap", module_qn
        )
        assert result is not None
        assert result[1] == "std::collections::HashMap"

    def test_wildcard_with_no_matching_function(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that wildcard imports don't match non-existent functions."""
        module_qn = "com.example.service"

        mock_updater.factory.import_processor.import_mapping[module_qn] = {}
        mock_updater.factory.import_processor.import_mapping[module_qn][
            "*java.util"
        ] = "java.util"

        mock_updater.function_registry["java.util.List"] = NodeType.CLASS

        result = mock_updater.factory.call_processor._resolve_function_call(
            "NonExistentClass", module_qn
        )
        assert result is None

    def test_fallback_still_works_after_wildcard_check(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that Phase 2/3 fallback still works when wildcard doesn't match."""
        module_qn = "com.example.service"

        mock_updater.factory.import_processor.import_mapping[module_qn] = {}
        mock_updater.factory.import_processor.import_mapping[module_qn][
            "*java.util"
        ] = "java.util"

        mock_updater.function_registry["com.example.service.LocalService"] = (
            NodeType.CLASS
        )
        mock_updater.simple_name_lookup["LocalService"].add(
            "com.example.service.LocalService"
        )

        result = mock_updater.factory.call_processor._resolve_function_call(
            "LocalService", module_qn
        )
        assert result is not None
        func_type, resolved_qn = result
        assert resolved_qn == "com.example.service.LocalService"
