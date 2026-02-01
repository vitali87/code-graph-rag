from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_modules_project(temp_repo: Path) -> Path:
    """Create a Rust project structure for module system testing."""
    project_path = temp_repo / "rust_modules_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Module system test crate"
    )
    (project_path / "src" / "utils").mkdir()
    (project_path / "src" / "utils" / "mod.rs").write_text(
        encoding="utf-8", data="// Utils module"
    )
    (project_path / "src" / "network").mkdir()
    (project_path / "src" / "network" / "mod.rs").write_text(
        encoding="utf-8", data="// Network module"
    )

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""[package]
name = "rust_modules_test"
version = "0.1.0"
edition = "2021"
""",
    )

    return project_path


def test_basic_module_declarations(
    rust_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic module declarations and inline modules."""
    test_file = rust_modules_project / "basic_modules.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Inline module declarations
mod utilities {
    pub fn helper_function() -> i32 {
        42
    }

    pub struct HelperStruct {
        pub value: i32,
    }

    impl HelperStruct {
        pub fn new(value: i32) -> Self {
            HelperStruct { value }
        }

        pub fn get_value(&self) -> i32 {
            self.value
        }
    }

    // Private function
    fn private_helper() -> String {
        "private".to_string()
    }

    // Nested module
    pub mod nested {
        pub fn nested_function() -> &'static str {
            "nested"
        }

        pub struct NestedStruct;

        impl NestedStruct {
            pub fn method(&self) -> i32 {
                100
            }
        }

        // Deeply nested module
        pub mod deep {
            pub const DEEP_CONSTANT: i32 = 999;

            pub fn deep_function() -> i32 {
                DEEP_CONSTANT
            }
        }
    }
}

// Module with various visibility levels
mod visibility_demo {
    // Public items
    pub struct PublicStruct {
        pub public_field: i32,
        pub(crate) crate_field: String,
        pub(super) super_field: bool,
        pub(self) self_field: f64,
        private_field: u8,
    }

    impl PublicStruct {
        pub fn new() -> Self {
            PublicStruct {
                public_field: 1,
                crate_field: "crate".to_string(),
                super_field: true,
                self_field: 3.14,
                private_field: 255,
            }
        }

        pub fn public_method(&self) -> i32 {
            self.public_field
        }

        pub(crate) fn crate_method(&self) -> &str {
            &self.crate_field
        }

        pub(super) fn super_method(&self) -> bool {
            self.super_field
        }

        fn private_method(&self) -> u8 {
            self.private_field
        }
    }

    // Pub(in path) visibility
    pub mod inner {
        pub(in crate::visibility_demo) struct InnerStruct {
            value: i32,
        }

        impl InnerStruct {
            pub(in crate::visibility_demo) fn new(value: i32) -> Self {
                InnerStruct { value }
            }
        }
    }
}

// Using items from modules
use utilities::HelperStruct;
use utilities::nested::{NestedStruct, deep::DEEP_CONSTANT};
use visibility_demo::PublicStruct;

fn test_module_usage() {
    // Using items from utilities module
    let helper = HelperStruct::new(42);
    println!("Helper value: {}", helper.get_value());

    // Using nested module items
    let nested = NestedStruct;
    println!("Nested method: {}", nested.method());

    // Using deep constant
    println!("Deep constant: {}", DEEP_CONSTANT);

    // Using visibility demo
    let public_struct = PublicStruct::new();
    println!("Public field: {}", public_struct.public_field);
}

// Module re-exports
pub mod re_exports {
    pub use utilities::HelperStruct as PublicHelper;
    pub use utilities::nested::NestedStruct;
    pub use utilities::nested::deep;

    // Re-export with different visibility
    pub(crate) use visibility_demo::PublicStruct as CratePublicStruct;
}

// External module declarations (would reference separate files)
mod external_math; // Would look for src/external_math.rs or src/external_math/mod.rs
mod external_io;   // Would look for src/external_io.rs or src/external_io/mod.rs

// Conditional compilation with modules
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_helper_struct() {
        let helper = HelperStruct::new(100);
        assert_eq!(helper.get_value(), 100);
    }

    #[test]
    fn test_nested_functionality() {
        let nested = NestedStruct;
        assert_eq!(nested.method(), 100);
    }
}

#[cfg(feature = "special")]
mod special_feature {
    pub fn special_function() -> &'static str {
        "special feature enabled"
    }
}

// Platform-specific modules
#[cfg(unix)]
mod unix_specific {
    pub fn unix_function() -> &'static str {
        "Unix-specific functionality"
    }
}

#[cfg(windows)]
mod windows_specific {
    pub fn windows_function() -> &'static str {
        "Windows-specific functionality"
    }
}
""",
    )

    run_updater(rust_modules_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    module_calls = [
        call
        for call in calls
        if "utilities" in str(call)
        or "HelperStruct" in str(call)
        or "visibility_demo" in str(call)
    ]
    assert len(module_calls) > 0, "Module structures should be detected"


def test_complex_use_statements(
    rust_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test complex use statements and path resolution."""
    test_file = rust_modules_project / "use_statements.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Standard library imports
use std::collections::{HashMap, HashSet, BTreeMap};
use std::sync::{Arc, Mutex, RwLock};
use std::thread;
use std::time::{Duration, Instant};
use std::fs::{File, OpenOptions};
use std::io::{Read, Write, BufReader, BufWriter};
use std::path::{Path, PathBuf};

// Glob imports
use std::collections::*;
use std::sync::mpsc::*;

// Aliased imports
use std::collections::HashMap as Map;
use std::sync::Arc as AtomicReference;
use std::time::Duration as TimeDuration;

// Nested imports with braces
use std::sync::{
    Arc,
    Mutex,
    mpsc::{Sender, Receiver, channel},
    atomic::{AtomicBool, AtomicUsize, Ordering},
};

// Self and super imports
use self::local_module::LocalStruct;
use super::parent_function;
use crate::root_function;

// Complex path imports
use crate::utils::math::{add, subtract, multiply};
use crate::network::http::{Request, Response, HttpClient};
use crate::database::models::{User, Post, Comment};

// Re-export patterns
pub use std::collections::HashMap;
pub use std::sync::Arc;
pub(crate) use std::thread::spawn;

// Conditional imports
#[cfg(feature = "serde")]
use serde::{Serialize, Deserialize};

#[cfg(feature = "tokio")]
use tokio::runtime::Runtime;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

#[cfg(windows)]
use std::os::windows::fs::OpenOptionsExt;

// Module with local imports
mod local_module {
    use super::Map; // Using aliased import from parent
    use crate::root_function;

    pub struct LocalStruct {
        data: Map<String, i32>,
    }

    impl LocalStruct {
        pub fn new() -> Self {
            LocalStruct {
                data: Map::new(),
            }
        }

        pub fn insert(&mut self, key: String, value: i32) {
            self.data.insert(key, value);
        }
    }

    // Nested module with complex imports
    mod nested {
        use super::super::AtomicReference; // Two levels up
        use crate::utils::string_utils::*;

        pub fn process_data() -> AtomicReference<Vec<String>> {
            AtomicReference::new(vec!["data".to_string()])
        }
    }
}

// External crate imports (would be in Cargo.toml dependencies)
extern crate serde;
extern crate tokio;
extern crate reqwest;

// Using external crates
use serde::{Serialize, Deserialize};
use tokio::runtime::Builder;
use reqwest::Client;

// Function using various imported types
fn demonstrate_imports() -> Result<(), Box<dyn std::error::Error>> {
    // Using standard library imports
    let mut map: Map<String, i32> = Map::new();
    map.insert("key".to_string(), 42);

    let shared_data = AtomicReference::new(Mutex::new(map));
    let duration = TimeDuration::from_secs(1);

    // Using local module
    let mut local = LocalStruct::new();
    local.insert("local_key".to_string(), 100);

    // Using channel
    let (tx, rx): (Sender<i32>, Receiver<i32>) = channel();
    tx.send(42)?;
    let received = rx.recv()?;

    println!("Received: {}", received);
    Ok(())
}

// Import groups with different visibility
pub mod public_imports {
    pub use std::collections::HashMap;
    pub use std::sync::Arc;

    pub(crate) use std::thread;
    pub(super) use std::time::Duration;
}

mod private_imports {
    use std::fs::File;
    use std::io::Read;

    pub(super) fn read_file() -> std::io::Result<String> {
        let mut file = File::open("example.txt")?;
        let mut contents = String::new();
        file.read_to_string(&mut contents)?;
        Ok(contents)
    }
}

// Macro imports
use std::vec;
use std::format;
use std::println;

// Trait imports
use std::fmt::{Display, Debug};
use std::iter::{Iterator, IntoIterator};
use std::ops::{Add, Sub, Mul, Div};

// Generic trait implementations using imports
#[derive(Debug)]
struct Calculator<T> {
    value: T,
}

impl<T> Calculator<T>
where
    T: Add<Output = T> + Sub<Output = T> + Mul<Output = T> + Div<Output = T> + Copy,
{
    fn new(value: T) -> Self {
        Calculator { value }
    }

    fn add(&self, other: T) -> T {
        self.value + other
    }

    fn subtract(&self, other: T) -> T {
        self.value - other
    }

    fn multiply(&self, other: T) -> T {
        self.value * other
    }

    fn divide(&self, other: T) -> T {
        self.value / other
    }
}

impl<T> Display for Calculator<T>
where
    T: Display,
{
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Calculator({})", self.value)
    }
}

// Functions that would be imported from other modules
fn root_function() -> &'static str {
    "root function"
}

fn parent_function() -> &'static str {
    "parent function"
}

// Mock modules that would be separate files
mod utils {
    pub mod math {
        pub fn add(a: i32, b: i32) -> i32 { a + b }
        pub fn subtract(a: i32, b: i32) -> i32 { a - b }
        pub fn multiply(a: i32, b: i32) -> i32 { a * b }
    }

    pub mod string_utils {
        pub fn reverse_string(s: &str) -> String {
            s.chars().rev().collect()
        }

        pub fn uppercase(s: &str) -> String {
            s.to_uppercase()
        }
    }
}

mod network {
    pub mod http {
        pub struct Request {
            pub url: String,
            pub method: String,
        }

        pub struct Response {
            pub status: u16,
            pub body: String,
        }

        pub struct HttpClient;

        impl HttpClient {
            pub fn new() -> Self {
                HttpClient
            }

            pub fn send(&self, req: Request) -> Response {
                Response {
                    status: 200,
                    body: "OK".to_string(),
                }
            }
        }
    }
}

mod database {
    pub mod models {
        pub struct User {
            pub id: u64,
            pub name: String,
        }

        pub struct Post {
            pub id: u64,
            pub title: String,
            pub author_id: u64,
        }

        pub struct Comment {
            pub id: u64,
            pub post_id: u64,
            pub content: String,
        }
    }
}
""",
    )

    run_updater(rust_modules_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    import_calls = [
        call
        for call in calls
        if "use" in str(call)
        or "Calculator" in str(call)
        or "demonstrate_imports" in str(call)
    ]
    assert len(import_calls) > 0, "Import statements and usage should be detected"


def test_module_path_resolution(
    rust_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test complex module path resolution and relative paths."""
    test_file = rust_modules_project / "path_resolution.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Complex module hierarchy for path testing
mod level1 {
    pub fn level1_function() -> &'static str {
        "level1"
    }

    pub mod level2 {
        pub fn level2_function() -> &'static str {
            "level2"
        }

        pub mod level3 {
            pub fn level3_function() -> &'static str {
                "level3"
            }

            // Using absolute paths
            pub fn call_root() -> &'static str {
                crate::root_function()
            }

            // Using relative paths
            pub fn call_parent() -> &'static str {
                super::level2_function()
            }

            // Using grandparent
            pub fn call_grandparent() -> &'static str {
                super::super::level1_function()
            }

            // Using self
            pub fn call_self() -> &'static str {
                self::level3_function()
            }

            pub mod level4 {
                // Complex relative navigation
                pub fn complex_navigation() -> String {
                    format!(
                        "{} - {} - {} - {}",
                        crate::root_function(),
                        super::super::super::level1_function(),
                        super::super::level2_function(),
                        super::level3_function()
                    )
                }

                // Import from multiple levels
                use super::super::super::level1_function;
                use super::super::level2_function;
                use super::level3_function;
                use crate::root_function;

                pub fn using_imports() -> String {
                    format!(
                        "{} - {} - {} - {}",
                        root_function(),
                        level1_function(),
                        level2_function(),
                        level3_function()
                    )
                }
            }
        }
    }

    // Sibling module access
    pub mod sibling {
        pub fn sibling_function() -> &'static str {
            "sibling"
        }

        // Access sibling through parent
        pub fn access_level2() -> &'static str {
            super::level2::level2_function()
        }

        // Access nested sibling
        pub fn access_level3() -> &'static str {
            super::level2::level3::level3_function()
        }
    }
}

// Another top-level module
mod parallel {
    pub fn parallel_function() -> &'static str {
        "parallel"
    }

    // Access different branch of module tree
    pub fn access_level1() -> &'static str {
        crate::level1::level1_function()
    }

    pub fn access_sibling() -> &'static str {
        crate::level1::sibling::sibling_function()
    }

    // Complex cross-module access
    pub fn complex_cross_access() -> String {
        use crate::level1::level2::level3::level4;
        level4::complex_navigation()
    }
}

// Root level functions
pub fn root_function() -> &'static str {
    "root"
}

// Module with path aliases
mod path_aliases {
    // Create type aliases for complex paths
    type Level3Module = crate::level1::level2::level3;
    type Level4Module = crate::level1::level2::level3::level4;

    // Use aliases in function signatures
    pub fn use_type_alias() -> String {
        Level4Module::complex_navigation()
    }

    // Re-export with aliases
    pub use crate::level1::level2::level3::level3_function as deep_function;
    pub use crate::parallel::parallel_function;

    pub fn call_reexports() -> String {
        format!("{} - {}", deep_function(), parallel_function())
    }
}

// Conditional module paths
#[cfg(feature = "advanced")]
mod advanced_paths {
    use crate::level1::level2::level3::level4;

    pub fn advanced_functionality() -> String {
        level4::using_imports()
    }
}

// Module with trait implementations using paths
mod trait_implementations {
    use std::fmt::Display;

    // Struct that uses types from various modules
    pub struct PathUser {
        level1_data: String,
        level2_data: String,
        level3_data: String,
    }

    impl PathUser {
        pub fn new() -> Self {
            PathUser {
                level1_data: crate::level1::level1_function().to_string(),
                level2_data: crate::level1::level2::level2_function().to_string(),
                level3_data: crate::level1::level2::level3::level3_function().to_string(),
            }
        }
    }

    impl Display for PathUser {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            write!(
                f,
                "PathUser {{ {}, {}, {} }}",
                self.level1_data,
                self.level2_data,
                self.level3_data
            )
        }
    }

    // Generic function using module paths
    pub fn process_with_paths<F>(processor: F) -> String
    where
        F: Fn(&str, &str, &str) -> String,
    {
        processor(
            crate::level1::level1_function(),
            crate::level1::level2::level2_function(),
            crate::level1::level2::level3::level3_function(),
        )
    }
}

// Testing module path resolution
mod path_tests {
    use super::*;

    // Import various paths for testing
    use crate::level1::{level1_function, level2, sibling};
    use crate::level1::level2::level3::{level3_function, level4};
    use crate::parallel;
    use crate::path_aliases;
    use crate::trait_implementations::{PathUser, process_with_paths};

    pub fn test_all_paths() {
        // Test direct function calls
        println!("Level 1: {}", level1_function());
        println!("Level 2: {}", level2::level2_function());
        println!("Level 3: {}", level3_function());

        // Test complex navigation
        println!("Complex: {}", level4::complex_navigation());
        println!("Using imports: {}", level4::using_imports());

        // Test sibling access
        println!("Sibling: {}", sibling::sibling_function());
        println!("Sibling to level2: {}", sibling::access_level2());

        // Test parallel module
        println!("Parallel: {}", parallel::parallel_function());
        println!("Parallel to level1: {}", parallel::access_level1());

        // Test aliases
        println!("Alias: {}", path_aliases::use_type_alias());
        println!("Reexports: {}", path_aliases::call_reexports());

        // Test trait implementations
        let path_user = PathUser::new();
        println!("PathUser: {}", path_user);

        let result = process_with_paths(|a, b, c| format!("{}-{}-{}", a, b, c));
        println!("Processed: {}", result);
    }
}

// Macro using module paths
macro_rules! call_module_function {
    ($module_path:path) => {
        $module_path()
    };
}

// Using the macro with different paths
pub fn macro_path_demo() {
    let result1 = call_module_function!(crate::level1::level1_function);
    let result2 = call_module_function!(crate::level1::level2::level2_function);
    let result3 = call_module_function!(crate::parallel::parallel_function);

    println!("Macro results: {} - {} - {}", result1, result2, result3);
}

// Workspace and external crate paths (conceptual)
mod external_integration {
    // These would reference external crates in a real project
    // use my_workspace_crate::utils::helper;
    // use external_dependency::api::Client;

    pub fn integration_example() {
        // Use of external paths would go here
        println!("External integration placeholder");
    }
}
""",
    )

    run_updater(rust_modules_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    path_calls = [
        call
        for call in calls
        if "level" in str(call) or "path" in str(call) or "PathUser" in str(call)
    ]
    assert len(path_calls) > 0, "Module path resolution should be detected"


def test_advanced_visibility_patterns(
    rust_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test advanced visibility patterns and access control."""
    test_file = rust_modules_project / "advanced_visibility.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Complex visibility hierarchy
pub mod public_api {
    // Public interface
    pub struct PublicClient {
        inner: InternalClient,
    }

    impl PublicClient {
        pub fn new() -> Self {
            PublicClient {
                inner: InternalClient::new(),
            }
        }

        pub fn execute(&self, command: &str) -> String {
            self.inner.process(command)
        }
    }

    // Internal implementation hidden from external users
    struct InternalClient {
        config: Config,
    }

    impl InternalClient {
        fn new() -> Self {
            InternalClient {
                config: Config::default(),
            }
        }

        fn process(&self, command: &str) -> String {
            format!("Processing: {} with config: {:?}", command, self.config)
        }
    }

    #[derive(Debug, Default)]
    struct Config {
        timeout: u64,
        retries: u32,
    }

    // Crate-visible helper
    pub(crate) fn internal_helper() -> &'static str {
        "internal helper"
    }

    // Super-visible (accessible to parent module)
    pub(super) fn parent_helper() -> &'static str {
        "parent helper"
    }
}

// Module demonstrating pub(in path) visibility
pub mod restricted_access {
    pub struct Container {
        pub public_data: String,
        pub(crate) crate_data: String,
        pub(super) super_data: String,
        pub(in crate::restricted_access) module_data: String,
        private_data: String,
    }

    impl Container {
        pub fn new() -> Self {
            Container {
                public_data: "public".to_string(),
                crate_data: "crate".to_string(),
                super_data: "super".to_string(),
                module_data: "module".to_string(),
                private_data: "private".to_string(),
            }
        }

        // Different visibility levels for methods
        pub fn public_method(&self) -> &str {
            &self.public_data
        }

        pub(crate) fn crate_method(&self) -> &str {
            &self.crate_data
        }

        pub(super) fn super_method(&self) -> &str {
            &self.super_data
        }

        pub(in crate::restricted_access) fn module_method(&self) -> &str {
            &self.module_data
        }

        fn private_method(&self) -> &str {
            &self.private_data
        }
    }

    // Submodule that can access module_data
    pub mod submodule {
        use super::Container;

        pub fn access_module_data() -> String {
            let container = Container::new();
            // Can access module_data because we're in crate::restricted_access
            format!("Accessed: {}", container.module_data)
        }

        pub fn call_module_method() -> String {
            let container = Container::new();
            container.module_method().to_string()
        }
    }
}

// Friend-like access patterns using pub(in path)
mod friendship_pattern {
    pub(in crate::friendship_pattern) struct SecretData {
        value: i32,
    }

    impl SecretData {
        pub(in crate::friendship_pattern) fn new(value: i32) -> Self {
            SecretData { value }
        }

        pub(in crate::friendship_pattern) fn get_value(&self) -> i32 {
            self.value
        }
    }

    pub mod friend1 {
        use super::SecretData;

        pub fn create_secret() -> SecretData {
            SecretData::new(42)
        }

        pub fn read_secret(data: &SecretData) -> i32 {
            data.get_value()
        }
    }

    pub mod friend2 {
        use super::SecretData;

        pub fn modify_secret(data: SecretData) -> SecretData {
            SecretData::new(data.get_value() * 2)
        }
    }
}

// Trait visibility patterns
pub mod trait_visibility {
    // Public trait with private methods
    pub trait PublicTrait {
        fn public_method(&self) -> String;

        // This would be private if traits supported it
        // Private implementation detail exposed as part of trait
        fn implementation_detail(&self) -> i32 {
            42
        }
    }

    // Crate-private trait
    pub(crate) trait CrateTrait {
        fn crate_method(&self) -> String;
    }

    // Module-private trait
    trait PrivateTrait {
        fn private_method(&self) -> String;
    }

    pub struct PublicStruct;

    impl PublicTrait for PublicStruct {
        fn public_method(&self) -> String {
            "public implementation".to_string()
        }
    }

    impl CrateTrait for PublicStruct {
        fn crate_method(&self) -> String {
            "crate implementation".to_string()
        }
    }

    impl PrivateTrait for PublicStruct {
        fn private_method(&self) -> String {
            "private implementation".to_string()
        }
    }

    // Function that uses private trait
    pub fn use_private_trait() -> String {
        let s = PublicStruct;
        s.private_method()
    }
}

// Enum and variant visibility
pub mod enum_visibility {
    // Public enum with mixed visibility variants
    pub enum PublicEnum {
        PublicVariant(i32),
        // Variants can't have different visibility in Rust
        // but fields within variants can
    }

    // Struct-like enum variants with field visibility
    pub enum ComplexEnum {
        StructVariant {
            pub public_field: String,
            pub(crate) crate_field: i32,
            private_field: bool,
        },
        TupleVariant(pub String, pub(crate) i32, bool),
    }

    impl ComplexEnum {
        pub fn new_struct() -> Self {
            ComplexEnum::StructVariant {
                public_field: "public".to_string(),
                crate_field: 42,
                private_field: true,
            }
        }

        pub fn new_tuple() -> Self {
            ComplexEnum::TupleVariant("public".to_string(), 42, true)
        }

        // Method to access private fields
        pub fn get_private_info(&self) -> bool {
            match self {
                ComplexEnum::StructVariant { private_field, .. } => *private_field,
                ComplexEnum::TupleVariant(_, _, private) => *private,
            }
        }
    }
}

// Macro visibility patterns
macro_rules! private_macro {
    ($x:expr) => {
        $x * 2
    };
}

pub macro_rules! public_macro {
    ($x:expr) => {
        $x * 3
    };
}

#[macro_export]
macro_rules! exported_macro {
    ($x:expr) => {
        $x * 4
    };
}

// Using macros with different visibility
pub fn macro_visibility_demo() {
    let a = private_macro!(5);  // Can use within same module
    let b = public_macro!(5);   // Can use because it's public
    let c = exported_macro!(5); // Can use because it's exported

    println!("Macro results: {} {} {}", a, b, c);
}

// Const and static visibility
pub mod constants {
    pub const PUBLIC_CONST: i32 = 100;
    pub(crate) const CRATE_CONST: i32 = 200;
    const PRIVATE_CONST: i32 = 300;

    pub static PUBLIC_STATIC: i32 = 1000;
    pub(crate) static CRATE_STATIC: i32 = 2000;
    static PRIVATE_STATIC: i32 = 3000;

    pub fn access_constants() -> (i32, i32, i32) {
        (PUBLIC_CONST, CRATE_CONST, PRIVATE_CONST)
    }

    pub fn access_statics() -> (i32, i32, i32) {
        (PUBLIC_STATIC, CRATE_STATIC, PRIVATE_STATIC)
    }
}

// Testing all visibility patterns
pub fn test_visibility_patterns() {
    // Test public API
    let client = public_api::PublicClient::new();
    println!("Client result: {}", client.execute("test"));

    // Test restricted access
    let container = restricted_access::Container::new();
    println!("Public data: {}", container.public_method());
    println!("Crate data: {}", container.crate_method());

    // Test submodule access
    println!("Submodule access: {}", restricted_access::submodule::access_module_data());

    // Test friendship pattern
    let secret = friendship_pattern::friend1::create_secret();
    let value = friendship_pattern::friend1::read_secret(&secret);
    let modified = friendship_pattern::friend2::modify_secret(secret);
    println!("Friendship pattern: {}", value);

    // Test trait visibility
    let s = trait_visibility::PublicStruct;
    println!("Public trait: {}", s.public_method());
    println!("Crate trait: {}", s.crate_method());
    println!("Private trait usage: {}", trait_visibility::use_private_trait());

    // Test enum visibility
    let enum_val = enum_visibility::ComplexEnum::new_struct();
    println!("Enum private info: {}", enum_val.get_private_info());

    // Test constants
    let (pub_const, crate_const, priv_const) = constants::access_constants();
    println!("Constants: {} {} {}", pub_const, crate_const, priv_const);

    // Test macros
    macro_visibility_demo();
}
""",
    )

    run_updater(rust_modules_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    visibility_calls = [
        call
        for call in calls
        if "PublicClient" in str(call)
        or "Container" in str(call)
        or "visibility" in str(call)
    ]
    assert len(visibility_calls) > 0, "Visibility patterns should be detected"


def test_module_attributes_and_cfg(
    rust_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test module attributes and conditional compilation."""
    test_file = rust_modules_project / "module_attributes.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Module with various attributes
#[cfg(feature = "networking")]
pub mod networking {
    pub fn network_function() -> &'static str {
        "networking enabled"
    }

    #[cfg(feature = "async")]
    pub mod async_networking {
        pub fn async_network_function() -> &'static str {
            "async networking enabled"
        }
    }

    #[cfg(not(feature = "async"))]
    pub mod sync_networking {
        pub fn sync_network_function() -> &'static str {
            "sync networking fallback"
        }
    }
}

#[cfg(not(feature = "networking"))]
pub mod no_networking {
    pub fn fallback_function() -> &'static str {
        "networking disabled"
    }
}

// Platform-specific modules
#[cfg(unix)]
pub mod unix {
    pub fn unix_specific() -> &'static str {
        "Unix platform"
    }

    #[cfg(target_os = "linux")]
    pub mod linux {
        pub fn linux_specific() -> &'static str {
            "Linux OS"
        }
    }

    #[cfg(target_os = "macos")]
    pub mod macos {
        pub fn macos_specific() -> &'static str {
            "macOS"
        }
    }
}

#[cfg(windows)]
pub mod windows {
    pub fn windows_specific() -> &'static str {
        "Windows platform"
    }
}

// Architecture-specific modules
#[cfg(target_arch = "x86_64")]
pub mod x86_64 {
    pub fn x86_64_optimized() -> &'static str {
        "x86_64 optimizations"
    }
}

#[cfg(target_arch = "aarch64")]
pub mod aarch64 {
    pub fn arm_optimized() -> &'static str {
        "ARM optimizations"
    }
}

// Debug vs Release modules
#[cfg(debug_assertions)]
pub mod debug_tools {
    pub fn debug_function() -> &'static str {
        "debug mode active"
    }

    pub fn assert_helper(condition: bool, message: &str) {
        assert!(condition, "{}", message);
    }
}

#[cfg(not(debug_assertions))]
pub mod release_optimizations {
    pub fn optimized_function() -> &'static str {
        "release mode optimizations"
    }

    #[inline(always)]
    pub fn inline_helper() -> i32 {
        42
    }
}

// Test configuration
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_conditional_compilation() {
        #[cfg(feature = "networking")]
        {
            assert_eq!(networking::network_function(), "networking enabled");
        }

        #[cfg(not(feature = "networking"))]
        {
            assert_eq!(no_networking::fallback_function(), "networking disabled");
        }
    }

    #[cfg(unix)]
    #[test]
    fn test_unix_specific() {
        assert_eq!(unix::unix_specific(), "Unix platform");
    }

    #[cfg(windows)]
    #[test]
    fn test_windows_specific() {
        assert_eq!(windows::windows_specific(), "Windows platform");
    }
}

// Documentation attributes
#[doc = "This module demonstrates documentation attributes"]
pub mod documented {
    /// This function has documentation
    ///
    /// # Examples
    ///
    /// ```
    /// use crate::documented::documented_function;
    /// let result = documented_function();
    /// assert_eq!(result, "documented");
    /// ```
    pub fn documented_function() -> &'static str {
        "documented"
    }

    #[doc(hidden)]
    pub fn hidden_function() -> &'static str {
        "hidden from docs"
    }
}

// Deprecated modules and items
#[deprecated(since = "1.0.0", note = "Use `new_module` instead")]
pub mod old_module {
    pub fn old_function() -> &'static str {
        "deprecated functionality"
    }
}

pub mod new_module {
    pub fn new_function() -> &'static str {
        "new functionality"
    }
}

// Custom attributes (would need proc macros to be meaningful)
#[custom_attribute]
pub mod custom_attributed {
    #[custom_function_attribute(param = "value")]
    pub fn custom_function() -> &'static str {
        "custom attributed"
    }
}

// Allow/warn/deny attributes
#[allow(dead_code)]
pub mod allowed {
    fn unused_function() -> i32 {
        42
    }

    #[warn(missing_docs)]
    pub mod warn_missing_docs {
        pub fn undocumented_function() -> i32 {
            100
        }
    }
}

#[deny(unused_variables)]
pub mod strict_module {
    pub fn strict_function() -> i32 {
        let used_var = 42;
        used_var
    }
}

// Path attributes
#[path = "alternative_path.rs"]
mod alternative_module;

// Inline module with path attributes
#[cfg(test)]
#[path = "test_utils"]
mod test_utilities {
    // This would normally reference an external file
    pub fn test_helper() -> &'static str {
        "test utility"
    }
}

// Module with multiple attributes
#[cfg(feature = "advanced")]
#[doc = "Advanced functionality module"]
#[allow(clippy::module_inception)]
pub mod advanced {
    #[cfg(feature = "experimental")]
    pub mod experimental {
        #[doc(hidden)]
        #[deprecated(note = "Experimental API")]
        pub fn experimental_function() -> &'static str {
            "experimental"
        }
    }

    #[cfg(not(feature = "experimental"))]
    pub mod stable {
        pub fn stable_function() -> &'static str {
            "stable API"
        }
    }
}

// Procedural macro attributes (conceptual)
#[proc_macro_attribute]
pub fn custom_derive(_attr: TokenStream, item: TokenStream) -> TokenStream {
    // This would be implemented in a proc macro crate
    item
}

// Using cfg_attr for conditional attributes
pub mod conditional_attrs {
    #[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
    pub struct ConditionalStruct {
        pub value: i32,
    }

    #[cfg_attr(debug_assertions, derive(Debug))]
    pub struct DebugStruct {
        pub data: String,
    }

    impl ConditionalStruct {
        pub fn new(value: i32) -> Self {
            ConditionalStruct { value }
        }
    }

    impl DebugStruct {
        pub fn new(data: String) -> Self {
            DebugStruct { data }
        }
    }
}

// Function to test all conditional compilation
pub fn test_all_attributes() {
    // Test feature-gated modules
    #[cfg(feature = "networking")]
    {
        println!("Networking: {}", networking::network_function());
    }

    // Test platform-specific code
    #[cfg(unix)]
    {
        println!("Unix: {}", unix::unix_specific());
    }

    #[cfg(windows)]
    {
        println!("Windows: {}", windows::windows_specific());
    }

    // Test debug vs release
    #[cfg(debug_assertions)]
    {
        println!("Debug: {}", debug_tools::debug_function());
    }

    #[cfg(not(debug_assertions))]
    {
        println!("Release: {}", release_optimizations::optimized_function());
    }

    // Test documented module
    println!("Documented: {}", documented::documented_function());

    // Test deprecated (with warning)
    #[allow(deprecated)]
    {
        println!("Old: {}", old_module::old_function());
    }
    println!("New: {}", new_module::new_function());

    // Test conditional attributes
    let conditional = conditional_attrs::ConditionalStruct::new(42);
    println!("Conditional value: {}", conditional.value);
}
""",
    )

    run_updater(rust_modules_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    attr_calls = [
        call
        for call in calls
        if "cfg" in str(call) or "networking" in str(call) or "documented" in str(call)
    ]
    assert len(attr_calls) > 0, "Module attributes should be detected"
