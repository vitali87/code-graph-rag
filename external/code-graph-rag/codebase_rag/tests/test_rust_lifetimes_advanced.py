from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_lifetimes_project(temp_repo: Path) -> Path:
    """Create a Rust project structure for advanced lifetimes testing."""
    project_path = temp_repo / "rust_lifetimes_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Advanced lifetimes test crate"
    )

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""[package]
name = "rust_lifetimes_test"
version = "0.1.0"
edition = "2021"
""",
    )

    return project_path


def test_complex_lifetime_relationships(
    rust_lifetimes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test complex lifetime relationships and dependencies."""
    test_file = rust_lifetimes_project / "complex_lifetimes.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Multiple lifetime parameters with dependencies
struct Container<'a, 'b: 'a> {
    primary: &'a str,
    secondary: &'b str,
}

impl<'a, 'b: 'a> Container<'a, 'b> {
    fn new(primary: &'a str, secondary: &'b str) -> Self {
        Container { primary, secondary }
    }

    fn get_primary(&self) -> &'a str {
        self.primary
    }

    fn get_secondary(&self) -> &'b str {
        self.secondary
    }

    // Method with additional lifetime constraints
    fn combine_with<'c>(&self, other: &'c str) -> String
    where
        'b: 'c,
    {
        format!("{} {} {}", self.primary, self.secondary, other)
    }

    // Method returning reference with shortest lifetime
    fn shortest(&self) -> &str {
        if self.primary.len() < self.secondary.len() {
            self.primary
        } else {
            self.secondary
        }
    }
}

// Lifetime bounds in trait definitions
trait DataProcessor<'a> {
    type Output: 'a;

    fn process(&self, input: &'a str) -> Self::Output;
    fn get_metadata(&self) -> &'a str;
}

struct StringProcessor<'a> {
    prefix: &'a str,
    suffix: &'a str,
}

impl<'a> StringProcessor<'a> {
    fn new(prefix: &'a str, suffix: &'a str) -> Self {
        StringProcessor { prefix, suffix }
    }
}

impl<'a> DataProcessor<'a> for StringProcessor<'a> {
    type Output = String;

    fn process(&self, input: &'a str) -> Self::Output {
        format!("{}{}{}", self.prefix, input, self.suffix)
    }

    fn get_metadata(&self) -> &'a str {
        self.prefix
    }
}

// Higher-ranked lifetime bounds
fn process_with_any_lifetime<F>(processor: F) -> String
where
    F: for<'a> Fn(&'a str) -> String,
{
    let data = "test data";
    processor(data)
}

// Multiple lifetime parameters in functions
fn compare_strings<'a, 'b>(s1: &'a str, s2: &'b str) -> &'static str
where
    'a: 'static,
    'b: 'static,
{
    if s1.len() > s2.len() {
        "first is longer"
    } else {
        "second is longer or equal"
    }
}

// Lifetime subtyping examples
fn lifetime_subtyping<'a, 'b>(long: &'a str, short: &'b str) -> &'a str
where
    'a: 'b, // 'a outlives 'b
{
    if long.contains(short) {
        long
    } else {
        // This works because 'a: 'b, so &'a str can be treated as &'b str
        long
    }
}

// Complex struct with multiple lifetime dependencies
struct Graph<'nodes, 'edges>
where
    'nodes: 'edges,
{
    nodes: &'nodes [Node],
    edges: &'edges [Edge<'nodes>],
}

struct Node {
    id: usize,
    data: String,
}

struct Edge<'a> {
    from: &'a Node,
    to: &'a Node,
    weight: f64,
}

impl<'nodes, 'edges> Graph<'nodes, 'edges>
where
    'nodes: 'edges,
{
    fn new(nodes: &'nodes [Node], edges: &'edges [Edge<'nodes>]) -> Self {
        Graph { nodes, edges }
    }

    fn find_node(&self, id: usize) -> Option<&'nodes Node> {
        self.nodes.iter().find(|node| node.id == id)
    }

    fn get_edges_from(&self, node: &'nodes Node) -> Vec<&'edges Edge<'nodes>> {
        self.edges
            .iter()
            .filter(|edge| std::ptr::eq(edge.from, node))
            .collect()
    }

    // Method with additional lifetime parameter
    fn path_exists<'search>(&'search self, from_id: usize, to_id: usize) -> bool {
        // Simplified path finding
        if let (Some(from), Some(to)) = (self.find_node(from_id), self.find_node(to_id)) {
            self.get_edges_from(from)
                .iter()
                .any(|edge| std::ptr::eq(edge.to, to))
        } else {
            false
        }
    }
}

// Function demonstrating lifetime variance
fn variance_example<'a, 'b>(x: &'a str, y: &'b str) -> &'a str
where
    'b: 'a, // 'b outlives 'a
{
    // Can return &'a str or &'b str
    if x.len() > y.len() {
        x // &'a str
    } else {
        y // &'b str, but coerced to &'a str
    }
}

// Testing complex lifetime scenarios
fn test_complex_lifetimes() {
    let primary_data = "primary";
    let secondary_data = "secondary";

    let container = Container::new(primary_data, secondary_data);
    println!("Primary: {}", container.get_primary());
    println!("Secondary: {}", container.get_secondary());

    let additional = "additional";
    let combined = container.combine_with(additional);
    println!("Combined: {}", combined);

    // Test processor
    let prefix = "<<";
    let suffix = ">>";
    let processor = StringProcessor::new(prefix, suffix);
    let processed = processor.process("data");
    println!("Processed: {}", processed);

    // Test higher-ranked trait bounds
    let result = process_with_any_lifetime(|s| format!("Processed: {}", s));
    println!("HRTB result: {}", result);

    // Test graph
    let nodes = vec![
        Node { id: 1, data: "Node 1".to_string() },
        Node { id: 2, data: "Node 2".to_string() },
        Node { id: 3, data: "Node 3".to_string() },
    ];

    let edges = vec![
        Edge { from: &nodes[0], to: &nodes[1], weight: 1.0 },
        Edge { from: &nodes[1], to: &nodes[2], weight: 2.0 },
    ];

    let graph = Graph::new(&nodes, &edges);
    let path_exists = graph.path_exists(1, 2);
    println!("Path exists: {}", path_exists);
}
""",
    )

    run_updater(rust_lifetimes_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    lifetime_calls = [
        call
        for call in calls
        if "Container" in str(call)
        or "Graph" in str(call)
        or "DataProcessor" in str(call)
    ]
    assert len(lifetime_calls) > 0, "Complex lifetime patterns should be detected"


def test_lifetime_elision_rules(
    rust_lifetimes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test lifetime elision rules and implicit lifetimes."""
    test_file = rust_lifetimes_project / "lifetime_elision.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Functions with implicit lifetime elision
fn first_word(s: &str) -> &str {
    let bytes = s.as_bytes();

    for (i, &item) in bytes.iter().enumerate() {
        if item == b' ' {
            return &s[0..i];
        }
    }

    &s[..]
}

fn longest_string(x: &str, y: &str) -> &str {
    if x.len() > y.len() {
        x
    } else {
        y
    }
}

// Method with implicit lifetime elision
struct TextAnalyzer {
    text: String,
}

impl TextAnalyzer {
    fn new(text: String) -> Self {
        TextAnalyzer { text }
    }

    // Implicit lifetime: &self and return have same lifetime
    fn get_text(&self) -> &str {
        &self.text
    }

    // Multiple references - elision applies to &self
    fn find_in_text(&self, needle: &str) -> Option<&str> {
        if self.text.contains(needle) {
            Some(&self.text)
        } else {
            None
        }
    }

    // Method where elision cannot be applied - explicit lifetime needed
    fn compare_with_external<'a>(&self, other: &'a str) -> &str {
        if self.text.len() > other.len() {
            &self.text
        } else {
            // Cannot return other without explicit lifetime
            &self.text
        }
    }

    // Method with multiple parameters and return
    fn combine_text(&self, prefix: &str, suffix: &str) -> String {
        format!("{}{}{}", prefix, self.text, suffix)
    }
}

// Structs with implicit lifetimes
struct ImportantExcerpt<'a> {
    part: &'a str,
}

impl<'a> ImportantExcerpt<'a> {
    // Constructor with lifetime elision
    fn new(text: &'a str) -> ImportantExcerpt<'a> {
        ImportantExcerpt { part: text }
    }

    // Method with implicit lifetime elision
    fn level(&self) -> i32 {
        3
    }

    // Method returning reference with elided lifetime
    fn announce_and_return_part(&self, announcement: &str) -> &str {
        println!("Attention please: {}", announcement);
        self.part
    }

    // Method where explicit lifetime is needed
    fn choose_text<'b>(&self, other: &'b str) -> &str {
        if self.part.len() > other.len() {
            self.part // Returns &'a str
        } else {
            self.part // Cannot return &'b str without explicit handling
        }
    }
}

// Functions demonstrating when elision applies and when it doesn't
fn single_input_reference(s: &str) -> &str {
    // Elision: input and output have same lifetime
    s
}

fn single_input_no_output(s: &str) {
    // Elision: only input, no output reference
    println!("{}", s);
}

fn multiple_inputs_single_output(x: &str, y: &str) -> String {
    // No elision needed: output is owned
    format!("{}{}", x, y)
}

// These require explicit lifetimes
fn multiple_inputs_reference_output<'a>(x: &'a str, y: &str) -> &'a str {
    // Explicit lifetime required: ambiguous which input to tie to output
    x
}

fn no_inputs_static_output() -> &'static str {
    // Static lifetime
    "static string"
}

// Trait with lifetime elision
trait TextProcessor {
    fn process(&self, input: &str) -> &str;
    fn get_name(&self) -> &str;
}

struct UppercaseProcessor {
    name: String,
    buffer: String,
}

impl UppercaseProcessor {
    fn new(name: String) -> Self {
        UppercaseProcessor {
            name,
            buffer: String::new(),
        }
    }
}

impl TextProcessor for UppercaseProcessor {
    // Lifetime elision: &self and return reference have same lifetime
    fn process(&self, input: &str) -> &str {
        // This is a simplified example - real implementation would need
        // to handle lifetime properly or return owned String
        input
    }

    fn get_name(&self) -> &str {
        &self.name
    }
}

// Generic struct with lifetime elision
struct Wrapper<T> {
    value: T,
}

impl<T> Wrapper<T> {
    fn new(value: T) -> Self {
        Wrapper { value }
    }

    fn get(&self) -> &T {
        // Lifetime elision: &self and &T have same lifetime
        &self.value
    }
}

// Testing lifetime elision scenarios
fn test_lifetime_elision() {
    let text = "hello world from rust";
    let first = first_word(text);
    println!("First word: {}", first);

    let analyzer = TextAnalyzer::new("sample text for analysis".to_string());
    println!("Text: {}", analyzer.get_text());

    if let Some(found) = analyzer.find_in_text("text") {
        println!("Found text: {}", found);
    }

    let external = "external";
    let comparison = analyzer.compare_with_external(external);
    println!("Comparison result: {}", comparison);

    // Test ImportantExcerpt
    let novel = "In the beginning was the Word";
    let excerpt = ImportantExcerpt::new(novel);
    println!("Excerpt level: {}", excerpt.level());

    let announced = excerpt.announce_and_return_part("Pay attention!");
    println!("Announced: {}", announced);

    // Test wrapper
    let wrapped_string = Wrapper::new("wrapped value".to_string());
    println!("Wrapped: {}", wrapped_string.get());

    let wrapped_number = Wrapper::new(42);
    println!("Wrapped number: {}", wrapped_number.get());

    // Test processor
    let processor = UppercaseProcessor::new("Uppercase".to_string());
    println!("Processor name: {}", processor.get_name());
    let processed = processor.process("test input");
    println!("Processed: {}", processed);
}

// Advanced elision scenarios
struct AdvancedStruct<'a> {
    data: &'a str,
    metadata: String,
}

impl<'a> AdvancedStruct<'a> {
    // Multiple lifetime scenarios
    fn new(data: &'a str, metadata: String) -> Self {
        AdvancedStruct { data, metadata }
    }

    // Elision with multiple references
    fn get_data(&self) -> &str {
        self.data // Returns &'a str due to elision
    }

    fn get_metadata(&self) -> &str {
        &self.metadata // Returns &str tied to &self lifetime
    }

    // Method that cannot use elision
    fn choose_data_or_input<'b>(&self, input: &'b str) -> &str {
        if self.data.len() > input.len() {
            self.data // &'a str
        } else {
            self.data // Cannot return input without explicit lifetime handling
        }
    }

    // Method with explicit lifetimes where elision could apply
    fn explicit_lifetime_example<'b>(&'b self) -> &'b str {
        self.data // Explicit lifetime parameter overrides elision
    }
}

// Function with complex elision patterns
fn complex_elision_example(
    primary: &str,
    secondary: &str,
    tertiary: &str,
) -> (String, &str, &str) {
    // Returns (owned, ???, ???) - which input lifetimes for references?
    // This would require explicit lifetimes for the reference returns
    (
        format!("{}-{}-{}", primary, secondary, tertiary),
        primary,    // This needs explicit lifetime
        secondary,  // This needs explicit lifetime
    )
}

// Corrected version with explicit lifetimes
fn explicit_complex_example<'a, 'b>(
    primary: &'a str,
    secondary: &'b str,
    tertiary: &str,
) -> (String, &'a str, &'b str) {
    (
        format!("{}-{}-{}", primary, secondary, tertiary),
        primary,
        secondary,
    )
}

fn test_advanced_elision() {
    let data = "important data";
    let metadata = "metadata".to_string();
    let advanced = AdvancedStruct::new(data, metadata);

    println!("Data: {}", advanced.get_data());
    println!("Metadata: {}", advanced.get_metadata());

    let input = "input data";
    let chosen = advanced.choose_data_or_input(input);
    println!("Chosen: {}", chosen);

    let explicit = advanced.explicit_lifetime_example();
    println!("Explicit: {}", explicit);

    // Test complex elision
    let (combined, first, second) = explicit_complex_example("one", "two", "three");
    println!("Combined: {}, First: {}, Second: {}", combined, first, second);
}
""",
    )

    run_updater(rust_lifetimes_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    elision_calls = [
        call
        for call in calls
        if "TextAnalyzer" in str(call)
        or "ImportantExcerpt" in str(call)
        or "elision" in str(call)
    ]
    assert len(elision_calls) > 0, "Lifetime elision patterns should be detected"


def test_borrowing_edge_cases(
    rust_lifetimes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test complex borrowing scenarios and edge cases."""
    test_file = rust_lifetimes_project / "borrowing_edge_cases.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::collections::HashMap;
use std::cell::{RefCell, Ref, RefMut};
use std::rc::Rc;

// Borrowing with multiple mutable references through interior mutability
struct SafeContainer {
    data: RefCell<Vec<String>>,
}

impl SafeContainer {
    fn new() -> Self {
        SafeContainer {
            data: RefCell::new(Vec::new()),
        }
    }

    fn add_item(&self, item: String) {
        self.data.borrow_mut().push(item);
    }

    fn get_item(&self, index: usize) -> Option<String> {
        let data = self.data.borrow();
        data.get(index).cloned()
    }

    fn process_items<F>(&self, mut processor: F)
    where
        F: FnMut(&str) -> String,
    {
        let mut data = self.data.borrow_mut();
        for item in data.iter_mut() {
            *item = processor(item);
        }
    }

    // Method that returns a borrowed reference
    fn borrow_data(&self) -> Ref<Vec<String>> {
        self.data.borrow()
    }

    // Method that returns a mutable borrowed reference
    fn borrow_data_mut(&self) -> RefMut<Vec<String>> {
        self.data.borrow_mut()
    }
}

// Complex borrowing with shared ownership
struct SharedData {
    content: Rc<RefCell<HashMap<String, i32>>>,
}

impl SharedData {
    fn new() -> Self {
        SharedData {
            content: Rc::new(RefCell::new(HashMap::new())),
        }
    }

    fn clone_handle(&self) -> SharedData {
        SharedData {
            content: Rc::clone(&self.content),
        }
    }

    fn insert(&self, key: String, value: i32) {
        self.content.borrow_mut().insert(key, value);
    }

    fn get(&self, key: &str) -> Option<i32> {
        self.content.borrow().get(key).copied()
    }

    fn update_all<F>(&self, updater: F)
    where
        F: Fn(&str, i32) -> i32,
    {
        let mut data = self.content.borrow_mut();
        for (key, value) in data.iter_mut() {
            *value = updater(key, *value);
        }
    }

    // Method that requires multiple borrows
    fn complex_operation(&self, key1: &str, key2: &str) -> Option<i32> {
        let data = self.content.borrow();
        if let (Some(&val1), Some(&val2)) = (data.get(key1), data.get(key2)) {
            Some(val1 + val2)
        } else {
            None
        }
    }
}

// Borrowing with lifetime-dependent structures
struct LifetimeDependentStruct<'a> {
    borrowed_data: &'a mut Vec<i32>,
    index: usize,
}

impl<'a> LifetimeDependentStruct<'a> {
    fn new(data: &'a mut Vec<i32>, index: usize) -> Self {
        LifetimeDependentStruct {
            borrowed_data: data,
            index,
        }
    }

    fn get_current(&self) -> Option<&i32> {
        self.borrowed_data.get(self.index)
    }

    fn set_current(&mut self, value: i32) -> Result<(), &'static str> {
        if self.index < self.borrowed_data.len() {
            self.borrowed_data[self.index] = value;
            Ok(())
        } else {
            Err("Index out of bounds")
        }
    }

    fn move_to_next(&mut self) -> bool {
        if self.index + 1 < self.borrowed_data.len() {
            self.index += 1;
            true
        } else {
            false
        }
    }

    // Method that returns borrowed data with tied lifetime
    fn get_slice(&self) -> &[i32] {
        &self.borrowed_data[self.index..]
    }

    // Method that requires careful lifetime management
    fn swap_with_next(&mut self) -> Result<(), &'static str> {
        if self.index + 1 < self.borrowed_data.len() {
            self.borrowed_data.swap(self.index, self.index + 1);
            Ok(())
        } else {
            Err("Cannot swap with next - at end")
        }
    }
}

// Borrowing across function boundaries
fn process_with_temporary_borrow<F>(data: &mut Vec<String>, processor: F) -> usize
where
    F: Fn(&mut String) -> bool,
{
    let mut count = 0;
    for item in data.iter_mut() {
        if processor(item) {
            count += 1;
        }
    }
    count
}

fn split_and_process(data: &mut Vec<String>, split_index: usize) -> (usize, usize) {
    if split_index >= data.len() {
        return (0, 0);
    }

    let (left, right) = data.split_at_mut(split_index);

    let left_count = left.iter_mut().map(|s| {
        s.push_str("_left");
        1
    }).sum();

    let right_count = right.iter_mut().map(|s| {
        s.push_str("_right");
        1
    }).sum();

    (left_count, right_count)
}

// Borrowing with closures and captures
struct ClosureBorrowingExample {
    data: Vec<i32>,
    multiplier: i32,
}

impl ClosureBorrowingExample {
    fn new(data: Vec<i32>, multiplier: i32) -> Self {
        ClosureBorrowingExample { data, multiplier }
    }

    fn apply_closure<F>(&mut self, mut closure: F)
    where
        F: FnMut(&mut i32),
    {
        for item in &mut self.data {
            closure(item);
        }
    }

    fn create_processor(&self) -> impl Fn(&mut i32) + '_ {
        move |x: &mut i32| {
            *x *= self.multiplier;
        }
    }

    // Method that demonstrates borrowing with move semantics
    fn process_with_move<F>(mut self, processor: F) -> Self
    where
        F: FnOnce(&mut Vec<i32>),
    {
        processor(&mut self.data);
        self
    }

    // Method with complex borrowing patterns
    fn complex_borrowing_operation(&mut self) -> Vec<&i32> {
        // Multiple borrowing phases
        let sum: i32 = self.data.iter().sum();

        // Modify data
        for item in &mut self.data {
            *item += 1;
        }

        // Return references (this creates a new borrow)
        self.data.iter().filter(|&&x| x > sum / self.data.len()).collect()
    }
}

// Testing complex borrowing scenarios
fn test_borrowing_edge_cases() {
    // Test SafeContainer
    let container = SafeContainer::new();
    container.add_item("item1".to_string());
    container.add_item("item2".to_string());

    {
        let data_ref = container.borrow_data();
        println!("Container has {} items", data_ref.len());
    } // Reference dropped here

    container.process_items(|item| format!("processed_{}", item));

    if let Some(item) = container.get_item(0) {
        println!("First item: {}", item);
    }

    // Test SharedData
    let shared1 = SharedData::new();
    let shared2 = shared1.clone_handle();

    shared1.insert("key1".to_string(), 10);
    shared2.insert("key2".to_string(), 20);

    if let Some(sum) = shared1.complex_operation("key1", "key2") {
        println!("Sum: {}", sum);
    }

    shared1.update_all(|_key, value| value * 2);

    // Test LifetimeDependentStruct
    let mut numbers = vec![1, 2, 3, 4, 5];
    {
        let mut dependent = LifetimeDependentStruct::new(&mut numbers, 2);

        if let Some(current) = dependent.get_current() {
            println!("Current value: {}", current);
        }

        dependent.set_current(10).unwrap();
        dependent.move_to_next();

        let slice = dependent.get_slice();
        println!("Remaining slice: {:?}", slice);

        dependent.swap_with_next().unwrap();
    } // dependent dropped here, releasing borrow on numbers

    println!("Modified numbers: {:?}", numbers);

    // Test borrowing across function boundaries
    let mut strings = vec!["hello".to_string(), "world".to_string(), "rust".to_string()];

    let processed_count = process_with_temporary_borrow(&mut strings, |s| {
        s.push('!');
        s.len() > 5
    });
    println!("Processed count: {}", processed_count);

    let (left_count, right_count) = split_and_process(&mut strings, 1);
    println!("Split processing: left={}, right={}", left_count, right_count);

    // Test closure borrowing
    let mut closure_example = ClosureBorrowingExample::new(vec![1, 2, 3, 4], 3);

    closure_example.apply_closure(|x| *x += 5);

    {
        let processor = closure_example.create_processor();
        closure_example.apply_closure(|x| processor(x));
    }

    let complex_refs = closure_example.complex_borrowing_operation();
    println!("Complex refs count: {}", complex_refs.len());

    // Test move semantics with borrowing
    let final_state = closure_example.process_with_move(|data| {
        data.sort();
        data.reverse();
    });

    println!("Final state: {:?}", final_state.data);
}

// Borrowing with async/await (conceptual)
async fn async_borrowing_example() {
    let container = SafeContainer::new();
    container.add_item("async_item".to_string());

    // Simulate async work that doesn't hold borrows across await points
    let item_count = {
        let data = container.borrow_data();
        data.len()
    }; // Borrow dropped before await

    tokio::time::sleep(std::time::Duration::from_millis(1)).await;

    println!("Async: {} items", item_count);
}

// Borrowing with iterators and lazy evaluation
fn iterator_borrowing_patterns() {
    let mut data = vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10];

    // Complex iterator chain with borrowing
    let result: Vec<i32> = data
        .iter()
        .enumerate()
        .filter(|(i, _)| i % 2 == 0)
        .map(|(_, &value)| value * 2)
        .filter(|&x| x > 4)
        .collect();

    println!("Iterator result: {:?}", result);

    // Mutable iterator borrowing
    data.iter_mut()
        .enumerate()
        .filter(|(i, _)| i % 3 == 0)
        .for_each(|(_, value)| *value *= 10);

    println!("Modified data: {:?}", data);

    // Iterator with complex lifetime relationships
    let groups: Vec<Vec<&i32>> = data
        .chunks(3)
        .map(|chunk| chunk.iter().collect())
        .collect();

    for (i, group) in groups.iter().enumerate() {
        println!("Group {}: {:?}", i, group);
    }
}
""",
    )

    run_updater(rust_lifetimes_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    borrowing_calls = [
        call
        for call in calls
        if "SafeContainer" in str(call)
        or "SharedData" in str(call)
        or "LifetimeDependentStruct" in str(call)
    ]
    assert len(borrowing_calls) > 0, "Borrowing edge cases should be detected"


def test_lifetime_variance_and_subtyping(
    rust_lifetimes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test lifetime variance, subtyping, and coercion rules."""
    test_file = rust_lifetimes_project / "lifetime_variance.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Variance in lifetime parameters
struct Covariant<'a> {
    data: &'a str,
}

impl<'a> Covariant<'a> {
    fn new(data: &'a str) -> Self {
        Covariant { data }
    }

    fn get_data(&self) -> &'a str {
        self.data
    }

    // Covariant subtyping: 'longer can be used where 'shorter is expected
    fn accepts_shorter<'short>(&self, other: Covariant<'short>) -> &'a str
    where
        'a: 'short, // 'a outlives 'short
    {
        self.data
    }
}

// Contravariant example (function parameters)
struct Contravariant<'a> {
    processor: fn(&'a str) -> String,
}

impl<'a> Contravariant<'a> {
    fn new(processor: fn(&'a str) -> String) -> Self {
        Contravariant { processor }
    }

    fn process(&self, data: &'a str) -> String {
        (self.processor)(data)
    }

    // Contravariant: function that accepts 'longer can be used where 'shorter is expected
    fn accepts_longer_fn<'long>(processor: fn(&'long str) -> String) -> Contravariant<'a>
    where
        'long: 'a, // 'long outlives 'a
    {
        Contravariant { processor }
    }
}

// Invariant example (mutable references)
struct Invariant<'a> {
    data: &'a mut String,
}

impl<'a> Invariant<'a> {
    fn new(data: &'a mut String) -> Self {
        Invariant { data }
    }

    fn get_mut(&mut self) -> &mut String {
        self.data
    }

    // Invariant: exact lifetime match required
    fn swap_data(&mut self, other: &mut Invariant<'a>) {
        std::mem::swap(&mut self.data, &mut other.data);
    }
}

// Lifetime subtyping examples
fn lifetime_subtyping_demo<'long, 'short>(
    long_data: &'long str,
    short_data: &'short str,
) -> &'short str
where
    'long: 'short, // 'long is a subtype of 'short
{
    // Can return either long_data or short_data
    if long_data.len() > short_data.len() {
        long_data // &'long str is coerced to &'short str
    } else {
        short_data
    }
}

// Higher-ranked trait bounds with variance
fn higher_ranked_variance<F>(f: F) -> String
where
    F: for<'a> Fn(&'a str) -> &'a str,
{
    let data = "test";
    f(data).to_string()
}

// Complex variance with multiple parameters
struct ComplexVariance<'a, 'b> {
    covariant_a: &'a str,
    covariant_b: &'b str,
    contravariant: fn(&'a str, &'b str) -> String,
}

impl<'a, 'b> ComplexVariance<'a, 'b> {
    fn new(
        covariant_a: &'a str,
        covariant_b: &'b str,
        contravariant: fn(&'a str, &'b str) -> String,
    ) -> Self {
        ComplexVariance {
            covariant_a,
            covariant_b,
            contravariant,
        }
    }

    fn process(&self) -> String {
        (self.contravariant)(self.covariant_a, self.covariant_b)
    }

    // Method demonstrating subtyping relationships
    fn extend_lifetimes<'longer_a, 'longer_b>(
        &self,
    ) -> ComplexVariance<'longer_a, 'longer_b>
    where
        'longer_a: 'a,
        'longer_b: 'b,
    {
        // This would require careful handling of the contravariant function
        // In practice, this demonstrates the complexity of variance
        ComplexVariance {
            covariant_a: self.covariant_a,
            covariant_b: self.covariant_b,
            contravariant: self.contravariant,
        }
    }
}

// Phantom variance markers
use std::marker::PhantomData;

struct PhantomCovariant<'a> {
    _phantom: PhantomData<&'a ()>,
    data: String,
}

impl<'a> PhantomCovariant<'a> {
    fn new(data: String) -> Self {
        PhantomCovariant {
            _phantom: PhantomData,
            data,
        }
    }

    fn get_data(&self) -> &str {
        &self.data
    }
}

struct PhantomContravariant<'a> {
    _phantom: PhantomData<fn(&'a ())>,
    data: String,
}

impl<'a> PhantomContravariant<'a> {
    fn new(data: String) -> Self {
        PhantomContravariant {
            _phantom: PhantomData,
            data,
        }
    }

    fn get_data(&self) -> &str {
        &self.data
    }
}

struct PhantomInvariant<'a> {
    _phantom: PhantomData<fn(&'a ()) -> &'a ()>,
    data: String,
}

impl<'a> PhantomInvariant<'a> {
    fn new(data: String) -> Self {
        PhantomInvariant {
            _phantom: PhantomData,
            data,
        }
    }

    fn get_data(&self) -> &str {
        &self.data
    }
}

// Trait object variance
trait VariantTrait<'a> {
    fn get_data(&self) -> &'a str;
}

struct TraitObjectVariance<'a> {
    trait_obj: Box<dyn VariantTrait<'a> + 'a>,
}

impl<'a> TraitObjectVariance<'a> {
    fn new(trait_obj: Box<dyn VariantTrait<'a> + 'a>) -> Self {
        TraitObjectVariance { trait_obj }
    }

    fn get_data(&self) -> &'a str {
        self.trait_obj.get_data()
    }

    // Subtyping with trait objects
    fn accepts_subtype<'sub>(&self) -> &'sub str
    where
        'a: 'sub,
    {
        self.trait_obj.get_data()
    }
}

// Implementation for trait object variance
struct ConcreteVariant {
    data: String,
}

impl ConcreteVariant {
    fn new(data: String) -> Self {
        ConcreteVariant { data }
    }
}

impl<'a> VariantTrait<'a> for ConcreteVariant {
    fn get_data(&self) -> &'a str {
        // This is problematic - lifetime mismatch
        // In practice, would need different approach
        &self.data
    }
}

// Testing variance and subtyping
fn test_variance_and_subtyping() {
    // Test covariant structure
    let long_lived = "long lived data";
    let covariant_long = Covariant::new(long_lived);

    {
        let short_lived = "short";
        let covariant_short = Covariant::new(short_lived);

        // This demonstrates covariance - longer lifetime can be used where shorter is expected
        let result = covariant_long.accepts_shorter(covariant_short);
        println!("Covariant result: {}", result);
    }

    // Test contravariant structure
    let processor_fn = |s: &str| format!("Processed: {}", s);
    let contravariant = Contravariant::new(processor_fn);
    let result = contravariant.process("test data");
    println!("Contravariant result: {}", result);

    // Test invariant structure
    let mut string1 = "mutable1".to_string();
    let mut string2 = "mutable2".to_string();

    {
        let mut invariant1 = Invariant::new(&mut string1);
        let mut invariant2 = Invariant::new(&mut string2);

        // Swap requires exact lifetime match (invariant)
        invariant1.swap_data(&mut invariant2);

        println!("After swap: {}, {}", invariant1.get_mut(), invariant2.get_mut());
    }

    // Test lifetime subtyping
    let long_data = "long lasting data";
    {
        let short_data = "short";
        let result = lifetime_subtyping_demo(long_data, short_data);
        println!("Subtyping result: {}", result);
    }

    // Test higher-ranked trait bounds
    let hr_result = higher_ranked_variance(|s| &s[0..s.len().min(4)]);
    println!("Higher-ranked result: {}", hr_result);

    // Test complex variance
    let complex = ComplexVariance::new(
        long_lived,
        "other data",
        |a, b| format!("{} + {}", a, b),
    );
    let complex_result = complex.process();
    println!("Complex variance result: {}", complex_result);

    // Test phantom variance
    let phantom_cov = PhantomCovariant::<'static>::new("phantom covariant".to_string());
    println!("Phantom covariant: {}", phantom_cov.get_data());

    let phantom_contra = PhantomContravariant::<'static>::new("phantom contravariant".to_string());
    println!("Phantom contravariant: {}", phantom_contra.get_data());

    let phantom_inv = PhantomInvariant::<'static>::new("phantom invariant".to_string());
    println!("Phantom invariant: {}", phantom_inv.get_data());
}

// Advanced variance examples with closures
fn closure_variance_examples() {
    // Closure that captures with different variance properties
    let data = "captured data";

    // Covariant closure (only reads the captured data)
    let covariant_closure = || {
        println!("Covariant: {}", data);
        data
    };

    // Test the closure
    let result = covariant_closure();
    println!("Closure result: {}", result);

    // Function that accepts closures with variance
    fn accepts_closure<F>(f: F) -> String
    where
        F: Fn() -> &'static str,
    {
        f().to_string()
    }

    // This would work if data has 'static lifetime
    // let closure_result = accepts_closure(covariant_closure);

    // Demonstrating variance with mutable captures
    let mut mutable_data = vec![1, 2, 3];

    let invariant_closure = || {
        mutable_data.push(4);
        &mut mutable_data
    };

    // Call the closure
    let vec_ref = invariant_closure();
    println!("Invariant closure result: {:?}", vec_ref);
}

// Variance with generic parameters
struct GenericVariance<'a, T> {
    data: &'a T,
    _phantom: PhantomData<T>,
}

impl<'a, T> GenericVariance<'a, T> {
    fn new(data: &'a T) -> Self {
        GenericVariance {
            data,
            _phantom: PhantomData,
        }
    }

    fn get_data(&self) -> &'a T {
        self.data
    }

    // Method demonstrating variance with generic types
    fn variance_with_generics<'b, U>(&self, other: &'b U) -> (&'a T, &'b U)
    where
        'a: 'b,
    {
        (self.data, other)
    }
}

fn test_generic_variance() {
    let number = 42;
    let string = "test".to_string();

    let generic_num = GenericVariance::new(&number);
    let generic_str = GenericVariance::new(&string);

    let (num_ref, str_ref) = generic_num.variance_with_generics(generic_str.get_data());
    println!("Generic variance: {} and {}", num_ref, str_ref);
}
""",
    )

    run_updater(rust_lifetimes_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    variance_calls = [
        call
        for call in calls
        if "Covariant" in str(call)
        or "Contravariant" in str(call)
        or "Invariant" in str(call)
    ]
    assert len(variance_calls) > 0, "Lifetime variance patterns should be detected"
