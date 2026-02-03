from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_performance_project(temp_repo: Path) -> Path:
    """Create a Rust project with performance examples."""
    project_path = temp_repo / "rust_performance_test"
    project_path.mkdir()

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""
[package]
name = "rust_performance_test"
version = "0.1.0"
edition = "2021"

[dependencies]
criterion = "0.5"
rayon = "1.7"
""",
    )

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Performance test crate"
    )

    return project_path


def test_benchmarking_patterns(
    rust_performance_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test benchmarking and measurement patterns."""
    test_file = rust_performance_project / "benchmarking.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::time::{Duration, Instant};

fn time_function<F, R>(f: F) -> (R, Duration)
where
    F: FnOnce() -> R,
{
    let start = Instant::now();
    let result = f();
    let duration = start.elapsed();
    (result, duration)
}

fn benchmark_sorting_algorithms() {
    let mut data1 = vec![5, 2, 8, 1, 9, 3, 7, 4, 6];
    let mut data2 = data1.clone();
    let mut data3 = data1.clone();

    // Benchmark bubble sort
    let (_, bubble_time) = time_function(|| {
        bubble_sort(&mut data1);
    });

    // Benchmark quick sort
    let (_, quick_time) = time_function(|| {
        quick_sort(&mut data2);
    });

    // Benchmark standard library sort
    let (_, std_time) = time_function(|| {
        data3.sort();
    });

    println!("Bubble sort: {:?}", bubble_time);
    println!("Quick sort: {:?}", quick_time);
    println!("Std sort: {:?}", std_time);
}

fn bubble_sort(arr: &mut [i32]) {
    let len = arr.len();
    for i in 0..len {
        for j in 0..len - 1 - i {
            if arr[j] > arr[j + 1] {
                arr.swap(j, j + 1);
            }
        }
    }
}

fn quick_sort(arr: &mut [i32]) {
    if arr.len() <= 1 {
        return;
    }

    let pivot_index = partition(arr);
    quick_sort(&mut arr[0..pivot_index]);
    quick_sort(&mut arr[pivot_index + 1..]);
}

fn partition(arr: &mut [i32]) -> usize {
    let len = arr.len();
    let pivot = arr[len - 1];
    let mut i = 0;

    for j in 0..len - 1 {
        if arr[j] <= pivot {
            arr.swap(i, j);
            i += 1;
        }
    }

    arr.swap(i, len - 1);
    i
}

fn benchmark_allocation_strategies() {
    let size = 10000;

    // Benchmark Vec with capacity
    let (_, with_capacity_time) = time_function(|| {
        let mut vec = Vec::with_capacity(size);
        for i in 0..size {
            vec.push(i);
        }
        vec
    });

    // Benchmark Vec without capacity
    let (_, without_capacity_time) = time_function(|| {
        let mut vec = Vec::new();
        for i in 0..size {
            vec.push(i);
        }
        vec
    });

    // Benchmark array initialization
    let (_, array_time) = time_function(|| {
        let arr = [0; 10000];
        arr
    });

    println!("With capacity: {:?}", with_capacity_time);
    println!("Without capacity: {:?}", without_capacity_time);
    println!("Array initialization: {:?}", array_time);
}

fn benchmark_string_operations() {
    let strings = vec!["hello", "world", "rust", "performance", "optimization"];

    // Benchmark String concatenation
    let (_, string_concat_time) = time_function(|| {
        let mut result = String::new();
        for s in &strings {
            result.push_str(s);
            result.push(' ');
        }
        result
    });

    // Benchmark String with capacity
    let (_, string_capacity_time) = time_function(|| {
        let total_len: usize = strings.iter().map(|s| s.len() + 1).sum();
        let mut result = String::with_capacity(total_len);
        for s in &strings {
            result.push_str(s);
            result.push(' ');
        }
        result
    });

    // Benchmark join
    let (_, join_time) = time_function(|| {
        strings.join(" ")
    });

    println!("String concatenation: {:?}", string_concat_time);
    println!("String with capacity: {:?}", string_capacity_time);
    println!("Join: {:?}", join_time);
}

fn benchmark_iterator_vs_loop() {
    let data: Vec<i32> = (0..100000).collect();

    // Traditional for loop
    let (sum1, loop_time) = time_function(|| {
        let mut sum = 0;
        for i in 0..data.len() {
            sum += data[i];
        }
        sum
    });

    // Iterator approach
    let (sum2, iter_time) = time_function(|| {
        data.iter().sum::<i32>()
    });

    // Manual loop with iterator
    let (sum3, manual_iter_time) = time_function(|| {
        let mut sum = 0;
        for &item in &data {
            sum += item;
        }
        sum
    });

    println!("For loop: {:?} (sum: {})", loop_time, sum1);
    println!("Iterator: {:?} (sum: {})", iter_time, sum2);
    println!("Manual iter: {:?} (sum: {})", manual_iter_time, sum3);
}

struct BenchmarkSuite {
    name: String,
    tests: Vec<Box<dyn Fn() -> Duration>>,
}

impl BenchmarkSuite {
    fn new(name: &str) -> Self {
        BenchmarkSuite {
            name: name.to_string(),
            tests: Vec::new(),
        }
    }

    fn add_test<F>(&mut self, test: F)
    where
        F: Fn() -> Duration + 'static,
    {
        self.tests.push(Box::new(test));
    }

    fn run(&self) {
        println!("Running benchmark suite: {}", self.name);

        for (i, test) in self.tests.iter().enumerate() {
            let duration = test();
            println!("  Test {}: {:?}", i, duration);
        }
    }
}

fn custom_benchmark_framework() {
    let mut suite = BenchmarkSuite::new("Math Operations");

    suite.add_test(|| {
        let (_, duration) = time_function(|| {
            let mut sum = 0;
            for i in 0..10000 {
                sum += i * i;
            }
            sum
        });
        duration
    });

    suite.add_test(|| {
        let (_, duration) = time_function(|| {
            (0..10000).map(|i| i * i).sum::<i32>()
        });
        duration
    });

    suite.run();
}

fn memory_usage_tracking() {
    struct MemoryTracker {
        allocations: std::collections::HashMap<*mut u8, usize>,
        total_allocated: usize,
    }

    impl MemoryTracker {
        fn new() -> Self {
            MemoryTracker {
                allocations: std::collections::HashMap::new(),
                total_allocated: 0,
            }
        }

        fn track_allocation(&mut self, ptr: *mut u8, size: usize) {
            self.allocations.insert(ptr, size);
            self.total_allocated += size;
        }

        fn track_deallocation(&mut self, ptr: *mut u8) {
            if let Some(size) = self.allocations.remove(&ptr) {
                self.total_allocated -= size;
            }
        }

        fn current_usage(&self) -> usize {
            self.total_allocated
        }
    }

    let mut tracker = MemoryTracker::new();

    // Simulate allocations
    let data = vec![1, 2, 3, 4, 5];
    let ptr = data.as_ptr() as *mut u8;
    tracker.track_allocation(ptr, data.len() * std::mem::size_of::<i32>());

    println!("Memory usage: {} bytes", tracker.current_usage());
}
""",
    )

    run_updater(rust_performance_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    benchmark_calls = [
        call
        for call in calls
        if "benchmark_sorting_algorithms" in str(call) or "time_function" in str(call)
    ]
    assert len(benchmark_calls) > 0, "Benchmarking functions should be detected"


def test_simd_vectorization(
    rust_performance_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test SIMD and vectorization patterns."""
    test_file = rust_performance_project / "simd_vectorization.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::arch::x86_64::*;

fn scalar_add(a: &[f32], b: &[f32], result: &mut [f32]) {
    assert_eq!(a.len(), b.len());
    assert_eq!(a.len(), result.len());

    for i in 0..a.len() {
        result[i] = a[i] + b[i];
    }
}

#[target_feature(enable = "avx2")]
unsafe fn simd_add_avx2(a: &[f32], b: &[f32], result: &mut [f32]) {
    assert_eq!(a.len(), b.len());
    assert_eq!(a.len(), result.len());
    assert_eq!(a.len() % 8, 0); // AVX2 processes 8 f32 at once

    let chunks = a.len() / 8;

    for i in 0..chunks {
        let offset = i * 8;

        let va = _mm256_loadu_ps(a.as_ptr().add(offset));
        let vb = _mm256_loadu_ps(b.as_ptr().add(offset));
        let vresult = _mm256_add_ps(va, vb);

        _mm256_storeu_ps(result.as_mut_ptr().add(offset), vresult);
    }
}

fn auto_vectorized_operations() {
    let a: Vec<f32> = (0..1000).map(|x| x as f32).collect();
    let b: Vec<f32> = (0..1000).map(|x| (x * 2) as f32).collect();
    let mut result = vec![0.0; 1000];

    // Compiler can auto-vectorize this
    for i in 0..1000 {
        result[i] = a[i] + b[i] * 2.0;
    }

    println!("Auto-vectorized result: {:?}", &result[0..5]);
}

fn simd_dot_product(a: &[f32], b: &[f32]) -> f32 {
    assert_eq!(a.len(), b.len());

    #[cfg(target_arch = "x86_64")]
    {
        if is_x86_feature_detected!("avx2") {
            return unsafe { simd_dot_product_avx2(a, b) };
        }
    }

    // Fallback to scalar implementation
    scalar_dot_product(a, b)
}

fn scalar_dot_product(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b.iter()).map(|(x, y)| x * y).sum()
}

#[target_feature(enable = "avx2")]
unsafe fn simd_dot_product_avx2(a: &[f32], b: &[f32]) -> f32 {
    let mut sum = _mm256_setzero_ps();
    let chunks = a.len() / 8;

    for i in 0..chunks {
        let offset = i * 8;
        let va = _mm256_loadu_ps(a.as_ptr().add(offset));
        let vb = _mm256_loadu_ps(b.as_ptr().add(offset));
        let vproduct = _mm256_mul_ps(va, vb);
        sum = _mm256_add_ps(sum, vproduct);
    }

    // Horizontal add to get final result
    let sum_array: [f32; 8] = std::mem::transmute(sum);
    sum_array.iter().sum()
}

fn matrix_multiplication_simd() {
    struct Matrix {
        data: Vec<f32>,
        rows: usize,
        cols: usize,
    }

    impl Matrix {
        fn new(rows: usize, cols: usize) -> Self {
            Matrix {
                data: vec![0.0; rows * cols],
                rows,
                cols,
            }
        }

        fn get(&self, row: usize, col: usize) -> f32 {
            self.data[row * self.cols + col]
        }

        fn set(&mut self, row: usize, col: usize, value: f32) {
            self.data[row * self.cols + col] = value;
        }

        fn multiply(&self, other: &Matrix) -> Matrix {
            assert_eq!(self.cols, other.rows);

            let mut result = Matrix::new(self.rows, other.cols);

            for i in 0..self.rows {
                for j in 0..other.cols {
                    let mut sum = 0.0;
                    for k in 0..self.cols {
                        sum += self.get(i, k) * other.get(k, j);
                    }
                    result.set(i, j, sum);
                }
            }

            result
        }
    }

    let mut a = Matrix::new(4, 4);
    let mut b = Matrix::new(4, 4);

    // Initialize matrices
    for i in 0..4 {
        for j in 0..4 {
            a.set(i, j, (i * 4 + j) as f32);
            b.set(i, j, ((i + j) * 2) as f32);
        }
    }

    let c = a.multiply(&b);
    println!("Matrix multiplication result: {}", c.get(0, 0));
}

fn parallel_simd_processing() {
    let data: Vec<f32> = (0..10000).map(|x| x as f32).collect();
    let chunk_size = 1000;

    let results: Vec<f32> = data
        .chunks(chunk_size)
        .map(|chunk| {
            // Process each chunk with SIMD
            let mut sum = 0.0;
            for &value in chunk {
                sum += value * value;
            }
            sum
        })
        .collect();

    let total: f32 = results.iter().sum();
    println!("Parallel SIMD processing result: {}", total);
}

fn prefetching_optimization() {
    unsafe fn prefetch_data(ptr: *const u8, locality: i32) {
        #[cfg(target_arch = "x86_64")]
        {
            match locality {
                0 => std::arch::x86_64::_mm_prefetch(ptr as *const i8, std::arch::x86_64::_MM_HINT_NTA),
                1 => std::arch::x86_64::_mm_prefetch(ptr as *const i8, std::arch::x86_64::_MM_HINT_T2),
                2 => std::arch::x86_64::_mm_prefetch(ptr as *const i8, std::arch::x86_64::_MM_HINT_T1),
                3 => std::arch::x86_64::_mm_prefetch(ptr as *const i8, std::arch::x86_64::_MM_HINT_T0),
                _ => {},
            }
        }
    }

    let data: Vec<i32> = (0..10000).collect();
    let mut sum = 0;

    for i in 0..data.len() {
        // Prefetch next cache line
        if i + 64 < data.len() {
            unsafe {
                prefetch_data(data.as_ptr().add(i + 64) as *const u8, 3);
            }
        }

        sum += data[i];
    }

    println!("Prefetching optimization result: {}", sum);
}

fn branch_prediction_optimization() {
    let data: Vec<i32> = (0..10000).map(|_| fastrand::i32(0..100)).collect();

    // Likely branch annotation
    fn process_likely_positive(value: i32) -> i32 {
        if likely(value > 0) {
            value * 2
        } else {
            value
        }
    }

    // Unlikely branch annotation
    fn process_unlikely_negative(value: i32) -> i32 {
        if unlikely(value < 0) {
            -value
        } else {
            value
        }
    }

    let processed1: Vec<i32> = data.iter().map(|&x| process_likely_positive(x)).collect();
    let processed2: Vec<i32> = data.iter().map(|&x| process_unlikely_negative(x)).collect();

    println!("Branch prediction optimization results: {} {}", processed1[0], processed2[0]);
}

#[inline(always)]
fn likely(condition: bool) -> bool {
    #[cfg(target_arch = "x86_64")]
    {
        std::hint::likely(condition)
    }
    #[cfg(not(target_arch = "x86_64"))]
    {
        condition
    }
}

#[inline(always)]
fn unlikely(condition: bool) -> bool {
    #[cfg(target_arch = "x86_64")]
    {
        std::hint::unlikely(condition)
    }
    #[cfg(not(target_arch = "x86_64"))]
    {
        condition
    }
}

mod fastrand {
    pub fn i32(range: std::ops::Range<i32>) -> i32 {
        range.start + (range.end - range.start) / 2
    }
}
""",
    )

    run_updater(rust_performance_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    simd_calls = [
        call
        for call in calls
        if "simd_add_avx2" in str(call) or "simd_dot_product" in str(call)
    ]
    assert len(simd_calls) > 0, "SIMD functions should be detected"


def test_parallel_processing_rayon(
    rust_performance_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parallel processing with Rayon."""
    test_file = rust_performance_project / "parallel_rayon.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use rayon::prelude::*;
use std::sync::{Arc, Mutex};
use std::time::Instant;

fn parallel_map_reduce() {
    let data: Vec<i32> = (0..1_000_000).collect();

    // Sequential processing
    let start = Instant::now();
    let sum_sequential: i32 = data.iter().map(|&x| x * x).sum();
    let sequential_time = start.elapsed();

    // Parallel processing with Rayon
    let start = Instant::now();
    let sum_parallel: i32 = data.par_iter().map(|&x| x * x).sum();
    let parallel_time = start.elapsed();

    println!("Sequential: {} in {:?}", sum_sequential, sequential_time);
    println!("Parallel: {} in {:?}", sum_parallel, parallel_time);

    assert_eq!(sum_sequential, sum_parallel);
}

fn parallel_sorting() {
    let mut data1: Vec<i32> = (0..100_000).rev().collect();
    let mut data2 = data1.clone();

    // Sequential sort
    let start = Instant::now();
    data1.sort();
    let sequential_time = start.elapsed();

    // Parallel sort
    let start = Instant::now();
    data2.par_sort();
    let parallel_time = start.elapsed();

    println!("Sequential sort: {:?}", sequential_time);
    println!("Parallel sort: {:?}", parallel_time);

    assert_eq!(data1, data2);
}

fn parallel_filter_map() {
    let numbers: Vec<i32> = (0..1_000_000).collect();

    let primes: Vec<i32> = numbers
        .par_iter()
        .filter_map(|&n| {
            if is_prime(n) {
                Some(n)
            } else {
                None
            }
        })
        .collect();

    println!("Found {} primes", primes.len());
}

fn is_prime(n: i32) -> bool {
    if n < 2 {
        return false;
    }
    if n == 2 {
        return true;
    }
    if n % 2 == 0 {
        return false;
    }

    let sqrt_n = (n as f64).sqrt() as i32;
    for i in (3..=sqrt_n).step_by(2) {
        if n % i == 0 {
            return false;
        }
    }
    true
}

fn parallel_fold_reduce() {
    let data: Vec<f64> = (0..1_000_000).map(|x| x as f64).collect();

    // Parallel fold with identity and reduce
    let sum = data
        .par_iter()
        .fold(|| 0.0, |acc, &x| acc + x * x)
        .reduce(|| 0.0, |a, b| a + b);

    println!("Parallel fold-reduce sum: {}", sum);

    // Parallel reduce for associative operations
    let product = data[0..1000]
        .par_iter()
        .map(|&x| x + 1.0) // Avoid zeros
        .reduce(|| 1.0, |a, b| a * b);

    println!("Parallel reduce product: {}", product);
}

fn parallel_group_by() {
    let data: Vec<i32> = (0..100_000).map(|_| fastrand::i32(0..100)).collect();

    // Group by remainder when divided by 10
    let groups: Vec<Vec<i32>> = (0..10)
        .into_par_iter()
        .map(|remainder| {
            data.par_iter()
                .filter_map(|&x| {
                    if x % 10 == remainder {
                        Some(x)
                    } else {
                        None
                    }
                })
                .collect()
        })
        .collect();

    for (i, group) in groups.iter().enumerate() {
        println!("Group {}: {} elements", i, group.len());
    }
}

fn parallel_matrix_operations() {
    struct Matrix {
        data: Vec<f64>,
        rows: usize,
        cols: usize,
    }

    impl Matrix {
        fn new(rows: usize, cols: usize, init_value: f64) -> Self {
            Matrix {
                data: vec![init_value; rows * cols],
                rows,
                cols,
            }
        }

        fn get(&self, row: usize, col: usize) -> f64 {
            self.data[row * self.cols + col]
        }

        fn set(&mut self, row: usize, col: usize, value: f64) {
            self.data[row * self.cols + col] = value;
        }

        fn parallel_multiply(&self, other: &Matrix) -> Matrix {
            assert_eq!(self.cols, other.rows);

            let mut result = Matrix::new(self.rows, other.cols, 0.0);

            // Parallel iteration over rows
            result.data
                .par_chunks_mut(other.cols)
                .enumerate()
                .for_each(|(i, result_row)| {
                    for j in 0..other.cols {
                        let mut sum = 0.0;
                        for k in 0..self.cols {
                            sum += self.get(i, k) * other.get(k, j);
                        }
                        result_row[j] = sum;
                    }
                });

            result
        }

        fn parallel_scalar_multiply(&mut self, scalar: f64) {
            self.data.par_iter_mut().for_each(|x| *x *= scalar);
        }
    }

    let mut a = Matrix::new(100, 100, 1.0);
    let b = Matrix::new(100, 100, 2.0);

    let c = a.parallel_multiply(&b);
    a.parallel_scalar_multiply(3.0);

    println!("Matrix operations complete: {}", c.get(0, 0));
}

fn parallel_file_processing() {
    use std::fs;
    use std::path::Path;

    fn count_lines_in_file(path: &Path) -> Result<usize, std::io::Error> {
        let contents = fs::read_to_string(path)?;
        Ok(contents.lines().count())
    }

    let files = vec!["file1.txt", "file2.txt", "file3.txt"];

    let line_counts: Vec<usize> = files
        .par_iter()
        .filter_map(|filename| {
            let path = Path::new(filename);
            count_lines_in_file(path).ok()
        })
        .collect();

    let total_lines: usize = line_counts.iter().sum();
    println!("Total lines across files: {}", total_lines);
}

fn parallel_pipeline_processing() {
    let data: Vec<String> = (0..10000)
        .map(|i| format!("item_{}", i))
        .collect();

    // Multi-stage parallel pipeline
    let processed: Vec<String> = data
        .par_iter()
        .map(|s| format!("processed_{}", s))    // Stage 1: Add prefix
        .map(|s| s.to_uppercase())              // Stage 2: Convert to uppercase
        .filter(|s| s.len() > 10)               // Stage 3: Filter by length
        .map(|s| format!("{}_final", s))        // Stage 4: Add suffix
        .collect();

    println!("Pipeline processed {} items", processed.len());
}

fn work_stealing_example() {
    use rayon::ThreadPoolBuilder;

    let pool = ThreadPoolBuilder::new()
        .num_threads(4)
        .build()
        .unwrap();

    pool.install(|| {
        // Uneven work distribution to demonstrate work stealing
        let work_items: Vec<usize> = vec![1, 10, 100, 1000, 10000];

        let results: Vec<u64> = work_items
            .par_iter()
            .map(|&n| {
                // Simulate work proportional to n
                (0..n).map(|i| i as u64).sum()
            })
            .collect();

        println!("Work stealing results: {:?}", results);
    });
}

fn parallel_search_algorithms() {
    let data: Vec<i32> = (0..1_000_000).collect();
    let target = 500_000;

    // Parallel binary search (for demonstration)
    fn parallel_binary_search(arr: &[i32], target: i32) -> Option<usize> {
        if arr.is_empty() {
            return None;
        }

        let mid = arr.len() / 2;
        match arr[mid].cmp(&target) {
            std::cmp::Ordering::Equal => Some(mid),
            std::cmp::Ordering::Greater => {
                parallel_binary_search(&arr[..mid], target)
            },
            std::cmp::Ordering::Less => {
                parallel_binary_search(&arr[mid + 1..], target)
                    .map(|idx| idx + mid + 1)
            },
        }
    }

    // Parallel linear search
    let found_linear = data
        .par_iter()
        .position_any(|&x| x == target);

    let found_binary = parallel_binary_search(&data, target);

    println!("Linear search found: {:?}", found_linear);
    println!("Binary search found: {:?}", found_binary);
}

mod fastrand {
    pub fn i32(range: std::ops::Range<i32>) -> i32 {
        range.start + (range.end - range.start) / 2
    }
}
""",
    )

    run_updater(rust_performance_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    parallel_calls = [
        call
        for call in calls
        if "parallel_map_reduce" in str(call) or "parallel_sorting" in str(call)
    ]
    assert len(parallel_calls) > 0, "Parallel processing functions should be detected"


def test_memory_optimization(
    rust_performance_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test memory optimization patterns."""
    test_file = rust_performance_project / "memory_optimization.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::mem;
use std::alloc::{alloc, dealloc, Layout};

fn cache_friendly_data_structures() {
    // Array of Structs (AoS) - not cache friendly for partial access
    #[derive(Clone)]
    struct Particle {
        x: f32,
        y: f32,
        z: f32,
        mass: f32,
        velocity_x: f32,
        velocity_y: f32,
        velocity_z: f32,
    }

    let particles_aos: Vec<Particle> = (0..1000)
        .map(|i| Particle {
            x: i as f32,
            y: i as f32,
            z: i as f32,
            mass: 1.0,
            velocity_x: 0.0,
            velocity_y: 0.0,
            velocity_z: 0.0,
        })
        .collect();

    // Struct of Arrays (SoA) - more cache friendly
    struct ParticleSystem {
        x: Vec<f32>,
        y: Vec<f32>,
        z: Vec<f32>,
        mass: Vec<f32>,
        velocity_x: Vec<f32>,
        velocity_y: Vec<f32>,
        velocity_z: Vec<f32>,
    }

    let mut particles_soa = ParticleSystem {
        x: Vec::new(),
        y: Vec::new(),
        z: Vec::new(),
        mass: Vec::new(),
        velocity_x: Vec::new(),
        velocity_y: Vec::new(),
        velocity_z: Vec::new(),
    };

    for i in 0..1000 {
        particles_soa.x.push(i as f32);
        particles_soa.y.push(i as f32);
        particles_soa.z.push(i as f32);
        particles_soa.mass.push(1.0);
        particles_soa.velocity_x.push(0.0);
        particles_soa.velocity_y.push(0.0);
        particles_soa.velocity_z.push(0.0);
    }

    // Process only positions (cache friendly with SoA)
    for i in 0..particles_soa.x.len() {
        particles_soa.x[i] += particles_soa.velocity_x[i];
        particles_soa.y[i] += particles_soa.velocity_y[i];
        particles_soa.z[i] += particles_soa.velocity_z[i];
    }

    println!("Cache friendly processing complete");
}

fn memory_pool_allocation() {
    struct MemoryPool<T> {
        pool: Vec<Option<T>>,
        free_indices: Vec<usize>,
    }

    impl<T> MemoryPool<T> {
        fn new(capacity: usize) -> Self {
            let mut pool = Vec::with_capacity(capacity);
            let mut free_indices = Vec::with_capacity(capacity);

            for i in 0..capacity {
                pool.push(None);
                free_indices.push(i);
            }

            MemoryPool { pool, free_indices }
        }

        fn allocate(&mut self, item: T) -> Option<usize> {
            if let Some(index) = self.free_indices.pop() {
                self.pool[index] = Some(item);
                Some(index)
            } else {
                None
            }
        }

        fn deallocate(&mut self, index: usize) -> Option<T> {
            if index < self.pool.len() && self.pool[index].is_some() {
                let item = self.pool[index].take();
                self.free_indices.push(index);
                item
            } else {
                None
            }
        }

        fn get(&self, index: usize) -> Option<&T> {
            self.pool.get(index)?.as_ref()
        }

        fn get_mut(&mut self, index: usize) -> Option<&mut T> {
            self.pool.get_mut(index)?.as_mut()
        }
    }

    let mut pool: MemoryPool<String> = MemoryPool::new(100);

    let id1 = pool.allocate("Hello".to_string()).unwrap();
    let id2 = pool.allocate("World".to_string()).unwrap();

    println!("Allocated: {} {}", pool.get(id1).unwrap(), pool.get(id2).unwrap());

    pool.deallocate(id1);
    let id3 = pool.allocate("Rust".to_string()).unwrap();

    println!("After reallocation: {}", pool.get(id3).unwrap());
}

fn zero_copy_operations() {
    // Using Cow for zero-copy when possible
    use std::borrow::Cow;

    fn process_string(input: &str) -> Cow<str> {
        if input.contains("bad") {
            Cow::Owned(input.replace("bad", "good"))
        } else {
            Cow::Borrowed(input)
        }
    }

    let good_string = "This is a good string";
    let bad_string = "This is a bad string";

    let result1 = process_string(good_string);  // Zero-copy
    let result2 = process_string(bad_string);   // Copy only when needed

    println!("Results: {} {}", result1, result2);

    // Slice operations for zero-copy
    let data = vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
    let slice1 = &data[0..5];   // Zero-copy view
    let slice2 = &data[5..];    // Zero-copy view

    println!("Slices: {:?} {:?}", slice1, slice2);
}

fn custom_allocator_example() {
    use std::alloc::{GlobalAlloc, Layout};
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct TrackingAllocator {
        allocated: AtomicUsize,
    }

    impl TrackingAllocator {
        const fn new() -> Self {
            TrackingAllocator {
                allocated: AtomicUsize::new(0),
            }
        }

        fn allocated_bytes(&self) -> usize {
            self.allocated.load(Ordering::Relaxed)
        }
    }

    unsafe impl GlobalAlloc for TrackingAllocator {
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            let ptr = std::alloc::System.alloc(layout);
            if !ptr.is_null() {
                self.allocated.fetch_add(layout.size(), Ordering::Relaxed);
            }
            ptr
        }

        unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
            std::alloc::System.dealloc(ptr, layout);
            self.allocated.fetch_sub(layout.size(), Ordering::Relaxed);
        }
    }

    static ALLOCATOR: TrackingAllocator = TrackingAllocator::new();

    println!("Current allocation: {} bytes", ALLOCATOR.allocated_bytes());
}

fn memory_layout_optimization() {
    // Bad layout - lots of padding
    #[repr(C)]
    struct BadLayout {
        a: u8,   // 1 byte
        b: u64,  // 8 bytes, but needs alignment
        c: u8,   // 1 byte
        d: u32,  // 4 bytes, but needs alignment
    }

    // Good layout - minimal padding
    #[repr(C)]
    struct GoodLayout {
        b: u64,  // 8 bytes
        d: u32,  // 4 bytes
        a: u8,   // 1 byte
        c: u8,   // 1 byte
        // 2 bytes padding at end
    }

    // Packed layout - no padding (careful with alignment!)
    #[repr(C, packed)]
    struct PackedLayout {
        a: u8,
        b: u64,
        c: u8,
        d: u32,
    }

    println!("BadLayout size: {}", mem::size_of::<BadLayout>());
    println!("GoodLayout size: {}", mem::size_of::<GoodLayout>());
    println!("PackedLayout size: {}", mem::size_of::<PackedLayout>());

    println!("BadLayout align: {}", mem::align_of::<BadLayout>());
    println!("GoodLayout align: {}", mem::align_of::<GoodLayout>());
    println!("PackedLayout align: {}", mem::align_of::<PackedLayout>());
}

fn stack_allocation_optimization() {
    // Use stack allocation when possible
    const STACK_SIZE: usize = 1024;

    fn process_small_data(data: &[u8]) -> [u8; STACK_SIZE] {
        let mut result = [0u8; STACK_SIZE];
        let copy_len = data.len().min(STACK_SIZE);
        result[..copy_len].copy_from_slice(&data[..copy_len]);
        result
    }

    // For larger data, use heap allocation
    fn process_large_data(data: &[u8]) -> Vec<u8> {
        let mut result = Vec::with_capacity(data.len());
        result.extend_from_slice(data);
        result
    }

    let small_data = [1u8; 512];
    let large_data = vec![1u8; 10000];

    let small_result = process_small_data(&small_data);
    let large_result = process_large_data(&large_data);

    println!("Stack result: {}", small_result[0]);
    println!("Heap result: {}", large_result[0]);
}

fn memory_access_patterns() {
    let size = 1000;
    let mut matrix = vec![vec![0i32; size]; size];

    // Bad access pattern - column-major (cache misses)
    let start = std::time::Instant::now();
    for j in 0..size {
        for i in 0..size {
            matrix[i][j] = i as i32 * j as i32;
        }
    }
    let column_major_time = start.elapsed();

    // Good access pattern - row-major (cache friendly)
    let start = std::time::Instant::now();
    for i in 0..size {
        for j in 0..size {
            matrix[i][j] = i as i32 * j as i32;
        }
    }
    let row_major_time = start.elapsed();

    println!("Column-major time: {:?}", column_major_time);
    println!("Row-major time: {:?}", row_major_time);
}

fn memory_mapped_files() {
    use std::fs::File;
    use std::io::Write;

    fn create_test_file() -> Result<(), std::io::Error> {
        let mut file = File::create("test_data.bin")?;
        let data: Vec<u8> = (0..1000).map(|x| (x % 256) as u8).collect();
        file.write_all(&data)?;
        Ok(())
    }

    // Memory-mapped file processing would go here
    // (requires external crate like memmap2)

    if create_test_file().is_ok() {
        println!("Test file created for memory mapping");
    }
}
""",
    )

    run_updater(rust_performance_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    memory_calls = [
        call
        for call in calls
        if "cache_friendly_data_structures" in str(call)
        or "memory_pool_allocation" in str(call)
    ]
    assert len(memory_calls) > 0, "Memory optimization functions should be detected"


def test_profiling_optimization_tools(
    rust_performance_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test profiling and optimization tooling patterns."""
    test_file = rust_performance_project / "profiling_tools.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::time::{Duration, Instant};
use std::collections::HashMap;

struct ProfilerScope {
    name: String,
    start_time: Instant,
}

impl ProfilerScope {
    fn new(name: &str) -> Self {
        ProfilerScope {
            name: name.to_string(),
            start_time: Instant::now(),
        }
    }
}

impl Drop for ProfilerScope {
    fn drop(&mut self) {
        let duration = self.start_time.elapsed();
        println!("PROFILE: {} took {:?}", self.name, duration);
    }
}

macro_rules! profile {
    ($name:expr) => {
        let _scope = ProfilerScope::new($name);
    };
}

fn profiled_function() {
    profile!("profiled_function");

    // Simulate work
    let mut sum = 0;
    for i in 0..1000000 {
        sum += i;
    }

    {
        profile!("nested_work");
        for i in 0..100000 {
            sum += i * i;
        }
    }

    println!("Work complete: {}", sum);
}

struct PerformanceCounter {
    counters: HashMap<String, u64>,
    timers: HashMap<String, Duration>,
}

impl PerformanceCounter {
    fn new() -> Self {
        PerformanceCounter {
            counters: HashMap::new(),
            timers: HashMap::new(),
        }
    }

    fn increment(&mut self, name: &str) {
        *self.counters.entry(name.to_string()).or_insert(0) += 1;
    }

    fn add_time(&mut self, name: &str, duration: Duration) {
        let entry = self.timers.entry(name.to_string()).or_insert(Duration::ZERO);
        *entry += duration;
    }

    fn time_function<F, R>(&mut self, name: &str, f: F) -> R
    where
        F: FnOnce() -> R,
    {
        let start = Instant::now();
        let result = f();
        let duration = start.elapsed();
        self.add_time(name, duration);
        result
    }

    fn report(&self) {
        println!("=== Performance Report ===");

        for (name, count) in &self.counters {
            println!("Counter {}: {}", name, count);
        }

        for (name, duration) in &self.timers {
            println!("Timer {}: {:?}", name, duration);
        }
    }
}

fn performance_monitoring() {
    let mut perf = PerformanceCounter::new();

    perf.time_function("data_processing", || {
        let data: Vec<i32> = (0..100000).collect();
        let _sum: i32 = data.iter().sum();

        for _ in 0..data.len() {
            perf.increment("loop_iteration");
        }
    });

    perf.time_function("string_operations", || {
        let mut result = String::new();
        for i in 0..1000 {
            result.push_str(&format!("item_{} ", i));
            perf.increment("string_append");
        }
    });

    perf.report();
}

fn flame_graph_simulation() {
    fn level_1() {
        profile!("level_1");
        level_2();
        level_3();
    }

    fn level_2() {
        profile!("level_2");
        // Simulate work
        std::thread::sleep(Duration::from_millis(10));
        level_4();
    }

    fn level_3() {
        profile!("level_3");
        // Simulate work
        std::thread::sleep(Duration::from_millis(5));
    }

    fn level_4() {
        profile!("level_4");
        // Simulate work
        std::thread::sleep(Duration::from_millis(15));
    }

    profile!("flame_graph_simulation");
    level_1();
}

fn memory_usage_tracking() {
    struct MemoryTracker {
        peak_usage: usize,
        current_usage: usize,
        allocations: HashMap<String, usize>,
    }

    impl MemoryTracker {
        fn new() -> Self {
            MemoryTracker {
                peak_usage: 0,
                current_usage: 0,
                allocations: HashMap::new(),
            }
        }

        fn track_allocation(&mut self, name: &str, size: usize) {
            self.current_usage += size;
            if self.current_usage > self.peak_usage {
                self.peak_usage = self.current_usage;
            }

            *self.allocations.entry(name.to_string()).or_insert(0) += size;

            println!("ALLOC: {} {} bytes (current: {}, peak: {})",
                    name, size, self.current_usage, self.peak_usage);
        }

        fn track_deallocation(&mut self, name: &str, size: usize) {
            self.current_usage = self.current_usage.saturating_sub(size);

            if let Some(total) = self.allocations.get_mut(name) {
                *total = total.saturating_sub(size);
            }

            println!("DEALLOC: {} {} bytes (current: {})", name, size, self.current_usage);
        }

        fn report(&self) {
            println!("=== Memory Report ===");
            println!("Current usage: {} bytes", self.current_usage);
            println!("Peak usage: {} bytes", self.peak_usage);

            for (name, size) in &self.allocations {
                if *size > 0 {
                    println!("Outstanding allocation {}: {} bytes", name, size);
                }
            }
        }
    }

    let mut tracker = MemoryTracker::new();

    // Simulate allocations
    let data1 = vec![0u8; 1000];
    tracker.track_allocation("data1", data1.len());

    let data2 = vec![0u8; 2000];
    tracker.track_allocation("data2", data2.len());

    // Simulate deallocation
    tracker.track_deallocation("data1", data1.len());
    drop(data1);

    tracker.report();
}

fn cpu_cache_simulation() {
    struct CacheSimulator {
        l1_hits: u64,
        l1_misses: u64,
        l2_hits: u64,
        l2_misses: u64,
    }

    impl CacheSimulator {
        fn new() -> Self {
            CacheSimulator {
                l1_hits: 0,
                l1_misses: 0,
                l2_hits: 0,
                l2_misses: 0,
            }
        }

        fn access(&mut self, address: usize) {
            // Simplified cache simulation
            let cache_line = address / 64; // 64-byte cache lines

            if cache_line % 8 == 0 {
                self.l1_hits += 1;
            } else if cache_line % 4 == 0 {
                self.l1_misses += 1;
                self.l2_hits += 1;
            } else {
                self.l1_misses += 1;
                self.l2_misses += 1;
            }
        }

        fn report(&self) {
            let total_accesses = self.l1_hits + self.l1_misses;
            let l1_hit_rate = if total_accesses > 0 {
                self.l1_hits as f64 / total_accesses as f64 * 100.0
            } else {
                0.0
            };

            println!("=== Cache Simulation Report ===");
            println!("L1 hits: {} ({:.1}%)", self.l1_hits, l1_hit_rate);
            println!("L1 misses: {}", self.l1_misses);
            println!("L2 hits: {}", self.l2_hits);
            println!("L2 misses: {}", self.l2_misses);
        }
    }

    let mut cache = CacheSimulator::new();

    // Simulate memory access patterns
    let data = vec![0u8; 10000];

    // Sequential access (cache friendly)
    for i in 0..data.len() {
        cache.access(data.as_ptr() as usize + i);
    }

    // Random access (cache unfriendly)
    for i in (0..data.len()).step_by(137) {
        cache.access(data.as_ptr() as usize + i);
    }

    cache.report();
}

fn branch_prediction_analysis() {
    struct BranchPredictor {
        predictions: HashMap<usize, bool>,
        correct_predictions: u64,
        total_predictions: u64,
    }

    impl BranchPredictor {
        fn new() -> Self {
            BranchPredictor {
                predictions: HashMap::new(),
                correct_predictions: 0,
                total_predictions: 0,
            }
        }

        fn predict_branch(&mut self, pc: usize, taken: bool) {
            // Simple predictor: assume same as last time
            let predicted = self.predictions.get(&pc).copied().unwrap_or(false);

            if predicted == taken {
                self.correct_predictions += 1;
            }

            self.predictions.insert(pc, taken);
            self.total_predictions += 1;
        }

        fn accuracy(&self) -> f64 {
            if self.total_predictions > 0 {
                self.correct_predictions as f64 / self.total_predictions as f64 * 100.0
            } else {
                0.0
            }
        }

        fn report(&self) {
            println!("=== Branch Prediction Report ===");
            println!("Correct predictions: {}/{} ({:.1}%)",
                    self.correct_predictions, self.total_predictions, self.accuracy());
        }
    }

    let mut predictor = BranchPredictor::new();
    let data = vec![1, 2, 1, 2, 3, 1, 2, 3, 4, 1];

    for (pc, &value) in data.iter().enumerate() {
        let branch_taken = value > 2;
        predictor.predict_branch(pc, branch_taken);
    }

    predictor.report();
}

fn hotspot_detection() {
    struct HotspotDetector {
        function_calls: HashMap<String, u64>,
        function_time: HashMap<String, Duration>,
    }

    impl HotspotDetector {
        fn new() -> Self {
            HotspotDetector {
                function_calls: HashMap::new(),
                function_time: HashMap::new(),
            }
        }

        fn record_call(&mut self, function: &str, duration: Duration) {
            *self.function_calls.entry(function.to_string()).or_insert(0) += 1;

            let total_time = self.function_time.entry(function.to_string()).or_insert(Duration::ZERO);
            *total_time += duration;
        }

        fn report_hotspots(&self, top_n: usize) {
            let mut functions: Vec<(&String, &Duration)> = self.function_time.iter().collect();
            functions.sort_by(|a, b| b.1.cmp(a.1));

            println!("=== Top {} Hotspots ===", top_n);
            for (i, (function, duration)) in functions.iter().take(top_n).enumerate() {
                let calls = self.function_calls.get(*function).unwrap_or(&0);
                let avg_duration = duration.as_nanos() / (*calls as u128).max(1);

                println!("{}. {} - {:?} total, {} calls, {}ns avg",
                        i + 1, function, duration, calls, avg_duration);
            }
        }
    }

    let mut detector = HotspotDetector::new();

    // Simulate function calls with different performance characteristics
    for _ in 0..1000 {
        detector.record_call("fast_function", Duration::from_nanos(100));
    }

    for _ in 0..100 {
        detector.record_call("medium_function", Duration::from_micros(10));
    }

    for _ in 0..10 {
        detector.record_call("slow_function", Duration::from_millis(1));
    }

    detector.report_hotspots(5);
}
""",
    )

    run_updater(rust_performance_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    profiling_calls = [
        call
        for call in calls
        if "profiled_function" in str(call) or "performance_monitoring" in str(call)
    ]
    assert len(profiling_calls) > 0, "Profiling functions should be detected"
