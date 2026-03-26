from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_traits_project(temp_repo: Path) -> Path:
    """Create a Rust project structure for traits and generics testing."""
    project_path = temp_repo / "rust_traits_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Library root"
    )

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""[package]
name = "rust_traits_test"
version = "0.1.0"
""",
    )

    return project_path


def test_basic_trait_definitions(
    rust_traits_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic trait definition parsing and method extraction."""
    test_file = rust_traits_project / "basic_traits.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic trait with method signatures
pub trait Drawable {
    fn draw(&self);
    fn area(&self) -> f64;
    fn perimeter(&self) -> f64;
}

// Trait with default implementations
pub trait Printable {
    fn print(&self);

    fn print_with_prefix(&self, prefix: &str) {
        print!("{}: ", prefix);
        self.print();
    }

    fn print_boxed(&self) {
        println!("┌─────────────┐");
        print!("│ ");
        self.print();
        println!(" │");
        println!("└─────────────┘");
    }
}

// Trait with associated types
pub trait Iterator {
    type Item;

    fn next(&mut self) -> Option<Self::Item>;

    fn count(self) -> usize
    where
        Self: Sized,
    {
        let mut count = 0;
        while let Some(_) = self.next() {
            count += 1;
        }
        count
    }

    fn collect<C>(self) -> C
    where
        Self: Sized,
        C: FromIterator<Self::Item>,
    {
        FromIterator::from_iter(self)
    }
}

// Trait with associated constants
pub trait Numeric {
    const ZERO: Self;
    const ONE: Self;

    fn add(self, other: Self) -> Self;
    fn multiply(self, other: Self) -> Self;

    fn is_zero(&self) -> bool;
}

// Trait with generic parameters
pub trait Converter<T> {
    type Error;

    fn convert(&self, input: T) -> Result<Self, Self::Error>;
}

// Marker trait (no methods)
pub trait Send {}
pub trait Sync {}

struct Circle {
    radius: f64,
}

struct Rectangle {
    width: f64,
    height: f64,
}

impl Drawable for Circle {
    fn draw(&self) {
        println!("Drawing a circle with radius {}", self.radius);
    }

    fn area(&self) -> f64 {
        std::f64::consts::PI * self.radius * self.radius
    }

    fn perimeter(&self) -> f64 {
        2.0 * std::f64::consts::PI * self.radius
    }
}

impl Drawable for Rectangle {
    fn draw(&self) {
        println!("Drawing a rectangle {}x{}", self.width, self.height);
    }

    fn area(&self) -> f64 {
        self.width * self.height
    }

    fn perimeter(&self) -> f64 {
        2.0 * (self.width + self.height)
    }
}

impl Printable for Circle {
    fn print(&self) {
        print!("Circle({})", self.radius);
    }
}

impl Printable for Rectangle {
    fn print(&self) {
        print!("Rectangle({}x{})", self.width, self.height);
    }
}
""",
    )

    run_updater(rust_traits_project, mock_ingestor, skip_if_missing="rust")
    calls = mock_ingestor.method_calls

    drawable_calls = [call for call in calls if "Drawable" in str(call)]
    assert len(drawable_calls) > 0, "Drawable trait should be detected"

    printable_calls = [call for call in calls if "Printable" in str(call)]
    assert len(printable_calls) > 0, "Printable trait should be detected"

    trait_impl_calls = [
        call
        for call in calls
        if any(concrete_type in str(call) for concrete_type in ["Circle", "Rectangle"])
        and any(
            trait_method in str(call)
            for trait_method in ["draw", "print", "area", "perimeter"]
        )
    ]
    assert len(trait_impl_calls) > 0, (
        "Trait implementations should be detected (concrete types should have trait methods)"
    )


def test_generic_types_and_constraints(
    rust_traits_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test generic type parameters and trait bounds."""
    test_file = rust_traits_project / "generics.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::fmt::{Debug, Display};
use std::ops::{Add, Mul};

// Generic struct with type parameters
pub struct Pair<T> {
    pub first: T,
    pub second: T,
}

// Generic struct with multiple type parameters
pub struct KeyValue<K, V> {
    pub key: K,
    pub value: V,
}

// Generic struct with lifetime parameters
pub struct Borrowed<'a, T> {
    pub data: &'a T,
    pub metadata: String,
}

// Generic struct with both lifetime and type parameters
pub struct Container<'a, T, U>
where
    T: Clone + Debug,
    U: Display,
{
    pub items: &'a [T],
    pub label: U,
    pub count: usize,
}

impl<T> Pair<T> {
    pub fn new(first: T, second: T) -> Self {
        Pair { first, second }
    }

    pub fn swap(self) -> Pair<T> {
        Pair {
            first: self.second,
            second: self.first,
        }
    }
}

impl<T: Clone> Pair<T> {
    pub fn duplicate_first(&self) -> Pair<T> {
        Pair {
            first: self.first.clone(),
            second: self.first.clone(),
        }
    }
}

impl<T: Add<Output = T> + Copy> Pair<T> {
    pub fn sum(&self) -> T {
        self.first + self.second
    }
}

impl<T: Debug> std::fmt::Debug for Pair<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Pair({:?}, {:?})", self.first, self.second)
    }
}

impl<K, V> KeyValue<K, V> {
    pub fn new(key: K, value: V) -> Self {
        KeyValue { key, value }
    }

    pub fn key(&self) -> &K {
        &self.key
    }

    pub fn value(&self) -> &V {
        &self.value
    }

    pub fn into_tuple(self) -> (K, V) {
        (self.key, self.value)
    }
}

impl<K: Clone, V: Clone> Clone for KeyValue<K, V> {
    fn clone(&self) -> Self {
        KeyValue {
            key: self.key.clone(),
            value: self.value.clone(),
        }
    }
}

// Generic functions with trait bounds
pub fn compare_and_print<T>(a: T, b: T) -> T
where
    T: PartialOrd + Debug + Clone,
{
    println!("Comparing {:?} and {:?}", a, b);
    if a > b { a } else { b }
}

pub fn multiply_and_display<T>(a: T, b: T)
where
    T: Mul<Output = T> + Display + Copy,
{
    let result = a * b;
    println!("{} * {} = {}", a, b, result);
}

// Function with multiple generic parameters
pub fn process_collection<T, F, R>(items: Vec<T>, processor: F) -> Vec<R>
where
    F: Fn(T) -> R,
{
    items.into_iter().map(processor).collect()
}

// Generic trait with associated types
pub trait Collectable<T> {
    type Output;

    fn collect_items(&self, items: Vec<T>) -> Self::Output;
}

pub struct ListCollector;
pub struct SetCollector;

impl<T> Collectable<T> for ListCollector {
    type Output = Vec<T>;

    fn collect_items(&self, items: Vec<T>) -> Self::Output {
        items
    }
}

impl<T: std::hash::Hash + Eq> Collectable<T> for SetCollector {
    type Output = std::collections::HashSet<T>;

    fn collect_items(&self, items: Vec<T>) -> Self::Output {
        items.into_iter().collect()
    }
}
""",
    )

    run_updater(rust_traits_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    pair_calls = [call for call in calls if "Pair" in str(call)]
    assert len(pair_calls) > 0, "Generic Pair struct should be detected"

    keyvalue_calls = [call for call in calls if "KeyValue" in str(call)]
    assert len(keyvalue_calls) > 0, "Generic KeyValue struct should be detected"

    generic_func_calls = [
        call
        for call in calls
        if any(
            name in str(call)
            for name in [
                "compare_and_print",
                "multiply_and_display",
                "process_collection",
            ]
        )
    ]
    assert len(generic_func_calls) > 0, "Generic functions should be detected"


def test_associated_types_and_constants(
    rust_traits_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test traits with associated types and constants."""
    test_file = rust_traits_project / "associated_types.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Trait with associated types and constants
pub trait Parser {
    type Input;
    type Output;
    type Error;

    const MAX_DEPTH: usize = 100;
    const BUFFER_SIZE: usize;

    fn parse(&mut self, input: Self::Input) -> Result<Self::Output, Self::Error>;
    fn reset(&mut self);

    fn can_parse(&self, input: &Self::Input) -> bool {
        true // default implementation
    }
}

// JSON parser implementation
pub struct JsonParser {
    depth: usize,
    buffer: Vec<u8>,
}

#[derive(Debug)]
pub enum JsonValue {
    Null,
    Bool(bool),
    Number(f64),
    String(String),
    Array(Vec<JsonValue>),
    Object(std::collections::HashMap<String, JsonValue>),
}

#[derive(Debug)]
pub enum JsonError {
    UnexpectedToken(String),
    MaxDepthExceeded,
    InvalidNumber,
    InvalidString,
}

impl Parser for JsonParser {
    type Input = String;
    type Output = JsonValue;
    type Error = JsonError;

    const BUFFER_SIZE: usize = 8192;

    fn parse(&mut self, input: Self::Input) -> Result<Self::Output, Self::Error> {
        self.depth = 0;
        self.buffer.clear();

        if input.trim().is_empty() {
            return Ok(JsonValue::Null);
        }

        // Simplified JSON parsing logic
        if input.starts_with('{') {
            Ok(JsonValue::Object(std::collections::HashMap::new()))
        } else if input.starts_with('[') {
            Ok(JsonValue::Array(Vec::new()))
        } else if input == "null" {
            Ok(JsonValue::Null)
        } else if input == "true" {
            Ok(JsonValue::Bool(true))
        } else if input == "false" {
            Ok(JsonValue::Bool(false))
        } else if let Ok(num) = input.parse::<f64>() {
            Ok(JsonValue::Number(num))
        } else {
            Ok(JsonValue::String(input))
        }
    }

    fn reset(&mut self) {
        self.depth = 0;
        self.buffer.clear();
    }

    fn can_parse(&self, input: &Self::Input) -> bool {
        !input.trim().is_empty()
    }
}

// XML parser implementation
pub struct XmlParser {
    state: ParseState,
}

#[derive(Debug)]
pub enum XmlNode {
    Element {
        name: String,
        attributes: std::collections::HashMap<String, String>,
        children: Vec<XmlNode>,
    },
    Text(String),
    Comment(String),
}

#[derive(Debug)]
pub enum XmlError {
    MalformedTag,
    UnclosedTag(String),
    InvalidAttribute,
}

#[derive(Debug)]
enum ParseState {
    Text,
    Tag,
    Attribute,
}

impl Parser for XmlParser {
    type Input = String;
    type Output = XmlNode;
    type Error = XmlError;

    const BUFFER_SIZE: usize = 16384;

    fn parse(&mut self, input: Self::Input) -> Result<Self::Output, Self::Error> {
        // Simplified XML parsing
        if input.trim().is_empty() {
            Ok(XmlNode::Text(String::new()))
        } else if input.starts_with('<') && input.ends_with('>') {
            Ok(XmlNode::Element {
                name: "root".to_string(),
                attributes: std::collections::HashMap::new(),
                children: Vec::new(),
            })
        } else {
            Ok(XmlNode::Text(input))
        }
    }

    fn reset(&mut self) {
        self.state = ParseState::Text;
    }
}

// Generic function using associated types
pub fn parse_multiple<P>(mut parser: P, inputs: Vec<P::Input>) -> Vec<Result<P::Output, P::Error>>
where
    P: Parser,
{
    inputs.into_iter().map(|input| parser.parse(input)).collect()
}

// Trait with multiple associated types for advanced scenarios
pub trait Database {
    type Connection;
    type Transaction;
    type QueryResult;
    type Error;

    const MAX_CONNECTIONS: usize = 100;
    const DEFAULT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(30);

    fn connect(&self) -> Result<Self::Connection, Self::Error>;
    fn begin_transaction(&self, conn: &Self::Connection) -> Result<Self::Transaction, Self::Error>;
    fn execute_query(&self, tx: &Self::Transaction, query: &str) -> Result<Self::QueryResult, Self::Error>;
    fn commit(&self, tx: Self::Transaction) -> Result<(), Self::Error>;
    fn rollback(&self, tx: Self::Transaction) -> Result<(), Self::Error>;
}

// Trait with associated types used in return position
pub trait Factory {
    type Product;

    fn create(&self) -> Self::Product;
    fn create_batch(&self, count: usize) -> Vec<Self::Product> {
        (0..count).map(|_| self.create()).collect()
    }
}

pub struct StringFactory;

impl Factory for StringFactory {
    type Product = String;

    fn create(&self) -> Self::Product {
        format!("Product-{}", std::thread_local! { static COUNTER: std::cell::Cell<u32> = std::cell::Cell::new(0); })
    }
}
""",
    )

    run_updater(rust_traits_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    parser_calls = [call for call in calls if "Parser" in str(call)]
    assert len(parser_calls) > 0, (
        "Parser trait with associated types should be detected"
    )

    database_calls = [call for call in calls if "Database" in str(call)]
    assert len(database_calls) > 0, "Database trait should be detected"

    json_calls = [call for call in calls if "JsonParser" in str(call)]
    assert len(json_calls) > 0, "JsonParser implementation should be detected"


def test_trait_objects_and_dynamic_dispatch(
    rust_traits_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test trait objects and dynamic dispatch patterns."""
    test_file = rust_traits_project / "trait_objects.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Object-safe trait for dynamic dispatch
pub trait Drawable {
    fn draw(&self);
    fn name(&self) -> &str;
    fn area(&self) -> f64;
}

// Non-object-safe trait (has generic methods)
pub trait Cloneable {
    fn clone_boxed(&self) -> Box<dyn Drawable>;
}

pub struct Circle {
    radius: f64,
}

pub struct Rectangle {
    width: f64,
    height: f64,
}

pub struct Triangle {
    base: f64,
    height: f64,
}

impl Drawable for Circle {
    fn draw(&self) {
        println!("Drawing circle with radius {}", self.radius);
    }

    fn name(&self) -> &str {
        "Circle"
    }

    fn area(&self) -> f64 {
        std::f64::consts::PI * self.radius * self.radius
    }
}

impl Cloneable for Circle {
    fn clone_boxed(&self) -> Box<dyn Drawable> {
        Box::new(Circle { radius: self.radius })
    }
}

impl Drawable for Rectangle {
    fn draw(&self) {
        println!("Drawing rectangle {}x{}", self.width, self.height);
    }

    fn name(&self) -> &str {
        "Rectangle"
    }

    fn area(&self) -> f64 {
        self.width * self.height
    }
}

impl Cloneable for Rectangle {
    fn clone_boxed(&self) -> Box<dyn Drawable> {
        Box::new(Rectangle { width: self.width, height: self.height })
    }
}

impl Drawable for Triangle {
    fn draw(&self) {
        println!("Drawing triangle with base {} and height {}", self.base, self.height);
    }

    fn name(&self) -> &str {
        "Triangle"
    }

    fn area(&self) -> f64 {
        0.5 * self.base * self.height
    }
}

impl Cloneable for Triangle {
    fn clone_boxed(&self) -> Box<dyn Drawable> {
        Box::new(Triangle { base: self.base, height: self.height })
    }
}

// Functions using trait objects
pub fn draw_shapes(shapes: &[Box<dyn Drawable>]) {
    for shape in shapes {
        shape.draw();
        println!("  Area: {}", shape.area());
    }
}

pub fn total_area(shapes: &[Box<dyn Drawable>]) -> f64 {
    shapes.iter().map(|shape| shape.area()).sum()
}

pub fn find_largest_shape(shapes: &[Box<dyn Drawable>]) -> Option<&Box<dyn Drawable>> {
    shapes.iter().max_by(|a, b| a.area().partial_cmp(&b.area()).unwrap())
}

// Shape factory using trait objects
pub struct ShapeFactory;

impl ShapeFactory {
    pub fn create_circle(radius: f64) -> Box<dyn Drawable> {
        Box::new(Circle { radius })
    }

    pub fn create_rectangle(width: f64, height: f64) -> Box<dyn Drawable> {
        Box::new(Rectangle { width, height })
    }

    pub fn create_triangle(base: f64, height: f64) -> Box<dyn Drawable> {
        Box::new(Triangle { base, height })
    }

    pub fn create_random_shapes(count: usize) -> Vec<Box<dyn Drawable>> {
        use rand::Rng;
        let mut rng = rand::thread_rng();
        let mut shapes = Vec::new();

        for _ in 0..count {
            match rng.gen_range(0..3) {
                0 => shapes.push(Self::create_circle(rng.gen_range(1.0..10.0))),
                1 => shapes.push(Self::create_rectangle(rng.gen_range(1.0..10.0), rng.gen_range(1.0..10.0))),
                2 => shapes.push(Self::create_triangle(rng.gen_range(1.0..10.0), rng.gen_range(1.0..10.0))),
                _ => unreachable!(),
            }
        }

        shapes
    }
}

// Trait object with multiple trait bounds
pub fn process_drawable_cloneable(item: &(dyn Drawable + Cloneable)) {
    item.draw();
    let cloned = item.clone_boxed();
    println!("Cloned {} with area {}", cloned.name(), cloned.area());
}

// Using Rc for shared ownership of trait objects
use std::rc::Rc;

pub struct Canvas {
    shapes: Vec<Rc<dyn Drawable>>,
}

impl Canvas {
    pub fn new() -> Self {
        Canvas { shapes: Vec::new() }
    }

    pub fn add_shape(&mut self, shape: Rc<dyn Drawable>) {
        self.shapes.push(shape);
    }

    pub fn draw_all(&self) {
        for shape in &self.shapes {
            shape.draw();
        }
    }

    pub fn get_total_area(&self) -> f64 {
        self.shapes.iter().map(|shape| shape.area()).sum()
    }

    pub fn clone_shapes(&self) -> Vec<Rc<dyn Drawable>> {
        self.shapes.clone()
    }
}

// Generic function with trait object parameters
pub fn compare_drawable_areas<T: AsRef<dyn Drawable>>(a: T, b: T) -> std::cmp::Ordering {
    let area_a = a.as_ref().area();
    let area_b = b.as_ref().area();
    area_a.partial_cmp(&area_b).unwrap_or(std::cmp::Ordering::Equal)
}
""",
    )

    run_updater(rust_traits_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    drawable_calls = [call for call in calls if "Drawable" in str(call)]
    assert len(drawable_calls) > 0, "Drawable trait for objects should be detected"

    factory_calls = [call for call in calls if "ShapeFactory" in str(call)]
    assert len(factory_calls) > 0, "ShapeFactory should be detected"

    canvas_calls = [call for call in calls if "Canvas" in str(call)]
    assert len(canvas_calls) > 0, "Canvas with trait objects should be detected"


def test_higher_ranked_trait_bounds(
    rust_traits_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test higher-ranked trait bounds (HRTB) and complex lifetime scenarios."""
    test_file = rust_traits_project / "hrtb.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Higher-ranked trait bounds with for<'a> syntax
pub fn call_with_any_lifetime<F>(f: F) -> i32
where
    F: for<'a> Fn(&'a str) -> i32,
{
    let s = "test string";
    f(s)
}

pub fn higher_ranked_closure_example() -> i32 {
    call_with_any_lifetime(|s: &str| s.len() as i32)
}

// HRTB with multiple lifetime parameters
pub fn complex_hrtb<F>(f: F) -> String
where
    F: for<'a, 'b> Fn(&'a str, &'b str) -> String,
{
    let s1 = "hello";
    let s2 = "world";
    f(s1, s2)
}

// Trait with HRTB in associated types
pub trait Processor {
    type Output<'a>
    where
        Self: 'a;

    fn process<'a>(&'a self, input: &'a str) -> Self::Output<'a>;
}

pub struct StringProcessor {
    prefix: String,
}

impl Processor for StringProcessor {
    type Output<'a> = std::borrow::Cow<'a, str>
    where
        Self: 'a;

    fn process<'a>(&'a self, input: &'a str) -> Self::Output<'a> {
        if self.prefix.is_empty() {
            std::borrow::Cow::Borrowed(input)
        } else {
            std::borrow::Cow::Owned(format!("{}: {}", self.prefix, input))
        }
    }
}

// Function with complex lifetime relationships
pub fn process_with_any_processor<P, F>(processor: P, transformer: F, input: &str) -> String
where
    P: Processor,
    F: for<'a> Fn(P::Output<'a>) -> String,
{
    let result = processor.process(input);
    transformer(result)
}

// Trait with lifetime bounds in generic parameters
pub trait Storage<T>
where
    T: for<'a> serde::Deserialize<'a>,
{
    type Error;

    fn store(&mut self, key: &str, value: &T) -> Result<(), Self::Error>;
    fn load(&self, key: &str) -> Result<T, Self::Error>;
}

// Generic struct with HRTB constraints
pub struct Database<T>
where
    T: for<'de> serde::Deserialize<'de> + serde::Serialize,
{
    data: std::collections::HashMap<String, String>,
    _phantom: std::marker::PhantomData<T>,
}

impl<T> Database<T>
where
    T: for<'de> serde::Deserialize<'de> + serde::Serialize,
{
    pub fn new() -> Self {
        Database {
            data: std::collections::HashMap::new(),
            _phantom: std::marker::PhantomData,
        }
    }

    pub fn insert(&mut self, key: String, value: T) -> Result<(), serde_json::Error> {
        let serialized = serde_json::to_string(&value)?;
        self.data.insert(key, serialized);
        Ok(())
    }

    pub fn get(&self, key: &str) -> Result<Option<T>, serde_json::Error> {
        match self.data.get(key) {
            Some(serialized) => {
                let value = serde_json::from_str(serialized)?;
                Ok(Some(value))
            }
            None => Ok(None),
        }
    }
}

// Function demonstrating complex HRTB usage
pub fn apply_to_any_ref<F, R>(f: F) -> R
where
    F: for<'a> Fn(&'a str) -> R,
{
    let owned_string = String::from("example");
    f(&owned_string)
}

// Closure that works with any lifetime
pub fn create_universal_mapper() -> impl for<'a> Fn(&'a str) -> usize {
    |s: &str| s.len()
}

// Complex example with multiple HRTB and associated types
pub trait UniversalTransformer {
    type Input<'a>: ?Sized;
    type Output<'a>;

    fn transform<'a>(&self, input: &'a Self::Input<'a>) -> Self::Output<'a>;
}

pub struct UppercaseTransformer;

impl UniversalTransformer for UppercaseTransformer {
    type Input<'a> = str;
    type Output<'a> = String;

    fn transform<'a>(&self, input: &'a Self::Input<'a>) -> Self::Output<'a> {
        input.to_uppercase()
    }
}

pub fn transform_with_universal<T, F>(transformer: T, processor: F, input: &str) -> String
where
    T: UniversalTransformer<Input<'_> = str, Output<'_> = String>,
    F: for<'a> Fn(&'a str) -> &'a str,
{
    let processed = processor(input);
    transformer.transform(processed)
}
""",
    )

    run_updater(rust_traits_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    hrtb_calls = [
        call
        for call in calls
        if any(
            name in str(call)
            for name in ["call_with_any_lifetime", "complex_hrtb", "apply_to_any_ref"]
        )
    ]
    assert len(hrtb_calls) > 0, "HRTB functions should be detected"

    processor_calls = [call for call in calls if "Processor" in str(call)]
    assert len(processor_calls) > 0, "Processor trait with HRTB should be detected"

    database_calls = [call for call in calls if "Database" in str(call)]
    assert len(database_calls) > 0, "Generic Database with HRTB should be detected"
