from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_concurrency_project(temp_repo: Path) -> Path:
    """Create a Rust project with concurrency examples."""
    project_path = temp_repo / "rust_concurrency_test"
    project_path.mkdir()

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""
[package]
name = "rust_concurrency_test"
version = "0.1.0"
edition = "2021"

[dependencies]
tokio = { version = "1", features = ["full"] }
futures = "0.3"
""",
    )

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Concurrency test crate"
    )

    return project_path


def test_basic_threads(
    rust_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic thread creation and management."""
    test_file = rust_concurrency_project / "basic_threads.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::thread;
use std::time::Duration;

fn spawn_thread() {
    let handle = thread::spawn(|| {
        for i in 1..10 {
            println!("hi number {} from the spawned thread!", i);
            thread::sleep(Duration::from_millis(1));
        }
    });

    for i in 1..5 {
        println!("hi number {} from the main thread!", i);
        thread::sleep(Duration::from_millis(1));
    }

    handle.join().unwrap();
}

fn thread_with_move() {
    let v = vec![1, 2, 3];

    let handle = thread::spawn(move || {
        println!("Here's a vector: {:?}", v);
    });

    handle.join().unwrap();
}

fn scoped_threads() {
    let mut a = vec![1, 2, 3];
    let mut x = 0;

    thread::scope(|s| {
        s.spawn(|| {
            println!("hello from the first scoped thread");
            dbg!(&a);
        });
        s.spawn(|| {
            println!("hello from the second scoped thread");
            x += a[0] + a[2];
        });
        println!("hello from the main thread");
    });

    assert_eq!(x, a[0] + a[2]);
}
""",
    )

    run_updater(rust_concurrency_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    thread_calls = [
        call
        for call in calls
        if "spawn_thread" in str(call) or "scoped_threads" in str(call)
    ]
    assert len(thread_calls) > 0, "Thread functions should be detected"


def test_message_passing_channels(
    rust_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test message passing with channels."""
    test_file = rust_concurrency_project / "channels.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

fn simple_channel() {
    let (tx, rx) = mpsc::channel();

    thread::spawn(move || {
        let val = String::from("hi");
        tx.send(val).unwrap();
    });

    let received = rx.recv().unwrap();
    println!("Got: {}", received);
}

fn multiple_messages() {
    let (tx, rx) = mpsc::channel();

    thread::spawn(move || {
        let vals = vec![
            String::from("hi"),
            String::from("from"),
            String::from("the"),
            String::from("thread"),
        ];

        for val in vals {
            tx.send(val).unwrap();
            thread::sleep(Duration::from_secs(1));
        }
    });

    for received in rx {
        println!("Got: {}", received);
    }
}

fn multiple_producers() {
    let (tx, rx) = mpsc::channel();

    let tx1 = tx.clone();
    thread::spawn(move || {
        let vals = vec![
            String::from("hi"),
            String::from("from"),
            String::from("the"),
            String::from("thread"),
        ];

        for val in vals {
            tx1.send(val).unwrap();
            thread::sleep(Duration::from_secs(1));
        }
    });

    thread::spawn(move || {
        let vals = vec![
            String::from("more"),
            String::from("messages"),
            String::from("for"),
            String::from("you"),
        ];

        for val in vals {
            tx.send(val).unwrap();
            thread::sleep(Duration::from_secs(1));
        }
    });

    for received in rx {
        println!("Got: {}", received);
    }
}

fn sync_channel() {
    let (tx, rx) = mpsc::sync_channel(1);

    let tx_clone = tx.clone();
    thread::spawn(move || {
        tx_clone.send(1).unwrap();
        tx_clone.send(2).unwrap(); // This will block
    });

    thread::sleep(Duration::from_millis(100));
    println!("Received: {}", rx.recv().unwrap());
    println!("Received: {}", rx.recv().unwrap());
}
""",
    )

    run_updater(rust_concurrency_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    channel_calls = [
        call
        for call in calls
        if "simple_channel" in str(call) or "multiple_producers" in str(call)
    ]
    assert len(channel_calls) > 0, "Channel functions should be detected"


def test_shared_state_mutex(
    rust_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test shared state with Mutex and Arc."""
    test_file = rust_concurrency_project / "shared_state.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::sync::{Arc, Mutex, RwLock};
use std::thread;

fn mutex_example() {
    let m = Mutex::new(5);

    {
        let mut num = m.lock().unwrap();
        *num = 6;
    }

    println!("m = {:?}", m);
}

fn shared_counter() {
    let counter = Arc::new(Mutex::new(0));
    let mut handles = vec![];

    for _ in 0..10 {
        let counter = Arc::clone(&counter);
        let handle = thread::spawn(move || {
            let mut num = counter.lock().unwrap();
            *num += 1;
        });
        handles.push(handle);
    }

    for handle in handles {
        handle.join().unwrap();
    }

    println!("Result: {}", *counter.lock().unwrap());
}

fn rwlock_example() {
    let lock = RwLock::new(5);

    // many reader locks can be held at once
    {
        let r1 = lock.read().unwrap();
        let r2 = lock.read().unwrap();
        assert_eq!(*r1, 5);
        assert_eq!(*r2, 5);
    } // read locks are dropped at this point

    // only one write lock may be held, however
    {
        let mut w = lock.write().unwrap();
        *w += 1;
        assert_eq!(*w, 6);
    } // write lock is dropped here
}

fn shared_data_structure() {
    use std::collections::HashMap;

    let data = Arc::new(Mutex::new(HashMap::new()));
    let mut handles = vec![];

    for i in 0..10 {
        let data = Arc::clone(&data);
        let handle = thread::spawn(move || {
            let mut map = data.lock().unwrap();
            map.insert(i, i * 2);
        });
        handles.push(handle);
    }

    for handle in handles {
        handle.join().unwrap();
    }

    println!("Final map: {:?}", *data.lock().unwrap());
}
""",
    )

    run_updater(rust_concurrency_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    mutex_calls = [
        call
        for call in calls
        if "mutex_example" in str(call) or "shared_counter" in str(call)
    ]
    assert len(mutex_calls) > 0, "Mutex functions should be detected"


def test_async_await_basics(
    rust_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test async/await basic patterns."""
    test_file = rust_concurrency_project / "async_basics.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::future::Future;
use std::pin::Pin;
use std::task::{Context, Poll};

async fn hello_world() {
    println!("Hello, world!");
}

async fn learn_song() -> String {
    "Mary had a little lamb".to_string()
}

async fn sing_song(song: String) {
    println!("Singing: {}", song);
}

async fn dance() {
    println!("Dancing!");
}

async fn learn_and_sing() {
    let song = learn_song().await;
    sing_song(song).await;
}

async fn async_main() {
    let f1 = learn_and_sing();
    let f2 = dance();

    futures::join!(f1, f2);
}

struct TimerFuture {
    shared_state: Arc<Mutex<SharedState>>,
}

struct SharedState {
    completed: bool,
    waker: Option<Waker>,
}

impl Future for TimerFuture {
    type Output = ();

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let mut shared_state = self.shared_state.lock().unwrap();
        if shared_state.completed {
            Poll::Ready(())
        } else {
            shared_state.waker = Some(cx.waker().clone());
            Poll::Pending
        }
    }
}

impl TimerFuture {
    pub fn new(duration: Duration) -> Self {
        let shared_state = Arc::new(Mutex::new(SharedState {
            completed: false,
            waker: None,
        }));

        let thread_shared_state = shared_state.clone();
        thread::spawn(move || {
            thread::sleep(duration);
            let mut shared_state = thread_shared_state.lock().unwrap();
            shared_state.completed = true;
            if let Some(waker) = shared_state.waker.take() {
                waker.wake()
            }
        });

        TimerFuture { shared_state }
    }
}
""",
    )

    run_updater(rust_concurrency_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    async_calls = [
        call
        for call in calls
        if "learn_song" in str(call) or "TimerFuture" in str(call)
    ]
    assert len(async_calls) > 0, "Async functions should be detected"


def test_tokio_async_runtime(
    rust_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Tokio async runtime patterns."""
    test_file = rust_concurrency_project / "tokio_async.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use tokio::time::{sleep, Duration};
use tokio::task;

#[tokio::main]
async fn main() {
    println!("Hello, Tokio!");

    tokio_basics().await;
    spawn_tasks().await;
    parallel_execution().await;
}

async fn tokio_basics() {
    println!("Starting async operation...");
    sleep(Duration::from_millis(100)).await;
    println!("Async operation completed!");
}

async fn spawn_tasks() {
    let handle1 = task::spawn(async {
        sleep(Duration::from_millis(100)).await;
        "Task 1 completed"
    });

    let handle2 = task::spawn(async {
        sleep(Duration::from_millis(200)).await;
        "Task 2 completed"
    });

    let result1 = handle1.await.unwrap();
    let result2 = handle2.await.unwrap();

    println!("{}", result1);
    println!("{}", result2);
}

async fn parallel_execution() {
    let start = std::time::Instant::now();

    let futures = (0..5)
        .map(|i| async move {
            sleep(Duration::from_millis(100)).await;
            i * 2
        })
        .collect::<Vec<_>>();

    let results = futures::future::join_all(futures).await;

    println!("Results: {:?}", results);
    println!("Time taken: {:?}", start.elapsed());
}

async fn select_example() {
    let mut interval = tokio::time::interval(Duration::from_secs(1));
    let sleep_future = sleep(Duration::from_secs(5));

    tokio::select! {
        _ = interval.tick() => println!("Interval tick"),
        _ = sleep_future => println!("Sleep completed"),
    }
}

use tokio::sync::{mpsc, oneshot};

async fn channel_communication() {
    let (tx, mut rx) = mpsc::channel(32);

    let producer = task::spawn(async move {
        for i in 0..10 {
            if tx.send(i).await.is_err() {
                break;
            }
            sleep(Duration::from_millis(10)).await;
        }
    });

    let consumer = task::spawn(async move {
        while let Some(value) = rx.recv().await {
            println!("Received: {}", value);
        }
    });

    let _ = tokio::join!(producer, consumer);
}

async fn oneshot_example() {
    let (tx, rx) = oneshot::channel();

    task::spawn(async move {
        sleep(Duration::from_millis(100)).await;
        let _ = tx.send("Hello from spawned task");
    });

    match rx.await {
        Ok(msg) => println!("Received: {}", msg),
        Err(_) => println!("Sender was dropped"),
    }
}
""",
    )

    run_updater(rust_concurrency_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    tokio_calls = [
        call
        for call in calls
        if "spawn_tasks" in str(call) or "channel_communication" in str(call)
    ]
    assert len(tokio_calls) > 0, "Tokio functions should be detected"


def test_parallel_computing(
    rust_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test parallel computing patterns."""
    test_file = rust_concurrency_project / "parallel_computing.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::thread;
use std::sync::{Arc, Barrier};

fn parallel_map<T, U, F>(data: Vec<T>, f: F) -> Vec<U>
where
    T: Send + 'static,
    U: Send + 'static,
    F: Fn(T) -> U + Send + Sync + 'static,
{
    let f = Arc::new(f);
    let chunk_size = data.len() / 4;
    let mut handles = vec![];
    let mut results = vec![vec![]; 4];

    for (i, chunk) in data.chunks(chunk_size).enumerate() {
        let f = Arc::clone(&f);
        let chunk = chunk.to_vec();

        let handle = thread::spawn(move || {
            chunk.into_iter().map(|x| f(x)).collect::<Vec<_>>()
        });

        handles.push((i, handle));
    }

    for (i, handle) in handles {
        results[i] = handle.join().unwrap();
    }

    results.into_iter().flatten().collect()
}

fn worker_pool() {
    use std::sync::mpsc;

    struct Worker {
        id: usize,
        thread: Option<thread::JoinHandle<()>>,
    }

    impl Worker {
        fn new(id: usize, receiver: Arc<Mutex<mpsc::Receiver<Job>>>) -> Worker {
            let thread = thread::spawn(move || loop {
                let job = receiver.lock().unwrap().recv().unwrap();
                println!("Worker {} got a job; executing.", id);
                job();
            });

            Worker {
                id,
                thread: Some(thread),
            }
        }
    }

    type Job = Box<dyn FnOnce() + Send + 'static>;

    struct ThreadPool {
        workers: Vec<Worker>,
        sender: mpsc::Sender<Job>,
    }

    impl ThreadPool {
        fn new(size: usize) -> ThreadPool {
            assert!(size > 0);

            let (sender, receiver) = mpsc::channel();
            let receiver = Arc::new(Mutex::new(receiver));
            let mut workers = Vec::with_capacity(size);

            for id in 0..size {
                workers.push(Worker::new(id, Arc::clone(&receiver)));
            }

            ThreadPool { workers, sender }
        }

        fn execute<F>(&self, f: F)
        where
            F: FnOnce() + Send + 'static,
        {
            let job = Box::new(f);
            self.sender.send(job).unwrap();
        }
    }
}

fn barrier_synchronization() {
    let n = 5;
    let barrier = Arc::new(Barrier::new(n));
    let mut handles = vec![];

    for i in 0..n {
        let c = Arc::clone(&barrier);
        handles.push(thread::spawn(move || {
            println!("Thread {} is working...", i);
            thread::sleep(std::time::Duration::from_millis(i as u64 * 100));

            println!("Thread {} finished work, waiting for others", i);
            c.wait();

            println!("Thread {} proceeding after barrier", i);
        }));
    }

    for handle in handles {
        handle.join().unwrap();
    }
}
""",
    )

    run_updater(rust_concurrency_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    parallel_calls = [
        call
        for call in calls
        if "parallel_map" in str(call) or "worker_pool" in str(call)
    ]
    assert len(parallel_calls) > 0, "Parallel computing functions should be detected"


def test_atomic_operations(
    rust_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test atomic operations and lock-free programming."""
    test_file = rust_concurrency_project / "atomics.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

fn atomic_counter() {
    let counter = Arc::new(AtomicUsize::new(0));
    let mut handles = vec![];

    for _ in 0..10 {
        let counter = Arc::clone(&counter);
        let handle = thread::spawn(move || {
            for _ in 0..1000 {
                counter.fetch_add(1, Ordering::SeqCst);
            }
        });
        handles.push(handle);
    }

    for handle in handles {
        handle.join().unwrap();
    }

    println!("Counter: {}", counter.load(Ordering::SeqCst));
}

fn atomic_flag() {
    let flag = Arc::new(AtomicBool::new(false));
    let flag_clone = Arc::clone(&flag);

    let handle = thread::spawn(move || {
        thread::sleep(Duration::from_millis(100));
        flag_clone.store(true, Ordering::SeqCst);
    });

    while !flag.load(Ordering::SeqCst) {
        println!("Waiting for flag...");
        thread::sleep(Duration::from_millis(10));
    }

    println!("Flag is set!");
    handle.join().unwrap();
}

fn compare_and_swap() {
    let value = Arc::new(AtomicUsize::new(0));
    let mut handles = vec![];

    for i in 0..10 {
        let value = Arc::clone(&value);
        let handle = thread::spawn(move || {
            loop {
                let current = value.load(Ordering::SeqCst);
                let new_value = current + i;

                match value.compare_exchange_weak(
                    current,
                    new_value,
                    Ordering::SeqCst,
                    Ordering::Relaxed,
                ) {
                    Ok(_) => {
                        println!("Thread {} succeeded: {} -> {}", i, current, new_value);
                        break;
                    }
                    Err(actual) => {
                        println!("Thread {} failed: expected {}, got {}", i, current, actual);
                    }
                }
            }
        });
        handles.push(handle);
    }

    for handle in handles {
        handle.join().unwrap();
    }

    println!("Final value: {}", value.load(Ordering::SeqCst));
}

use std::sync::atomic::{AtomicPtr, AtomicI32};

fn atomic_pointer() {
    let data = Box::new(42);
    let ptr = AtomicPtr::new(Box::into_raw(data));

    let raw_ptr = ptr.load(Ordering::SeqCst);
    unsafe {
        println!("Value: {}", *raw_ptr);
        let _ = Box::from_raw(raw_ptr);
    }
}

fn memory_ordering_examples() {
    let data = AtomicI32::new(0);
    let flag = AtomicBool::new(false);

    // Relaxed ordering
    data.store(42, Ordering::Relaxed);
    let value = data.load(Ordering::Relaxed);

    // Acquire-Release ordering
    data.store(42, Ordering::Release);
    flag.store(true, Ordering::Release);

    if flag.load(Ordering::Acquire) {
        let value = data.load(Ordering::Acquire);
        println!("Got value: {}", value);
    }

    // Sequential consistency
    data.store(42, Ordering::SeqCst);
    let value = data.load(Ordering::SeqCst);
}
""",
    )

    run_updater(rust_concurrency_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    atomic_calls = [
        call
        for call in calls
        if "atomic_counter" in str(call) or "compare_and_swap" in str(call)
    ]
    assert len(atomic_calls) > 0, "Atomic functions should be detected"
