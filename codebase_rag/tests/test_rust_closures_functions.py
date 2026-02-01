from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_closures_project(temp_repo: Path) -> Path:
    """Create a Rust project structure for closures and function testing."""
    project_path = temp_repo / "rust_closures_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Closures and functions test crate"
    )

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""[package]
name = "rust_closures_test"
version = "0.1.0"
edition = "2021"
""",
    )

    return project_path


def test_basic_closures_and_captures(
    rust_closures_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic closure syntax and different capture modes."""
    test_file = rust_closures_project / "basic_closures.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic closure definitions
fn basic_closure_examples() {
    // Simple closure with no captures
    let add_one = |x| x + 1;
    println!("add_one(5) = {}", add_one(5));

    // Closure with explicit types
    let multiply: fn(i32, i32) -> i32 = |x, y| x * y;
    println!("multiply(3, 4) = {}", multiply(3, 4));

    // Closure with block body
    let complex_operation = |x: i32, y: i32| {
        let temp = x * 2;
        let result = temp + y;
        println!("Complex operation: {} * 2 + {} = {}", x, y, result);
        result
    };

    let result = complex_operation(5, 3);
}

// Closure capture modes
fn closure_capture_modes() {
    let x = 10;
    let y = 20;
    let mut z = 30;

    // Capture by reference (Fn)
    let capture_ref = |a| a + x + y;
    println!("Capture by ref: {}", capture_ref(5));

    // Capture by mutable reference (FnMut)
    let mut capture_mut = |a| {
        z += a;
        z
    };
    println!("Capture by mut ref: {}", capture_mut(5));
    println!("z after closure: {}", z);

    // Capture by value (FnOnce)
    let s = String::from("hello");
    let capture_move = move |suffix: &str| {
        format!("{} {}", s, suffix)
    };
    println!("Capture by move: {}", capture_move("world"));
    // s is no longer available here
}

// Explicit move closures
fn move_closures() {
    let data = vec![1, 2, 3, 4, 5];

    // Move closure for thread safety
    let process_data = move |multiplier: i32| {
        data.iter()
            .map(|&x| x * multiplier)
            .collect::<Vec<i32>>()
    };

    let result = process_data(2);
    println!("Processed data: {:?}", result);
    // data is no longer available here

    // Move specific variables
    let x = 42;
    let y = 24;
    let selective_move = move |z| {
        x + z // x is moved, but y could still be borrowed if used elsewhere
    };

    println!("Selective move result: {}", selective_move(10));
    println!("y is still available: {}", y);
}

// Returning closures
fn create_adder(n: i32) -> impl Fn(i32) -> i32 {
    move |x| x + n
}

fn create_multiplier(n: i32) -> Box<dyn Fn(i32) -> i32> {
    Box::new(move |x| x * n)
}

fn create_counter() -> impl FnMut() -> i32 {
    let mut count = 0;
    move || {
        count += 1;
        count
    }
}

fn returning_closures_demo() {
    let add_five = create_adder(5);
    println!("add_five(10) = {}", add_five(10));

    let multiply_three = create_multiplier(3);
    println!("multiply_three(7) = {}", multiply_three(7));

    let mut counter = create_counter();
    println!("counter() = {}", counter());
    println!("counter() = {}", counter());
    println!("counter() = {}", counter());
}

// Closure as struct fields
struct Calculator {
    operation: Box<dyn Fn(f64, f64) -> f64>,
    name: String,
}

impl Calculator {
    fn new<F>(name: String, op: F) -> Self
    where
        F: Fn(f64, f64) -> f64 + 'static,
    {
        Calculator {
            operation: Box::new(op),
            name,
        }
    }

    fn calculate(&self, a: f64, b: f64) -> f64 {
        (self.operation)(a, b)
    }

    fn get_name(&self) -> &str {
        &self.name
    }
}

fn calculator_with_closures() {
    let add_calc = Calculator::new(
        "Adder".to_string(),
        |a, b| a + b,
    );

    let multiply_calc = Calculator::new(
        "Multiplier".to_string(),
        |a, b| a * b,
    );

    let power_calc = Calculator::new(
        "Power".to_string(),
        |base, exp| base.powf(exp),
    );

    println!("{}: 5 + 3 = {}", add_calc.get_name(), add_calc.calculate(5.0, 3.0));
    println!("{}: 5 * 3 = {}", multiply_calc.get_name(), multiply_calc.calculate(5.0, 3.0));
    println!("{}: 5^3 = {}", power_calc.get_name(), power_calc.calculate(5.0, 3.0));
}

// Complex capture scenarios
fn complex_captures() {
    let mut state = vec![1, 2, 3];
    let config = "debug";

    // Closure capturing different types
    let processor = |new_item: i32| {
        if config == "debug" {
            println!("Adding {} to state", new_item);
        }
        state.push(new_item);
        state.len()
    };

    // Note: This would require careful lifetime management in real code
    // For demonstration purposes only
}

// Closure with generic parameters
fn generic_closure_operations<T, F>(items: Vec<T>, operation: F) -> Vec<T>
where
    F: Fn(T) -> T,
    T: Clone,
{
    items.into_iter().map(operation).collect()
}

fn generic_closures_demo() {
    let numbers = vec![1, 2, 3, 4, 5];
    let doubled = generic_closure_operations(numbers, |x| x * 2);
    println!("Doubled: {:?}", doubled);

    let strings = vec!["hello".to_string(), "world".to_string()];
    let uppercased = generic_closure_operations(strings, |s| s.to_uppercase());
    println!("Uppercased: {:?}", uppercased);
}
""",
    )

    run_updater(rust_closures_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    closure_calls = [
        call
        for call in calls
        if "closure" in str(call)
        or "create_adder" in str(call)
        or "Calculator" in str(call)
    ]
    assert len(closure_calls) > 0, "Closure functions should be detected"


def test_function_pointers_and_types(
    rust_closures_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test function pointers, function types, and function item types."""
    test_file = rust_closures_project / "function_pointers.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Function pointer types
type UnaryOp = fn(i32) -> i32;
type BinaryOp = fn(i32, i32) -> i32;
type Predicate = fn(i32) -> bool;

// Basic functions for function pointers
fn add(a: i32, b: i32) -> i32 {
    a + b
}

fn subtract(a: i32, b: i32) -> i32 {
    a - b
}

fn multiply(a: i32, b: i32) -> i32 {
    a * b
}

fn divide(a: i32, b: i32) -> i32 {
    if b != 0 { a / b } else { 0 }
}

fn square(x: i32) -> i32 {
    x * x
}

fn cube(x: i32) -> i32 {
    x * x * x
}

fn is_even(x: i32) -> bool {
    x % 2 == 0
}

fn is_positive(x: i32) -> bool {
    x > 0
}

// Function that takes function pointers
fn apply_binary_op(a: i32, b: i32, op: BinaryOp) -> i32 {
    op(a, b)
}

fn apply_unary_op(x: i32, op: UnaryOp) -> i32 {
    op(x)
}

fn filter_numbers(numbers: &[i32], predicate: Predicate) -> Vec<i32> {
    numbers.iter()
        .filter(|&&x| predicate(x))
        .copied()
        .collect()
}

// Array of function pointers
fn function_pointer_arrays() {
    let binary_ops: [BinaryOp; 4] = [add, subtract, multiply, divide];
    let unary_ops: [UnaryOp; 2] = [square, cube];
    let predicates: [Predicate; 2] = [is_even, is_positive];

    let a = 10;
    let b = 3;

    for (i, op) in binary_ops.iter().enumerate() {
        let result = op(a, b);
        println!("Binary op {}: {} with ({}, {}) = {}", i, "operation", a, b, result);
    }

    let x = 5;
    for (i, op) in unary_ops.iter().enumerate() {
        let result = op(x);
        println!("Unary op {}: {} with {} = {}", i, "operation", x, result);
    }

    let numbers = vec![-2, -1, 0, 1, 2, 3, 4, 5];
    for (i, predicate) in predicates.iter().enumerate() {
        let filtered = filter_numbers(&numbers, *predicate);
        println!("Predicate {}: {:?}", i, filtered);
    }
}

// Function pointer in structs
struct MathProcessor {
    name: String,
    operation: BinaryOp,
}

impl MathProcessor {
    fn new(name: String, operation: BinaryOp) -> Self {
        MathProcessor { name, operation }
    }

    fn process(&self, a: i32, b: i32) -> i32 {
        (self.operation)(a, b)
    }

    fn get_name(&self) -> &str {
        &self.name
    }
}

fn math_processor_demo() {
    let processors = vec![
        MathProcessor::new("Addition".to_string(), add),
        MathProcessor::new("Subtraction".to_string(), subtract),
        MathProcessor::new("Multiplication".to_string(), multiply),
        MathProcessor::new("Division".to_string(), divide),
    ];

    let a = 20;
    let b = 4;

    for processor in &processors {
        let result = processor.process(a, b);
        println!("{}: {} op {} = {}", processor.get_name(), a, b, result);
    }
}

// Higher-order functions with function pointers
fn compose(f: UnaryOp, g: UnaryOp) -> impl Fn(i32) -> i32 {
    move |x| f(g(x))
}

fn twice(f: UnaryOp) -> impl Fn(i32) -> i32 {
    move |x| f(f(x))
}

fn conditional_apply(condition: Predicate, f: UnaryOp) -> impl Fn(i32) -> i32 {
    move |x| if condition(x) { f(x) } else { x }
}

fn higher_order_demo() {
    // Composition: square(cube(x))
    let square_of_cube = compose(square, cube);
    println!("square(cube(2)) = {}", square_of_cube(2));

    // Apply function twice: square(square(x))
    let double_square = twice(square);
    println!("square(square(3)) = {}", double_square(3));

    // Conditional application
    let square_if_positive = conditional_apply(is_positive, square);
    println!("square_if_positive(-3) = {}", square_if_positive(-3));
    println!("square_if_positive(3) = {}", square_if_positive(3));
}

// Function pointer casting
fn function_pointer_casting() {
    // Cast function items to function pointers
    let add_ptr: fn(i32, i32) -> i32 = add;
    let square_ptr: fn(i32) -> i32 = square;

    // Function pointers can be cast to raw pointers
    let add_raw = add_ptr as *const ();
    let square_raw = square_ptr as *const ();

    println!("Function pointers as raw pointers:");
    println!("add: {:p}", add_raw);
    println!("square: {:p}", square_raw);

    // Cast back (unsafe)
    unsafe {
        let recovered_add: fn(i32, i32) -> i32 = std::mem::transmute(add_raw);
        println!("Recovered add(5, 3) = {}", recovered_add(5, 3));
    }
}

// Generic function pointers
fn apply_to_pair<T, F>(pair: (T, T), f: F) -> (T, T)
where
    F: Fn(T) -> T,
{
    (f(pair.0), f(pair.1))
}

fn map_array<T, F, const N: usize>(array: [T; N], f: F) -> [T; N]
where
    F: Fn(T) -> T,
    T: Copy,
{
    let mut result = array;
    for i in 0..N {
        result[i] = f(array[i]);
    }
    result
}

fn generic_function_pointers() {
    let number_pair = (3, 7);
    let squared_pair = apply_to_pair(number_pair, square);
    println!("Squared pair: {:?}", squared_pair);

    let numbers = [1, 2, 3, 4, 5];
    let cubed_numbers = map_array(numbers, cube);
    println!("Cubed numbers: {:?}", cubed_numbers);
}

// Function pointers with lifetimes
fn process_strings<'a>(
    strings: &'a [String],
    processor: fn(&str) -> String,
) -> Vec<String> {
    strings.iter()
        .map(|s| processor(s))
        .collect()
}

fn to_uppercase_copy(s: &str) -> String {
    s.to_uppercase()
}

fn reverse_copy(s: &str) -> String {
    s.chars().rev().collect()
}

fn function_pointers_with_lifetimes() {
    let strings = vec![
        "hello".to_string(),
        "world".to_string(),
        "rust".to_string(),
    ];

    let uppercase_strings = process_strings(&strings, to_uppercase_copy);
    let reversed_strings = process_strings(&strings, reverse_copy);

    println!("Original: {:?}", strings);
    println!("Uppercase: {:?}", uppercase_strings);
    println!("Reversed: {:?}", reversed_strings);
}

// Function traits vs function pointers
fn demonstrate_fn_traits() {
    // Fn trait - can be called multiple times, immutable borrows
    let fn_closure = |x: i32| x * 2;
    call_fn_trait(&fn_closure);
    call_fn_trait(&fn_closure); // Can call multiple times

    // FnMut trait - can be called multiple times, mutable borrows
    let mut counter = 0;
    let mut fn_mut_closure = |x: i32| {
        counter += 1;
        x + counter
    };
    call_fn_mut_trait(&mut fn_mut_closure);
    call_fn_mut_trait(&mut fn_mut_closure);

    // FnOnce trait - can only be called once, takes ownership
    let data = vec![1, 2, 3];
    let fn_once_closure = move |x: i32| {
        println!("Data: {:?}", data);
        x
    };
    call_fn_once_trait(fn_once_closure);
    // fn_once_closure is no longer available
}

fn call_fn_trait<F>(f: &F) -> i32
where
    F: Fn(i32) -> i32,
{
    f(10)
}

fn call_fn_mut_trait<F>(f: &mut F) -> i32
where
    F: FnMut(i32) -> i32,
{
    f(10)
}

fn call_fn_once_trait<F>(f: F) -> i32
where
    F: FnOnce(i32) -> i32,
{
    f(10)
}
""",
    )

    run_updater(rust_closures_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    function_calls = [
        call
        for call in calls
        if "function_pointer" in str(call) or "MathProcessor" in str(call)
    ]
    assert len(function_calls) > 0, "Function pointer operations should be detected"


def test_higher_order_functions(
    rust_closures_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test higher-order functions and functional programming patterns."""
    test_file = rust_closures_project / "higher_order.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::collections::HashMap;

// Higher-order function that takes multiple function parameters
fn transform_and_filter<T, U, F, P>(
    items: Vec<T>,
    transform: F,
    predicate: P,
) -> Vec<U>
where
    F: Fn(T) -> U,
    P: Fn(&U) -> bool,
{
    items
        .into_iter()
        .map(transform)
        .filter(predicate)
        .collect()
}

// Curried functions
fn add_curried(x: i32) -> impl Fn(i32) -> i32 {
    move |y| x + y
}

fn multiply_curried(x: i32) -> impl Fn(i32) -> i32 {
    move |y| x * y
}

fn compose_curried<A, B, C, F, G>(f: F) -> impl Fn(G) -> impl Fn(A) -> C
where
    F: Fn(B) -> C,
    G: Fn(A) -> B,
{
    move |g| move |x| f(g(x))
}

// Function composition utilities
fn pipe<A, B, C, F, G>(f: F, g: G) -> impl Fn(A) -> C
where
    F: Fn(A) -> B,
    G: Fn(B) -> C,
{
    move |x| g(f(x))
}

fn chain<T, F1, F2, F3>(f1: F1, f2: F2, f3: F3) -> impl Fn(T) -> T
where
    F1: Fn(T) -> T,
    F2: Fn(T) -> T,
    F3: Fn(T) -> T,
{
    move |x| f3(f2(f1(x)))
}

// Memoization higher-order function
fn memoize<F, A, R>(mut f: F) -> impl FnMut(A) -> R
where
    F: FnMut(A) -> R,
    A: std::hash::Hash + Eq + Clone,
    R: Clone,
{
    let mut cache = HashMap::new();

    move |arg: A| {
        if let Some(result) = cache.get(&arg) {
            result.clone()
        } else {
            let result = f(arg.clone());
            cache.insert(arg, result.clone());
            result
        }
    }
}

// Retry higher-order function
fn with_retry<F, T, E>(mut f: F, max_attempts: usize) -> impl FnMut() -> Result<T, E>
where
    F: FnMut() -> Result<T, E>,
{
    let mut attempts = 0;

    move || {
        loop {
            attempts += 1;
            match f() {
                Ok(value) => return Ok(value),
                Err(e) if attempts >= max_attempts => return Err(e),
                Err(_) => continue,
            }
        }
    }
}

// Timing higher-order function
fn timed<F, T>(f: F) -> impl Fn() -> (T, std::time::Duration)
where
    F: Fn() -> T,
{
    move || {
        let start = std::time::Instant::now();
        let result = f();
        let duration = start.elapsed();
        (result, duration)
    }
}

// Conditional execution
fn when<F, T>(condition: bool, f: F) -> Option<T>
where
    F: FnOnce() -> T,
{
    if condition {
        Some(f())
    } else {
        None
    }
}

fn unless<F, T>(condition: bool, f: F) -> Option<T>
where
    F: FnOnce() -> T,
{
    when(!condition, f)
}

// Demonstration functions
fn higher_order_demos() {
    // Transform and filter demo
    let numbers = vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
    let even_squares = transform_and_filter(
        numbers,
        |x| x * x,
        |&x| x % 2 == 0,
    );
    println!("Even squares: {:?}", even_squares);

    // Currying demo
    let add_five = add_curried(5);
    let multiply_three = multiply_curried(3);

    println!("add_five(10) = {}", add_five(10));
    println!("multiply_three(7) = {}", multiply_three(7));

    // Function composition demo
    let square = |x: i32| x * x;
    let add_one = |x: i32| x + 1;
    let double = |x: i32| x * 2;

    let composed = pipe(square, add_one);
    println!("pipe(square, add_one)(5) = {}", composed(5));

    let chained = chain(add_one, square, double);
    println!("chain(add_one, square, double)(3) = {}", chained(3));

    // Memoization demo
    let fibonacci = |n: u32| -> u64 {
        match n {
            0 => 0,
            1 => 1,
            n => {
                // This would be inefficient without memoization
                // In practice, you'd implement this recursively with memoization
                (1..n).fold((0, 1), |(a, b), _| (b, a + b)).1
            }
        }
    };

    let mut memoized_fib = memoize(fibonacci);
    println!("fib(10) = {}", memoized_fib(10));
    println!("fib(10) again = {}", memoized_fib(10)); // Should use cache

    // Timing demo
    let expensive_computation = || {
        std::thread::sleep(std::time::Duration::from_millis(100));
        42
    };

    let timed_computation = timed(expensive_computation);
    let (result, duration) = timed_computation();
    println!("Result: {}, Time: {:?}", result, duration);

    // Conditional execution demo
    let value = 10;
    let maybe_result = when(value > 5, || value * 2);
    println!("Conditional result: {:?}", maybe_result);

    let unless_result = unless(value < 5, || value + 10);
    println!("Unless result: {:?}", unless_result);
}

// Functional error handling
fn try_chain<T, E, F1, F2, F3>(
    f1: F1,
    f2: F2,
    f3: F3,
) -> impl Fn(T) -> Result<T, E>
where
    F1: Fn(T) -> Result<T, E>,
    F2: Fn(T) -> Result<T, E>,
    F3: Fn(T) -> Result<T, E>,
{
    move |input| {
        f1(input)
            .and_then(&f2)
            .and_then(&f3)
    }
}

fn or_else_chain<T, E, F1, F2>(
    f1: F1,
    f2: F2,
) -> impl Fn(T) -> Result<T, E>
where
    F1: Fn(T) -> Result<T, E>,
    F2: Fn(T) -> Result<T, E>,
{
    move |input| {
        f1(input).or_else(|_| f2(input))
    }
}

// State machine using closures
type StateMachine<S, I, O> = Box<dyn FnMut(S, I) -> (S, O)>;

fn create_counter_state_machine() -> StateMachine<i32, String, String> {
    Box::new(|state, input| {
        match input.as_str() {
            "increment" => (state + 1, format!("Count: {}", state + 1)),
            "decrement" => (state - 1, format!("Count: {}", state - 1)),
            "reset" => (0, "Count: 0".to_string()),
            _ => (state, format!("Count: {} (unknown command)", state)),
        }
    })
}

fn state_machine_demo() {
    let mut state_machine = create_counter_state_machine();
    let mut current_state = 0;

    let commands = vec!["increment", "increment", "decrement", "reset", "increment"];

    for command in commands {
        let (new_state, output) = state_machine(current_state, command.to_string());
        current_state = new_state;
        println!("Command: {}, Output: {}", command, output);
    }
}

// Event handling with closures
type EventHandler<E> = Box<dyn FnMut(E)>;

struct EventSystem<E> {
    handlers: Vec<EventHandler<E>>,
}

impl<E> EventSystem<E>
where
    E: Clone,
{
    fn new() -> Self {
        EventSystem {
            handlers: Vec::new(),
        }
    }

    fn subscribe<F>(&mut self, handler: F)
    where
        F: FnMut(E) + 'static,
    {
        self.handlers.push(Box::new(handler));
    }

    fn emit(&mut self, event: E) {
        for handler in &mut self.handlers {
            handler(event.clone());
        }
    }
}

#[derive(Clone, Debug)]
enum AppEvent {
    UserLogin(String),
    UserLogout,
    DataUpdate(i32),
}

fn event_system_demo() {
    let mut event_system = EventSystem::new();

    // Subscribe various handlers
    event_system.subscribe(|event| {
        println!("Logger: {:?}", event);
    });

    event_system.subscribe(|event| {
        match event {
            AppEvent::UserLogin(user) => println!("Welcome, {}!", user),
            AppEvent::UserLogout => println!("Goodbye!"),
            AppEvent::DataUpdate(count) => println!("Data updated: {} items", count),
        }
    });

    // Emit events
    event_system.emit(AppEvent::UserLogin("Alice".to_string()));
    event_system.emit(AppEvent::DataUpdate(42));
    event_system.emit(AppEvent::UserLogout);
}

// Functional reactive programming patterns
fn create_observable<T, F>(generator: F) -> impl Iterator<Item = T>
where
    F: Fn() -> T,
{
    std::iter::repeat_with(generator)
}

fn map_observable<T, U, F, I>(observable: I, mapper: F) -> impl Iterator<Item = U>
where
    I: Iterator<Item = T>,
    F: Fn(T) -> U,
{
    observable.map(mapper)
}

fn filter_observable<T, P, I>(observable: I, predicate: P) -> impl Iterator<Item = T>
where
    I: Iterator<Item = T>,
    P: Fn(&T) -> bool,
{
    observable.filter(predicate)
}

fn observable_demo() {
    // Create a simple observable that generates random-ish numbers
    let mut counter = 0;
    let number_stream = create_observable(move || {
        counter += 1;
        counter * 7 % 100 // Simple pseudo-random
    });

    // Transform the stream
    let doubled_stream = map_observable(number_stream, |x| x * 2);
    let even_stream = filter_observable(doubled_stream, |&x| x % 2 == 0);

    // Take first 5 values
    let results: Vec<i32> = even_stream.take(5).collect();
    println!("Observable results: {:?}", results);
}
""",
    )

    run_updater(rust_closures_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    higher_order_calls = [
        call
        for call in calls
        if "transform_and_filter" in str(call)
        or "memoize" in str(call)
        or "EventSystem" in str(call)
    ]
    assert len(higher_order_calls) > 0, "Higher-order functions should be detected"


def test_async_closures_and_futures(
    rust_closures_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test async closures and future-related patterns."""
    test_file = rust_closures_project / "async_closures.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::future::Future;
use std::pin::Pin;
use std::task::{Context, Poll};

// Async function types
type AsyncFn<T> = Box<dyn Fn() -> Pin<Box<dyn Future<Output = T>>>>;
type AsyncFnMut<T> = Box<dyn FnMut() -> Pin<Box<dyn Future<Output = T>>>>;

// Creating async closures manually
fn create_async_closure() -> impl Fn() -> Pin<Box<dyn Future<Output = i32>>> {
    || {
        Box::pin(async {
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            42
        })
    }
}

// Async closure with captures
fn async_closure_with_capture(value: i32) -> impl Fn() -> Pin<Box<dyn Future<Output = i32>>> {
    move || {
        Box::pin(async move {
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            value * 2
        })
    }
}

// Future combinators with closures
async fn chain_futures<F1, F2, T, U>(
    fut1: F1,
    then: F2,
) -> U
where
    F1: Future<Output = T>,
    F2: FnOnce(T) -> U,
{
    let result = fut1.await;
    then(result)
}

async fn map_future<F, Func, T, U>(
    future: F,
    mapper: Func,
) -> U
where
    F: Future<Output = T>,
    Func: FnOnce(T) -> U,
{
    let result = future.await;
    mapper(result)
}

// Async retry with closure
async fn async_retry<F, T, E>(
    mut operation: F,
    max_attempts: usize,
) -> Result<T, E>
where
    F: FnMut() -> Pin<Box<dyn Future<Output = Result<T, E>>>>,
{
    let mut attempts = 0;

    loop {
        attempts += 1;
        match operation().await {
            Ok(value) => return Ok(value),
            Err(e) if attempts >= max_attempts => return Err(e),
            Err(_) => continue,
        }
    }
}

// Stream processing with closures
async fn process_stream<S, F, T, U>(
    mut stream: S,
    processor: F,
) -> Vec<U>
where
    S: futures::Stream<Item = T> + Unpin,
    F: Fn(T) -> U,
{
    use futures::StreamExt;

    let mut results = Vec::new();
    while let Some(item) = stream.next().await {
        results.push(processor(item));
    }
    results
}

// Async state machine
type AsyncStateMachine<S, I, O> =
    Box<dyn FnMut(S, I) -> Pin<Box<dyn Future<Output = (S, O)>>>>;

fn create_async_state_machine() -> AsyncStateMachine<i32, String, String> {
    Box::new(|state, input| {
        Box::pin(async move {
            // Simulate async work
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;

            match input.as_str() {
                "async_increment" => {
                    (state + 1, format!("Async Count: {}", state + 1))
                }
                "async_decrement" => {
                    (state - 1, format!("Async Count: {}", state - 1))
                }
                _ => (state, format!("Async Count: {} (unknown)", state)),
            }
        })
    })
}

// Async event handler
type AsyncEventHandler<E> = Box<dyn FnMut(E) -> Pin<Box<dyn Future<Output = ()>>>>;

struct AsyncEventSystem<E> {
    handlers: Vec<AsyncEventHandler<E>>,
}

impl<E> AsyncEventSystem<E>
where
    E: Clone,
{
    fn new() -> Self {
        AsyncEventSystem {
            handlers: Vec::new(),
        }
    }

    fn subscribe<F, Fut>(&mut self, mut handler: F)
    where
        F: FnMut(E) -> Fut + 'static,
        Fut: Future<Output = ()> + 'static,
    {
        self.handlers.push(Box::new(move |event| {
            Box::pin(handler(event))
        }));
    }

    async fn emit(&mut self, event: E) {
        for handler in &mut self.handlers {
            handler(event.clone()).await;
        }
    }
}

// Demonstration functions
async fn demo_async_closures() {
    // Basic async closure
    let async_closure = create_async_closure();
    let result = async_closure().await;
    println!("Async closure result: {}", result);

    // Async closure with capture
    let captured_closure = async_closure_with_capture(21);
    let captured_result = captured_closure().await;
    println!("Captured closure result: {}", captured_result);

    // Future chaining
    let future = async { 10 };
    let chained_result = chain_futures(future, |x| x * 3).await;
    println!("Chained result: {}", chained_result);

    // Future mapping
    let future = async { "hello" };
    let mapped_result = map_future(future, |s| s.len()).await;
    println!("Mapped result: {}", mapped_result);
}

async fn demo_async_retry() {
    let mut attempt_count = 0;
    let flaky_operation = || {
        attempt_count += 1;
        Box::pin(async move {
            if attempt_count < 3 {
                Err("Not ready yet")
            } else {
                Ok("Success!")
            }
        })
    };

    match async_retry(flaky_operation, 5).await {
        Ok(result) => println!("Retry succeeded: {}", result),
        Err(e) => println!("Retry failed: {}", e),
    }
}

async fn demo_async_state_machine() {
    let mut state_machine = create_async_state_machine();
    let mut current_state = 0;

    let commands = vec!["async_increment", "async_increment", "async_decrement"];

    for command in commands {
        let (new_state, output) = state_machine(current_state, command.to_string()).await;
        current_state = new_state;
        println!("Async Command: {}, Output: {}", command, output);
    }
}

async fn demo_async_events() {
    let mut event_system = AsyncEventSystem::new();

    // Subscribe async handlers
    event_system.subscribe(|event: String| async move {
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        println!("Async handler 1: {}", event);
    });

    event_system.subscribe(|event: String| async move {
        tokio::time::sleep(std::time::Duration::from_millis(30)).await;
        println!("Async handler 2: {}", event);
    });

    // Emit events
    event_system.emit("Hello".to_string()).await;
    event_system.emit("World".to_string()).await;
}

// Async closure factories
fn create_delayed_computation<F, T>(
    delay_ms: u64,
    computation: F,
) -> impl Fn() -> Pin<Box<dyn Future<Output = T>>>
where
    F: Fn() -> T + 'static,
    T: 'static,
{
    move || {
        let comp = computation();
        Box::pin(async move {
            tokio::time::sleep(std::time::Duration::from_millis(delay_ms)).await;
            comp
        })
    }
}

async fn demo_async_factories() {
    let delayed_add = create_delayed_computation(100, || 5 + 3);
    let result = delayed_add().await;
    println!("Delayed computation result: {}", result);

    let delayed_string = create_delayed_computation(50, || "Hello, async world!".to_string());
    let string_result = delayed_string().await;
    println!("Delayed string: {}", string_result);
}

// Parallel execution with closures
async fn parallel_map<T, U, F>(
    items: Vec<T>,
    mapper: F,
) -> Vec<U>
where
    T: Send + 'static,
    U: Send + 'static,
    F: Fn(T) -> Pin<Box<dyn Future<Output = U> + Send>> + Send + Sync + 'static,
{
    use futures::future::join_all;

    let futures: Vec<_> = items.into_iter().map(mapper).collect();
    join_all(futures).await
}

async fn demo_parallel_processing() {
    let numbers = vec![1, 2, 3, 4, 5];

    let results = parallel_map(numbers, |x| {
        Box::pin(async move {
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            x * x
        })
    }).await;

    println!("Parallel results: {:?}", results);
}
""",
    )

    run_updater(rust_closures_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    async_calls = [
        call
        for call in calls
        if "async" in str(call)
        or "create_delayed" in str(call)
        or "AsyncEventSystem" in str(call)
    ]
    assert len(async_calls) > 0, "Async closure functions should be detected"
