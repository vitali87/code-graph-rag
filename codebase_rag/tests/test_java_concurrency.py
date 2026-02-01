from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, run_updater


@pytest.fixture
def java_concurrency_project(temp_repo: Path) -> Path:
    """Create a Java project for testing concurrency features."""
    project_path = temp_repo / "java_concurrency_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_synchronized_methods_blocks(
    java_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java synchronized methods and blocks parsing."""
    test_file = (
        java_concurrency_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "SynchronizedExample.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

public class SynchronizedExample {
    private int counter = 0;
    private final Object lock = new Object();
    private static int staticCounter = 0;

    // Synchronized instance method
    public synchronized void incrementCounter() {
        counter++;
        notifyAll(); // Can use wait/notify in synchronized context
    }

    // Synchronized static method
    public static synchronized void incrementStaticCounter() {
        staticCounter++;
    }

    public synchronized int getCounter() {
        return counter;
    }

    public static synchronized int getStaticCounter() {
        return staticCounter;
    }

    // Synchronized block on this
    public void incrementWithBlock() {
        synchronized (this) {
            counter++;
            System.out.println("Counter: " + counter);
        }
    }

    // Synchronized block on custom object
    public void incrementWithCustomLock() {
        synchronized (lock) {
            counter++;
            try {
                lock.wait(1000); // Wait with timeout
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    // Synchronized block on class
    public void incrementWithClassLock() {
        synchronized (SynchronizedExample.class) {
            staticCounter++;
        }
    }

    // Multiple synchronized blocks
    public void multipleBlocks() {
        synchronized (this) {
            counter++;
        }

        synchronized (lock) {
            counter *= 2;
        }

        synchronized (SynchronizedExample.class) {
            staticCounter += counter;
        }
    }

    // Nested synchronized blocks
    public void nestedSynchronized() {
        synchronized (this) {
            counter++;
            synchronized (lock) {
                counter += 10;
                System.out.println("Nested synchronized: " + counter);
            }
        }
    }

    // Producer-Consumer pattern with wait/notify
    private volatile boolean produced = false;
    private String data;

    public synchronized void produce(String value) throws InterruptedException {
        while (produced) {
            wait(); // Wait until consumed
        }
        data = value;
        produced = true;
        notifyAll(); // Notify consumers
    }

    public synchronized String consume() throws InterruptedException {
        while (!produced) {
            wait(); // Wait until produced
        }
        String result = data;
        produced = false;
        notifyAll(); // Notify producers
        return result;
    }
}
""",
    )

    run_updater(java_concurrency_project, mock_ingestor, skip_if_missing="java")

    project_name = java_concurrency_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.SynchronizedExample.SynchronizedExample",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_volatile_fields(
    java_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java volatile field parsing."""
    test_file = (
        java_concurrency_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "VolatileExample.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

public class VolatileExample {
    private volatile boolean running = true;
    private volatile int counter = 0;
    private volatile String status = "IDLE";
    private volatile double progress = 0.0;

    // Static volatile fields
    private static volatile boolean systemReady = false;
    private static volatile long lastUpdateTime = 0L;

    // Volatile array reference (array itself is not volatile)
    private volatile int[] data;
    private volatile String[] messages;

    // Volatile object references
    private volatile Thread workerThread;
    private volatile java.util.concurrent.locks.Lock lock;

    public void startWorker() {
        running = true;
        status = "STARTING";

        workerThread = new Thread(() -> {
            while (running) {
                counter++;
                progress = counter / 100.0;

                if (counter % 10 == 0) {
                    status = "WORKING - " + counter;
                }

                try {
                    Thread.sleep(100);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
            status = "STOPPED";
        });

        workerThread.start();
    }

    public void stopWorker() {
        running = false; // Volatile write - visible to worker thread
        status = "STOPPING";

        if (workerThread != null) {
            try {
                workerThread.join(5000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    // Getters for volatile fields
    public boolean isRunning() {
        return running; // Volatile read
    }

    public int getCounter() {
        return counter; // Volatile read
    }

    public String getStatus() {
        return status; // Volatile read
    }

    public double getProgress() {
        return progress; // Volatile read
    }

    // Static methods for static volatile fields
    public static void setSystemReady() {
        systemReady = true;
        lastUpdateTime = System.currentTimeMillis();
    }

    public static boolean isSystemReady() {
        return systemReady;
    }

    public static long getLastUpdateTime() {
        return lastUpdateTime;
    }

    // Double-checked locking pattern with volatile
    private static volatile VolatileExample instance;

    public static VolatileExample getInstance() {
        if (instance == null) {
            synchronized (VolatileExample.class) {
                if (instance == null) {
                    instance = new VolatileExample();
                }
            }
        }
        return instance;
    }

    // Volatile with arrays
    public void updateData(int[] newData) {
        data = newData.clone(); // Volatile write of array reference
    }

    public int[] getData() {
        return data; // Volatile read of array reference
    }

    public void addMessage(String message) {
        String[] current = messages; // Volatile read
        if (current == null) {
            messages = new String[]{message}; // Volatile write
        } else {
            String[] newMessages = new String[current.length + 1];
            System.arraycopy(current, 0, newMessages, 0, current.length);
            newMessages[current.length] = message;
            messages = newMessages; // Volatile write
        }
    }
}
""",
    )

    run_updater(java_concurrency_project, mock_ingestor, skip_if_missing="java")

    project_name = java_concurrency_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.VolatileExample.VolatileExample",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_concurrent_collections(
    java_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java concurrent collections parsing."""
    test_file = (
        java_concurrency_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "ConcurrentCollections.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import java.util.*;

public class ConcurrentCollections {

    // ConcurrentHashMap
    private final ConcurrentMap<String, Integer> scores = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, List<String>> userGroups = new ConcurrentHashMap<>();

    // ConcurrentLinkedQueue
    private final Queue<String> messageQueue = new ConcurrentLinkedQueue<>();

    // BlockingQueue implementations
    private final BlockingQueue<Task> taskQueue = new ArrayBlockingQueue<>(100);
    private final BlockingQueue<String> priorityQueue = new PriorityBlockingQueue<>();
    private final BlockingQueue<Object> linkedQueue = new LinkedBlockingQueue<>();
    private final BlockingQueue<String> synchronousQueue = new SynchronousQueue<>();

    // CopyOnWriteArrayList and Set
    private final List<String> observers = new CopyOnWriteArrayList<>();
    private final Set<String> activeUsers = new CopyOnWriteArraySet<>();

    // Atomic variables
    private final AtomicInteger counter = new AtomicInteger(0);
    private final AtomicLong timestamp = new AtomicLong();
    private final AtomicBoolean initialized = new AtomicBoolean(false);
    private final AtomicReference<String> status = new AtomicReference<>("IDLE");

    // Concurrent operations with ConcurrentHashMap
    public void updateScore(String player, int points) {
        scores.compute(player, (key, currentScore) -> {
            return (currentScore == null) ? points : currentScore + points;
        });
    }

    public void addUserToGroup(String user, String group) {
        userGroups.computeIfAbsent(group, k -> new CopyOnWriteArrayList<>()).add(user);
    }

    public void removeUserFromGroup(String user, String group) {
        userGroups.computeIfPresent(group, (key, users) -> {
            users.remove(user);
            return users.isEmpty() ? null : users;
        });
    }

    // Producer-Consumer with BlockingQueue
    public void submitTask(Task task) throws InterruptedException {
        taskQueue.put(task); // Blocks if queue is full
    }

    public Task takeTask() throws InterruptedException {
        return taskQueue.take(); // Blocks if queue is empty
    }

    public boolean submitTaskNonBlocking(Task task) {
        return taskQueue.offer(task); // Returns false if queue is full
    }

    public Task pollTask(long timeout, TimeUnit unit) throws InterruptedException {
        return taskQueue.poll(timeout, unit); // Waits up to timeout
    }

    // Message processing
    public void sendMessage(String message) {
        messageQueue.offer(message);
        notifyObservers("Message sent: " + message);
    }

    public void processMessages() {
        String message;
        while ((message = messageQueue.poll()) != null) {
            System.out.println("Processing: " + message);
            counter.incrementAndGet();
        }
    }

    // Observer pattern with CopyOnWriteArrayList
    public void addObserver(String observer) {
        observers.add(observer);
    }

    public void removeObserver(String observer) {
        observers.remove(observer);
    }

    public void notifyObservers(String event) {
        // Safe iteration during concurrent modifications
        for (String observer : observers) {
            System.out.println("Notifying " + observer + ": " + event);
        }
    }

    // Active users management
    public void userLogin(String user) {
        activeUsers.add(user);
        timestamp.set(System.currentTimeMillis());
    }

    public void userLogout(String user) {
        activeUsers.remove(user);
        timestamp.set(System.currentTimeMillis());
    }

    public Set<String> getActiveUsers() {
        return new HashSet<>(activeUsers); // Safe snapshot
    }

    // Atomic operations
    public int getNextSequenceNumber() {
        return counter.incrementAndGet();
    }

    public boolean initializeIfNotDone() {
        return initialized.compareAndSet(false, true);
    }

    public void updateStatus(String newStatus) {
        String oldStatus = status.getAndSet(newStatus);
        System.out.println("Status changed from " + oldStatus + " to " + newStatus);
    }

    public boolean changeStatusIfCurrent(String expectedStatus, String newStatus) {
        return status.compareAndSet(expectedStatus, newStatus);
    }

    // Batch operations
    public void processBatchTasks(List<Task> tasks) throws InterruptedException {
        for (Task task : tasks) {
            taskQueue.put(task);
        }
    }

    public List<Task> drainTasks() {
        List<Task> drained = new ArrayList<>();
        taskQueue.drainTo(drained);
        return drained;
    }

    public List<Task> drainTasks(int maxElements) {
        List<Task> drained = new ArrayList<>();
        taskQueue.drainTo(drained, maxElements);
        return drained;
    }

    // Statistics
    public Map<String, Object> getStatistics() {
        Map<String, Object> stats = new ConcurrentHashMap<>();
        stats.put("totalScores", scores.size());
        stats.put("totalGroups", userGroups.size());
        stats.put("messagesQueued", messageQueue.size());
        stats.put("tasksQueued", taskQueue.size());
        stats.put("observerCount", observers.size());
        stats.put("activeUserCount", activeUsers.size());
        stats.put("counter", counter.get());
        stats.put("timestamp", timestamp.get());
        stats.put("initialized", initialized.get());
        stats.put("status", status.get());
        return stats;
    }
}

class Task {
    private final String id;
    private final String description;

    public Task(String id, String description) {
        this.id = id;
        this.description = description;
    }

    public String getId() { return id; }
    public String getDescription() { return description; }
}
""",
    )

    run_updater(java_concurrency_project, mock_ingestor, skip_if_missing="java")

    project_name = java_concurrency_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.ConcurrentCollections.ConcurrentCollections",
        f"{project_name}.src.main.java.com.example.ConcurrentCollections.Task",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_executor_service_patterns(
    java_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java ExecutorService and thread pool parsing."""
    test_file = (
        java_concurrency_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "ExecutorExample.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.*;

public class ExecutorExample {

    // Different types of executor services
    private final ExecutorService fixedThreadPool = Executors.newFixedThreadPool(4);
    private final ExecutorService cachedThreadPool = Executors.newCachedThreadPool();
    private final ScheduledExecutorService scheduledExecutor = Executors.newScheduledThreadPool(2);
    private final ExecutorService singleThreadExecutor = Executors.newSingleThreadExecutor();

    // Custom thread pool
    private final ThreadPoolExecutor customExecutor = new ThreadPoolExecutor(
        2, // core pool size
        4, // maximum pool size
        60L, TimeUnit.SECONDS, // keep alive time
        new LinkedBlockingQueue<>(100), // work queue
        new ThreadFactory() {
            private final AtomicInteger threadNumber = new AtomicInteger(1);

            @Override
            public Thread newThread(Runnable r) {
                Thread t = new Thread(r, "CustomThread-" + threadNumber.getAndIncrement());
                t.setDaemon(false);
                return t;
            }
        },
        new ThreadPoolExecutor.CallerRunsPolicy() // rejection policy
    );

    // CompletionService for managing completed tasks
    private final CompletionService<String> completionService =
        new ExecutorCompletionService<>(fixedThreadPool);

    // Submit tasks and get futures
    public Future<String> submitTask(String taskName) {
        return fixedThreadPool.submit(() -> {
            Thread.sleep(1000); // Simulate work
            return "Completed: " + taskName;
        });
    }

    public List<Future<String>> submitMultipleTasks(List<String> taskNames) {
        List<Future<String>> futures = new ArrayList<>();
        for (String taskName : taskNames) {
            Future<String> future = fixedThreadPool.submit(() -> {
                Thread.sleep(500);
                return "Batch completed: " + taskName;
            });
            futures.add(future);
        }
        return futures;
    }

    // Execute tasks without return values
    public void executeRunnableTasks() {
        for (int i = 0; i < 10; i++) {
            final int taskId = i;
            fixedThreadPool.execute(() -> {
                System.out.println("Executing task " + taskId);
                try {
                    Thread.sleep(100);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            });
        }
    }

    // Scheduled tasks
    public ScheduledFuture<?> schedulePeriodicTask() {
        return scheduledExecutor.scheduleAtFixedRate(
            () -> System.out.println("Periodic task at " + System.currentTimeMillis()),
            0, // initial delay
            5, // period
            TimeUnit.SECONDS
        );
    }

    public ScheduledFuture<String> scheduleDelayedTask() {
        return scheduledExecutor.schedule(
            () -> "Delayed task completed at " + System.currentTimeMillis(),
            10, // delay
            TimeUnit.SECONDS
        );
    }

    // Bulk operations
    public List<String> invokeAllTasks(List<String> taskNames) throws InterruptedException {
        List<Callable<String>> callables = new ArrayList<>();
        for (String taskName : taskNames) {
            callables.add(() -> {
                Thread.sleep(200);
                return "Invoke all: " + taskName;
            });
        }

        List<Future<String>> futures = fixedThreadPool.invokeAll(callables);
        List<String> results = new ArrayList<>();

        for (Future<String> future : futures) {
            try {
                results.add(future.get());
            } catch (ExecutionException e) {
                results.add("Error: " + e.getCause().getMessage());
            }
        }

        return results;
    }

    public String invokeAnyTask(List<String> taskNames) throws InterruptedException, ExecutionException {
        List<Callable<String>> callables = new ArrayList<>();
        for (String taskName : taskNames) {
            callables.add(() -> {
                Thread.sleep((long) (Math.random() * 1000));
                return "First completed: " + taskName;
            });
        }

        return fixedThreadPool.invokeAny(callables);
    }

    // CompletionService usage
    public void submitToCompletionService(List<String> taskNames) {
        for (String taskName : taskNames) {
            completionService.submit(() -> {
                Thread.sleep((long) (Math.random() * 1000));
                return "Completion service: " + taskName;
            });
        }
    }

    public List<String> pollCompletedTasks(int maxTasks) throws InterruptedException {
        List<String> results = new ArrayList<>();
        for (int i = 0; i < maxTasks; i++) {
            Future<String> future = completionService.poll(1, TimeUnit.SECONDS);
            if (future != null) {
                try {
                    results.add(future.get());
                } catch (ExecutionException e) {
                    results.add("Error: " + e.getCause().getMessage());
                }
            } else {
                break; // No more completed tasks
            }
        }
        return results;
    }

    // Graceful shutdown
    public void shutdown() {
        shutdownExecutor(fixedThreadPool, "FixedThreadPool");
        shutdownExecutor(cachedThreadPool, "CachedThreadPool");
        shutdownExecutor(scheduledExecutor, "ScheduledExecutor");
        shutdownExecutor(singleThreadExecutor, "SingleThreadExecutor");
        shutdownExecutor(customExecutor, "CustomExecutor");
    }

    private void shutdownExecutor(ExecutorService executor, String name) {
        executor.shutdown();
        try {
            if (!executor.awaitTermination(60, TimeUnit.SECONDS)) {
                System.out.println(name + " did not terminate gracefully, forcing shutdown");
                executor.shutdownNow();
                if (!executor.awaitTermination(60, TimeUnit.SECONDS)) {
                    System.err.println(name + " did not terminate");
                }
            }
        } catch (InterruptedException e) {
            executor.shutdownNow();
            Thread.currentThread().interrupt();
        }
    }

    // Error handling
    public void handleTaskExceptions() {
        Future<String> future = fixedThreadPool.submit(() -> {
            if (Math.random() < 0.5) {
                throw new RuntimeException("Random task failure");
            }
            return "Task succeeded";
        });

        try {
            String result = future.get(5, TimeUnit.SECONDS);
            System.out.println("Result: " + result);
        } catch (TimeoutException e) {
            System.err.println("Task timed out");
            future.cancel(true);
        } catch (ExecutionException e) {
            System.err.println("Task failed: " + e.getCause().getMessage());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            System.err.println("Task was interrupted");
        }
    }
}
""",
    )

    run_updater(java_concurrency_project, mock_ingestor, skip_if_missing="java")

    project_name = java_concurrency_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.ExecutorExample.ExecutorExample",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_completable_future_patterns(
    java_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java CompletableFuture parsing."""
    test_file = (
        java_concurrency_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "CompletableFutureExample.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.concurrent.*;
import java.util.*;
import java.util.function.*;

public class CompletableFutureExample {

    private final ExecutorService executor = Executors.newFixedThreadPool(4);

    // Basic CompletableFuture creation
    public CompletableFuture<String> createSimpleFuture() {
        return CompletableFuture.supplyAsync(() -> {
            try {
                Thread.sleep(1000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            return "Hello, World!";
        });
    }

    public CompletableFuture<Void> createVoidFuture() {
        return CompletableFuture.runAsync(() -> {
            System.out.println("Running asynchronously");
        });
    }

    // Chaining operations
    public CompletableFuture<String> chainOperations(String input) {
        return CompletableFuture
            .supplyAsync(() -> input.toLowerCase())
            .thenApply(s -> s.trim())
            .thenApply(s -> s.toUpperCase())
            .thenApply(s -> "Result: " + s);
    }

    public CompletableFuture<Void> chainWithConsumer(String input) {
        return CompletableFuture
            .supplyAsync(() -> input.length())
            .thenAccept(length -> System.out.println("Length: " + length))
            .thenRun(() -> System.out.println("Processing complete"));
    }

    // Composing futures
    public CompletableFuture<String> composeFutures(String input) {
        return CompletableFuture
            .supplyAsync(() -> input)
            .thenCompose(s -> CompletableFuture.supplyAsync(() -> s.toUpperCase()))
            .thenCompose(s -> CompletableFuture.supplyAsync(() -> "Composed: " + s));
    }

    // Combining futures
    public CompletableFuture<String> combineFutures(String input1, String input2) {
        CompletableFuture<String> future1 = CompletableFuture.supplyAsync(() -> input1.toUpperCase());
        CompletableFuture<String> future2 = CompletableFuture.supplyAsync(() -> input2.toLowerCase());

        return future1.thenCombine(future2, (s1, s2) -> s1 + " + " + s2);
    }

    // Exception handling
    public CompletableFuture<String> handleExceptions(boolean shouldFail) {
        return CompletableFuture
            .supplyAsync(() -> {
                if (shouldFail) {
                    throw new RuntimeException("Intentional failure");
                }
                return "Success";
            })
            .exceptionally(throwable -> "Failed: " + throwable.getMessage())
            .handle((result, throwable) -> {
                if (throwable != null) {
                    return "Handled: " + throwable.getMessage();
                }
                return "Completed: " + result;
            });
    }

    public CompletableFuture<String> handleWithCompletion(boolean shouldFail) {
        return CompletableFuture
            .supplyAsync(() -> {
                if (shouldFail) {
                    throw new RuntimeException("Failure");
                }
                return "Success";
            })
            .whenComplete((result, throwable) -> {
                if (throwable != null) {
                    System.err.println("Error occurred: " + throwable.getMessage());
                } else {
                    System.out.println("Success: " + result);
                }
            });
    }

    // Multiple futures operations
    public CompletableFuture<Void> waitForAll(List<String> inputs) {
        List<CompletableFuture<String>> futures = inputs.stream()
            .map(input -> CompletableFuture.supplyAsync(() -> {
                try {
                    Thread.sleep(500);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                return input.toUpperCase();
            }))
            .toList();

        CompletableFuture<Void> allOf = CompletableFuture.allOf(
            futures.toArray(new CompletableFuture[0])
        );

        return allOf.thenRun(() -> {
            System.out.println("All futures completed");
            futures.forEach(future -> {
                try {
                    System.out.println("Result: " + future.get());
                } catch (Exception e) {
                    System.err.println("Error: " + e.getMessage());
                }
            });
        });
    }

    public CompletableFuture<Object> waitForAny(List<String> inputs) {
        List<CompletableFuture<String>> futures = inputs.stream()
            .map(input -> CompletableFuture.supplyAsync(() -> {
                try {
                    Thread.sleep((long) (Math.random() * 1000));
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                return "Completed: " + input;
            }))
            .toList();

        return CompletableFuture.anyOf(futures.toArray(new CompletableFuture[0]));
    }

    // Custom executor
    public CompletableFuture<String> useCustomExecutor(String input) {
        return CompletableFuture
            .supplyAsync(() -> input.toLowerCase(), executor)
            .thenApplyAsync(s -> s.toUpperCase(), executor)
            .thenApplyAsync(s -> "Custom: " + s, executor);
    }

    // Timeout handling
    public CompletableFuture<String> withTimeout(String input, long timeoutMillis) {
        CompletableFuture<String> future = CompletableFuture.supplyAsync(() -> {
            try {
                Thread.sleep(timeoutMillis * 2); // Simulate slow operation
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            return "Completed: " + input;
        });

        return future.orTimeout(timeoutMillis, TimeUnit.MILLISECONDS)
            .exceptionally(throwable -> "Timeout occurred");
    }

    // Delayed execution
    public CompletableFuture<String> delayedExecution(String input, long delayMillis) {
        return CompletableFuture
            .delayedExecutor(delayMillis, TimeUnit.MILLISECONDS)
            .execute(() -> System.out.println("Delayed start"))
            .thenSupplyAsync(() -> "Delayed: " + input);
    }

    // Manual completion
    public CompletableFuture<String> manualCompletion() {
        CompletableFuture<String> future = new CompletableFuture<>();

        // Simulate some background process that completes the future
        executor.submit(() -> {
            try {
                Thread.sleep(1000);
                if (Math.random() < 0.8) {
                    future.complete("Manually completed");
                } else {
                    future.completeExceptionally(new RuntimeException("Manual failure"));
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                future.completeExceptionally(e);
            }
        });

        return future;
    }

    // Clean up
    public void shutdown() {
        executor.shutdown();
        try {
            if (!executor.awaitTermination(60, TimeUnit.SECONDS)) {
                executor.shutdownNow();
            }
        } catch (InterruptedException e) {
            executor.shutdownNow();
            Thread.currentThread().interrupt();
        }
    }
}
""",
    )

    run_updater(java_concurrency_project, mock_ingestor, skip_if_missing="java")

    project_name = java_concurrency_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.CompletableFutureExample.CompletableFutureExample",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_locks_and_conditions(
    java_concurrency_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java locks and conditions parsing."""
    test_file = (
        java_concurrency_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "LocksExample.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.concurrent.locks.*;
import java.util.concurrent.*;
import java.util.*;

public class LocksExample {

    // Different types of locks
    private final ReentrantLock reentrantLock = new ReentrantLock();
    private final ReentrantLock fairLock = new ReentrantLock(true);
    private final ReadWriteLock readWriteLock = new ReentrantReadWriteLock();
    private final Lock readLock = readWriteLock.readLock();
    private final Lock writeLock = readWriteLock.writeLock();
    private final StampedLock stampedLock = new StampedLock();

    // Condition variables
    private final Condition notEmpty = reentrantLock.newCondition();
    private final Condition notFull = reentrantLock.newCondition();

    // Shared data
    private final List<String> buffer = new ArrayList<>();
    private final int capacity = 10;
    private volatile boolean running = true;

    // Basic lock usage
    public void basicLockUsage() {
        reentrantLock.lock();
        try {
            // Critical section
            System.out.println("Inside critical section");
            buffer.add("item");
        } finally {
            reentrantLock.unlock();
        }
    }

    // Try lock with timeout
    public boolean tryLockWithTimeout(long timeoutMillis) {
        try {
            if (reentrantLock.tryLock(timeoutMillis, TimeUnit.MILLISECONDS)) {
                try {
                    // Critical section
                    buffer.clear();
                    return true;
                } finally {
                    reentrantLock.unlock();
                }
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        return false;
    }

    // Read-write lock usage
    public String readData() {
        readLock.lock();
        try {
            // Multiple readers can access simultaneously
            return buffer.isEmpty() ? null : buffer.get(0);
        } finally {
            readLock.unlock();
        }
    }

    public void writeData(String item) {
        writeLock.lock();
        try {
            // Only one writer can access
            buffer.add(item);
        } finally {
            writeLock.unlock();
        }
    }

    // Producer-Consumer with conditions
    public void produce(String item) throws InterruptedException {
        reentrantLock.lock();
        try {
            while (buffer.size() >= capacity) {
                notFull.await(); // Wait until buffer has space
            }
            buffer.add(item);
            notEmpty.signalAll(); // Notify waiting consumers
        } finally {
            reentrantLock.unlock();
        }
    }

    public String consume() throws InterruptedException {
        reentrantLock.lock();
        try {
            while (buffer.isEmpty()) {
                notEmpty.await(); // Wait until buffer has items
            }
            String item = buffer.remove(0);
            notFull.signalAll(); // Notify waiting producers
            return item;
        } finally {
            reentrantLock.unlock();
        }
    }

    // StampedLock optimistic reading
    private volatile double x, y;

    public double calculateDistance() {
        long stamp = stampedLock.tryOptimisticRead();
        double currentX = x;
        double currentY = y;

        if (!stampedLock.validate(stamp)) {
            // Data might have changed, acquire read lock
            stamp = stampedLock.readLock();
            try {
                currentX = x;
                currentY = y;
            } finally {
                stampedLock.unlockRead(stamp);
            }
        }

        return Math.sqrt(currentX * currentX + currentY * currentY);
    }

    public void updateCoordinates(double newX, double newY) {
        long stamp = stampedLock.writeLock();
        try {
            x = newX;
            y = newY;
        } finally {
            stampedLock.unlockWrite(stamp);
        }
    }

    // Lock upgrading with StampedLock
    public void conditionalUpdate(double threshold) {
        long stamp = stampedLock.readLock();
        try {
            while (x == 0.0 && y == 0.0) {
                // Try to upgrade to write lock
                long writeStamp = stampedLock.tryConvertToWriteLock(stamp);
                if (writeStamp != 0L) {
                    stamp = writeStamp;
                    x = threshold;
                    y = threshold;
                    break;
                } else {
                    // Couldn't upgrade, release read lock and acquire write lock
                    stampedLock.unlockRead(stamp);
                    stamp = stampedLock.writeLock();
                    // Re-check condition after acquiring write lock
                }
            }
        } finally {
            stampedLock.unlock(stamp);
        }
    }

    // Interruptible lock operations
    public void interruptibleOperation() throws InterruptedException {
        reentrantLock.lockInterruptibly();
        try {
            while (running) {
                // Perform work that can be interrupted
                Thread.sleep(100);
            }
        } finally {
            reentrantLock.unlock();
        }
    }

    // Lock debugging information
    public void printLockInfo() {
        System.out.println("Reentrant lock info:");
        System.out.println("  Hold count: " + reentrantLock.getHoldCount());
        System.out.println("  Queue length: " + reentrantLock.getQueueLength());
        System.out.println("  Has queued threads: " + reentrantLock.hasQueuedThreads());
        System.out.println("  Is fair: " + fairLock.isFair());
        System.out.println("  Is locked: " + reentrantLock.isLocked());
        System.out.println("  Is held by current thread: " + reentrantLock.isHeldByCurrentThread());

        if (readWriteLock instanceof ReentrantReadWriteLock) {
            ReentrantReadWriteLock rwLock = (ReentrantReadWriteLock) readWriteLock;
            System.out.println("Read-write lock info:");
            System.out.println("  Read lock count: " + rwLock.getReadLockCount());
            System.out.println("  Write lock count: " + rwLock.getWriteHoldCount());
            System.out.println("  Is write locked: " + rwLock.isWriteLocked());
        }
    }

    // Cleanup
    public void stop() {
        running = false;
        reentrantLock.lock();
        try {
            notEmpty.signalAll();
            notFull.signalAll();
        } finally {
            reentrantLock.unlock();
        }
    }
}
""",
    )

    run_updater(java_concurrency_project, mock_ingestor, skip_if_missing="java")

    project_name = java_concurrency_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.LocksExample.LocksExample",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )
