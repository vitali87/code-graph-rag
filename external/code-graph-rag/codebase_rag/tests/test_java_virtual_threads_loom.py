from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, run_updater


@pytest.fixture
def java_loom_project(temp_repo: Path) -> Path:
    """Create a Java project for testing Project Loom features."""
    project_path = temp_repo / "java_loom_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_virtual_threads_basics(
    java_loom_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic virtual thread usage patterns."""
    test_file = (
        java_loom_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "VirtualThreads.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.time.Duration;
import java.util.concurrent.*;
import java.util.stream.IntStream;

public class VirtualThreadBasics {

    // Basic virtual thread creation
    public void basicVirtualThread() {
        // Thread.ofVirtual() pattern
        Thread virtualThread = Thread.ofVirtual()
            .name("virtual-worker")
            .start(() -> {
                System.out.println("Running in virtual thread: " + Thread.currentThread());
                try {
                    Thread.sleep(Duration.ofSeconds(1));
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            });

        try {
            virtualThread.join();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    // Virtual thread factory
    public void virtualThreadFactory() {
        ThreadFactory factory = Thread.ofVirtual().factory();

        Thread thread1 = factory.newThread(() -> {
            System.out.println("Factory thread 1: " + Thread.currentThread());
        });

        Thread thread2 = factory.newThread(() -> {
            System.out.println("Factory thread 2: " + Thread.currentThread());
        });

        thread1.start();
        thread2.start();

        try {
            thread1.join();
            thread2.join();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    // Virtual thread executor
    public void virtualThreadExecutor() {
        try (ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor()) {
            // Submit multiple tasks
            var futures = IntStream.range(0, 1000)
                .mapToObj(i -> executor.submit(() -> {
                    try {
                        Thread.sleep(Duration.ofMillis(100));
                        return "Task " + i + " completed on " + Thread.currentThread();
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        return "Task " + i + " interrupted";
                    }
                }))
                .toList();

            // Collect results
            for (Future<String> future : futures) {
                try {
                    String result = future.get();
                    System.out.println(result);
                } catch (InterruptedException | ExecutionException e) {
                    System.err.println("Task failed: " + e.getMessage());
                }
            }
        }
    }

    // Virtual thread builder with custom properties
    public void customVirtualThreads() {
        Thread.Builder.OfVirtual builder = Thread.ofVirtual()
            .name("custom-virtual-", 1)
            .inheritInheritableThreadLocals(true);

        // Start multiple threads with the builder
        for (int i = 0; i < 5; i++) {
            final int taskId = i;
            builder.start(() -> {
                System.out.println("Custom virtual thread " + taskId + ": " + Thread.currentThread());

                // Simulate some work
                try {
                    Thread.sleep(Duration.ofMillis(50));
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            });
        }
    }

    // Platform vs Virtual thread comparison
    public void compareThreadTypes() {
        // Platform thread
        Thread platformThread = Thread.ofPlatform()
            .name("platform-worker")
            .start(() -> {
                System.out.println("Platform thread: " + Thread.currentThread());
                System.out.println("Is virtual: " + Thread.currentThread().isVirtual());
            });

        // Virtual thread
        Thread virtualThread = Thread.ofVirtual()
            .name("virtual-worker")
            .start(() -> {
                System.out.println("Virtual thread: " + Thread.currentThread());
                System.out.println("Is virtual: " + Thread.currentThread().isVirtual());
            });

        try {
            platformThread.join();
            virtualThread.join();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    // Carrier thread pinning demonstration
    public void carrierThreadPinning() {
        Thread.ofVirtual().start(() -> {
            System.out.println("Before synchronized block: " + Thread.currentThread());

            // This may pin the virtual thread to its carrier
            synchronized(this) {
                System.out.println("Inside synchronized block: " + Thread.currentThread());
                try {
                    Thread.sleep(Duration.ofMillis(100)); // This could pin the carrier
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }

            System.out.println("After synchronized block: " + Thread.currentThread());
        });
    }

    // ThreadLocal usage with virtual threads
    private static final ThreadLocal<String> threadLocalValue = new ThreadLocal<>();
    private static final ThreadLocal<Integer> threadLocalCounter = ThreadLocal.withInitial(() -> 0);

    public void threadLocalWithVirtualThreads() {
        try (ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor()) {
            var futures = IntStream.range(0, 10)
                .mapToObj(i -> executor.submit(() -> {
                    threadLocalValue.set("Thread-" + i);
                    threadLocalCounter.set(threadLocalCounter.get() + 1);

                    try {
                        Thread.sleep(Duration.ofMillis(10));
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }

                    return threadLocalValue.get() + " counter: " + threadLocalCounter.get();
                }))
                .toList();

            for (Future<String> future : futures) {
                try {
                    System.out.println(future.get());
                } catch (InterruptedException | ExecutionException e) {
                    System.err.println("ThreadLocal task failed: " + e.getMessage());
                }
            }
        }
    }
}
""",
    )

    run_updater(java_loom_project, mock_ingestor, skip_if_missing="java")

    project_name = java_loom_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.VirtualThreads.VirtualThreadBasics",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_structured_concurrency(
    java_loom_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test structured concurrency patterns (Java 19+ incubating)."""
    test_file = (
        java_loom_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "StructuredConcurrency.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.time.Duration;
import java.time.Instant;
import java.util.concurrent.*;
import java.util.concurrent.StructuredTaskScope;
import java.util.function.Supplier;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.stream.IntStream;

// Structured concurrency examples (requires --enable-preview)
public class StructuredConcurrencyExamples {

    // Basic structured task scope
    public String fetchUserData(String userId) throws Exception {
        try (var scope = new StructuredTaskScope.ShutdownOnFailure()) {
            // Start multiple subtasks
            Supplier<String> userTask = scope.fork(() -> fetchUser(userId));
            Supplier<String> profileTask = scope.fork(() -> fetchProfile(userId));
            Supplier<String> preferencesTask = scope.fork(() -> fetchPreferences(userId));

            // Wait for all tasks to complete or fail
            scope.join();           // Wait for all tasks
            scope.throwIfFailed();  // Throw if any task failed

            // Combine results
            return combineUserData(userTask.get(), profileTask.get(), preferencesTask.get());
        }
    }

    // Shutdown on success pattern
    public String findFirstAvailable(String[] servers) throws Exception {
        try (var scope = new StructuredTaskScope.ShutdownOnSuccess<String>()) {
            // Start tasks for each server
            for (String server : servers) {
                scope.fork(() -> checkServerAvailability(server));
            }

            // Wait for first success
            scope.join();

            // Return the first successful result
            return scope.result();
        }
    }

    // Custom task scope
    public class CustomTaskScope<T> extends StructuredTaskScope<T> {
        private volatile int completedTasks = 0;
        private volatile int failedTasks = 0;

        @Override
        protected void handleComplete(Subtask<? extends T> subtask) {
            if (subtask.state() == Subtask.State.SUCCESS) {
                completedTasks++;
            } else if (subtask.state() == Subtask.State.FAILED) {
                failedTasks++;
            }

            // Custom shutdown logic
            if (completedTasks >= 2) {
                shutdown(); // Stop after 2 successful tasks
            }
        }

        public int getCompletedTasks() {
            return completedTasks;
        }

        public int getFailedTasks() {
            return failedTasks;
        }
    }

    // Using custom task scope
    public void useCustomTaskScope() throws Exception {
        try (var scope = new CustomTaskScope<String>()) {
            // Start multiple tasks
            for (int i = 0; i < 10; i++) {
                final int taskId = i;
                scope.fork(() -> performTask(taskId));
            }

            // Wait for completion (or custom shutdown condition)
            scope.join();

            System.out.println("Completed tasks: " + scope.getCompletedTasks());
            System.out.println("Failed tasks: " + scope.getFailedTasks());
        }
    }

    // Timeout handling with structured concurrency
    public String fetchWithTimeout(String resourceId, Duration timeout) throws Exception {
        try (var scope = new StructuredTaskScope.ShutdownOnFailure()) {
            Supplier<String> dataTask = scope.fork(() -> fetchLongRunningData(resourceId));

            // Join with timeout
            scope.joinUntil(Instant.now().plus(timeout));
            scope.throwIfFailed();

            return dataTask.get();
        } catch (TimeoutException e) {
            throw new RuntimeException("Operation timed out after " + timeout, e);
        }
    }

    // Error handling patterns
    public class ErrorHandlingScope extends StructuredTaskScope<String> {
        private final List<Exception> errors = new CopyOnWriteArrayList<>();

        @Override
        protected void handleComplete(Subtask<? extends String> subtask) {
            if (subtask.state() == Subtask.State.FAILED) {
                errors.add((Exception) subtask.exception());
            }
        }

        public List<Exception> getErrors() {
            return List.copyOf(errors);
        }

        public boolean hasErrors() {
            return !errors.isEmpty();
        }
    }

    // Collecting results pattern
    public void collectResults() throws Exception {
        try (var scope = new ErrorHandlingScope()) {
            var tasks = IntStream.range(0, 5)
                .mapToObj(i -> scope.fork(() -> computeValue(i)))
                .toList();

            scope.join();

            // Process successful results
            var results = tasks.stream()
                .filter(task -> task.state() == Subtask.State.SUCCESS)
                .map(Supplier::get)
                .toList();

            System.out.println("Successful results: " + results);

            // Handle errors
            if (scope.hasErrors()) {
                System.err.println("Errors occurred: " + scope.getErrors().size());
                for (Exception error : scope.getErrors()) {
                    System.err.println("Error: " + error.getMessage());
                }
            }
        }
    }

    // Helper methods (simulated)
    private String fetchUser(String userId) {
        try {
            Thread.sleep(Duration.ofMillis(100));
            return "User{id=" + userId + "}";
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }

    private String fetchProfile(String userId) {
        try {
            Thread.sleep(Duration.ofMillis(150));
            return "Profile{userId=" + userId + "}";
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }

    private String fetchPreferences(String userId) {
        try {
            Thread.sleep(Duration.ofMillis(80));
            return "Preferences{userId=" + userId + "}";
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }

    private String combineUserData(String user, String profile, String preferences) {
        return String.format("UserData{%s, %s, %s}", user, profile, preferences);
    }

    private String checkServerAvailability(String server) {
        try {
            Thread.sleep(Duration.ofMillis((long) (Math.random() * 500)));
            if (Math.random() > 0.7) {
                throw new RuntimeException("Server " + server + " unavailable");
            }
            return "Server " + server + " is available";
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }

    private String performTask(int taskId) {
        try {
            Thread.sleep(Duration.ofMillis((long) (Math.random() * 200)));
            if (Math.random() > 0.8) {
                throw new RuntimeException("Task " + taskId + " failed");
            }
            return "Task " + taskId + " completed";
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }

    private String fetchLongRunningData(String resourceId) {
        try {
            Thread.sleep(Duration.ofSeconds(2)); // Simulate long operation
            return "Data for " + resourceId;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }

    private String computeValue(int input) {
        try {
            Thread.sleep(Duration.ofMillis(50));
            if (input % 3 == 0) {
                throw new RuntimeException("Cannot compute for input " + input);
            }
            return "Result " + (input * 2);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }
}
""",
    )

    run_updater(java_loom_project, mock_ingestor, skip_if_missing="java")

    project_name = java_loom_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.StructuredConcurrency.StructuredConcurrencyExamples",
        f"{project_name}.src.main.java.com.example.StructuredConcurrency.StructuredConcurrencyExamples.CustomTaskScope",
        f"{project_name}.src.main.java.com.example.StructuredConcurrency.StructuredConcurrencyExamples.ErrorHandlingScope",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_scoped_values(
    java_loom_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test scoped values (replacement for ThreadLocal in Project Loom)."""
    test_file = (
        java_loom_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "ScopedValues.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.concurrent.StructuredTaskScope;
import java.util.function.Supplier;

// Scoped values examples (requires --enable-preview)
public class ScopedValuesExamples {

    // Define scoped values
    private static final ScopedValue<String> USER_ID = ScopedValue.newInstance();
    private static final ScopedValue<String> REQUEST_ID = ScopedValue.newInstance();
    private static final ScopedValue<Integer> TENANT_ID = ScopedValue.newInstance();

    // Basic scoped value usage
    public void basicScopedValueUsage() {
        // Bind values and run code in that scope
        ScopedValue.where(USER_ID, "user123")
                   .where(REQUEST_ID, "req456")
                   .run(() -> {
                       processRequest();
                   });
    }

    // Scoped value with return value
    public String getScopedData() {
        return ScopedValue.where(USER_ID, "user789")
                         .where(TENANT_ID, 42)
                         .call(() -> {
                             return fetchUserData();
                         });
    }

    // Nested scoped values
    public void nestedScopedValues() {
        ScopedValue.where(TENANT_ID, 1).run(() -> {
            System.out.println("Outer tenant: " + TENANT_ID.get());

            ScopedValue.where(USER_ID, "outer-user").run(() -> {
                System.out.println("Outer user: " + USER_ID.get());

                // Inner scope with different user
                ScopedValue.where(USER_ID, "inner-user").run(() -> {
                    System.out.println("Inner user: " + USER_ID.get());
                    System.out.println("Still same tenant: " + TENANT_ID.get());
                });

                System.out.println("Back to outer user: " + USER_ID.get());
            });
        });
    }

    // Scoped values with structured concurrency
    public void scopedValuesWithStructuredConcurrency() throws Exception {
        ScopedValue.where(REQUEST_ID, "req123")
                   .where(USER_ID, "user456")
                   .run(() -> {
                       try (var scope = new StructuredTaskScope.ShutdownOnFailure()) {
                           // All forked tasks inherit the scoped values
                           Supplier<String> task1 = scope.fork(() -> performTaskWithContext(1));
                           Supplier<String> task2 = scope.fork(() -> performTaskWithContext(2));
                           Supplier<String> task3 = scope.fork(() -> performTaskWithContext(3));

                           scope.join();
                           scope.throwIfFailed();

                           System.out.println("Task 1: " + task1.get());
                           System.out.println("Task 2: " + task2.get());
                           System.out.println("Task 3: " + task3.get());

                       } catch (Exception e) {
                           throw new RuntimeException("Structured task failed", e);
                       }
                   });
    }

    // Conditional scoped value access
    public void conditionalScopedAccess() {
        ScopedValue.where(USER_ID, "admin").run(() -> {
            if (USER_ID.isBound()) {
                String userId = USER_ID.get();
                System.out.println("User ID is bound: " + userId);

                if ("admin".equals(userId)) {
                    performAdminOperations();
                } else {
                    performUserOperations();
                }
            } else {
                System.out.println("No user context available");
            }
        });
    }

    // Scoped value inheritance patterns
    public class ServiceContext {
        private static final ScopedValue<String> CORRELATION_ID = ScopedValue.newInstance();
        private static final ScopedValue<String> SERVICE_NAME = ScopedValue.newInstance();

        public <T> T withContext(String correlationId, String serviceName, Supplier<T> operation) {
            return ScopedValue.where(CORRELATION_ID, correlationId)
                             .where(SERVICE_NAME, serviceName)
                             .call(operation);
        }

        public void logWithContext(String message) {
            String correlation = CORRELATION_ID.isBound() ? CORRELATION_ID.get() : "unknown";
            String service = SERVICE_NAME.isBound() ? SERVICE_NAME.get() : "unknown";

            System.out.println(String.format("[%s] [%s] %s", correlation, service, message));
        }
    }

    // Using service context
    public void useServiceContext() {
        ServiceContext context = new ServiceContext();

        String result = context.withContext("corr-123", "user-service", () -> {
            context.logWithContext("Starting user operation");

            // Nested call maintains context
            return context.withContext("corr-124", "auth-service", () -> {
                context.logWithContext("Performing authentication");
                return "authenticated";
            });
        });

        System.out.println("Final result: " + result);
    }

    // Error handling with scoped values
    public void errorHandlingWithScopedValues() {
        try {
            ScopedValue.where(USER_ID, "error-user").run(() -> {
                try {
                    riskyOperation();
                } catch (Exception e) {
                    String userId = USER_ID.isBound() ? USER_ID.get() : "unknown";
                    System.err.println("Error for user " + userId + ": " + e.getMessage());
                    throw new RuntimeException("Operation failed for user " + userId, e);
                }
            });
        } catch (Exception e) {
            System.err.println("Outer catch: " + e.getMessage());
        }
    }

    // Performance comparison helper
    public void performanceComparison() {
        // Using ThreadLocal (old way)
        ThreadLocal<String> threadLocal = new ThreadLocal<>();
        threadLocal.set("thread-local-value");

        long startTime = System.nanoTime();
        for (int i = 0; i < 1000000; i++) {
            String value = threadLocal.get();
        }
        long threadLocalTime = System.nanoTime() - startTime;

        threadLocal.remove();

        // Using ScopedValue (new way)
        ScopedValue.where(USER_ID, "scoped-value").run(() -> {
            long startTime2 = System.nanoTime();
            for (int i = 0; i < 1000000; i++) {
                String value = USER_ID.get();
            }
            long scopedValueTime = System.nanoTime() - startTime2;

            System.out.println("ThreadLocal time: " + threadLocalTime + " ns");
            System.out.println("ScopedValue time: " + scopedValueTime + " ns");
        });
    }

    // Helper methods
    private void processRequest() {
        String userId = USER_ID.isBound() ? USER_ID.get() : "anonymous";
        String requestId = REQUEST_ID.isBound() ? REQUEST_ID.get() : "unknown";

        System.out.println("Processing request " + requestId + " for user " + userId);
    }

    private String fetchUserData() {
        String userId = USER_ID.get();
        Integer tenantId = TENANT_ID.get();

        return String.format("UserData{userId=%s, tenantId=%d}", userId, tenantId);
    }

    private String performTaskWithContext(int taskNumber) {
        String requestId = REQUEST_ID.get();
        String userId = USER_ID.get();

        try {
            Thread.sleep(100); // Simulate work
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }

        return String.format("Task %d completed for request %s, user %s",
                           taskNumber, requestId, userId);
    }

    private void performAdminOperations() {
        System.out.println("Performing admin operations for user: " + USER_ID.get());
    }

    private void performUserOperations() {
        System.out.println("Performing user operations for user: " + USER_ID.get());
    }

    private void riskyOperation() {
        if (Math.random() > 0.5) {
            throw new RuntimeException("Simulated failure");
        }
        System.out.println("Risky operation succeeded for user: " + USER_ID.get());
    }
}
""",
    )

    run_updater(java_loom_project, mock_ingestor, skip_if_missing="java")

    project_name = java_loom_project.name
    created_classes = get_node_names(mock_ingestor, "Class")

    expected_classes = {
        f"{project_name}.src.main.java.com.example.ScopedValues.ScopedValuesExamples",
        f"{project_name}.src.main.java.com.example.ScopedValues.ScopedValuesExamples.ServiceContext",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )
