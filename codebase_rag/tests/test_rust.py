from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def rust_project(temp_repo: Path) -> Path:
    """Create a comprehensive Rust project structure."""
    project_path = temp_repo / "rust_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Library root"
    )
    (project_path / "src" / "main.rs").write_text(encoding="utf-8", data="fn main() {}")
    (project_path / "src" / "utils").mkdir()
    (project_path / "src" / "utils" / "mod.rs").write_text(
        encoding="utf-8", data="// Utils module"
    )
    (project_path / "tests").mkdir()
    (project_path / "examples").mkdir()

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""[package]
name = "rust_test"
version = "0.1.0"
""",
    )

    return project_path


def test_basic_rust_functions(
    rust_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic Rust function parsing including async, const, and unsafe functions."""
    test_file = rust_project / "basic_functions.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic function declarations
fn simple_function() {
    println!("Hello, world!");
}

fn function_with_params(x: i32, y: &str) -> i32 {
    println!("{}", y);
    x * 2
}

// Function with generics
fn generic_function<T: Clone>(value: T) -> T {
    value.clone()
}

// Function with lifetimes
fn lifetime_function<'a>(x: &'a str, y: &'a str) -> &'a str {
    if x.len() > y.len() { x } else { y }
}

// Async function
async fn async_function() -> Result<String, std::io::Error> {
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    Ok("completed".to_string())
}

// Const function
const fn const_function(x: i32) -> i32 {
    x + 1
}

// Unsafe function
unsafe fn unsafe_function(ptr: *const i32) -> i32 {
    *ptr
}

// Function with complex return type
fn complex_return() -> impl Iterator<Item = i32> {
    (0..10).filter(|x| x % 2 == 0)
}

// Function with where clause
fn where_clause_function<T, U>(x: T, y: U) -> T
where
    T: Clone + std::fmt::Debug,
    U: Into<String>,
{
    println!("{:?}", x);
    println!("{}", y.into());
    x.clone()
}

// Demonstrating function calls
fn demonstrate_functions() {
    simple_function();
    let result = function_with_params(42, "test");
    let generic_result = generic_function::<i32>(10);

    let s1 = "hello";
    let s2 = "world";
    let longer = lifetime_function(s1, s2);

    let const_result = const_function(5);

    let value = 42i32;
    unsafe {
        let unsafe_result = unsafe_function(&value);
    }

    let iter = complex_return();
    let where_result = where_clause_function(100, "test");
}
""",
    )

    run_updater(rust_project, mock_ingestor)

    project_name = rust_project.name

    expected_functions = [
        f"{project_name}.basic_functions.simple_function",
        f"{project_name}.basic_functions.function_with_params",
        f"{project_name}.basic_functions.generic_function",
        f"{project_name}.basic_functions.lifetime_function",
        f"{project_name}.basic_functions.async_function",
        f"{project_name}.basic_functions.const_function",
        f"{project_name}.basic_functions.unsafe_function",
        f"{project_name}.basic_functions.complex_return",
        f"{project_name}.basic_functions.where_clause_function",
        f"{project_name}.basic_functions.demonstrate_functions",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    function_call_relationships = [
        call
        for call in call_relationships
        if "basic_functions" in call.args[0][2]
        and any(
            func_name in call.args[2][2]
            for func_name in [
                "simple_function",
                "function_with_params",
                "generic_function",
            ]
        )
    ]

    assert len(function_call_relationships) >= 3, (
        f"Expected at least 3 function call relationships, found {len(function_call_relationships)}"
    )


def test_rust_structs_enums_unions(
    rust_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Rust struct, enum, and union declarations."""
    test_file = rust_project / "types.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic struct
struct Point {
    x: f64,
    y: f64,
}

// Tuple struct
struct Color(u8, u8, u8);

// Unit struct
struct Unit;

// Generic struct
struct Container<T> {
    value: T,
}

// Struct with lifetime parameters
struct Borrowed<'a> {
    data: &'a str,
}

// Struct with both generics and lifetimes
struct GenericBorrowed<'a, T> {
    data: &'a T,
}

// Basic enum
enum Direction {
    North,
    South,
    East,
    West,
}

// Enum with associated data
enum Message {
    Quit,
    Move { x: i32, y: i32 },
    Write(String),
    ChangeColor(i32, i32, i32),
}

// Generic enum
enum Option<T> {
    Some(T),
    None,
}

// Enum with lifetimes
enum Cow<'a> {
    Borrowed(&'a str),
    Owned(String),
}

// Union (unsafe)
union FloatOrInt {
    f: f32,
    i: i32,
}

// Implementing methods
impl Point {
    fn new(x: f64, y: f64) -> Self {
        Point { x, y }
    }

    fn distance(&self, other: &Point) -> f64 {
        ((self.x - other.x).powi(2) + (self.y - other.y).powi(2)).sqrt()
    }

    fn origin() -> Point {
        Point::new(0.0, 0.0)
    }
}

impl<T> Container<T> {
    fn new(value: T) -> Self {
        Container { value }
    }

    fn get(&self) -> &T {
        &self.value
    }
}

impl Direction {
    fn opposite(&self) -> Direction {
        match self {
            Direction::North => Direction::South,
            Direction::South => Direction::North,
            Direction::East => Direction::West,
            Direction::West => Direction::East,
        }
    }
}

// Demonstrating struct and enum usage
fn demonstrate_types() {
    let point1 = Point::new(1.0, 2.0);
    let point2 = Point { x: 3.0, y: 4.0 };
    let distance = point1.distance(&point2);

    let origin = Point::origin();

    let red = Color(255, 0, 0);
    let unit = Unit;

    let container = Container::new(42);
    let value = container.get();

    let direction = Direction::North;
    let opposite = direction.opposite();

    let msg = Message::Move { x: 10, y: 20 };
    match msg {
        Message::Quit => println!("Quit"),
        Message::Move { x, y } => println!("Move to ({}, {})", x, y),
        Message::Write(text) => println!("Write: {}", text),
        Message::ChangeColor(r, g, b) => println!("Color: ({}, {}, {})", r, g, b),
    }
}
""",
    )

    run_updater(rust_project, mock_ingestor)

    project_name = rust_project.name

    expected_classes = [
        f"{project_name}.types.Point",
        f"{project_name}.types.Color",
        f"{project_name}.types.Unit",
        f"{project_name}.types.Container",
        f"{project_name}.types.Borrowed",
        f"{project_name}.types.GenericBorrowed",
        f"{project_name}.types.Direction",
        f"{project_name}.types.Message",
        f"{project_name}.types.Option",
        f"{project_name}.types.Cow",
        f"{project_name}.types.FloatOrInt",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected types: {sorted(list(missing_classes))}"
    )

    expected_methods = [
        f"{project_name}.types.Point.new",
        f"{project_name}.types.Point.distance",
        f"{project_name}.types.Point.origin",
        f"{project_name}.types.Container.new",
        f"{project_name}.types.Container.get",
        f"{project_name}.types.Direction.opposite",
    ]

    created_methods = get_node_names(mock_ingestor, "Method")

    missing_methods = set(expected_methods) - created_methods
    assert not missing_methods, (
        f"Missing expected methods: {sorted(list(missing_methods))}"
    )


def test_rust_traits_and_implementations(
    rust_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Rust trait definitions and implementations."""
    test_file = rust_project / "traits.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic trait
trait Display {
    fn fmt(&self) -> String;

    // Default implementation
    fn print(&self) {
        println!("{}", self.fmt());
    }
}

// Generic trait
trait Clone<T = Self> {
    fn clone(&self) -> T;
}

// Trait with associated types
trait Iterator {
    type Item;

    fn next(&mut self) -> Option<Self::Item>;

    fn collect<C: FromIterator<Self::Item>>(self) -> C
    where
        Self: Sized,
    {
        FromIterator::from_iter(self)
    }
}

// Trait with associated constants
trait Constants {
    const MAX_SIZE: usize;
    const DEFAULT_VALUE: i32 = 0;
}

// Trait bounds and where clauses
trait Complex<T>
where
    T: Clone + Send + Sync,
{
    fn process(&self, value: T) -> T;
}

// Marker trait
trait Send {}
trait Sync {}

// Supertrait
trait Drawable: Display + Clone<Self> {
    fn draw(&self);
}

struct Point {
    x: f64,
    y: f64,
}

struct Circle {
    center: Point,
    radius: f64,
}

// Implementing traits
impl Display for Point {
    fn fmt(&self) -> String {
        format!("({}, {})", self.x, self.y)
    }
}

impl Clone<Point> for Point {
    fn clone(&self) -> Point {
        Point { x: self.x, y: self.y }
    }
}

impl Display for Circle {
    fn fmt(&self) -> String {
        format!("Circle at {} with radius {}", self.center.fmt(), self.radius)
    }
}

impl Constants for Circle {
    const MAX_SIZE: usize = 1000;
}

impl<T> Complex<T> for Circle
where
    T: Clone + Send + Sync,
{
    fn process(&self, value: T) -> T {
        value.clone()
    }
}

// Generic implementation
impl<T: Display> Display for Vec<T> {
    fn fmt(&self) -> String {
        let items: Vec<String> = self.iter().map(|item| item.fmt()).collect();
        format!("[{}]", items.join(", "))
    }
}

// Conditional implementation
impl<T: Clone> Clone<Vec<T>> for Vec<T> {
    fn clone(&self) -> Vec<T> {
        self.iter().cloned().collect()
    }
}

// Demonstrating trait usage
fn demonstrate_traits() {
    let point = Point { x: 1.0, y: 2.0 };
    point.print(); // Uses default implementation
    let cloned_point = point.clone();

    let circle = Circle {
        center: Point { x: 0.0, y: 0.0 },
        radius: 5.0,
    };
    circle.print();

    let numbers = vec![1, 2, 3];
    let formatted = numbers.fmt();

    // Trait object
    let drawable: &dyn Display = &point;
    drawable.print();
}
""",
    )

    run_updater(rust_project, mock_ingestor)

    project_name = rust_project.name

    expected_traits = [
        f"{project_name}.traits.Display",
        f"{project_name}.traits.Clone",
        f"{project_name}.traits.Iterator",
        f"{project_name}.traits.Constants",
        f"{project_name}.traits.Complex",
        f"{project_name}.traits.Send",
        f"{project_name}.traits.Sync",
        f"{project_name}.traits.Drawable",
    ]

    expected_structs = [
        f"{project_name}.traits.Point",
        f"{project_name}.traits.Circle",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    all_expected = expected_traits + expected_structs
    missing_classes = set(all_expected) - created_classes
    assert not missing_classes, (
        f"Missing expected traits/structs: {sorted(list(missing_classes))}"
    )

    expected_methods = [
        f"{project_name}.traits.Display.fmt",
        f"{project_name}.traits.Display.print",
        f"{project_name}.traits.Clone.clone",
        f"{project_name}.traits.Iterator.next",
        f"{project_name}.traits.Iterator.collect",
        f"{project_name}.traits.Complex.process",
        f"{project_name}.traits.Drawable.draw",
    ]

    created_methods = get_node_names(mock_ingestor, "Method")

    found_methods = set(expected_methods) & created_methods
    assert len(found_methods) >= 3, (
        f"Expected at least 3 trait methods, found: {sorted(list(found_methods))}"
    )


def test_rust_modules_and_crates(
    rust_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Rust module system and crate organization."""
    main_file = rust_project / "modules.rs"
    main_file.write_text(
        encoding="utf-8",
        data="""
// Inline module
mod inline_module {
    pub fn public_function() {
        println!("Public function in inline module");
    }

    fn private_function() {
        println!("Private function");
    }

    pub mod nested {
        pub fn nested_function() {
            super::private_function();
        }
    }
}

// Module declaration (file-based)
mod file_module;
mod utils;

// External crate usage
extern crate serde;
extern crate serde_json;

// Re-exports
pub use inline_module::public_function;
pub use utils::helper;

// Module with visibility modifiers
pub mod public_module {
    pub struct PublicStruct {
        pub field: i32,
    }

    pub(crate) struct CrateVisibleStruct {
        field: String,
    }

    pub(super) fn super_visible_function() {}
}

// Conditional compilation
#[cfg(feature = "extra")]
mod extra_module {
    pub fn extra_function() {}
}

// Module with attributes
#[allow(dead_code)]
mod test_module {
    #[derive(Debug, Clone)]
    pub struct TestStruct {
        value: i32,
    }
}

// Using modules
fn demonstrate_modules() {
    inline_module::public_function();
    inline_module::nested::nested_function();

    public_function(); // Re-exported
    helper(); // Re-exported from utils

    let public_struct = public_module::PublicStruct { field: 42 };
    let crate_struct = public_module::CrateVisibleStruct {
        field: "test".to_string(),
    };

    public_module::super_visible_function();

    #[cfg(feature = "extra")]
    extra_module::extra_function();
}
""",
    )

    file_module = rust_project / "file_module.rs"
    file_module.write_text(
        encoding="utf-8",
        data="""
pub fn file_module_function() {
    println!("Function in file module");
}

pub struct FileModuleStruct {
    pub data: String,
}
""",
    )

    utils_dir = rust_project / "utils"
    utils_dir.mkdir(exist_ok=True)
    (utils_dir / "mod.rs").write_text(
        encoding="utf-8",
        data="""
pub fn helper() {
    println!("Helper function");
}

pub mod math;
""",
    )

    (utils_dir / "math.rs").write_text(
        encoding="utf-8",
        data="""
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

pub fn multiply(a: i32, b: i32) -> i32 {
    a * b
}
""",
    )

    run_updater(rust_project, mock_ingestor)

    project_name = rust_project.name

    expected_functions = [
        f"{project_name}.modules.inline_module.public_function",
        f"{project_name}.modules.inline_module.private_function",
        f"{project_name}.modules.inline_module.nested.nested_function",
        f"{project_name}.modules.public_module.super_visible_function",
        f"{project_name}.modules.demonstrate_modules",
        f"{project_name}.file_module.file_module_function",
        f"{project_name}.utils.helper",
        f"{project_name}.utils.math.add",
        f"{project_name}.utils.math.multiply",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_functions = set(expected_functions) & created_functions
    assert len(found_functions) >= 5, (
        f"Expected at least 5 module functions, found: {sorted(list(found_functions))}"
    )

    expected_structs = [
        f"{project_name}.modules.public_module.PublicStruct",
        f"{project_name}.modules.public_module.CrateVisibleStruct",
        f"{project_name}.file_module.FileModuleStruct",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_structs = set(expected_structs) & created_classes
    assert len(found_structs) >= 2, (
        f"Expected at least 2 module structs, found: {sorted(list(found_structs))}"
    )


def test_rust_generics_and_lifetimes(
    rust_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Rust generics, lifetimes, and advanced type features."""
    test_file = rust_project / "generics.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic generic struct
struct Pair<T, U> {
    first: T,
    second: U,
}

// Generic with constraints
struct BoundedStruct<T: Clone + Send> {
    value: T,
}

// Generic with lifetime parameters
struct Reference<'a, T> {
    data: &'a T,
}

// Multiple lifetimes
struct TwoReferences<'a, 'b, T> {
    first: &'a T,
    second: &'b T,
}

// Generic enum
enum Result<T, E> {
    Ok(T),
    Err(E),
}

// Complex generic with associated types
trait Collect<T> {
    type Output;

    fn collect(self) -> Self::Output;
}

// Implementation with generics
impl<T, U> Pair<T, U> {
    fn new(first: T, second: U) -> Self {
        Pair { first, second }
    }

    fn get_first(&self) -> &T {
        &self.first
    }

    fn swap(self) -> Pair<U, T> {
        Pair {
            first: self.second,
            second: self.first,
        }
    }
}

// Generic implementation with constraints
impl<T: Clone + Send> BoundedStruct<T> {
    fn new(value: T) -> Self {
        BoundedStruct { value }
    }

    fn clone_value(&self) -> T {
        self.value.clone()
    }
}

// Implementation with lifetime parameters
impl<'a, T> Reference<'a, T> {
    fn new(data: &'a T) -> Self {
        Reference { data }
    }

    fn get(&self) -> &T {
        self.data
    }
}

// Generic function with multiple type parameters
fn generic_function<T, U, V>(t: T, u: U) -> V
where
    T: Into<V>,
    U: Clone,
    V: From<T>,
{
    t.into()
}

// Function with lifetime parameters
fn longest<'a>(x: &'a str, y: &'a str) -> &'a str {
    if x.len() > y.len() {
        x
    } else {
        y
    }
}

// Higher-ranked trait bounds
fn higher_ranked<F>(f: F) -> i32
where
    F: for<'a> Fn(&'a str) -> i32,
{
    f("test")
}

// Associated type projection
fn use_associated_type<I>(iter: I) -> I::Item
where
    I: Iterator,
    I::Item: Default,
{
    iter.next().unwrap_or_default()
}

// Complex where clause
fn complex_where_clause<T, U, F>(x: T, y: U, f: F) -> T
where
    T: Clone + std::fmt::Debug + PartialEq<U>,
    U: Into<String> + Clone,
    F: Fn(T, U) -> T + Send + Sync,
{
    if x == y {
        f(x.clone(), y)
    } else {
        x
    }
}

// Demonstrating generic usage
fn demonstrate_generics() {
    let pair = Pair::new(1, "hello");
    let first = pair.get_first();
    let swapped = pair.swap();

    let bounded = BoundedStruct::new(42);
    let cloned = bounded.clone_value();

    let value = 100;
    let reference = Reference::new(&value);
    let borrowed = reference.get();

    let s1 = "short";
    let s2 = "longer string";
    let longer = longest(s1, s2);

    let result = higher_ranked(|s| s.len() as i32);
}
""",
    )

    run_updater(rust_project, mock_ingestor)

    project_name = rust_project.name

    expected_structs = [
        f"{project_name}.generics.Pair",
        f"{project_name}.generics.BoundedStruct",
        f"{project_name}.generics.Reference",
        f"{project_name}.generics.TwoReferences",
        f"{project_name}.generics.Result",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_structs = set(expected_structs) & created_classes
    assert len(found_structs) >= 3, (
        f"Expected at least 3 generic structs, found: {sorted(list(found_structs))}"
    )

    expected_functions = [
        f"{project_name}.generics.generic_function",
        f"{project_name}.generics.longest",
        f"{project_name}.generics.higher_ranked",
        f"{project_name}.generics.use_associated_type",
        f"{project_name}.generics.complex_where_clause",
        f"{project_name}.generics.demonstrate_generics",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_functions = set(expected_functions) & created_functions
    assert len(found_functions) >= 4, (
        f"Expected at least 4 generic functions, found: {sorted(list(found_functions))}"
    )


def test_rust_pattern_matching(
    rust_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Rust pattern matching with match expressions and if let."""
    test_file = rust_project / "pattern_matching.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
enum Color {
    Red,
    Green,
    Blue,
    Rgb(u8, u8, u8),
    Hsl { h: u16, s: u8, l: u8 },
}

enum Message {
    Quit,
    Move { x: i32, y: i32 },
    Write(String),
    ChangeColor(Color),
}

struct Point {
    x: i32,
    y: i32,
}

// Basic match expression
fn match_color(color: Color) -> String {
    match color {
        Color::Red => "red".to_string(),
        Color::Green => "green".to_string(),
        Color::Blue => "blue".to_string(),
        Color::Rgb(r, g, b) => format!("rgb({}, {}, {})", r, g, b),
        Color::Hsl { h, s, l } => format!("hsl({}, {}, {})", h, s, l),
    }
}

// Match with guards
fn match_with_guards(x: i32) -> &'static str {
    match x {
        n if n < 0 => "negative",
        0 => "zero",
        1..=10 => "small positive",
        11..=100 => "medium positive",
        _ => "large positive",
    }
}

// Complex pattern matching
fn process_message(msg: Message) {
    match msg {
        Message::Quit => {
            println!("Quit message received");
            std::process::exit(0);
        },
        Message::Move { x, y } if x > 0 && y > 0 => {
            println!("Moving to positive coordinates: ({}, {})", x, y);
        },
        Message::Move { x, y } => {
            println!("Moving to: ({}, {})", x, y);
        },
        Message::Write(ref text) if text.len() > 10 => {
            println!("Long message: {}", text);
        },
        Message::Write(text) => {
            println!("Short message: {}", text);
        },
        Message::ChangeColor(Color::Red) => {
            println!("Changing to red");
        },
        Message::ChangeColor(Color::Rgb(r, g, b)) if r > 128 || g > 128 || b > 128 => {
            println!("Bright color: ({}, {}, {})", r, g, b);
        },
        Message::ChangeColor(color) => {
            println!("Changing color to: {}", match_color(color));
        },
    }
}

// If let patterns
fn if_let_examples(opt: Option<i32>, msg: Message) {
    // Basic if let
    if let Some(value) = opt {
        println!("Got value: {}", value);
    }

    // If let with else
    if let Some(x) = opt {
        println!("Value is {}", x);
    } else {
        println!("No value");
    }

    // If let with complex pattern
    if let Message::Move { x, y } = msg {
        println!("Moving to ({}, {})", x, y);
    } else if let Message::Write(text) = msg {
        println!("Writing: {}", text);
    }

    // While let
    let mut stack = vec![1, 2, 3, 4, 5];
    while let Some(value) = stack.pop() {
        println!("Popped: {}", value);
    }
}

// Pattern matching in function parameters
fn destructure_point((x, y): (i32, i32)) -> i32 {
    x + y
}

fn destructure_struct(Point { x, y }: Point) -> i32 {
    x * y
}

// Pattern matching with references
fn match_reference(point: &Point) {
    match point {
        Point { x: 0, y: 0 } => println!("Origin"),
        Point { x: 0, y } => println!("On Y axis at {}", y),
        Point { x, y: 0 } => println!("On X axis at {}", x),
        Point { x, y } => println!("Point at ({}, {})", x, y),
    }
}

// Nested pattern matching
fn nested_match(nested: Option<Result<i32, String>>) {
    match nested {
        Some(Ok(value)) if value > 0 => println!("Positive: {}", value),
        Some(Ok(value)) => println!("Non-positive: {}", value),
        Some(Err(error)) => println!("Error: {}", error),
        None => println!("No value"),
    }
}

// Demonstrating pattern matching
fn demonstrate_patterns() {
    let red = Color::Red;
    let rgb = Color::Rgb(255, 128, 0);
    let hsl = Color::Hsl { h: 240, s: 100, l: 50 };

    println!("{}", match_color(red));
    println!("{}", match_color(rgb));
    println!("{}", match_color(hsl));

    println!("{}", match_with_guards(-5));
    println!("{}", match_with_guards(5));
    println!("{}", match_with_guards(50));

    let msg = Message::Move { x: 10, y: 20 };
    process_message(msg);

    let opt_val = Some(42);
    let no_val: Option<i32> = None;
    if_let_examples(opt_val, Message::Write("test".to_string()));

    let tuple = (3, 4);
    let sum = destructure_point(tuple);

    let point = Point { x: 5, y: 6 };
    let product = destructure_struct(point);
    match_reference(&Point { x: 0, y: 0 });

    let nested = Some(Ok(42));
    nested_match(nested);
}
""",
    )

    run_updater(rust_project, mock_ingestor)

    project_name = rust_project.name

    expected_types = [
        f"{project_name}.pattern_matching.Color",
        f"{project_name}.pattern_matching.Message",
        f"{project_name}.pattern_matching.Point",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_types = set(expected_types) & created_classes
    assert len(found_types) >= 3, (
        f"Expected at least 3 types, found: {sorted(list(found_types))}"
    )

    expected_functions = [
        f"{project_name}.pattern_matching.match_color",
        f"{project_name}.pattern_matching.match_with_guards",
        f"{project_name}.pattern_matching.process_message",
        f"{project_name}.pattern_matching.if_let_examples",
        f"{project_name}.pattern_matching.destructure_point",
        f"{project_name}.pattern_matching.destructure_struct",
        f"{project_name}.pattern_matching.match_reference",
        f"{project_name}.pattern_matching.nested_match",
        f"{project_name}.pattern_matching.demonstrate_patterns",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_functions = set(expected_functions) & created_functions
    assert len(found_functions) >= 6, (
        f"Expected at least 6 pattern matching functions, found: {sorted(list(found_functions))}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    pattern_call_relationships = [
        call
        for call in call_relationships
        if "pattern_matching" in call.args[0][2]
        and any(
            func_name in call.args[2][2]
            for func_name in ["match_color", "process_message", "match_with_guards"]
        )
    ]

    assert len(pattern_call_relationships) >= 2, (
        f"Expected at least 2 pattern matching call relationships, found {len(pattern_call_relationships)}"
    )


def test_rust_closures_and_lambdas(
    rust_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Rust closures and functional programming features."""
    test_file = rust_project / "closures.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::thread;
use std::sync::Arc;

// Function demonstrating closures
fn demonstrate_closures() {
    let x = 10;

    // Basic closure
    let add_x = |y| x + y;
    let result1 = add_x(5);

    // Closure with explicit types
    let multiply: fn(i32, i32) -> i32 = |a, b| a * b;
    let result2 = multiply(3, 4);

    // Closure capturing by reference
    let mut count = 0;
    let mut increment = || {
        count += 1;
        count
    };
    let result3 = increment();
    let result4 = increment();

    // Closure capturing by value (move)
    let data = vec![1, 2, 3, 4, 5];
    let process_data = move || {
        data.iter().sum::<i32>()
    };
    let sum = process_data();

    // Higher-order functions
    let numbers = vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10];

    let evens: Vec<i32> = numbers
        .iter()
        .filter(|&&x| x % 2 == 0)
        .cloned()
        .collect();

    let doubled: Vec<i32> = numbers
        .iter()
        .map(|&x| x * 2)
        .collect();

    let sum_of_squares: i32 = numbers
        .iter()
        .map(|&x| x * x)
        .sum();

    // Closure as function parameter
    let custom_filter = |&x: &i32| x > 5;
    let filtered: Vec<i32> = numbers
        .iter()
        .filter(custom_filter)
        .cloned()
        .collect();
}

// Function taking closure as parameter
fn apply_twice<F>(f: F, x: i32) -> i32
where
    F: Fn(i32) -> i32,
{
    f(f(x))
}

fn apply_to_vec<F>(vec: Vec<i32>, f: F) -> Vec<i32>
where
    F: Fn(i32) -> i32,
{
    vec.into_iter().map(f).collect()
}

// Returning closures (boxed)
fn make_adder(n: i32) -> Box<dyn Fn(i32) -> i32> {
    Box::new(move |x| x + n)
}

fn make_multiplier() -> impl Fn(i32) -> i32 {
    |x| x * 2
}

// Complex closure scenarios
fn complex_closures() {
    // Closure with complex capture
    let data = vec![1, 2, 3, 4, 5];
    let threshold = 3;

    let processor = |filter_fn: fn(&i32) -> bool| {
        data.iter()
            .filter(|&&x| filter_fn(&x))
            .map(|&x| x * 2)
            .collect::<Vec<i32>>()
    };

    let greater_than_threshold = |&x: &i32| x > threshold;
    let result = processor(greater_than_threshold);

    // Nested closures
    let outer_value = 10;
    let outer_closure = |inner_value| {
        let inner_closure = |multiplier| inner_value * multiplier * outer_value;
        inner_closure(2)
    };
    let nested_result = outer_closure(5);

    // Closure in thread
    let shared_data = Arc::new(vec![1, 2, 3, 4, 5]);
    let shared_data_clone = Arc::clone(&shared_data);

    let handle = thread::spawn(move || {
        shared_data_clone.iter().sum::<i32>()
    });

    let thread_result = handle.join().unwrap();
}

// Using function pointers and closures
fn demonstrate_function_usage() {
    // Using closures with higher-order functions
    let square = |x: i32| x * x;
    let result1 = apply_twice(square, 3); // 3^4 = 81

    let increment = |x| x + 1;
    let numbers = vec![1, 2, 3, 4, 5];
    let incremented = apply_to_vec(numbers, increment);

    // Using closure factories
    let add_5 = make_adder(5);
    let result2 = add_5(10); // 15

    let double = make_multiplier();
    let result3 = double(7); // 14

    // Complex usage
    complex_closures();

    // Closure with different trait bounds
    let mut accumulator = 0;
    let mut add_to_accumulator = |x: i32| {
        accumulator += x;
        accumulator
    };

    let values = [1, 2, 3, 4, 5];
    for value in values.iter() {
        add_to_accumulator(*value);
    }

    // Iterator adaptors
    let text = "hello world rust programming";
    let word_lengths: Vec<usize> = text
        .split_whitespace()
        .map(|word| word.len())
        .filter(|&len| len > 4)
        .collect();

    let total_chars: usize = text
        .chars()
        .filter(|&c| c.is_alphabetic())
        .count();
}
""",
    )

    run_updater(rust_project, mock_ingestor)

    project_name = rust_project.name

    expected_functions = [
        f"{project_name}.closures.demonstrate_closures",
        f"{project_name}.closures.apply_twice",
        f"{project_name}.closures.apply_to_vec",
        f"{project_name}.closures.make_adder",
        f"{project_name}.closures.make_multiplier",
        f"{project_name}.closures.complex_closures",
        f"{project_name}.closures.demonstrate_function_usage",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_functions = set(expected_functions) & created_functions
    assert len(found_functions) >= 5, (
        f"Expected at least 5 closure functions, found: {sorted(list(found_functions))}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    closure_call_relationships = [
        call
        for call in call_relationships
        if "closures" in call.args[0][2]
        and any(
            func_name in call.args[2][2]
            for func_name in ["apply_twice", "apply_to_vec", "make_adder"]
        )
    ]

    assert len(closure_call_relationships) >= 2, (
        f"Expected at least 2 closure call relationships, found {len(closure_call_relationships)}"
    )


def test_rust_macros(
    rust_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Rust macro definitions and usage."""
    test_file = rust_project / "macros.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Declarative macros (macro_rules!)
macro_rules! say_hello {
    () => {
        println!("Hello, world!");
    };
}

macro_rules! create_function {
    ($func_name:ident) => {
        fn $func_name() {
            println!("You called {}!", stringify!($func_name));
        }
    };
}

macro_rules! print_result {
    ($expression:expr) => {
        println!("{} = {}", stringify!($expression), $expression);
    };
}

macro_rules! find_min {
    ($x:expr) => ($x);
    ($x:expr, $($y:expr),+) => (
        std::cmp::min($x, find_min!($($y),+))
    );
}

macro_rules! vec_of_strings {
    ($($element:expr),*) => {
        {
            let mut v = Vec::new();
            $(
                v.push($element.to_string());
            )*
            v
        }
    };
}

// Complex macro with patterns
macro_rules! hashmap {
    ($($key:expr => $value:expr),* $(,)?) => {
        {
            let mut map = std::collections::HashMap::new();
            $(
                map.insert($key, $value);
            )*
            map
        }
    };
}

// Macro generating struct
macro_rules! create_struct {
    ($name:ident { $($field:ident: $type:ty),* }) => {
        struct $name {
            $(
                $field: $type,
            )*
        }

        impl $name {
            fn new($($field: $type),*) -> Self {
                $name {
                    $(
                        $field,
                    )*
                }
            }
        }
    };
}

// Using the macros
create_function!(foo);
create_function!(bar);

create_struct!(Person {
    name: String,
    age: u32
});

create_struct!(Point {
    x: f64,
    y: f64
});

// Function demonstrating macro usage
fn demonstrate_macros() {
    // Basic macro call
    say_hello!();

    // Generated functions
    foo();
    bar();

    // Expression macro
    print_result!(1 + 2);
    print_result!(2 * 3);

    // Variadic macro
    let min = find_min!(5, 3, 8, 1, 9);
    println!("Minimum: {}", min);

    // Vector creation macro
    let strings = vec_of_strings!["hello", "world", "rust"];

    // HashMap creation macro
    let map = hashmap! {
        "name" => "John",
        "age" => "30",
        "city" => "New York"
    };

    // Using generated structs
    let person = Person::new("Alice".to_string(), 25);
    let point = Point::new(3.14, 2.71);

    // Built-in macros
    println!("Debug: {:?}", person);
    eprintln!("Error message");

    let formatted = format!("Person: {} is {} years old", person.name, person.age);
    println!("{}", formatted);

    // Assert macros
    assert_eq!(2 + 2, 4);
    assert_ne!(5, 3);
    assert!(true);

    // Debug assertions
    debug_assert!(min < 10);
    debug_assert_eq!(strings.len(), 3);

    // Panic macro
    // panic!("This would cause a panic");
}

// Attribute macros (procedural macros would be defined elsewhere)
#[derive(Debug, Clone, PartialEq)]
struct MacroStruct {
    value: i32,
    text: String,
}

#[derive(Debug)]
enum MacroEnum {
    Variant1,
    Variant2(i32),
    Variant3 { field: String },
}

// Conditional compilation macros
#[cfg(debug_assertions)]
fn debug_only_function() {
    println!("This only runs in debug builds");
}

#[cfg(not(debug_assertions))]
fn release_only_function() {
    println!("This only runs in release builds");
}

// Test macros
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_macro_functionality() {
        let person = Person::new("Test".to_string(), 20);
        assert_eq!(person.name, "Test");
        assert_eq!(person.age, 20);
    }

    #[test]
    fn test_find_min() {
        assert_eq!(find_min!(5, 3, 8), 3);
        assert_eq!(find_min!(1), 1);
    }
}
""",
    )

    run_updater(rust_project, mock_ingestor)

    project_name = rust_project.name

    expected_functions = [
        f"{project_name}.macros.foo",
        f"{project_name}.macros.bar",
        f"{project_name}.macros.demonstrate_macros",
        f"{project_name}.macros.debug_only_function",
        f"{project_name}.macros.release_only_function",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_functions = set(expected_functions) & created_functions
    assert len(found_functions) >= 2, (
        f"Expected at least 2 macro functions, found: {sorted(list(found_functions))}"
    )

    expected_structs = [
        f"{project_name}.macros.Person",
        f"{project_name}.macros.Point",
        f"{project_name}.macros.MacroStruct",
        f"{project_name}.macros.MacroEnum",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_structs = set(expected_structs) & created_classes
    assert len(found_structs) >= 2, (
        f"Expected at least 2 macro structs, found: {sorted(list(found_structs))}"
    )


def test_rust_imports_and_use_statements(
    rust_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Rust import system including use statements and external crates."""
    test_file = rust_project / "imports.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Standard library imports
use std::collections::HashMap;
use std::collections::HashSet;
use std::io::{self, Read, Write};
use std::fs::File;
use std::path::Path;

// Multiple imports from same module
use std::fmt::{Debug, Display, Formatter, Result as FmtResult};

// Glob imports
use std::prelude::*;
use std::collections::*;

// Aliased imports
use std::collections::HashMap as Map;
use std::vec::Vec as Vector;
use std::result::Result as StdResult;

// External crate imports
extern crate serde;
extern crate serde_json;
extern crate tokio;

use serde::{Serialize, Deserialize};
use serde_json::{Value, json};
use tokio::runtime::Runtime;

// Relative imports (within same crate)
use crate::utils::helper_function;
use crate::types::{Point, Color};

// Re-exports
pub use std::collections::HashMap;
pub use crate::utils::*;

// Conditional imports
#[cfg(feature = "networking")]
use std::net::{TcpListener, TcpStream};

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

#[cfg(windows)]
use std::os::windows::fs::MetadataExt;

// Module declarations
mod utils {
    pub fn helper_function() -> i32 {
        42
    }

    pub fn another_helper() -> String {
        "helper".to_string()
    }
}

mod types {
    #[derive(Debug, Clone)]
    pub struct Point {
        pub x: f64,
        pub y: f64,
    }

    #[derive(Debug, Clone)]
    pub enum Color {
        Red,
        Green,
        Blue,
    }
}

// Using imported types and functions
fn demonstrate_imports() {
    // Standard library usage
    let mut map: HashMap<String, i32> = HashMap::new();
    map.insert("key".to_string(), 42);

    let mut set = HashSet::new();
    set.insert("value");

    let file_path = Path::new("example.txt");

    // Aliased imports
    let aliased_map: Map<String, i32> = Map::new();
    let vector: Vector<i32> = Vector::new();

    // External crate usage
    let json_value = json!({
        "name": "John",
        "age": 30
    });

    let runtime = Runtime::new().unwrap();

    // Relative imports
    let helper_result = helper_function();
    let point = Point { x: 1.0, y: 2.0 };
    let color = Color::Red;

    // I/O operations using imported traits
    let mut buffer = String::new();
    // io::stdin().read_to_string(&mut buffer).unwrap();

    // Multiple trait usage
    println!("{:?}", point);  // Debug trait

    // Conditional usage
    #[cfg(feature = "networking")]
    {
        let listener = TcpListener::bind("127.0.0.1:8080").unwrap();
    }
}

// Complex import patterns
use std::{
    collections::{HashMap, HashSet, BTreeMap},
    io::{Error, ErrorKind, Result},
    sync::{Arc, Mutex, RwLock},
    thread,
    time::Duration,
};

// Macro imports
use std::println;
use std::format;
use std::vec;

// Using complex imports
fn complex_import_usage() {
    let shared_map: Arc<Mutex<HashMap<String, i32>>> = Arc::new(Mutex::new(HashMap::new()));
    let shared_map_clone = Arc::clone(&shared_map);

    let handle = thread::spawn(move || {
        let mut map = shared_map_clone.lock().unwrap();
        map.insert("thread_key".to_string(), 100);
    });

    handle.join().unwrap();

    thread::sleep(Duration::from_millis(100));

    let tree_map: BTreeMap<String, String> = BTreeMap::new();
    let rw_lock: RwLock<Vec<i32>> = RwLock::new(vec![1, 2, 3]);

    match rw_lock.read() {
        Ok(data) => println!("Data: {:?}", *data),
        Err(e) => eprintln!("Error: {:?}", e),
    }
}

// Trait imports and implementations
use std::fmt;

struct CustomStruct {
    value: i32,
}

impl fmt::Display for CustomStruct {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "CustomStruct({})", self.value)
    }
}

impl Debug for CustomStruct {
    fn fmt(&self, f: &mut Formatter<'_>) -> FmtResult {
        f.debug_struct("CustomStruct")
            .field("value", &self.value)
            .finish()
    }
}
""",
    )

    run_updater(rust_project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    rust_imports = [
        call for call in import_relationships if "imports" in call.args[0][2]
    ]

    assert len(rust_imports) >= 10, (
        f"Expected at least 10 import relationships, found {len(rust_imports)}"
    )

    imported_modules = [call.args[2][2] for call in rust_imports]

    expected_std_imports = [
        "std::collections",
        "std::io",
        "std::fs",
        "std::path",
        "std::fmt",
    ]

    found_std_imports = 0
    for expected in expected_std_imports:
        if any(expected in module for module in imported_modules):
            found_std_imports += 1

    assert found_std_imports >= 3, (
        f"Expected at least 3 std module imports, found {found_std_imports} matches"
    )

    expected_external_imports = [
        "serde",
        "serde_json",
        "tokio",
    ]

    found_external_imports = 0
    for expected in expected_external_imports:
        if any(expected in module for module in imported_modules):
            found_external_imports += 1

    assert found_external_imports >= 2, (
        f"Expected at least 2 external crate imports, found {found_external_imports} matches"
    )

    project_name = rust_project.name

    expected_functions = [
        f"{project_name}.imports.demonstrate_imports",
        f"{project_name}.imports.complex_import_usage",
        f"{project_name}.imports.utils.helper_function",
        f"{project_name}.imports.utils.another_helper",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_functions = set(expected_functions) & created_functions
    assert len(found_functions) >= 3, (
        f"Expected at least 3 import-related functions, found: {sorted(list(found_functions))}"
    )


def test_rust_error_handling(
    rust_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Rust error handling with Result, Option, and ? operator."""
    test_file = rust_project / "error_handling.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::fs::File;
use std::io::{self, Read, Write};
use std::num::ParseIntError;
use std::fmt;

// Custom error types
#[derive(Debug)]
enum CustomError {
    IoError(io::Error),
    ParseError(ParseIntError),
    ValidationError(String),
    NetworkError { code: u16, message: String },
}

impl fmt::Display for CustomError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CustomError::IoError(err) => write!(f, "I/O error: {}", err),
            CustomError::ParseError(err) => write!(f, "Parse error: {}", err),
            CustomError::ValidationError(msg) => write!(f, "Validation error: {}", msg),
            CustomError::NetworkError { code, message } => {
                write!(f, "Network error {}: {}", code, message)
            }
        }
    }
}

impl From<io::Error> for CustomError {
    fn from(error: io::Error) -> Self {
        CustomError::IoError(error)
    }
}

impl From<ParseIntError> for CustomError {
    fn from(error: ParseIntError) -> Self {
        CustomError::ParseError(error)
    }
}

// Basic Result handling
fn divide(a: f64, b: f64) -> Result<f64, String> {
    if b == 0.0 {
        Err("Division by zero".to_string())
    } else {
        Ok(a / b)
    }
}

fn parse_number(s: &str) -> Result<i32, ParseIntError> {
    s.parse::<i32>()
}

// Option handling
fn find_word(text: &str, word: &str) -> Option<usize> {
    text.find(word)
}

fn get_first_word(text: &str) -> Option<&str> {
    text.split_whitespace().next()
}

// Using ? operator
fn read_file_content(filename: &str) -> Result<String, io::Error> {
    let mut file = File::open(filename)?;
    let mut contents = String::new();
    file.read_to_string(&mut contents)?;
    Ok(contents)
}

fn process_numbers(input: &str) -> Result<Vec<i32>, ParseIntError> {
    input
        .split_whitespace()
        .map(|s| s.parse::<i32>())
        .collect()
}

fn validate_and_parse(input: &str) -> Result<i32, CustomError> {
    if input.is_empty() {
        return Err(CustomError::ValidationError("Input cannot be empty".to_string()));
    }

    if input.len() > 10 {
        return Err(CustomError::ValidationError("Input too long".to_string()));
    }

    let number = input.parse::<i32>()?;

    if number < 0 {
        return Err(CustomError::ValidationError("Number must be positive".to_string()));
    }

    Ok(number)
}

// Complex error handling patterns
fn complex_operation() -> Result<String, CustomError> {
    let content = read_file_content("data.txt")?;
    let numbers = process_numbers(&content)?;

    let sum: i32 = numbers.iter().sum();

    if sum > 1000 {
        return Err(CustomError::ValidationError("Sum too large".to_string()));
    }

    Ok(format!("Sum: {}", sum))
}

// Option chaining and combinators
fn option_combinators() -> Option<String> {
    let text = "hello world rust programming";

    find_word(text, "rust")?; // Early return if not found

    let first_word = get_first_word(text)?;
    let word_length = first_word.len();

    if word_length > 3 {
        Some(first_word.to_uppercase())
    } else {
        None
    }
}

// Result combinators
fn result_combinators(input: &str) -> Result<i32, String> {
    parse_number(input)
        .map(|n| n * 2)
        .map_err(|e| format!("Failed to parse: {}", e))
        .and_then(|n| {
            if n > 100 {
                Err("Number too large after doubling".to_string())
            } else {
                Ok(n)
            }
        })
}

// Pattern matching on Results and Options
fn handle_results_and_options() {
    let result = divide(10.0, 2.0);
    match result {
        Ok(value) => println!("Result: {}", value),
        Err(error) => println!("Error: {}", error),
    }

    let option = find_word("hello world", "world");
    match option {
        Some(index) => println!("Found at index: {}", index),
        None => println!("Not found"),
    }

    // Using if let
    if let Ok(content) = read_file_content("test.txt") {
        println!("File content: {}", content);
    }

    if let Some(word) = get_first_word("rust programming") {
        println!("First word: {}", word);
    }

    // Nested Result/Option handling
    let nested_result: Result<Option<i32>, String> = Ok(Some(42));
    match nested_result {
        Ok(Some(value)) => println!("Value: {}", value),
        Ok(None) => println!("No value"),
        Err(error) => println!("Error: {}", error),
    }
}

// Demonstrating error handling patterns
fn demonstrate_error_handling() {
    // Basic Result handling
    match divide(10.0, 2.0) {
        Ok(result) => println!("10 / 2 = {}", result),
        Err(err) => println!("Error: {}", err),
    }

    match divide(10.0, 0.0) {
        Ok(result) => println!("10 / 0 = {}", result),
        Err(err) => println!("Error: {}", err),
    }

    // Option handling
    if let Some(index) = find_word("hello world", "world") {
        println!("Found 'world' at index {}", index);
    }

    // Using ? operator results
    match validate_and_parse("42") {
        Ok(number) => println!("Valid number: {}", number),
        Err(error) => println!("Validation error: {}", error),
    }

    match validate_and_parse("-5") {
        Ok(number) => println!("Valid number: {}", number),
        Err(error) => println!("Validation error: {}", error),
    }

    // Combinator results
    match result_combinators("25") {
        Ok(doubled) => println!("Doubled: {}", doubled),
        Err(error) => println!("Error: {}", error),
    }

    // Option chaining
    if let Some(result) = option_combinators() {
        println!("Option result: {}", result);
    }

    // Complex pattern matching
    handle_results_and_options();
}
""",
    )

    run_updater(rust_project, mock_ingestor)

    project_name = rust_project.name

    expected_functions = [
        f"{project_name}.error_handling.divide",
        f"{project_name}.error_handling.parse_number",
        f"{project_name}.error_handling.find_word",
        f"{project_name}.error_handling.get_first_word",
        f"{project_name}.error_handling.read_file_content",
        f"{project_name}.error_handling.process_numbers",
        f"{project_name}.error_handling.validate_and_parse",
        f"{project_name}.error_handling.complex_operation",
        f"{project_name}.error_handling.option_combinators",
        f"{project_name}.error_handling.result_combinators",
        f"{project_name}.error_handling.handle_results_and_options",
        f"{project_name}.error_handling.demonstrate_error_handling",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_functions = set(expected_functions) & created_functions
    assert len(found_functions) >= 8, (
        f"Expected at least 8 error handling functions, found: {sorted(list(found_functions))}"
    )

    expected_enums = [
        f"{project_name}.error_handling.CustomError",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_enums = set(expected_enums) & created_classes
    assert len(found_enums) >= 1, (
        f"Expected at least 1 custom error enum, found: {sorted(list(found_enums))}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    error_call_relationships = [
        call
        for call in call_relationships
        if "error_handling" in call.args[0][2]
        and any(
            func_name in call.args[2][2]
            for func_name in ["divide", "validate_and_parse", "find_word"]
        )
    ]

    assert len(error_call_relationships) >= 2, (
        f"Expected at least 2 error handling call relationships, found {len(error_call_relationships)}"
    )


def test_rust_comprehensive_integration(
    rust_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive integration test combining all Rust language features."""
    test_file = rust_project / "comprehensive.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// All Rust features in one integration test
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::thread;
use std::fmt::{self, Display, Debug};

// Traits with associated types and generics
trait Repository<T> {
    type Error;
    type Id;

    fn save(&mut self, item: T) -> Result<Self::Id, Self::Error>;
    fn find(&self, id: Self::Id) -> Result<Option<T>, Self::Error>;
    fn delete(&mut self, id: Self::Id) -> Result<bool, Self::Error>;
}

// Generic struct with lifetimes
#[derive(Debug, Clone)]
struct User<'a> {
    id: u32,
    name: &'a str,
    email: String,
    active: bool,
}

// Error enum with complex variants
#[derive(Debug)]
enum RepositoryError {
    NotFound(u32),
    DatabaseError { code: i32, message: String },
    ValidationError(Vec<String>),
    NetworkTimeout,
}

impl Display for RepositoryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RepositoryError::NotFound(id) => write!(f, "User {} not found", id),
            RepositoryError::DatabaseError { code, message } => {
                write!(f, "Database error {}: {}", code, message)
            }
            RepositoryError::ValidationError(errors) => {
                write!(f, "Validation errors: {}", errors.join(", "))
            }
            RepositoryError::NetworkTimeout => write!(f, "Network timeout"),
        }
    }
}

// Implementation with complex generics and lifetimes
struct UserRepository<'a> {
    users: HashMap<u32, User<'a>>,
    next_id: u32,
}

impl<'a> UserRepository<'a> {
    fn new() -> Self {
        UserRepository {
            users: HashMap::new(),
            next_id: 1,
        }
    }

    fn validate_user(&self, user: &User<'a>) -> Result<(), RepositoryError> {
        let mut errors = Vec::new();

        if user.name.is_empty() {
            errors.push("Name cannot be empty".to_string());
        }

        if !user.email.contains('@') {
            errors.push("Invalid email format".to_string());
        }

        if errors.is_empty() {
            Ok(())
        } else {
            Err(RepositoryError::ValidationError(errors))
        }
    }
}

impl<'a> Repository<User<'a>> for UserRepository<'a> {
    type Error = RepositoryError;
    type Id = u32;

    fn save(&mut self, user: User<'a>) -> Result<Self::Id, Self::Error> {
        self.validate_user(&user)?;

        let id = if user.id == 0 {
            let new_id = self.next_id;
            self.next_id += 1;
            new_id
        } else {
            user.id
        };

        let mut user_with_id = user;
        user_with_id.id = id;
        self.users.insert(id, user_with_id);

        Ok(id)
    }

    fn find(&self, id: Self::Id) -> Result<Option<User<'a>>, Self::Error> {
        Ok(self.users.get(&id).cloned())
    }

    fn delete(&mut self, id: Self::Id) -> Result<bool, Self::Error> {
        Ok(self.users.remove(&id).is_some())
    }
}

// Async function with complex error handling
async fn process_users_async<'a>(
    repository: Arc<Mutex<UserRepository<'a>>>,
    users: Vec<User<'a>>,
) -> Result<Vec<u32>, RepositoryError> {
    let mut ids = Vec::new();

    for user in users {
        let mut repo = repository.lock().unwrap();
        let id = repo.save(user)?;
        ids.push(id);
    }

    Ok(ids)
}

// Complex closure with pattern matching
fn filter_and_transform_users<'a, F, P>(
    users: Vec<User<'a>>,
    filter: F,
    transform: P,
) -> Vec<String>
where
    F: Fn(&User<'a>) -> bool,
    P: Fn(&User<'a>) -> String,
{
    users
        .iter()
        .filter(|user| filter(user))
        .map(|user| transform(user))
        .collect()
}

// Macro for creating test users
macro_rules! create_user {
    ($name:expr, $email:expr) => {
        User {
            id: 0,
            name: $name,
            email: $email.to_string(),
            active: true,
        }
    };
    ($id:expr, $name:expr, $email:expr, $active:expr) => {
        User {
            id: $id,
            name: $name,
            email: $email.to_string(),
            active: $active,
        }
    };
}

// Generic function with complex constraints
fn aggregate_data<T, F, R>(data: Vec<T>, aggregator: F) -> Option<R>
where
    T: Clone + Debug,
    F: Fn(Vec<T>) -> R,
    R: Display + Debug,
{
    if data.is_empty() {
        None
    } else {
        let result = aggregator(data);
        println!("Aggregation result: {}", result);
        Some(result)
    }
}

// Pattern matching with guards and complex destructuring
fn analyze_user_pattern(user: &User) -> String {
    match user {
        User { active: false, .. } => "Inactive user".to_string(),
        User { id, name, active: true, .. } if *id > 1000 => {
            format!("Premium user: {}", name)
        }
        User { name, email, .. } if email.ends_with(".com") => {
            format!("Commercial user: {}", name)
        }
        User { name, .. } if name.len() > 10 => {
            format!("User with long name: {}", name)
        }
        User { name, .. } => format!("Regular user: {}", name),
    }
}

// Demonstration function using all features
fn demonstrate_comprehensive_rust() {
    // Create users with macro
    let user1 = create_user!("Alice", "alice@example.com");
    let user2 = create_user!(1001, "Bob", "bob@company.com", true);
    let user3 = create_user!("Charlie", "charlie@test.org");

    // Repository operations with error handling
    let mut repository = UserRepository::new();

    match repository.save(user1.clone()) {
        Ok(id) => println!("Saved user with ID: {}", id),
        Err(error) => println!("Error saving user: {}", error),
    }

    // Pattern matching on results
    let save_results: Vec<Result<u32, RepositoryError>> = vec![
        repository.save(user2.clone()),
        repository.save(user3.clone()),
    ];

    for (i, result) in save_results.into_iter().enumerate() {
        match result {
            Ok(id) => println!("User {} saved with ID: {}", i + 2, id),
            Err(error) => println!("Error saving user {}: {}", i + 2, error),
        }
    }

    // Closures with complex logic
    let active_filter = |user: &User| user.active && !user.email.is_empty();
    let name_transformer = |user: &User| format!("User: {}", user.name.to_uppercase());

    let users = vec![user1.clone(), user2.clone(), user3.clone()];
    let transformed = filter_and_transform_users(users.clone(), active_filter, name_transformer);

    for transformed_user in transformed {
        println!("{}", transformed_user);
    }

    // Pattern analysis
    for user in &users {
        println!("{}", analyze_user_pattern(user));
    }

    // Generic aggregation
    let user_count_aggregator = |users: Vec<User>| users.len();
    if let Some(count) = aggregate_data(users, user_count_aggregator) {
        println!("Total users processed: {}", count);
    }

    // Thread-safe operations
    let shared_repo = Arc::new(Mutex::new(repository));
    let shared_repo_clone = Arc::clone(&shared_repo);

    let handle = thread::spawn(move || {
        let new_user = create_user!("David", "david@example.com");
        let mut repo = shared_repo_clone.lock().unwrap();
        repo.save(new_user).unwrap_or_else(|e| {
            eprintln!("Thread error: {}", e);
            0
        })
    });

    let thread_result = handle.join().unwrap();
    println!("Thread saved user with ID: {}", thread_result);

    // Complex Option/Result chaining
    let repo_guard = shared_repo.lock().unwrap();
    let user_search = repo_guard.find(1)
        .map_err(|e| format!("Search error: {}", e))
        .and_then(|opt| opt.ok_or_else(|| "User not found".to_string()));

    match user_search {
        Ok(user) => println!("Found user: {:?}", user),
        Err(error) => println!("Search failed: {}", error),
    }
}
""",
    )

    run_updater(rust_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")
    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    comprehensive_calls = [
        call for call in call_relationships if "comprehensive" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 5, (
        f"Expected at least 5 comprehensive function calls, found {len(comprehensive_calls)}"
    )

    comprehensive_imports = [
        call for call in import_relationships if "comprehensive" in call.args[0][2]
    ]

    assert len(comprehensive_imports) >= 3, (
        f"Expected at least 3 imports, found {len(comprehensive_imports)}"
    )

    for relationship in comprehensive_calls:
        assert len(relationship.args) == 3, "Call relationship should have 3 args"
        assert relationship.args[1] == "CALLS", "Second arg should be 'CALLS'"

        source_module = relationship.args[0][2]
        target_module = relationship.args[2][2]

        assert "comprehensive" in source_module, (
            f"Source module should contain test file name: {source_module}"
        )

        assert isinstance(target_module, str) and target_module, (
            f"Target should be non-empty string: {target_module}"
        )

    assert defines_relationships, "Should still have DEFINES relationships"

    project_name = rust_project.name

    expected_types = [
        f"{project_name}.comprehensive.User",
        f"{project_name}.comprehensive.RepositoryError",
        f"{project_name}.comprehensive.UserRepository",
        f"{project_name}.comprehensive.Repository",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_types = set(expected_types) & created_classes
    assert len(found_types) >= 3, (
        f"Expected at least 3 comprehensive types, found: {sorted(list(found_types))}"
    )


def test_rust_advanced_edge_cases(
    rust_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test advanced Rust edge cases including complex lifetimes, generics, FFI, async, and more."""
    test_file = rust_project / "advanced_edge_cases.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::marker::PhantomData;
use std::pin::Pin;
use std::future::Future;
use std::task::{Context, Poll};
use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_int};
use std::mem;

// 1. Complex Lifetime Scenarios

// Higher-ranked trait bounds (for<'a>)
fn apply_closure<F>(f: F) -> i32
where
    F: for<'a> Fn(&'a str) -> i32,
{
    f("test string")
}

// Multiple lifetime parameters with constraints
struct ComplexLifetimes<'a, 'b, T>
where
    'a: 'b,
    T: 'a,
{
    first: &'a T,
    second: &'b str,
}

// Lifetime elision in complex function signatures
fn complex_lifetime_function<'a>(
    x: &'a str,
    y: &str,
) -> impl Iterator<Item = char> + 'a {
    x.chars().chain(y.chars())
}

// Associated types with lifetime bounds
trait AssociatedLifetime<'a> {
    type Output: 'a;

    fn process(&self, input: &'a str) -> Self::Output;
}

impl<'a> AssociatedLifetime<'a> for String {
    type Output = &'a str;

    fn process(&self, input: &'a str) -> Self::Output {
        input
    }
}

// 2. Advanced Generic Constraints

// Where clauses with multiple bounds
fn multi_bound_function<T, U, V>(a: T, b: U, c: V) -> String
where
    T: Clone + std::fmt::Debug + Send + Sync + 'static,
    U: Into<String> + std::fmt::Display,
    V: Iterator<Item = i32> + Clone,
{
    format!("{:?} {} {:?}", a, b.into(), c.collect::<Vec<_>>())
}

// Associated type projections (Iterator<Item = T>)
fn collect_items<I, T>(iter: I) -> Vec<T>
where
    I: Iterator<Item = T>,
    T: Clone + std::fmt::Debug,
{
    iter.collect()
}

// Generic constraints on impl blocks
struct ConstrainedStruct<T> {
    value: T,
}

impl<T> ConstrainedStruct<T>
where
    T: Clone + std::fmt::Debug + PartialEq + Send + Sync,
{
    fn new(value: T) -> Self {
        Self { value }
    }

    fn compare_and_clone(&self, other: &T) -> Option<T> {
        if self.value == *other {
            Some(self.value.clone())
        } else {
            None
        }
    }
}

// Phantom types and zero-sized types
struct PhantomStruct<T> {
    _phantom: PhantomData<T>,
    data: Vec<u8>,
}

impl<T> PhantomStruct<T> {
    fn new() -> Self {
        Self {
            _phantom: PhantomData,
            data: Vec::new(),
        }
    }
}

// 3. Macro Edge Cases

// Declarative macros with complex patterns
macro_rules! complex_match {
    ($(($key:expr, $value:expr)),* $(,)?) => {
        {
            let mut map = std::collections::HashMap::new();
            $(
                map.insert($key, $value);
            )*
            map
        }
    };
    ($single:expr) => {
        vec![$single]
    };
    (nested { $($inner:tt)* }) => {
        complex_match!($($inner)*)
    };
}

// Macro-generated structs and functions
macro_rules! generate_processor {
    ($name:ident, $input_type:ty, $output_type:ty, $process_fn:expr) => {
        struct $name;

        impl $name {
            fn process(input: $input_type) -> $output_type {
                $process_fn(input)
            }
        }
    };
}

generate_processor!(StringProcessor, String, usize, |s: String| s.len());
generate_processor!(NumberProcessor, i32, String, |n: i32| format!("Number: {}", n));

// Nested macro invocations
macro_rules! create_nested {
    ($outer_name:ident { $($inner_name:ident: $inner_type:ty),* }) => {
        struct $outer_name {
            $(
                $inner_name: $inner_type,
            )*
        }

        impl $outer_name {
            fn new($($inner_name: $inner_type),*) -> Self {
                Self { $($inner_name),* }
            }
        }
    };
}

create_nested!(NestedStruct {
    id: u32,
    name: String,
    active: bool
});

// Conditional compilation with cfg macros
#[cfg(feature = "advanced")]
macro_rules! conditional_macro {
    ($expr:expr) => {
        println!("Advanced feature: {}", $expr);
    };
}

#[cfg(not(feature = "advanced"))]
macro_rules! conditional_macro {
    ($expr:expr) => {
        println!("Basic feature: {}", $expr);
    };
}

// 4. Async/Await Advanced Features

// Async closures and async blocks
async fn async_closure_example() -> i32 {
    let async_closure = async || {
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        42
    };

    let async_block = async {
        let result = async_closure().await;
        result * 2
    };

    async_block.await
}

// Complex Future combinations
struct ComplexFuture<T> {
    state: Option<T>,
}

impl<T> Future for ComplexFuture<T>
where
    T: Clone + Send + 'static,
{
    type Output = T;

    fn poll(mut self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Self::Output> {
        if let Some(value) = self.state.take() {
            Poll::Ready(value)
        } else {
            Poll::Pending
        }
    }
}

// Async trait methods (using async-trait pattern)
trait AsyncProcessor {
    fn process_async<'a>(&'a self, input: &'a str) -> Pin<Box<dyn Future<Output = String> + Send + 'a>>;
}

struct AsyncStringProcessor;

impl AsyncProcessor for AsyncStringProcessor {
    fn process_async<'a>(&'a self, input: &'a str) -> Pin<Box<dyn Future<Output = String> + Send + 'a>> {
        Box::pin(async move {
            tokio::time::sleep(std::time::Duration::from_millis(1)).await;
            input.to_uppercase()
        })
    }
}

// Stream and AsyncIterator patterns
async fn stream_processing<S>(mut stream: S) -> Vec<String>
where
    S: futures::Stream<Item = String> + Unpin,
{
    use futures::StreamExt;

    let mut results = Vec::new();
    while let Some(item) = stream.next().await {
        results.push(format!("Processed: {}", item));
    }
    results
}

// 5. FFI and Unsafe Code

// extern "C" function declarations
extern "C" {
    fn strlen(s: *const c_char) -> c_int;
    fn malloc(size: usize) -> *mut std::ffi::c_void;
    fn free(ptr: *mut std::ffi::c_void);
}

// Unsafe trait implementations
unsafe trait UnsafeTrait {
    unsafe fn unsafe_method(&self) -> i32;
}

struct UnsafeStruct {
    ptr: *mut i32,
}

unsafe impl UnsafeTrait for UnsafeStruct {
    unsafe fn unsafe_method(&self) -> i32 {
        if !self.ptr.is_null() {
            *self.ptr
        } else {
            0
        }
    }
}

// Raw pointer manipulation
unsafe fn raw_pointer_operations() -> i32 {
    let value = 42;
    let ptr = &value as *const i32;
    let mut_ptr = ptr as *mut i32;

    // Unsafe dereferencing
    let deref_value = *ptr;

    // Memory allocation
    let allocated = malloc(mem::size_of::<i32>()) as *mut i32;
    if !allocated.is_null() {
        *allocated = 100;
        let result = *allocated;
        free(allocated as *mut std::ffi::c_void);
        result
    } else {
        deref_value
    }
}

// Memory layout attributes (#[repr(C)])
#[repr(C)]
struct CCompatibleStruct {
    a: u32,
    b: u16,
    c: u8,
}

#[repr(C, packed)]
struct PackedStruct {
    a: u32,
    b: u8,
}

// 6. Advanced Pattern Matching

// Complex nested destructuring
enum ComplexEnum {
    Variant1 {
        inner: Box<ComplexEnum>,
        metadata: (String, i32),
    },
    Variant2(Option<Vec<String>>),
    Variant3 {
        data: std::collections::HashMap<String, i32>,
    },
}

fn complex_destructuring(value: ComplexEnum) -> String {
    match value {
        ComplexEnum::Variant1 {
            inner: box ComplexEnum::Variant2(Some(ref strings)),
            metadata: (ref name, count),
        } => {
            format!("Nested variant with {} strings for {}, count: {}",
                    strings.len(), name, count)
        },
        ComplexEnum::Variant1 {
            inner: box nested,
            metadata: (name, count),
        } => {
            format!("Nested variant {}, count: {}", name, count)
        },
        ComplexEnum::Variant2(Some(strings)) => {
            format!("Strings: {:?}", strings)
        },
        ComplexEnum::Variant2(None) => "No strings".to_string(),
        ComplexEnum::Variant3 { data } if data.len() > 5 => {
            "Large data set".to_string()
        },
        ComplexEnum::Variant3 { data } => {
            format!("Data keys: {:?}", data.keys().collect::<Vec<_>>())
        },
    }
}

// Guard clauses with multiple conditions
fn multi_guard_pattern(x: i32, y: i32, z: Option<String>) -> &'static str {
    match (x, y, z) {
        (a, b, Some(ref s)) if a > 0 && b > 0 && s.len() > 5 => "Complex positive with long string",
        (a, b, Some(_)) if a > 0 && b > 0 => "Simple positive with string",
        (a, b, None) if a.abs() + b.abs() > 100 => "Large magnitude without string",
        (0, 0, _) => "Origin point",
        _ => "Default case",
    }
}

// Pattern matching in function parameters
fn destructure_in_params((x, y): (i32, i32), ComplexStruct { field1, field2 }: ComplexStruct) -> i32 {
    x + y + field1 + field2 as i32
}

struct ComplexStruct {
    field1: i32,
    field2: u8,
}

// Or-patterns and range patterns
fn or_and_range_patterns(value: char) -> &'static str {
    match value {
        'a'..='z' | 'A'..='Z' => "Letter",
        '0'..='9' => "Digit",
        ' ' | '\t' | '\n' | '\r' => "Whitespace",
        '!'..='/' | ':'..='@' | '['..='`' | '{'..='~' => "Punctuation",
        _ => "Other",
    }
}

// 7. Const Generics and Compile-time Evaluation

// Const generic parameters
struct FixedArray<T, const N: usize> {
    data: [T; N],
}

impl<T, const N: usize> FixedArray<T, N>
where
    T: Default + Copy,
{
    fn new() -> Self {
        Self {
            data: [T::default(); N],
        }
    }

    fn len(&self) -> usize {
        N
    }
}

// Const evaluation in type positions
const MAX_SIZE: usize = 1024;
type LargeArray = FixedArray<i32, MAX_SIZE>;
type SmallArray<T> = FixedArray<T, 16>;

// Complex const expressions
const fn complex_const_fn(n: usize) -> usize {
    if n == 0 {
        1
    } else {
        n * complex_const_fn(n - 1)
    }
}

const FACTORIAL_5: usize = complex_const_fn(5);

// 8. Advanced Trait System

// Higher-ranked trait bounds in trait definitions
trait HigherRankedTrait {
    fn apply<F>(&self, f: F) -> i32
    where
        F: for<'a> Fn(&'a str) -> i32;
}

struct HigherRankedImpl;

impl HigherRankedTrait for HigherRankedImpl {
    fn apply<F>(&self, f: F) -> i32
    where
        F: for<'a> Fn(&'a str) -> i32,
    {
        f("test")
    }
}

// Object safety considerations
trait ObjectSafeTrait {
    fn safe_method(&self) -> i32;
}

trait NotObjectSafeTrait {
    fn generic_method<T>(&self, value: T) -> T; // Makes trait not object-safe
}

// Associated const definitions
trait MathConstants {
    const PI: f64;
    const E: f64 = 2.718281828;

    fn calculate_circle_area(&self, radius: f64) -> f64 {
        Self::PI * radius * radius
    }
}

struct MathImpl;

impl MathConstants for MathImpl {
    const PI: f64 = 3.141592653589793;
}

// Default implementations with complex bounds
trait ComplexDefault<T>
where
    T: Clone + std::fmt::Debug + Send + Sync,
{
    fn default_behavior(&self, value: T) -> String {
        format!("Default: {:?}", value)
    }

    fn required_method(&self, value: T) -> T;

    fn chained_method(&self, value: T) -> String
    where
        T: std::fmt::Display,
    {
        let processed = self.required_method(value.clone());
        format!("{} -> {}", value, self.default_behavior(processed))
    }
}

// 9. Module System Edge Cases

// Complex re-export chains (pub use)
pub mod inner {
    pub mod deep {
        pub struct DeepStruct {
            pub value: i32,
        }

        pub fn deep_function() -> i32 {
            42
        }
    }

    pub use deep::{DeepStruct, deep_function};

    pub fn inner_function() -> DeepStruct {
        DeepStruct { value: deep_function() }
    }
}

pub use inner::{DeepStruct as ExportedStruct, deep_function as exported_fn};

// Conditional imports with cfg
#[cfg(feature = "networking")]
use std::net::TcpStream;

#[cfg(all(unix, feature = "filesystem"))]
use std::os::unix::fs::PermissionsExt;

// Module aliasing
use std::collections::HashMap as Map;
use std::sync::Arc as SharedPointer;

// extern crate with renaming
extern crate serde as serialization;
extern crate tokio as async_runtime;

// 10. Error Handling Patterns

// Custom Error types with complex From implementations
#[derive(Debug)]
enum ComplexError {
    Io(std::io::Error),
    Parse(std::num::ParseIntError),
    Validation { field: String, reason: String },
    Multiple(Vec<ComplexError>),
    Chained {
        source: Box<ComplexError>,
        context: String,
    },
}

impl std::fmt::Display for ComplexError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ComplexError::Io(e) => write!(f, "IO error: {}", e),
            ComplexError::Parse(e) => write!(f, "Parse error: {}", e),
            ComplexError::Validation { field, reason } => {
                write!(f, "Validation error in {}: {}", field, reason)
            },
            ComplexError::Multiple(errors) => {
                write!(f, "Multiple errors: ")?;
                for (i, error) in errors.iter().enumerate() {
                    if i > 0 { write!(f, ", ")?; }
                    write!(f, "{}", error)?;
                }
                Ok(())
            },
            ComplexError::Chained { source, context } => {
                write!(f, "{}: {}", context, source)
            },
        }
    }
}

impl From<std::io::Error> for ComplexError {
    fn from(error: std::io::Error) -> Self {
        ComplexError::Io(error)
    }
}

impl From<std::num::ParseIntError> for ComplexError {
    fn from(error: std::num::ParseIntError) -> Self {
        ComplexError::Parse(error)
    }
}

// Question mark operator in complex contexts
fn complex_question_mark() -> Result<String, ComplexError> {
    let content = std::fs::read_to_string("config.txt")?;
    let number: i32 = content.trim().parse()?;

    if number < 0 {
        return Err(ComplexError::Validation {
            field: "number".to_string(),
            reason: "must be positive".to_string(),
        });
    }

    Ok(format!("Processed: {}", number))
}

// Result and Option combinators
fn complex_combinators(input: &str) -> Result<Option<String>, ComplexError> {
    if input.is_empty() {
        return Ok(None);
    }

    input.parse::<i32>()
        .map_err(ComplexError::from)
        .and_then(|n| {
            if n > 100 {
                Err(ComplexError::Validation {
                    field: "input".to_string(),
                    reason: "too large".to_string(),
                })
            } else {
                Ok(Some(format!("Valid: {}", n)))
            }
        })
}

// Error recovery patterns
fn error_recovery_patterns() -> Result<Vec<i32>, ComplexError> {
    let inputs = vec!["1", "invalid", "3", "4", "bad"];
    let mut results = Vec::new();
    let mut errors = Vec::new();

    for input in inputs {
        match input.parse::<i32>() {
            Ok(n) => results.push(n),
            Err(e) => errors.push(ComplexError::from(e)),
        }
    }

    if errors.is_empty() {
        Ok(results)
    } else if results.is_empty() {
        Err(ComplexError::Multiple(errors))
    } else {
        // Partial success - continue with valid results
        eprintln!("Some inputs failed to parse, continuing with {} valid results", results.len());
        Ok(results)
    }
}

// Comprehensive demonstration function
fn demonstrate_advanced_edge_cases() -> Result<String, ComplexError> {
    // Test complex lifetimes
    let processor = String::from("test");
    let result = processor.process("input string");

    // Test advanced generics
    let constrained = ConstrainedStruct::new(42);
    let comparison = constrained.compare_and_clone(&42);

    // Test phantom types
    let phantom: PhantomStruct<String> = PhantomStruct::new();

    // Test macro usage
    let map = complex_match![
        ("key1", 1),
        ("key2", 2),
        ("key3", 3),
    ];

    let processor_result = StringProcessor::process("test string".to_string());

    // Test pattern matching
    let complex_val = ComplexEnum::Variant2(Some(vec!["test".to_string()]));
    let pattern_result = complex_destructuring(complex_val);

    // Test const generics
    let fixed_array: FixedArray<i32, 10> = FixedArray::new();
    let array_len = fixed_array.len();

    // Test FFI (unsafe)
    let unsafe_result = unsafe { raw_pointer_operations() };

    // Test error handling
    let recovery_result = error_recovery_patterns()?;

    // Test combinators
    let combinator_result = complex_combinators("50")?;

    Ok(format!(
        "Advanced edge cases tested: pattern={}, array_len={}, unsafe={}, recovery_count={}, combinator={:?}",
        pattern_result, array_len, unsafe_result, recovery_result.len(), combinator_result
    ))
}

// Test async functionality
async fn test_async_features() -> Result<String, ComplexError> {
    let result = async_closure_example().await;
    let processor = AsyncStringProcessor;
    let processed = processor.process_async("test input").await;

    Ok(format!("Async result: {}, processed: {}", result, processed))
}
""",
    )

    run_updater(rust_project, mock_ingestor)

    project_name = rust_project.name

    expected_functions = [
        f"{project_name}.advanced_edge_cases.apply_closure",
        f"{project_name}.advanced_edge_cases.complex_lifetime_function",
        f"{project_name}.advanced_edge_cases.multi_bound_function",
        f"{project_name}.advanced_edge_cases.collect_items",
        f"{project_name}.advanced_edge_cases.async_closure_example",
        f"{project_name}.advanced_edge_cases.stream_processing",
        f"{project_name}.advanced_edge_cases.raw_pointer_operations",
        f"{project_name}.advanced_edge_cases.complex_destructuring",
        f"{project_name}.advanced_edge_cases.multi_guard_pattern",
        f"{project_name}.advanced_edge_cases.destructure_in_params",
        f"{project_name}.advanced_edge_cases.or_and_range_patterns",
        f"{project_name}.advanced_edge_cases.complex_const_fn",
        f"{project_name}.advanced_edge_cases.complex_question_mark",
        f"{project_name}.advanced_edge_cases.complex_combinators",
        f"{project_name}.advanced_edge_cases.error_recovery_patterns",
        f"{project_name}.advanced_edge_cases.demonstrate_advanced_edge_cases",
        f"{project_name}.advanced_edge_cases.test_async_features",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    found_functions = set(expected_functions) & created_functions
    assert len(found_functions) >= 10, (
        f"Expected at least 10 advanced edge case functions, found: {sorted(list(found_functions))}"
    )

    expected_types = [
        f"{project_name}.advanced_edge_cases.ComplexLifetimes",
        f"{project_name}.advanced_edge_cases.ConstrainedStruct",
        f"{project_name}.advanced_edge_cases.PhantomStruct",
        f"{project_name}.advanced_edge_cases.StringProcessor",
        f"{project_name}.advanced_edge_cases.NumberProcessor",
        f"{project_name}.advanced_edge_cases.NestedStruct",
        f"{project_name}.advanced_edge_cases.ComplexFuture",
        f"{project_name}.advanced_edge_cases.AsyncStringProcessor",
        f"{project_name}.advanced_edge_cases.UnsafeStruct",
        f"{project_name}.advanced_edge_cases.CCompatibleStruct",
        f"{project_name}.advanced_edge_cases.PackedStruct",
        f"{project_name}.advanced_edge_cases.ComplexEnum",
        f"{project_name}.advanced_edge_cases.ComplexStruct",
        f"{project_name}.advanced_edge_cases.FixedArray",
        f"{project_name}.advanced_edge_cases.HigherRankedImpl",
        f"{project_name}.advanced_edge_cases.MathImpl",
        f"{project_name}.advanced_edge_cases.ComplexError",
        f"{project_name}.advanced_edge_cases.AssociatedLifetime",
        f"{project_name}.advanced_edge_cases.AsyncProcessor",
        f"{project_name}.advanced_edge_cases.UnsafeTrait",
        f"{project_name}.advanced_edge_cases.HigherRankedTrait",
        f"{project_name}.advanced_edge_cases.ObjectSafeTrait",
        f"{project_name}.advanced_edge_cases.NotObjectSafeTrait",
        f"{project_name}.advanced_edge_cases.MathConstants",
        f"{project_name}.advanced_edge_cases.ComplexDefault",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    found_types = set(expected_types) & created_classes
    assert len(found_types) >= 8, (
        f"Expected at least 8 advanced types, found: {sorted(list(found_types))}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    edge_case_calls = [
        call for call in call_relationships if "advanced_edge_cases" in call.args[0][2]
    ]

    assert len(edge_case_calls) >= 8, (
        f"Expected at least 8 advanced edge case call relationships, found {len(edge_case_calls)}"
    )

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    edge_case_imports = [
        call
        for call in import_relationships
        if "advanced_edge_cases" in call.args[0][2]
    ]

    assert len(edge_case_imports) >= 5, (
        f"Expected at least 5 import relationships for advanced features, found {len(edge_case_imports)}"
    )
