from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_pattern_project(temp_repo: Path) -> Path:
    """Create a Rust project structure for pattern matching testing."""
    project_path = temp_repo / "rust_pattern_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Pattern matching test crate"
    )

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""[package]
name = "rust_pattern_test"
version = "0.1.0"
edition = "2021"
""",
    )

    return project_path


def test_exhaustive_enum_matching(
    rust_pattern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test exhaustive pattern matching on enums with complex variants."""
    test_file = rust_pattern_project / "enum_matching.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Complex enum for pattern matching
#[derive(Debug, Clone)]
enum Message {
    Quit,
    Move { x: i32, y: i32 },
    Write(String),
    ChangeColor(i32, i32, i32),
    Complex {
        id: u64,
        data: Vec<u8>,
        metadata: Option<String>,
    },
}

// Exhaustive matching function
fn handle_message(msg: Message) {
    match msg {
        Message::Quit => {
            println!("Quitting application");
        }
        Message::Move { x, y } => {
            println!("Moving to ({}, {})", x, y);
        }
        Message::Write(text) => {
            println!("Writing: {}", text);
        }
        Message::ChangeColor(r, g, b) => {
            println!("Changing color to RGB({}, {}, {})", r, g, b);
        }
        Message::Complex { id, data, metadata } => {
            println!("Processing complex message {}", id);
            match metadata {
                Some(meta) => println!("Metadata: {}", meta),
                None => println!("No metadata"),
            }
            println!("Data length: {}", data.len());
        }
    }
}

// Nested enum matching
#[derive(Debug)]
enum HttpStatus {
    Success(SuccessCode),
    ClientError(ClientErrorCode),
    ServerError(ServerErrorCode),
}

#[derive(Debug)]
enum SuccessCode {
    Ok,
    Created,
    Accepted,
    NoContent,
}

#[derive(Debug)]
enum ClientErrorCode {
    BadRequest,
    Unauthorized,
    Forbidden,
    NotFound,
}

#[derive(Debug)]
enum ServerErrorCode {
    InternalServerError,
    NotImplemented,
    BadGateway,
    ServiceUnavailable,
}

fn handle_http_status(status: HttpStatus) -> &'static str {
    match status {
        HttpStatus::Success(SuccessCode::Ok) => "200 OK",
        HttpStatus::Success(SuccessCode::Created) => "201 Created",
        HttpStatus::Success(SuccessCode::Accepted) => "202 Accepted",
        HttpStatus::Success(SuccessCode::NoContent) => "204 No Content",

        HttpStatus::ClientError(ClientErrorCode::BadRequest) => "400 Bad Request",
        HttpStatus::ClientError(ClientErrorCode::Unauthorized) => "401 Unauthorized",
        HttpStatus::ClientError(ClientErrorCode::Forbidden) => "403 Forbidden",
        HttpStatus::ClientError(ClientErrorCode::NotFound) => "404 Not Found",

        HttpStatus::ServerError(ServerErrorCode::InternalServerError) => "500 Internal Server Error",
        HttpStatus::ServerError(ServerErrorCode::NotImplemented) => "501 Not Implemented",
        HttpStatus::ServerError(ServerErrorCode::BadGateway) => "502 Bad Gateway",
        HttpStatus::ServerError(ServerErrorCode::ServiceUnavailable) => "503 Service Unavailable",
    }
}

// Generic enum matching
#[derive(Debug)]
enum Result<T, E> {
    Ok(T),
    Err(E),
}

fn process_result<T, E>(result: Result<T, E>) -> String
where
    T: std::fmt::Debug,
    E: std::fmt::Debug,
{
    match result {
        Result::Ok(value) => format!("Success: {:?}", value),
        Result::Err(error) => format!("Error: {:?}", error),
    }
}

// Matching with multiple patterns
fn categorize_number(n: i32) -> &'static str {
    match n {
        0 => "zero",
        1 | 2 | 3 | 5 | 7 | 11 | 13 | 17 | 19 => "small prime or special",
        n if n < 0 => "negative",
        n if n % 2 == 0 => "positive even",
        n if n % 2 == 1 => "positive odd",
        _ => "other",
    }
}

// Box pattern matching
enum TreeNode {
    Leaf(i32),
    Branch(Box<TreeNode>, Box<TreeNode>),
}

fn sum_tree(node: TreeNode) -> i32 {
    match node {
        TreeNode::Leaf(value) => value,
        TreeNode::Branch(left, right) => sum_tree(*left) + sum_tree(*right),
    }
}

fn tree_depth(node: &TreeNode) -> usize {
    match node {
        TreeNode::Leaf(_) => 1,
        TreeNode::Branch(left, right) => {
            1 + tree_depth(left).max(tree_depth(right))
        }
    }
}
""",
    )

    run_updater(rust_pattern_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    pattern_calls = [
        call
        for call in calls
        if "handle_message" in str(call) or "handle_http_status" in str(call)
    ]
    assert len(pattern_calls) > 0, "Pattern matching functions should be detected"


def test_destructuring_patterns(
    rust_pattern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test complex destructuring patterns for structs, tuples, and arrays."""
    test_file = rust_pattern_project / "destructuring.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Struct for destructuring
#[derive(Debug)]
struct Point {
    x: f64,
    y: f64,
}

#[derive(Debug)]
struct Person {
    name: String,
    age: u32,
    address: Address,
}

#[derive(Debug)]
struct Address {
    street: String,
    city: String,
    zip: String,
}

// Tuple destructuring
fn destructure_tuples() {
    let triple = (1, 2.5, "hello");

    // Basic destructuring
    let (a, b, c) = triple;
    println!("a: {}, b: {}, c: {}", a, b, c);

    // Ignoring values
    let (first, _, third) = triple;
    println!("first: {}, third: {}", first, third);

    // Nested tuples
    let nested = ((1, 2), (3, 4), (5, 6));
    let ((x1, y1), (x2, y2), (x3, y3)) = nested;
    println!("Points: ({}, {}), ({}, {}), ({}, {})", x1, y1, x2, y2, x3, y3);
}

// Struct destructuring
fn destructure_structs() {
    let person = Person {
        name: "Alice".to_string(),
        age: 30,
        address: Address {
            street: "123 Main St".to_string(),
            city: "Anytown".to_string(),
            zip: "12345".to_string(),
        },
    };

    // Basic struct destructuring
    let Person { name, age, address } = person;
    println!("Name: {}, Age: {}", name, age);

    // Nested struct destructuring
    let Person {
        name: person_name,
        age: person_age,
        address: Address { street, city, zip },
    } = Person {
        name: "Bob".to_string(),
        age: 25,
        address: Address {
            street: "456 Oak Ave".to_string(),
            city: "Somewhere".to_string(),
            zip: "67890".to_string(),
        },
    };

    println!("Person: {} ({})", person_name, person_age);
    println!("Address: {}, {}, {}", street, city, zip);
}

// Array and slice destructuring
fn destructure_arrays() {
    let arr = [1, 2, 3, 4, 5];

    // Fixed array destructuring
    let [first, second, third, fourth, fifth] = arr;
    println!("Array: {}, {}, {}, {}, {}", first, second, third, fourth, fifth);

    // Slice pattern matching
    match arr.as_slice() {
        [] => println!("Empty slice"),
        [single] => println!("Single element: {}", single),
        [first, rest @ ..] => println!("First: {}, Rest: {:?}", first, rest),
    }

    // Head and tail destructuring
    match &arr[..] {
        [] => println!("Empty"),
        [head, tail @ ..] => {
            println!("Head: {}", head);
            println!("Tail: {:?}", tail);
        }
    }

    // Multiple element patterns
    match &arr[..] {
        [a, b] => println!("Two elements: {}, {}", a, b),
        [a, b, c] => println!("Three elements: {}, {}, {}", a, b, c),
        [a, b, rest @ ..] => println!("At least two: {}, {}, rest: {:?}", a, b, rest),
    }
}

// Reference destructuring
fn destructure_references() {
    let point = Point { x: 1.0, y: 2.0 };
    let point_ref = &point;

    // Destructuring references
    match point_ref {
        Point { x, y } => println!("Reference point: ({}, {})", x, y),
    }

    // Pattern with ref
    match point {
        Point { ref x, ref y } => {
            println!("Ref pattern: ({}, {})", x, y);
        }
    }

    // Mutable reference destructuring
    let mut mutable_point = Point { x: 3.0, y: 4.0 };
    match &mut mutable_point {
        Point { x, y } => {
            *x += 1.0;
            *y += 1.0;
        }
    }
}

// Option and Result destructuring
fn destructure_options_results() {
    let maybe_number: Option<i32> = Some(42);
    let maybe_string: Option<String> = None;

    // Option destructuring
    match maybe_number {
        Some(n) => println!("Number: {}", n),
        None => println!("No number"),
    }

    // Nested Option destructuring
    let nested_option: Option<Option<i32>> = Some(Some(100));
    match nested_option {
        Some(Some(value)) => println!("Nested value: {}", value),
        Some(None) => println!("Inner None"),
        None => println!("Outer None"),
    }

    // Result destructuring
    let result: Result<i32, String> = Ok(42);
    match result {
        Ok(value) => println!("Success: {}", value),
        Err(error) => println!("Error: {}", error),
    }
}

// Vector and collection destructuring
fn destructure_collections() {
    let vec = vec![1, 2, 3, 4, 5];

    // Vector slice patterns
    match vec.as_slice() {
        [] => println!("Empty vector"),
        [single] => println!("Single element vector: {}", single),
        [first, second] => println!("Two element vector: {}, {}", first, second),
        [first, middle @ .., last] => {
            println!("First: {}, Last: {}, Middle: {:?}", first, last, middle);
        }
    }

    // Iterator destructuring
    let mut iter = vec.iter();
    while let Some(value) = iter.next() {
        println!("Iterator value: {}", value);
    }
}

// Advanced pattern combinations
fn advanced_pattern_combinations() {
    #[derive(Debug)]
    enum Data {
        Text(String),
        Number(i32),
        Pair(i32, i32),
        Complex { id: u64, values: Vec<i32> },
    }

    let data_items = vec![
        Data::Text("hello".to_string()),
        Data::Number(42),
        Data::Pair(1, 2),
        Data::Complex {
            id: 123,
            values: vec![1, 2, 3],
        },
    ];

    for item in data_items {
        match item {
            Data::Text(ref s) if s.len() > 5 => {
                println!("Long text: {}", s);
            }
            Data::Text(s) => {
                println!("Short text: {}", s);
            }
            Data::Number(n) if n > 0 => {
                println!("Positive number: {}", n);
            }
            Data::Number(n) => {
                println!("Non-positive number: {}", n);
            }
            Data::Pair(x, y) if x == y => {
                println!("Equal pair: ({}, {})", x, y);
            }
            Data::Pair(x, y) => {
                println!("Different pair: ({}, {})", x, y);
            }
            Data::Complex { id, values } if values.len() > 2 => {
                println!("Complex with many values: {} - {:?}", id, values);
            }
            Data::Complex { id, values } => {
                println!("Simple complex: {} - {:?}", id, values);
            }
        }
    }
}

// String pattern matching
fn string_pattern_matching() {
    let text = "hello world";

    match text {
        "hello" => println!("Simple hello"),
        s if s.starts_with("hello") => println!("Starts with hello: {}", s),
        s if s.contains("world") => println!("Contains world: {}", s),
        s if s.len() > 10 => println!("Long string: {}", s),
        _ => println!("Other string: {}", text),
    }

    // Character pattern matching
    for ch in text.chars() {
        match ch {
            'a'..='z' => println!("Lowercase: {}", ch),
            'A'..='Z' => println!("Uppercase: {}", ch),
            '0'..='9' => println!("Digit: {}", ch),
            ' ' => println!("Space"),
            _ => println!("Other character: {}", ch),
        }
    }
}
""",
    )

    run_updater(rust_pattern_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    destructure_calls = [
        call
        for call in calls
        if "destructure" in str(call) or "advanced_pattern" in str(call)
    ]
    assert len(destructure_calls) > 0, "Destructuring functions should be detected"


def test_pattern_guards_and_ranges(
    rust_pattern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test pattern guards, range patterns, and conditional matching."""
    test_file = rust_pattern_project / "guards_ranges.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Pattern guards with complex conditions
fn classify_number_with_guards(n: i32) -> &'static str {
    match n {
        x if x < 0 => "negative",
        0 => "zero",
        x if x > 0 && x <= 10 => "small positive",
        x if x > 10 && x <= 100 => "medium positive",
        x if x > 100 && x % 2 == 0 => "large even",
        x if x > 100 && x % 2 == 1 => "large odd",
        _ => "other",
    }
}

// Range patterns
fn classify_by_range(value: i32) -> &'static str {
    match value {
        i32::MIN..=-1 => "negative",
        0 => "zero",
        1..=10 => "single digit positive",
        11..=99 => "double digit",
        100..=999 => "triple digit",
        1000..=i32::MAX => "four or more digits",
    }
}

// Character range patterns
fn classify_character(ch: char) -> &'static str {
    match ch {
        'a'..='z' => "lowercase letter",
        'A'..='Z' => "uppercase letter",
        '0'..='9' => "digit",
        '\t' | '\n' | '\r' | ' ' => "whitespace",
        '!'..='/' | ':'..='@' | '['..='`' | '{'..='~' => "punctuation",
        _ => "other",
    }
}

// Multiple guard conditions
fn complex_guards(x: i32, y: i32) -> String {
    match (x, y) {
        (a, b) if a == b => format!("Equal: {} == {}", a, b),
        (a, b) if a > b && a - b < 10 => format!("Close: {} > {} (diff < 10)", a, b),
        (a, b) if a < b && b - a < 10 => format!("Close: {} < {} (diff < 10)", a, b),
        (a, b) if a > 0 && b > 0 => format!("Both positive: {}, {}", a, b),
        (a, b) if a < 0 && b < 0 => format!("Both negative: {}, {}", a, b),
        (a, b) if a * b < 0 => format!("Different signs: {}, {}", a, b),
        (a, b) => format!("Other case: {}, {}", a, b),
    }
}

// Guards with destructuring
#[derive(Debug)]
struct Point {
    x: i32,
    y: i32,
}

fn classify_point(point: Point) -> &'static str {
    match point {
        Point { x: 0, y: 0 } => "origin",
        Point { x, y } if x == y => "diagonal",
        Point { x: 0, y } if y > 0 => "positive y-axis",
        Point { x: 0, y } if y < 0 => "negative y-axis",
        Point { x, y: 0 } if x > 0 => "positive x-axis",
        Point { x, y: 0 } if x < 0 => "negative x-axis",
        Point { x, y } if x > 0 && y > 0 => "first quadrant",
        Point { x, y } if x < 0 && y > 0 => "second quadrant",
        Point { x, y } if x < 0 && y < 0 => "third quadrant",
        Point { x, y } if x > 0 && y < 0 => "fourth quadrant",
        _ => "undefined",
    }
}

// Option guards
fn process_optional_value(opt: Option<i32>) -> String {
    match opt {
        Some(x) if x > 100 => format!("Large value: {}", x),
        Some(x) if x > 0 => format!("Positive value: {}", x),
        Some(x) if x == 0 => "Zero value".to_string(),
        Some(x) if x < 0 => format!("Negative value: {}", x),
        None => "No value".to_string(),
    }
}

// Result guards
fn process_result_with_guards(result: Result<i32, String>) -> String {
    match result {
        Ok(x) if x > 0 => format!("Success with positive: {}", x),
        Ok(x) if x == 0 => "Success with zero".to_string(),
        Ok(x) => format!("Success with negative: {}", x),
        Err(e) if e.len() > 10 => format!("Long error: {}", e),
        Err(e) => format!("Short error: {}", e),
    }
}

// Vector guards
fn analyze_vector(vec: Vec<i32>) -> String {
    match vec.as_slice() {
        [] => "Empty vector".to_string(),
        [single] if *single > 0 => format!("Single positive: {}", single),
        [single] => format!("Single non-positive: {}", single),
        [first, second] if first == second => {
            format!("Two equal elements: {}", first)
        }
        [first, second] if first > second => {
            format!("Descending pair: {} > {}", first, second)
        }
        [first, second] => {
            format!("Ascending pair: {} <= {}", first, second)
        }
        slice if slice.len() > 10 => {
            format!("Large vector with {} elements", slice.len())
        }
        slice if slice.iter().all(|&x| x > 0) => {
            format!("All positive vector: {:?}", slice)
        }
        slice if slice.iter().any(|&x| x < 0) => {
            format!("Contains negative: {:?}", slice)
        }
        slice => format!("Other vector: {:?}", slice),
    }
}

// Nested guards
enum NestedData {
    Level1(Level2),
    Simple(i32),
}

enum Level2 {
    Level3(i32),
    Data(String),
}

fn process_nested_with_guards(data: NestedData) -> String {
    match data {
        NestedData::Simple(x) if x > 100 => format!("Large simple: {}", x),
        NestedData::Simple(x) => format!("Small simple: {}", x),
        NestedData::Level1(Level2::Level3(x)) if x > 0 => {
            format!("Positive nested: {}", x)
        }
        NestedData::Level1(Level2::Level3(x)) => {
            format!("Non-positive nested: {}", x)
        }
        NestedData::Level1(Level2::Data(s)) if s.len() > 5 => {
            format!("Long nested string: {}", s)
        }
        NestedData::Level1(Level2::Data(s)) => {
            format!("Short nested string: {}", s)
        }
    }
}

// Guards with references
fn process_string_ref(s: &str) -> &'static str {
    match s {
        s if s.is_empty() => "empty",
        s if s.len() == 1 => "single character",
        s if s.chars().all(|c| c.is_ascii_digit()) => "all digits",
        s if s.chars().all(|c| c.is_ascii_alphabetic()) => "all letters",
        s if s.starts_with("http://") || s.starts_with("https://") => "URL",
        s if s.contains('@') && s.contains('.') => "email-like",
        _ => "other string",
    }
}

// Floating point range guards
fn classify_float(f: f64) -> &'static str {
    match f {
        f if f.is_nan() => "NaN",
        f if f.is_infinite() && f.is_sign_positive() => "positive infinity",
        f if f.is_infinite() && f.is_sign_negative() => "negative infinity",
        f if f == 0.0 => "zero",
        f if f > 0.0 && f < 1.0 => "small positive",
        f if f >= 1.0 && f <= 10.0 => "medium positive",
        f if f > 10.0 => "large positive",
        f if f < 0.0 && f > -1.0 => "small negative",
        f if f <= -1.0 && f >= -10.0 => "medium negative",
        f if f < -10.0 => "large negative",
        _ => "other",
    }
}
""",
    )

    run_updater(rust_pattern_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    guard_calls = [
        call
        for call in calls
        if "classify" in str(call) or "complex_guards" in str(call)
    ]
    assert len(guard_calls) > 0, "Pattern guard functions should be detected"


def test_advanced_if_let_while_let(
    rust_pattern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test if let, while let, and other pattern matching constructs."""
    test_file = rust_pattern_project / "if_while_let.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::collections::HashMap;

// if let patterns
fn process_optional_data() {
    let maybe_value: Option<i32> = Some(42);

    // Basic if let
    if let Some(value) = maybe_value {
        println!("Got value: {}", value);
    } else {
        println!("No value");
    }

    // Nested if let
    let nested_option: Option<Option<String>> = Some(Some("hello".to_string()));
    if let Some(inner) = nested_option {
        if let Some(text) = inner {
            println!("Nested text: {}", text);
        }
    }

    // if let with destructuring
    let point_opt: Option<(i32, i32)> = Some((10, 20));
    if let Some((x, y)) = point_opt {
        println!("Point coordinates: ({}, {})", x, y);
    }
}

// while let patterns
fn process_iterator() {
    let mut vec = vec![1, 2, 3, 4, 5];
    let mut iter = vec.iter();

    // Basic while let
    while let Some(value) = iter.next() {
        println!("Iterator value: {}", value);
    }

    // while let with mutable iterator
    let mut stack = vec![1, 2, 3, 4, 5];
    while let Some(top) = stack.pop() {
        println!("Stack top: {}", top);
    }
}

// Complex if let chains
fn process_result_chain() {
    let results: Vec<Result<i32, String>> = vec![
        Ok(1),
        Err("error1".to_string()),
        Ok(2),
        Ok(3),
    ];

    for result in results {
        if let Ok(value) = result {
            if value > 2 {
                println!("Large value: {}", value);
            } else {
                println!("Small value: {}", value);
            }
        } else if let Err(error) = result {
            println!("Error occurred: {}", error);
        }
    }
}

// Enum if let patterns
#[derive(Debug)]
enum Event {
    KeyPress(char),
    MouseClick { x: i32, y: i32, button: MouseButton },
    WindowResize { width: u32, height: u32 },
    Timer(u64),
}

#[derive(Debug)]
enum MouseButton {
    Left,
    Right,
    Middle,
}

fn handle_events(events: Vec<Event>) {
    for event in events {
        if let Event::KeyPress(key) = event {
            println!("Key pressed: {}", key);
        } else if let Event::MouseClick { x, y, button } = event {
            println!("Mouse click at ({}, {}) with {:?}", x, y, button);
        } else if let Event::WindowResize { width, height } = event {
            println!("Window resized to {}x{}", width, height);
        } else if let Event::Timer(elapsed) = event {
            println!("Timer fired: {} ms", elapsed);
        }
    }
}

// Multiple pattern if let
fn process_multiple_patterns() {
    let data: Result<Option<i32>, String> = Ok(Some(42));

    // Nested if let
    if let Ok(maybe_value) = data {
        if let Some(value) = maybe_value {
            println!("Success with value: {}", value);
        } else {
            println!("Success but no value");
        }
    } else {
        println!("Error in data");
    }

    // Alternative: match for complex patterns
    match data {
        Ok(Some(value)) => println!("Direct success: {}", value),
        Ok(None) => println!("Success, no value"),
        Err(error) => println!("Error: {}", error),
    }
}

// HashMap if let patterns
fn process_hashmap() {
    let mut map = HashMap::new();
    map.insert("key1", 100);
    map.insert("key2", 200);
    map.insert("key3", 300);

    // if let with HashMap get
    if let Some(value) = map.get("key1") {
        println!("Found key1: {}", value);
    }

    // if let with HashMap remove
    if let Some(removed) = map.remove("key2") {
        println!("Removed key2: {}", removed);
    }

    // Iterate with if let
    for (key, value) in &map {
        if let Ok(parsed) = key.parse::<i32>() {
            println!("Numeric key {}: {}", parsed, value);
        } else {
            println!("String key {}: {}", key, value);
        }
    }
}

// Custom iterator with while let
struct Counter {
    current: usize,
    max: usize,
}

impl Counter {
    fn new(max: usize) -> Counter {
        Counter { current: 0, max }
    }
}

impl Iterator for Counter {
    type Item = usize;

    fn next(&mut self) -> Option<Self::Item> {
        if self.current < self.max {
            let current = self.current;
            self.current += 1;
            Some(current)
        } else {
            None
        }
    }
}

fn use_custom_iterator() {
    let mut counter = Counter::new(5);

    while let Some(value) = counter.next() {
        println!("Counter: {}", value);
    }
}

// Chained if let patterns
fn process_chained_patterns() {
    let complex_data: Result<Option<Vec<i32>>, String> = Ok(Some(vec![1, 2, 3]));

    if let Ok(maybe_vec) = complex_data {
        if let Some(vec) = maybe_vec {
            if let Some(first) = vec.first() {
                println!("First element: {}", first);

                if let Some(last) = vec.last() {
                    println!("Last element: {}", last);
                }
            }
        }
    }
}

// if let guards
fn process_with_guards() {
    let values = vec![Some(1), Some(2), None, Some(3), Some(4)];

    for opt_value in values {
        if let Some(value) = opt_value {
            if value > 2 {
                println!("Large value: {}", value);
            } else {
                println!("Small value: {}", value);
            }
        } else {
            println!("No value");
        }
    }
}

// Combining if let with regular if
fn mixed_conditions() {
    let maybe_number: Option<i32> = Some(42);
    let flag = true;

    if flag {
        if let Some(n) = maybe_number {
            println!("Flag is true and number is: {}", n);
        } else {
            println!("Flag is true but no number");
        }
    } else if let Some(n) = maybe_number {
        println!("Flag is false but number is: {}", n);
    } else {
        println!("Flag is false and no number");
    }
}

// Reference patterns in if let
fn process_string_refs() {
    let maybe_string: Option<String> = Some("hello world".to_string());

    if let Some(ref s) = maybe_string {
        println!("String length: {}", s.len());
        // `s` is &String, `maybe_string` is still owned
    }

    // maybe_string is still available here
    if let Some(s) = maybe_string {
        println!("Owned string: {}", s);
        // `s` is String, `maybe_string` is moved
    }
}
""",
    )

    run_updater(rust_pattern_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    let_calls = [
        call
        for call in calls
        if "process_" in str(call) or "handle_events" in str(call)
    ]
    assert len(let_calls) > 0, "If let and while let functions should be detected"


def test_macro_pattern_matching(
    rust_pattern_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test pattern matching within macros and macro patterns."""
    test_file = rust_pattern_project / "macro_patterns.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Macro with pattern matching
macro_rules! match_type {
    ($value:expr, i32) => {
        println!("It's an i32: {}", $value);
    };
    ($value:expr, f64) => {
        println!("It's an f64: {}", $value);
    };
    ($value:expr, String) => {
        println!("It's a String: {}", $value);
    };
    ($value:expr, $t:ty) => {
        println!("It's some other type: {:?}", $value);
    };
}

// Macro for pattern matching with guards
macro_rules! conditional_match {
    ($value:expr, positive) => {
        if $value > 0 {
            println!("Positive value: {}", $value);
        } else {
            println!("Not positive: {}", $value);
        }
    };
    ($value:expr, negative) => {
        if $value < 0 {
            println!("Negative value: {}", $value);
        } else {
            println!("Not negative: {}", $value);
        }
    };
    ($value:expr, zero) => {
        if $value == 0 {
            println!("Zero value");
        } else {
            println!("Not zero: {}", $value);
        }
    };
}

// Macro for destructuring patterns
macro_rules! destructure_and_print {
    (($x:expr, $y:expr)) => {
        println!("Tuple: ({}, {})", $x, $y);
    };
    ([$($elements:expr),*]) => {
        println!("Array: [{}]", stringify!($($elements),*));
    };
    ({$field1:ident: $value1:expr, $field2:ident: $value2:expr}) => {
        println!("Struct: {} = {}, {} = {}",
                stringify!($field1), $value1,
                stringify!($field2), $value2);
    };
}

// Pattern matching in macro arms
macro_rules! handle_option {
    (Some($value:expr)) => {
        println!("Some variant with value: {}", $value);
    };
    (None) => {
        println!("None variant");
    };
    ($other:expr) => {
        match $other {
            Some(v) => println!("Dynamic Some: {}", v),
            None => println!("Dynamic None"),
        }
    };
}

// Complex pattern matching macro
macro_rules! process_data {
    // Pattern for single value
    (single $value:expr) => {
        println!("Processing single value: {}", $value);
    };

    // Pattern for pair
    (pair $x:expr, $y:expr) => {
        println!("Processing pair: ({}, {})", $x, $y);
    };

    // Pattern for list
    (list $($items:expr),+) => {
        println!("Processing list:");
        $(println!("  Item: {}", $items);)+
    };

    // Pattern for named values
    (named $($name:ident = $value:expr),+) => {
        println!("Processing named values:");
        $(println!("  {} = {}", stringify!($name), $value);)+
    };

    // Pattern with conditions
    (conditional $value:expr, if $condition:expr) => {
        if $condition {
            println!("Condition met, processing: {}", $value);
        } else {
            println!("Condition not met, skipping: {}", $value);
        }
    };
}

// Recursive pattern matching macro
macro_rules! count_items {
    () => (0);
    ($head:expr) => (1);
    ($head:expr, $($tail:expr),+) => (1 + count_items!($($tail),+));
}

// Pattern matching with different token types
macro_rules! token_patterns {
    // Identifier pattern
    ($name:ident) => {
        println!("Identifier: {}", stringify!($name));
    };

    // Literal pattern
    ($lit:literal) => {
        println!("Literal: {}", $lit);
    };

    // Expression pattern
    ($expr:expr) => {
        println!("Expression result: {}", $expr);
    };

    // Type pattern
    ($type:ty) => {
        println!("Type: {}", stringify!($type));
    };

    // Pattern pattern
    ($pat:pat) => {
        match 42 {
            $pat => println!("Pattern matched!"),
            _ => println!("Pattern didn't match"),
        }
    };

    // Statement pattern
    ($stmt:stmt) => {
        $stmt
        println!("Statement executed");
    };
}

// Macro that generates pattern matching code
macro_rules! generate_matcher {
    ($enum_name:ident, $($variant:ident),+) => {
        fn match_enum(value: $enum_name) -> &'static str {
            match value {
                $(
                    $enum_name::$variant => stringify!($variant),
                )+
            }
        }
    };
}

#[derive(Debug)]
enum Color {
    Red,
    Green,
    Blue,
    Yellow,
}

generate_matcher!(Color, Red, Green, Blue, Yellow);

// Test functions using the macros
fn test_macro_patterns() {
    // Test match_type macro
    match_type!(42, i32);
    match_type!(3.14, f64);
    match_type!("hello".to_string(), String);

    // Test conditional_match macro
    conditional_match!(5, positive);
    conditional_match!(-3, negative);
    conditional_match!(0, zero);

    // Test destructure_and_print macro
    destructure_and_print!((1, 2));
    destructure_and_print!([1, 2, 3, 4]);
    destructure_and_print!({x: 10, y: 20});

    // Test handle_option macro
    handle_option!(Some(42));
    handle_option!(None);
    let opt = Some(100);
    handle_option!(opt);

    // Test process_data macro
    process_data!(single 42);
    process_data!(pair 1, 2);
    process_data!(list 1, 2, 3, 4);
    process_data!(named x = 10, y = 20, z = 30);
    process_data!(conditional 42, if true);

    // Test count_items macro
    let count1 = count_items!();
    let count2 = count_items!(1);
    let count3 = count_items!(1, 2, 3, 4, 5);
    println!("Counts: {}, {}, {}", count1, count2, count3);

    // Test token_patterns macro
    token_patterns!(my_identifier);
    token_patterns!(42);
    token_patterns!(2 + 3);
    token_patterns!(i32);
    token_patterns!(42);
    token_patterns!(let x = 10;);

    // Test generated matcher
    let color = Color::Red;
    let color_name = match_enum(color);
    println!("Color: {}", color_name);
}

// Macro with advanced pattern syntax
macro_rules! advanced_patterns {
    // Optional pattern
    ($value:expr $(, $optional:expr)?) => {
        println!("Value: {}", $value);
        $(println!("Optional: {}", $optional);)?
    };

    // Repetition with separator
    (sum $($num:expr),+ $(,)?) => {
        {
            let mut total = 0;
            $(total += $num;)+
            total
        }
    };

    // Nested repetition
    (matrix $($($element:expr),+);+) => {
        {
            let matrix = vec![
                $(vec![$($element),+]),+
            ];
            matrix
        }
    };
}

fn test_advanced_macro_patterns() {
    // Test optional pattern
    advanced_patterns!(42);
    advanced_patterns!(42, 100);

    // Test sum pattern
    let sum1 = advanced_patterns!(sum 1, 2, 3, 4, 5);
    let sum2 = advanced_patterns!(sum 10, 20, 30,);
    println!("Sums: {}, {}", sum1, sum2);

    // Test matrix pattern
    let matrix = advanced_patterns!(matrix 1, 2, 3; 4, 5, 6; 7, 8, 9);
    println!("Matrix: {:?}", matrix);
}
""",
    )

    run_updater(rust_pattern_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    macro_calls = [
        call
        for call in calls
        if "test_macro" in str(call) or "generate_matcher" in str(call)
    ]
    assert len(macro_calls) > 0, "Macro pattern functions should be detected"
