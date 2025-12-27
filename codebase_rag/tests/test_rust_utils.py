from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.rs.utils import (
    build_module_path,
    extract_impl_target,
    extract_use_imports,
)


def get_rust_parser():
    parsers, _ = load_parsers()
    if "rust" not in parsers:
        pytest.skip("Rust parser not available")
    return parsers["rust"]


def parse_rust_code(code: str):
    parser = get_rust_parser()
    tree = parser.parse(code.encode())
    return tree.root_node


def find_node_by_type(root, node_type: str):
    if root.type == node_type:
        return root
    for child in root.children:
        result = find_node_by_type(child, node_type)
        if result:
            return result
    return None


def find_all_nodes_by_type(root, node_type: str) -> list:
    results = []
    if root.type == node_type:
        results.append(root)
    for child in root.children:
        results.extend(find_all_nodes_by_type(child, node_type))
    return results


class TestExtractImplTarget:
    def test_simple_impl(self) -> None:
        code = """
impl Foo {
    fn bar() {}
}
"""
        root = parse_rust_code(code)
        impl_node = find_node_by_type(root, "impl_item")
        assert impl_node is not None

        result = extract_impl_target(impl_node)
        assert result == "Foo"

    def test_impl_with_generic(self) -> None:
        code = """
impl<T> Container<T> {
    fn new() -> Self {}
}
"""
        root = parse_rust_code(code)
        impl_node = find_node_by_type(root, "impl_item")
        assert impl_node is not None

        result = extract_impl_target(impl_node)
        assert result == "Container"

    def test_impl_trait_for_type(self) -> None:
        code = """
impl Display for MyType {
    fn fmt(&self, f: &mut Formatter) -> Result {}
}
"""
        root = parse_rust_code(code)
        impl_node = find_node_by_type(root, "impl_item")
        assert impl_node is not None

        result = extract_impl_target(impl_node)
        assert result == "MyType"

    def test_impl_scoped_type(self) -> None:
        code = """
impl module::SubModule::MyType {
    fn method(&self) {}
}
"""
        root = parse_rust_code(code)
        impl_node = find_node_by_type(root, "impl_item")
        assert impl_node is not None

        result = extract_impl_target(impl_node)
        assert result == "MyType"

    def test_non_impl_node_returns_none(self) -> None:
        code = """
fn foo() {}
"""
        root = parse_rust_code(code)
        fn_node = find_node_by_type(root, "function_item")
        assert fn_node is not None

        result = extract_impl_target(fn_node)
        assert result is None


class TestExtractUseImports:
    def test_simple_import(self) -> None:
        code = "use std::collections::HashMap;"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "HashMap" in result
        assert result["HashMap"] == "std::collections::HashMap"

    def test_grouped_imports(self) -> None:
        code = "use std::{fs, io};"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "fs" in result
        assert "io" in result
        assert result["fs"] == "std::fs"
        assert result["io"] == "std::io"

    def test_aliased_import(self) -> None:
        code = "use std::collections::HashMap as Map;"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "Map" in result
        assert result["Map"] == "std::collections::HashMap"

    def test_wildcard_import(self) -> None:
        code = "use crate::utils::*;"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "*crate::utils" in result
        assert result["*crate::utils"] == "crate::utils"

    def test_self_import(self) -> None:
        code = "use self::local_module;"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "local_module" in result
        assert result["local_module"] == "self::local_module"

    def test_super_import(self) -> None:
        code = "use super::parent_module;"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "parent_module" in result
        assert result["parent_module"] == "super::parent_module"

    def test_crate_import(self) -> None:
        code = "use crate::module::item;"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "item" in result
        assert result["item"] == "crate::module::item"

    def test_nested_grouped_imports(self) -> None:
        code = "use std::{io::{Read, Write}, fs::File};"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "Read" in result
        assert "Write" in result
        assert "File" in result
        assert result["Read"] == "std::io::Read"
        assert result["Write"] == "std::io::Write"
        assert result["File"] == "fs::File"

    def test_self_alias_in_group(self) -> None:
        code = "use std::io::{self as Sio, Read};"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "Sio" in result
        assert "Read" in result
        assert result["Sio"] == "std::io"
        assert result["Read"] == "std::io::Read"

    def test_non_use_node_returns_empty(self) -> None:
        code = "fn foo() {}"
        root = parse_rust_code(code)
        fn_node = find_node_by_type(root, "function_item")
        assert fn_node is not None

        result = extract_use_imports(fn_node)
        assert result == {}

    def test_multiple_nested_groups(self) -> None:
        code = "use crate::{module1, module2::{submod1, submod2}};"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "module1" in result
        assert "submod1" in result
        assert "submod2" in result
        assert result["module1"] == "crate::module1"
        assert result["submod1"] == "crate::module2::submod1"
        assert result["submod2"] == "crate::module2::submod2"


class TestBuildModulePath:
    def test_function_in_module(self) -> None:
        code = """
mod outer {
    fn inner_func() {}
}
"""
        root = parse_rust_code(code)
        fn_node = find_node_by_type(root, "function_item")
        assert fn_node is not None

        result = build_module_path(fn_node)
        assert result == ["outer"]

    def test_function_in_nested_modules(self) -> None:
        code = """
mod outer {
    mod inner {
        fn deep_func() {}
    }
}
"""
        root = parse_rust_code(code)
        fn_node = find_node_by_type(root, "function_item")
        assert fn_node is not None

        result = build_module_path(fn_node)
        assert result == ["outer", "inner"]

    def test_method_in_impl_with_target(self) -> None:
        code = """
mod mymod {
    impl MyStruct {
        fn method(&self) {}
    }
}
"""
        root = parse_rust_code(code)
        fn_node = find_node_by_type(root, "function_item")
        assert fn_node is not None

        result = build_module_path(fn_node, include_impl_targets=True)
        assert result == ["mymod", "MyStruct"]

    def test_method_in_impl_without_target(self) -> None:
        code = """
mod mymod {
    impl MyStruct {
        fn method(&self) {}
    }
}
"""
        root = parse_rust_code(code)
        fn_node = find_node_by_type(root, "function_item")
        assert fn_node is not None

        result = build_module_path(fn_node, include_impl_targets=False)
        assert result == ["mymod"]

    def test_top_level_function(self) -> None:
        code = "fn top_level() {}"
        root = parse_rust_code(code)
        fn_node = find_node_by_type(root, "function_item")
        assert fn_node is not None

        result = build_module_path(fn_node)
        assert result == []

    def test_function_with_class_node_types(self) -> None:
        code = """
mod mymod {
    struct MyStruct {
        field: i32,
    }
}
"""
        root = parse_rust_code(code)
        struct_node = find_node_by_type(root, "struct_item")
        assert struct_node is not None

        result = build_module_path(
            struct_node, include_classes=True, class_node_types=["struct_item"]
        )
        assert result == ["mymod"]


class TestRustImportsIntegration:
    def test_imports_create_relationships(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        from codebase_rag.graph_updater import GraphUpdater

        test_file = temp_repo / "lib.rs"
        test_file.write_text("""
use std::collections::HashMap;
use std::io::{Read, Write};

fn main() {
    let map: HashMap<String, i32> = HashMap::new();
}
""")
        parsers, queries = load_parsers()
        if "rust" not in parsers:
            pytest.skip("Rust parser not available")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        project_name = temp_repo.name
        module_name = f"{project_name}.lib"

        assert module_name in updater.factory.import_processor.import_mapping
        imports = updater.factory.import_processor.import_mapping[module_name]

        assert "HashMap" in imports
        assert "Read" in imports
        assert "Write" in imports

    def test_complex_nested_imports_integration(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        from codebase_rag.graph_updater import GraphUpdater

        test_file = temp_repo / "complex.rs"
        test_file.write_text("""
use std::{
    collections::{HashMap, HashSet, BTreeMap},
    io::{self, Read, Write, BufReader},
    fs::{File, OpenOptions},
};

use crate::module::{SubModule, AnotherModule as AM};

fn process() {
    let file = File::open("test.txt").unwrap();
    let reader = BufReader::new(file);
}
""")
        parsers, queries = load_parsers()
        if "rust" not in parsers:
            pytest.skip("Rust parser not available")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        project_name = temp_repo.name
        module_name = f"{project_name}.complex"

        assert module_name in updater.factory.import_processor.import_mapping
        imports = updater.factory.import_processor.import_mapping[module_name]

        expected_imports = [
            "HashMap",
            "HashSet",
            "BTreeMap",
            "Read",
            "Write",
            "BufReader",
            "File",
            "OpenOptions",
            "SubModule",
            "AM",
        ]
        for imp in expected_imports:
            assert imp in imports, f"Missing import: {imp}"

    def test_impl_methods_have_correct_qualified_names(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        from codebase_rag.graph_updater import GraphUpdater

        test_file = temp_repo / "structs.rs"
        test_file.write_text("""
pub struct User {
    name: String,
}

impl User {
    pub fn new(name: String) -> Self {
        User { name }
    }

    pub fn get_name(&self) -> &str {
        &self.name
    }
}
""")
        parsers, queries = load_parsers()
        if "rust" not in parsers:
            pytest.skip("Rust parser not available")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        method_calls = [
            call
            for call in mock_ingestor.ensure_node_batch.call_args_list
            if call[0][0] == "Method"
        ]

        method_names = {call[0][1].get("qualified_name", "") for call in method_calls}

        project_name = temp_repo.name
        expected_methods = [
            f"{project_name}.structs.User.new",
            f"{project_name}.structs.User.get_name",
        ]

        for expected in expected_methods:
            assert expected in method_names, f"Missing method: {expected}"

    def test_wildcard_imports_tracked(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        from codebase_rag.graph_updater import GraphUpdater

        test_file = temp_repo / "wildcards.rs"
        test_file.write_text("""
use std::prelude::v1::*;
use crate::utils::*;
use super::parent::*;

fn use_wildcards() {}
""")
        parsers, queries = load_parsers()
        if "rust" not in parsers:
            pytest.skip("Rust parser not available")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        project_name = temp_repo.name
        module_name = f"{project_name}.wildcards"

        assert module_name in updater.factory.import_processor.import_mapping
        imports = updater.factory.import_processor.import_mapping[module_name]

        wildcard_keys = [k for k in imports if k.startswith("*")]
        assert len(wildcard_keys) >= 2

    def test_aliased_imports_tracked(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        from codebase_rag.graph_updater import GraphUpdater

        test_file = temp_repo / "aliases.rs"
        test_file.write_text("""
use std::collections::HashMap as Map;
use std::io::{self as io_module, Read as ReadTrait};
use crate::models::User as UserModel;

fn use_aliases() {
    let m: Map<String, i32> = Map::new();
}
""")
        parsers, queries = load_parsers()
        if "rust" not in parsers:
            pytest.skip("Rust parser not available")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        project_name = temp_repo.name
        module_name = f"{project_name}.aliases"

        assert module_name in updater.factory.import_processor.import_mapping
        imports = updater.factory.import_processor.import_mapping[module_name]

        assert "Map" in imports
        assert imports["Map"] == "std::collections::HashMap"

        assert "io_module" in imports
        assert imports["io_module"] == "std::io"

        assert "ReadTrait" in imports
        assert imports["ReadTrait"] == "std::io::Read"

        assert "UserModel" in imports
        assert imports["UserModel"] == "crate::models::User"


class TestExtractUseImportsEdgeCases:
    def test_empty_group(self) -> None:
        code = "use std::{};"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        if use_node is None:
            pytest.skip("Parser does not accept empty use group")

        result = extract_use_imports(use_node)
        assert result == {}

    def test_deeply_nested_path(self) -> None:
        code = "use a::b::c::d::e::f::g::Target;"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "Target" in result
        assert result["Target"] == "a::b::c::d::e::f::g::Target"

    def test_super_super_import(self) -> None:
        code = "use super::super::grandparent;"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "grandparent" in result

    def test_mixed_self_and_items_in_group(self) -> None:
        code = "use std::fs::{self, File, read_to_string};"
        root = parse_rust_code(code)
        use_node = find_node_by_type(root, "use_declaration")
        assert use_node is not None

        result = extract_use_imports(use_node)
        assert "self" in result
        assert "File" in result
        assert "read_to_string" in result
        assert result["self"] == "std::fs"
        assert result["File"] == "std::fs::File"
        assert result["read_to_string"] == "std::fs::read_to_string"
