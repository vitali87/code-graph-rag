"""
Java Stream API and functional programming parsing testing.
Tests Stream operations, functional interfaces, method references, Optional,
lambda expressions, and functional programming patterns.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def java_streams_project(temp_repo: Path) -> Path:
    """Create a Java project for testing streams and functional programming."""
    project_path = temp_repo / "java_streams_test"
    project_path.mkdir()

    # Create standard Java project structure
    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_stream_operations(
    java_streams_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java Stream API operations."""
    test_file = (
        java_streams_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "StreamOperations.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.stream.*;
import java.util.function.*;
import java.math.BigDecimal;

public class StreamOperations {

    private final List<String> names = Arrays.asList(
        "Alice", "Bob", "Charlie", "David", "Eve", "Frank", "Grace", "Henry"
    );

    private final List<Integer> numbers = Arrays.asList(
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20, 25, 30
    );

    // Basic stream operations
    public void basicStreamOperations() {
        // Filter and collect
        List<String> longNames = names.stream()
            .filter(name -> name.length() > 4)
            .collect(Collectors.toList());

        // Map and collect
        List<String> upperNames = names.stream()
            .map(String::toUpperCase)
            .collect(Collectors.toList());

        // Sorted stream
        List<String> sortedNames = names.stream()
            .sorted()
            .collect(Collectors.toList());

        // Reverse sorted
        List<String> reverseSorted = names.stream()
            .sorted(Comparator.reverseOrder())
            .collect(Collectors.toList());

        // Distinct elements
        List<Integer> distinctNumbers = Stream.of(1, 2, 2, 3, 3, 4, 5, 5)
            .distinct()
            .collect(Collectors.toList());

        // Limit and skip
        List<String> limitedNames = names.stream()
            .skip(2)
            .limit(3)
            .collect(Collectors.toList());
    }

    // Advanced transformations
    public void advancedTransformations() {
        // FlatMap with collections
        List<List<String>> nestedLists = Arrays.asList(
            Arrays.asList("a", "b"),
            Arrays.asList("c", "d", "e"),
            Arrays.asList("f")
        );

        List<String> flattened = nestedLists.stream()
            .flatMap(List::stream)
            .collect(Collectors.toList());

        // FlatMap with optional
        List<Optional<String>> optionals = Arrays.asList(
            Optional.of("hello"),
            Optional.empty(),
            Optional.of("world")
        );

        List<String> presentValues = optionals.stream()
            .flatMap(Optional::stream)
            .collect(Collectors.toList());

        // Complex mapping chains
        List<String> processed = names.stream()
            .filter(name -> name.length() > 3)
            .map(String::toLowerCase)
            .map(name -> name + "_processed")
            .sorted()
            .collect(Collectors.toList());

        // Map to different types
        List<Integer> nameLengths = names.stream()
            .mapToInt(String::length)
            .boxed()
            .collect(Collectors.toList());

        // MapToDouble and statistics
        DoubleSummaryStatistics stats = names.stream()
            .mapToDouble(String::length)
            .summaryStatistics();
    }

    // Reduction operations
    public void reductionOperations() {
        // Simple reductions
        OptionalInt sum = numbers.stream()
            .mapToInt(Integer::intValue)
            .reduce(Integer::sum);

        Optional<Integer> max = numbers.stream()
            .reduce(Integer::max);

        Optional<Integer> min = numbers.stream()
            .reduce(Integer::min);

        // Custom reduction
        Optional<String> concatenated = names.stream()
            .reduce((s1, s2) -> s1 + ", " + s2);

        // Reduction with identity
        String allNames = names.stream()
            .reduce("Names: ", (acc, name) -> acc + name + " ");

        // Complex reduction
        int totalLength = names.stream()
            .map(String::length)
            .reduce(0, Integer::sum);

        // Parallel reduction
        long count = numbers.parallelStream()
            .filter(n -> n % 2 == 0)
            .count();
    }

    // Collectors
    public void collectorsExamples() {
        // Basic collectors
        List<String> toList = names.stream()
            .collect(Collectors.toList());

        Set<String> toSet = names.stream()
            .collect(Collectors.toSet());

        String joined = names.stream()
            .collect(Collectors.joining(", "));

        String joinedWithPrefix = names.stream()
            .collect(Collectors.joining(", ", "Names: [", "]"));

        // Grouping
        Map<Integer, List<String>> byLength = names.stream()
            .collect(Collectors.groupingBy(String::length));

        Map<Boolean, List<String>> partitioned = names.stream()
            .collect(Collectors.partitioningBy(name -> name.length() > 4));

        // Counting
        Map<Integer, Long> lengthCounts = names.stream()
            .collect(Collectors.groupingBy(String::length, Collectors.counting()));

        // Downstream collectors
        Map<Integer, String> lengthToNames = names.stream()
            .collect(Collectors.groupingBy(
                String::length,
                Collectors.mapping(String::toUpperCase, Collectors.joining(", "))
            ));

        // Statistics collectors
        IntSummaryStatistics lengthStats = names.stream()
            .collect(Collectors.summarizingInt(String::length));

        // Custom collector
        String customResult = names.stream()
            .collect(Collector.of(
                StringBuilder::new,
                (sb, name) -> sb.append(name).append("|"),
                StringBuilder::append,
                StringBuilder::toString
            ));
    }

    // Terminal operations
    public void terminalOperations() {
        // Find operations
        Optional<String> first = names.stream()
            .filter(name -> name.startsWith("A"))
            .findFirst();

        Optional<String> any = names.stream()
            .parallel()
            .filter(name -> name.length() > 5)
            .findAny();

        // Match operations
        boolean allLongEnough = names.stream()
            .allMatch(name -> name.length() > 0);

        boolean anyStartsWithA = names.stream()
            .anyMatch(name -> name.startsWith("A"));

        boolean noneEmpty = names.stream()
            .noneMatch(String::isEmpty);

        // forEach operations
        names.stream()
            .filter(name -> name.length() > 4)
            .forEach(System.out::println);

        names.stream()
            .sorted()
            .forEachOrdered(System.out::println);

        // Conversion to arrays
        String[] nameArray = names.stream()
            .toArray(String[]::new);

        Object[] objectArray = names.stream()
            .toArray();
    }

    // Primitive streams
    public void primitiveStreams() {
        // IntStream
        IntStream.range(1, 10)
            .filter(i -> i % 2 == 0)
            .forEach(System.out::println);

        IntStream.rangeClosed(1, 10)
            .map(i -> i * i)
            .sum();

        // LongStream
        LongStream.of(1L, 2L, 3L, 4L, 5L)
            .skip(2)
            .limit(2)
            .forEach(System.out::println);

        // DoubleStream
        DoubleStream.of(1.1, 2.2, 3.3, 4.4)
            .mapToObj(BigDecimal::valueOf)
            .collect(Collectors.toList());

        // Random streams
        new Random().ints(5, 1, 10)
            .forEach(System.out::println);

        new Random().doubles(3)
            .forEach(System.out::println);

        // String to IntStream
        "hello".chars()
            .mapToObj(ch -> (char) ch)
            .forEach(System.out::println);
    }

    // Parallel streams
    public void parallelStreams() {
        // Parallel processing
        long count = numbers.parallelStream()
            .filter(n -> isPrime(n))
            .count();

        // Parallel reduction
        int sum = numbers.parallelStream()
            .reduce(0, Integer::sum);

        // Parallel collect
        List<String> processed = names.parallelStream()
            .map(String::toUpperCase)
            .filter(name -> name.length() > 3)
            .collect(Collectors.toList());

        // Custom parallel processing
        Map<Boolean, List<Integer>> evenOdd = numbers.parallelStream()
            .collect(Collectors.partitioningBy(n -> n % 2 == 0));

        // Sequential after parallel
        List<String> result = names.parallelStream()
            .filter(name -> name.length() > 4)
            .sequential()
            .sorted()
            .collect(Collectors.toList());
    }

    // Stream creation methods
    public void streamCreation() {
        // From collections
        Stream<String> fromList = names.stream();
        Stream<String> fromArray = Arrays.stream(new String[]{"a", "b", "c"});

        // Empty stream
        Stream<String> empty = Stream.empty();

        // Single element
        Stream<String> single = Stream.of("single");

        // Multiple elements
        Stream<String> multiple = Stream.of("a", "b", "c", "d");

        // Generated streams
        Stream<Double> randomStream = Stream.generate(Math::random)
            .limit(5);

        Stream<Integer> iteratedStream = Stream.iterate(0, n -> n + 2)
            .limit(10);

        // Java 9+ iterate with predicate
        Stream<Integer> limitedIterate = Stream.iterate(1, n -> n < 100, n -> n * 2);

        // From file lines
        try {
            Stream<String> lines = java.nio.file.Files.lines(
                java.nio.file.Paths.get("example.txt")
            );
        } catch (Exception e) {
            // Handle file operations
        }

        // Builder pattern
        Stream<String> built = Stream.<String>builder()
            .add("first")
            .add("second")
            .add("third")
            .build();
    }

    // Helper method
    private boolean isPrime(int n) {
        if (n <= 1) return false;
        if (n <= 3) return true;
        if (n % 2 == 0 || n % 3 == 0) return false;

        for (int i = 5; i * i <= n; i += 6) {
            if (n % i == 0 || n % (i + 2) == 0) {
                return false;
            }
        }
        return true;
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_streams_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify the class was detected
    project_name = java_streams_project.name
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    expected_classes = {
        f"{project_name}.src.main.java.com.example.StreamOperations.StreamOperations",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_functional_interfaces(
    java_streams_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java functional interfaces and lambda expressions."""
    test_file = (
        java_streams_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "FunctionalInterfaces.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.function.*;
import java.util.concurrent.Callable;

// Custom functional interfaces
@FunctionalInterface
interface StringProcessor {
    String process(String input);

    // Default methods allowed
    default String processWithPrefix(String input, String prefix) {
        return prefix + process(input);
    }

    // Static methods allowed
    static String reverse(String input) {
        return new StringBuilder(input).reverse().toString();
    }
}

@FunctionalInterface
interface Calculator<T extends Number> {
    T calculate(T a, T b);
}

@FunctionalInterface
interface TriFunction<T, U, V, R> {
    R apply(T t, U u, V v);
}

@FunctionalInterface
interface EventHandler {
    void handle(String event);

    default EventHandler andThen(EventHandler after) {
        return event -> {
            handle(event);
            after.handle(event);
        };
    }
}

public class FunctionalInterfaces {

    // Built-in functional interfaces usage
    public void builtInFunctionalInterfaces() {
        // Predicate - tests a condition
        Predicate<String> isEmpty = String::isEmpty;
        Predicate<String> isLong = s -> s.length() > 10;
        Predicate<String> isShortAndNotEmpty = isEmpty.negate().and(isLong.negate());

        // Function - transforms input to output
        Function<String, Integer> stringLength = String::length;
        Function<String, String> toUpperCase = String::toUpperCase;
        Function<String, String> processString = stringLength.andThen(Object::toString)
            .andThen(s -> "Length: " + s);

        // Consumer - performs action on input
        Consumer<String> printer = System.out::println;
        Consumer<String> logger = s -> System.err.println("LOG: " + s);
        Consumer<String> combined = printer.andThen(logger);

        // Supplier - provides a value
        Supplier<String> randomString = () -> "Random: " + Math.random();
        Supplier<List<String>> listSupplier = ArrayList::new;

        // BiFunction - two inputs, one output
        BiFunction<String, String, String> concatenate = (s1, s2) -> s1 + s2;
        BiFunction<Integer, Integer, Integer> add = Integer::sum;

        // BiPredicate - two inputs, boolean output
        BiPredicate<String, String> startsWith = String::startsWith;
        BiPredicate<Integer, Integer> isEqual = Objects::equals;

        // BiConsumer - action on two inputs
        BiConsumer<String, Integer> printWithIndex = (s, i) ->
            System.out.println(i + ": " + s);

        // UnaryOperator - special case of Function (same input/output type)
        UnaryOperator<String> trim = String::trim;
        UnaryOperator<Integer> square = x -> x * x;

        // BinaryOperator - special case of BiFunction (same types)
        BinaryOperator<String> longerString = (s1, s2) ->
            s1.length() > s2.length() ? s1 : s2;
        BinaryOperator<Integer> max = Integer::max;
    }

    // Lambda expressions with different syntaxes
    public void lambdaExpressions() {
        // No parameters
        Runnable simpleTask = () -> System.out.println("Running");
        Supplier<Integer> getConstant = () -> 42;

        // Single parameter (parentheses optional)
        Function<String, String> addExclamation = s -> s + "!";
        Function<String, String> addExclamationAlt = (s) -> s + "!";

        // Multiple parameters
        BiFunction<Integer, Integer, Integer> multiply = (a, b) -> a * b;
        TriFunction<String, String, String, String> combine = (a, b, c) -> a + b + c;

        // Block syntax
        Function<String, String> complexProcessor = input -> {
            String processed = input.trim().toLowerCase();
            if (processed.isEmpty()) {
                return "empty";
            }
            return processed.substring(0, 1).toUpperCase() + processed.substring(1);
        };

        // Capturing local variables (effectively final)
        String prefix = "PREFIX: ";
        Function<String, String> addPrefix = s -> prefix + s;

        // Method references
        Function<String, Integer> methodRef1 = String::length;  // instance method
        Function<String, String> methodRef2 = String::toUpperCase;  // instance method
        BiPredicate<String, String> methodRef3 = String::equals;  // instance method with parameter
        Supplier<String> methodRef4 = String::new;  // constructor reference
        Function<String, String[]> methodRef5 = String[]::new;  // array constructor
        Function<String, Optional<String>> methodRef6 = Optional::of;  // static method
    }

    // Custom functional interfaces usage
    public void customFunctionalInterfaces() {
        // StringProcessor implementations
        StringProcessor toUpper = String::toUpperCase;
        StringProcessor addStars = s -> "***" + s + "***";
        StringProcessor reverse = s -> new StringBuilder(s).reverse().toString();

        // Using default method
        String processed = toUpper.processWithPrefix("hello", "Greeting: ");

        // Static method usage
        String reversed = StringProcessor.reverse("world");

        // Calculator with generics
        Calculator<Integer> intCalculator = (a, b) -> a + b;
        Calculator<Double> doubleCalculator = (a, b) -> a * b;

        // TriFunction usage
        TriFunction<String, Integer, Boolean, String> formatter =
            (text, number, uppercase) -> {
                String result = text + ": " + number;
                return uppercase ? result.toUpperCase() : result;
            };

        // EventHandler with composition
        EventHandler logger = event -> System.out.println("LOG: " + event);
        EventHandler auditor = event -> System.out.println("AUDIT: " + event);
        EventHandler combined = logger.andThen(auditor);

        combined.handle("User logged in");
    }

    // Higher-order functions
    public void higherOrderFunctions() {
        // Function that returns a function
        Function<String, Function<String, String>> prefixAdder = prefix ->
            text -> prefix + text;

        Function<String, String> addHello = prefixAdder.apply("Hello, ");
        String result = addHello.apply("World");

        // Function that takes a function as parameter
        Function<String, String> processText(Function<String, String> processor, String input) {
            return "Processed: " + processor.apply(input);
        }

        // Currying example
        TriFunction<Integer, Integer, Integer, Integer> addThree = (a, b, c) -> a + b + c;
        Function<Integer, Function<Integer, Function<Integer, Integer>>> curriedAdd =
            a -> b -> c -> addThree.apply(a, b, c);

        Integer curriedResult = curriedAdd.apply(1).apply(2).apply(3);
    }

    // Exception handling in lambdas
    public void exceptionHandling() {
        // Wrapper for checked exceptions
        Function<String, Integer> parseInteger = s -> {
            try {
                return Integer.parseInt(s);
            } catch (NumberFormatException e) {
                return 0;
            }
        };

        // Using wrapper functional interface
        List<String> numbers = Arrays.asList("1", "2", "invalid", "4");
        List<Integer> parsed = numbers.stream()
            .map(parseInteger)
            .collect(java.util.stream.Collectors.toList());

        // Callable for exceptions
        Callable<String> riskyOperation = () -> {
            if (Math.random() < 0.5) {
                throw new Exception("Random failure");
            }
            return "Success";
        };
    }

    // Functional composition
    public void functionalComposition() {
        // Function composition
        Function<String, String> trim = String::trim;
        Function<String, String> upper = String::toUpperCase;
        Function<String, Integer> length = String::length;

        Function<String, Integer> processAndCount = trim
            .andThen(upper)
            .andThen(length);

        Function<String, String> processString = trim
            .andThen(upper)
            .compose(s -> s + " "); // Apply before trim

        // Predicate composition
        Predicate<String> notNull = Objects::nonNull;
        Predicate<String> notEmpty = s -> !s.isEmpty();
        Predicate<String> longEnough = s -> s.length() > 3;

        Predicate<String> valid = notNull
            .and(notEmpty)
            .and(longEnough);

        // Consumer composition
        Consumer<String> print = System.out::println;
        Consumer<String> log = s -> System.err.println("LOG: " + s);
        Consumer<String> audit = s -> System.err.println("AUDIT: " + s);

        Consumer<String> fullProcess = print
            .andThen(log)
            .andThen(audit);
    }

    // Functional interfaces with generics
    public void genericFunctionalInterfaces() {
        // Generic function composition
        Function<List<String>, Integer> listSize = List::size;
        Function<String, List<String>> wrapInList = Arrays::asList;
        Function<String, Integer> stringToListSize = wrapInList.andThen(listSize);

        // Generic predicates
        Predicate<Collection<?>> isEmpty = Collection::isEmpty;
        Predicate<Optional<?>> isPresent = Optional::isPresent;

        // Generic suppliers
        Supplier<Map<String, Object>> mapSupplier = HashMap::new;
        Supplier<Set<Integer>> setSupplier = HashSet::new;

        // Bounded type parameters
        Function<List<? extends Number>, Double> average = list ->
            list.stream()
                .mapToDouble(Number::doubleValue)
                .average()
                .orElse(0.0);
    }

    // Method reference examples
    public void methodReferences() {
        // Static method references
        Function<String, Integer> parseIntRef = Integer::parseInt;
        BinaryOperator<Integer> maxRef = Integer::max;

        // Instance method references of particular object
        String text = "Hello World";
        Supplier<String> upperCaseRef = text::toUpperCase;
        Supplier<Integer> lengthRef = text::length;

        // Instance method references of arbitrary object
        Function<String, String> trimRef = String::trim;
        Function<String, Integer> lengthFuncRef = String::length;

        // Constructor references
        Supplier<StringBuilder> sbSupplier = StringBuilder::new;
        Function<String, StringBuilder> sbFunction = StringBuilder::new;
        Function<Integer, int[]> arrayConstructor = int[]::new;

        // Array constructor references
        Function<Integer, String[]> stringArrayConstructor = String[]::new;
        IntFunction<boolean[]> boolArrayConstructor = boolean[]::new;
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_streams_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify the classes and interfaces were detected
    project_name = java_streams_project.name
    all_calls = mock_ingestor.ensure_node_batch.call_args_list

    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    expected_classes = {
        f"{project_name}.src.main.java.com.example.FunctionalInterfaces.FunctionalInterfaces",
    }

    expected_interfaces = {
        f"{project_name}.src.main.java.com.example.FunctionalInterfaces.StringProcessor",
        f"{project_name}.src.main.java.com.example.FunctionalInterfaces.Calculator",
        f"{project_name}.src.main.java.com.example.FunctionalInterfaces.TriFunction",
        f"{project_name}.src.main.java.com.example.FunctionalInterfaces.EventHandler",
    }

    missing_classes = expected_classes - created_classes
    missing_interfaces = expected_interfaces - created_interfaces

    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )
    assert not missing_interfaces, (
        f"Missing expected interfaces: {sorted(list(missing_interfaces))}"
    )


def test_optional_patterns(
    java_streams_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java Optional patterns and usage."""
    test_file = (
        java_streams_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "OptionalPatterns.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.function.*;
import java.util.stream.*;

public class OptionalPatterns {

    // Optional creation
    public void optionalCreation() {
        // Empty optional
        Optional<String> empty = Optional.empty();

        // Optional with value
        Optional<String> withValue = Optional.of("Hello");

        // Optional with nullable value
        String nullableValue = null;
        Optional<String> nullable = Optional.ofNullable(nullableValue);

        // Creating optionals from collections
        List<String> names = Arrays.asList("Alice", "Bob", "Charlie");
        Optional<String> first = names.stream().findFirst();
        Optional<String> any = names.stream().findAny();
    }

    // Basic optional operations
    public void basicOptionalOperations() {
        Optional<String> optional = Optional.of("Hello World");

        // Check presence
        if (optional.isPresent()) {
            String value = optional.get();
            System.out.println("Value: " + value);
        }

        // Check emptiness (Java 11+)
        if (optional.isEmpty()) {
            System.out.println("Optional is empty");
        }

        // Safe value extraction
        String value = optional.orElse("Default Value");
        String lazyValue = optional.orElseGet(() -> "Computed Default");

        // Exception throwing
        try {
            String requiredValue = optional.orElseThrow();
            String customException = optional.orElseThrow(() ->
                new IllegalStateException("Value is required"));
        } catch (Exception e) {
            System.err.println("No value present: " + e.getMessage());
        }
    }

    // Optional transformations
    public void optionalTransformations() {
        Optional<String> original = Optional.of("  Hello World  ");

        // Map - transform the value if present
        Optional<String> trimmed = original.map(String::trim);
        Optional<Integer> length = original.map(String::length);
        Optional<String> upper = original.map(String::toUpperCase);

        // Chained map operations
        Optional<Integer> processedLength = original
            .map(String::trim)
            .map(String::toUpperCase)
            .map(String::length);

        // FlatMap - for operations that return Optional
        Optional<String> flattened = original
            .flatMap(this::extractFirstWord);

        // Multiple flatMap operations
        Optional<Integer> wordLength = original
            .flatMap(this::extractFirstWord)
            .map(String::length);
    }

    private Optional<String> extractFirstWord(String text) {
        String[] words = text.trim().split("\\\\s+");
        return words.length > 0 ? Optional.of(words[0]) : Optional.empty();
    }

    // Conditional operations
    public void conditionalOperations() {
        Optional<String> optional = Optional.of("Hello");

        // Filter - keep value only if predicate matches
        Optional<String> longString = optional.filter(s -> s.length() > 3);
        Optional<String> startsWithH = optional.filter(s -> s.startsWith("H"));

        // Chained filters
        Optional<String> filtered = optional
            .filter(s -> s.length() > 2)
            .filter(s -> s.contains("l"))
            .filter(s -> !s.isEmpty());

        // ifPresent - perform action if value present
        optional.ifPresent(System.out::println);
        optional.ifPresent(value -> System.out.println("Found: " + value));

        // ifPresentOrElse (Java 9+)
        optional.ifPresentOrElse(
            value -> System.out.println("Value: " + value),
            () -> System.out.println("No value present")
        );
    }

    // Advanced optional patterns
    public void advancedOptionalPatterns() {
        // Optional chaining with flatMap
        Optional<Person> person = findPerson("John");
        Optional<String> emailDomain = person
            .flatMap(Person::getEmail)
            .map(email -> email.substring(email.indexOf('@') + 1));

        // Optional with streams
        List<Optional<String>> optionals = Arrays.asList(
            Optional.of("Alice"),
            Optional.empty(),
            Optional.of("Bob"),
            Optional.empty(),
            Optional.of("Charlie")
        );

        // Filter present values
        List<String> presentValues = optionals.stream()
            .filter(Optional::isPresent)
            .map(Optional::get)
            .collect(Collectors.toList());

        // Using flatMap with streams (Java 9+)
        List<String> streamFlattened = optionals.stream()
            .flatMap(Optional::stream)
            .collect(Collectors.toList());

        // Optional reduction
        Optional<String> concatenated = optionals.stream()
            .flatMap(Optional::stream)
            .reduce((s1, s2) -> s1 + ", " + s2);
    }

    // Optional best practices
    public void optionalBestPractices() {
        // Avoid nested optionals
        Optional<Optional<String>> nested = Optional.of(Optional.of("value")); // BAD
        Optional<String> flattened = nested.flatMap(Function.identity()); // GOOD

        // Don't use Optional for collections
        Optional<List<String>> optionalList = getOptionalList(); // QUESTIONABLE
        List<String> list = getListOrEmpty(); // BETTER

        // Optional method parameters
        public void processValue(Optional<String> value) { // AVOID
            value.ifPresent(this::process);
        }

        public void processValue(String value) { // PREFER
            if (value != null) {
                process(value);
            }
        }

        // Use orElseThrow for required values
        String required = findValue().orElseThrow(() ->
            new IllegalStateException("Required value not found"));

        // Use orElse for simple defaults
        String withDefault = findValue().orElse("default");

        // Use orElseGet for expensive defaults
        String withExpensiveDefault = findValue().orElseGet(this::computeExpensiveDefault);
    }

    // Optional with different types
    public void optionalWithTypes() {
        // Optional primitives (not recommended, use OptionalInt, etc.)
        Optional<Integer> optionalInt = Optional.of(42);
        Optional<Double> optionalDouble = Optional.of(3.14);
        Optional<Boolean> optionalBoolean = Optional.of(true);

        // Specialized optional types
        OptionalInt optInt = OptionalInt.of(42);
        OptionalLong optLong = OptionalLong.of(123L);
        OptionalDouble optDouble = OptionalDouble.of(3.14);

        // Converting between types
        Optional<Integer> fromOptionalInt = optInt.isPresent()
            ? Optional.of(optInt.getAsInt())
            : Optional.empty();

        // Optional with complex types
        Optional<List<String>> optionalList = Optional.of(Arrays.asList("a", "b", "c"));
        Optional<Map<String, Integer>> optionalMap = Optional.of(Map.of("key", 1));

        // Optional with custom objects
        Optional<Person> optionalPerson = Optional.of(new Person("John", "john@example.com"));
    }

    // Optional error handling patterns
    public void optionalErrorHandling() {
        // Instead of returning null
        public Optional<String> findUserEmail(String userId) {
            if (userId == null || userId.isEmpty()) {
                return Optional.empty();
            }
            // Database lookup simulation
            if (userId.equals("1")) {
                return Optional.of("user1@example.com");
            }
            return Optional.empty();
        }

        // Chain optional operations with error handling
        String result = findUserEmail("1")
            .filter(email -> email.contains("@"))
            .map(String::toLowerCase)
            .orElseThrow(() -> new IllegalArgumentException("Invalid email"));

        // Optional validation chain
        Optional<String> validatedInput = Optional.ofNullable(getUserInput())
            .filter(input -> !input.trim().isEmpty())
            .filter(input -> input.length() <= 100)
            .map(String::trim);
    }

    // Helper methods and classes
    private Optional<Person> findPerson(String name) {
        if ("John".equals(name)) {
            return Optional.of(new Person("John", "john@example.com"));
        }
        return Optional.empty();
    }

    private Optional<List<String>> getOptionalList() {
        return Optional.of(Arrays.asList("item1", "item2"));
    }

    private List<String> getListOrEmpty() {
        return Arrays.asList("item1", "item2");
    }

    private Optional<String> findValue() {
        return Math.random() > 0.5 ? Optional.of("found") : Optional.empty();
    }

    private String computeExpensiveDefault() {
        // Simulate expensive computation
        return "expensive_default_" + System.currentTimeMillis();
    }

    private String getUserInput() {
        return "user input";
    }

    private void process(String value) {
        System.out.println("Processing: " + value);
    }

    // Helper class
    static class Person {
        private final String name;
        private final String email;

        public Person(String name, String email) {
            this.name = name;
            this.email = email;
        }

        public String getName() {
            return name;
        }

        public Optional<String> getEmail() {
            return Optional.ofNullable(email);
        }
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_streams_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify the classes were detected
    project_name = java_streams_project.name
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    expected_classes = {
        f"{project_name}.src.main.java.com.example.OptionalPatterns.OptionalPatterns",
        f"{project_name}.src.main.java.com.example.OptionalPatterns.OptionalPatterns.Person",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_method_references_patterns(
    java_streams_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java method reference patterns."""
    test_file = (
        java_streams_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "MethodReferences.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.function.*;
import java.util.stream.*;

public class MethodReferences {

    // Static method references
    public void staticMethodReferences() {
        // Reference to static methods
        Function<String, Integer> parseIntRef = Integer::parseInt;
        BinaryOperator<Integer> maxRef = Integer::max;
        BinaryOperator<Double> minRef = Double::min;
        Function<Object, String> toStringRef = String::valueOf;

        // Using static method references
        List<String> numbers = Arrays.asList("1", "2", "3", "4", "5");
        List<Integer> parsed = numbers.stream()
            .map(Integer::parseInt)
            .collect(Collectors.toList());

        Optional<Integer> maximum = parsed.stream()
            .reduce(Integer::max);

        // Static utility methods
        List<String> texts = Arrays.asList("hello", "world", "java");
        texts.stream()
            .map(String::valueOf)
            .forEach(System.out::println);

        // Math static methods
        List<Double> values = Arrays.asList(1.5, 2.7, 3.9);
        List<Long> rounded = values.stream()
            .map(Math::round)
            .collect(Collectors.toList());

        // Custom static methods
        List<String> processedTexts = texts.stream()
            .map(TextUtils::capitalize)
            .collect(Collectors.toList());
    }

    // Instance method references on specific objects
    public void boundMethodReferences() {
        String prefix = "PREFIX: ";
        List<String> items = Arrays.asList("item1", "item2", "item3");

        // Method reference to instance method of specific object
        Function<String, String> addPrefix = prefix::concat;

        List<String> prefixed = items.stream()
            .map(prefix::concat)
            .collect(Collectors.toList());

        // System.out methods
        items.forEach(System.out::println);
        items.forEach(System.err::println);

        // StringBuilder methods
        StringBuilder sb = new StringBuilder();
        items.forEach(sb::append);

        // Collection methods
        Set<String> resultSet = new HashSet<>();
        items.forEach(resultSet::add);

        // Custom object methods
        TextProcessor processor = new TextProcessor();
        List<String> processed = items.stream()
            .map(processor::process)
            .collect(Collectors.toList());

        // Method chaining with bound references
        items.stream()
            .map(processor::toUpperCase)
            .map(processor::addBrackets)
            .forEach(System.out::println);
    }

    // Instance method references on arbitrary objects
    public void unboundMethodReferences() {
        List<String> texts = Arrays.asList("hello", "WORLD", "Java", "Programming");

        // String instance methods
        List<String> upperCase = texts.stream()
            .map(String::toUpperCase)
            .collect(Collectors.toList());

        List<String> lowerCase = texts.stream()
            .map(String::toLowerCase)
            .collect(Collectors.toList());

        List<String> trimmed = texts.stream()
            .map(String::trim)
            .collect(Collectors.toList());

        List<Integer> lengths = texts.stream()
            .map(String::length)
            .collect(Collectors.toList());

        // Comparisons using method references
        Optional<String> longest = texts.stream()
            .max(Comparator.comparing(String::length));

        List<String> sorted = texts.stream()
            .sorted(String::compareToIgnoreCase)
            .collect(Collectors.toList());

        // Predicates with method references
        List<String> nonEmpty = texts.stream()
            .filter(s -> !s.isEmpty()) // Lambda
            .filter(((Predicate<String>) String::isEmpty).negate()) // Method reference
            .collect(Collectors.toList());

        // Complex transformations
        Map<Integer, List<String>> byLength = texts.stream()
            .collect(Collectors.groupingBy(String::length));

        // Custom object method references
        List<Person> people = Arrays.asList(
            new Person("Alice", 25),
            new Person("Bob", 30),
            new Person("Charlie", 35)
        );

        List<String> names = people.stream()
            .map(Person::getName)
            .collect(Collectors.toList());

        List<Integer> ages = people.stream()
            .map(Person::getAge)
            .collect(Collectors.toList());

        Optional<Person> oldest = people.stream()
            .max(Comparator.comparing(Person::getAge));
    }

    // Constructor references
    public void constructorReferences() {
        // Simple constructor references
        Supplier<StringBuilder> sbSupplier = StringBuilder::new;
        Supplier<ArrayList<String>> listSupplier = ArrayList::new;
        Supplier<HashSet<Integer>> setSupplier = HashSet::new;

        // Constructor with parameters
        Function<String, StringBuilder> sbWithString = StringBuilder::new;
        Function<Integer, ArrayList<String>> listWithCapacity = ArrayList::new;

        // Using constructor references in streams
        List<String> texts = Arrays.asList("hello", "world", "java");
        List<StringBuilder> builders = texts.stream()
            .map(StringBuilder::new)
            .collect(Collectors.toList());

        // Array constructor references
        Function<Integer, String[]> stringArrayConstructor = String[]::new;
        Function<Integer, int[]> intArrayConstructor = int[]::new;
        IntFunction<Object[]> objectArrayConstructor = Object[]::new;

        // Using array constructor references
        String[] textArray = texts.stream()
            .toArray(String[]::new);

        Integer[] numberArray = Stream.of(1, 2, 3, 4, 5)
            .toArray(Integer[]::new);

        // Custom class constructor references
        Function<String, Person> personConstructor = Person::new;
        BiFunction<String, Integer, Person> personWithAge = Person::new;

        List<Person> people = texts.stream()
            .map(Person::new)
            .collect(Collectors.toList());

        // Complex constructor usage
        Map<String, Person> personMap = people.stream()
            .collect(Collectors.toMap(
                Person::getName,
                Function.identity()
            ));
    }

    // Generic method references
    public void genericMethodReferences() {
        // Generic static methods
        List<String> list1 = Arrays.asList("a", "b", "c");
        List<String> list2 = Arrays.asList("d", "e", "f");

        // Collections static methods
        Collections.reverse(list1);
        Collections.sort(list2);

        // Stream static methods
        Stream<String> concatenated = Stream.concat(list1.stream(), list2.stream());

        // Optional static methods
        List<Optional<String>> optionals = Arrays.asList(
            Optional.of("value1"),
            Optional.empty(),
            Optional.of("value2")
        );

        List<String> presentValues = optionals.stream()
            .flatMap(Optional::stream)
            .collect(Collectors.toList());

        // Generic instance methods
        List<List<String>> nestedLists = Arrays.asList(list1, list2);
        List<String> flattened = nestedLists.stream()
            .flatMap(List::stream)
            .collect(Collectors.toList());

        // Map methods
        Map<String, Integer> map = Map.of("a", 1, "b", 2, "c", 3);
        Set<String> keys = map.keySet();
        Collection<Integer> values = map.values();

        List<String> keyList = map.keySet().stream()
            .collect(Collectors.toList());
    }

    // Method references in different contexts
    public void methodReferencesInContext() {
        List<String> items = Arrays.asList("apple", "banana", "cherry", "date");

        // In stream operations
        long count = items.stream()
            .filter(s -> s.length() > 4)
            .map(String::toUpperCase)
            .peek(System.out::println)
            .count();

        // In collectors
        Map<Integer, String> lengthToItem = items.stream()
            .collect(Collectors.toMap(
                String::length,
                Function.identity(),
                (existing, replacement) -> existing + ", " + replacement
            ));

        String joined = items.stream()
            .collect(Collectors.joining(", ", "[", "]"));

        // In comparators
        Comparator<String> byLength = Comparator.comparing(String::length);
        Comparator<String> byLengthThenName = Comparator
            .comparing(String::length)
            .thenComparing(String::compareTo);

        List<String> sorted = items.stream()
            .sorted(byLengthThenName)
            .collect(Collectors.toList());

        // In optional operations
        Optional<String> longest = items.stream()
            .max(Comparator.comparing(String::length));

        longest.ifPresent(System.out::println);
        String result = longest.orElseGet(() -> "No items");

        // In parallel streams
        List<String> parallelProcessed = items.parallelStream()
            .map(String::toUpperCase)
            .map(String::trim)
            .filter(s -> s.length() > 3)
            .collect(Collectors.toList());
    }

    // Helper classes and methods
    static class TextUtils {
        public static String capitalize(String text) {
            if (text == null || text.isEmpty()) {
                return text;
            }
            return text.substring(0, 1).toUpperCase() + text.substring(1).toLowerCase();
        }

        public static String reverse(String text) {
            return new StringBuilder(text).reverse().toString();
        }
    }

    static class TextProcessor {
        public String process(String text) {
            return "[PROCESSED] " + text;
        }

        public String toUpperCase(String text) {
            return text.toUpperCase();
        }

        public String addBrackets(String text) {
            return "[" + text + "]";
        }
    }

    static class Person {
        private final String name;
        private final int age;

        public Person(String name) {
            this(name, 0);
        }

        public Person(String name, int age) {
            this.name = name;
            this.age = age;
        }

        public String getName() {
            return name;
        }

        public int getAge() {
            return age;
        }

        @Override
        public String toString() {
            return name + " (" + age + ")";
        }
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_streams_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify the classes were detected
    project_name = java_streams_project.name
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    expected_classes = {
        f"{project_name}.src.main.java.com.example.MethodReferences.MethodReferences",
        f"{project_name}.src.main.java.com.example.MethodReferences.MethodReferences.TextUtils",
        f"{project_name}.src.main.java.com.example.MethodReferences.MethodReferences.TextProcessor",
        f"{project_name}.src.main.java.com.example.MethodReferences.MethodReferences.Person",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )
