from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_advanced_types_project(temp_repo: Path) -> Path:
    """Create a Rust project structure for advanced type system testing."""
    project_path = temp_repo / "rust_advanced_types_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Advanced types test crate"
    )

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""[package]
name = "rust_advanced_types_test"
version = "0.1.0"
edition = "2021"
""",
    )

    return project_path


def test_phantom_types_and_markers(
    rust_advanced_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test phantom types and type markers for compile-time guarantees."""
    test_file = rust_advanced_types_project / "phantom_types.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::marker::PhantomData;

// Phantom type for units
struct Meters<T>(f64, PhantomData<T>);
struct Kilometers<T>(f64, PhantomData<T>);
struct Feet<T>(f64, PhantomData<T>);

// Type state pattern
struct Open;
struct Closed;
struct Database<State> {
    connection: String,
    state: PhantomData<State>,
}

impl Database<Closed> {
    fn new(connection: String) -> Database<Closed> {
        Database {
            connection,
            state: PhantomData,
        }
    }

    fn open(self) -> Database<Open> {
        Database {
            connection: self.connection,
            state: PhantomData,
        }
    }
}

impl Database<Open> {
    fn query(&self, sql: &str) -> Vec<String> {
        vec![]
    }

    fn close(self) -> Database<Closed> {
        Database {
            connection: self.connection,
            state: PhantomData,
        }
    }
}

// Phantom type for ownership tracking
struct Owned;
struct Borrowed;
struct Buffer<Ownership> {
    data: Vec<u8>,
    ownership: PhantomData<Ownership>,
}

impl Buffer<Owned> {
    fn new(data: Vec<u8>) -> Self {
        Buffer {
            data,
            ownership: PhantomData,
        }
    }

    fn borrow(&self) -> Buffer<Borrowed> {
        Buffer {
            data: self.data.clone(),
            ownership: PhantomData,
        }
    }
}

impl<T> Buffer<T> {
    fn len(&self) -> usize {
        self.data.len()
    }
}
""",
    )

    run_updater(rust_advanced_types_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    struct_calls = [
        call
        for call in calls
        if "Database" in str(call) or "Buffer" in str(call) or "Meters" in str(call)
    ]
    assert len(struct_calls) > 0, "Phantom type structs should be detected"


def test_higher_ranked_trait_bounds(
    rust_advanced_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test higher-ranked trait bounds (HRTB) and for<> syntax."""
    test_file = rust_advanced_types_project / "hrtb.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Higher-ranked trait bounds
fn call_with_ref<F>(f: F) -> String
where
    F: for<'a> Fn(&'a str) -> String,
{
    f("hello")
}

fn closure_example() {
    let closure = |s: &str| format!("Processed: {}", s);
    let result = call_with_ref(closure);
}

// HRTB with multiple lifetimes
trait Transform<Input, Output> {
    fn transform(&self, input: Input) -> Output;
}

fn process_transform<T, F>(transformer: T, func: F) -> String
where
    T: Transform<String, String>,
    F: for<'a, 'b> Fn(&'a str, &'b str) -> String,
{
    let result = transformer.transform("input".to_string());
    func(&result, "suffix")
}

// Complex HRTB with associated types
trait Processor {
    type Input;
    type Output;

    fn process(&self, input: Self::Input) -> Self::Output;
}

fn apply_processor<P, F>(processor: P, callback: F)
where
    P: Processor<Input = String, Output = String>,
    F: for<'a> Fn(&'a P::Output) -> usize,
{
    let result = processor.process("test".to_string());
    callback(&result);
}

// HRTB with trait objects
type ProcessorFn = Box<dyn for<'a> Fn(&'a str) -> String>;

fn create_processor() -> ProcessorFn {
    Box::new(|s| s.to_uppercase())
}

// Nested HRTB
fn nested_hrtb<F>() -> impl Fn(&str) -> String
where
    F: for<'a> Fn(&'a str) -> &'a str,
{
    |s| s.to_string()
}
""",
    )

    run_updater(rust_advanced_types_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    function_calls = [
        call
        for call in calls
        if "call_with_ref" in str(call) or "process_transform" in str(call)
    ]
    assert len(function_calls) > 0, "HRTB functions should be detected"


def test_advanced_associated_types(
    rust_advanced_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test complex associated type patterns and projections."""
    test_file = rust_advanced_types_project / "associated_types.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Generic associated types (GATs)
trait StreamingIterator {
    type Item<'a> where Self: 'a;

    fn next<'a>(&'a mut self) -> Option<Self::Item<'a>>;
}

struct WindowIterator<I> {
    iter: I,
    window_size: usize,
}

impl<I> StreamingIterator for WindowIterator<I>
where
    I: Iterator,
{
    type Item<'a> = Vec<&'a I::Item> where Self: 'a, I: 'a;

    fn next<'a>(&'a mut self) -> Option<Self::Item<'a>> {
        None // Simplified implementation
    }
}

// Associated type projections
trait Collect<T> {
    type Output;
    fn collect(self) -> Self::Output;
}

impl<I, T> Collect<T> for I
where
    I: Iterator<Item = T>,
{
    type Output = Vec<T>;

    fn collect(self) -> Self::Output {
        self.collect()
    }
}

// Complex associated type bounds
fn process_collection<C, T>(collection: C) -> Vec<T>
where
    C: Collect<T, Output = Vec<T>>,
{
    collection.collect()
}

// Associated types with where clauses
trait Repository {
    type Entity;
    type Error;
    type Connection;

    fn find_by_id(
        &self,
        id: u64,
        conn: &Self::Connection,
    ) -> Result<Option<Self::Entity>, Self::Error>
    where
        Self::Entity: Clone,
        Self::Error: std::fmt::Display;
}

// Associated consts and types together
trait DatabaseDriver {
    type Connection;
    type Statement;
    type Row;

    const MAX_CONNECTIONS: usize;
    const DRIVER_NAME: &'static str;

    fn connect(&self) -> Self::Connection;
    fn prepare(&self, sql: &str) -> Self::Statement;
    fn execute(&self, stmt: Self::Statement) -> Vec<Self::Row>;
}

struct PostgresDriver;

impl DatabaseDriver for PostgresDriver {
    type Connection = String;
    type Statement = String;
    type Row = Vec<String>;

    const MAX_CONNECTIONS: usize = 100;
    const DRIVER_NAME: &'static str = "postgres";

    fn connect(&self) -> Self::Connection {
        "connection".to_string()
    }

    fn prepare(&self, sql: &str) -> Self::Statement {
        sql.to_string()
    }

    fn execute(&self, stmt: Self::Statement) -> Vec<Self::Row> {
        vec![]
    }
}
""",
    )

    run_updater(rust_advanced_types_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    trait_calls = [
        call
        for call in calls
        if "StreamingIterator" in str(call) or "Repository" in str(call)
    ]
    assert len(trait_calls) > 0, "Associated type traits should be detected"


def test_type_level_programming(
    rust_advanced_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test type-level programming and compile-time computation."""
    test_file = rust_advanced_types_project / "type_level.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::marker::PhantomData;

// Type-level numbers
struct Zero;
struct Succ<N>(PhantomData<N>);

type One = Succ<Zero>;
type Two = Succ<One>;
type Three = Succ<Two>;

// Type-level addition
trait Add<Rhs> {
    type Output;
}

impl<N> Add<Zero> for N {
    type Output = N;
}

impl<N, M> Add<Succ<M>> for N
where
    N: Add<M>,
    Succ<N::Output>: Sized,
{
    type Output = Succ<<N as Add<M>>::Output>;
}

// Type-level lists
struct Nil;
struct Cons<H, T>(PhantomData<H>, PhantomData<T>);

type List1 = Cons<i32, Nil>;
type List2 = Cons<String, Cons<i32, Nil>>;

// Type-level length calculation
trait Length {
    type Output;
}

impl Length for Nil {
    type Output = Zero;
}

impl<H, T> Length for Cons<H, T>
where
    T: Length,
    Succ<T::Output>: Sized,
{
    type Output = Succ<T::Output>;
}

// Type-level boolean logic
struct True;
struct False;

trait And<Rhs> {
    type Output;
}

impl And<True> for True {
    type Output = True;
}

impl And<False> for True {
    type Output = False;
}

impl<Rhs> And<Rhs> for False {
    type Output = False;
}

// Type-level equality
trait TypeEq<Rhs> {
    type Output;
}

struct IsEqual;
struct NotEqual;

impl<T> TypeEq<T> for T {
    type Output = IsEqual;
}

// Compile-time array bounds checking
struct Array<T, N> {
    data: Vec<T>,
    size: PhantomData<N>,
}

impl<T, N> Array<T, N>
where
    N: TypeEq<Three, Output = IsEqual>,
{
    fn new_size_three() -> Self {
        Array {
            data: Vec::with_capacity(3),
            size: PhantomData,
        }
    }
}

// Type-level programming with const generics
struct Matrix<T, const ROWS: usize, const COLS: usize> {
    data: [[T; COLS]; ROWS],
}

impl<T, const N: usize> Matrix<T, N, N>
where
    T: Default + Copy,
{
    fn identity() -> Self {
        let mut data = [[T::default(); N]; N];
        for i in 0..N {
            data[i][i] = T::default(); // Would set to 1 for numeric types
        }
        Matrix { data }
    }
}

const fn factorial(n: usize) -> usize {
    match n {
        0 | 1 => 1,
        _ => n * factorial(n - 1),
    }
}

type FactorialArray<const N: usize> = [u8; factorial(N)];

fn create_factorial_array<const N: usize>() -> FactorialArray<N> {
    [0; factorial(N)]
}
""",
    )

    run_updater(rust_advanced_types_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    type_calls = [
        call
        for call in calls
        if "Zero" in str(call) or "Matrix" in str(call) or "Array" in str(call)
    ]
    assert len(type_calls) > 0, "Type-level programming constructs should be detected"


def test_const_generics_advanced(
    rust_advanced_types_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test advanced const generics patterns and compile-time evaluation."""
    test_file = rust_advanced_types_project / "const_generics.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Advanced const generics with bounds
trait ArrayOps<T, const N: usize> {
    fn sum(&self) -> T where T: std::ops::Add<Output = T> + Copy + Default;
    fn max(&self) -> T where T: PartialOrd + Copy;
}

impl<T, const N: usize> ArrayOps<T, N> for [T; N] {
    fn sum(&self) -> T
    where
        T: std::ops::Add<Output = T> + Copy + Default,
    {
        let mut result = T::default();
        for item in self.iter() {
            result = result + *item;
        }
        result
    }

    fn max(&self) -> T
    where
        T: PartialOrd + Copy,
    {
        let mut max = self[0];
        for item in self.iter().skip(1) {
            if *item > max {
                max = *item;
            }
        }
        max
    }
}

// Const generic expressions
struct Buffer<T, const SIZE: usize> {
    data: [T; SIZE],
    len: usize,
}

impl<T, const SIZE: usize> Buffer<T, SIZE>
where
    T: Default + Copy,
{
    fn new() -> Self {
        Buffer {
            data: [T::default(); SIZE],
            len: 0,
        }
    }

    fn push(&mut self, item: T) -> Result<(), &'static str> {
        if self.len >= SIZE {
            Err("Buffer full")
        } else {
            self.data[self.len] = item;
            self.len += 1;
            Ok(())
        }
    }

    fn capacity(&self) -> usize {
        SIZE
    }
}

// Const generic arithmetic
struct Grid<T, const WIDTH: usize, const HEIGHT: usize> {
    cells: [[T; WIDTH]; HEIGHT],
}

impl<T, const W: usize, const H: usize> Grid<T, W, H>
where
    T: Default + Copy,
{
    fn new() -> Self {
        Grid {
            cells: [[T::default(); W]; H],
        }
    }

    fn get(&self, x: usize, y: usize) -> Option<&T> {
        if x < W && y < H {
            Some(&self.cells[y][x])
        } else {
            None
        }
    }

    fn set(&mut self, x: usize, y: usize, value: T) -> Result<(), &'static str> {
        if x < W && y < H {
            self.cells[y][x] = value;
            Ok(())
        } else {
            Err("Index out of bounds")
        }
    }

    const fn area() -> usize {
        W * H
    }

    const fn perimeter() -> usize {
        2 * (W + H)
    }
}

// Generic const expressions with where clauses
struct FixedString<const N: usize>
where
    [(); N + 1]:,
{
    data: [u8; N],
    null_term: [u8; 1],
}

impl<const N: usize> FixedString<N>
where
    [(); N + 1]:,
{
    fn new() -> Self {
        FixedString {
            data: [0; N],
            null_term: [0; 1],
        }
    }

    fn from_str(s: &str) -> Result<Self, &'static str> {
        if s.len() > N {
            Err("String too long")
        } else {
            let mut fixed = Self::new();
            let bytes = s.as_bytes();
            fixed.data[..bytes.len()].copy_from_slice(bytes);
            Ok(fixed)
        }
    }
}

// Const generics with complex expressions
const fn fibonacci(n: usize) -> usize {
    match n {
        0 => 0,
        1 => 1,
        _ => fibonacci(n - 1) + fibonacci(n - 2),
    }
}

struct FibArray<const N: usize> {
    data: [usize; fibonacci(N)],
}

impl<const N: usize> FibArray<N> {
    fn new() -> Self {
        FibArray {
            data: [0; fibonacci(N)],
        }
    }

    const fn size() -> usize {
        fibonacci(N)
    }
}

// Const generics with type bounds
trait ConstArrayExt<T, const N: usize> {
    fn chunk<const CHUNK_SIZE: usize>(&self) -> impl Iterator<Item = &[T]>
    where
        [(); N / CHUNK_SIZE]:;
}

impl<T, const N: usize> ConstArrayExt<T, N> for [T; N] {
    fn chunk<const CHUNK_SIZE: usize>(&self) -> impl Iterator<Item = &[T]>
    where
        [(); N / CHUNK_SIZE]:,
    {
        self.chunks(CHUNK_SIZE)
    }
}
""",
    )

    run_updater(rust_advanced_types_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    const_calls = [
        call
        for call in calls
        if "Buffer" in str(call) or "Grid" in str(call) or "FixedString" in str(call)
    ]
    assert len(const_calls) > 0, "Const generic constructs should be detected"
