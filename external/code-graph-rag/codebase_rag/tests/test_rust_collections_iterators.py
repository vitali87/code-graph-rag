from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_collections_project(temp_repo: Path) -> Path:
    """Create a Rust project with collections examples."""
    project_path = temp_repo / "rust_collections_test"
    project_path.mkdir()

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""
[package]
name = "rust_collections_test"
version = "0.1.0"
edition = "2021"
""",
    )

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Collections test crate"
    )

    return project_path


def test_vector_operations(
    rust_collections_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test vector operations and methods."""
    test_file = rust_collections_project / "vectors.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
fn basic_vector_operations() {
    let mut v = Vec::new();
    v.push(1);
    v.push(2);
    v.push(3);

    let third: &i32 = &v[2];
    println!("The third element is {}", third);

    match v.get(2) {
        Some(third) => println!("The third element is {}", third),
        None => println!("There is no third element."),
    }

    v.pop();

    for i in &v {
        println!("{}", i);
    }

    for i in &mut v {
        *i += 50;
    }
}

fn vector_with_enum() {
    enum SpreadsheetCell {
        Int(i32),
        Float(f64),
        Text(String),
    }

    let row = vec![
        SpreadsheetCell::Int(3),
        SpreadsheetCell::Text(String::from("blue")),
        SpreadsheetCell::Float(10.12),
    ];

    for cell in &row {
        match cell {
            SpreadsheetCell::Int(i) => println!("Integer: {}", i),
            SpreadsheetCell::Float(f) => println!("Float: {}", f),
            SpreadsheetCell::Text(s) => println!("Text: {}", s),
        }
    }
}

fn vector_capacity_management() {
    let mut v = Vec::with_capacity(10);
    println!("Initial capacity: {}", v.capacity());

    for i in 0..15 {
        v.push(i);
    }

    println!("After 15 pushes, capacity: {}", v.capacity());
    println!("Length: {}", v.len());

    v.shrink_to_fit();
    println!("After shrink_to_fit, capacity: {}", v.capacity());

    v.reserve(100);
    println!("After reserve(100), capacity: {}", v.capacity());
}

fn vector_slicing() {
    let v = vec![1, 2, 3, 4, 5];

    let slice = &v[1..4];
    println!("Slice: {:?}", slice);

    let first_half = &v[..3];
    let second_half = &v[2..];

    println!("First half: {:?}", first_half);
    println!("Second half: {:?}", second_half);
}

fn vector_sorting() {
    let mut numbers = vec![5, 2, 8, 1, 9, 3];

    numbers.sort();
    println!("Sorted: {:?}", numbers);

    let mut strings = vec!["banana", "apple", "cherry", "date"];
    strings.sort();
    println!("Sorted strings: {:?}", strings);

    let mut pairs = vec![(2, "two"), (1, "one"), (3, "three")];
    pairs.sort_by_key(|item| item.0);
    println!("Sorted pairs: {:?}", pairs);
}

fn vector_deduplication() {
    let mut v = vec![1, 2, 2, 3, 2, 4, 2];
    v.sort();
    v.dedup();
    println!("Deduplicated: {:?}", v);

    let mut v2 = vec![1, 2, 3, 2, 1];
    v2.retain(|&x| x != 2);
    println!("After retaining non-2s: {:?}", v2);
}

fn vector_binary_search() {
    let v = vec![1, 3, 5, 7, 9, 11];

    match v.binary_search(&5) {
        Ok(index) => println!("Found 5 at index {}", index),
        Err(index) => println!("5 not found, would insert at index {}", index),
    }

    match v.binary_search(&6) {
        Ok(index) => println!("Found 6 at index {}", index),
        Err(index) => println!("6 not found, would insert at index {}", index),
    }
}
""",
    )

    run_updater(rust_collections_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    vector_calls = [
        call
        for call in calls
        if "basic_vector_operations" in str(call) or "vector_sorting" in str(call)
    ]
    assert len(vector_calls) > 0, "Vector functions should be detected"


def test_hashmap_operations(
    rust_collections_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test HashMap operations and methods."""
    test_file = rust_collections_project / "hashmaps.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::collections::HashMap;

fn basic_hashmap_operations() {
    let mut scores = HashMap::new();

    scores.insert(String::from("Blue"), 10);
    scores.insert(String::from("Yellow"), 50);

    let team_name = String::from("Blue");
    let score = scores.get(&team_name);

    match score {
        Some(s) => println!("Blue team score: {}", s),
        None => println!("Blue team not found"),
    }

    for (key, value) in &scores {
        println!("{}: {}", key, value);
    }
}

fn hashmap_from_vectors() {
    let teams = vec![String::from("Blue"), String::from("Yellow")];
    let initial_scores = vec![10, 50];

    let scores: HashMap<_, _> = teams
        .into_iter()
        .zip(initial_scores.into_iter())
        .collect();

    println!("Scores from vectors: {:?}", scores);
}

fn hashmap_ownership() {
    let mut map = HashMap::new();

    let field_name = String::from("Favorite color");
    let field_value = String::from("Blue");

    map.insert(field_name, field_value);
    // field_name and field_value are invalid at this point

    println!("Map: {:?}", map);
}

fn hashmap_updating() {
    let mut scores = HashMap::new();

    // Overwriting values
    scores.insert(String::from("Blue"), 10);
    scores.insert(String::from("Blue"), 25);

    println!("Overwritten: {:?}", scores);

    // Only inserting if key has no value
    scores.entry(String::from("Yellow")).or_insert(50);
    scores.entry(String::from("Blue")).or_insert(50);

    println!("After or_insert: {:?}", scores);

    // Updating based on old value
    let text = "hello world wonderful world";
    let mut word_counts = HashMap::new();

    for word in text.split_whitespace() {
        let count = word_counts.entry(word).or_insert(0);
        *count += 1;
    }

    println!("Word counts: {:?}", word_counts);
}

fn hashmap_advanced_operations() {
    let mut map: HashMap<String, i32> = HashMap::new();
    map.insert("a".to_string(), 1);
    map.insert("b".to_string(), 2);
    map.insert("c".to_string(), 3);

    // Remove and get the value
    if let Some(removed) = map.remove("b") {
        println!("Removed value: {}", removed);
    }

    // Check if key exists
    if map.contains_key("a") {
        println!("Key 'a' exists");
    }

    // Get or insert with default
    let value = map.entry("d".to_string()).or_insert(4);
    *value += 10;

    println!("Final map: {:?}", map);
}

fn hashmap_with_custom_types() {
    use std::hash::{Hash, Hasher};
    use std::collections::hash_map::DefaultHasher;

    #[derive(Debug, PartialEq, Eq, Hash)]
    struct Person {
        name: String,
        age: u32,
    }

    let mut people = HashMap::new();

    let person1 = Person {
        name: "Alice".to_string(),
        age: 30,
    };

    let person2 = Person {
        name: "Bob".to_string(),
        age: 25,
    };

    people.insert(person1, "Engineer");
    people.insert(person2, "Designer");

    println!("People map: {:?}", people);
}

fn hashmap_iteration_patterns() {
    let mut map = HashMap::new();
    map.insert("a", 1);
    map.insert("b", 2);
    map.insert("c", 3);

    // Iterate over keys
    for key in map.keys() {
        println!("Key: {}", key);
    }

    // Iterate over values
    for value in map.values() {
        println!("Value: {}", value);
    }

    // Iterate over mutable values
    for value in map.values_mut() {
        *value *= 2;
    }

    // Iterate over key-value pairs
    for (key, value) in &map {
        println!("{}: {}", key, value);
    }

    println!("Final map: {:?}", map);
}
""",
    )

    run_updater(rust_collections_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    hashmap_calls = [
        call
        for call in calls
        if "basic_hashmap_operations" in str(call) or "hashmap_updating" in str(call)
    ]
    assert len(hashmap_calls) > 0, "HashMap functions should be detected"


def test_iterator_patterns(
    rust_collections_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test iterator patterns and methods."""
    test_file = rust_collections_project / "iterators.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
fn basic_iterator_usage() {
    let v = vec![1, 2, 3, 4, 5];

    // for loop creates iterator automatically
    for item in &v {
        println!("Item: {}", item);
    }

    // Explicit iterator creation
    let v_iter = v.iter();
    for item in v_iter {
        println!("Explicit iter: {}", item);
    }

    // Consuming the iterator
    let sum: i32 = v.iter().sum();
    println!("Sum: {}", sum);
}

fn iterator_adaptors() {
    let v = vec![1, 2, 3, 4, 5];

    // Map - transform each element
    let doubled: Vec<i32> = v.iter().map(|x| x * 2).collect();
    println!("Doubled: {:?}", doubled);

    // Filter - keep only elements matching predicate
    let even: Vec<i32> = v.iter().filter(|&x| x % 2 == 0).collect();
    println!("Even numbers: {:?}", even);

    // Chain iterators
    let v2 = vec![6, 7, 8];
    let chained: Vec<i32> = v.iter().chain(v2.iter()).cloned().collect();
    println!("Chained: {:?}", chained);

    // Enumerate - add indices
    let indexed: Vec<(usize, i32)> = v.iter().enumerate().map(|(i, &x)| (i, x)).collect();
    println!("Indexed: {:?}", indexed);
}

fn iterator_consuming_adaptors() {
    let v = vec![1, 2, 3, 4, 5];

    // Collect into different collections
    let collected_vec: Vec<i32> = v.iter().cloned().collect();
    let collected_set: std::collections::HashSet<i32> = v.iter().cloned().collect();

    println!("Collected vec: {:?}", collected_vec);
    println!("Collected set: {:?}", collected_set);

    // Reduce operations
    let sum: i32 = v.iter().sum();
    let product: i32 = v.iter().product();
    let max = v.iter().max();
    let min = v.iter().min();

    println!("Sum: {}, Product: {}", sum, product);
    println!("Max: {:?}, Min: {:?}", max, min);

    // Find operations
    let found = v.iter().find(|&&x| x > 3);
    let position = v.iter().position(|&x| x > 3);

    println!("Found: {:?}, Position: {:?}", found, position);
}

fn iterator_fold_reduce() {
    let v = vec![1, 2, 3, 4, 5];

    // Fold with initial value
    let sum = v.iter().fold(0, |acc, &x| acc + x);
    println!("Fold sum: {}", sum);

    // Reduce without initial value
    let sum_reduce = v.iter().cloned().reduce(|acc, x| acc + x);
    println!("Reduce sum: {:?}", sum_reduce);

    // Complex fold example
    let words = vec!["hello", "world", "rust"];
    let sentence = words.iter().fold(String::new(), |mut acc, &word| {
        if !acc.is_empty() {
            acc.push(' ');
        }
        acc.push_str(word);
        acc
    });
    println!("Sentence: {}", sentence);
}

fn iterator_zip_operations() {
    let names = vec!["Alice", "Bob", "Charlie"];
    let ages = vec![30, 25, 35];
    let cities = vec!["New York", "London", "Tokyo"];

    // Zip two iterators
    let people: Vec<(&&str, &i32)> = names.iter().zip(ages.iter()).collect();
    println!("People: {:?}", people);

    // Zip three iterators (using multiple zips)
    let full_info: Vec<(&&str, &i32, &&str)> = names
        .iter()
        .zip(ages.iter())
        .zip(cities.iter())
        .map(|((name, age), city)| (name, age, city))
        .collect();

    println!("Full info: {:?}", full_info);
}

fn iterator_partition_group() {
    let numbers = vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10];

    // Partition into two collections
    let (even, odd): (Vec<i32>, Vec<i32>) = numbers
        .iter()
        .cloned()
        .partition(|&x| x % 2 == 0);

    println!("Even: {:?}", even);
    println!("Odd: {:?}", odd);

    // Group consecutive elements
    let words = vec!["apple", "banana", "apricot", "blueberry", "avocado"];
    let grouped_by_first_letter: std::collections::HashMap<char, Vec<&str>> =
        words.iter().fold(std::collections::HashMap::new(), |mut acc, &word| {
            let first_char = word.chars().next().unwrap();
            acc.entry(first_char).or_insert(Vec::new()).push(word);
            acc
        });

    println!("Grouped by first letter: {:?}", grouped_by_first_letter);
}

fn iterator_take_skip() {
    let numbers: Vec<i32> = (0..20).collect();

    // Take first n elements
    let first_five: Vec<i32> = numbers.iter().take(5).cloned().collect();
    println!("First five: {:?}", first_five);

    // Skip first n elements
    let skip_five: Vec<i32> = numbers.iter().skip(5).cloned().collect();
    println!("Skip five: {:?}", skip_five);

    // Take while condition is true
    let take_while_small: Vec<i32> = numbers.iter().take_while(|&&x| x < 5).cloned().collect();
    println!("Take while < 5: {:?}", take_while_small);

    // Skip while condition is true
    let skip_while_small: Vec<i32> = numbers.iter().skip_while(|&&x| x < 5).cloned().collect();
    println!("Skip while < 5: {:?}", skip_while_small);
}

fn custom_iterator() {
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

    let counter = Counter::new(5);
    let squares: Vec<usize> = counter.map(|x| x * x).collect();
    println!("Squares: {:?}", squares);

    // Using our custom iterator
    for n in Counter::new(3) {
        println!("Counter: {}", n);
    }
}
""",
    )

    run_updater(rust_collections_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    iterator_calls = [
        call
        for call in calls
        if "iterator_adaptors" in str(call) or "custom_iterator" in str(call)
    ]
    assert len(iterator_calls) > 0, "Iterator functions should be detected"


def test_other_collections(
    rust_collections_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test other collection types like HashSet, BTreeMap, etc."""
    test_file = rust_collections_project / "other_collections.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::collections::{HashSet, BTreeMap, BTreeSet, VecDeque, LinkedList};

fn hashset_operations() {
    let mut books = HashSet::new();

    books.insert("A Dance With Dragons".to_string());
    books.insert("To Kill a Mockingbird".to_string());
    books.insert("The Odyssey".to_string());
    books.insert("The Great Gatsby".to_string());

    if !books.contains("The Winds of Winter") {
        println!("We have {} books, but The Winds of Winter ain't one.", books.len());
    }

    books.remove("The Odyssey");

    // Set operations
    let mut set1: HashSet<i32> = [1, 2, 3, 4, 5].iter().cloned().collect();
    let mut set2: HashSet<i32> = [4, 5, 6, 7, 8].iter().cloned().collect();

    let intersection: HashSet<i32> = set1.intersection(&set2).cloned().collect();
    let union: HashSet<i32> = set1.union(&set2).cloned().collect();
    let difference: HashSet<i32> = set1.difference(&set2).cloned().collect();

    println!("Intersection: {:?}", intersection);
    println!("Union: {:?}", union);
    println!("Difference: {:?}", difference);
}

fn btreemap_operations() {
    let mut map = BTreeMap::new();

    map.insert("c", 3);
    map.insert("a", 1);
    map.insert("b", 2);
    map.insert("d", 4);

    // BTreeMap keeps keys sorted
    for (key, value) in &map {
        println!("{}: {}", key, value);
    }

    // Range operations
    let partial: BTreeMap<&str, i32> = map.range("b".."d").map(|(k, v)| (*k, *v)).collect();
    println!("Range b to d: {:?}", partial);

    // Split operations
    let mut map2 = map.split_off("c");
    println!("Original after split: {:?}", map);
    println!("Split off part: {:?}", map2);
}

fn btreeset_operations() {
    let mut set = BTreeSet::new();

    set.insert(3);
    set.insert(1);
    set.insert(4);
    set.insert(1); // Duplicate, won't be added
    set.insert(5);

    // BTreeSet keeps elements sorted
    for value in &set {
        println!("{}", value);
    }

    // Range operations
    let range: Vec<i32> = set.range(2..5).cloned().collect();
    println!("Range 2 to 5: {:?}", range);

    // First and last
    println!("First: {:?}", set.first());
    println!("Last: {:?}", set.last());
}

fn vecdeque_operations() {
    let mut deque = VecDeque::new();

    // Add to both ends
    deque.push_back(1);
    deque.push_back(2);
    deque.push_front(0);
    deque.push_front(-1);

    println!("Deque: {:?}", deque);

    // Remove from both ends
    let front = deque.pop_front();
    let back = deque.pop_back();

    println!("Removed front: {:?}, back: {:?}", front, back);
    println!("Remaining: {:?}", deque);

    // Use as a sliding window
    let data = vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
    let window_size = 3;
    let mut window = VecDeque::with_capacity(window_size);

    for &item in &data {
        if window.len() == window_size {
            window.pop_front();
        }
        window.push_back(item);

        if window.len() == window_size {
            println!("Window: {:?}", window);
        }
    }
}

fn linkedlist_operations() {
    let mut list = LinkedList::new();

    list.push_back(1);
    list.push_back(2);
    list.push_back(3);
    list.push_front(0);

    println!("List: {:?}", list);

    // Split the list
    let mut second_list = list.split_off(2);
    println!("First part: {:?}", list);
    println!("Second part: {:?}", second_list);

    // Append one list to another
    list.append(&mut second_list);
    println!("After append: {:?}", list);
    println!("Second list after append: {:?}", second_list);
}

fn collection_conversion() {
    // Vector to other collections
    let vec = vec![1, 2, 3, 2, 4, 5, 4];

    let set: HashSet<i32> = vec.iter().cloned().collect();
    let btree_set: BTreeSet<i32> = vec.iter().cloned().collect();
    let deque: VecDeque<i32> = vec.iter().cloned().collect();

    println!("Original vec: {:?}", vec);
    println!("As HashSet: {:?}", set);
    println!("As BTreeSet: {:?}", btree_set);
    println!("As VecDeque: {:?}", deque);

    // Back to vector
    let back_to_vec: Vec<i32> = set.iter().cloned().collect();
    println!("Back to vec from set: {:?}", back_to_vec);
}

fn collection_performance_characteristics() {
    use std::time::Instant;

    // Compare insertion performance
    let data: Vec<i32> = (0..10000).collect();

    // Vector insertion at end
    let start = Instant::now();
    let mut vec = Vec::new();
    for &item in &data {
        vec.push(item);
    }
    let vec_time = start.elapsed();

    // HashSet insertion
    let start = Instant::now();
    let mut set = HashSet::new();
    for &item in &data {
        set.insert(item);
    }
    let set_time = start.elapsed();

    // BTreeSet insertion
    let start = Instant::now();
    let mut btree_set = BTreeSet::new();
    for &item in &data {
        btree_set.insert(item);
    }
    let btree_time = start.elapsed();

    println!("Vector insertion time: {:?}", vec_time);
    println!("HashSet insertion time: {:?}", set_time);
    println!("BTreeSet insertion time: {:?}", btree_time);
}
""",
    )

    run_updater(rust_collections_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    collections_calls = [
        call
        for call in calls
        if "hashset_operations" in str(call) or "btreemap_operations" in str(call)
    ]
    assert len(collections_calls) > 0, "Other collections functions should be detected"


def test_functional_programming(
    rust_collections_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test functional programming patterns with collections."""
    test_file = rust_collections_project / "functional.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
fn higher_order_functions() {
    let numbers = vec![1, 2, 3, 4, 5];

    // Function that takes a closure
    fn apply_operation<F>(vec: &Vec<i32>, op: F) -> Vec<i32>
    where
        F: Fn(i32) -> i32,
    {
        vec.iter().map(|&x| op(x)).collect()
    }

    let doubled = apply_operation(&numbers, |x| x * 2);
    let squared = apply_operation(&numbers, |x| x * x);

    println!("Doubled: {:?}", doubled);
    println!("Squared: {:?}", squared);
}

fn closure_capture() {
    let multiplier = 10;
    let factor = 2;

    let numbers = vec![1, 2, 3, 4, 5];

    // Closure capturing by reference
    let scaled: Vec<i32> = numbers.iter().map(|&x| x * multiplier).collect();

    // Closure capturing by move
    let processed: Vec<i32> = numbers
        .into_iter()
        .map(move |x| x * factor)
        .collect();

    println!("Scaled: {:?}", scaled);
    println!("Processed: {:?}", processed);
}

fn function_composition() {
    let numbers = vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10];

    // Compose operations
    let result: Vec<i32> = numbers
        .iter()
        .filter(|&&x| x % 2 == 0)  // Keep even numbers
        .map(|&x| x * x)           // Square them
        .filter(|&x| x > 10)       // Keep squares > 10
        .collect();

    println!("Composed operations result: {:?}", result);

    // Alternative using function composition
    let is_even = |x: &i32| *x % 2 == 0;
    let square = |x: i32| x * x;
    let greater_than_10 = |x: &i32| *x > 10;

    let result2: Vec<i32> = numbers
        .iter()
        .filter(is_even)
        .map(|&x| square(x))
        .filter(greater_than_10)
        .collect();

    println!("Function composition result: {:?}", result2);
}

fn monadic_operations() {
    // Option monad-like operations
    let maybe_numbers = vec![Some(1), None, Some(3), Some(4), None];

    // Flatten Options
    let numbers: Vec<i32> = maybe_numbers.into_iter().flatten().collect();
    println!("Flattened: {:?}", numbers);

    // Chain operations on Options
    let result = Some(5)
        .and_then(|x| if x > 0 { Some(x * 2) } else { None })
        .and_then(|x| if x < 20 { Some(x + 1) } else { None })
        .map(|x| x.to_string());

    println!("Chained operations: {:?}", result);

    // Result monad-like operations
    let parse_results: Vec<Result<i32, std::num::ParseIntError>> = vec![
        "1".parse(),
        "not_a_number".parse(),
        "3".parse(),
    ];

    let successful: Vec<i32> = parse_results.into_iter().filter_map(Result::ok).collect();
    println!("Successful parses: {:?}", successful);
}

fn lazy_evaluation() {
    let large_range = 0..1_000_000;

    // Iterator is lazy - no computation yet
    let processed = large_range
        .filter(|&x| x % 1000 == 0)
        .map(|x| x * x)
        .take(5);

    // Only now do we actually compute
    let results: Vec<i32> = processed.collect();
    println!("Lazy evaluation results: {:?}", results);

    // Infinite iterator (conceptually)
    let fibonacci = std::iter::successors(Some((0, 1)), |(a, b)| Some((*b, a + b)))
        .map(|(a, _)| a);

    let first_10_fib: Vec<i32> = fibonacci.take(10).collect();
    println!("First 10 Fibonacci: {:?}", first_10_fib);
}

fn currying_partial_application() {
    // Simulated currying
    fn add_curry(x: i32) -> impl Fn(i32) -> i32 {
        move |y| x + y
    }

    let add_5 = add_curry(5);
    let numbers = vec![1, 2, 3, 4, 5];

    let incremented: Vec<i32> = numbers.iter().map(|&x| add_5(x)).collect();
    println!("Incremented by 5: {:?}", incremented);

    // Partial application with closures
    let multiply_by = |factor: i32| move |x: i32| x * factor;
    let double = multiply_by(2);
    let triple = multiply_by(3);

    let doubled: Vec<i32> = numbers.iter().map(|&x| double(x)).collect();
    let tripled: Vec<i32> = numbers.iter().map(|&x| triple(x)).collect();

    println!("Doubled: {:?}", doubled);
    println!("Tripled: {:?}", tripled);
}

fn functor_like_operations() {
    // Option as a functor
    let maybe_value = Some(42);
    let result = maybe_value
        .map(|x| x * 2)
        .map(|x| x + 10)
        .map(|x| x.to_string());

    println!("Option functor: {:?}", result);

    // Vector as a functor
    let numbers = vec![1, 2, 3, 4, 5];
    let transformed: Vec<String> = numbers
        .iter()
        .map(|&x| x * 2)
        .map(|x| x + 1)
        .map(|x| format!("Number: {}", x))
        .collect();

    println!("Vector functor: {:?}", transformed);

    // Result as a functor
    let parse_result: Result<i32, _> = "42".parse();
    let final_result = parse_result
        .map(|x| x * 2)
        .map(|x| x + 10)
        .map(|x| format!("Final: {}", x));

    println!("Result functor: {:?}", final_result);
}

fn monad_like_operations() {
    // Flatten nested structures
    let nested = vec![vec![1, 2], vec![3, 4, 5], vec![6]];
    let flattened: Vec<i32> = nested.into_iter().flatten().collect();
    println!("Flattened: {:?}", flattened);

    // Bind-like operation with flat_map
    let words = vec!["hello", "world", "rust"];
    let chars: Vec<char> = words
        .iter()
        .flat_map(|&word| word.chars())
        .collect();
    println!("All characters: {:?}", chars);

    // Option bind-like operations
    fn safe_divide(x: f64, y: f64) -> Option<f64> {
        if y != 0.0 { Some(x / y) } else { None }
    }

    fn safe_sqrt(x: f64) -> Option<f64> {
        if x >= 0.0 { Some(x.sqrt()) } else { None }
    }

    let result = Some(16.0)
        .and_then(|x| safe_divide(x, 4.0))
        .and_then(safe_sqrt);

    println!("Monadic computation: {:?}", result);
}
""",
    )

    run_updater(rust_collections_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    functional_calls = [
        call
        for call in calls
        if "higher_order_functions" in str(call) or "monadic_operations" in str(call)
    ]
    assert len(functional_calls) > 0, (
        "Functional programming functions should be detected"
    )
