from __future__ import annotations

import pytest

from codebase_rag.constants import (
    CPP_EXTENSIONS,
    CS_EXTENSIONS,
    GO_EXTENSIONS,
    JAVA_EXTENSIONS,
    JS_EXTENSIONS,
    LANGUAGE_METADATA,
    LUA_EXTENSIONS,
    NODE_UNIQUE_CONSTRAINTS,
    PHP_EXTENSIONS,
    PY_EXTENSIONS,
    RS_EXTENSIONS,
    SCALA_EXTENSIONS,
    TS_EXTENSIONS,
    NodeLabel,
    RelationshipType,
    SupportedLanguage,
)
from codebase_rag.language_spec import (
    LANGUAGE_SPECS,
    get_language_for_extension,
)
from codebase_rag.types_defs import NodeType


class TestSupportedLanguageCoverage:
    @pytest.mark.parametrize("lang", list(SupportedLanguage))
    def test_each_language_has_metadata(self, lang: SupportedLanguage) -> None:
        assert lang in LANGUAGE_METADATA, (
            f"SupportedLanguage.{lang.name} ({lang.value}) missing from LANGUAGE_METADATA. "
            "Every supported language must have metadata defined."
        )

    @pytest.mark.parametrize("lang", list(SupportedLanguage))
    def test_each_language_has_language_spec(self, lang: SupportedLanguage) -> None:
        assert lang in LANGUAGE_SPECS, (
            f"SupportedLanguage.{lang.name} ({lang.value}) missing from LANGUAGE_SPECS. "
            "Every supported language must have a language specification."
        )

    @pytest.mark.parametrize("lang", list(SupportedLanguage))
    def test_each_language_has_file_extensions(self, lang: SupportedLanguage) -> None:
        spec = LANGUAGE_SPECS.get(lang)
        assert spec is not None, f"No spec for {lang}"
        assert spec.file_extensions, (
            f"SupportedLanguage.{lang.name} has no file extensions defined. "
            "Every language must have at least one file extension."
        )


LANGUAGE_SPEC_PARAMS = [
    (SupportedLanguage.PYTHON, PY_EXTENSIONS),
    (SupportedLanguage.JS, JS_EXTENSIONS),
    (SupportedLanguage.TS, TS_EXTENSIONS),
    (SupportedLanguage.RUST, RS_EXTENSIONS),
    (SupportedLanguage.GO, GO_EXTENSIONS),
    (SupportedLanguage.SCALA, SCALA_EXTENSIONS),
    (SupportedLanguage.JAVA, JAVA_EXTENSIONS),
    (SupportedLanguage.CPP, CPP_EXTENSIONS),
    (SupportedLanguage.CSHARP, CS_EXTENSIONS),
    (SupportedLanguage.PHP, PHP_EXTENSIONS),
    (SupportedLanguage.LUA, LUA_EXTENSIONS),
]


class TestLanguageSpecsComplete:
    @pytest.mark.parametrize("lang,extensions", LANGUAGE_SPEC_PARAMS)
    def test_language_spec_has_correct_extensions(
        self, lang: SupportedLanguage, extensions: tuple[str, ...]
    ) -> None:
        assert lang in LANGUAGE_SPECS
        spec = LANGUAGE_SPECS[lang]
        assert spec.file_extensions == extensions


EXTENSION_MAPPING_PARAMS = [
    (".py", SupportedLanguage.PYTHON),
    (".js", SupportedLanguage.JS),
    (".jsx", SupportedLanguage.JS),
    (".ts", SupportedLanguage.TS),
    (".tsx", SupportedLanguage.TS),
    (".rs", SupportedLanguage.RUST),
    (".go", SupportedLanguage.GO),
    (".scala", SupportedLanguage.SCALA),
    (".java", SupportedLanguage.JAVA),
    (".cpp", SupportedLanguage.CPP),
    (".h", SupportedLanguage.CPP),
    (".hpp", SupportedLanguage.CPP),
    (".cc", SupportedLanguage.CPP),
    (".cs", SupportedLanguage.CSHARP),
    (".php", SupportedLanguage.PHP),
    (".lua", SupportedLanguage.LUA),
]


class TestExtensionToLanguageMapping:
    @pytest.mark.parametrize("ext,expected_lang", EXTENSION_MAPPING_PARAMS)
    def test_extension_maps_to_language(
        self, ext: str, expected_lang: SupportedLanguage
    ) -> None:
        assert get_language_for_extension(ext) == expected_lang


class TestAllExtensionsHaveLanguage:
    @pytest.mark.parametrize("ext", list(PY_EXTENSIONS))
    def test_python_extensions_all_mapped(self, ext: str) -> None:
        assert get_language_for_extension(ext) == SupportedLanguage.PYTHON

    @pytest.mark.parametrize("ext", list(JS_EXTENSIONS))
    def test_javascript_extensions_all_mapped(self, ext: str) -> None:
        assert get_language_for_extension(ext) == SupportedLanguage.JS

    @pytest.mark.parametrize("ext", list(TS_EXTENSIONS))
    def test_typescript_extensions_all_mapped(self, ext: str) -> None:
        assert get_language_for_extension(ext) == SupportedLanguage.TS

    @pytest.mark.parametrize("ext", list(RS_EXTENSIONS))
    def test_rust_extensions_all_mapped(self, ext: str) -> None:
        assert get_language_for_extension(ext) == SupportedLanguage.RUST

    @pytest.mark.parametrize("ext", list(GO_EXTENSIONS))
    def test_go_extensions_all_mapped(self, ext: str) -> None:
        assert get_language_for_extension(ext) == SupportedLanguage.GO

    @pytest.mark.parametrize("ext", list(SCALA_EXTENSIONS))
    def test_scala_extensions_all_mapped(self, ext: str) -> None:
        assert get_language_for_extension(ext) == SupportedLanguage.SCALA

    @pytest.mark.parametrize("ext", list(JAVA_EXTENSIONS))
    def test_java_extensions_all_mapped(self, ext: str) -> None:
        assert get_language_for_extension(ext) == SupportedLanguage.JAVA

    @pytest.mark.parametrize("ext", list(CPP_EXTENSIONS))
    def test_cpp_extensions_all_mapped(self, ext: str) -> None:
        assert get_language_for_extension(ext) == SupportedLanguage.CPP

    @pytest.mark.parametrize("ext", list(CS_EXTENSIONS))
    def test_csharp_extensions_all_mapped(self, ext: str) -> None:
        assert get_language_for_extension(ext) == SupportedLanguage.CSHARP

    @pytest.mark.parametrize("ext", list(PHP_EXTENSIONS))
    def test_php_extensions_all_mapped(self, ext: str) -> None:
        assert get_language_for_extension(ext) == SupportedLanguage.PHP

    @pytest.mark.parametrize("ext", list(LUA_EXTENSIONS))
    def test_lua_extensions_all_mapped(self, ext: str) -> None:
        assert get_language_for_extension(ext) == SupportedLanguage.LUA


class TestNodeTypesForLanguages:
    @pytest.mark.parametrize("node_type", list(NodeType))
    def test_all_node_types_have_constraints(self, node_type: NodeType) -> None:
        assert node_type.value in NODE_UNIQUE_CONSTRAINTS, (
            f"NodeType.{node_type.name} ({node_type.value}) missing from constraints. "
            "Nodes of this type will be silently dropped."
        )

    def test_interface_node_type_supported(self) -> None:
        assert NodeType.INTERFACE.value in NODE_UNIQUE_CONSTRAINTS

    def test_enum_node_type_supported(self) -> None:
        assert NodeType.ENUM.value in NODE_UNIQUE_CONSTRAINTS

    def test_type_alias_node_type_supported(self) -> None:
        assert NodeType.TYPE.value in NODE_UNIQUE_CONSTRAINTS

    def test_union_node_type_supported(self) -> None:
        assert NodeType.UNION.value in NODE_UNIQUE_CONSTRAINTS

    def test_class_node_type_supported(self) -> None:
        assert NodeType.CLASS.value in NODE_UNIQUE_CONSTRAINTS

    def test_function_node_type_supported(self) -> None:
        assert NodeType.FUNCTION.value in NODE_UNIQUE_CONSTRAINTS

    def test_method_node_type_supported(self) -> None:
        assert NodeType.METHOD.value in NODE_UNIQUE_CONSTRAINTS

    def test_module_node_type_supported(self) -> None:
        assert NodeType.MODULE.value in NODE_UNIQUE_CONSTRAINTS

    def test_package_node_type_supported(self) -> None:
        assert NodeType.PACKAGE.value in NODE_UNIQUE_CONSTRAINTS


class TestLanguageSpecificNodeTypes:
    def test_typescript_interface_has_constraint(self) -> None:
        assert "Interface" in NODE_UNIQUE_CONSTRAINTS

    def test_typescript_enum_has_constraint(self) -> None:
        assert "Enum" in NODE_UNIQUE_CONSTRAINTS

    def test_typescript_type_alias_has_constraint(self) -> None:
        assert "Type" in NODE_UNIQUE_CONSTRAINTS

    def test_java_interface_has_constraint(self) -> None:
        assert "Interface" in NODE_UNIQUE_CONSTRAINTS

    def test_java_enum_has_constraint(self) -> None:
        assert "Enum" in NODE_UNIQUE_CONSTRAINTS

    def test_rust_enum_has_constraint(self) -> None:
        assert "Enum" in NODE_UNIQUE_CONSTRAINTS

    def test_rust_union_has_constraint(self) -> None:
        assert "Union" in NODE_UNIQUE_CONSTRAINTS

    def test_cpp_class_has_constraint(self) -> None:
        assert "Class" in NODE_UNIQUE_CONSTRAINTS

    def test_cpp_enum_has_constraint(self) -> None:
        assert "Enum" in NODE_UNIQUE_CONSTRAINTS

    def test_cpp_union_has_constraint(self) -> None:
        assert "Union" in NODE_UNIQUE_CONSTRAINTS

    def test_cpp_module_interface_has_constraint(self) -> None:
        assert "ModuleInterface" in NODE_UNIQUE_CONSTRAINTS

    def test_cpp_module_implementation_has_constraint(self) -> None:
        assert "ModuleImplementation" in NODE_UNIQUE_CONSTRAINTS

    def test_go_interface_has_constraint(self) -> None:
        assert "Interface" in NODE_UNIQUE_CONSTRAINTS

    def test_scala_class_has_constraint(self) -> None:
        assert "Class" in NODE_UNIQUE_CONSTRAINTS

    def test_csharp_interface_has_constraint(self) -> None:
        assert "Interface" in NODE_UNIQUE_CONSTRAINTS

    def test_csharp_enum_has_constraint(self) -> None:
        assert "Enum" in NODE_UNIQUE_CONSTRAINTS

    def test_php_class_has_constraint(self) -> None:
        assert "Class" in NODE_UNIQUE_CONSTRAINTS

    def test_php_interface_has_constraint(self) -> None:
        assert "Interface" in NODE_UNIQUE_CONSTRAINTS

    def test_python_class_has_constraint(self) -> None:
        assert "Class" in NODE_UNIQUE_CONSTRAINTS

    def test_lua_function_has_constraint(self) -> None:
        assert "Function" in NODE_UNIQUE_CONSTRAINTS


class TestRelationshipTypesComplete:
    @pytest.mark.parametrize("rel_type", list(RelationshipType))
    def test_each_relationship_type_is_valid_string(
        self, rel_type: RelationshipType
    ) -> None:
        assert rel_type.value, f"RelationshipType.{rel_type.name} has empty value"
        assert rel_type.value == rel_type.value.upper(), (
            f"RelationshipType.{rel_type.name} value '{rel_type.value}' is not uppercase"
        )

    def test_defines_relationship_exists(self) -> None:
        assert RelationshipType.DEFINES.value == "DEFINES"

    def test_defines_method_relationship_exists(self) -> None:
        assert RelationshipType.DEFINES_METHOD.value == "DEFINES_METHOD"

    def test_calls_relationship_exists(self) -> None:
        assert RelationshipType.CALLS.value == "CALLS"

    def test_imports_relationship_exists(self) -> None:
        assert RelationshipType.IMPORTS.value == "IMPORTS"

    def test_inherits_relationship_exists(self) -> None:
        assert RelationshipType.INHERITS.value == "INHERITS"

    def test_implements_relationship_exists(self) -> None:
        assert RelationshipType.IMPLEMENTS.value == "IMPLEMENTS"

    def test_overrides_relationship_exists(self) -> None:
        assert RelationshipType.OVERRIDES.value == "OVERRIDES"

    def test_exports_relationship_exists(self) -> None:
        assert RelationshipType.EXPORTS.value == "EXPORTS"

    def test_contains_package_relationship_exists(self) -> None:
        assert RelationshipType.CONTAINS_PACKAGE.value == "CONTAINS_PACKAGE"

    def test_contains_folder_relationship_exists(self) -> None:
        assert RelationshipType.CONTAINS_FOLDER.value == "CONTAINS_FOLDER"

    def test_contains_file_relationship_exists(self) -> None:
        assert RelationshipType.CONTAINS_FILE.value == "CONTAINS_FILE"

    def test_contains_module_relationship_exists(self) -> None:
        assert RelationshipType.CONTAINS_MODULE.value == "CONTAINS_MODULE"

    def test_depends_on_external_relationship_exists(self) -> None:
        assert RelationshipType.DEPENDS_ON_EXTERNAL.value == "DEPENDS_ON_EXTERNAL"

    def test_exports_module_relationship_exists(self) -> None:
        assert RelationshipType.EXPORTS_MODULE.value == "EXPORTS_MODULE"

    def test_implements_module_relationship_exists(self) -> None:
        assert RelationshipType.IMPLEMENTS_MODULE.value == "IMPLEMENTS_MODULE"


class TestNodeLabelStringValues:
    @pytest.mark.parametrize("label", list(NodeLabel))
    def test_node_label_value_is_pascal_case(self, label: NodeLabel) -> None:
        value = label.value
        assert value[0].isupper(), (
            f"NodeLabel.{label.name} value '{value}' should start with uppercase"
        )
        assert "_" not in value, (
            f"NodeLabel.{label.name} value '{value}' should be PascalCase, not snake_case"
        )


class TestNodeTypeStringValues:
    @pytest.mark.parametrize("node_type", list(NodeType))
    def test_node_type_value_is_pascal_case(self, node_type: NodeType) -> None:
        value = node_type.value
        assert value[0].isupper(), (
            f"NodeType.{node_type.name} value '{value}' should start with uppercase"
        )
        assert "_" not in value, (
            f"NodeType.{node_type.name} value '{value}' should be PascalCase"
        )


class TestConstraintsKeyFormat:
    def test_all_constraint_keys_are_strings(self) -> None:
        for key in NODE_UNIQUE_CONSTRAINTS:
            assert isinstance(key, str), f"Constraint key {key} is not a string"

    def test_all_constraint_values_are_strings(self) -> None:
        for key, value in NODE_UNIQUE_CONSTRAINTS.items():
            assert isinstance(value, str), (
                f"Constraint value for {key} is not a string: {value}"
            )

    def test_all_constraint_keys_are_pascal_case(self) -> None:
        for key in NODE_UNIQUE_CONSTRAINTS:
            assert key[0].isupper(), f"Constraint key '{key}' should be PascalCase"

    def test_all_constraint_values_are_valid_property_names(self) -> None:
        valid_properties = {"name", "path", "qualified_name"}
        for key, value in NODE_UNIQUE_CONSTRAINTS.items():
            assert value in valid_properties, (
                f"Constraint for '{key}' has invalid property '{value}'. "
                f"Must be one of {valid_properties}."
            )


class TestLanguageSpecHasRequiredFields:
    @pytest.mark.parametrize("lang", list(SupportedLanguage))
    def test_each_language_spec_has_function_node_types(
        self, lang: SupportedLanguage
    ) -> None:
        if lang not in LANGUAGE_SPECS:
            pytest.skip(f"Language {lang} not in LANGUAGE_SPECS")
        spec = LANGUAGE_SPECS[lang]
        assert spec.function_node_types is not None, (
            f"Language {lang} has no function_node_types defined"
        )

    @pytest.mark.parametrize("lang", list(SupportedLanguage))
    def test_each_language_spec_has_class_node_types(
        self, lang: SupportedLanguage
    ) -> None:
        if lang not in LANGUAGE_SPECS:
            pytest.skip(f"Language {lang} not in LANGUAGE_SPECS")
        spec = LANGUAGE_SPECS[lang]
        assert spec.class_node_types is not None, (
            f"Language {lang} has no class_node_types defined"
        )

    @pytest.mark.parametrize("lang", list(SupportedLanguage))
    def test_each_language_spec_has_module_node_types(
        self, lang: SupportedLanguage
    ) -> None:
        if lang not in LANGUAGE_SPECS:
            pytest.skip(f"Language {lang} not in LANGUAGE_SPECS")
        spec = LANGUAGE_SPECS[lang]
        assert spec.module_node_types is not None, (
            f"Language {lang} has no module_node_types defined"
        )

    @pytest.mark.parametrize("lang", list(SupportedLanguage))
    def test_each_language_spec_has_call_node_types(
        self, lang: SupportedLanguage
    ) -> None:
        if lang not in LANGUAGE_SPECS:
            pytest.skip(f"Language {lang} not in LANGUAGE_SPECS")
        spec = LANGUAGE_SPECS[lang]
        assert spec.call_node_types is not None, (
            f"Language {lang} has no call_node_types defined"
        )


class TestLanguageMetadataComplete:
    @pytest.mark.parametrize("lang", list(SupportedLanguage))
    def test_each_language_has_status(self, lang: SupportedLanguage) -> None:
        assert lang in LANGUAGE_METADATA, f"Language {lang} not in LANGUAGE_METADATA"
        metadata = LANGUAGE_METADATA[lang]
        assert metadata.status is not None, f"Language {lang} has no status"
