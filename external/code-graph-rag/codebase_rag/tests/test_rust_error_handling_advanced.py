from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_error_project(temp_repo: Path) -> Path:
    """Create a Rust project with error handling examples."""
    project_path = temp_repo / "rust_error_test"
    project_path.mkdir()

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""
[package]
name = "rust_error_test"
version = "0.1.0"
edition = "2021"

[dependencies]
thiserror = "1.0"
anyhow = "1.0"
serde = { version = "1.0", features = ["derive"] }
""",
    )

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Error handling test crate"
    )

    return project_path


def test_result_option_basics(
    rust_error_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic Result and Option handling."""
    test_file = rust_error_project / "result_option.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
fn divide(dividend: f64, divisor: f64) -> Result<f64, String> {
    if divisor == 0.0 {
        Err("Cannot divide by zero".to_string())
    } else {
        Ok(dividend / divisor)
    }
}

fn find_item(items: &[i32], target: i32) -> Option<usize> {
    for (index, &item) in items.iter().enumerate() {
        if item == target {
            return Some(index);
        }
    }
    None
}

fn handle_results() {
    match divide(10.0, 2.0) {
        Ok(result) => println!("Result: {}", result),
        Err(error) => println!("Error: {}", error),
    }

    if let Ok(result) = divide(10.0, 0.0) {
        println!("This won't print: {}", result);
    } else {
        println!("Division by zero handled");
    }
}

fn handle_options() {
    let numbers = vec![1, 2, 3, 4, 5];

    match find_item(&numbers, 3) {
        Some(index) => println!("Found at index: {}", index),
        None => println!("Item not found"),
    }

    if let Some(index) = find_item(&numbers, 6) {
        println!("Found at index: {}", index);
    } else {
        println!("Item 6 not found");
    }
}

fn chaining_operations() -> Result<i32, String> {
    "42"
        .parse::<i32>()
        .map_err(|e| format!("Parse error: {}", e))?
        .checked_add(8)
        .ok_or_else(|| "Overflow error".to_string())
}

fn option_combinators() {
    let x = Some(2);
    let y = Some(4);

    let result = x
        .and_then(|x| y.map(|y| x + y))
        .or_else(|| Some(0))
        .unwrap_or(10);

    println!("Result: {}", result);
}

fn result_combinators() -> Result<String, Box<dyn std::error::Error>> {
    let num: i32 = "42".parse()?;
    let doubled = num.checked_mul(2)
        .ok_or("Multiplication overflow")?;
    Ok(format!("Result: {}", doubled))
}
""",
    )

    run_updater(rust_error_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    result_calls = [
        call for call in calls if "divide" in str(call) or "find_item" in str(call)
    ]
    assert len(result_calls) > 0, "Result/Option functions should be detected"


def test_custom_error_types(
    rust_error_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test custom error type definitions."""
    test_file = rust_error_project / "custom_errors.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::fmt;
use std::error::Error;

#[derive(Debug)]
enum MathError {
    DivisionByZero,
    NegativeSquareRoot,
    Overflow,
}

impl fmt::Display for MathError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            MathError::DivisionByZero => write!(f, "Cannot divide by zero"),
            MathError::NegativeSquareRoot => write!(f, "Cannot take square root of negative number"),
            MathError::Overflow => write!(f, "Mathematical overflow occurred"),
        }
    }
}

impl Error for MathError {}

fn safe_divide(dividend: f64, divisor: f64) -> Result<f64, MathError> {
    if divisor == 0.0 {
        Err(MathError::DivisionByZero)
    } else {
        Ok(dividend / divisor)
    }
}

fn safe_sqrt(value: f64) -> Result<f64, MathError> {
    if value < 0.0 {
        Err(MathError::NegativeSquareRoot)
    } else {
        Ok(value.sqrt())
    }
}

fn safe_multiply(a: i32, b: i32) -> Result<i32, MathError> {
    a.checked_mul(b).ok_or(MathError::Overflow)
}

#[derive(Debug)]
struct ValidationError {
    field: String,
    message: String,
}

impl fmt::Display for ValidationError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "Validation error in field '{}': {}", self.field, self.message)
    }
}

impl Error for ValidationError {}

impl ValidationError {
    fn new(field: &str, message: &str) -> Self {
        ValidationError {
            field: field.to_string(),
            message: message.to_string(),
        }
    }
}

fn validate_email(email: &str) -> Result<(), ValidationError> {
    if !email.contains('@') {
        return Err(ValidationError::new("email", "Must contain @ symbol"));
    }

    if email.len() < 5 {
        return Err(ValidationError::new("email", "Must be at least 5 characters"));
    }

    Ok(())
}

fn validate_age(age: i32) -> Result<(), ValidationError> {
    if age < 0 {
        return Err(ValidationError::new("age", "Must be non-negative"));
    }

    if age > 150 {
        return Err(ValidationError::new("age", "Must be realistic"));
    }

    Ok(())
}

use thiserror::Error;

#[derive(Error, Debug)]
enum ApiError {
    #[error("Network error: {message}")]
    Network { message: String },

    #[error("Authentication failed")]
    Auth,

    #[error("Resource not found: {resource}")]
    NotFound { resource: String },

    #[error("Rate limit exceeded. Try again in {retry_after}s")]
    RateLimit { retry_after: u64 },

    #[error("Validation error")]
    Validation(#[from] ValidationError),

    #[error("IO error")]
    Io(#[from] std::io::Error),
}

fn make_api_request(endpoint: &str) -> Result<String, ApiError> {
    if endpoint.is_empty() {
        return Err(ApiError::Network {
            message: "Empty endpoint".to_string(),
        });
    }

    if endpoint == "/protected" {
        return Err(ApiError::Auth);
    }

    if endpoint == "/missing" {
        return Err(ApiError::NotFound {
            resource: endpoint.to_string(),
        });
    }

    Ok(format!("Response from {}", endpoint))
}
""",
    )

    run_updater(rust_error_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    error_calls = [
        call for call in calls if "MathError" in str(call) or "ApiError" in str(call)
    ]
    assert len(error_calls) > 0, "Custom error types should be detected"


def test_error_propagation(
    rust_error_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test error propagation with ? operator and error conversion."""
    test_file = rust_error_project / "error_propagation.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::fs::File;
use std::io::{self, Read};
use std::num::ParseIntError;
use std::fmt;
use std::error::Error;

#[derive(Debug)]
enum AppError {
    Io(io::Error),
    Parse(ParseIntError),
    Custom(String),
}

impl fmt::Display for AppError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            AppError::Io(err) => write!(f, "IO error: {}", err),
            AppError::Parse(err) => write!(f, "Parse error: {}", err),
            AppError::Custom(msg) => write!(f, "Custom error: {}", msg),
        }
    }
}

impl Error for AppError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            AppError::Io(err) => Some(err),
            AppError::Parse(err) => Some(err),
            AppError::Custom(_) => None,
        }
    }
}

impl From<io::Error> for AppError {
    fn from(error: io::Error) -> Self {
        AppError::Io(error)
    }
}

impl From<ParseIntError> for AppError {
    fn from(error: ParseIntError) -> Self {
        AppError::Parse(error)
    }
}

fn read_file_contents(filename: &str) -> Result<String, AppError> {
    let mut file = File::open(filename)?;
    let mut contents = String::new();
    file.read_to_string(&mut contents)?;
    Ok(contents)
}

fn parse_numbers_from_file(filename: &str) -> Result<Vec<i32>, AppError> {
    let contents = read_file_contents(filename)?;

    let numbers: Result<Vec<i32>, ParseIntError> = contents
        .lines()
        .map(|line| line.trim().parse::<i32>())
        .collect();

    Ok(numbers?)
}

fn calculate_sum(filename: &str) -> Result<i32, AppError> {
    let numbers = parse_numbers_from_file(filename)?;

    if numbers.is_empty() {
        return Err(AppError::Custom("No numbers found".to_string()));
    }

    let sum = numbers.iter().sum();
    Ok(sum)
}

fn nested_operations() -> Result<String, Box<dyn Error>> {
    let result = calculate_sum("numbers.txt")?;
    let doubled = result.checked_mul(2)
        .ok_or("Integer overflow")?;

    Ok(format!("Final result: {}", doubled))
}

fn multiple_error_sources() -> Result<(), Box<dyn Error>> {
    // This could fail with IO error
    let _file_contents = read_file_contents("config.txt")?;

    // This could fail with parse error
    let _number: i32 = "not_a_number".parse()?;

    // This could fail with custom error
    calculate_sum("data.txt")?;

    Ok(())
}

use anyhow::{Result, Context, bail, ensure};

fn anyhow_examples() -> Result<()> {
    let content = std::fs::read_to_string("config.toml")
        .context("Failed to read config file")?;

    ensure!(!content.is_empty(), "Config file is empty");

    let lines: Vec<&str> = content.lines().collect();
    if lines.len() < 2 {
        bail!("Config file must have at least 2 lines");
    }

    Ok(())
}

fn error_chain_example() -> Result<()> {
    let data = read_file_contents("important_data.txt")
        .context("Failed to load critical application data")?;

    let parsed_data = parse_numbers_from_file("numbers.txt")
        .context("Failed to parse numerical data for calculations")?;

    ensure!(!parsed_data.is_empty(), "No data available for processing");

    Ok(())
}

fn recoverable_errors() -> Result<Vec<i32>, AppError> {
    let primary_result = parse_numbers_from_file("primary.txt");

    match primary_result {
        Ok(data) => Ok(data),
        Err(AppError::Io(_)) => {
            // Try fallback file
            println!("Primary file not found, trying backup...");
            parse_numbers_from_file("backup.txt")
        },
        Err(other) => Err(other),
    }
}
""",
    )

    run_updater(rust_error_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    propagation_calls = [
        call
        for call in calls
        if "parse_numbers_from_file" in str(call) or "calculate_sum" in str(call)
    ]
    assert len(propagation_calls) > 0, "Error propagation functions should be detected"


def test_panic_handling(
    rust_error_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test panic handling and recovery."""
    test_file = rust_error_project / "panic_handling.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::panic;

fn might_panic(should_panic: bool) {
    if should_panic {
        panic!("Something went wrong!");
    }
    println!("Everything is fine");
}

fn catch_panic() {
    let result = panic::catch_unwind(|| {
        might_panic(true);
    });

    match result {
        Ok(_) => println!("No panic occurred"),
        Err(_) => println!("Panic caught and handled"),
    }
}

fn panic_with_custom_hook() {
    panic::set_hook(Box::new(|info| {
        println!("Custom panic handler: {}", info);
    }));

    let _ = panic::catch_unwind(|| {
        panic!("This will be caught by custom handler");
    });

    // Reset to default panic handler
    let _ = panic::take_hook();
}

fn unwind_safe_operations() {
    use std::panic::UnwindSafe;

    fn safe_operation<F>(f: F) -> Result<(), String>
    where
        F: FnOnce() + UnwindSafe,
    {
        match panic::catch_unwind(f) {
            Ok(_) => Ok(()),
            Err(_) => Err("Operation panicked".to_string()),
        }
    }

    let data = vec![1, 2, 3, 4, 5];
    let result = safe_operation(|| {
        // This won't panic
        let _sum: i32 = data.iter().sum();
    });

    println!("Safe operation result: {:?}", result);
}

fn abort_vs_unwind() {
    // Different panic strategies can be configured in Cargo.toml:
    // [profile.release]
    // panic = "abort"  # Don't unwind, just abort
    // panic = "unwind" # Default: unwind the stack

    println!("This example shows different panic handling strategies");
}

use std::thread;
use std::time::Duration;

fn panic_in_threads() {
    let handle = thread::spawn(|| {
        thread::sleep(Duration::from_millis(100));
        panic!("Thread panic!");
    });

    match handle.join() {
        Ok(_) => println!("Thread completed successfully"),
        Err(_) => println!("Thread panicked"),
    }
}

fn graceful_shutdown() {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    let running = Arc::new(AtomicBool::new(true));
    let r = running.clone();

    let handle = thread::spawn(move || {
        while r.load(Ordering::SeqCst) {
            // Simulate work
            thread::sleep(Duration::from_millis(10));

            // Check for panic condition
            if rand::random::<f64>() < 0.001 {
                panic!("Random panic occurred!");
            }
        }
    });

    thread::sleep(Duration::from_millis(500));
    running.store(false, Ordering::SeqCst);

    match handle.join() {
        Ok(_) => println!("Worker thread shut down gracefully"),
        Err(_) => println!("Worker thread panicked, but we handled it"),
    }
}
""",
    )

    run_updater(rust_error_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    panic_calls = [
        call
        for call in calls
        if "catch_panic" in str(call) or "panic_in_threads" in str(call)
    ]
    assert len(panic_calls) > 0, "Panic handling functions should be detected"


def test_error_handling_patterns(
    rust_error_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test advanced error handling patterns."""
    test_file = rust_error_project / "error_patterns.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::collections::HashMap;

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

struct Database {
    data: HashMap<String, String>,
}

impl Database {
    fn new() -> Self {
        Database {
            data: HashMap::new(),
        }
    }

    fn get(&self, key: &str) -> Option<&String> {
        self.data.get(key)
    }

    fn set(&mut self, key: String, value: String) {
        self.data.insert(key, value);
    }
}

fn transactional_operation(db: &mut Database) -> Result<()> {
    // Simulate a multi-step operation
    db.set("step1".to_string(), "completed".to_string());

    // This might fail
    if db.get("enable_failure").is_some() {
        return Err("Simulated failure in step 2".into());
    }

    db.set("step2".to_string(), "completed".to_string());
    db.set("transaction".to_string(), "success".to_string());

    Ok(())
}

fn retry_pattern<F, T, E>(mut operation: F, max_retries: usize) -> std::result::Result<T, E>
where
    F: FnMut() -> std::result::Result<T, E>,
{
    let mut retries = 0;

    loop {
        match operation() {
            Ok(result) => return Ok(result),
            Err(error) => {
                retries += 1;
                if retries >= max_retries {
                    return Err(error);
                }
                println!("Retry {}/{}", retries, max_retries);
            }
        }
    }
}

fn circuit_breaker_pattern() {
    #[derive(Debug)]
    enum CircuitState {
        Closed,
        Open,
        HalfOpen,
    }

    struct CircuitBreaker {
        state: CircuitState,
        failure_count: usize,
        failure_threshold: usize,
        success_count: usize,
    }

    impl CircuitBreaker {
        fn new(failure_threshold: usize) -> Self {
            CircuitBreaker {
                state: CircuitState::Closed,
                failure_count: 0,
                failure_threshold,
                success_count: 0,
            }
        }

        fn call<F, T, E>(&mut self, operation: F) -> std::result::Result<T, String>
        where
            F: FnOnce() -> std::result::Result<T, E>,
            E: std::fmt::Display,
        {
            match self.state {
                CircuitState::Open => Err("Circuit breaker is open".to_string()),
                CircuitState::Closed | CircuitState::HalfOpen => {
                    match operation() {
                        Ok(result) => {
                            self.on_success();
                            Ok(result)
                        }
                        Err(error) => {
                            self.on_failure();
                            Err(format!("Operation failed: {}", error))
                        }
                    }
                }
            }
        }

        fn on_success(&mut self) {
            self.failure_count = 0;
            self.success_count += 1;

            if matches!(self.state, CircuitState::HalfOpen) && self.success_count >= 3 {
                self.state = CircuitState::Closed;
                self.success_count = 0;
            }
        }

        fn on_failure(&mut self) {
            self.failure_count += 1;

            if self.failure_count >= self.failure_threshold {
                self.state = CircuitState::Open;
            }
        }

        fn half_open(&mut self) {
            self.state = CircuitState::HalfOpen;
            self.success_count = 0;
        }
    }

    let mut circuit = CircuitBreaker::new(3);

    // Simulate operations
    for i in 0..10 {
        let result = circuit.call(|| -> std::result::Result<i32, &str> {
            if i % 3 == 0 {
                Err("Simulated failure")
            } else {
                Ok(i)
            }
        });

        println!("Operation {}: {:?}", i, result);
    }
}

fn error_accumulation() -> Result<Vec<i32>> {
    let inputs = vec!["1", "2", "invalid", "4", "also_invalid", "6"];
    let mut results = Vec::new();
    let mut errors = Vec::new();

    for input in inputs {
        match input.parse::<i32>() {
            Ok(num) => results.push(num),
            Err(err) => errors.push(format!("Failed to parse '{}': {}", input, err)),
        }
    }

    if !errors.is_empty() {
        return Err(format!("Multiple parse errors: {}", errors.join(", ")).into());
    }

    Ok(results)
}

fn partial_success_pattern() -> (Vec<i32>, Vec<String>) {
    let inputs = vec!["1", "2", "invalid", "4", "also_invalid", "6"];
    let mut successes = Vec::new();
    let mut failures = Vec::new();

    for input in inputs {
        match input.parse::<i32>() {
            Ok(num) => successes.push(num),
            Err(err) => failures.push(format!("Failed to parse '{}': {}", input, err)),
        }
    }

    (successes, failures)
}

use std::time::{Duration, Instant};

fn timeout_pattern<F, T>(operation: F, timeout: Duration) -> Result<T>
where
    F: FnOnce() -> T + Send + 'static,
    T: Send + 'static,
{
    use std::sync::mpsc;
    use std::thread;

    let (tx, rx) = mpsc::channel();

    thread::spawn(move || {
        let result = operation();
        let _ = tx.send(result);
    });

    match rx.recv_timeout(timeout) {
        Ok(result) => Ok(result),
        Err(_) => Err("Operation timed out".into()),
    }
}

fn fallback_chain() -> Result<String> {
    // Try primary source
    if let Ok(data) = primary_data_source() {
        return Ok(data);
    }

    // Try secondary source
    if let Ok(data) = secondary_data_source() {
        return Ok(data);
    }

    // Try cache
    if let Ok(data) = cached_data_source() {
        return Ok(data);
    }

    // Use default
    Ok("default_value".to_string())
}

fn primary_data_source() -> Result<String> {
    Err("Primary source unavailable".into())
}

fn secondary_data_source() -> Result<String> {
    Err("Secondary source unavailable".into())
}

fn cached_data_source() -> Result<String> {
    Ok("cached_data".to_string())
}
""",
    )

    run_updater(rust_error_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    pattern_calls = [
        call
        for call in calls
        if "retry_pattern" in str(call) or "circuit_breaker_pattern" in str(call)
    ]
    assert len(pattern_calls) > 0, "Error pattern functions should be detected"
