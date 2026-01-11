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
    SupportedLanguage,
    UniqueKeyType,
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
    @pytest.mark.parametrize("lang,extensions", LANGUAGE_SPEC_PARAMS)
    def test_all_extensions_map_to_correct_language(
        self, lang: SupportedLanguage, extensions: tuple[str, ...]
    ) -> None:
        for ext in extensions:
            assert get_language_for_extension(ext) == lang


class TestNodeTypesForLanguages:
    @pytest.mark.parametrize("node_type", list(NodeType))
    def test_all_node_types_have_constraints(self, node_type: NodeType) -> None:
        assert node_type.value in NODE_UNIQUE_CONSTRAINTS, (
            f"NodeType.{node_type.name} ({node_type.value}) missing from constraints. "
            "Nodes of this type will be silently dropped."
        )


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
        valid_properties = {v.value for v in UniqueKeyType}
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
