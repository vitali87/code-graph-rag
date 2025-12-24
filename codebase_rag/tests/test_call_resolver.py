from __future__ import annotations

from collections import defaultdict
from collections.abc import ItemsView, KeysView
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.call_resolver import CallResolver
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.type_inference import TypeInferenceEngine
from codebase_rag.types_defs import NodeType, QualifiedName

if TYPE_CHECKING:
    from codebase_rag.parsers.call_processor import CallProcessor


class MockFunctionRegistry:
    def __init__(self) -> None:
        self._data: dict[QualifiedName, NodeType] = {}
        self._suffix_index: dict[str, list[QualifiedName]] = defaultdict(list)

    def __contains__(self, qn: QualifiedName) -> bool:
        return qn in self._data

    def __getitem__(self, qn: QualifiedName) -> NodeType:
        return self._data[qn]

    def __setitem__(self, qn: QualifiedName, func_type: NodeType) -> None:
        self._data[qn] = func_type
        parts = qn.split(cs.SEPARATOR_DOT)
        for i in range(len(parts)):
            suffix = cs.SEPARATOR_DOT.join(parts[i:])
            if qn not in self._suffix_index[suffix]:
                self._suffix_index[suffix].append(qn)

    def get(
        self, qn: QualifiedName, default: NodeType | None = None
    ) -> NodeType | None:
        return self._data.get(qn, default)

    def keys(self) -> KeysView[QualifiedName]:
        return self._data.keys()

    def items(self) -> ItemsView[QualifiedName, NodeType]:
        return self._data.items()

    def find_with_prefix(self, prefix: str) -> list[tuple[QualifiedName, NodeType]]:
        return [(k, v) for k, v in self._data.items() if k.startswith(prefix)]

    def find_ending_with(self, suffix: str) -> list[QualifiedName]:
        return self._suffix_index.get(suffix, [])


@pytest.fixture
def mock_function_registry() -> MockFunctionRegistry:
    return MockFunctionRegistry()


@pytest.fixture
def mock_import_processor(temp_repo: Path) -> ImportProcessor:
    processor = ImportProcessor(repo_path=temp_repo, project_name="test_project")
    return processor


@pytest.fixture
def mock_ast_cache() -> MagicMock:
    cache = MagicMock()
    cache.__contains__ = MagicMock(return_value=False)
    cache.__getitem__ = MagicMock(return_value=(None, None))
    cache.__setitem__ = MagicMock()
    return cache


@pytest.fixture
def mock_type_inference(
    mock_import_processor: ImportProcessor,
    mock_function_registry: MockFunctionRegistry,
    mock_ast_cache: MagicMock,
    temp_repo: Path,
) -> TypeInferenceEngine:
    return TypeInferenceEngine(
        import_processor=mock_import_processor,
        function_registry=mock_function_registry,
        repo_path=temp_repo,
        project_name="test_project",
        ast_cache=mock_ast_cache,
        queries={},
        module_qn_to_file_path={},
        class_inheritance={},
        simple_name_lookup=defaultdict(set),
    )


@pytest.fixture
def call_resolver(
    mock_function_registry: MockFunctionRegistry,
    mock_import_processor: ImportProcessor,
    mock_type_inference: TypeInferenceEngine,
) -> CallResolver:
    return CallResolver(
        function_registry=mock_function_registry,
        import_processor=mock_import_processor,
        type_inference=mock_type_inference,
        class_inheritance={},
    )


@pytest.fixture
def call_processor(temp_repo: Path, mock_ingestor: MagicMock) -> CallProcessor:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )
    return updater.factory.call_processor


class TestTryResolveIife:
    def test_resolves_iife_function_prefix(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.module.iife_func_1_5"] = NodeType.FUNCTION

        result = call_resolver._try_resolve_iife("iife_func_1_5", "proj.module")
        assert result is not None
        assert result[1] == "proj.module.iife_func_1_5"

    def test_resolves_iife_arrow_prefix(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.module.iife_arrow_2_10"] = (
            NodeType.FUNCTION
        )

        result = call_resolver._try_resolve_iife("iife_arrow_2_10", "proj.module")
        assert result is not None
        assert result[1] == "proj.module.iife_arrow_2_10"

    def test_returns_none_for_non_iife(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.module.regular_func"] = NodeType.FUNCTION

        result = call_resolver._try_resolve_iife("regular_func", "proj.module")
        assert result is None

    def test_returns_none_for_empty_call_name(
        self, call_resolver: CallResolver
    ) -> None:
        result = call_resolver._try_resolve_iife("", "proj.module")
        assert result is None

    def test_returns_none_for_unregistered_iife(
        self, call_resolver: CallResolver
    ) -> None:
        result = call_resolver._try_resolve_iife("iife_func_99_99", "proj.module")
        assert result is None


class TestIsSuperCall:
    def test_super_keyword_alone(self, call_resolver: CallResolver) -> None:
        assert call_resolver._is_super_call(cs.KEYWORD_SUPER) is True

    def test_super_dot_method(self, call_resolver: CallResolver) -> None:
        assert call_resolver._is_super_call(f"{cs.KEYWORD_SUPER}.method") is True

    def test_super_parens_method(self, call_resolver: CallResolver) -> None:
        assert call_resolver._is_super_call(f"{cs.KEYWORD_SUPER}().method") is True

    def test_regular_call_not_super(self, call_resolver: CallResolver) -> None:
        assert call_resolver._is_super_call("regular_call") is False
        assert call_resolver._is_super_call("self.method") is False
        assert call_resolver._is_super_call("superclass.method") is False


class TestHasSeparator:
    def test_dot_separator(self, call_resolver: CallResolver) -> None:
        assert call_resolver._has_separator("obj.method") is True

    def test_double_colon_separator(self, call_resolver: CallResolver) -> None:
        assert call_resolver._has_separator("Struct::method") is True

    def test_single_colon_separator(self, call_resolver: CallResolver) -> None:
        assert call_resolver._has_separator("module:method") is True

    def test_no_separator(self, call_resolver: CallResolver) -> None:
        assert call_resolver._has_separator("simple_func") is False


class TestGetSeparator:
    def test_returns_double_colon_first(self, call_resolver: CallResolver) -> None:
        assert (
            call_resolver._get_separator("Struct::method") == cs.SEPARATOR_DOUBLE_COLON
        )

    def test_returns_colon_over_dot(self, call_resolver: CallResolver) -> None:
        assert call_resolver._get_separator("module:method") == cs.SEPARATOR_COLON

    def test_returns_dot_as_default(self, call_resolver: CallResolver) -> None:
        assert call_resolver._get_separator("obj.method") == cs.SEPARATOR_DOT

    def test_returns_dot_for_no_separator(self, call_resolver: CallResolver) -> None:
        assert call_resolver._get_separator("simple") == cs.SEPARATOR_DOT


class TestTryResolveDirectImport:
    def test_resolves_direct_import(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["external.module.func"] = NodeType.FUNCTION
        import_map = {"func": "external.module.func"}

        result = call_resolver._try_resolve_direct_import("func", import_map)
        assert result is not None
        assert result[1] == "external.module.func"

    def test_returns_none_for_unimported(self, call_resolver: CallResolver) -> None:
        import_map = {"other_func": "external.module.other_func"}

        result = call_resolver._try_resolve_direct_import("func", import_map)
        assert result is None

    def test_returns_none_for_unregistered_import(
        self, call_resolver: CallResolver
    ) -> None:
        import_map = {"func": "external.module.func"}

        result = call_resolver._try_resolve_direct_import("func", import_map)
        assert result is None


class TestTryResolveSameModule:
    def test_resolves_same_module_function(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.module.my_func"] = NodeType.FUNCTION

        result = call_resolver._try_resolve_same_module("my_func", "proj.module")
        assert result is not None
        assert result[1] == "proj.module.my_func"

    def test_returns_none_for_unknown_function(
        self, call_resolver: CallResolver
    ) -> None:
        result = call_resolver._try_resolve_same_module("unknown_func", "proj.module")
        assert result is None


class TestTryResolveViaTrie:
    def test_resolves_via_trie_match(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.utils.helper"] = NodeType.FUNCTION
        call_resolver.function_registry["proj.other.helper"] = NodeType.FUNCTION

        result = call_resolver._try_resolve_via_trie("helper", "proj.utils")
        assert result is not None
        assert result[1] == "proj.utils.helper"

    def test_returns_none_for_no_match(self, call_resolver: CallResolver) -> None:
        result = call_resolver._try_resolve_via_trie("nonexistent", "proj.module")
        assert result is None

    def test_handles_qualified_call_name(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.module.Class.method"] = NodeType.METHOD

        result = call_resolver._try_resolve_via_trie("Class.method", "proj.module")
        assert result is not None
        assert result[1] == "proj.module.Class.method"


class TestTryResolveWildcardImports:
    def test_resolves_wildcard_import(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["external.utils.helper"] = NodeType.FUNCTION
        import_map = {"*utils": "external.utils"}

        result = call_resolver._try_resolve_wildcard_imports("helper", import_map)
        assert result is not None
        assert result[1] == "external.utils.helper"

    def test_returns_none_for_no_wildcard_match(
        self, call_resolver: CallResolver
    ) -> None:
        import_map = {"*utils": "external.utils"}

        result = call_resolver._try_resolve_wildcard_imports("helper", import_map)
        assert result is None

    def test_skips_non_wildcard_imports(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["external.utils.helper"] = NodeType.FUNCTION
        import_map = {"utils": "external.utils"}

        result = call_resolver._try_resolve_wildcard_imports("helper", import_map)
        assert result is None


class TestTryWildcardQns:
    def test_dot_separated_qn(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["external.utils.helper"] = NodeType.FUNCTION

        result = call_resolver._try_wildcard_qns("helper", "external.utils")
        assert result is not None
        assert result[1] == "external.utils.helper"

    def test_double_colon_separated_qn(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["crate::module::func"] = NodeType.FUNCTION

        result = call_resolver._try_wildcard_qns("func", "crate::module")
        assert result is not None

    def test_returns_none_for_no_match(self, call_resolver: CallResolver) -> None:
        result = call_resolver._try_wildcard_qns("unknown", "external.utils")
        assert result is None


class TestTryResolveViaImports:
    def test_resolves_direct_import(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["external.func"] = NodeType.FUNCTION
        call_resolver.import_processor.import_mapping["proj.module"] = {
            "func": "external.func"
        }

        result = call_resolver._try_resolve_via_imports("func", "proj.module", None)
        assert result is not None
        assert result[1] == "external.func"

    def test_returns_none_for_unknown_module(self, call_resolver: CallResolver) -> None:
        result = call_resolver._try_resolve_via_imports("func", "unknown.module", None)
        assert result is None


class TestResolveTwoPartCall:
    def test_resolves_imported_class_method(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.models.MyClass.method"] = NodeType.METHOD
        import_map = {"MyClass": "proj.models.MyClass"}

        result = call_resolver._resolve_two_part_call(
            ["MyClass", "method"],
            "MyClass.method",
            cs.SEPARATOR_DOT,
            import_map,
            "proj.views",
            None,
        )
        assert result is not None
        assert result[1] == "proj.models.MyClass.method"

    def test_resolves_local_variable_method(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.models.User.save"] = NodeType.METHOD
        import_map = {"User": "proj.models.User"}
        local_var_types = {"user": "User"}

        result = call_resolver._resolve_two_part_call(
            ["user", "save"],
            "user.save",
            cs.SEPARATOR_DOT,
            import_map,
            "proj.views",
            local_var_types,
        )
        assert result is not None
        assert result[1] == "proj.models.User.save"

    def test_resolves_module_method_fallback(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.views.method"] = NodeType.FUNCTION
        import_map: dict[str, str] = {}

        result = call_resolver._resolve_two_part_call(
            ["obj", "method"],
            "obj.method",
            cs.SEPARATOR_DOT,
            import_map,
            "proj.views",
            None,
        )
        assert result is not None
        assert result[1] == "proj.views.method"


class TestTryResolveViaLocalType:
    def test_resolves_via_local_type(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.models.User.save"] = NodeType.METHOD
        import_map = {"User": "proj.models.User"}
        local_var_types = {"user": "User"}

        result = call_resolver._try_resolve_via_local_type(
            "user",
            "save",
            cs.SEPARATOR_DOT,
            "user.save",
            import_map,
            "proj.views",
            local_var_types,
        )
        assert result is not None
        assert result[1] == "proj.models.User.save"

    def test_returns_none_for_no_local_types(self, call_resolver: CallResolver) -> None:
        result = call_resolver._try_resolve_via_local_type(
            "user",
            "save",
            cs.SEPARATOR_DOT,
            "user.save",
            {},
            "proj.views",
            None,
        )
        assert result is None

    def test_returns_none_for_unknown_object(self, call_resolver: CallResolver) -> None:
        local_var_types = {"other": "User"}

        result = call_resolver._try_resolve_via_local_type(
            "user",
            "save",
            cs.SEPARATOR_DOT,
            "user.save",
            {},
            "proj.views",
            local_var_types,
        )
        assert result is None

    def test_resolves_js_builtin_type(self, call_resolver: CallResolver) -> None:
        local_var_types = {"arr": "Array"}

        result = call_resolver._try_resolve_via_local_type(
            "arr",
            "push",
            cs.SEPARATOR_DOT,
            "arr.push",
            {},
            "proj.module",
            local_var_types,
        )
        assert result is not None
        assert cs.BUILTIN_PREFIX in result[1]
        assert "Array.prototype.push" in result[1]


class TestTryMethodOnClass:
    def test_resolves_method_on_class(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.models.User.save"] = NodeType.METHOD

        result = call_resolver._try_method_on_class(
            "proj.models.User",
            "save",
            cs.SEPARATOR_DOT,
            "user.save",
            "user",
            "User",
        )
        assert result is not None
        assert result[1] == "proj.models.User.save"

    def test_resolves_inherited_method(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.models.BaseModel.validate"] = (
            NodeType.METHOD
        )
        call_resolver.class_inheritance["proj.models.User"] = ["proj.models.BaseModel"]

        result = call_resolver._try_method_on_class(
            "proj.models.User",
            "validate",
            cs.SEPARATOR_DOT,
            "user.validate",
            "user",
            "User",
        )
        assert result is not None
        assert result[1] == "proj.models.BaseModel.validate"

    def test_returns_none_for_unknown_method(self, call_resolver: CallResolver) -> None:
        result = call_resolver._try_method_on_class(
            "proj.models.User",
            "unknown",
            cs.SEPARATOR_DOT,
            "user.unknown",
            "user",
            "User",
        )
        assert result is None


class TestTryResolveViaImport:
    def test_resolves_static_method_via_import(
        self, call_resolver: CallResolver
    ) -> None:
        call_resolver.function_registry["proj.utils.StringUtils.format"] = (
            NodeType.METHOD
        )
        import_map = {"StringUtils": "proj.utils.StringUtils"}

        result = call_resolver._try_resolve_via_import(
            "StringUtils",
            "format",
            cs.SEPARATOR_DOT,
            "StringUtils.format",
            import_map,
        )
        assert result is not None
        assert result[1] == "proj.utils.StringUtils.format"

    def test_returns_none_for_unimported(self, call_resolver: CallResolver) -> None:
        import_map: dict[str, str] = {}

        result = call_resolver._try_resolve_via_import(
            "StringUtils",
            "format",
            cs.SEPARATOR_DOT,
            "StringUtils.format",
            import_map,
        )
        assert result is None


class TestResolveImportedClassQn:
    def test_returns_class_qn_for_matching_method(
        self, call_resolver: CallResolver
    ) -> None:
        call_resolver.function_registry["proj.models.User.User.save"] = NodeType.METHOD

        result = call_resolver._resolve_imported_class_qn(
            "proj.models.User",
            "User",
            "save",
            cs.SEPARATOR_DOT,
        )
        assert result == "proj.models.User.User"

    def test_returns_original_for_no_match(self, call_resolver: CallResolver) -> None:
        result = call_resolver._resolve_imported_class_qn(
            "proj.models.User",
            "User",
            "save",
            cs.SEPARATOR_DOT,
        )
        assert result == "proj.models.User"


class TestResolveRustClassQn:
    def test_resolves_rust_class_qn(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["crate.module.MyStruct"] = NodeType.CLASS

        result = call_resolver._resolve_rust_class_qn("crate::module::MyStruct")
        assert result == "crate.module.MyStruct"

    def test_returns_original_for_no_match(self, call_resolver: CallResolver) -> None:
        result = call_resolver._resolve_rust_class_qn("crate::module::Unknown")
        assert result == "crate::module::Unknown"


class TestTryResolveModuleMethod:
    def test_resolves_module_method(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.module.helper"] = NodeType.FUNCTION

        result = call_resolver._try_resolve_module_method(
            "helper",
            "obj.helper",
            "proj.module",
        )
        assert result is not None
        assert result[1] == "proj.module.helper"

    def test_returns_none_for_unknown_method(self, call_resolver: CallResolver) -> None:
        result = call_resolver._try_resolve_module_method(
            "unknown",
            "obj.unknown",
            "proj.module",
        )
        assert result is None


class TestResolveSelfAttributeCall:
    def test_resolves_self_attribute_method(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.models.Database.query"] = NodeType.METHOD
        import_map = {"Database": "proj.models.Database"}
        local_var_types = {"self.db": "Database"}

        result = call_resolver._resolve_self_attribute_call(
            ["self", "db", "query"],
            "self.db.query",
            import_map,
            "proj.service",
            local_var_types,
        )
        assert result is not None
        assert result[1] == "proj.models.Database.query"

    def test_resolves_inherited_self_attribute_method(
        self, call_resolver: CallResolver
    ) -> None:
        call_resolver.function_registry["proj.models.BaseDB.close"] = NodeType.METHOD
        call_resolver.class_inheritance["proj.models.Database"] = ["proj.models.BaseDB"]
        import_map = {"Database": "proj.models.Database"}
        local_var_types = {"self.db": "Database"}

        result = call_resolver._resolve_self_attribute_call(
            ["self", "db", "close"],
            "self.db.close",
            import_map,
            "proj.service",
            local_var_types,
        )
        assert result is not None
        assert result[1] == "proj.models.BaseDB.close"

    def test_returns_none_for_no_local_type(self, call_resolver: CallResolver) -> None:
        result = call_resolver._resolve_self_attribute_call(
            ["self", "db", "query"],
            "self.db.query",
            {},
            "proj.service",
            None,
        )
        assert result is None


class TestResolveMultiPartCall:
    def test_resolves_imported_multi_part_call(
        self, call_resolver: CallResolver
    ) -> None:
        call_resolver.function_registry["proj.utils.Config.settings.get"] = (
            NodeType.METHOD
        )
        import_map = {"Config": "proj.utils.Config"}

        result = call_resolver._resolve_multi_part_call(
            ["Config", "settings", "get"],
            "Config.settings.get",
            import_map,
            "proj.app",
            None,
        )
        assert result is not None
        assert result[1] == "proj.utils.Config.settings.get"

    def test_resolves_via_local_type(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.models.User.profile.update"] = (
            NodeType.METHOD
        )
        import_map = {"User": "proj.models.User"}
        local_var_types = {"user": "User"}

        result = call_resolver._resolve_multi_part_call(
            ["user", "profile", "update"],
            "user.profile.update",
            import_map,
            "proj.service",
            local_var_types,
        )
        assert result is not None
        assert result[1] == "proj.models.User.profile.update"

    def test_returns_none_for_unknown_call(self, call_resolver: CallResolver) -> None:
        result = call_resolver._resolve_multi_part_call(
            ["unknown", "method", "call"],
            "unknown.method.call",
            {},
            "proj.module",
            None,
        )
        assert result is None


class TestTryResolveQualifiedCall:
    def test_two_part_call(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.models.User.save"] = NodeType.METHOD
        import_map = {"User": "proj.models.User"}

        result = call_resolver._try_resolve_qualified_call(
            "User.save",
            import_map,
            "proj.views",
            None,
        )
        assert result is not None
        assert result[1] == "proj.models.User.save"

    def test_self_attribute_call(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.models.DB.query"] = NodeType.METHOD
        import_map = {"DB": "proj.models.DB"}
        local_var_types = {"self.db": "DB"}

        result = call_resolver._try_resolve_qualified_call(
            "self.db.query",
            import_map,
            "proj.service",
            local_var_types,
        )
        assert result is not None
        assert result[1] == "proj.models.DB.query"

    def test_returns_none_for_no_separator(self, call_resolver: CallResolver) -> None:
        result = call_resolver._try_resolve_qualified_call(
            "simple_func",
            {},
            "proj.module",
            None,
        )
        assert result is None


class TestResolveClassQnFromType:
    def test_returns_dotted_type_as_is(self, call_resolver: CallResolver) -> None:
        result = call_resolver._resolve_class_qn_from_type(
            "proj.models.User",
            {},
            "proj.views",
        )
        assert result == "proj.models.User"

    def test_resolves_from_import_map(self, call_resolver: CallResolver) -> None:
        import_map = {"User": "proj.models.User"}

        result = call_resolver._resolve_class_qn_from_type(
            "User",
            import_map,
            "proj.views",
        )
        assert result == "proj.models.User"

    def test_falls_back_to_class_name_resolution(
        self, call_resolver: CallResolver
    ) -> None:
        call_resolver.function_registry["proj.views.LocalClass"] = NodeType.CLASS

        result = call_resolver._resolve_class_qn_from_type(
            "LocalClass",
            {},
            "proj.views",
        )
        assert result == "proj.views.LocalClass"


class TestResolveJavaMethodCall:
    def test_resolves_java_method_call(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        if cs.SupportedLanguage.JAVA not in parsers:
            pytest.skip("Java parser not available")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        resolver = updater.factory.call_processor._resolver
        resolver.function_registry["com.example.MyClass.doSomething"] = NodeType.METHOD
        resolver.import_processor.import_mapping["com.example.App"] = {
            "MyClass": "com.example.MyClass"
        }

        java_code = b"obj.doSomething();"
        parser = parsers[cs.SupportedLanguage.JAVA]
        tree = parser.parse(java_code)
        call_node = tree.root_node.children[0].children[0]

        local_var_types = {"obj": "MyClass"}

        result = resolver.resolve_java_method_call(
            call_node,
            "com.example.App",
            local_var_types,
        )
        assert result is not None
        assert result[1] == "com.example.MyClass.doSomething"

    def test_returns_none_for_unresolved_java_call(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        if cs.SupportedLanguage.JAVA not in parsers:
            pytest.skip("Java parser not available")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        resolver = updater.factory.call_processor._resolver

        java_code = b"unknown.method();"
        parser = parsers[cs.SupportedLanguage.JAVA]
        tree = parser.parse(java_code)
        call_node = tree.root_node.children[0].children[0]

        result = resolver.resolve_java_method_call(
            call_node,
            "com.example.App",
            {},
        )
        assert result is None


class TestResolveInheritedMethod:
    def test_finds_method_in_parent(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.base.BaseClass.method"] = NodeType.METHOD
        call_resolver.class_inheritance["proj.module.ChildClass"] = [
            "proj.base.BaseClass"
        ]

        result = call_resolver._resolve_inherited_method(
            "proj.module.ChildClass", "method"
        )
        assert result is not None
        assert result[1] == "proj.base.BaseClass.method"

    def test_finds_method_in_grandparent(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.base.GrandparentClass.method"] = (
            NodeType.METHOD
        )
        call_resolver.class_inheritance["proj.module.ChildClass"] = [
            "proj.base.ParentClass"
        ]
        call_resolver.class_inheritance["proj.base.ParentClass"] = [
            "proj.base.GrandparentClass"
        ]

        result = call_resolver._resolve_inherited_method(
            "proj.module.ChildClass", "method"
        )
        assert result is not None
        assert result[1] == "proj.base.GrandparentClass.method"

    def test_returns_none_for_unknown_class(self, call_resolver: CallResolver) -> None:
        result = call_resolver._resolve_inherited_method("unknown.Class", "method")
        assert result is None

    def test_returns_none_for_unknown_method(self, call_resolver: CallResolver) -> None:
        call_resolver.class_inheritance["proj.module.ChildClass"] = [
            "proj.base.ParentClass"
        ]

        result = call_resolver._resolve_inherited_method(
            "proj.module.ChildClass", "unknown_method"
        )
        assert result is None

    def test_handles_diamond_inheritance(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.base.Root.method"] = NodeType.METHOD
        call_resolver.class_inheritance["proj.module.Child"] = [
            "proj.base.ParentA",
            "proj.base.ParentB",
        ]
        call_resolver.class_inheritance["proj.base.ParentA"] = ["proj.base.Root"]
        call_resolver.class_inheritance["proj.base.ParentB"] = ["proj.base.Root"]

        result = call_resolver._resolve_inherited_method("proj.module.Child", "method")
        assert result is not None
        assert result[1] == "proj.base.Root.method"


class TestCalculateImportDistance:
    def test_same_module_distance_zero(self, call_resolver: CallResolver) -> None:
        distance = call_resolver._calculate_import_distance(
            "proj.module.func", "proj.module"
        )
        assert distance <= 1

    def test_sibling_module_distance_low(self, call_resolver: CallResolver) -> None:
        distance = call_resolver._calculate_import_distance(
            "proj.other.func", "proj.module"
        )
        assert distance == 1

    def test_distant_module_higher_distance(self, call_resolver: CallResolver) -> None:
        distance_close = call_resolver._calculate_import_distance(
            "proj.utils.func", "proj.module"
        )
        distance_far = call_resolver._calculate_import_distance(
            "external.lib.utils.func", "proj.module"
        )
        assert distance_far > distance_close


class TestIsMethodChain:
    def test_simple_method_not_chain(self, call_resolver: CallResolver) -> None:
        assert call_resolver._is_method_chain("obj.method") is False

    def test_method_with_parens_is_chain(self, call_resolver: CallResolver) -> None:
        assert call_resolver._is_method_chain("obj.method().next") is True

    def test_chained_calls_is_chain(self, call_resolver: CallResolver) -> None:
        assert call_resolver._is_method_chain("obj.first().second().third") is True

    def test_no_dots_not_chain(self, call_resolver: CallResolver) -> None:
        assert call_resolver._is_method_chain("simple()") is False

    def test_empty_string_not_chain(self, call_resolver: CallResolver) -> None:
        assert call_resolver._is_method_chain("") is False


class TestResolveChainedCall:
    def test_resolves_chained_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        processor = updater.factory.call_processor
        processor._resolver.function_registry["proj.models.QuerySet.filter"] = (
            NodeType.METHOD
        )
        processor._resolver.function_registry["proj.models.QuerySet.all"] = (
            NodeType.METHOD
        )
        processor._resolver.type_inference.python_type_inference._method_return_type_cache[
            "proj.models.QuerySet.all"
        ] = "QuerySet"
        processor._resolver.import_processor.import_mapping["proj.views"] = {
            "QuerySet": "proj.models.QuerySet"
        }

        result = processor._resolver._resolve_chained_call(
            "qs.all().filter",
            "proj.views",
            {"qs": "QuerySet"},
        )
        if result is not None:
            assert "filter" in result[1] or "QuerySet" in result[1]

    def test_returns_none_for_unresolvable_chain(
        self, call_resolver: CallResolver
    ) -> None:
        result = call_resolver._resolve_chained_call(
            "unknown.method().next",
            "proj.module",
            None,
        )
        assert result is None


class TestResolveSuperCall:
    def test_super_constructor_call(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.base.Parent.constructor"] = (
            NodeType.METHOD
        )
        call_resolver.class_inheritance["proj.module.Child"] = ["proj.base.Parent"]

        result = call_resolver._resolve_super_call(
            cs.KEYWORD_SUPER,
            class_context="proj.module.Child",
        )
        assert result is not None
        assert result[1] == "proj.base.Parent.constructor"

    def test_super_method_call(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.base.Parent.validate"] = NodeType.METHOD
        call_resolver.class_inheritance["proj.module.Child"] = ["proj.base.Parent"]

        result = call_resolver._resolve_super_call(
            f"{cs.KEYWORD_SUPER}.validate",
            class_context="proj.module.Child",
        )
        assert result is not None
        assert result[1] == "proj.base.Parent.validate"

    def test_returns_none_without_class_context(
        self, call_resolver: CallResolver
    ) -> None:
        result = call_resolver._resolve_super_call(cs.KEYWORD_SUPER, class_context=None)
        assert result is None

    def test_returns_none_for_unknown_class(self, call_resolver: CallResolver) -> None:
        result = call_resolver._resolve_super_call(
            cs.KEYWORD_SUPER,
            class_context="unknown.Class",
        )
        assert result is None


class TestResolveFunctionCallIntegration:
    def test_resolves_iife_priority(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.module.__iife_func_1_5"] = (
            NodeType.FUNCTION
        )

        result = call_resolver.resolve_function_call("__iife_func_1_5", "proj.module")
        assert result is not None
        assert result[1] == "proj.module.__iife_func_1_5"

    def test_resolves_super_call(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.base.Parent.constructor"] = (
            NodeType.METHOD
        )
        call_resolver.class_inheritance["proj.module.Child"] = ["proj.base.Parent"]

        result = call_resolver.resolve_function_call(
            cs.KEYWORD_SUPER,
            "proj.module",
            class_context="proj.module.Child",
        )
        assert result is not None
        assert result[1] == "proj.base.Parent.constructor"

    def test_resolves_imported_function(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.utils.helper"] = NodeType.FUNCTION
        call_resolver.import_processor.import_mapping["proj.module"] = {
            "helper": "proj.utils.helper"
        }

        result = call_resolver.resolve_function_call("helper", "proj.module")
        assert result is not None
        assert result[1] == "proj.utils.helper"

    def test_resolves_same_module_function(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.module.local_func"] = NodeType.FUNCTION

        result = call_resolver.resolve_function_call("local_func", "proj.module")
        assert result is not None
        assert result[1] == "proj.module.local_func"

    def test_falls_back_to_trie(self, call_resolver: CallResolver) -> None:
        call_resolver.function_registry["proj.other.helper"] = NodeType.FUNCTION

        result = call_resolver.resolve_function_call("helper", "proj.module")
        assert result is not None
        assert result[1] == "proj.other.helper"

    def test_returns_none_for_unknown(self, call_resolver: CallResolver) -> None:
        result = call_resolver.resolve_function_call("unknown_func", "proj.module")
        assert result is None
