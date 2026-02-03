from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_smart_pointers_project(temp_repo: Path) -> Path:
    """Create a Rust project structure for smart pointers testing."""
    project_path = temp_repo / "rust_smart_pointers_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Smart pointers test crate"
    )

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""[package]
name = "rust_smart_pointers_test"
version = "0.1.0"
edition = "2021"
""",
    )

    return project_path


def test_box_pointer_patterns(
    rust_smart_pointers_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Box smart pointer patterns and heap allocation."""
    test_file = rust_smart_pointers_project / "box_patterns.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic Box usage
struct LargeStruct {
    data: [u8; 1024],
    metadata: String,
}

impl LargeStruct {
    fn new(value: u8, metadata: String) -> Self {
        LargeStruct {
            data: [value; 1024],
            metadata,
        }
    }

    fn get_checksum(&self) -> u32 {
        self.data.iter().map(|&x| x as u32).sum()
    }
}

// Recursive data structures with Box
#[derive(Debug)]
enum List<T> {
    Cons(T, Box<List<T>>),
    Nil,
}

impl<T> List<T> {
    fn new() -> Self {
        List::Nil
    }

    fn prepend(self, elem: T) -> Self {
        List::Cons(elem, Box::new(self))
    }

    fn len(&self) -> usize {
        match self {
            List::Cons(_, tail) => 1 + tail.len(),
            List::Nil => 0,
        }
    }

    fn head(&self) -> Option<&T> {
        match self {
            List::Cons(head, _) => Some(head),
            List::Nil => None,
        }
    }

    fn tail(&self) -> Option<&List<T>> {
        match self {
            List::Cons(_, tail) => Some(tail),
            List::Nil => None,
        }
    }
}

// Binary tree with Box
#[derive(Debug)]
struct TreeNode<T> {
    value: T,
    left: Option<Box<TreeNode<T>>>,
    right: Option<Box<TreeNode<T>>>,
}

impl<T> TreeNode<T>
where
    T: PartialOrd + Clone,
{
    fn new(value: T) -> Self {
        TreeNode {
            value,
            left: None,
            right: None,
        }
    }

    fn insert(&mut self, new_value: T) {
        if new_value <= self.value {
            match &mut self.left {
                Some(left_node) => left_node.insert(new_value),
                None => self.left = Some(Box::new(TreeNode::new(new_value))),
            }
        } else {
            match &mut self.right {
                Some(right_node) => right_node.insert(new_value),
                None => self.right = Some(Box::new(TreeNode::new(new_value))),
            }
        }
    }

    fn search(&self, target: &T) -> bool {
        if &self.value == target {
            true
        } else if target < &self.value {
            self.left.as_ref().map_or(false, |left| left.search(target))
        } else {
            self.right.as_ref().map_or(false, |right| right.search(target))
        }
    }

    fn inorder_traversal(&self) -> Vec<T> {
        let mut result = Vec::new();

        if let Some(left) = &self.left {
            result.extend(left.inorder_traversal());
        }

        result.push(self.value.clone());

        if let Some(right) = &self.right {
            result.extend(right.inorder_traversal());
        }

        result
    }
}

// Box with trait objects
trait Processor {
    fn process(&self, input: &str) -> String;
    fn name(&self) -> &str;
}

struct UppercaseProcessor {
    name: String,
}

impl UppercaseProcessor {
    fn new(name: String) -> Self {
        UppercaseProcessor { name }
    }
}

impl Processor for UppercaseProcessor {
    fn process(&self, input: &str) -> String {
        input.to_uppercase()
    }

    fn name(&self) -> &str {
        &self.name
    }
}

struct ReverseProcessor {
    name: String,
}

impl ReverseProcessor {
    fn new(name: String) -> Self {
        ReverseProcessor { name }
    }
}

impl Processor for ReverseProcessor {
    fn process(&self, input: &str) -> String {
        input.chars().rev().collect()
    }

    fn name(&self) -> &str {
        &self.name
    }
}

// Processing chain with boxed trait objects
struct ProcessingChain {
    processors: Vec<Box<dyn Processor>>,
}

impl ProcessingChain {
    fn new() -> Self {
        ProcessingChain {
            processors: Vec::new(),
        }
    }

    fn add_processor(&mut self, processor: Box<dyn Processor>) {
        self.processors.push(processor);
    }

    fn process(&self, mut input: String) -> String {
        for processor in &self.processors {
            input = processor.process(&input);
        }
        input
    }

    fn describe(&self) -> String {
        let names: Vec<&str> = self.processors.iter().map(|p| p.name()).collect();
        format!("Chain: [{}]", names.join(" -> "))
    }
}

fn test_box_patterns() {
    // Test large struct on heap
    let large = Box::new(LargeStruct::new(42, "test metadata".to_string()));
    println!("Large struct checksum: {}", large.get_checksum());

    // Test recursive list
    let list = List::new()
        .prepend(3)
        .prepend(2)
        .prepend(1);

    println!("List length: {}", list.len());
    if let Some(head) = list.head() {
        println!("List head: {}", head);
    }

    // Test binary tree
    let mut tree = TreeNode::new(5);
    tree.insert(3);
    tree.insert(7);
    tree.insert(1);
    tree.insert(9);

    println!("Tree contains 7: {}", tree.search(&7));
    println!("Tree contains 4: {}", tree.search(&4));
    println!("Inorder traversal: {:?}", tree.inorder_traversal());

    // Test processing chain
    let mut chain = ProcessingChain::new();
    chain.add_processor(Box::new(UppercaseProcessor::new("Upper".to_string())));
    chain.add_processor(Box::new(ReverseProcessor::new("Reverse".to_string())));

    println!("{}", chain.describe());
    let result = chain.process("hello world".to_string());
    println!("Processing result: {}", result);
}

// Box leak and into_raw patterns
fn box_raw_patterns() {
    // Convert Box to raw pointer
    let boxed = Box::new(42);
    let raw_ptr = Box::into_raw(boxed);

    unsafe {
        println!("Raw pointer value: {}", *raw_ptr);

        // Convert back to Box
        let restored = Box::from_raw(raw_ptr);
        println!("Restored value: {}", *restored);
    }

    // Box leak for 'static references
    let leaked: &'static mut i32 = Box::leak(Box::new(100));
    *leaked += 1;
    println!("Leaked value: {}", *leaked);
}
""",
    )

    run_updater(rust_smart_pointers_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    box_calls = [
        call
        for call in calls
        if "LargeStruct" in str(call)
        or "TreeNode" in str(call)
        or "ProcessingChain" in str(call)
    ]
    assert len(box_calls) > 0, "Box pointer patterns should be detected"


def test_rc_reference_counting(
    rust_smart_pointers_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Rc reference counting patterns and shared ownership."""
    test_file = rust_smart_pointers_project / "rc_patterns.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::rc::{Rc, Weak};
use std::cell::RefCell;

// Shared ownership with Rc
#[derive(Debug)]
struct SharedData {
    id: u32,
    content: String,
}

impl SharedData {
    fn new(id: u32, content: String) -> Self {
        SharedData { id, content }
    }

    fn display_info(&self) {
        println!("Data {}: {}", self.id, self.content);
    }
}

// Multiple owners of shared data
struct DataOwner {
    name: String,
    shared_data: Rc<SharedData>,
}

impl DataOwner {
    fn new(name: String, shared_data: Rc<SharedData>) -> Self {
        DataOwner { name, shared_data }
    }

    fn access_data(&self) {
        println!("{} accessing:", self.name);
        self.shared_data.display_info();
    }

    fn reference_count(&self) -> usize {
        Rc::strong_count(&self.shared_data)
    }
}

// Graph structure with Rc
#[derive(Debug)]
struct GraphNode {
    id: u32,
    data: String,
    edges: RefCell<Vec<Rc<GraphNode>>>,
}

impl GraphNode {
    fn new(id: u32, data: String) -> Rc<Self> {
        Rc::new(GraphNode {
            id,
            data,
            edges: RefCell::new(Vec::new()),
        })
    }

    fn add_edge(self: &Rc<Self>, target: &Rc<GraphNode>) {
        self.edges.borrow_mut().push(Rc::clone(target));
    }

    fn get_neighbors(&self) -> Vec<Rc<GraphNode>> {
        self.edges.borrow().clone()
    }

    fn neighbor_count(&self) -> usize {
        self.edges.borrow().len()
    }

    fn display(&self) {
        println!("Node {}: {} (ref_count: {})",
                 self.id,
                 self.data,
                 Rc::strong_count(&Rc::new(GraphNode {
                     id: 0,
                     data: String::new(),
                     edges: RefCell::new(Vec::new()),
                 })));
    }
}

// Circular reference problem and solution with Weak
#[derive(Debug)]
struct Parent {
    children: RefCell<Vec<Rc<Child>>>,
    name: String,
}

#[derive(Debug)]
struct Child {
    parent: Weak<Parent>,
    name: String,
}

impl Parent {
    fn new(name: String) -> Rc<Self> {
        Rc::new(Parent {
            children: RefCell::new(Vec::new()),
            name,
        })
    }

    fn add_child(self: &Rc<Self>, child_name: String) {
        let child = Rc::new(Child {
            parent: Rc::downgrade(self),
            name: child_name,
        });
        self.children.borrow_mut().push(child);
    }

    fn list_children(&self) {
        println!("Parent {} has children:", self.name);
        for child in self.children.borrow().iter() {
            println!("  - {}", child.name);
        }
    }

    fn child_count(&self) -> usize {
        self.children.borrow().len()
    }
}

impl Child {
    fn get_parent_name(&self) -> Option<String> {
        self.parent.upgrade().map(|parent| parent.name.clone())
    }

    fn is_parent_alive(&self) -> bool {
        self.parent.upgrade().is_some()
    }
}

// Shared mutable state with Rc<RefCell<T>>
struct SharedCounter {
    value: Rc<RefCell<i32>>,
}

impl SharedCounter {
    fn new(initial: i32) -> Self {
        SharedCounter {
            value: Rc::new(RefCell::new(initial)),
        }
    }

    fn clone_handle(&self) -> Self {
        SharedCounter {
            value: Rc::clone(&self.value),
        }
    }

    fn increment(&self) {
        *self.value.borrow_mut() += 1;
    }

    fn decrement(&self) {
        *self.value.borrow_mut() -= 1;
    }

    fn get(&self) -> i32 {
        *self.value.borrow()
    }

    fn set(&self, new_value: i32) {
        *self.value.borrow_mut() = new_value;
    }

    fn reference_count(&self) -> usize {
        Rc::strong_count(&self.value)
    }
}

fn test_rc_patterns() {
    // Test shared data ownership
    let shared_data = Rc::new(SharedData::new(1, "Shared content".to_string()));

    let owner1 = DataOwner::new("Owner1".to_string(), Rc::clone(&shared_data));
    let owner2 = DataOwner::new("Owner2".to_string(), Rc::clone(&shared_data));
    let owner3 = DataOwner::new("Owner3".to_string(), Rc::clone(&shared_data));

    println!("Reference count: {}", owner1.reference_count());

    owner1.access_data();
    owner2.access_data();
    owner3.access_data();

    // Test graph with Rc
    let node1 = GraphNode::new(1, "Node 1".to_string());
    let node2 = GraphNode::new(2, "Node 2".to_string());
    let node3 = GraphNode::new(3, "Node 3".to_string());

    node1.add_edge(&node2);
    node1.add_edge(&node3);
    node2.add_edge(&node3);

    println!("Node 1 neighbors: {}", node1.neighbor_count());
    println!("Node 2 neighbors: {}", node2.neighbor_count());

    // Test parent-child with weak references
    let parent = Parent::new("Parent".to_string());
    parent.add_child("Child1".to_string());
    parent.add_child("Child2".to_string());

    parent.list_children();
    println!("Parent has {} children", parent.child_count());

    // Access child and check parent
    if let Some(first_child) = parent.children.borrow().first() {
        if let Some(parent_name) = first_child.get_parent_name() {
            println!("Child's parent: {}", parent_name);
        }
        println!("Parent alive: {}", first_child.is_parent_alive());
    }

    // Test shared counter
    let counter = SharedCounter::new(0);
    let counter_handle1 = counter.clone_handle();
    let counter_handle2 = counter.clone_handle();

    println!("Initial counter reference count: {}", counter.reference_count());

    counter.increment();
    counter_handle1.increment();
    counter_handle2.increment();

    println!("Counter value: {}", counter.get());

    counter_handle1.set(100);
    println!("Updated counter value: {}", counter_handle2.get());
}

// Advanced Rc patterns
fn advanced_rc_patterns() {
    // Rc with custom Drop
    struct CustomDrop {
        name: String,
    }

    impl Drop for CustomDrop {
        fn drop(&mut self) {
            println!("Dropping CustomDrop: {}", self.name);
        }
    }

    {
        let custom = Rc::new(CustomDrop {
            name: "Custom1".to_string(),
        });
        let custom_clone = Rc::clone(&custom);

        println!("Custom reference count: {}", Rc::strong_count(&custom));
    } // custom and custom_clone dropped here

    // Weak reference cycles
    let weak_example = {
        let strong = Rc::new("Strong reference".to_string());
        let weak = Rc::downgrade(&strong);

        println!("Strong count: {}", Rc::strong_count(&strong));
        println!("Weak count: {}", Rc::weak_count(&strong));

        // Try to upgrade weak reference
        if let Some(upgraded) = weak.upgrade() {
            println!("Upgraded weak: {}", *upgraded);
        }

        weak
    }; // strong is dropped here

    // Try to upgrade after strong is dropped
    if weak_example.upgrade().is_none() {
        println!("Weak reference can no longer be upgraded");
    }
}

// Rc with trait objects
trait Drawable {
    fn draw(&self) -> String;
    fn area(&self) -> f64;
}

struct Circle {
    radius: f64,
}

impl Circle {
    fn new(radius: f64) -> Self {
        Circle { radius }
    }
}

impl Drawable for Circle {
    fn draw(&self) -> String {
        format!("Circle with radius {}", self.radius)
    }

    fn area(&self) -> f64 {
        std::f64::consts::PI * self.radius * self.radius
    }
}

struct Rectangle {
    width: f64,
    height: f64,
}

impl Rectangle {
    fn new(width: f64, height: f64) -> Self {
        Rectangle { width, height }
    }
}

impl Drawable for Rectangle {
    fn draw(&self) -> String {
        format!("Rectangle {}x{}", self.width, self.height)
    }

    fn area(&self) -> f64 {
        self.width * self.height
    }
}

struct Canvas {
    shapes: Vec<Rc<dyn Drawable>>,
}

impl Canvas {
    fn new() -> Self {
        Canvas {
            shapes: Vec::new(),
        }
    }

    fn add_shape(&mut self, shape: Rc<dyn Drawable>) {
        self.shapes.push(shape);
    }

    fn draw_all(&self) -> Vec<String> {
        self.shapes.iter().map(|shape| shape.draw()).collect()
    }

    fn total_area(&self) -> f64 {
        self.shapes.iter().map(|shape| shape.area()).sum()
    }

    fn shape_count(&self) -> usize {
        self.shapes.len()
    }
}

fn test_rc_trait_objects() {
    let mut canvas = Canvas::new();

    let circle: Rc<dyn Drawable> = Rc::new(Circle::new(5.0));
    let rectangle: Rc<dyn Drawable> = Rc::new(Rectangle::new(10.0, 8.0));

    // Shapes can be shared across multiple canvases
    canvas.add_shape(Rc::clone(&circle));
    canvas.add_shape(Rc::clone(&rectangle));

    let mut canvas2 = Canvas::new();
    canvas2.add_shape(circle); // Move ownership
    canvas2.add_shape(Rc::clone(&rectangle));

    println!("Canvas 1 shapes: {:?}", canvas.draw_all());
    println!("Canvas 1 total area: {:.2}", canvas.total_area());

    println!("Canvas 2 shapes: {:?}", canvas2.draw_all());
    println!("Canvas 2 total area: {:.2}", canvas2.total_area());

    println!("Rectangle reference count: {}", Rc::strong_count(&rectangle));
}
""",
    )

    run_updater(rust_smart_pointers_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    rc_calls = [
        call
        for call in calls
        if "SharedData" in str(call)
        or "GraphNode" in str(call)
        or "SharedCounter" in str(call)
    ]
    assert len(rc_calls) > 0, "Rc reference counting patterns should be detected"


def test_arc_atomic_reference_counting(
    rust_smart_pointers_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Arc atomic reference counting for thread safety."""
    test_file = rust_smart_pointers_project / "arc_patterns.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::sync::{Arc, Mutex, RwLock};
use std::thread;
use std::time::Duration;

// Thread-safe shared data with Arc
#[derive(Debug)]
struct ThreadSafeData {
    id: u32,
    content: String,
}

impl ThreadSafeData {
    fn new(id: u32, content: String) -> Self {
        ThreadSafeData { id, content }
    }

    fn display(&self) {
        println!("Thread-safe data {}: {}", self.id, self.content);
    }
}

// Shared mutable state with Arc<Mutex<T>>
struct SharedMutexCounter {
    value: Arc<Mutex<i32>>,
}

impl SharedMutexCounter {
    fn new(initial: i32) -> Self {
        SharedMutexCounter {
            value: Arc::new(Mutex::new(initial)),
        }
    }

    fn clone_handle(&self) -> Self {
        SharedMutexCounter {
            value: Arc::clone(&self.value),
        }
    }

    fn increment(&self) -> Result<(), Box<dyn std::error::Error>> {
        let mut guard = self.value.lock()?;
        *guard += 1;
        Ok(())
    }

    fn get(&self) -> Result<i32, Box<dyn std::error::Error>> {
        let guard = self.value.lock()?;
        Ok(*guard)
    }

    fn reference_count(&self) -> usize {
        Arc::strong_count(&self.value)
    }
}

// Read-write shared state with Arc<RwLock<T>>
struct SharedRwData {
    data: Arc<RwLock<Vec<String>>>,
}

impl SharedRwData {
    fn new() -> Self {
        SharedRwData {
            data: Arc::new(RwLock::new(Vec::new())),
        }
    }

    fn clone_handle(&self) -> Self {
        SharedRwData {
            data: Arc::clone(&self.data),
        }
    }

    fn add_item(&self, item: String) -> Result<(), Box<dyn std::error::Error>> {
        let mut write_guard = self.data.write()?;
        write_guard.push(item);
        Ok(())
    }

    fn read_all(&self) -> Result<Vec<String>, Box<dyn std::error::Error>> {
        let read_guard = self.data.read()?;
        Ok(read_guard.clone())
    }

    fn len(&self) -> Result<usize, Box<dyn std::error::Error>> {
        let read_guard = self.data.read()?;
        Ok(read_guard.len())
    }

    fn contains(&self, item: &str) -> Result<bool, Box<dyn std::error::Error>> {
        let read_guard = self.data.read()?;
        Ok(read_guard.iter().any(|s| s == item))
    }
}

// Work distribution system with Arc
struct WorkItem {
    id: u32,
    description: String,
    completed: bool,
}

impl WorkItem {
    fn new(id: u32, description: String) -> Self {
        WorkItem {
            id,
            description,
            completed: false,
        }
    }

    fn complete(&mut self) {
        self.completed = true;
    }
}

struct WorkQueue {
    items: Arc<Mutex<Vec<WorkItem>>>,
}

impl WorkQueue {
    fn new() -> Self {
        WorkQueue {
            items: Arc::new(Mutex::new(Vec::new())),
        }
    }

    fn clone_handle(&self) -> Self {
        WorkQueue {
            items: Arc::clone(&self.items),
        }
    }

    fn add_work(&self, item: WorkItem) -> Result<(), Box<dyn std::error::Error>> {
        let mut guard = self.items.lock()?;
        guard.push(item);
        Ok(())
    }

    fn take_work(&self) -> Result<Option<WorkItem>, Box<dyn std::error::Error>> {
        let mut guard = self.items.lock()?;
        Ok(guard.pop())
    }

    fn pending_count(&self) -> Result<usize, Box<dyn std::error::Error>> {
        let guard = self.items.lock()?;
        Ok(guard.iter().filter(|item| !item.completed).count())
    }

    fn completed_count(&self) -> Result<usize, Box<dyn std::error::Error>> {
        let guard = self.items.lock()?;
        Ok(guard.iter().filter(|item| item.completed).count())
    }
}

fn test_arc_patterns() {
    // Test shared data across threads
    let shared_data = Arc::new(ThreadSafeData::new(1, "Shared across threads".to_string()));

    let mut handles = Vec::new();

    for i in 0..3 {
        let data_clone = Arc::clone(&shared_data);
        let handle = thread::spawn(move || {
            println!("Thread {} accessing:", i);
            data_clone.display();
            thread::sleep(Duration::from_millis(100));
        });
        handles.push(handle);
    }

    for handle in handles {
        handle.join().unwrap();
    }

    println!("Final reference count: {}", Arc::strong_count(&shared_data));

    // Test shared mutex counter
    let counter = SharedMutexCounter::new(0);
    let mut thread_handles = Vec::new();

    for i in 0..5 {
        let counter_clone = counter.clone_handle();
        let handle = thread::spawn(move || {
            for _ in 0..10 {
                counter_clone.increment().unwrap();
                thread::sleep(Duration::from_millis(1));
            }
            println!("Thread {} finished incrementing", i);
        });
        thread_handles.push(handle);
    }

    for handle in thread_handles {
        handle.join().unwrap();
    }

    println!("Final counter value: {}", counter.get().unwrap());

    // Test RwLock data
    let rw_data = SharedRwData::new();
    let mut rw_handles = Vec::new();

    // Writer threads
    for i in 0..3 {
        let data_clone = rw_data.clone_handle();
        let handle = thread::spawn(move || {
            data_clone.add_item(format!("Item from thread {}", i)).unwrap();
            thread::sleep(Duration::from_millis(50));
        });
        rw_handles.push(handle);
    }

    // Reader threads
    for i in 0..5 {
        let data_clone = rw_data.clone_handle();
        let handle = thread::spawn(move || {
            thread::sleep(Duration::from_millis(25));
            let len = data_clone.len().unwrap();
            println!("Reader thread {} sees {} items", i, len);
        });
        rw_handles.push(handle);
    }

    for handle in rw_handles {
        handle.join().unwrap();
    }

    println!("Final data: {:?}", rw_data.read_all().unwrap());
}

// Work queue demonstration
fn test_work_queue() -> Result<(), Box<dyn std::error::Error>> {
    let work_queue = WorkQueue::new();

    // Add some work items
    for i in 1..=10 {
        work_queue.add_work(WorkItem::new(i, format!("Task {}", i)))?;
    }

    let mut worker_handles = Vec::new();

    // Spawn worker threads
    for worker_id in 1..=3 {
        let queue_clone = work_queue.clone_handle();
        let handle = thread::spawn(move || -> Result<(), Box<dyn std::error::Error>> {
            loop {
                match queue_clone.take_work()? {
                    Some(mut work_item) => {
                        println!("Worker {} processing: {}", worker_id, work_item.description);
                        thread::sleep(Duration::from_millis(100)); // Simulate work
                        work_item.complete();
                        println!("Worker {} completed: {}", worker_id, work_item.description);
                    }
                    None => {
                        println!("Worker {} found no work, exiting", worker_id);
                        break;
                    }
                }
            }
            Ok(())
        });
        worker_handles.push(handle);
    }

    // Wait for all workers to finish
    for handle in worker_handles {
        handle.join().unwrap()?;
    }

    println!("Pending work: {}", work_queue.pending_count()?);
    Ok(())
}

// Advanced Arc patterns with channels
use std::sync::mpsc;

struct ProducerConsumerSystem {
    data: Arc<RwLock<Vec<String>>>,
}

impl ProducerConsumerSystem {
    fn new() -> Self {
        ProducerConsumerSystem {
            data: Arc::new(RwLock::new(Vec::new())),
        }
    }

    fn run_producers_and_consumers(&self) -> Result<(), Box<dyn std::error::Error>> {
        let (tx, rx) = mpsc::channel();

        // Producer threads
        for producer_id in 1..=3 {
            let data_clone = Arc::clone(&self.data);
            let tx_clone = tx.clone();

            thread::spawn(move || {
                for i in 1..=5 {
                    let item = format!("Producer {} - Item {}", producer_id, i);

                    // Add to shared data
                    if let Ok(mut write_guard) = data_clone.write() {
                        write_guard.push(item.clone());
                    }

                    // Send notification
                    tx_clone.send(item).unwrap();
                    thread::sleep(Duration::from_millis(100));
                }
            });
        }

        drop(tx); // Close the sending side

        // Consumer thread
        let data_clone = Arc::clone(&self.data);
        let consumer_handle = thread::spawn(move || {
            while let Ok(item) = rx.recv() {
                println!("Consumer received: {}", item);

                // Read current state
                if let Ok(read_guard) = data_clone.read() {
                    println!("Current data size: {}", read_guard.len());
                }

                thread::sleep(Duration::from_millis(50));
            }
            println!("Consumer finished");
        });

        consumer_handle.join().unwrap();

        // Final state
        let final_data = self.data.read()?;
        println!("Final data count: {}", final_data.len());

        Ok(())
    }
}

fn test_producer_consumer() {
    let system = ProducerConsumerSystem::new();
    if let Err(e) = system.run_producers_and_consumers() {
        eprintln!("Error in producer-consumer system: {}", e);
    }
}

// Arc with custom types and trait objects
trait ThreadSafeProcessor: Send + Sync {
    fn process(&self, input: &str) -> String;
    fn name(&self) -> &str;
}

struct UppercaseProcessor {
    name: String,
}

impl UppercaseProcessor {
    fn new(name: String) -> Self {
        UppercaseProcessor { name }
    }
}

impl ThreadSafeProcessor for UppercaseProcessor {
    fn process(&self, input: &str) -> String {
        input.to_uppercase()
    }

    fn name(&self) -> &str {
        &self.name
    }
}

struct ThreadSafeProcessingPool {
    processors: Vec<Arc<dyn ThreadSafeProcessor>>,
}

impl ThreadSafeProcessingPool {
    fn new() -> Self {
        ThreadSafeProcessingPool {
            processors: Vec::new(),
        }
    }

    fn add_processor(&mut self, processor: Arc<dyn ThreadSafeProcessor>) {
        self.processors.push(processor);
    }

    fn process_concurrent(&self, inputs: Vec<String>) -> Vec<String> {
        let mut handles = Vec::new();

        for (i, input) in inputs.into_iter().enumerate() {
            let processor_index = i % self.processors.len();
            let processor = Arc::clone(&self.processors[processor_index]);

            let handle = thread::spawn(move || {
                processor.process(&input)
            });

            handles.push(handle);
        }

        handles
            .into_iter()
            .map(|h| h.join().unwrap())
            .collect()
    }
}

fn test_arc_trait_objects() {
    let mut pool = ThreadSafeProcessingPool::new();

    pool.add_processor(Arc::new(UppercaseProcessor::new("Upper1".to_string())));
    pool.add_processor(Arc::new(UppercaseProcessor::new("Upper2".to_string())));

    let inputs = vec![
        "hello".to_string(),
        "world".to_string(),
        "rust".to_string(),
        "programming".to_string(),
    ];

    let results = pool.process_concurrent(inputs);
    println!("Concurrent processing results: {:?}", results);
}
""",
    )

    run_updater(rust_smart_pointers_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    arc_calls = [
        call
        for call in calls
        if "SharedMutexCounter" in str(call)
        or "WorkQueue" in str(call)
        or "ThreadSafeData" in str(call)
    ]
    assert len(arc_calls) > 0, (
        "Arc atomic reference counting patterns should be detected"
    )


def test_refcell_interior_mutability(
    rust_smart_pointers_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test RefCell interior mutability patterns."""
    test_file = rust_smart_pointers_project / "refcell_patterns.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::cell::{RefCell, Ref, RefMut, Cell};
use std::rc::Rc;

// Basic RefCell usage
struct MockDatabase {
    queries: RefCell<Vec<String>>,
    connection_count: Cell<u32>,
}

impl MockDatabase {
    fn new() -> Self {
        MockDatabase {
            queries: RefCell::new(Vec::new()),
            connection_count: Cell::new(0),
        }
    }

    fn execute_query(&self, query: &str) -> String {
        // Log the query
        self.queries.borrow_mut().push(query.to_string());

        // Increment connection count
        let current = self.connection_count.get();
        self.connection_count.set(current + 1);

        // Return mock result
        format!("Result for: {}", query)
    }

    fn get_query_history(&self) -> Vec<String> {
        self.queries.borrow().clone()
    }

    fn query_count(&self) -> usize {
        self.queries.borrow().len()
    }

    fn connection_count(&self) -> u32 {
        self.connection_count.get()
    }

    fn clear_history(&self) {
        self.queries.borrow_mut().clear();
    }
}

// Complex data structure with interior mutability
struct CacheEntry {
    key: String,
    value: String,
    access_count: Cell<u32>,
    last_accessed: RefCell<std::time::SystemTime>,
}

impl CacheEntry {
    fn new(key: String, value: String) -> Self {
        CacheEntry {
            key,
            value,
            access_count: Cell::new(0),
            last_accessed: RefCell::new(std::time::SystemTime::now()),
        }
    }

    fn get_value(&self) -> String {
        // Update access statistics
        let count = self.access_count.get();
        self.access_count.set(count + 1);

        *self.last_accessed.borrow_mut() = std::time::SystemTime::now();

        self.value.clone()
    }

    fn access_count(&self) -> u32 {
        self.access_count.get()
    }

    fn last_accessed(&self) -> std::time::SystemTime {
        *self.last_accessed.borrow()
    }
}

struct Cache {
    entries: RefCell<std::collections::HashMap<String, Rc<CacheEntry>>>,
    max_size: usize,
}

impl Cache {
    fn new(max_size: usize) -> Self {
        Cache {
            entries: RefCell::new(std::collections::HashMap::new()),
            max_size,
        }
    }

    fn insert(&self, key: String, value: String) {
        let entry = Rc::new(CacheEntry::new(key.clone(), value));
        let mut entries = self.entries.borrow_mut();

        // Simple eviction if over capacity
        if entries.len() >= self.max_size {
            if let Some(first_key) = entries.keys().next().cloned() {
                entries.remove(&first_key);
            }
        }

        entries.insert(key, entry);
    }

    fn get(&self, key: &str) -> Option<String> {
        let entries = self.entries.borrow();
        entries.get(key).map(|entry| entry.get_value())
    }

    fn contains_key(&self, key: &str) -> bool {
        self.entries.borrow().contains_key(key)
    }

    fn size(&self) -> usize {
        self.entries.borrow().len()
    }

    fn access_stats(&self, key: &str) -> Option<(u32, std::time::SystemTime)> {
        let entries = self.entries.borrow();
        entries.get(key).map(|entry| {
            (entry.access_count(), entry.last_accessed())
        })
    }
}

// RefCell with borrowing rules demonstration
struct BorrowingExample {
    data: RefCell<Vec<i32>>,
}

impl BorrowingExample {
    fn new() -> Self {
        BorrowingExample {
            data: RefCell::new(vec![1, 2, 3, 4, 5]),
        }
    }

    fn read_operations(&self) {
        // Multiple immutable borrows are allowed
        let borrow1 = self.data.borrow();
        let borrow2 = self.data.borrow();

        println!("Borrow 1 length: {}", borrow1.len());
        println!("Borrow 2 first element: {:?}", borrow2.first());

        // Borrows are automatically dropped here
    }

    fn write_operation(&self) {
        // Only one mutable borrow is allowed
        let mut borrow = self.data.borrow_mut();
        borrow.push(6);
        borrow.push(7);

        println!("After modification: {:?}", *borrow);

        // Mutable borrow is dropped here
    }

    fn try_borrow_operations(&self) -> Result<(), Box<dyn std::error::Error>> {
        // Safe borrowing with try_borrow
        if let Ok(borrow) = self.data.try_borrow() {
            println!("Successfully borrowed immutably: {:?}", *borrow);
        } else {
            println!("Could not borrow immutably");
        }

        if let Ok(mut borrow) = self.data.try_borrow_mut() {
            borrow.push(8);
            println!("Successfully borrowed mutably and added element");
        } else {
            println!("Could not borrow mutably");
        }

        Ok(())
    }

    // This would panic if called while any borrows exist
    fn dangerous_operation(&self) {
        let _borrow1 = self.data.borrow();
        // This would panic:
        // let _borrow2 = self.data.borrow_mut();
    }
}

// Observer pattern with RefCell
trait Observer {
    fn notify(&self, event: &str);
}

struct Subject {
    observers: RefCell<Vec<Rc<dyn Observer>>>,
    state: RefCell<String>,
}

impl Subject {
    fn new() -> Self {
        Subject {
            observers: RefCell::new(Vec::new()),
            state: RefCell::new("initial".to_string()),
        }
    }

    fn add_observer(&self, observer: Rc<dyn Observer>) {
        self.observers.borrow_mut().push(observer);
    }

    fn remove_observer(&self, observer: &Rc<dyn Observer>) {
        self.observers.borrow_mut().retain(|obs| {
            !Rc::ptr_eq(obs, observer)
        });
    }

    fn set_state(&self, new_state: String) {
        *self.state.borrow_mut() = new_state.clone();
        self.notify_observers(&new_state);
    }

    fn get_state(&self) -> String {
        self.state.borrow().clone()
    }

    fn notify_observers(&self, event: &str) {
        let observers = self.observers.borrow();
        for observer in observers.iter() {
            observer.notify(event);
        }
    }

    fn observer_count(&self) -> usize {
        self.observers.borrow().len()
    }
}

struct ConcreteObserver {
    id: u32,
    notifications: RefCell<Vec<String>>,
}

impl ConcreteObserver {
    fn new(id: u32) -> Self {
        ConcreteObserver {
            id,
            notifications: RefCell::new(Vec::new()),
        }
    }

    fn notification_count(&self) -> usize {
        self.notifications.borrow().len()
    }

    fn get_notifications(&self) -> Vec<String> {
        self.notifications.borrow().clone()
    }
}

impl Observer for ConcreteObserver {
    fn notify(&self, event: &str) {
        self.notifications.borrow_mut().push(format!("Observer {} received: {}", self.id, event));
    }
}

fn test_refcell_patterns() {
    // Test mock database
    let db = MockDatabase::new();

    let result1 = db.execute_query("SELECT * FROM users");
    let result2 = db.execute_query("SELECT * FROM orders");

    println!("Query result: {}", result1);
    println!("Query count: {}", db.query_count());
    println!("Connection count: {}", db.connection_count());

    let history = db.get_query_history();
    println!("Query history: {:?}", history);

    // Test cache
    let cache = Cache::new(3);

    cache.insert("key1".to_string(), "value1".to_string());
    cache.insert("key2".to_string(), "value2".to_string());

    if let Some(value) = cache.get("key1") {
        println!("Cache hit: {}", value);
    }

    if let Some((count, _time)) = cache.access_stats("key1") {
        println!("Key1 access count: {}", count);
    }

    // Test borrowing example
    let borrowing = BorrowingExample::new();

    borrowing.read_operations();
    borrowing.write_operation();
    borrowing.try_borrow_operations().unwrap();

    // Test observer pattern
    let subject = Subject::new();

    let observer1 = Rc::new(ConcreteObserver::new(1));
    let observer2 = Rc::new(ConcreteObserver::new(2));

    subject.add_observer(Rc::clone(&observer1));
    subject.add_observer(Rc::clone(&observer2));

    subject.set_state("state1".to_string());
    subject.set_state("state2".to_string());

    println!("Observer 1 notifications: {}", observer1.notification_count());
    println!("Observer 2 notifications: {}", observer2.notification_count());

    // Remove an observer
    subject.remove_observer(&observer1);
    subject.set_state("state3".to_string());

    println!("After removal - Observer 1 notifications: {}", observer1.notification_count());
    println!("After removal - Observer 2 notifications: {}", observer2.notification_count());
}

// Advanced RefCell patterns
struct StateMachine {
    current_state: RefCell<String>,
    transitions: RefCell<Vec<(String, String, String)>>, // (from, to, event)
    history: RefCell<Vec<String>>,
}

impl StateMachine {
    fn new(initial_state: String) -> Self {
        StateMachine {
            current_state: RefCell::new(initial_state),
            transitions: RefCell::new(Vec::new()),
            history: RefCell::new(Vec::new()),
        }
    }

    fn add_transition(&self, from: String, to: String, event: String) {
        self.transitions.borrow_mut().push((from, to, event));
    }

    fn trigger_event(&self, event: &str) -> Result<String, String> {
        let current = self.current_state.borrow().clone();
        let transitions = self.transitions.borrow();

        for (from, to, trigger) in transitions.iter() {
            if from == &current && trigger == event {
                *self.current_state.borrow_mut() = to.clone();
                self.history.borrow_mut().push(format!("{} -> {} ({})", from, to, event));
                return Ok(to.clone());
            }
        }

        Err(format!("No transition from {} with event {}", current, event))
    }

    fn get_current_state(&self) -> String {
        self.current_state.borrow().clone()
    }

    fn get_history(&self) -> Vec<String> {
        self.history.borrow().clone()
    }
}

fn test_advanced_refcell() {
    let state_machine = StateMachine::new("idle".to_string());

    // Add transitions
    state_machine.add_transition("idle".to_string(), "running".to_string(), "start".to_string());
    state_machine.add_transition("running".to_string(), "paused".to_string(), "pause".to_string());
    state_machine.add_transition("paused".to_string(), "running".to_string(), "resume".to_string());
    state_machine.add_transition("running".to_string(), "idle".to_string(), "stop".to_string());

    // Trigger events
    println!("Initial state: {}", state_machine.get_current_state());

    match state_machine.trigger_event("start") {
        Ok(new_state) => println!("Transitioned to: {}", new_state),
        Err(e) => println!("Transition error: {}", e),
    }

    state_machine.trigger_event("pause").unwrap();
    state_machine.trigger_event("resume").unwrap();
    state_machine.trigger_event("stop").unwrap();

    println!("Final state: {}", state_machine.get_current_state());
    println!("Transition history: {:?}", state_machine.get_history());
}
""",
    )

    run_updater(rust_smart_pointers_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    refcell_calls = [
        call
        for call in calls
        if "MockDatabase" in str(call) or "Cache" in str(call) or "Subject" in str(call)
    ]
    assert len(refcell_calls) > 0, (
        "RefCell interior mutability patterns should be detected"
    )


def test_custom_smart_pointers(
    rust_smart_pointers_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test custom smart pointer implementations."""
    test_file = rust_smart_pointers_project / "custom_smart_pointers.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::ops::{Deref, DerefMut, Drop};
use std::ptr::NonNull;
use std::marker::PhantomData;

// Custom Box-like smart pointer
struct MyBox<T> {
    ptr: NonNull<T>,
    _marker: PhantomData<T>,
}

impl<T> MyBox<T> {
    fn new(value: T) -> Self {
        let boxed = Box::new(value);
        let ptr = NonNull::new(Box::into_raw(boxed)).unwrap();

        MyBox {
            ptr,
            _marker: PhantomData,
        }
    }

    fn into_inner(self) -> T {
        let ptr = self.ptr.as_ptr();
        std::mem::forget(self); // Prevent double-free
        unsafe { *Box::from_raw(ptr) }
    }
}

impl<T> Deref for MyBox<T> {
    type Target = T;

    fn deref(&self) -> &Self::Target {
        unsafe { self.ptr.as_ref() }
    }
}

impl<T> DerefMut for MyBox<T> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        unsafe { self.ptr.as_mut() }
    }
}

impl<T> Drop for MyBox<T> {
    fn drop(&mut self) {
        unsafe {
            let _ = Box::from_raw(self.ptr.as_ptr());
        }
    }
}

// Custom reference counting smart pointer
struct MyRc<T> {
    ptr: NonNull<RcInner<T>>,
    _marker: PhantomData<RcInner<T>>,
}

struct RcInner<T> {
    value: T,
    ref_count: std::cell::Cell<usize>,
}

impl<T> MyRc<T> {
    fn new(value: T) -> Self {
        let inner = RcInner {
            value,
            ref_count: std::cell::Cell::new(1),
        };

        let boxed = Box::new(inner);
        let ptr = NonNull::new(Box::into_raw(boxed)).unwrap();

        MyRc {
            ptr,
            _marker: PhantomData,
        }
    }

    fn strong_count(&self) -> usize {
        unsafe { self.ptr.as_ref().ref_count.get() }
    }

    fn try_unwrap(self) -> Result<T, Self> {
        if self.strong_count() == 1 {
            let inner = unsafe { Box::from_raw(self.ptr.as_ptr()) };
            std::mem::forget(self);
            Ok(inner.value)
        } else {
            Err(self)
        }
    }
}

impl<T> Clone for MyRc<T> {
    fn clone(&self) -> Self {
        unsafe {
            let inner = self.ptr.as_ref();
            let old_count = inner.ref_count.get();
            inner.ref_count.set(old_count + 1);
        }

        MyRc {
            ptr: self.ptr,
            _marker: PhantomData,
        }
    }
}

impl<T> Deref for MyRc<T> {
    type Target = T;

    fn deref(&self) -> &Self::Target {
        unsafe { &self.ptr.as_ref().value }
    }
}

impl<T> Drop for MyRc<T> {
    fn drop(&mut self) {
        unsafe {
            let inner = self.ptr.as_ref();
            let old_count = inner.ref_count.get();

            if old_count == 1 {
                // Last reference, deallocate
                let _ = Box::from_raw(self.ptr.as_ptr());
            } else {
                // Decrement reference count
                inner.ref_count.set(old_count - 1);
            }
        }
    }
}

// Unique pointer with move semantics
struct UniquePtr<T> {
    ptr: Option<NonNull<T>>,
    _marker: PhantomData<T>,
}

impl<T> UniquePtr<T> {
    fn new(value: T) -> Self {
        let boxed = Box::new(value);
        let ptr = NonNull::new(Box::into_raw(boxed));

        UniquePtr {
            ptr,
            _marker: PhantomData,
        }
    }

    fn null() -> Self {
        UniquePtr {
            ptr: None,
            _marker: PhantomData,
        }
    }

    fn is_null(&self) -> bool {
        self.ptr.is_none()
    }

    fn take(&mut self) -> Option<T> {
        if let Some(ptr) = self.ptr.take() {
            unsafe { Some(*Box::from_raw(ptr.as_ptr())) }
        } else {
            None
        }
    }

    fn reset(&mut self, value: Option<T>) {
        // Drop current value if any
        if let Some(ptr) = self.ptr.take() {
            unsafe { let _ = Box::from_raw(ptr.as_ptr()); }
        }

        // Set new value
        if let Some(val) = value {
            let boxed = Box::new(val);
            self.ptr = NonNull::new(Box::into_raw(boxed));
        }
    }

    fn get(&self) -> Option<&T> {
        self.ptr.map(|ptr| unsafe { ptr.as_ref() })
    }

    fn get_mut(&mut self) -> Option<&mut T> {
        self.ptr.map(|ptr| unsafe { ptr.as_mut() })
    }
}

impl<T> Drop for UniquePtr<T> {
    fn drop(&mut self) {
        if let Some(ptr) = self.ptr {
            unsafe { let _ = Box::from_raw(ptr.as_ptr()); }
        }
    }
}

// RAII guard for automatic cleanup
struct ResourceGuard<T, F>
where
    F: FnOnce(&mut T),
{
    resource: Option<T>,
    cleanup: Option<F>,
}

impl<T, F> ResourceGuard<T, F>
where
    F: FnOnce(&mut T),
{
    fn new(resource: T, cleanup: F) -> Self {
        ResourceGuard {
            resource: Some(resource),
            cleanup: Some(cleanup),
        }
    }

    fn get(&self) -> Option<&T> {
        self.resource.as_ref()
    }

    fn get_mut(&mut self) -> Option<&mut T> {
        self.resource.as_mut()
    }

    fn release(mut self) -> T {
        self.cleanup.take(); // Prevent cleanup
        self.resource.take().unwrap()
    }
}

impl<T, F> Drop for ResourceGuard<T, F>
where
    F: FnOnce(&mut T),
{
    fn drop(&mut self) {
        if let (Some(mut resource), Some(cleanup)) = (self.resource.take(), self.cleanup.take()) {
            cleanup(&mut resource);
        }
    }
}

// Lazy initialization smart pointer
struct Lazy<T, F>
where
    F: FnOnce() -> T,
{
    value: std::cell::RefCell<Option<T>>,
    init: std::cell::RefCell<Option<F>>,
}

impl<T, F> Lazy<T, F>
where
    F: FnOnce() -> T,
{
    fn new(init: F) -> Self {
        Lazy {
            value: std::cell::RefCell::new(None),
            init: std::cell::RefCell::new(Some(init)),
        }
    }

    fn get(&self) -> std::cell::Ref<T> {
        // Initialize if not already done
        if self.value.borrow().is_none() {
            if let Some(init) = self.init.borrow_mut().take() {
                *self.value.borrow_mut() = Some(init());
            }
        }

        std::cell::Ref::map(self.value.borrow(), |opt| opt.as_ref().unwrap())
    }

    fn is_initialized(&self) -> bool {
        self.value.borrow().is_some()
    }
}

fn test_custom_smart_pointers() {
    // Test MyBox
    {
        let my_box = MyBox::new(42);
        println!("MyBox value: {}", *my_box);

        let value = my_box.into_inner();
        println!("Extracted value: {}", value);
    }

    // Test MyRc
    {
        let rc1 = MyRc::new("shared data".to_string());
        println!("Initial ref count: {}", rc1.strong_count());

        let rc2 = rc1.clone();
        println!("After clone ref count: {}", rc1.strong_count());

        println!("RC1 value: {}", *rc1);
        println!("RC2 value: {}", *rc2);

        drop(rc2);
        println!("After drop ref count: {}", rc1.strong_count());

        match rc1.try_unwrap() {
            Ok(value) => println!("Unwrapped value: {}", value),
            Err(_) => println!("Could not unwrap"),
        }
    }

    // Test UniquePtr
    {
        let mut unique = UniquePtr::new(100);
        println!("Unique ptr is null: {}", unique.is_null());

        if let Some(value) = unique.get() {
            println!("Unique ptr value: {}", value);
        }

        if let Some(value) = unique.take() {
            println!("Taken value: {}", value);
        }

        println!("After take, is null: {}", unique.is_null());

        unique.reset(Some(200));
        if let Some(value) = unique.get() {
            println!("Reset value: {}", value);
        }
    }

    // Test ResourceGuard
    {
        let resource = vec![1, 2, 3, 4, 5];
        let guard = ResourceGuard::new(resource, |vec| {
            println!("Cleaning up vector with {} elements", vec.len());
            vec.clear();
        });

        if let Some(vec) = guard.get() {
            println!("Guarded resource: {:?}", vec);
        }

        // guard will be dropped here and cleanup will run
    }

    // Test Lazy
    {
        let lazy = Lazy::new(|| {
            println!("Initializing lazy value");
            42 * 2
        });

        println!("Lazy is initialized: {}", lazy.is_initialized());

        let value = lazy.get();
        println!("Lazy value: {}", *value);

        println!("Lazy is initialized: {}", lazy.is_initialized());

        // Second access should not reinitialize
        let value2 = lazy.get();
        println!("Lazy value again: {}", *value2);
    }
}

// Advanced custom smart pointer with thread safety
use std::sync::{Arc, Mutex};

struct ThreadSafePtr<T> {
    inner: Arc<Mutex<Option<T>>>,
}

impl<T> ThreadSafePtr<T> {
    fn new(value: T) -> Self {
        ThreadSafePtr {
            inner: Arc::new(Mutex::new(Some(value))),
        }
    }

    fn empty() -> Self {
        ThreadSafePtr {
            inner: Arc::new(Mutex::new(None)),
        }
    }

    fn clone_handle(&self) -> Self {
        ThreadSafePtr {
            inner: Arc::clone(&self.inner),
        }
    }

    fn with<F, R>(&self, f: F) -> Option<R>
    where
        F: FnOnce(&T) -> R,
    {
        let guard = self.inner.lock().ok()?;
        guard.as_ref().map(f)
    }

    fn with_mut<F, R>(&self, f: F) -> Option<R>
    where
        F: FnOnce(&mut T) -> R,
    {
        let mut guard = self.inner.lock().ok()?;
        guard.as_mut().map(f)
    }

    fn take(&self) -> Option<T> {
        self.inner.lock().ok()?.take()
    }

    fn replace(&self, value: T) -> Option<T> {
        self.inner.lock().ok()?.replace(value)
    }

    fn is_empty(&self) -> bool {
        self.inner.lock().map_or(true, |guard| guard.is_none())
    }
}

fn test_thread_safe_ptr() {
    use std::thread;
    use std::time::Duration;

    let ptr = ThreadSafePtr::new(vec![1, 2, 3]);
    let ptr_clone = ptr.clone_handle();

    let handle = thread::spawn(move || {
        ptr_clone.with_mut(|vec| {
            vec.push(4);
            vec.push(5);
        });

        ptr_clone.with(|vec| {
            println!("Thread sees vector: {:?}", vec);
        });
    });

    thread::sleep(Duration::from_millis(10));

    ptr.with(|vec| {
        println!("Main thread sees vector: {:?}", vec);
    });

    handle.join().unwrap();

    if let Some(final_vec) = ptr.take() {
        println!("Final vector: {:?}", final_vec);
    }

    println!("Pointer is empty: {}", ptr.is_empty());
}
""",
    )

    run_updater(rust_smart_pointers_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    custom_calls = [
        call
        for call in calls
        if "MyBox" in str(call) or "MyRc" in str(call) or "UniquePtr" in str(call)
    ]
    assert len(custom_calls) > 0, "Custom smart pointer patterns should be detected"
