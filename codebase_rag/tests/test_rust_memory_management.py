"""
Comprehensive Rust memory management and ownership testing.
Tests borrowing, ownership, lifetimes, and memory safety features.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def rust_memory_project(temp_repo: Path) -> Path:
    """Create a Rust project with memory management examples."""
    project_path = temp_repo / "rust_memory_test"
    project_path.mkdir()

    # Create Cargo.toml
    (project_path / "Cargo.toml").write_text("""
[package]
name = "rust_memory_test"
version = "0.1.0"
edition = "2021"
""")

    # Create src directory
    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text("// Memory management test crate")

    return project_path


def test_ownership_borrowing_basic(
    rust_memory_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic ownership and borrowing patterns."""
    test_file = rust_memory_project / "ownership_basic.rs"
    test_file.write_text(
        """
fn take_ownership(s: String) {
    println!("{}", s);
}

fn borrow_immutable(s: &String) {
    println!("{}", s);
}

fn borrow_mutable(s: &mut String) {
    s.push_str(" world");
}

fn return_ownership() -> String {
    String::from("hello")
}

fn main() {
    let s1 = String::from("hello");
    take_ownership(s1);

    let s2 = String::from("world");
    borrow_immutable(&s2);

    let mut s3 = String::from("hello");
    borrow_mutable(&mut s3);

    let s4 = return_ownership();
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=rust_memory_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()
    calls = mock_ingestor.method_calls

    # Verify functions are detected
    function_calls = [
        call
        for call in calls
        if "take_ownership" in str(call)
        or "borrow_immutable" in str(call)
        or "borrow_mutable" in str(call)
    ]
    assert len(function_calls) > 0, "Memory management functions should be detected"


def test_lifetimes_explicit(
    rust_memory_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test explicit lifetime annotations."""
    test_file = rust_memory_project / "lifetimes.rs"
    test_file.write_text(
        """
fn longest<'a>(x: &'a str, y: &'a str) -> &'a str {
    if x.len() > y.len() {
        x
    } else {
        y
    }
}

fn first_word<'a>(s: &'a str) -> &'a str {
    let bytes = s.as_bytes();

    for (i, &item) in bytes.iter().enumerate() {
        if item == b' ' {
            return &s[0..i];
        }
    }

    &s[..]
}

struct ImportantExcerpt<'a> {
    part: &'a str,
}

impl<'a> ImportantExcerpt<'a> {
    fn level(&self) -> i32 {
        3
    }

    fn announce_and_return_part(&self, announcement: &str) -> &str {
        println!("Attention please: {}", announcement);
        self.part
    }
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=rust_memory_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()
    calls = mock_ingestor.method_calls

    # Verify lifetime functions and structs are detected
    lifetime_calls = [
        call
        for call in calls
        if "longest" in str(call) or "ImportantExcerpt" in str(call)
    ]
    assert len(lifetime_calls) > 0, (
        "Lifetime-annotated functions and structs should be detected"
    )


def test_smart_pointers(
    rust_memory_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test smart pointer usage (Box, Rc, RefCell, etc.)."""
    test_file = rust_memory_project / "smart_pointers.rs"
    test_file.write_text(
        """
use std::rc::Rc;
use std::cell::RefCell;

struct Node {
    value: i32,
    children: RefCell<Vec<Rc<Node>>>,
}

fn create_tree() -> Rc<Node> {
    let leaf = Rc::new(Node {
        value: 3,
        children: RefCell::new(vec![]),
    });

    let branch = Rc::new(Node {
        value: 5,
        children: RefCell::new(vec![Rc::clone(&leaf)]),
    });

    branch
}

fn box_example() {
    let b = Box::new(5);
    println!("b = {}", b);
}

enum List {
    Cons(i32, Box<List>),
    Nil,
}

fn create_list() -> List {
    use List::{Cons, Nil};
    Cons(1, Box::new(Cons(2, Box::new(Cons(3, Box::new(Nil))))))
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=rust_memory_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()
    calls = mock_ingestor.method_calls

    # Verify smart pointer functions are detected
    smart_pointer_calls = [
        call
        for call in calls
        if "create_tree" in str(call) or "box_example" in str(call)
    ]
    assert len(smart_pointer_calls) > 0, "Smart pointer functions should be detected"


def test_reference_counting(
    rust_memory_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test reference counting patterns."""
    test_file = rust_memory_project / "reference_counting.rs"
    test_file.write_text(
        """
use std::rc::{Rc, Weak};
use std::cell::RefCell;

#[derive(Debug)]
struct Node {
    value: i32,
    parent: RefCell<Weak<Node>>,
    children: RefCell<Vec<Rc<Node>>>,
}

impl Node {
    fn new(value: i32) -> Rc<Self> {
        Rc::new(Node {
            value,
            parent: RefCell::new(Weak::new()),
            children: RefCell::new(vec![]),
        })
    }

    fn add_child(self: &Rc<Self>, child: Rc<Node>) {
        *child.parent.borrow_mut() = Rc::downgrade(self);
        self.children.borrow_mut().push(child);
    }
}

fn create_tree_with_cycles() {
    let leaf = Node::new(3);
    let branch = Node::new(5);

    branch.add_child(leaf.clone());

    println!(
        "leaf parent = {:?}",
        leaf.parent.borrow().upgrade()
    );
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=rust_memory_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()
    calls = mock_ingestor.method_calls

    # Verify reference counting functions are detected
    rc_calls = [
        call
        for call in calls
        if "create_tree_with_cycles" in str(call) or "add_child" in str(call)
    ]
    assert len(rc_calls) > 0, "Reference counting functions should be detected"


def test_drop_trait_cleanup(
    rust_memory_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Drop trait implementation for cleanup."""
    test_file = rust_memory_project / "drop_cleanup.rs"
    test_file.write_text(
        """
struct CustomSmartPointer {
    data: String,
}

impl Drop for CustomSmartPointer {
    fn drop(&mut self) {
        println!("Dropping CustomSmartPointer with data `{}`!", self.data);
    }
}

fn create_and_drop() {
    let c = CustomSmartPointer {
        data: String::from("my stuff"),
    };
    let d = CustomSmartPointer {
        data: String::from("other stuff"),
    };
    println!("CustomSmartPointers created.");
}

fn early_drop() {
    let c = CustomSmartPointer {
        data: String::from("some data"),
    };
    println!("CustomSmartPointer created.");
    drop(c);
    println!("CustomSmartPointer dropped before the end of main.");
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=rust_memory_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()
    calls = mock_ingestor.method_calls

    # Verify Drop trait implementation is detected
    drop_calls = [
        call
        for call in calls
        if "CustomSmartPointer" in str(call) or "drop" in str(call)
    ]
    assert len(drop_calls) > 0, "Drop trait implementation should be detected"


def test_unsafe_code_patterns(
    rust_memory_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test unsafe code patterns and raw pointers."""
    test_file = rust_memory_project / "unsafe_patterns.rs"
    test_file.write_text(
        """
fn raw_pointers() {
    let mut num = 5;

    let r1 = &num as *const i32;
    let r2 = &mut num as *mut i32;

    unsafe {
        println!("r1 is: {}", *r1);
        println!("r2 is: {}", *r2);
    }
}

unsafe fn dangerous() {
    println!("This is a dangerous function");
}

fn call_unsafe_function() {
    unsafe {
        dangerous();
    }
}

extern "C" {
    fn abs(input: i32) -> i32;
}

fn call_external_function() {
    unsafe {
        println!("Absolute value of -3 according to C: {}", abs(-3));
    }
}

static mut COUNTER: usize = 0;

fn add_to_count(inc: usize) {
    unsafe {
        COUNTER += inc;
    }
}

unsafe trait Foo {
    fn dangerous_method(&self);
}

struct Bar;

unsafe impl Foo for Bar {
    fn dangerous_method(&self) {
        println!("Implementing dangerous method");
    }
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=rust_memory_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()
    calls = mock_ingestor.method_calls

    # Verify unsafe functions and traits are detected
    unsafe_calls = [
        call
        for call in calls
        if "dangerous" in str(call) or "raw_pointers" in str(call) or "Foo" in str(call)
    ]
    assert len(unsafe_calls) > 0, "Unsafe functions and traits should be detected"


def test_memory_layout_optimization(
    rust_memory_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test memory layout and optimization patterns."""
    test_file = rust_memory_project / "memory_layout.rs"
    test_file.write_text(
        """
use std::mem;

#[repr(C)]
struct CStyleStruct {
    x: i32,
    y: i64,
}

#[repr(packed)]
struct PackedStruct {
    x: i8,
    y: i64,
}

#[derive(Clone, Copy)]
struct Point {
    x: f64,
    y: f64,
}

fn memory_info() {
    println!("CStyleStruct size: {}", mem::size_of::<CStyleStruct>());
    println!("PackedStruct size: {}", mem::size_of::<PackedStruct>());
    println!("Point size: {}", mem::size_of::<Point>());
}

fn stack_vs_heap() {
    // Stack allocated
    let point = Point { x: 1.0, y: 2.0 };

    // Heap allocated
    let boxed_point = Box::new(Point { x: 3.0, y: 4.0 });

    println!("Stack point: {:?}", mem::size_of_val(&point));
    println!("Heap point: {:?}", mem::size_of_val(&*boxed_point));
}

union MyUnion {
    f: f32,
    u: u32,
}

fn union_example() {
    let mut u = MyUnion { f: 1.0 };

    unsafe {
        u.u = 0x3F800000;
        println!("Union as float: {}", u.f);
    }
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=rust_memory_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()
    calls = mock_ingestor.method_calls

    # Verify memory layout functions are detected
    memory_calls = [
        call
        for call in calls
        if "memory_info" in str(call) or "union_example" in str(call)
    ]
    assert len(memory_calls) > 0, "Memory layout functions should be detected"
