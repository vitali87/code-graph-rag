from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_structs_project(temp_repo: Path) -> Path:
    """Create a Rust project structure for structs and enums testing."""
    project_path = temp_repo / "rust_structs_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Library root"
    )

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""[package]
name = "rust_structs_test"
version = "0.1.0"
""",
    )

    return project_path


def test_basic_struct_definitions(
    rust_structs_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic struct definition parsing and field extraction."""
    test_file = rust_structs_project / "basic_structs.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Named struct with fields
pub struct Person {
    pub name: String,
    age: u32,
    email: Option<String>,
}

// Struct with generic parameters
pub struct Point<T> {
    pub x: T,
    pub y: T,
}

// Struct with lifetime parameters
pub struct StringRef<'a> {
    data: &'a str,
    length: usize,
}

// Struct with both generics and lifetimes
pub struct Container<'a, T> {
    items: &'a [T],
    capacity: usize,
}

// Unit struct
pub struct Unit;

// Tuple struct
pub struct Color(pub u8, pub u8, pub u8);

// Newtype pattern
pub struct UserId(pub u64);

impl Person {
    pub fn new(name: String, age: u32) -> Self {
        Person { name, age, email: None }
    }

    pub fn set_email(&mut self, email: String) {
        self.email = Some(email);
    }
}

impl<T> Point<T> {
    pub fn new(x: T, y: T) -> Self {
        Point { x, y }
    }
}
""",
    )

    run_updater(rust_structs_project, mock_ingestor, skip_if_missing="rust")
    calls = mock_ingestor.method_calls

    struct_calls = [call for call in calls if "Person" in str(call)]
    assert len(struct_calls) > 0, "Person struct should be detected"

    point_calls = [call for call in calls if "Point" in str(call)]
    assert len(point_calls) > 0, "Generic Point struct should be detected"

    method_calls = [
        call for call in calls if ("new" in str(call) or "set_email" in str(call))
    ]
    assert len(method_calls) > 0, "Struct methods should be detected"


def test_enum_definitions_and_variants(
    rust_structs_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test enum definition parsing and variant extraction."""
    test_file = rust_structs_project / "enums.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Simple enum with unit variants
#[derive(Debug, Clone, PartialEq)]
pub enum Direction {
    North,
    South,
    East,
    West,
}

// Enum with data variants
pub enum Message {
    Quit,
    Move { x: i32, y: i32 },
    Write(String),
    ChangeColor(i32, i32, i32),
}

// Generic enum
pub enum Option<T> {
    Some(T),
    None,
}

// Enum with complex variants
pub enum IpAddr {
    V4(u8, u8, u8, u8),
    V6(String),
}

// Enum with methods
impl Direction {
    pub fn opposite(&self) -> Direction {
        match self {
            Direction::North => Direction::South,
            Direction::South => Direction::North,
            Direction::East => Direction::West,
            Direction::West => Direction::East,
        }
    }

    pub fn is_vertical(&self) -> bool {
        matches!(self, Direction::North | Direction::South)
    }
}

impl Message {
    pub fn call(&self) {
        match self {
            Message::Quit => println!("Quit message"),
            Message::Move { x, y } => println!("Move to ({}, {})", x, y),
            Message::Write(text) => println!("Text: {}", text),
            Message::ChangeColor(r, g, b) => println!("RGB({}, {}, {})", r, g, b),
        }
    }
}

// Function using enums
pub fn process_message(msg: Message) -> String {
    match msg {
        Message::Quit => "Quitting".to_string(),
        Message::Move { x, y } => format!("Moving to ({}, {})", x, y),
        Message::Write(text) => text,
        Message::ChangeColor(r, g, b) => format!("Color: RGB({}, {}, {})", r, g, b),
    }
}
""",
    )

    run_updater(rust_structs_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    direction_calls = [call for call in calls if "Direction" in str(call)]
    assert len(direction_calls) > 0, "Direction enum should be detected"

    message_calls = [call for call in calls if "Message" in str(call)]
    assert len(message_calls) > 0, "Message enum should be detected"

    method_calls = [
        call for call in calls if ("opposite" in str(call) or "call" in str(call))
    ]
    assert len(method_calls) > 0, "Enum methods should be detected"


def test_pattern_matching_destructuring(
    rust_structs_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test pattern matching and destructuring in various contexts."""
    test_file = rust_structs_project / "patterns.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
pub struct Point {
    pub x: i32,
    pub y: i32,
}

pub enum Shape {
    Circle { radius: f64 },
    Rectangle { width: f64, height: f64 },
    Triangle { a: f64, b: f64, c: f64 },
}

// Function with pattern matching in parameters
pub fn extract_coordinates((x, y): (i32, i32)) -> String {
    format!("({}, {})", x, y)
}

// Pattern matching in let statements
pub fn destructure_point(p: Point) {
    let Point { x, y } = p;
    println!("Point at ({}, {})", x, y);

    // Nested destructuring
    let points = vec![Point { x: 1, y: 2 }, Point { x: 3, y: 4 }];
    let [Point { x: x1, y: y1 }, Point { x: x2, y: y2 }] = &points[..2] else {
        panic!("Not enough points");
    };

    // Tuple destructuring
    let (first, second, ..) = (1, 2, 3, 4, 5);

    // Array destructuring with rest
    let [head, tail @ ..] = [1, 2, 3, 4, 5];
}

// Match expressions with guards
pub fn analyze_shape(shape: Shape) -> f64 {
    match shape {
        Shape::Circle { radius } if radius > 0.0 => {
            std::f64::consts::PI * radius * radius
        }
        Shape::Rectangle { width, height } if width > 0.0 && height > 0.0 => {
            width * height
        }
        Shape::Triangle { a, b, c } if a > 0.0 && b > 0.0 && c > 0.0 => {
            // Heron's formula
            let s = (a + b + c) / 2.0;
            (s * (s - a) * (s - b) * (s - c)).sqrt()
        }
        _ => 0.0,
    }
}

// If-let patterns
pub fn process_option(opt: Option<i32>) {
    if let Some(value) = opt {
        println!("Got value: {}", value);
    } else {
        println!("No value");
    }

    // Multiple if-let chains
    if let Some(x) = opt {
        if let Ok(result) = calculate(x) {
            println!("Result: {}", result);
        }
    }
}

// While-let patterns
pub fn consume_iterator(mut iter: impl Iterator<Item = i32>) {
    while let Some(item) = iter.next() {
        if item > 10 {
            break;
        }
        println!("Processing: {}", item);
    }
}

// For loop destructuring
pub fn iterate_pairs(pairs: Vec<(i32, String)>) {
    for (id, name) in pairs {
        println!("ID: {}, Name: {}", id, name);
    }
}

fn calculate(x: i32) -> Result<i32, String> {
    if x < 0 {
        Err("Negative input".to_string())
    } else {
        Ok(x * 2)
    }
}
""",
    )

    run_updater(rust_structs_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    pattern_calls = [
        call
        for call in calls
        if any(
            name in str(call)
            for name in [
                "extract_coordinates",
                "destructure_point",
                "analyze_shape",
                "process_option",
                "consume_iterator",
            ]
        )
    ]
    assert len(pattern_calls) > 0, "Pattern matching functions should be detected"


def test_complex_struct_relationships(
    rust_structs_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test complex struct relationships and nested types."""
    test_file = rust_structs_project / "complex_structs.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::collections::HashMap;
use std::rc::Rc;
use std::cell::RefCell;

// Struct with complex field types
pub struct Database {
    tables: HashMap<String, Table>,
    connections: Vec<Connection>,
    metadata: Metadata,
}

pub struct Table {
    name: String,
    columns: Vec<Column>,
    rows: Vec<Row>,
    indices: HashMap<String, Index>,
}

pub struct Column {
    name: String,
    data_type: DataType,
    nullable: bool,
    default_value: Option<Value>,
}

pub struct Row {
    values: HashMap<String, Value>,
    id: u64,
}

pub struct Index {
    name: String,
    columns: Vec<String>,
    unique: bool,
}

pub struct Connection {
    id: u32,
    user: String,
    active: bool,
}

pub struct Metadata {
    version: String,
    created_at: chrono::DateTime<chrono::Utc>,
    schema_version: u32,
}

#[derive(Debug, Clone)]
pub enum DataType {
    Integer,
    Text,
    Boolean,
    Float,
    DateTime,
    Binary,
}

#[derive(Debug, Clone)]
pub enum Value {
    Integer(i64),
    Text(String),
    Boolean(bool),
    Float(f64),
    DateTime(chrono::DateTime<chrono::Utc>),
    Binary(Vec<u8>),
    Null,
}

// Struct with self-referential types using Rc<RefCell<>>
pub struct Node<T> {
    value: T,
    children: Vec<Rc<RefCell<Node<T>>>>,
    parent: Option<Rc<RefCell<Node<T>>>>,
}

impl Database {
    pub fn new() -> Self {
        Database {
            tables: HashMap::new(),
            connections: Vec::new(),
            metadata: Metadata {
                version: "1.0.0".to_string(),
                created_at: chrono::Utc::now(),
                schema_version: 1,
            },
        }
    }

    pub fn add_table(&mut self, table: Table) {
        self.tables.insert(table.name.clone(), table);
    }

    pub fn get_table(&self, name: &str) -> Option<&Table> {
        self.tables.get(name)
    }

    pub fn create_connection(&mut self, user: String) -> u32 {
        let id = self.connections.len() as u32;
        self.connections.push(Connection {
            id,
            user,
            active: true,
        });
        id
    }
}

impl<T> Node<T> {
    pub fn new(value: T) -> Rc<RefCell<Self>> {
        Rc::new(RefCell::new(Node {
            value,
            children: Vec::new(),
            parent: None,
        }))
    }

    pub fn add_child(&mut self, child: Rc<RefCell<Node<T>>>) {
        self.children.push(child);
    }
}
""",
    )

    run_updater(rust_structs_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    db_calls = [call for call in calls if "Database" in str(call)]
    assert len(db_calls) > 0, "Database struct should be detected"

    node_calls = [call for call in calls if "Node" in str(call)]
    assert len(node_calls) > 0, "Generic Node struct should be detected"

    table_calls = [call for call in calls if "Table" in str(call)]
    assert len(table_calls) > 0, "Table struct should be detected"


def test_struct_derive_attributes(
    rust_structs_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test struct definitions with derive attributes and custom implementations."""
    test_file = rust_structs_project / "derives.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Struct with standard derives
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Product {
    pub id: u64,
    pub name: String,
    pub price: u32, // in cents
}

// Struct with custom Debug implementation
#[derive(Clone, PartialEq)]
pub struct SecretData {
    public_info: String,
    secret: String,
}

impl std::fmt::Debug for SecretData {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SecretData")
            .field("public_info", &self.public_info)
            .field("secret", &"[REDACTED]")
            .finish()
    }
}

// Struct with custom PartialEq
#[derive(Debug, Clone)]
pub struct CaseInsensitiveString {
    value: String,
}

impl PartialEq for CaseInsensitiveString {
    fn eq(&self, other: &Self) -> bool {
        self.value.to_lowercase() == other.value.to_lowercase()
    }
}

impl Eq for CaseInsensitiveString {}

// Struct with custom ordering
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Priority {
    level: u8,
    name: String,
}

impl PartialOrd for Priority {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Priority {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        // Higher priority levels come first
        other.level.cmp(&self.level)
            .then_with(|| self.name.cmp(&other.name))
    }
}

// Struct with Serde derives (conditional compilation)
#[cfg(feature = "serde")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ApiResponse<T> {
    pub success: bool,
    pub data: Option<T>,
    pub error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub metadata: Option<HashMap<String, String>>,
}

// Newtype with custom Display
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Email(pub String);

impl std::fmt::Display for Email {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::str::FromStr for Email {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        if s.contains('@') {
            Ok(Email(s.to_string()))
        } else {
            Err("Invalid email format".to_string())
        }
    }
}

use std::collections::HashMap;
""",
    )

    run_updater(rust_structs_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    product_calls = [call for call in calls if "Product" in str(call)]
    assert len(product_calls) > 0, "Product struct with derives should be detected"

    secret_calls = [call for call in calls if "SecretData" in str(call)]
    assert len(secret_calls) > 0, "SecretData struct should be detected"

    impl_calls = [
        call
        for call in calls
        if any(
            trait_name in str(call) for trait_name in ["fmt", "eq", "cmp", "from_str"]
        )
    ]
    assert len(impl_calls) > 0, "Custom trait implementations should be detected"


def test_enum_pattern_matching_advanced(
    rust_structs_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test advanced enum pattern matching and complex enum structures."""
    test_file = rust_structs_project / "advanced_enums.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Complex enum with various data patterns
#[derive(Debug, Clone)]
pub enum Event {
    KeyPress { key: char, modifiers: Vec<String> },
    MouseClick { x: i32, y: i32, button: MouseButton },
    WindowResize(u32, u32),
    Timer(std::time::Duration),
    Custom { event_type: String, data: serde_json::Value },
}

#[derive(Debug, Clone)]
pub enum MouseButton {
    Left,
    Right,
    Middle,
    Other(u8),
}

// Recursive enum for expression trees
#[derive(Debug, Clone)]
pub enum Expr {
    Number(f64),
    Variable(String),
    BinaryOp {
        op: BinaryOperator,
        left: Box<Expr>,
        right: Box<Expr>,
    },
    UnaryOp {
        op: UnaryOperator,
        operand: Box<Expr>,
    },
    FunctionCall {
        name: String,
        args: Vec<Expr>,
    },
}

#[derive(Debug, Clone)]
pub enum BinaryOperator {
    Add,
    Subtract,
    Multiply,
    Divide,
    Power,
    Modulo,
}

#[derive(Debug, Clone)]
pub enum UnaryOperator {
    Negate,
    Not,
    Abs,
}

impl Event {
    pub fn is_input_event(&self) -> bool {
        matches!(self, Event::KeyPress { .. } | Event::MouseClick { .. })
    }

    pub fn extract_coordinates(&self) -> Option<(i32, i32)> {
        match self {
            Event::MouseClick { x, y, .. } => Some((*x, *y)),
            _ => None,
        }
    }
}

impl Expr {
    pub fn evaluate(&self, vars: &std::collections::HashMap<String, f64>) -> Result<f64, String> {
        match self {
            Expr::Number(n) => Ok(*n),
            Expr::Variable(name) => {
                vars.get(name)
                    .copied()
                    .ok_or_else(|| format!("Undefined variable: {}", name))
            }
            Expr::BinaryOp { op, left, right } => {
                let left_val = left.evaluate(vars)?;
                let right_val = right.evaluate(vars)?;
                match op {
                    BinaryOperator::Add => Ok(left_val + right_val),
                    BinaryOperator::Subtract => Ok(left_val - right_val),
                    BinaryOperator::Multiply => Ok(left_val * right_val),
                    BinaryOperator::Divide => {
                        if right_val == 0.0 {
                            Err("Division by zero".to_string())
                        } else {
                            Ok(left_val / right_val)
                        }
                    }
                    BinaryOperator::Power => Ok(left_val.powf(right_val)),
                    BinaryOperator::Modulo => Ok(left_val % right_val),
                }
            }
            Expr::UnaryOp { op, operand } => {
                let val = operand.evaluate(vars)?;
                match op {
                    UnaryOperator::Negate => Ok(-val),
                    UnaryOperator::Not => Ok(if val == 0.0 { 1.0 } else { 0.0 }),
                    UnaryOperator::Abs => Ok(val.abs()),
                }
            }
            Expr::FunctionCall { name, args } => {
                match name.as_str() {
                    "sin" if args.len() == 1 => {
                        let arg = args[0].evaluate(vars)?;
                        Ok(arg.sin())
                    }
                    "cos" if args.len() == 1 => {
                        let arg = args[0].evaluate(vars)?;
                        Ok(arg.cos())
                    }
                    "sqrt" if args.len() == 1 => {
                        let arg = args[0].evaluate(vars)?;
                        if arg < 0.0 {
                            Err("Square root of negative number".to_string())
                        } else {
                            Ok(arg.sqrt())
                        }
                    }
                    _ => Err(format!("Unknown function: {}", name)),
                }
            }
        }
    }
}

// Pattern matching with nested destructuring
pub fn process_complex_event(event: Event) -> String {
    match event {
        Event::KeyPress { key: 'q', modifiers } if modifiers.contains(&"ctrl".to_string()) => {
            "Quit command detected".to_string()
        }
        Event::KeyPress { key, modifiers } => {
            format!("Key '{}' pressed with modifiers: {:?}", key, modifiers)
        }
        Event::MouseClick { x, y, button: MouseButton::Left } if x > 0 && y > 0 => {
            format!("Left click at ({}, {})", x, y)
        }
        Event::MouseClick { button: MouseButton::Other(code), .. } => {
            format!("Unknown mouse button: {}", code)
        }
        Event::WindowResize(width, height) => {
            format!("Window resized to {}x{}", width, height)
        }
        Event::Timer(duration) => {
            format!("Timer event: {:?}", duration)
        }
        Event::Custom { event_type, .. } => {
            format!("Custom event: {}", event_type)
        }
        _ => "Other event".to_string(),
    }
}

// Function demonstrating exhaustive pattern matching
pub fn classify_expression(expr: &Expr) -> &'static str {
    match expr {
        Expr::Number(_) => "literal",
        Expr::Variable(_) => "variable",
        Expr::BinaryOp { .. } => "binary_operation",
        Expr::UnaryOp { .. } => "unary_operation",
        Expr::FunctionCall { .. } => "function_call",
    }
}
""",
    )

    run_updater(rust_structs_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    event_calls = [call for call in calls if "Event" in str(call)]
    assert len(event_calls) > 0, "Event enum should be detected"

    expr_calls = [call for call in calls if "Expr" in str(call)]
    assert len(expr_calls) > 0, "Expr enum should be detected"

    method_calls = [
        call
        for call in calls
        if any(
            name in str(call)
            for name in ["evaluate", "process_complex_event", "classify_expression"]
        )
    ]
    assert len(method_calls) > 0, (
        "Enum methods and pattern matching functions should be detected"
    )
