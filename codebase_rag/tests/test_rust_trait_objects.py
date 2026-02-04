from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_trait_objects_project(temp_repo: Path) -> Path:
    """Create a Rust project structure for trait objects testing."""
    project_path = temp_repo / "rust_trait_objects_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Trait objects test crate"
    )

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""[package]
name = "rust_trait_objects_test"
version = "0.1.0"
edition = "2021"
""",
    )

    return project_path


def test_basic_trait_objects(
    rust_trait_objects_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic trait objects and dynamic dispatch."""
    test_file = rust_trait_objects_project / "basic_trait_objects.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic trait for trait objects
trait Drawable {
    fn draw(&self) -> String;
    fn area(&self) -> f64;
    fn name(&self) -> &str;
}

// Concrete implementations
struct Circle {
    radius: f64,
    name: String,
}

impl Circle {
    fn new(radius: f64, name: String) -> Self {
        Circle { radius, name }
    }
}

impl Drawable for Circle {
    fn draw(&self) -> String {
        format!("Drawing circle with radius {}", self.radius)
    }

    fn area(&self) -> f64 {
        std::f64::consts::PI * self.radius * self.radius
    }

    fn name(&self) -> &str {
        &self.name
    }
}

struct Rectangle {
    width: f64,
    height: f64,
    name: String,
}

impl Rectangle {
    fn new(width: f64, height: f64, name: String) -> Self {
        Rectangle { width, height, name }
    }
}

impl Drawable for Rectangle {
    fn draw(&self) -> String {
        format!("Drawing rectangle {}x{}", self.width, self.height)
    }

    fn area(&self) -> f64 {
        self.width * self.height
    }

    fn name(&self) -> &str {
        &self.name
    }
}

struct Triangle {
    base: f64,
    height: f64,
    name: String,
}

impl Triangle {
    fn new(base: f64, height: f64, name: String) -> Self {
        Triangle { base, height, name }
    }
}

impl Drawable for Triangle {
    fn draw(&self) -> String {
        format!("Drawing triangle with base {} and height {}", self.base, self.height)
    }

    fn area(&self) -> f64 {
        0.5 * self.base * self.height
    }

    fn name(&self) -> &str {
        &self.name
    }
}

// Functions using trait objects
fn draw_shape(shape: &dyn Drawable) -> String {
    format!("{}: {}", shape.name(), shape.draw())
}

fn calculate_total_area(shapes: &[Box<dyn Drawable>]) -> f64 {
    shapes.iter().map(|shape| shape.area()).sum()
}

fn print_shape_info(shape: &dyn Drawable) {
    println!("Shape: {}", shape.name());
    println!("Drawing: {}", shape.draw());
    println!("Area: {:.2}", shape.area());
}

// Vector of trait objects
fn create_shape_collection() -> Vec<Box<dyn Drawable>> {
    vec![
        Box::new(Circle::new(5.0, "Circle1".to_string())),
        Box::new(Rectangle::new(10.0, 8.0, "Rectangle1".to_string())),
        Box::new(Triangle::new(6.0, 4.0, "Triangle1".to_string())),
        Box::new(Circle::new(3.0, "Circle2".to_string())),
    ]
}

fn process_shapes() {
    let shapes = create_shape_collection();

    // Iterate over trait objects
    for shape in &shapes {
        print_shape_info(shape.as_ref());
    }

    // Calculate total area
    let total_area = calculate_total_area(&shapes);
    println!("Total area: {:.2}", total_area);

    // Find largest shape
    let largest = shapes
        .iter()
        .max_by(|a, b| a.area().partial_cmp(&b.area()).unwrap());

    if let Some(shape) = largest {
        println!("Largest shape: {} with area {:.2}", shape.name(), shape.area());
    }
}

// Trait object as return type
fn create_random_shape(shape_type: u32) -> Box<dyn Drawable> {
    match shape_type % 3 {
        0 => Box::new(Circle::new(5.0, "RandomCircle".to_string())),
        1 => Box::new(Rectangle::new(8.0, 6.0, "RandomRectangle".to_string())),
        _ => Box::new(Triangle::new(7.0, 5.0, "RandomTriangle".to_string())),
    }
}

// Trait object in struct fields
struct Canvas {
    shapes: Vec<Box<dyn Drawable>>,
    background_color: String,
}

impl Canvas {
    fn new(background_color: String) -> Self {
        Canvas {
            shapes: Vec::new(),
            background_color,
        }
    }

    fn add_shape(&mut self, shape: Box<dyn Drawable>) {
        self.shapes.push(shape);
    }

    fn render(&self) -> String {
        let mut result = format!("Canvas with {} background:\n", self.background_color);
        for shape in &self.shapes {
            result.push_str(&format!("  {}\n", shape.draw()));
        }
        result
    }

    fn total_area(&self) -> f64 {
        self.shapes.iter().map(|shape| shape.area()).sum()
    }

    fn count_shapes(&self) -> usize {
        self.shapes.len()
    }
}

// Generic function with trait object parameter
fn compare_shapes<F>(shape1: &dyn Drawable, shape2: &dyn Drawable, comparator: F) -> bool
where
    F: Fn(f64, f64) -> bool,
{
    comparator(shape1.area(), shape2.area())
}

fn canvas_operations() {
    let mut canvas = Canvas::new("white".to_string());

    // Add various shapes
    canvas.add_shape(Box::new(Circle::new(4.0, "CanvasCircle".to_string())));
    canvas.add_shape(Box::new(Rectangle::new(12.0, 6.0, "CanvasRect".to_string())));
    canvas.add_shape(create_random_shape(42));

    // Render canvas
    println!("{}", canvas.render());
    println!("Total canvas area: {:.2}", canvas.total_area());
    println!("Shape count: {}", canvas.count_shapes());

    // Compare shapes
    if canvas.shapes.len() >= 2 {
        let is_first_larger = compare_shapes(
            canvas.shapes[0].as_ref(),
            canvas.shapes[1].as_ref(),
            |a, b| a > b,
        );
        println!("First shape is larger: {}", is_first_larger);
    }
}
""",
    )

    run_updater(rust_trait_objects_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    trait_obj_calls = [
        call
        for call in calls
        if "Drawable" in str(call) or "Canvas" in str(call) or "draw_shape" in str(call)
    ]
    assert len(trait_obj_calls) > 0, "Trait objects should be detected"


def test_object_safety_patterns(
    rust_trait_objects_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test object safety requirements and patterns."""
    test_file = rust_trait_objects_project / "object_safety.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Object-safe trait (can be used as trait object)
trait ObjectSafeTrait {
    fn method_with_self(&self) -> String;
    fn method_with_self_ref(&self, other: &Self) -> bool
    where
        Self: PartialEq; // This makes the method not object-safe individually
    fn default_method(&self) -> i32 {
        42
    }
}

// Object-safe trait without problematic methods
trait SafeProcessor {
    fn process(&self, input: &str) -> String;
    fn get_name(&self) -> &str;
    fn reset(&self);
}

// Implementations
struct TextProcessor {
    name: String,
    prefix: String,
}

impl TextProcessor {
    fn new(name: String, prefix: String) -> Self {
        TextProcessor { name, prefix }
    }
}

impl SafeProcessor for TextProcessor {
    fn process(&self, input: &str) -> String {
        format!("{}{}", self.prefix, input)
    }

    fn get_name(&self) -> &str {
        &self.name
    }

    fn reset(&self) {
        // In real implementation, might use interior mutability
        println!("Resetting {}", self.name);
    }
}

struct NumberProcessor {
    name: String,
    multiplier: f64,
}

impl NumberProcessor {
    fn new(name: String, multiplier: f64) -> Self {
        NumberProcessor { name, multiplier }
    }
}

impl SafeProcessor for NumberProcessor {
    fn process(&self, input: &str) -> String {
        if let Ok(num) = input.parse::<f64>() {
            format!("{:.2}", num * self.multiplier)
        } else {
            "Invalid number".to_string()
        }
    }

    fn get_name(&self) -> &str {
        &self.name
    }

    fn reset(&self) {
        println!("Resetting number processor {}", self.name);
    }
}

// Using object-safe trait objects
fn process_with_trait_object(processor: &dyn SafeProcessor, input: &str) -> String {
    println!("Using processor: {}", processor.get_name());
    processor.process(input)
}

fn create_processor_collection() -> Vec<Box<dyn SafeProcessor>> {
    vec![
        Box::new(TextProcessor::new(
            "PrefixProcessor".to_string(),
            ">> ".to_string(),
        )),
        Box::new(NumberProcessor::new(
            "DoubleProcessor".to_string(),
            2.0,
        )),
        Box::new(TextProcessor::new(
            "SuffixProcessor".to_string(),
            " <<".to_string(),
        )),
    ]
}

// Trait with associated types (not object-safe)
trait NotObjectSafe {
    type Output;
    fn process(&self) -> Self::Output;
    fn generic_method<T>(&self, item: T) -> T; // Generic methods not object-safe
}

// Making trait object-safe by removing problematic parts
trait ObjectSafeVersion {
    fn process_string(&self) -> String;
    fn process_number(&self) -> f64;
}

// Trait with Self in parameter (not object-safe without where clause)
trait Comparable {
    fn compare(&self, other: &Self) -> std::cmp::Ordering
    where
        Self: Sized; // This makes it not usable in trait objects

    fn is_equal(&self, other: &dyn Comparable) -> bool; // Object-safe alternative
}

struct ComparableValue {
    value: i32,
}

impl ComparableValue {
    fn new(value: i32) -> Self {
        ComparableValue { value }
    }
}

impl Comparable for ComparableValue {
    fn compare(&self, other: &Self) -> std::cmp::Ordering {
        self.value.cmp(&other.value)
    }

    fn is_equal(&self, other: &dyn Comparable) -> bool {
        // This is a simplified comparison
        // In practice, you'd need a way to get the value for comparison
        false
    }
}

// Trait that can be made object-safe with careful design
trait EventHandler {
    fn handle_event(&self, event: &Event);
    fn get_handler_name(&self) -> &str;
    fn can_handle(&self, event_type: &str) -> bool;
}

#[derive(Debug)]
struct Event {
    event_type: String,
    data: String,
    timestamp: u64,
}

impl Event {
    fn new(event_type: String, data: String) -> Self {
        Event {
            event_type,
            data,
            timestamp: 0, // Would use actual timestamp
        }
    }
}

struct LoggingHandler {
    name: String,
}

impl LoggingHandler {
    fn new(name: String) -> Self {
        LoggingHandler { name }
    }
}

impl EventHandler for LoggingHandler {
    fn handle_event(&self, event: &Event) {
        println!("[{}] Logging event: {:?}", self.name, event);
    }

    fn get_handler_name(&self) -> &str {
        &self.name
    }

    fn can_handle(&self, event_type: &str) -> bool {
        true // Logger handles all events
    }
}

struct FilteringHandler {
    name: String,
    accepted_types: Vec<String>,
}

impl FilteringHandler {
    fn new(name: String, accepted_types: Vec<String>) -> Self {
        FilteringHandler { name, accepted_types }
    }
}

impl EventHandler for FilteringHandler {
    fn handle_event(&self, event: &Event) {
        if self.can_handle(&event.event_type) {
            println!("[{}] Filtering event: {}", self.name, event.event_type);
        }
    }

    fn get_handler_name(&self) -> &str {
        &self.name
    }

    fn can_handle(&self, event_type: &str) -> bool {
        self.accepted_types.contains(&event_type.to_string())
    }
}

// Event system using trait objects
struct EventSystem {
    handlers: Vec<Box<dyn EventHandler>>,
}

impl EventSystem {
    fn new() -> Self {
        EventSystem {
            handlers: Vec::new(),
        }
    }

    fn add_handler(&mut self, handler: Box<dyn EventHandler>) {
        self.handlers.push(handler);
    }

    fn dispatch_event(&self, event: &Event) {
        for handler in &self.handlers {
            if handler.can_handle(&event.event_type) {
                handler.handle_event(event);
            }
        }
    }

    fn list_handlers(&self) -> Vec<&str> {
        self.handlers
            .iter()
            .map(|h| h.get_handler_name())
            .collect()
    }
}

fn test_object_safety() {
    // Test safe processor trait objects
    let processors = create_processor_collection();

    let inputs = ["Hello World", "42.5", "Test"];

    for input in inputs {
        for processor in &processors {
            let result = process_with_trait_object(processor.as_ref(), input);
            println!("Input: '{}' -> '{}'", input, result);
        }
    }

    // Test event system
    let mut event_system = EventSystem::new();

    event_system.add_handler(Box::new(LoggingHandler::new(
        "MainLogger".to_string(),
    )));

    event_system.add_handler(Box::new(FilteringHandler::new(
        "ErrorFilter".to_string(),
        vec!["error".to_string(), "warning".to_string()],
    )));

    // Dispatch various events
    let events = vec![
        Event::new("info".to_string(), "Application started".to_string()),
        Event::new("error".to_string(), "Database connection failed".to_string()),
        Event::new("warning".to_string(), "Low memory".to_string()),
        Event::new("debug".to_string(), "Variable state".to_string()),
    ];

    println!("Registered handlers: {:?}", event_system.list_handlers());

    for event in &events {
        println!("\\nDispatching event: {}", event.event_type);
        event_system.dispatch_event(event);
    }
}

// Workarounds for non-object-safe traits
trait AsAny {
    fn as_any(&self) -> &dyn std::any::Any;
}

impl<T: 'static> AsAny for T {
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

// Combining object-safe trait with downcasting
trait ProcessorWithDowncast: SafeProcessor + AsAny {
    fn as_safe_processor(&self) -> &dyn SafeProcessor {
        self
    }
}

impl ProcessorWithDowncast for TextProcessor {}
impl ProcessorWithDowncast for NumberProcessor {}

fn downcast_example() {
    let processors: Vec<Box<dyn ProcessorWithDowncast>> = vec![
        Box::new(TextProcessor::new("Text".to_string(), "> ".to_string())),
        Box::new(NumberProcessor::new("Number".to_string(), 3.0)),
    ];

    for processor in &processors {
        let result = processor.process("test");
        println!("Processed: {}", result);

        // Attempt to downcast to specific type
        if let Some(text_proc) = processor.as_any().downcast_ref::<TextProcessor>() {
            println!("This is a TextProcessor with prefix: {}", text_proc.prefix);
        } else if let Some(num_proc) = processor.as_any().downcast_ref::<NumberProcessor>() {
            println!("This is a NumberProcessor with multiplier: {}", num_proc.multiplier);
        }
    }
}
""",
    )

    run_updater(rust_trait_objects_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    safety_calls = [
        call
        for call in calls
        if "SafeProcessor" in str(call)
        or "EventHandler" in str(call)
        or "EventSystem" in str(call)
    ]
    assert len(safety_calls) > 0, "Object safety patterns should be detected"


def test_dynamic_dispatch_performance(
    rust_trait_objects_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test dynamic dispatch patterns and performance considerations."""
    test_file = rust_trait_objects_project / "dynamic_dispatch.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::time::Instant;

// Trait for performance testing
trait Operation {
    fn execute(&self, input: i32) -> i32;
    fn name(&self) -> &str;
}

// Different implementations with varying complexity
struct SimpleAdd {
    value: i32,
}

impl SimpleAdd {
    fn new(value: i32) -> Self {
        SimpleAdd { value }
    }
}

impl Operation for SimpleAdd {
    fn execute(&self, input: i32) -> i32 {
        input + self.value
    }

    fn name(&self) -> &str {
        "SimpleAdd"
    }
}

struct ComplexMath {
    coefficients: Vec<f64>,
}

impl ComplexMath {
    fn new(coefficients: Vec<f64>) -> Self {
        ComplexMath { coefficients }
    }
}

impl Operation for ComplexMath {
    fn execute(&self, input: i32) -> i32 {
        let x = input as f64;
        let result = self.coefficients
            .iter()
            .enumerate()
            .map(|(i, &coef)| coef * x.powi(i as i32))
            .sum::<f64>();
        result as i32
    }

    fn name(&self) -> &str {
        "ComplexMath"
    }
}

struct BitManipulation {
    shift_amount: u32,
    mask: i32,
}

impl BitManipulation {
    fn new(shift_amount: u32, mask: i32) -> Self {
        BitManipulation { shift_amount, mask }
    }
}

impl Operation for BitManipulation {
    fn execute(&self, input: i32) -> i32 {
        ((input << self.shift_amount) ^ input) & self.mask
    }

    fn name(&self) -> &str {
        "BitManipulation"
    }
}

// Function using static dispatch (monomorphization)
fn static_dispatch<T: Operation>(op: &T, input: i32, iterations: usize) -> (i32, std::time::Duration) {
    let start = Instant::now();
    let mut result = input;

    for _ in 0..iterations {
        result = op.execute(result);
    }

    let duration = start.elapsed();
    (result, duration)
}

// Function using dynamic dispatch (vtable lookup)
fn dynamic_dispatch(op: &dyn Operation, input: i32, iterations: usize) -> (i32, std::time::Duration) {
    let start = Instant::now();
    let mut result = input;

    for _ in 0..iterations {
        result = op.execute(result);
    }

    let duration = start.elapsed();
    (result, duration)
}

// Batch processing with trait objects
fn process_batch_dynamic(operations: &[Box<dyn Operation>], inputs: &[i32]) -> Vec<Vec<i32>> {
    let start = Instant::now();

    let results: Vec<Vec<i32>> = operations
        .iter()
        .map(|op| {
            inputs
                .iter()
                .map(|&input| op.execute(input))
                .collect()
        })
        .collect();

    let duration = start.elapsed();
    println!("Batch dynamic dispatch took: {:?}", duration);
    results
}

// Performance comparison framework
struct PerformanceTest {
    operations: Vec<Box<dyn Operation>>,
    test_inputs: Vec<i32>,
    iterations: usize,
}

impl PerformanceTest {
    fn new() -> Self {
        PerformanceTest {
            operations: vec![
                Box::new(SimpleAdd::new(5)),
                Box::new(ComplexMath::new(vec![1.0, 2.0, 0.5, 0.1])),
                Box::new(BitManipulation::new(3, 0xFF)),
            ],
            test_inputs: (1..=100).collect(),
            iterations: 10000,
        }
    }

    fn run_static_tests(&self) {
        println!("Running static dispatch tests...");

        let simple_add = SimpleAdd::new(5);
        let (result, duration) = static_dispatch(&simple_add, 42, self.iterations);
        println!("Static SimpleAdd: result={}, time={:?}", result, duration);

        let complex_math = ComplexMath::new(vec![1.0, 2.0, 0.5, 0.1]);
        let (result, duration) = static_dispatch(&complex_math, 42, self.iterations);
        println!("Static ComplexMath: result={}, time={:?}", result, duration);

        let bit_manip = BitManipulation::new(3, 0xFF);
        let (result, duration) = static_dispatch(&bit_manip, 42, self.iterations);
        println!("Static BitManipulation: result={}, time={:?}", result, duration);
    }

    fn run_dynamic_tests(&self) {
        println!("\\nRunning dynamic dispatch tests...");

        for operation in &self.operations {
            let (result, duration) = dynamic_dispatch(operation.as_ref(), 42, self.iterations);
            println!("Dynamic {}: result={}, time={:?}", operation.name(), result, duration);
        }
    }

    fn run_batch_test(&self) {
        println!("\\nRunning batch processing test...");
        let results = process_batch_dynamic(&self.operations, &self.test_inputs);

        for (i, op_results) in results.iter().enumerate() {
            let sum: i32 = op_results.iter().sum();
            println!("Operation {}: total sum = {}", self.operations[i].name(), sum);
        }
    }
}

// Vtable exploration (conceptual)
struct VtableInfo {
    trait_object_size: usize,
    pointer_size: usize,
}

impl VtableInfo {
    fn analyze() -> Self {
        // Size of trait object (fat pointer)
        let trait_obj_size = std::mem::size_of::<&dyn Operation>();

        // Size of regular pointer
        let ptr_size = std::mem::size_of::<*const u8>();

        VtableInfo {
            trait_object_size: trait_obj_size,
            pointer_size: ptr_size,
        }
    }

    fn print_info(&self) {
        println!("\\nVtable Information:");
        println!("Trait object size: {} bytes", self.trait_object_size);
        println!("Regular pointer size: {} bytes", self.pointer_size);
        println!("Overhead: {} bytes", self.trait_object_size - self.pointer_size);
    }
}

// Polymorphic container with different access patterns
struct OperationContainer {
    operations: Vec<Box<dyn Operation>>,
    cache: std::collections::HashMap<String, i32>,
}

impl OperationContainer {
    fn new() -> Self {
        OperationContainer {
            operations: Vec::new(),
            cache: std::collections::HashMap::new(),
        }
    }

    fn add_operation(&mut self, operation: Box<dyn Operation>) {
        self.operations.push(operation);
    }

    fn execute_all(&mut self, input: i32) -> Vec<i32> {
        self.operations
            .iter()
            .map(|op| {
                let cache_key = format!("{}_{}", op.name(), input);

                // Check cache first
                if let Some(&cached_result) = self.cache.get(&cache_key) {
                    cached_result
                } else {
                    let result = op.execute(input);
                    self.cache.insert(cache_key, result);
                    result
                }
            })
            .collect()
    }

    fn execute_by_name(&self, name: &str, input: i32) -> Option<i32> {
        self.operations
            .iter()
            .find(|op| op.name() == name)
            .map(|op| op.execute(input))
    }

    fn benchmark_sequential(&self, input: i32, iterations: usize) -> std::time::Duration {
        let start = Instant::now();

        for _ in 0..iterations {
            for operation in &self.operations {
                operation.execute(input);
            }
        }

        start.elapsed()
    }
}

// Branch prediction and polymorphism
fn test_branch_prediction() {
    let operations: Vec<Box<dyn Operation>> = vec![
        Box::new(SimpleAdd::new(1)),
        Box::new(SimpleAdd::new(2)),
        Box::new(SimpleAdd::new(3)),
        Box::new(ComplexMath::new(vec![1.0, 1.0])),
        Box::new(BitManipulation::new(1, 0xFFFF)),
    ];

    // Sequential access (good for branch prediction)
    let start = Instant::now();
    for _ in 0..10000 {
        for op in &operations {
            op.execute(42);
        }
    }
    let sequential_time = start.elapsed();

    // Random access (poor for branch prediction)
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};

    let start = Instant::now();
    for i in 0..50000 {
        let mut hasher = DefaultHasher::new();
        i.hash(&mut hasher);
        let index = (hasher.finish() as usize) % operations.len();
        operations[index].execute(42);
    }
    let random_time = start.elapsed();

    println!("\\nBranch prediction test:");
    println!("Sequential access: {:?}", sequential_time);
    println!("Random access: {:?}", random_time);
    println!("Random/Sequential ratio: {:.2}",
             random_time.as_nanos() as f64 / sequential_time.as_nanos() as f64);
}

// Main test function
fn run_performance_tests() {
    let test = PerformanceTest::new();

    test.run_static_tests();
    test.run_dynamic_tests();
    test.run_batch_test();

    let vtable_info = VtableInfo::analyze();
    vtable_info.print_info();

    // Test operation container
    let mut container = OperationContainer::new();
    container.add_operation(Box::new(SimpleAdd::new(10)));
    container.add_operation(Box::new(ComplexMath::new(vec![2.0, 1.0, 0.5])));
    container.add_operation(Box::new(BitManipulation::new(2, 0x3F)));

    println!("\\nContainer test:");
    let results = container.execute_all(25);
    println!("Results: {:?}", results);

    let sequential_time = container.benchmark_sequential(25, 100000);
    println!("Container sequential benchmark: {:?}", sequential_time);

    test_branch_prediction();
}
""",
    )

    run_updater(rust_trait_objects_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    dispatch_calls = [
        call
        for call in calls
        if "Operation" in str(call)
        or "PerformanceTest" in str(call)
        or "dynamic_dispatch" in str(call)
    ]
    assert len(dispatch_calls) > 0, "Dynamic dispatch patterns should be detected"


def test_advanced_trait_object_patterns(
    rust_trait_objects_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test advanced trait object patterns and combinations."""
    test_file = rust_trait_objects_project / "advanced_patterns.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

// Multiple trait bounds in trait objects
trait Processable: Send + Sync + std::fmt::Debug {
    fn process(&self, data: &str) -> String;
    fn priority(&self) -> u8;
}

trait Configurable {
    fn configure(&mut self, config: &str) -> Result<(), String>;
    fn get_config(&self) -> String;
}

// Combined trait object
trait ProcessorInterface: Processable + Configurable {}

#[derive(Debug)]
struct WebProcessor {
    name: String,
    url_prefix: String,
    priority_level: u8,
    config: String,
}

impl WebProcessor {
    fn new(name: String, url_prefix: String, priority_level: u8) -> Self {
        WebProcessor {
            name,
            url_prefix,
            priority_level,
            config: "default".to_string(),
        }
    }
}

impl Processable for WebProcessor {
    fn process(&self, data: &str) -> String {
        format!("{}{}/{}", self.url_prefix, self.name, data)
    }

    fn priority(&self) -> u8 {
        self.priority_level
    }
}

impl Configurable for WebProcessor {
    fn configure(&mut self, config: &str) -> Result<(), String> {
        if config.is_empty() {
            Err("Config cannot be empty".to_string())
        } else {
            self.config = config.to_string();
            Ok(())
        }
    }

    fn get_config(&self) -> String {
        self.config.clone()
    }
}

impl ProcessorInterface for WebProcessor {}

#[derive(Debug)]
struct DatabaseProcessor {
    name: String,
    table_prefix: String,
    priority_level: u8,
    config: String,
}

impl DatabaseProcessor {
    fn new(name: String, table_prefix: String, priority_level: u8) -> Self {
        DatabaseProcessor {
            name,
            table_prefix,
            priority_level,
            config: "default".to_string(),
        }
    }
}

impl Processable for DatabaseProcessor {
    fn process(&self, data: &str) -> String {
        format!("INSERT INTO {}{} VALUES ('{}')", self.table_prefix, self.name, data)
    }

    fn priority(&self) -> u8 {
        self.priority_level
    }
}

impl Configurable for DatabaseProcessor {
    fn configure(&mut self, config: &str) -> Result<(), String> {
        self.config = config.to_string();
        Ok(())
    }

    fn get_config(&self) -> String {
        self.config.clone()
    }
}

impl ProcessorInterface for DatabaseProcessor {}

// Thread-safe trait object container
struct ProcessorPool {
    processors: Vec<Arc<Mutex<dyn ProcessorInterface>>>,
}

impl ProcessorPool {
    fn new() -> Self {
        ProcessorPool {
            processors: Vec::new(),
        }
    }

    fn add_processor(&mut self, processor: Arc<Mutex<dyn ProcessorInterface>>) {
        self.processors.push(processor);
    }

    fn process_parallel(&self, data: Vec<String>) -> Vec<String> {
        let mut handles = Vec::new();

        for (i, item) in data.into_iter().enumerate() {
            let processor_index = i % self.processors.len();
            let processor = Arc::clone(&self.processors[processor_index]);

            let handle = thread::spawn(move || {
                let proc = processor.lock().unwrap();
                proc.process(&item)
            });

            handles.push(handle);
        }

        handles
            .into_iter()
            .map(|h| h.join().unwrap())
            .collect()
    }

    fn configure_all(&self, config: &str) -> Vec<Result<(), String>> {
        self.processors
            .iter()
            .map(|proc| {
                let mut p = proc.lock().unwrap();
                p.configure(config)
            })
            .collect()
    }

    fn sort_by_priority(&mut self) {
        self.processors.sort_by_key(|proc| {
            let p = proc.lock().unwrap();
            std::cmp::Reverse(p.priority()) // Sort by priority descending
        });
    }
}

// Trait object with lifetime parameters
trait DataSource<'a> {
    fn get_data(&self) -> &'a str;
    fn is_valid(&self) -> bool;
}

struct StaticDataSource {
    data: &'static str,
}

impl StaticDataSource {
    fn new(data: &'static str) -> Self {
        StaticDataSource { data }
    }
}

impl<'a> DataSource<'a> for StaticDataSource {
    fn get_data(&self) -> &'a str {
        self.data
    }

    fn is_valid(&self) -> bool {
        !self.data.is_empty()
    }
}

// Using trait objects with lifetimes
fn process_data_sources<'a>(sources: &[Box<dyn DataSource<'a> + 'a>]) -> Vec<&'a str> {
    sources
        .iter()
        .filter(|source| source.is_valid())
        .map(|source| source.get_data())
        .collect()
}

// Higher-ranked trait bounds with trait objects
trait Transformer {
    fn transform<'a>(&self, input: &'a str) -> &'a str;
}

struct IdentityTransformer;

impl Transformer for IdentityTransformer {
    fn transform<'a>(&self, input: &'a str) -> &'a str {
        input
    }
}

// Factory pattern with trait objects
trait ProcessorFactory {
    fn create_processor(&self, config: &str) -> Box<dyn ProcessorInterface>;
    fn get_factory_name(&self) -> &str;
}

struct WebProcessorFactory;

impl ProcessorFactory for WebProcessorFactory {
    fn create_processor(&self, config: &str) -> Box<dyn ProcessorInterface> {
        Box::new(WebProcessor::new(
            "web".to_string(),
            config.to_string(),
            5,
        ))
    }

    fn get_factory_name(&self) -> &str {
        "WebProcessorFactory"
    }
}

struct DatabaseProcessorFactory;

impl ProcessorFactory for DatabaseProcessorFactory {
    fn create_processor(&self, config: &str) -> Box<dyn ProcessorInterface> {
        Box::new(DatabaseProcessor::new(
            "db".to_string(),
            config.to_string(),
            8,
        ))
    }

    fn get_factory_name(&self) -> &str {
        "DatabaseProcessorFactory"
    }
}

// Registry pattern with trait objects
struct ProcessorRegistry {
    factories: std::collections::HashMap<String, Box<dyn ProcessorFactory>>,
}

impl ProcessorRegistry {
    fn new() -> Self {
        ProcessorRegistry {
            factories: std::collections::HashMap::new(),
        }
    }

    fn register_factory(&mut self, name: String, factory: Box<dyn ProcessorFactory>) {
        self.factories.insert(name, factory);
    }

    fn create_processor(&self, factory_name: &str, config: &str) -> Option<Box<dyn ProcessorInterface>> {
        self.factories
            .get(factory_name)
            .map(|factory| factory.create_processor(config))
    }

    fn list_factories(&self) -> Vec<&str> {
        self.factories
            .values()
            .map(|f| f.get_factory_name())
            .collect()
    }
}

// Visitor pattern with trait objects
trait Visitor {
    fn visit_web_processor(&mut self, processor: &WebProcessor);
    fn visit_database_processor(&mut self, processor: &DatabaseProcessor);
}

trait Visitable {
    fn accept(&self, visitor: &mut dyn Visitor);
}

impl Visitable for WebProcessor {
    fn accept(&self, visitor: &mut dyn Visitor) {
        visitor.visit_web_processor(self);
    }
}

impl Visitable for DatabaseProcessor {
    fn accept(&self, visitor: &mut dyn Visitor) {
        visitor.visit_database_processor(self);
    }
}

struct ConfigurationVisitor {
    config_count: usize,
    config_summary: String,
}

impl ConfigurationVisitor {
    fn new() -> Self {
        ConfigurationVisitor {
            config_count: 0,
            config_summary: String::new(),
        }
    }

    fn get_summary(&self) -> (usize, &str) {
        (self.config_count, &self.config_summary)
    }
}

impl Visitor for ConfigurationVisitor {
    fn visit_web_processor(&mut self, processor: &WebProcessor) {
        self.config_count += 1;
        self.config_summary.push_str(&format!("Web[{}] ", processor.get_config()));
    }

    fn visit_database_processor(&mut self, processor: &DatabaseProcessor) {
        self.config_count += 1;
        self.config_summary.push_str(&format!("DB[{}] ", processor.get_config()));
    }
}

// Testing advanced patterns
fn test_advanced_patterns() {
    // Test processor pool
    let mut pool = ProcessorPool::new();

    pool.add_processor(Arc::new(Mutex::new(WebProcessor::new(
        "web1".to_string(),
        "https://api.".to_string(),
        5,
    ))));

    pool.add_processor(Arc::new(Mutex::new(DatabaseProcessor::new(
        "db1".to_string(),
        "tbl_".to_string(),
        8,
    ))));

    // Configure all processors
    let config_results = pool.configure_all("production");
    println!("Configuration results: {:?}", config_results);

    // Sort by priority
    pool.sort_by_priority();

    // Process data in parallel
    let test_data = vec![
        "item1".to_string(),
        "item2".to_string(),
        "item3".to_string(),
        "item4".to_string(),
    ];

    let results = pool.process_parallel(test_data);
    println!("Parallel processing results: {:?}", results);

    // Test factory pattern
    let mut registry = ProcessorRegistry::new();
    registry.register_factory("web".to_string(), Box::new(WebProcessorFactory));
    registry.register_factory("db".to_string(), Box::new(DatabaseProcessorFactory));

    println!("Available factories: {:?}", registry.list_factories());

    if let Some(processor) = registry.create_processor("web", "https://example.com") {
        let result = processor.process("test_data");
        println!("Factory-created processor result: {}", result);
    }

    // Test visitor pattern
    let web_proc = WebProcessor::new("visitor_web".to_string(), "https://".to_string(), 3);
    let db_proc = DatabaseProcessor::new("visitor_db".to_string(), "v_".to_string(), 7);

    let mut visitor = ConfigurationVisitor::new();
    web_proc.accept(&mut visitor);
    db_proc.accept(&mut visitor);

    let (count, summary) = visitor.get_summary();
    println!("Visitor results: {} processors, summary: {}", count, summary);

    // Test data sources with lifetimes
    let sources: Vec<Box<dyn DataSource<'static> + 'static>> = vec![
        Box::new(StaticDataSource::new("static_data_1")),
        Box::new(StaticDataSource::new("static_data_2")),
        Box::new(StaticDataSource::new("")), // Invalid
    ];

    let valid_data = process_data_sources(&sources);
    println!("Valid data sources: {:?}", valid_data);
}

// Trait object with async methods (using Pin and Future)
trait AsyncProcessor: Send + Sync {
    fn process_async<'a>(&'a self, data: &'a str) ->
        std::pin::Pin<Box<dyn std::future::Future<Output = String> + Send + 'a>>;
}

struct AsyncWebProcessor {
    base_url: String,
}

impl AsyncWebProcessor {
    fn new(base_url: String) -> Self {
        AsyncWebProcessor { base_url }
    }
}

impl AsyncProcessor for AsyncWebProcessor {
    fn process_async<'a>(&'a self, data: &'a str) ->
        std::pin::Pin<Box<dyn std::future::Future<Output = String> + Send + 'a>> {
        Box::pin(async move {
            // Simulate async work
            tokio::time::sleep(Duration::from_millis(10)).await;
            format!("{}/{}", self.base_url, data)
        })
    }
}

async fn test_async_trait_objects() {
    let processors: Vec<Box<dyn AsyncProcessor>> = vec![
        Box::new(AsyncWebProcessor::new("https://api1.com".to_string())),
        Box::new(AsyncWebProcessor::new("https://api2.com".to_string())),
    ];

    for (i, processor) in processors.iter().enumerate() {
        let result = processor.process_async(&format!("data_{}", i)).await;
        println!("Async processor {} result: {}", i, result);
    }
}
""",
    )

    run_updater(rust_trait_objects_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    advanced_calls = [
        call
        for call in calls
        if "ProcessorPool" in str(call)
        or "ProcessorRegistry" in str(call)
        or "Visitor" in str(call)
    ]
    assert len(advanced_calls) > 0, "Advanced trait object patterns should be detected"
